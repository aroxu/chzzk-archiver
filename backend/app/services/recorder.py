"""Recording orchestration: capture directly into split fMP4 HLS storage."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx
from peewee import IntegrityError

from ..config import logger, settings
from ..db import session
from ..encoding_commands import probe_duration
from ..models import Broadcast, Channel, Entitlement, Recording, Subscription
from . import chzzk
from .credentials import user_cookies
from .downloads import download_progressive, download_progressive_aria2
from .hls_mirror import mirror_hls
from .media import (
    STORAGE_VERSION,
    directory_size,
    finalize_hls_bundle,
    generate_thumbnail,
    package_media_as_hls,
    thumbnail_path,
    valid_hls_bundle,
)
from .state import DownloadCancelled, active_processes, recording_semaphore


def ensure_recording(
    ch: Channel,
    live: dict,
    users: list[int],
    retry_states: tuple[str, ...] = ("failed",),
) -> tuple[Recording, bool]:
    if live.get("author"):
        ch.name = live["author"]
        ch.save()
    broadcast = Broadcast.get_or_none(Broadcast.channel == ch.id, Broadcast.broadcast_id == live["id"])
    created = False
    if not broadcast:
        broadcast = Broadcast.create(
            channel=ch.id,
            broadcast_id=live["id"],
            source_type="live",
            source_url=f"https://chzzk.naver.com/live/{ch.chzzk_id}",
            title=live["title"],
            category=live.get("category"),
            thumbnail_url=live.get("thumbnail"),
            started_at=datetime.now(UTC),
        )
    recording = Recording.get_or_none(Recording.broadcast == broadcast.id)
    if not recording:
        try:
            recording = Recording.create(broadcast=broadcast.id)
            created = True
        except IntegrityError:
            recording = Recording.get(Recording.broadcast == broadcast.id)
    elif recording.state in retry_states:
        Recording.update(
            state="queued", error=None, finished_at=None, started_at=None, speed_bps=0,
            eta_seconds=None, path=None, size=0, total_size=0, duration_seconds=0,
        ).where(Recording.id == recording.id).execute()
        recording = Recording.get_by_id(recording.id)
        created = True
    for uid in users:
        Entitlement.get_or_create(user=uid, recording=recording.id)
    return recording, created


def _sanitize(value: str, limit: int) -> str:
    return re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE)[:limit]


def _prepare_paths(rec: Recording) -> tuple[Path, Path]:
    safe_channel = _sanitize(rec.broadcast.channel.name, 80)
    now = datetime.now()
    folder = settings.recordings_dir / safe_channel / str(now.year) / f"{now.month:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    safe_id = _sanitize(rec.broadcast.broadcast_id, 100)
    base = f"{now:%Y%m%d-%H%M%S}-{safe_id}"
    final = folder / f"{base}.hls"
    temporary = folder / f".{base}.hls.part"
    if rec.path:
        existing = Path(rec.path)
        if existing.name == "master.m3u8":
            final = existing.parent
            temporary = final.with_name(f".{final.name}.part")
        elif existing.is_dir() and existing.name.endswith(".part"):
            temporary = existing
            final = existing.with_name(existing.name.removeprefix(".").removesuffix(".part"))
    return temporary, final


def _bundle_size(path: Path) -> int:
    return directory_size(path)


async def _download_progressive_source(
    url: str,
    destination: Path,
    cookies: dict[str, str],
    referer: str,
    recording_id: int,
    total_size: int,
) -> None:
    connections = max(1, min(16, settings.download_connections))
    if shutil.which("aria2c"):
        logger.info(
            "recording=%s accelerated download selected engine=aria2 connections=%s",
            recording_id,
            connections,
        )
        await download_progressive_aria2(
            url,
            destination,
            cookies,
            referer,
            recording_id,
            total_size,
            connections,
        )
        return
    logger.info("recording=%s accelerated downloader unavailable; using HTTP stream", recording_id)
    await asyncio.to_thread(
        download_progressive,
        url,
        destination,
        cookies,
        referer,
        recording_id,
    )


async def monitor_live_progress(
    recording_id: int,
    path: Path,
    process: asyncio.subprocess.Process,
    total_size: int = 0,
) -> None:
    previous_size = _bundle_size(path)
    previous_at = time.monotonic()
    started_size = previous_size
    started_at = previous_at
    last_growth_at: float | None = None
    while process.returncode is None:
        await asyncio.sleep(1)
        current_size = _bundle_size(path)
        now = time.monotonic()
        if current_size > previous_size:
            last_growth_at = now
        average = max(0, current_size - started_size) / max(0.001, now - started_at)
        active = last_growth_at is not None and now - last_growth_at <= 15
        eta = max(0, int((total_size - current_size) / average)) if total_size and average > 0 else None
        with session():
            updated = Recording.update(
                size=current_size,
                total_size=total_size if total_size else Recording.total_size,
                speed_bps=int(average) if active else 0,
                eta_seconds=eta if active else None,
            ).where(Recording.id == recording_id, Recording.state == "recording").execute()
        if not updated:
            return
        previous_size, previous_at = current_size, now


_SECRET_PATTERNS = (
    (re.compile(r"https?://\S+"), "[redacted-url]"),
    (re.compile(r"(NID_AUT|NID_SES)=[^;,\s\"']+", re.IGNORECASE), r"\1=[redacted]"),
    (re.compile(r"\b(key|token|inKey)=[^&;,\s\"']+", re.IGNORECASE), r"\1=[redacted]"),
)


def redact(message: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        message = pattern.sub(replacement, message)
    return message


_redact = redact
FINAL_STATES = ("canceled",)


def _finalize(recording_id: int, **fields) -> bool:
    with session():
        return bool(Recording.update(**fields).where(
            Recording.id == recording_id, Recording.state.not_in(FINAL_STATES)
        ).execute())


def _capture_command(
    playback_url: str,
    source_url: str,
    cookies: dict[str, str],
    destination: Path,
    source_type: str,
) -> list[str]:
    cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
    headers = f"User-Agent: Mozilla/5.0\r\nReferer: {source_url}\r\nAccept: application/dash+xml,application/vnd.apple.mpegurl,*/*\r\n"
    if cookie_header:
        headers += f"Cookie: {cookie_header}\r\n"
    playlist_type = "event" if source_type == "live" else "vod"
    common = [
        "-f", "hls", "-hls_time", "6", "-hls_playlist_type", playlist_type,
        "-hls_segment_type", "fmp4",
    ]
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-headers", headers, "-i", playback_url,
        "-map", "0:v:0", "-an", "-c:v", "copy", *common, "-hls_flags", "independent_segments",
        "-hls_fmp4_init_filename", "video-init.mp4", "-hls_segment_filename", "video-segment_%05d.m4s", "video.m3u8",
        "-map", "0:a:0", "-vn", "-c:a", "copy", *common,
        "-hls_fmp4_init_filename", "audio-init.mp4", "-hls_segment_filename", "audio-segment_%05d.m4s", "audio.m3u8",
    ]


async def run_recording(recording_id: int) -> None:
    async with recording_semaphore:
        with session():
            rec = Recording.get_or_none(Recording.id == recording_id)
            if not rec or rec.state != "queued":
                return
            started_at = rec.started_at or datetime.now(UTC)
            Recording.update(state="recording", started_at=started_at).where(
                Recording.id == recording_id, Recording.state == "queued"
            ).execute()
            temporary, final = _prepare_paths(rec)
            owners = [row.user_id for row in Entitlement.select().where(Entitlement.recording == rec.id)]
            cookie_candidates = user_cookies(owners) or [{}]
            source_url = rec.broadcast.source_url or f"https://chzzk.naver.com/live/{rec.broadcast.channel.chzzk_id}"
            source_type = rec.broadcast.source_type
            title = rec.broadcast.title
        shutil.rmtree(temporary, ignore_errors=True)
        temporary.mkdir(parents=True)
        if not _finalize(recording_id, path=str(temporary), size=0, storage_version=STORAGE_VERSION):
            return
        logger.info("recording=%s started type=%s title=%s", recording_id, source_type, title)
        try:
            errors: list[str] = []
            mirrored = False
            mirrored_duration = 0.0
            for cookies in cookie_candidates:
                try:
                    resolved = await asyncio.to_thread(
                        chzzk.resolve_streamlink if source_type == "live" else chzzk.resolve_direct,
                        source_url,
                        cookies,
                    )
                    playback_url = resolved["playback_url"]
                    logger.info(
                        "recording=%s source resolved protocol=%s estimated_bytes=%s",
                        recording_id, resolved.get("protocol"), resolved.get("total_size") or 0,
                    )
                except Exception as exc:
                    errors.append(str(exc))
                    logger.warning("recording=%s source resolve failed attempt=%s error=%s", recording_id, len(errors), _redact(str(exc))[-300:])
                    continue
                if resolved.get("protocol") == "hls":
                    try:
                        _, _, mirrored_duration = await mirror_hls(
                            playback_url,
                            temporary,
                            recording_id=recording_id,
                            referer=source_url,
                            cookies=cookies,
                            live=source_type == "live",
                            total_size=int(resolved.get("total_size") or 0),
                            concurrency=settings.hls_download_concurrency,
                        )
                        mirrored = True
                        break
                    except DownloadCancelled:
                        raise
                    except Exception as exc:
                        errors.append(str(exc))
                        logger.warning("recording=%s hls mirror failed attempt=%s error=%s", recording_id, len(errors), _redact(str(exc))[-300:])
                        continue
                if resolved.get("protocol") == "progressive":
                    try:
                        source = temporary / "source.mp4"
                        await _download_progressive_source(
                            playback_url,
                            source,
                            cookies,
                            source_url,
                            recording_id,
                            int(resolved.get("total_size") or 0),
                        )
                        with session():
                            Recording.update(speed_bps=0, eta_seconds=None).where(
                                Recording.id == recording_id,
                                Recording.state == "recording",
                            ).execute()
                        await asyncio.to_thread(package_media_as_hls, source, temporary)
                        mirrored = True
                        break
                    except DownloadCancelled:
                        raise
                    except Exception as exc:
                        errors.append(str(exc))
                        logger.warning(
                            "recording=%s accelerated download failed attempt=%s error=%s",
                            recording_id,
                            len(errors),
                            _redact(str(exc))[-300:],
                        )
                        continue
                command = _capture_command(playback_url, source_url, cookies, temporary, source_type)
                proc = await asyncio.create_subprocess_exec(
                    *command, cwd=temporary, stdout=subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
                )
                active_processes[recording_id] = proc
                progress = asyncio.create_task(
                    monitor_live_progress(recording_id, temporary, proc, int(resolved.get("total_size") or 0))
                )
                try:
                    _, stderr = await proc.communicate()
                finally:
                    active_processes.pop(recording_id, None)
                    progress.cancel()
                    with suppress(asyncio.CancelledError):
                        await progress
                with session():
                    canceled = Recording.get_by_id(recording_id).state == "canceled"
                if canceled:
                    raise DownloadCancelled
                if proc.returncode == 0 or (source_type == "live" and valid_hls_bundle_after_masterless(temporary)):
                    break
                errors.append(stderr.decode(errors="replace")[-1000:])
                logger.warning("recording=%s ffmpeg capture failed returncode=%s error=%s", recording_id, proc.returncode, _redact(errors[-1])[-300:])
            else:
                raise RuntimeError("; ".join(errors))
            master = temporary / "master.m3u8" if mirrored else finalize_hls_bundle(temporary)
            shutil.rmtree(final, ignore_errors=True)
            temporary.replace(final)
            master = final / master.name
            try:
                await asyncio.to_thread(generate_thumbnail, master)
            except Exception as exc:
                # A missing thumbnail must not turn an otherwise valid archive
                # into a failed capture; the API retries lazily on first view.
                logger.warning("recording=%s thumbnail generation failed error=%s", recording_id, _redact(str(exc))[-300:])
            size = directory_size(final)
            duration = mirrored_duration or await asyncio.to_thread(probe_duration, master)
            if not _finalize(
                recording_id,
                state="completed", path=str(master), size=size, total_size=size,
                speed_bps=0, eta_seconds=0, duration_seconds=duration,
                storage_version=STORAGE_VERSION, error=None, finished_at=datetime.now(UTC),
            ):
                shutil.rmtree(final, ignore_errors=True)
                return
            logger.info("recording=%s capture completed bytes=%s", recording_id, size)
        except DownloadCancelled:
            active_processes.pop(recording_id, None)
            shutil.rmtree(temporary, ignore_errors=True)
            shutil.rmtree(final, ignore_errors=True)
            with session():
                Recording.update(
                    state="canceled", path=None, size=0, total_size=0, speed_bps=0,
                    eta_seconds=None, duration_seconds=0, error=None, finished_at=datetime.now(UTC),
                ).where(Recording.id == recording_id).execute()
        except Exception as exc:
            active_processes.pop(recording_id, None)
            partial = directory_size(temporary)
            _finalize(
                recording_id, state="failed", path=str(temporary) if temporary.exists() else None,
                size=partial, speed_bps=0, eta_seconds=None,
                error=_redact(str(exc))[-1000:], finished_at=datetime.now(UTC),
            )
            logger.error("recording=%s failed error=%s", recording_id, _redact(str(exc))[-500:])


def valid_hls_bundle_after_masterless(directory: Path) -> bool:
    """A live input can end non-zero after writing complete variant playlists."""
    try:
        required = (directory / "video.m3u8", directory / "video-init.mp4", directory / "audio.m3u8", directory / "audio-init.mp4")
        return all(path.is_file() and path.stat().st_size > 0 for path in required) and bool(
            list(directory.glob("video-segment_*.m4s")) and list(directory.glob("audio-segment_*.m4s"))
        )
    except OSError:
        return False


async def monitor_live_channels_once() -> list[int]:
    with session():
        channels = [
            (row.id, row.chzzk_id)
            for row in Channel.select(Channel.id, Channel.chzzk_id)
            .join(Subscription, on=(Subscription.channel == Channel.id))
            .where(Subscription.active == True, Subscription.auto_record == True)  # noqa: E712
            .distinct()
        ]
    if not channels:
        return []
    semaphore = asyncio.Semaphore(max(1, settings.live_probe_concurrency))
    async with httpx.AsyncClient(headers={"User-Agent": "chzzk-archiver/0.1"}) as client:
        async def probe(channel_pk: int, channel_id: str):
            async with semaphore:
                return channel_pk, await chzzk.fetch_live(channel_id, client)
        results = await asyncio.gather(*(probe(pk, cid) for pk, cid in channels))
    started: list[int] = []
    for channel_pk, live in results:
        if live is chzzk.LIVE_PROBE_FAILED:
            continue
        with session():
            channel = Channel.get_or_none(Channel.id == channel_pk)
            if not channel:
                continue
            if live is None:
                if channel.last_live:
                    Channel.update(last_live=False).where(Channel.id == channel.id).execute()
                continue
            channel.last_live = True
            channel.name = live.get("author") or channel.name
            channel.image_url = live.get("channel_image") or channel.image_url
            channel.profile_backfilled = True
            channel.save()
            users = [
                row.user_id for row in Subscription.select(Subscription.user).where(
                    Subscription.channel == channel.id,
                    Subscription.active == True, Subscription.auto_record == True,  # noqa: E712
                )
            ]
            recording, created = ensure_recording(channel, live, users)
            if created:
                started.append(recording.id)
                asyncio.create_task(run_recording(recording.id))
    return started
