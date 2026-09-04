"""Recording orchestration: dedupe, capture, remux and lifecycle bookkeeping."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx
from peewee import IntegrityError

from ..config import logger, settings
from ..db import session
from ..models import Broadcast, Channel, EncodingJob, Entitlement, Recording, Subscription
from . import chzzk
from .credentials import user_cookies
from .downloads import cancel_requested, download_progressive, download_progressive_aria2
from .encoding import enqueue_encoding, process_local_job
from .media import generate_thumbnail, thumbnail_path
from .state import DownloadCancelled, active_processes, recording_semaphore


def ensure_recording(
    ch: Channel,
    live: dict,
    users: list[int],
    retry_states: tuple[str, ...] = ("failed",),
) -> tuple[Recording, bool]:
    """Create or reuse the single recording row shared by all subscribers.

    ``retry_states`` lists the terminal states a caller is willing to restart.
    Automatic polling only retries failures; a user asking to record on demand
    also revives a capture they previously canceled.
    """
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
            broadcast = Broadcast.get(Broadcast.channel == ch.id, Broadcast.broadcast_id == live["id"])
            recording = Recording.get(Recording.broadcast == broadcast.id)
    elif recording.state in retry_states:
        recording.state = "queued"
        recording.error = None
        recording.finished_at = None
        recording.started_at = None
        recording.speed_bps = 0
        recording.eta_seconds = None
        recording.path = None
        recording.size = 0
        recording.total_size = 0
        recording.save()
        created = True
    for uid in users:
        if not Entitlement.get_or_none(Entitlement.user == uid, Entitlement.recording == recording.id):
            Entitlement.create(user=uid, recording=recording.id)
    return recording, created


async def monitor_live_progress(
    recording_id: int,
    path: Path,
    process: asyncio.subprocess.Process,
    total_size: int = 0,
) -> None:
    """Track file growth for live and FFmpeg-based segmented captures."""
    previous_size = path.stat().st_size if path.exists() else 0
    previous_at = time.monotonic()
    monitor_start_size = previous_size
    monitor_started_at = previous_at
    last_growth_at: float | None = None
    while process.returncode is None:
        await asyncio.sleep(1)
        current_size = path.stat().st_size if path.exists() else 0
        now = time.monotonic()
        elapsed = max(0.001, now - previous_at)
        instant_speed = max(0, current_size - previous_size) / elapsed
        if instant_speed > 0:
            last_growth_at = now
        average_speed = max(0, current_size - monitor_start_size) / max(0.001, now - monitor_started_at)
        recently_writing = last_growth_at is not None and now - last_growth_at <= 15
        eta_seconds = (
            max(0, int((total_size - current_size) / average_speed))
            if total_size and average_speed > 0
            else None
        )
        with session():
            updated = (
                Recording.update(
                    size=current_size,
                    total_size=total_size if total_size else Recording.total_size,
                    speed_bps=int(average_speed) if recently_writing else 0,
                    eta_seconds=eta_seconds if recently_writing else None,
                )
                .where(Recording.id == recording_id, Recording.state == "recording")
                .execute()
            )
        if not updated:
            return
        previous_size = current_size
        previous_at = now


def _sanitize(value: str, limit: int) -> str:
    return re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE)[:limit]


def _prepare_paths(rec: Recording) -> tuple[Path, Path]:
    """Resolve the .ts staging path and .mp4 destination, resuming partials."""
    safe = _sanitize(rec.broadcast.channel.name, 80)
    folder = settings.recordings_dir / safe / str(datetime.now().year) / f"{datetime.now().month:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    safe_broadcast_id = _sanitize(rec.broadcast.broadcast_id, 100)
    base = f"{datetime.now():%Y%m%d-%H%M%S}-{safe_broadcast_id}"
    temp, final = folder / f"{base}.ts", folder / f"{base}.mp4"
    partials = list(folder.glob(f"*-{safe_broadcast_id}.ts"))
    if rec.path and rec.path.endswith(".ts") and Path(rec.path).exists():
        partials.append(Path(rec.path))
    if partials:
        temp = max(set(partials), key=lambda path: path.stat().st_size)
        final = temp.with_suffix(".mp4")
    return temp, final


_SECRET_PATTERNS = (
    (re.compile(r"https?://\S+"), "[redacted-url]"),
    (re.compile(r"(NID_AUT|NID_SES)=[^;,\s\"']+", re.IGNORECASE), r"\1=[redacted]"),
    (re.compile(r"\b(key|token|inKey)=[^&;,\s\"']+", re.IGNORECASE), r"\1=[redacted]"),
)


def redact(message: str) -> str:
    """Strip URLs, session cookies and signing keys before surfacing an error.

    Recording errors are stored on a row that every entitled user can read, and
    a recording is shared between subscribers, so one user's CHZZK cookies must
    never leak through another user's library.
    """
    for pattern, replacement in _SECRET_PATTERNS:
        message = pattern.sub(replacement, message)
    return message


_redact = redact


#: A user cancel is terminal. Workers finishing later must not overwrite it.
FINAL_STATES = ("canceled",)


def _finalize(recording_id: int, **fields) -> bool:
    """Write a terminal state unless the user already canceled the recording.

    Peewee's ``Model.save()`` issues an UPDATE for every column, so a worker
    holding a row it read earlier would happily resurrect a canceled recording.
    Scoping the UPDATE with a state predicate makes the transition atomic.
    """
    with session():
        return bool(
            Recording.update(**fields)
            .where(Recording.id == recording_id, Recording.state.not_in(FINAL_STATES))
            .execute()
        )


async def run_recording(recording_id: int) -> None:
    async with recording_semaphore:
        with session():
            rec = Recording.get_or_none(Recording.id == recording_id)
            if not rec or rec.state != "queued":
                return
            rec.state = "recording"
            if not rec.started_at:
                rec.started_at = datetime.now(UTC)
            Recording.update(state="recording", started_at=rec.started_at).where(
                Recording.id == recording_id, Recording.state == "queued"
            ).execute()
            temp, final = _prepare_paths(rec)
            owners = [row.user_id for row in Entitlement.select().where(Entitlement.recording == rec.id)]
            cookie_candidates = user_cookies(owners) or [{}]
            url = rec.broadcast.source_url or f"https://chzzk.naver.com/live/{rec.broadcast.channel.chzzk_id}"
            source_type = rec.broadcast.source_type
            title = rec.broadcast.title
        if not _finalize(
            recording_id,
            path=str(temp),
            size=temp.stat().st_size if temp.exists() else 0,
        ):
            logger.info("recording=%s canceled before capture started", recording_id)
            return
        remote_job = None
        if settings.encoding_mode == "remote" and source_type == "live":
            # The TCP worker may start consuming this growing transport stream
            # immediately. VOD/DASH inputs are not safe to stream while growing:
            # fragmented MP4 needs its initialization/index data first, so those
            # jobs are queued only after the local download and remux finish.
            remote_job = enqueue_encoding(recording_id, temp, mark_processing=False)
        logger.info("recording=%s started type=%s title=%s", recording_id, source_type, title)
        try:
            errors: list[str] = []
            for cookies in cookie_candidates:
                capture_total_size = 0
                output_handle = None
                stdout_target = subprocess.DEVNULL
                if source_type == "live":
                    args = ["streamlink", "--stdout"]
                    for key, value in cookies.items():
                        args += ["--http-cookie", f"{key}={value}"]
                    args += [url, "1080p60,1080p,best"]
                    output_handle = temp.open("ab")
                    stdout_target = output_handle
                else:
                    try:
                        direct = await asyncio.to_thread(chzzk.resolve_direct, url, cookies)
                    except DownloadCancelled:
                        raise
                    except Exception as exc:
                        errors.append(str(exc))
                        continue
                    if direct["protocol"] == "progressive":
                        try:
                            if shutil.which("aria2c"):
                                logger.info(
                                    "recording=%s using aria2c connections=%s",
                                    recording_id,
                                    max(1, min(16, settings.download_connections)),
                                )
                                await download_progressive_aria2(
                                    direct["playback_url"], temp, cookies, url, recording_id, direct.get("total_size", 0)
                                )
                            else:
                                await asyncio.to_thread(
                                    download_progressive, direct["playback_url"], temp, cookies, url, recording_id
                                )
                            break
                        except DownloadCancelled:
                            raise
                        except Exception as exc:
                            errors.append(str(exc))
                            continue
                    capture_total_size = int(direct.get("total_size") or 0)
                    temp.unlink(missing_ok=True)
                    if direct["protocol"] == "hls":
                        segment_threads = max(1, min(10, settings.download_segment_threads))
                        args = [
                            sys.executable, "-m", "streamlink", "--stdout",
                            "--stream-segment-threads", str(segment_threads),
                            "--http-header", f"Referer={url}",
                        ]
                        for key, value in cookies.items():
                            args += ["--http-cookie", f"{key}={value}"]
                        args += [f"hls://{direct['playback_url']}", "best"]
                        output_handle = temp.open("wb")
                        stdout_target = output_handle
                        logger.info(
                            "recording=%s using Streamlink HLS segment_threads=%s",
                            recording_id,
                            segment_threads,
                        )
                    else:
                        cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
                        headers = f"Referer: {url}\r\nAccept: application/dash+xml,application/vnd.apple.mpegurl,*/*\r\n"
                        if cookie_header:
                            headers += f"Cookie: {cookie_header}\r\n"
                        args = [
                            "ffmpeg", "-y", "-loglevel", "warning", "-user_agent", "Mozilla/5.0",
                            "-headers", headers,
                            # CHZZK uses CMAF/fMP4 media with .m4v segment names. New
                            # FFmpeg releases reject the extension/MIME mismatch by
                            # default unless the HLS demuxer is put in compatibility mode.
                            "-extension_picky", "false",
                            "-i", direct["playback_url"], "-c", "copy", "-f", "mpegts", str(temp),
                        ]
                progress_task = None
                try:
                    proc = await asyncio.create_subprocess_exec(*args, stdout=stdout_target, stderr=asyncio.subprocess.PIPE)
                    active_processes[recording_id] = proc
                    # Both Streamlink live captures and FFmpeg DASH/HLS captures
                    # write a growing transport stream. Progressive HTTP/aria2
                    # downloads report their own byte counters and break above.
                    progress_task = asyncio.create_task(
                        monitor_live_progress(recording_id, temp, proc, capture_total_size)
                    )
                    _, err = await proc.communicate()
                finally:
                    active_processes.pop(recording_id, None)
                    if progress_task:
                        progress_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await progress_task
                    if output_handle:
                        output_handle.close()
                if cancel_requested(recording_id):
                    raise DownloadCancelled
                if proc.returncode == 0:
                    if source_type == "live":
                        _finalize(
                            recording_id,
                            size=temp.stat().st_size if temp.exists() else 0,
                            speed_bps=0,
                        )
                    break
                errors.append(err.decode(errors="replace")[-500:])
            else:
                # A live capture that already wrote bytes is worth keeping: the
                # broadcast usually ended, so retrying can never succeed and
                # discarding the footage would lose hours of recording.
                salvage = source_type == "live" and temp.exists() and temp.stat().st_size > 0
                if not salvage:
                    raise RuntimeError("; ".join(errors))
                logger.warning(
                    "recording=%s stream ended; salvaging %s bytes error=%s",
                    recording_id,
                    temp.stat().st_size,
                    _redact("; ".join(errors))[-300:],
                )
            remux = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(temp), "-c", "copy", "-movflags", "+faststart", str(final),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            active_processes[recording_id] = remux
            _, err = await remux.communicate()
            active_processes.pop(recording_id, None)
            if cancel_requested(recording_id):
                raise DownloadCancelled
            if remux.returncode != 0:
                raise RuntimeError(err.decode(errors="replace")[-1000:])
            if settings.encoding_mode != "remote":
                temp.unlink(missing_ok=True)
            try:
                await asyncio.to_thread(generate_thumbnail, final)
            except Exception as exc:
                logger.warning("thumbnail generation failed recording=%s error=%s", recording_id, str(exc)[:200])
            completed_size = final.stat().st_size
            target_state = "completed" if settings.encoding_mode == "disabled" else "processing"
            if not _finalize(
                recording_id,
                state=target_state,
                path=str(final),
                size=completed_size,
                total_size=completed_size,
                speed_bps=0,
                eta_seconds=0,
                finished_at=datetime.now(UTC),
            ):
                # Cancelled while we were finishing: drop the artefacts we just made.
                final.unlink(missing_ok=True)
                thumbnail_path(final).unlink(missing_ok=True)
                logger.info("recording=%s completed but was canceled; artefacts removed", recording_id)
                return
            if settings.encoding_mode == "local":
                job = enqueue_encoding(recording_id, final)
                if job:
                    await process_local_job(job.id)
            elif settings.encoding_mode == "remote":
                if remote_job is None:
                    # VOD and clip files become streamable only after +faststart
                    # remuxing has put MP4 metadata at the front of the file.
                    remote_job = enqueue_encoding(recording_id, final)
                else:
                    # A worker could exhaust its retries before a long capture
                    # finished. In that case publish the safe original now.
                    with session():
                        current_job = EncodingJob.get_by_id(remote_job.id)
                        if current_job.state == "failed":
                            Recording.update(
                                state="completed",
                                error="원격 인코딩 실패, 원본 보존됨",
                                finished_at=datetime.now(UTC),
                            ).where(Recording.id == recording_id).execute()
            logger.info("recording=%s capture completed bytes=%s", recording_id, completed_size)
        except DownloadCancelled:
            active_processes.pop(recording_id, None)
            temp.unlink(missing_ok=True)
            Path(f"{temp}.aria2").unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            thumbnail_path(final).unlink(missing_ok=True)
            with session():
                job = EncodingJob.get_or_none(EncodingJob.recording == recording_id)
                if job:
                    if job.upload_path:
                        Path(job.upload_path).unlink(missing_ok=True)
                    EncodingJob.update(state="canceled", finished_at=datetime.now(UTC)).where(
                        EncodingJob.id == job.id
                    ).execute()
                Recording.update(
                    state="canceled",
                    path=None,
                    size=0,
                    total_size=0,
                    speed_bps=0,
                    eta_seconds=None,
                    error=None,
                    finished_at=datetime.now(UTC),
                ).where(Recording.id == recording_id).execute()
            logger.info("recording=%s canceled", recording_id)
        except Exception as exc:
            active_processes.pop(recording_id, None)
            partial_bytes = temp.stat().st_size if temp.exists() else 0
            reason = _redact(str(exc))[-1000:]
            if partial_bytes:
                # Without this the library shows an unplayable .ts with a raw
                # streamlink error and no hint that footage was kept.
                reason = (
                    f"녹화가 중단되어 {partial_bytes / 1024 ** 3:.2f} GB 부분 파일만 남았습니다"
                    f" (재생 불가). {reason}"
                )
            _finalize(
                recording_id,
                state="failed",
                path=str(temp) if temp.exists() else None,
                size=partial_bytes,
                speed_bps=0,
                eta_seconds=None,
                error=reason[-1000:],
                finished_at=datetime.now(UTC),
            )
            logger.error("recording=%s failed error=%s", recording_id, _redact(str(exc))[-500:])


async def monitor_live_channels_once() -> list[int]:
    """Probe every unique auto-recorded channel once and enqueue new broadcasts."""
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
        async def probe(channel_pk: int, chzzk_id: str):
            async with semaphore:
                return channel_pk, await chzzk.fetch_live(chzzk_id, client)

        results = await asyncio.gather(*(probe(pk, chzzk_id) for pk, chzzk_id in channels))

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
                    channel.last_live = False
                    channel.save()
                continue

            channel.last_live = True
            if live.get("author"):
                channel.name = live["author"]
            if live.get("channel_image"):
                channel.image_url = live["channel_image"]
            channel.save()
            users = [
                row.user_id
                for row in Subscription.select(Subscription.user).where(
                    Subscription.channel == channel.id,
                    Subscription.active == True,  # noqa: E712
                    Subscription.auto_record == True,  # noqa: E712
                )
            ]
            recording, created = ensure_recording(channel, live, users)
            if created:
                started.append(recording.id)
                asyncio.create_task(run_recording(recording.id))
    return started
