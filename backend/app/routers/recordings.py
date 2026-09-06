"""Recording library listing, manual downloads, cancellation and deletion."""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from ..config import settings
from ..db import database, db
from ..models import Broadcast, Channel, Entitlement, Recording, Subscription, User
from ..schemas import ManualDownloadBody
from ..security import admin, audit, current_user
from ..services import chzzk
from ..services.credentials import user_cookies
from ..services.media import archive_directory, audio_asset_path, recording_json, thumbnail_path
from ..services.recorder import ensure_recording, redact, run_recording
from ..services.state import active_processes

router = APIRouter()


def _delete_artifact(path: Path) -> None:
    """Delete only files contained by the configured recordings directory."""
    root = settings.recordings_dir.resolve()
    target = path.resolve()
    if not target.is_relative_to(root):
        raise RuntimeError(f"녹화 폴더 밖의 파일은 삭제할 수 없습니다: {target}")
    target.unlink(missing_ok=True)


def _purge_recording_files(rec: Recording) -> None:
    paths: set[Path] = set()
    for value in (rec.path,):
        if not value:
            continue
        media = Path(value)
        paths.update(
            {
                media,
                thumbnail_path(media),
                Path(f"{media}.aria2"),
                media.with_suffix(".mp4"),
                media.with_suffix(".mkv"),
                audio_asset_path(media, "aac"),
                audio_asset_path(media, "flac"),
            }
        )
        paths.add(archive_directory(media))
    for path in paths:
        if path.is_dir():
            root = settings.recordings_dir.resolve()
            target = path.resolve()
            if not target.is_relative_to(root):
                raise RuntimeError(f"녹화 폴더 밖의 폴더는 삭제할 수 없습니다: {target}")
            shutil.rmtree(target, ignore_errors=False)
        else:
            _delete_artifact(path)


def entitled(user: User, rid: int) -> Recording:
    rec = Recording.get_or_none(Recording.id == rid)
    if not rec:
        raise HTTPException(404)
    if user.role != "admin" and not Entitlement.get_or_none(
        Entitlement.user == user.id, Entitlement.recording == rid
    ):
        raise HTTPException(404)
    return rec


@router.get("/api/recordings")
def recordings(user: User = Depends(current_user), _=Depends(db)):
    # Select the broadcast and channel alongside the recording so serialising
    # the list stays a single query instead of two lookups per row.
    rows = (
        Recording.select(Recording, Broadcast, Channel)
        .join(Broadcast, on=(Recording.broadcast == Broadcast.id))
        .join(Channel, on=(Broadcast.channel == Channel.id))
        .join(Entitlement, on=(Entitlement.recording == Recording.id))
        .where(Entitlement.user == user.id)
        .order_by(Recording.created_at.desc())
        .distinct()
    )
    return [recording_json(x) for x in rows]


@router.post("/api/recordings/manual")
async def manual(
    body: ManualDownloadBody,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    _=Depends(db),
):
    kind, content_id, url = chzzk.parse_content_url(body.url)
    cookies = user_cookies([user.id])
    try:
        resolver = chzzk.resolve_streamlink if kind == "live" else chzzk.resolve_direct
        metadata = await asyncio.to_thread(resolver, url, cookies[0] if cookies else {})
    except Exception as exc:
        # The resolver error can embed signed playback URLs and cookies.
        raise HTTPException(409, f"영상을 열 수 없습니다: {redact(str(exc))}") from exc
    if kind == "live":
        ch = Channel.get_or_none(Channel.chzzk_id == content_id)
        if not ch:
            ch = Channel.create(chzzk_id=content_id, name=metadata["author"])
        users = [user.id] + [
            row.user_id
            for row in Subscription.select(Subscription.user).where(
                Subscription.channel == ch.id,
                Subscription.active == True,  # noqa: E712
                Subscription.auto_record == True,  # noqa: E712
            )
        ]
    else:
        virtual_id = f"{kind}:{content_id}"[:64]
        ch = Channel.get_or_none(Channel.chzzk_id == virtual_id)
        if not ch:
            ch = Channel.create(
                chzzk_id=virtual_id,
                name=metadata["author"],
                profile_backfilled=True,
            )
        elif not ch.profile_backfilled:
            ch.profile_backfilled = True
            ch.save(only=[Channel.profile_backfilled])
        users = [user.id]
    live = {
        "id": f"{kind}:{metadata['id']}",
        "title": metadata["title"],
        "category": metadata["category"],
        "thumbnail": metadata.get("thumbnail"),
    }
    rec, created = ensure_recording(ch, live, list(set(users)))
    broadcast = rec.broadcast
    broadcast.title = metadata["title"]
    broadcast.thumbnail_url = metadata.get("thumbnail")
    broadcast.save()
    ch.name = metadata["author"]
    ch.save()
    should_start = created or rec.state in {"failed", "interrupted"}
    if should_start:
        if rec.path and not rec.path.endswith(".ts"):
            previous_media = Path(rec.path)
            _purge_recording_files(rec)
            rec.path = None
            rec.size = 0
            rec.total_size = 0
            rec.duration_seconds = 0
        rec.state = "queued"
        rec.speed_bps = 0
        rec.eta_seconds = None
        rec.error = None
        rec.finished_at = None
        rec.save()
        broadcast.source_type = kind
        broadcast.source_url = url
        broadcast.save()
        background.add_task(run_recording, rec.id)
    return recording_json(Recording.get_by_id(rec.id))


@router.delete("/api/recordings/{recording_id}", status_code=204)
def remove_recording(recording_id: int, user: User = Depends(current_user), _=Depends(db)):
    rec = entitled(user, recording_id)
    Entitlement.delete().where(
        Entitlement.user == user.id, Entitlement.recording == recording_id
    ).execute()
    remaining = Entitlement.select().where(Entitlement.recording == recording_id).exists()
    if not remaining and rec.state not in ("queued", "recording"):
        if rec.path:
            _purge_recording_files(rec)
        rec.delete_instance()


@router.delete("/api/admin/recordings/{recording_id}", status_code=204)
def purge_recording(recording_id: int, user: User = Depends(admin), _=Depends(db)):
    """Permanently remove one archive and every user's reference to it."""
    rec = Recording.get_or_none(Recording.id == recording_id)
    if not rec:
        raise HTTPException(404, "아카이브를 찾을 수 없습니다")
    if rec.state in {"queued", "recording"}:
        raise HTTPException(409, "진행 중인 작업을 먼저 중단한 뒤 삭제하세요")
    try:
        _purge_recording_files(rec)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(409, f"아카이브 파일을 삭제할 수 없습니다: {exc}") from exc
    title = rec.broadcast.title
    with database.atomic():
        Entitlement.delete().where(Entitlement.recording == recording_id).execute()
        Recording.delete().where(Recording.id == recording_id).execute()
        audit(user.id, "recording.purge", recording_id=recording_id, title=title)


@router.post("/api/recordings/{recording_id}/cancel", status_code=204)
def cancel_recording(recording_id: int, user: User = Depends(current_user), _=Depends(db)):
    rec = entitled(user, recording_id)
    if rec.state not in {"queued", "recording"}:
        raise HTTPException(409, "진행 중인 작업이 아닙니다")
    rec.state = "canceled"
    rec.finished_at = datetime.now(UTC)
    rec.save()
    process = active_processes.get(recording_id)
    if process and process.returncode is None:
        process.terminate()
