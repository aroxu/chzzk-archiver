"""Durable local and remote HEVC encoding job orchestration."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..config import logger, settings
from ..db import database, session
from ..encoding_commands import (
    choose_encoder,
    detect_hevc_encoders,
    ffmpeg_encode_command,
    output_extension,
    parse_ffmpeg_time,
    probe_duration,
    probe_media,
    with_progress,
)
from ..models import EncodingJob, Recording, WorkerNode
from .media import generate_thumbnail, thumbnail_path
from .state import active_processes

encoding_semaphore = asyncio.Semaphore(settings.max_encodings)
MAX_ATTEMPTS = 3


def enqueue_encoding(
    recording_id: int, source_path: Path | None = None, *, mark_processing: bool = True
) -> EncodingJob | None:
    """Create/reset the post-capture job and expose processing state."""
    if settings.encoding_mode == "disabled":
        return None
    if settings.encoding_mode not in {"local", "remote"}:
        raise RuntimeError(f"지원하지 않는 인코딩 모드입니다: {settings.encoding_mode}")
    extension = output_extension(settings.encoding_audio)
    with session(), database.atomic():
        recording = Recording.get_by_id(recording_id)
        job, _ = EncodingJob.get_or_create(
            recording=recording.id,
            defaults={
                "video_encoder": settings.encoding_video_encoder,
                "quality": settings.encoding_quality,
                "preset": settings.encoding_preset,
                "audio_mode": settings.encoding_audio,
                "output_extension": extension,
                "source_path": str(source_path) if source_path else recording.path,
            },
        )
        EncodingJob.update(
            state="queued",
            worker=None,
            video_encoder=settings.encoding_video_encoder,
            quality=settings.encoding_quality,
            preset=settings.encoding_preset,
            audio_mode=settings.encoding_audio,
            output_extension=extension,
            source_path=str(source_path) if source_path else recording.path,
            attempts=0,
            lease_expires_at=None,
            upload_path=None,
            progress=0,
            processed_seconds=0,
            duration_seconds=0,
            encoding_speed=0,
            eta_seconds=None,
            error=None,
            started_at=None,
            finished_at=None,
        ).where(EncodingJob.id == job.id).execute()
        if mark_processing:
            Recording.update(state="processing", speed_bps=0, eta_seconds=None).where(
                Recording.id == recording_id,
                Recording.state.not_in(["canceled", "failed"]),
            ).execute()
        return EncodingJob.get_by_id(job.id)


def register_worker(
    worker_id: str, hostname: str, platform: str, encoders: list[str], version: str
) -> WorkerNode:
    now = datetime.now(UTC)
    with session():
        worker, _ = WorkerNode.get_or_create(
            id=worker_id,
            defaults={"hostname": hostname, "platform": platform},
        )
        WorkerNode.update(
            hostname=hostname,
            platform=platform,
            encoders=json.dumps(encoders),
            version=version,
            last_seen_at=now,
        ).where(WorkerNode.id == worker.id).execute()
        return WorkerNode.get_by_id(worker.id)


def _requeue_expired(now: datetime) -> None:
    expired = list(
        EncodingJob.select().where(
            EncodingJob.state.in_(["leased", "encoding", "uploading"]),
            EncodingJob.lease_expires_at < now,
        )
    )
    for job in expired:
        if job.upload_path:
            Path(job.upload_path).unlink(missing_ok=True)
        next_state = "failed" if job.attempts >= MAX_ATTEMPTS else "queued"
        EncodingJob.update(
            state=next_state,
            worker=None,
            lease_expires_at=None,
            upload_path=None,
            progress=0 if next_state == "queued" else job.progress,
            processed_seconds=0 if next_state == "queued" else job.processed_seconds,
            duration_seconds=0 if next_state == "queued" else job.duration_seconds,
            encoding_speed=0,
            eta_seconds=None,
            error="worker lease expired" if next_state == "failed" else None,
            finished_at=now if next_state == "failed" else None,
        ).where(EncodingJob.id == job.id).execute()
        if next_state == "failed":
            _preserve_original(job.recording_id, "원격 인코딩 워커 응답이 없습니다")


def lease_job(worker: WorkerNode, encoders: list[str]) -> EncodingJob | None:
    """Atomically lease the oldest compatible job to a polling worker."""
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=max(30, settings.worker_lease_seconds))
    with session(), database.atomic():
        _requeue_expired(now)
        candidates = EncodingJob.select().where(EncodingJob.state == "queued").order_by(EncodingJob.created_at)
        for job in candidates:
            if job.video_encoder != "auto" and job.video_encoder not in encoders:
                continue
            if job.video_encoder == "auto" and not encoders:
                continue
            updated = (
                EncodingJob.update(
                    state="leased",
                    worker=worker.id,
                    attempts=EncodingJob.attempts + 1,
                    lease_expires_at=expires,
                    started_at=job.started_at or now,
                    error=None,
                )
                .where(EncodingJob.id == job.id, EncodingJob.state == "queued")
                .execute()
            )
            if updated:
                return EncodingJob.get_by_id(job.id)
    return None


def heartbeat_job(
    job_id: int,
    worker_id: str,
    state: str = "encoding",
    *,
    processed_seconds: float | None = None,
    encoding_speed: float | None = None,
) -> bool:
    expires = datetime.now(UTC) + timedelta(seconds=max(30, settings.worker_lease_seconds))
    with session():
        job = EncodingJob.get_or_none(
            EncodingJob.id == job_id,
            EncodingJob.worker == worker_id,
            EncodingJob.state.in_(["leased", "encoding", "uploading"]),
        )
        if not job:
            return False
        # A late progress heartbeat can race with the stream receiver marking
        # the upload complete. Never move that terminal hand-off backwards.
        values: dict = {
            "state": "uploading" if job.state == "uploading" else state,
            "lease_expires_at": expires,
        }
        if processed_seconds is not None:
            duration = job.duration_seconds
            if duration <= 0 and job.recording.state == "processing":
                source = Path(job.source_path or job.recording.path)
                duration = probe_duration(source)
                values["duration_seconds"] = duration
            processed = max(job.processed_seconds, processed_seconds)
            speed = max(0.0, encoding_speed or 0.0)
            values.update(processed_seconds=processed, encoding_speed=speed)
            if duration > 0:
                values["progress"] = min(99.9, processed / duration * 100)
                values["eta_seconds"] = (
                    max(0, int((duration - processed) / speed)) if speed > 0 else None
                )
        return bool(EncodingJob.update(**values).where(EncodingJob.id == job.id).execute())


def upload_destination(job: EncodingJob) -> Path:
    source = Path(job.recording.path)
    stream_extension = ".mkv" if job.audio_mode == "flac24" else ".ts"
    return source.with_name(f".{source.stem}.worker-{job.id}.stream{stream_extension}")


def begin_upload(job_id: int, worker_id: str) -> tuple[EncodingJob, Path]:
    with session():
        job = EncodingJob.get_or_none(
            EncodingJob.id == job_id,
            EncodingJob.worker == worker_id,
            EncodingJob.state.in_(["leased", "encoding", "uploading"]),
        )
        if not job:
            raise LookupError("job lease is no longer active")
        destination = upload_destination(job)
        destination.unlink(missing_ok=True)
        EncodingJob.update(
            state="uploading",
            upload_path=str(destination),
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=max(30, settings.worker_lease_seconds)),
        ).where(EncodingJob.id == job.id).execute()
        return job, destination


def _preserve_original(recording_id: int, message: str) -> None:
    with session():
        Recording.update(
            state="completed",
            error=message,
            finished_at=datetime.now(UTC),
            speed_bps=0,
            eta_seconds=0,
        ).where(Recording.id == recording_id, Recording.state == "processing").execute()


def fail_job(job_id: int, worker_id: str | None, error: str) -> None:
    """Retry a remote failure, preserving the captured original after the limit."""
    with session():
        query = EncodingJob.select().where(EncodingJob.id == job_id)
        if worker_id is not None:
            query = query.where(EncodingJob.worker == worker_id)
        job = query.get_or_none()
        if not job or job.state in {"completed", "canceled"}:
            return
        if job.upload_path:
            Path(job.upload_path).unlink(missing_ok=True)
        terminal = job.attempts >= MAX_ATTEMPTS or worker_id is None
        EncodingJob.update(
            state="failed" if terminal else "queued",
            worker=None,
            lease_expires_at=None,
            upload_path=None,
            progress=job.progress if terminal else 0,
            processed_seconds=job.processed_seconds if terminal else 0,
            duration_seconds=job.duration_seconds if terminal else 0,
            encoding_speed=0,
            eta_seconds=None,
            error=error[-1000:],
            finished_at=datetime.now(UTC) if terminal else None,
        ).where(EncodingJob.id == job.id).execute()
        if terminal:
            _preserve_original(job.recording_id, f"인코딩 실패, 원본 보존됨: {error[-300:]}")


def _install_output(job_id: int, encoded: Path) -> Path:
    """Validate then atomically publish encoded media and update both rows."""
    probe_media(encoded)
    with session():
        job = EncodingJob.get_by_id(job_id)
        recording = Recording.get_by_id(job.recording_id)
        if job.state == "canceled" or recording.state == "canceled":
            encoded.unlink(missing_ok=True)
            raise RuntimeError("encoding job was canceled")
        source = Path(recording.path)
        final = source.with_suffix(job.output_extension)
        publishable = encoded
        if job.output_extension == ".mp4":
            publishable = source.with_name(f".{source.stem}.publish-{job.id}.mp4")
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(encoded), "-c", "copy", "-tag:v", "hvc1",
                    "-movflags", "+faststart", str(publishable),
                ],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                publishable.unlink(missing_ok=True)
                raise RuntimeError(result.stderr.decode(errors="replace")[-1000:])
            probe_media(publishable)
            encoded.unlink(missing_ok=True)
        old_thumbnail = thumbnail_path(source)
        publishable.replace(final)
        if source != final:
            source.unlink(missing_ok=True)
        old_thumbnail.unlink(missing_ok=True)
        try:
            generate_thumbnail(final)
        except Exception as exc:
            logger.warning("encoded thumbnail failed recording=%s error=%s", recording.id, str(exc)[:200])
        size = final.stat().st_size
        now = datetime.now(UTC)
        with database.atomic():
            EncodingJob.update(
                state="completed",
                progress=100,
                processed_seconds=EncodingJob.duration_seconds,
                encoding_speed=0,
                eta_seconds=0,
                lease_expires_at=None,
                upload_path=None,
                error=None,
                finished_at=now,
            ).where(EncodingJob.id == job.id, EncodingJob.state != "canceled").execute()
            updated = Recording.update(
                state="completed",
                path=str(final),
                size=size,
                total_size=size,
                speed_bps=0,
                eta_seconds=0,
                error=None,
                finished_at=now,
            ).where(Recording.id == recording.id, Recording.state == "processing").execute()
        if not updated:
            final.unlink(missing_ok=True)
            thumbnail_path(final).unlink(missing_ok=True)
            raise RuntimeError("recording is no longer awaiting encoding")
        if job.source_path:
            streamed_source = Path(job.source_path)
            if streamed_source != final:
                streamed_source.unlink(missing_ok=True)
        return final


def complete_uploaded_job(job_id: int, worker_id: str) -> Path:
    with session():
        job = EncodingJob.get_or_none(
            EncodingJob.id == job_id,
            EncodingJob.worker == worker_id,
            EncodingJob.state == "uploading",
        )
        if not job or not job.upload_path:
            raise LookupError("uploaded job was not found")
        path = Path(job.upload_path)
    try:
        return _install_output(job_id, path)
    except Exception as exc:
        path.unlink(missing_ok=True)
        fail_job(job_id, worker_id, str(exc))
        raise


async def process_local_job(job_id: int) -> None:
    """Run one queued job on the controller host using its FFmpeg binary."""
    async with encoding_semaphore:
        local_id = f"local:{socket.gethostname()}"
        available = await asyncio.to_thread(detect_hevc_encoders)
        worker = register_worker(local_id, socket.gethostname(), os.name, available, "builtin")
        with session():
            job = EncodingJob.get_by_id(job_id)
            source = Path(job.recording.path)
            encoder = choose_encoder(job.video_encoder, available)
            encoded = source.with_name(f".{source.stem}.local-{job.id}.part{job.output_extension}")
            EncodingJob.update(
                state="encoding",
                worker=worker.id,
                # Keep video_encoder as the *request* so a retry stays leasable
                # by any worker; record what actually ran separately.
                used_encoder=encoder,
                attempts=EncodingJob.attempts + 1,
                started_at=datetime.now(UTC),
                lease_expires_at=datetime.now(UTC) + timedelta(days=1),
            ).where(EncodingJob.id == job.id, EncodingJob.state == "queued").execute()
            command = ffmpeg_encode_command(
                source,
                encoded,
                encoder=encoder,
                quality=job.quality,
                preset=job.preset,
                audio_mode=job.audio_mode,
            )
            duration = await asyncio.to_thread(probe_duration, source)
            EncodingJob.update(duration_seconds=duration).where(EncodingJob.id == job.id).execute()
            command = with_progress(command)
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            active_processes[job.recording_id] = process
            progress_values: dict[str, str] = {}
            stderr_lines: list[str] = []
            assert process.stderr is not None
            while line := await process.stderr.readline():
                text = line.decode(errors="replace").strip()
                if "=" in text:
                    key, value = text.split("=", 1)
                    if key in {"out_time", "speed", "progress"}:
                        progress_values[key] = value
                        if key == "progress":
                            processed = parse_ffmpeg_time(progress_values.get("out_time", ""))
                            speed_text = progress_values.get("speed", "0").rstrip("x")
                            try:
                                encode_speed = float(speed_text)
                            except ValueError:
                                encode_speed = 0.0
                            if processed is not None:
                                heartbeat_job(
                                    job.id,
                                    worker.id,
                                    processed_seconds=processed,
                                    encoding_speed=encode_speed,
                                )
                        continue
                stderr_lines.append(text)
                stderr_lines = stderr_lines[-50:]
            await process.wait()
            active_processes.pop(job.recording_id, None)
            if process.returncode != 0:
                raise RuntimeError("\n".join(stderr_lines)[-1000:])
            await asyncio.to_thread(_install_output, job.id, encoded)
            logger.info("recording=%s encoded locally encoder=%s", job.recording_id, encoder)
        except Exception as exc:
            active_processes.pop(job.recording_id, None)
            encoded.unlink(missing_ok=True)
            with session():
                canceled = Recording.get_by_id(job.recording_id).state == "canceled"
                if canceled:
                    EncodingJob.update(state="canceled", finished_at=datetime.now(UTC)).where(
                        EncodingJob.id == job.id
                    ).execute()
                else:
                    fail_job(job.id, None, str(exc))
            logger.error("recording=%s local encoding failed error=%s", job.recording_id, str(exc)[-500:])


def resume_local_jobs() -> list[int]:
    """Reset interrupted local work and return queued job ids."""
    if settings.encoding_mode != "local":
        return []
    with session():
        EncodingJob.update(
            state="queued", worker=None, lease_expires_at=None, upload_path=None
        ).where(EncodingJob.state.in_(["leased", "encoding", "uploading"])).execute()
        return [job.id for job in EncodingJob.select(EncodingJob.id).where(EncodingJob.state == "queued")]
