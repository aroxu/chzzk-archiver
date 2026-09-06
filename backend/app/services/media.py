"""Thumbnail generation and recording serialization."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from ..config import settings
from ..db import session
from ..encoding_commands import probe_duration
from ..models import EncodingJob, Recording, as_utc
from .state import active_processes

_audio_locks: dict[Path, threading.Lock] = {}
_audio_locks_guard = threading.Lock()
_hls_locks: dict[Path, threading.Lock] = {}
_hls_locks_guard = threading.Lock()


def thumbnail_path(video_path: Path) -> Path:
    return video_path.with_suffix(".thumbnail.jpg")


def audio_asset_path(video_path: Path, audio_format: str) -> Path:
    """Return the separately stored AAC or FLAC companion path."""
    if audio_format == "aac":
        return video_path.with_suffix(".audio.m4a")
    if audio_format == "flac":
        return video_path.with_suffix(".audio.flac")
    raise ValueError(f"unsupported audio format: {audio_format}")


def generate_audio_assets(video_path: Path, source_path: Path | None = None) -> dict[str, Path]:
    """Create both independently seekable audio assets and reuse fresh ones."""
    source = source_path or video_path
    destinations = {audio_format: audio_asset_path(video_path, audio_format) for audio_format in ("aac", "flac")}
    with _audio_locks_guard:
        lock = _audio_locks.setdefault(video_path, threading.Lock())
    with lock:
        for audio_format, destination in destinations.items():
            if source_path is None and destination.exists() and destination.stat().st_size > 0:
                continue
            temporary = destination.with_name(f".{destination.name}.{threading.get_ident()}.part")
            codec = (
                ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "mp4"]
                if audio_format == "aac"
                else [
                    "-c:a",
                    "flac",
                    "-compression_level",
                    "12",
                    "-sample_fmt",
                    "s32",
                    "-bits_per_raw_sample",
                    "24",
                    "-f",
                    "flac",
                ]
            )
            try:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(source),
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-sn",
                        "-dn",
                        *codec,
                        str(temporary),
                    ],
                    capture_output=True,
                    check=True,
                )
                if not temporary.exists() or temporary.stat().st_size == 0:
                    raise RuntimeError(f"ffmpeg did not create {audio_format} audio")
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        return destinations


def hls_directory(video_path: Path) -> Path:
    return video_path.with_suffix(".hls")


def generate_hls_bundle(video_path: Path, aac_path: Path) -> Path:
    """Package the split video and AAC delivery track as VOD HLS."""
    destination = hls_directory(video_path)
    master = destination / "master.m3u8"
    with _hls_locks_guard:
        lock = _hls_locks.setdefault(video_path, threading.Lock())
    with lock:
        newest_source = max(video_path.stat().st_mtime_ns, aac_path.stat().st_mtime_ns)
        if master.exists() and master.stat().st_size > 0 and master.stat().st_mtime_ns >= newest_source:
            return master
        temporary = destination.with_name(f".{destination.name}.{threading.get_ident()}.part")
        shutil.rmtree(temporary, ignore_errors=True)
        (temporary / "video").mkdir(parents=True)
        (temporary / "audio").mkdir(parents=True)
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(video_path),
                    "-i",
                    str(aac_path),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "copy",
                    "-f",
                    "hls",
                    "-hls_time",
                    "6",
                    "-hls_playlist_type",
                    "vod",
                    "-hls_segment_type",
                    "fmp4",
                    "-hls_fmp4_init_filename",
                    str(temporary / "%v" / "init.mp4"),
                    "-hls_flags",
                    "independent_segments",
                    "-master_pl_name",
                    "master.m3u8",
                    "-var_stream_map",
                    "v:0,agroup:audio,name:video a:0,agroup:audio,default:yes,name:audio",
                    "-hls_segment_filename",
                    str(temporary / "%v" / "segment_%05d.m4s"),
                    str(temporary / "%v" / "index.m3u8"),
                ],
                capture_output=True,
                check=False,
            )
            generated_master = temporary / "master.m3u8"
            if result.returncode != 0 or not generated_master.exists():
                raise RuntimeError(result.stderr.decode(errors="replace")[-1000:])
            # FFmpeg emits native Windows separators and may embed the temporary
            # directory in EXT-X-MAP.  HLS URIs are URLs, and the temporary
            # directory is renamed atomically below, so keep every reference
            # relative to its final playlist.
            for playlist in temporary.rglob("*.m3u8"):
                lines = []
                for line in playlist.read_text(encoding="utf-8").splitlines():
                    if line.startswith("#EXT-X-MAP:"):
                        line = '#EXT-X-MAP:URI="init.mp4"'
                    lines.append(line.replace("\\", "/"))
                playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
            shutil.rmtree(destination, ignore_errors=True)
            temporary.replace(destination)
            return master
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


def _stream_codecs(path: Path) -> tuple[str | None, bool]:
    """Return the first video codec and whether an audio stream exists."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    video = next((item.get("codec_name") for item in streams if item.get("codec_type") == "video"), None)
    return video, any(item.get("codec_type") == "audio" for item in streams)


def migrate_legacy_recording(recording_id: int) -> Path:
    """Convert one completed legacy archive to split v2 storage.

    The combined source is kept until both audio assets and the HLS bundle are
    complete.  The final video swap is atomic, so an interrupted migration is
    safely retried on the next startup.
    """
    with session():
        recording = Recording.get_by_id(recording_id)
        if recording.state != "completed" or not recording.path:
            raise RuntimeError("완료된 기존 아카이브가 아닙니다")
        if recording.storage_version >= 2:
            return Path(recording.path)
        source = Path(recording.path)
    if not source.is_file():
        raise RuntimeError("기존 아카이브 파일이 없습니다")
    root = settings.recordings_dir.resolve()
    resolved_source = source.resolve()
    if not resolved_source.is_relative_to(root):
        raise RuntimeError("녹화 폴더 밖의 파일은 마이그레이션할 수 없습니다")

    final = source.with_suffix(".mp4")
    video_codec, has_audio = _stream_codecs(source)
    if not video_codec:
        raise RuntimeError("기존 아카이브에 비디오 스트림이 없습니다")
    required_free = max(512 * 1024 * 1024, int(source.stat().st_size * 2.2))
    available_free = shutil.disk_usage(final.parent).free
    if available_free < required_free:
        raise RuntimeError(
            f"마이그레이션 임시 공간 부족: 필요 {required_free} bytes, 사용 가능 {available_free} bytes"
        )
    audio_assets = {audio_format: audio_asset_path(final, audio_format) for audio_format in ("aac", "flac")}
    if has_audio:
        generate_audio_assets(final, source_path=source)
    elif not all(path.is_file() and path.stat().st_size > 0 for path in audio_assets.values()):
        raise RuntimeError("오디오가 없는 기존 아카이브에는 라디오 트랙을 만들 수 없습니다")

    video_temporary: Path | None = None
    staged_hls: Path | None = None
    try:
        if source.suffix.lower() == ".mp4" and not has_audio:
            video_ready = source
        else:
            video_temporary = final.with_name(f".{final.name}.legacy-{recording_id}.part.mp4")
            command = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-an",
                "-c:v",
                "copy",
            ]
            if video_codec in {"hevc", "h265"}:
                command += ["-tag:v", "hvc1"]
            command += ["-movflags", "+faststart", str(video_temporary)]
            result = subprocess.run(command, capture_output=True, check=False)
            if result.returncode != 0 or not video_temporary.is_file() or video_temporary.stat().st_size == 0:
                raise RuntimeError(result.stderr.decode(errors="replace")[-1000:])
            video_ready = video_temporary

        generated_hls = generate_hls_bundle(video_ready, audio_assets["aac"])
        staged_hls = generated_hls.parent
        final_hls = hls_directory(final)
        if staged_hls != final_hls:
            shutil.rmtree(final_hls, ignore_errors=True)
            staged_hls.replace(final_hls)
            staged_hls = None
        if video_temporary:
            video_temporary.replace(final)
            video_temporary = None

        old_thumbnail = thumbnail_path(source)
        final_thumbnail = thumbnail_path(final)
        if source != final and old_thumbnail.is_file() and not final_thumbnail.exists():
            old_thumbnail.replace(final_thumbnail)
        if not final_thumbnail.exists():
            try:
                generate_thumbnail(final)
            except Exception:
                pass
        hls_size = sum(path.stat().st_size for path in final_hls.rglob("*") if path.is_file())
        total_size = final.stat().st_size + sum(path.stat().st_size for path in audio_assets.values()) + hls_size
        duration = probe_duration(final)
        with session():
            updated = Recording.update(
                path=str(final),
                size=total_size,
                total_size=total_size,
                duration_seconds=duration,
                storage_version=2,
            ).where(Recording.id == recording_id, Recording.state == "completed").execute()
        if not updated:
            raise RuntimeError("마이그레이션 중 아카이브 상태가 변경되었습니다")
        # For TS/MKV sources, update the durable pointer before removing the
        # old file. A crash between these steps leaves only a harmless stale
        # transport that startup cleanup can safely remove.
        if source != final:
            source.unlink(missing_ok=True)
        return final
    finally:
        if video_temporary:
            video_temporary.unlink(missing_ok=True)
        if staged_hls and staged_hls != hls_directory(final):
            shutil.rmtree(staged_hls, ignore_errors=True)


def generate_thumbnail(video_path: Path) -> Path:
    """Create a 640px-wide JPEG from the middle of a completed video."""
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(video_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    # Some streamed containers carry no duration in their header, so ffprobe
    # answers "N/A". Grab an early frame instead of failing the thumbnail.
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        duration = 0.0
    seek = max(0.0, duration / 2)
    destination = thumbnail_path(video_path)
    temporary = destination.with_name(f".{destination.name}.tmp.jpg")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-ss", f"{seek:.3f}",
                "-i", str(video_path), "-frames:v", "1", "-vf", "scale=640:-2",
                "-q:v", "3", str(temporary),
            ],
            capture_output=True,
            check=True,
        )
        if not temporary.exists() or temporary.stat().st_size == 0:
            raise RuntimeError("ffmpeg did not create a thumbnail")
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def recording_json(r: Recording) -> dict:
    reported_size = r.size
    if r.state == "recording" and r.path:
        with suppress(OSError):
            reported_size = Path(r.path).stat().st_size
    progress = round(reported_size / r.total_size * 100, 1) if r.total_size else None
    process = active_processes.get(r.id)
    process_active = bool(process and process.returncode is None)
    recorded_seconds = max(0, int(r.duration_seconds or 0))
    started_at = as_utc(r.started_at)
    if not recorded_seconds and started_at and r.state in {"queued", "recording", "interrupted"}:
        finished_at = as_utc(r.finished_at) or datetime.now(UTC)
        recorded_seconds = max(0, int((finished_at - started_at).total_seconds()))
    thumbnail = f"/api/thumbnails/{r.id}" if r.path and thumbnail_path(Path(r.path)).exists() else None
    broadcast = r.broadcast
    channel = broadcast.channel
    encoding = None
    if r.state == "processing":
        encoding = EncodingJob.get_or_none(EncodingJob.recording == r.id)
    return {
        "id": r.id,
        "state": r.state,
        "type": broadcast.source_type,
        "title": broadcast.title,
        "channel": channel.name,
        "channel_id": channel.chzzk_id,
        "thumbnail": thumbnail,
        "size": reported_size,
        "total_size": r.total_size,
        "progress": progress,
        "speed_bps": r.speed_bps,
        "eta_seconds": r.eta_seconds,
        "encoding_progress": encoding.progress if encoding else None,
        "encoding_speed": encoding.encoding_speed if encoding else None,
        "encoding_eta_seconds": encoding.eta_seconds if encoding else None,
        "encoding_processed_seconds": encoding.processed_seconds if encoding else None,
        "encoding_state": encoding.state if encoding else None,
        "recorded_seconds": recorded_seconds,
        "duration_seconds": max(0, float(r.duration_seconds or 0)),
        "recording_active": process_active,
        "created_at": as_utc(r.created_at),
        "finished_at": as_utc(r.finished_at),
        "error": r.error,
    }
