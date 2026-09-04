"""Channel subscription management."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ..config import logger
from ..db import database, db
from ..models import Broadcast, Channel, Entitlement, Recording, Subscription, User
from ..schemas import StartLiveBody, SubscribeBody, UnsubscribeBody
from ..security import audit, current_user
from ..services import chzzk
from ..services.media import recording_json
from ..services.recorder import ensure_recording, run_recording

router = APIRouter()


async def _probe_live(chzzk_id: str) -> dict | None:
    """Return live metadata, or None when the channel is offline/unreachable."""
    async with httpx.AsyncClient(headers={"User-Agent": "chzzk-archiver/0.1"}) as client:
        live = await chzzk.fetch_live(chzzk_id, client)
    return None if live is chzzk.LIVE_PROBE_FAILED or live is None else live


@router.get("/api/subscriptions")
def subscriptions(user: User = Depends(current_user), _=Depends(db)):
    rows = (
        Subscription.select(Subscription, Channel)
        .join(Channel, on=(Subscription.channel == Channel.id))
        .where(Subscription.user == user.id, Subscription.active == True)  # noqa: E712
    )
    return [
        {
            "id": x.id,
            "channel_id": x.channel.chzzk_id,
            "name": x.channel.name,
            "image": x.channel.image_url,
            "live": x.channel.last_live,
            "auto_record": x.auto_record,
        }
        for x in rows
    ]


@router.post("/api/subscriptions")
async def subscribe(body: SubscribeBody, user: User = Depends(current_user), _=Depends(db)):
    cid = chzzk.channel_id(body.channel)
    try:
        profile = await chzzk.fetch_channel_profile(cid)
    except Exception as exc:
        logger.warning("channel profile lookup failed channel=%s error=%s", cid, type(exc).__name__)
        raise HTTPException(502, "치지직 채널 정보를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
    # Channel, subscription and back-fill entitlements form one unit: a partial
    # commit would leave an orphan channel or a subscription without access.
    with database.atomic():
        ch = Channel.get_or_none(Channel.chzzk_id == cid)
        if not ch:
            ch = Channel.create(chzzk_id=cid, name=profile["name"], image_url=profile.get("image"))
        else:
            ch.name = profile["name"]
            ch.image_url = profile.get("image")
            ch.save()
        sub = Subscription.get_or_none(Subscription.user == user.id, Subscription.channel == ch.id)
        if sub:
            sub.active = True
            sub.auto_record = body.auto_record
            sub.save()
        else:
            sub = Subscription.create(user=user.id, channel=ch.id, auto_record=body.auto_record)
        recordings = (
            Recording.select()
            .join(Broadcast, on=(Recording.broadcast == Broadcast.id))
            .where(Broadcast.channel == ch.id)
        )
        for rec in recordings:
            if not Entitlement.get_or_none(Entitlement.user == user.id, Entitlement.recording == rec.id):
                Entitlement.create(user=user.id, recording=rec.id)
        audit(user.id, "subscribe", channel=cid)
    # Probing after the commit keeps the subscription write off the network
    # path: a slow or failing CHZZK response must not roll back the subscribe.
    live = await _probe_live(cid)
    if live is not None and not ch.last_live:
        with database.atomic():
            Channel.update(last_live=True).where(Channel.id == ch.id).execute()
    return {
        "id": sub.id,
        "channel_id": cid,
        "name": ch.name,
        "image": ch.image_url,
        "auto_record": sub.auto_record,
        "live": live is not None,
        "live_title": live.get("title") if live else None,
    }


@router.post("/api/subscriptions/start-live")
async def start_live(body: StartLiveBody, user: User = Depends(current_user), _=Depends(db)):
    """Capture an in-progress broadcast without waiting for the poll cycle."""
    cid = chzzk.channel_id(body.channel)
    ch = Channel.get_or_none(Channel.chzzk_id == cid)
    if not ch or not Subscription.get_or_none(
        Subscription.user == user.id,
        Subscription.channel == ch.id,
        Subscription.active == True,  # noqa: E712
    ):
        raise HTTPException(404, "구독 중인 채널이 아닙니다")
    live = await _probe_live(cid)
    if live is None:
        Channel.update(last_live=False).where(Channel.id == ch.id).execute()
        raise HTTPException(409, "현재 라이브 중이 아닙니다")
    # Everyone auto-recording this channel shares the single capture, matching
    # how the background monitor dedupes broadcasts.
    users = {user.id} | {
        row.user_id
        for row in Subscription.select(Subscription.user).where(
            Subscription.channel == ch.id,
            Subscription.active == True,  # noqa: E712
            Subscription.auto_record == True,  # noqa: E712
        )
    }
    ch.last_live = True
    ch.save()
    # The background monitor must never resume a capture the user stopped, but
    # an explicit opt-in is exactly the request to start it again.
    rec, created = ensure_recording(ch, live, sorted(users), retry_states=("failed", "canceled"))
    # ensure_recording() reports created for a brand new row and for a failed row
    # it reset to "queued", which is exactly when a worker needs scheduling.
    if created:
        asyncio.create_task(run_recording(rec.id))
    audit(user.id, "subscribe.start_live", channel=cid, recording=rec.id, created=created)
    return {"started": created, **recording_json(Recording.get_by_id(rec.id))}


@router.post("/api/subscriptions/{subscription_id}/unsubscribe", status_code=204)
def unsubscribe(subscription_id: int, body: UnsubscribeBody, user: User = Depends(current_user), _=Depends(db)):
    sub = Subscription.get_or_none(Subscription.id == subscription_id, Subscription.user == user.id)
    if not sub:
        raise HTTPException(404)
    sub.active = False
    sub.save()
    if body.remove_recordings:
        ids = [
            row.id
            for row in Recording.select(Recording.id)
            .join(Broadcast, on=(Recording.broadcast == Broadcast.id))
            .where(Broadcast.channel == sub.channel_id)
        ]
        if ids:
            Entitlement.delete().where(
                Entitlement.user == user.id, Entitlement.recording.in_(ids)
            ).execute()
    audit(user.id, "unsubscribe", channel_id=sub.channel_id, remove_recordings=body.remove_recordings)
