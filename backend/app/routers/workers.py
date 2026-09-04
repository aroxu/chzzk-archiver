"""Outbound-polling remote encoding worker protocol."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..config import settings
from ..db import db
from ..models import EncodingJob
from ..services.encoding import (
    complete_uploaded_job,
    fail_job,
    heartbeat_job,
    lease_job,
    register_worker,
)

router = APIRouter(prefix="/api/worker", tags=["worker"])


class WorkerHello(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    hostname: str = Field(min_length=1, max_length=255)
    platform: str = Field(min_length=1, max_length=80)
    encoders: list[str]
    version: str = Field(default="unknown", max_length=40)


class WorkerFailure(BaseModel):
    error: str = Field(min_length=1, max_length=4000)


def worker_auth(
    authorization: Annotated[str | None, Header()] = None,
    x_worker_id: Annotated[str | None, Header()] = None,
) -> str:
    if not settings.worker_token:
        raise HTTPException(503, "원격 인코딩 워커가 활성화되지 않았습니다")
    supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not secrets.compare_digest(supplied, settings.worker_token):
        raise HTTPException(401, "유효하지 않은 워커 토큰입니다")
    if not x_worker_id:
        raise HTTPException(400, "X-Worker-ID 헤더가 필요합니다")
    return x_worker_id


def assigned_job(job_id: int, worker_id: str) -> EncodingJob:
    job = EncodingJob.get_or_none(
        EncodingJob.id == job_id,
        EncodingJob.worker == worker_id,
        EncodingJob.state.in_(["leased", "encoding", "uploading"]),
    )
    if not job:
        raise HTTPException(409, "작업 lease가 만료되었거나 취소되었습니다")
    return job


@router.post("/lease")
def lease(
    body: WorkerHello,
    request: Request,
    worker_id: str = Depends(worker_auth),
    _=Depends(db),
):
    if worker_id != body.worker_id:
        raise HTTPException(400, "워커 ID가 헤더와 일치하지 않습니다")
    worker = register_worker(
        body.worker_id, body.hostname, body.platform, body.encoders, body.version
    )
    job = lease_job(worker, body.encoders)
    if not job:
        return Response(status_code=204)
    encoder = job.video_encoder
    if encoder == "auto":
        # The worker reports its encoders fastest-first, so the head of the
        # list is its GPU when one is usable.
        encoder = next(iter(body.encoders), "")
    EncodingJob.update(used_encoder=encoder).where(EncodingJob.id == job.id).execute()
    return {
        "id": job.id,
        "recording_id": job.recording_id,
        "complete_url": f"/api/worker/jobs/{job.id}/complete",
        "heartbeat_url": f"/api/worker/jobs/{job.id}/heartbeat",
        "fail_url": f"/api/worker/jobs/{job.id}/fail",
        "stream_host": settings.worker_stream_host or request.url.hostname,
        "stream_port": settings.worker_stream_port,
        "output_extension": job.output_extension,
        "encoder": encoder,
        "quality": job.quality,
        "preset": job.preset,
        "audio_mode": job.audio_mode,
    }


@router.post("/jobs/{job_id}/heartbeat", status_code=204)
def job_heartbeat(job_id: int, worker_id: str = Depends(worker_auth), _=Depends(db)):
    if not heartbeat_job(job_id, worker_id):
        raise HTTPException(409, "작업 lease가 만료되었거나 취소되었습니다")


@router.post("/jobs/{job_id}/complete")
async def job_complete(job_id: int, worker_id: str = Depends(worker_auth), _=Depends(db)):
    try:
        path = await __import__("asyncio").to_thread(complete_uploaded_job, job_id, worker_id)
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422, f"결과 검증에 실패했습니다: {str(exc)[-500:]}") from exc
    return {"status": "completed", "size": path.stat().st_size}


@router.post("/jobs/{job_id}/fail", status_code=204)
def job_fail(
    job_id: int,
    body: WorkerFailure,
    worker_id: str = Depends(worker_auth),
    _=Depends(db),
):
    assigned_job(job_id, worker_id)
    fail_job(job_id, worker_id, body.error)
