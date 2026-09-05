"""Full-duplex TCP data plane for remote real-time encoding.

The controller sends a growing capture file to the worker while receiving the
encoded FFmpeg stdout on the same connection. Control and authentication stay
on the HTTP API; the socket carries media bytes only after a short JSON hello.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..config import logger, settings
from ..db import session
from ..models import EncodingJob, Recording
from .encoding import (
    complete_uploaded_job,
    fail_job,
    heartbeat_job,
    upload_destination,
    uploaded_job_status,
)


async def _send_growing_source(
    job_id: int, source: Path, writer: asyncio.StreamWriter
) -> int:
    sent = 0
    while not source.exists():
        with session():
            job = EncodingJob.get_by_id(job_id)
            recording = Recording.get_by_id(job.recording_id)
            if job.state == "canceled" or recording.state in {"canceled", "failed"}:
                raise RuntimeError("source recording was canceled")
        await asyncio.sleep(0.2)

    with source.open("rb") as handle:
        while True:
            chunk = await asyncio.to_thread(handle.read, settings.worker_stream_chunk_size)
            if chunk:
                writer.write(chunk)
                await writer.drain()
                sent += len(chunk)
                if sent % (32 * 1024 * 1024) < len(chunk):
                    with session():
                        worker_id = EncodingJob.get_by_id(job_id).worker_id
                    if not heartbeat_job(job_id, worker_id, "encoding"):
                        raise RuntimeError("stream job was canceled")
                continue
            with session():
                job = EncodingJob.get_by_id(job_id)
                recording = Recording.get_by_id(job.recording_id)
                active = recording.state in {"queued", "recording"}
                canceled = job.state == "canceled" or recording.state in {"canceled", "failed"}
            if canceled:
                raise RuntimeError("source recording was canceled")
            if not active:
                # Recheck once after the capture state transition so bytes
                # flushed just before the transition cannot be missed.
                await asyncio.sleep(0.1)
                tail = await asyncio.to_thread(handle.read, settings.worker_stream_chunk_size)
                if tail:
                    writer.write(tail)
                    await writer.drain()
                    sent += len(tail)
                    continue
                break
            heartbeat_job(job_id, job.worker_id, "encoding")
            await asyncio.sleep(0.2)
    with suppress(NotImplementedError, RuntimeError):
        writer.write_eof()
        await writer.drain()
    return sent


async def _receive_encoded(
    job_id: int, worker_id: str, destination: Path, reader: asyncio.StreamReader
) -> int:
    received = 0
    with destination.open("wb") as handle:
        while chunk := await reader.read(settings.worker_stream_chunk_size):
            await asyncio.to_thread(handle.write, chunk)
            received += len(chunk)
            if received % (32 * 1024 * 1024) < len(chunk):
                if not heartbeat_job(job_id, worker_id, "encoding"):
                    raise RuntimeError("stream job was canceled")
        await asyncio.to_thread(handle.flush)
    return received


async def handle_stream(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    peer = writer.get_extra_info("peername")
    job_id: int | None = None
    worker_id: str | None = None
    destination: Path | None = None
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        if len(line) > 8192:
            raise RuntimeError("stream hello is too large")
        hello = json.loads(line)
        job_id = int(hello["job_id"])
        worker_id = str(hello["worker_id"])
        token = str(hello.get("token", ""))
        if not settings.worker_token or not secrets.compare_digest(token, settings.worker_token):
            raise RuntimeError("invalid worker token")
        with session():
            job = EncodingJob.get_or_none(
                EncodingJob.id == job_id,
                EncodingJob.worker == worker_id,
                EncodingJob.state.in_(["leased", "encoding"]),
            )
            if not job or not job.source_path:
                raise RuntimeError("stream job lease is not active")
            source = Path(job.source_path)
            destination = upload_destination(job)
            destination.unlink(missing_ok=True)
            EncodingJob.update(
                state="encoding",
                upload_path=str(destination),
                lease_expires_at=datetime.now(UTC)
                + timedelta(seconds=max(30, settings.worker_lease_seconds)),
            ).where(EncodingJob.id == job.id).execute()
        writer.write(b"OK\n")
        await writer.drain()
        sent, received = await asyncio.gather(
            _send_growing_source(job_id, source, writer),
            _receive_encoded(job_id, worker_id, destination, reader),
        )
        if received == 0:
            raise RuntimeError("worker returned an empty encoded stream")
        with session():
            EncodingJob.update(
                state="uploading",
                lease_expires_at=datetime.now(UTC)
                + timedelta(seconds=max(30, settings.worker_lease_seconds)),
            ).where(
                EncodingJob.id == job_id,
                EncodingJob.worker == worker_id,
                EncodingJob.state == "encoding",
            ).execute()
        logger.info(
            "encoding stream complete job=%s worker=%s sent=%s received=%s",
            job_id,
            worker_id,
            sent,
            received,
        )
        # Finalization belongs to the controller, not to a best-effort HTTP
        # acknowledgement from the worker.  Otherwise a worker restart or a
        # malformed control URL leaves a fully received file stuck forever.
        try:
            await asyncio.to_thread(complete_uploaded_job, job_id, worker_id)
        except LookupError:
            # A very fast worker can send the legacy HTTP completion ACK at
            # the same instant.  That request may already own finalization.
            if uploaded_job_status(job_id, worker_id) not in {"finalizing", "completed"}:
                raise
    except Exception as exc:
        if destination:
            destination.unlink(missing_ok=True)
        if job_id is not None and worker_id:
            fail_job(job_id, worker_id, str(exc))
        logger.warning("encoding stream failed peer=%s error=%s", peer, str(exc)[:300])
        with suppress(Exception):
            writer.write(f"ERROR {str(exc)[:200]}\n".encode())
            await writer.drain()
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


async def start_stream_server() -> asyncio.AbstractServer | None:
    if settings.encoding_mode != "remote":
        return None
    server = await asyncio.start_server(
        handle_stream, host="0.0.0.0", port=settings.worker_stream_port
    )
    logger.info("remote encoding TCP stream listening port=%s", settings.worker_stream_port)
    return server
