"""Startup migrations, background scheduler and one-off backfill jobs."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

from .config import logger, settings
from .db import database, session
from .models import Channel, Recording
from .encoding_commands import probe_duration
from .schema_migrations import migrate
from .services import chzzk
from .services.encoding import process_local_job, resume_local_jobs
from .services.media import generate_thumbnail, migrate_legacy_recording, thumbnail_path
from .services.recorder import monitor_live_channels_once, run_recording
from .services.stream_transport import start_stream_server


async def scheduler() -> None:
    while True:
        started_at = time.monotonic()
        try:
            await monitor_live_channels_once()
        except Exception:
            logger.exception("live monitor cycle failed")
        elapsed = time.monotonic() - started_at
        await asyncio.sleep(max(0.0, max(30, settings.poll_interval) - elapsed))


async def backfill_thumbnails() -> None:
    with session():
        pending = [
            Path(row.path)
            for row in Recording.select(Recording.path).where(
                Recording.state == "completed", Recording.path.is_null(False)
            )
            if row.path
        ]
    for video_path in pending:
        if not video_path.exists() or thumbnail_path(video_path).exists():
            continue
        try:
            await asyncio.to_thread(generate_thumbnail, video_path)
        except Exception as exc:
            logger.warning("thumbnail backfill failed path=%s error=%s", video_path, str(exc)[:200])


async def backfill_durations() -> None:
    """Populate media-derived durations for archives created before this field existed."""
    with session():
        pending = [
            (row.id, Path(row.path))
            for row in Recording.select(Recording.id, Recording.path).where(
                Recording.state == "completed",
                Recording.path.is_null(False),
                Recording.duration_seconds <= 0,
            )
            if row.path
        ]
    for recording_id, video_path in pending:
        if not video_path.exists():
            continue
        duration = await asyncio.to_thread(probe_duration, video_path)
        if duration > 0:
            with session():
                Recording.update(duration_seconds=duration).where(
                    Recording.id == recording_id,
                    Recording.duration_seconds <= 0,
                ).execute()


async def migrate_legacy_media() -> None:
    """Upgrade pre-v2 combined archives in the background, one at a time."""
    with session():
        recording_ids = [
            row.id
            for row in Recording.select(Recording.id).where(
                Recording.state == "completed",
                Recording.path.is_null(False),
                Recording.storage_version < 2,
            )
        ]
    for recording_id in recording_ids:
        try:
            await asyncio.to_thread(migrate_legacy_recording, recording_id)
            logger.info("legacy media migrated recording=%s storage_version=2", recording_id)
        except Exception as exc:
            logger.warning(
                "legacy media migration deferred recording=%s error=%s",
                recording_id,
                str(exc)[:300],
            )


async def backfill_media_metadata() -> None:
    # Migration may rename TS/MKV sources, so metadata jobs must observe the
    # updated path instead of racing the atomic replacement.
    await migrate_legacy_media()
    await backfill_thumbnails()
    await backfill_durations()


async def cleanup_completed_transports() -> None:
    """Remove obsolete transport/container files paired with published MP4 archives."""
    with session():
        published = [
            Path(row.path)
            for row in Recording.select(Recording.path).where(
                Recording.state == "completed", Recording.path.is_null(False)
            )
            if row.path and Path(row.path).suffix.lower() == ".mp4"
        ]
    for media_path in published:
        for extension in (".ts", ".mkv"):
            stale_path = media_path.with_suffix(extension)
            try:
                stale_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "stale media cleanup failed path=%s error=%s",
                    stale_path,
                    str(exc)[:200],
                )


async def backfill_channel_profiles() -> None:
    with session():
        # Filter in SQL so startup does not pull every channel into memory.
        channels = [
            (row.id, row.chzzk_id)
            for row in Channel.select(Channel.id, Channel.chzzk_id).where(
                Channel.name.startswith("채널 ")
                | Channel.image_url.is_null(True)
                | (Channel.image_url == "")
            )
        ]
    if not channels:
        return
    async with httpx.AsyncClient(headers={"User-Agent": "chzzk-archiver/0.1"}) as client:
        for channel_pk, chzzk_id in channels:
            try:
                profile = await chzzk.fetch_channel_profile(chzzk_id, client)
                with session():
                    channel = Channel.get_or_none(Channel.id == channel_pk)
                    if channel:
                        channel.name = profile["name"]
                        channel.image_url = profile.get("image")
                        channel.save()
            except Exception as exc:
                logger.warning("channel profile backfill failed channel=%s error=%s", chzzk_id, type(exc).__name__)


def partial_size(path: str | None) -> int | None:
    """Bytes already captured on disk, or None when unknowable."""
    if not path:
        return None
    try:
        return Path(path).stat().st_size
    except OSError:
        # A removed or unreachable file must not block startup.
        return None


def requeue_interrupted() -> list[int]:
    """Collect recordings that need a worker after a restart.

    ``queued`` rows are included because no task survives the process: a
    recording waiting on the concurrency semaphore would otherwise sit in the
    queue forever with nothing scheduled to pick it up.

    A crashed worker stops updating ``size`` mid-capture, so the stored value
    trails the bytes actually on disk. Resynchronising here keeps the library
    honest even when the retry immediately fails because the stream ended.
    """
    with session():
        pending = list(
            Recording.select(Recording.id, Recording.path, Recording.size).where(
                Recording.state.in_(["queued", "recording", "interrupted"])
            )
        )
        resume_ids = [rec.id for rec in pending]
        if not resume_ids:
            return []
        Recording.update(state="queued").where(Recording.id.in_(resume_ids)).execute()
        for rec in pending:
            actual = partial_size(rec.path)
            if actual is not None and actual != rec.size:
                logger.info(
                    "recording=%s size resynced stored=%s disk=%s", rec.id, rec.size, actual
                )
                Recording.update(size=actual).where(Recording.id == rec.id).execute()
    return resume_ids


@asynccontextmanager
async def lifespan(_: FastAPI):
    with session():
        migrate()
    resume_ids = requeue_interrupted()
    encoding_ids = resume_local_jobs()
    stream_server = await start_stream_server()
    tasks = [
        asyncio.create_task(scheduler()),
        asyncio.create_task(backfill_media_metadata()),
        asyncio.create_task(cleanup_completed_transports()),
        asyncio.create_task(backfill_channel_profiles()),
    ]
    tasks += [asyncio.create_task(run_recording(rid)) for rid in resume_ids]
    tasks += [asyncio.create_task(process_local_job(jid)) for jid in encoding_ids]
    try:
        yield
    finally:
        # Cancel every task before closing the database so no worker touches a
        # dead connection on the way out.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if stream_server:
            stream_server.close()
            await stream_server.wait_closed()
        if not database.is_closed():
            database.close()
