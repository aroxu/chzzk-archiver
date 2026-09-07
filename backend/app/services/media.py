"""HLS storage, lazy derivatives, thumbnails, and recording serialization."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from ..config import logger, settings
from ..db import session
from ..encoding_commands import probe_duration
from ..models import Recording, as_utc
from .state import active_captures, active_processes

STORAGE_VERSION = 3

_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path.resolve(), threading.Lock())


def archive_directory(media_path: Path) -> Path:
    return media_path.parent if media_path.name == "master.m3u8" else media_path.with_suffix(".hls")


def hls_directory(media_path: Path) -> Path:
    """Compatibility alias used by cleanup and delivery code."""
    return archive_directory(media_path)


def thumbnail_path(media_path: Path) -> Path:
    if media_path.name == "master.m3u8":
        return media_path.parent / "thumbnail.jpg"
    return media_path.with_suffix(".thumbnail.jpg")


def audio_asset_path(media_path: Path, audio_format: str) -> Path:
    if audio_format not in {"aac", "flac"}:
        raise ValueError(f"unsupported audio format: {audio_format}")
    if media_path.name == "master.m3u8":
        return media_path.parent / ("audio.m4a" if audio_format == "aac" else "audio.flac")
    return media_path.with_suffix(".audio.m4a" if audio_format == "aac" else ".audio.flac")


def download_path(media_path: Path) -> Path:
    return archive_directory(media_path) / "download.mp4"


def directory_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) if path.exists() else 0


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _input_options(path: Path) -> list[str]:
    # CHZZK HLS VODs commonly use .m4v media segments. FFmpeg's HLS demuxer
    # rejects that extension by default even though the segment is valid MP4.
    return (
        ["-allowed_extensions", "ALL", "-extension_picky", "0"]
        if path.suffix.lower() == ".m3u8"
        else []
    )


def valid_hls_bundle(directory: Path) -> bool:
    required = (
        directory / "master.m3u8",
        directory / "video.m3u8",
        directory / "video-init.mp4",
        directory / "audio.m3u8",
        directory / "audio-init.mp4",
    )
    try:
        split = (
            all(_nonempty(path) for path in required)
            and any(_nonempty(path) for path in directory.glob("video-segment_*.m4s"))
            and any(_nonempty(path) for path in directory.glob("audio-segment_*.m4s"))
        )
        mirrored = _nonempty(directory / "master.m3u8") and any(
            _nonempty(path)
            for pattern in ("*.m4v", "*.m4s", "*.ts")
            for path in directory.glob(pattern)
        )
        return split or mirrored
    except OSError:
        return False


def _write_master(directory: Path) -> Path:
    master = directory / "master.m3u8"
    master.write_text(
        "#EXTM3U\n"
        "#EXT-X-VERSION:7\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="AAC",DEFAULT=YES,'
        'AUTOSELECT=YES,URI="audio.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=8000000,AUDIO="audio"\n'
        "video.m3u8\n",
        encoding="utf-8",
    )
    return master


def finalize_hls_bundle(directory: Path) -> Path:
    _write_master(directory)
    for playlist in directory.glob("*.m3u8"):
        text = playlist.read_text(encoding="utf-8").replace("\\", "/")
        playlist.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    if not valid_hls_bundle(directory):
        raise RuntimeError("HLS 필수 영상/오디오 파일 검증 실패")
    return directory / "master.m3u8"


def _probe_codecs(path: Path) -> tuple[str | None, str | None]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", *_input_options(path), "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    video = next((s.get("codec_name") for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s.get("codec_name") for s in streams if s.get("codec_type") == "audio"), None)
    return video, audio


def _run_variant(source: Path, directory: Path, variant: str, *, transcode_h264: bool = False) -> None:
    common = ["-f", "hls", "-hls_time", "6", "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4"]
    if variant == "video":
        codec = (
            ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p"]
            if transcode_h264 else ["-c:v", "copy"]
        )
        mapping = ["-map", "0:v:0", "-an", *codec, *common, "-hls_flags", "independent_segments"]
    else:
        _, audio_codec = _probe_codecs(source)
        codec = ["-c:a", "copy"] if audio_codec == "aac" else ["-c:a", "aac", "-b:a", "192k"]
        mapping = ["-map", "0:a:0", "-vn", *codec, *common]
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *_input_options(source), "-i", str(source.resolve()),
            *mapping, "-hls_fmp4_init_filename", f"{variant}-init.mp4",
            "-hls_segment_filename", f"{variant}-segment_%05d.m4s", f"{variant}.m3u8",
        ],
        capture_output=True, check=False, cwd=directory,
    )
    required = (directory / f"{variant}.m3u8", directory / f"{variant}-init.mp4")
    if result.returncode or not all(_nonempty(path) for path in required) or not any(
        _nonempty(path) for path in directory.glob(f"{variant}-segment_*.m4s")
    ):
        error = result.stderr.decode(errors="replace")[-1000:]
        raise RuntimeError(f"{variant} HLS 생성 실패: {error or '필수 파일 누락'}")


def package_media_as_hls(
    source: Path,
    destination: Path,
    *,
    audio_source: Path | None = None,
    transcode_h264: bool = False,
) -> Path:
    """Package local media as split fMP4 HLS, optionally converting HEVC to H.264."""
    logger.info(
        "hls package started source=%s destination=%s transcode_h264=%s",
        source.name, destination, transcode_h264,
    )
    temporary = destination.with_name(f".{destination.name}.{threading.get_ident()}.part")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True)
    try:
        _run_variant(source, temporary, "video", transcode_h264=transcode_h264)
        _run_variant(audio_source or source, temporary, "audio")
        finalize_hls_bundle(temporary)
        shutil.rmtree(destination, ignore_errors=True)
        temporary.replace(destination)
        logger.info("hls package completed destination=%s bytes=%s", destination, directory_size(destination))
        return destination / "master.m3u8"
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def generate_hls_bundle(video_path: Path, aac_path: Path) -> Path:
    """Compatibility helper for a legacy split-video archive."""
    destination = archive_directory(video_path)
    with _lock(destination):
        if valid_hls_bundle(destination):
            return destination / "master.m3u8"
        return package_media_as_hls(video_path, destination, audio_source=aac_path)


def generate_flac_asset(media_path: Path) -> Path:
    """Create the optional maximum-compression 24-bit FLAC radio asset once."""
    destination = audio_asset_path(media_path, "flac")
    with _lock(destination):
        if _nonempty(destination):
            logger.info("flac asset cache hit path=%s bytes=%s", destination, destination.stat().st_size)
            return destination
        logger.info("flac asset generation started source=%s destination=%s", media_path, destination)
        source = archive_directory(media_path) / "audio.m3u8" if media_path.name == "master.m3u8" else media_path
        temporary = destination.with_name(f".{destination.name}.{threading.get_ident()}.part")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *_input_options(source), "-i", str(source),
                    "-map", "0:a:0", "-vn", "-c:a", "flac", "-compression_level", "12",
                    "-sample_fmt", "s32", "-bits_per_raw_sample", "24", "-f", "flac", str(temporary),
                ],
                capture_output=True, check=True,
            )
            if not _nonempty(temporary):
                raise RuntimeError("ffmpeg did not create FLAC audio")
            temporary.replace(destination)
            logger.info("flac asset generation completed path=%s bytes=%s", destination, destination.stat().st_size)
            return destination
        finally:
            temporary.unlink(missing_ok=True)


def generate_aac_hls(media_path: Path) -> Path:
    """Create a cached audio-only HLS rendition by copying the AAC stream."""
    directory = archive_directory(media_path)
    destination = directory / "audio.m3u8"
    with _lock(destination):
        if _nonempty(destination) and any(_nonempty(path) for path in directory.glob("audio-segment_*.m4s")):
            return destination
        token = str(threading.get_ident())
        temporary = directory / f".audio-{token}.m3u8"
        init_name = f".audio-{token}-init.mp4"
        segment_pattern = directory / f".audio-{token}-segment_%05d.m4s"
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *_input_options(media_path), "-i", str(media_path),
                    "-map", "0:a:0", "-vn", "-c:a", "copy", "-f", "hls", "-hls_time", "6",
                    "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4",
                    "-hls_fmp4_init_filename", init_name,
                    "-hls_segment_filename", str(segment_pattern), str(temporary),
                ],
                capture_output=True, check=False, cwd=directory,
            )
            generated_init = directory / init_name
            generated_segments = sorted(directory.glob(f".audio-{token}-segment_*.m4s"))
            if result.returncode or not _nonempty(temporary) or not _nonempty(generated_init) or not generated_segments:
                raise RuntimeError(result.stderr.decode(errors="replace")[-1000:])
            final_init = directory / "audio-init.mp4"
            generated_init.replace(final_init)
            replacements = {init_name: final_init.name}
            for index, segment in enumerate(generated_segments):
                final_segment = directory / f"audio-segment_{index:05d}.m4s"
                replacements[segment.name] = final_segment.name
                segment.replace(final_segment)
            text = temporary.read_text(encoding="utf-8")
            for old, new in replacements.items():
                text = text.replace(old, new)
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)
            (directory / init_name).unlink(missing_ok=True)
            for path in directory.glob(f".audio-{token}-segment_*.m4s"):
                path.unlink(missing_ok=True)


def generate_download_mp4(media_path: Path) -> Path:
    """Create and cache a seekable MP4 download using stream-copy only."""
    destination = download_path(media_path)
    with _lock(destination):
        if _nonempty(destination):
            logger.info("mp4 download cache hit path=%s bytes=%s", destination, destination.stat().st_size)
            return destination
        source = media_path if media_path.name == "master.m3u8" else archive_directory(media_path) / "master.m3u8"
        temporary = destination.with_name(f".{destination.name}.{threading.get_ident()}.part.mp4")
        logger.info("mp4 stream-copy remux started source=%s destination=%s", source, destination)
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *_input_options(source), "-i", str(source),
                    "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-movflags", "+faststart", str(temporary),
                ],
                capture_output=True, check=False,
            )
            if result.returncode or not _nonempty(temporary):
                raise RuntimeError(result.stderr.decode(errors="replace")[-1000:])
            temporary.replace(destination)
            logger.info("mp4 stream-copy remux completed path=%s bytes=%s", destination, destination.stat().st_size)
            return destination
        finally:
            temporary.unlink(missing_ok=True)


def generate_audio_assets(video_path: Path, source_path: Path | None = None) -> dict[str, Path]:
    """Legacy API: retain existing AAC and lazily create only FLAC."""
    aac = audio_asset_path(video_path, "aac")
    if source_path and not _nonempty(aac):
        temporary = aac.with_name(f".{aac.name}.{threading.get_ident()}.part.m4a")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", *_input_options(source_path), "-i", str(source_path), "-map", "0:a:0", "-vn", "-c:a", "aac", "-b:a", "192k", str(temporary)],
                capture_output=True, check=True,
            )
            temporary.replace(aac)
        finally:
            temporary.unlink(missing_ok=True)
    return {"aac": aac, "flac": generate_flac_asset(video_path)}


def migrate_legacy_recording(recording_id: int) -> Path:
    """Upgrade an old archive to v3 split HLS without eager FLAC/download files.

    storage v2 used HEVC video. Those videos are explicitly converted to H.264
    for broad HLS playback; H.264 sources remain a stream-copy operation.
    """
    with session():
        recording = Recording.get_by_id(recording_id)
        if recording.state != "completed" or not recording.path:
            raise RuntimeError("완료된 기존 아카이브가 아닙니다")
        source = Path(recording.path)
        old_version = recording.storage_version
        if old_version >= STORAGE_VERSION and source.name == "master.m3u8" and valid_hls_bundle(source.parent):
            return source
    if not source.is_file():
        raise RuntimeError("기존 아카이브 파일이 없습니다")
    root = settings.recordings_dir.resolve()
    if not source.resolve().is_relative_to(root):
        raise RuntimeError("녹화 폴더 밖의 파일은 마이그레이션할 수 없습니다")
    video_codec, audio_codec = _probe_codecs(source)
    if not video_codec:
        raise RuntimeError("기존 아카이브에 비디오 스트림이 없습니다")
    legacy_aac = audio_asset_path(source, "aac")
    audio_source = legacy_aac if _nonempty(legacy_aac) else source
    if not audio_codec and not _nonempty(legacy_aac):
        raise RuntimeError("기존 아카이브에 오디오 스트림이 없습니다")
    destination = source.with_suffix(".hls")
    required_free = max(512 * 1024 * 1024, int(source.stat().st_size * (1.5 if old_version == 2 else 1.1)))
    if shutil.disk_usage(destination.parent).free < required_free:
        raise RuntimeError(f"마이그레이션 임시 공간 부족: 필요 {required_free} bytes")
    transcode_h264 = old_version == 2 and video_codec in {"hevc", "h265"}
    logger.info(
        "legacy migration started recording=%s version=%s video_codec=%s audio_codec=%s transcode_h264=%s",
        recording_id, old_version, video_codec, audio_codec, transcode_h264,
    )
    master = package_media_as_hls(source, destination, audio_source=audio_source, transcode_h264=transcode_h264)
    old_thumbnail = thumbnail_path(source)
    new_thumbnail = thumbnail_path(master)
    if old_thumbnail.is_file() and not new_thumbnail.exists():
        shutil.copy2(old_thumbnail, new_thumbnail)
    if not new_thumbnail.exists():
        with suppress(Exception):
            generate_thumbnail(master)
    size = directory_size(destination)
    duration = probe_duration(master)
    with session():
        updated = Recording.update(
            path=str(master), size=size, total_size=size, duration_seconds=duration,
            storage_version=STORAGE_VERSION,
        ).where(Recording.id == recording_id, Recording.state == "completed").execute()
    if not updated:
        shutil.rmtree(destination, ignore_errors=True)
        raise RuntimeError("마이그레이션 중 아카이브 상태가 변경되었습니다")
    source.unlink(missing_ok=True)
    old_thumbnail.unlink(missing_ok=True)
    legacy_aac.unlink(missing_ok=True)
    audio_asset_path(source, "flac").unlink(missing_ok=True)
    logger.info(
        "legacy migration completed recording=%s version=%s path=%s bytes=%s",
        recording_id, STORAGE_VERSION, master, size,
    )
    return master


def generate_thumbnail(media_path: Path) -> Path:
    duration = probe_duration(media_path)
    destination = thumbnail_path(media_path)
    temporary = destination.with_name(f".{destination.name}.tmp.jpg")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        seek = max(0.0, duration / 2)
        commands = [
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{seek:.3f}",
                *_input_options(media_path), "-i", str(media_path), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", str(temporary),
            ],
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *_input_options(media_path),
                "-i", str(media_path), "-ss", f"{seek:.3f}", "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", str(temporary),
            ],
            [
                # A freshly mirrored/live HLS playlist may advertise the full
                # duration while only its first segment is immediately usable.
                # Fall back to the first decodable frame instead of returning
                # 422 when midpoint seeking lands beyond the available data.
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *_input_options(media_path),
                "-i", str(media_path), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", str(temporary),
            ],
        ]
        last_error = ""
        for command in commands:
            result = subprocess.run(command, capture_output=True, check=False)
            if result.returncode == 0 and _nonempty(temporary):
                break
            last_error = result.stderr.decode(errors="replace").strip()
            temporary.unlink(missing_ok=True)
        else:
            raise RuntimeError(last_error[-1000:] or "ffmpeg did not create a thumbnail")
        if not _nonempty(temporary):
            raise RuntimeError("ffmpeg did not create a thumbnail")
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def recording_json(r: Recording) -> dict:
    reported_size = r.size
    if r.state == "recording" and r.path:
        with suppress(OSError):
            reported_size = directory_size(Path(r.path))
    progress = min(100.0, round(reported_size / r.total_size * 100, 1)) if r.total_size else None
    process = active_processes.get(r.id)
    started_at = as_utc(r.started_at)
    recorded_seconds = max(0, int(r.duration_seconds or 0))
    if not recorded_seconds and started_at and r.state in {"queued", "recording", "interrupted"}:
        recorded_seconds = max(0, int(((as_utc(r.finished_at) or datetime.now(UTC)) - started_at).total_seconds()))
    broadcast = r.broadcast
    return {
        "id": r.id, "state": r.state, "type": broadcast.source_type, "title": broadcast.title,
        "channel": broadcast.channel.name, "channel_id": broadcast.channel.chzzk_id,
        # Completed archives can lazily create a thumbnail on first request.
        # Do not permanently hide the URL after a transient FFmpeg failure.
        "thumbnail": f"/api/thumbnails/{r.id}" if r.path and r.state == "completed" else None,
        "size": reported_size, "total_size": r.total_size, "progress": progress,
        "speed_bps": r.speed_bps, "eta_seconds": r.eta_seconds,
        "recorded_seconds": recorded_seconds, "duration_seconds": max(0, float(r.duration_seconds or 0)),
        "recording_active": r.id in active_captures or bool(process and process.returncode is None),
        "created_at": as_utc(r.created_at), "finished_at": as_utc(r.finished_at), "error": r.error,
    }
