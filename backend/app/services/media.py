"""Thumbnail generation and recording serialization."""

from __future__ import annotations

import subprocess
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from ..models import EncodingJob, Recording, as_utc
from .state import active_processes


def thumbnail_path(video_path: Path) -> Path:
    return video_path.with_suffix(".thumbnail.jpg")


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
        "recorded_seconds": recorded_seconds,
        "duration_seconds": max(0, float(r.duration_seconds or 0)),
        "recording_active": process_active,
        "created_at": as_utc(r.created_at),
        "finished_at": as_utc(r.finished_at),
        "error": r.error,
    }
