"""Administrator-only invite issuing and instance statistics."""

from __future__ import annotations

import secrets
import shutil
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends

from ..config import settings
from ..db import db
from ..models import Invite, Recording, Subscription, User
from ..security import admin, audit, digest

router = APIRouter()


@router.post("/api/admin/invites")
def create_invite(minutes: int = 1440, user: User = Depends(admin), _=Depends(db)):
    raw = secrets.token_urlsafe(24)
    Invite.create(
        token_hash=digest(raw),
        expires_at=datetime.now(UTC) + timedelta(minutes=min(minutes, 10080)),
    )
    audit(user.id, "invite.create")
    return {"token": raw, "expires_in": minutes * 60}


@router.get("/api/admin/overview")
def overview(_user: User = Depends(admin), _=Depends(db)):
    usage = shutil.disk_usage(settings.recordings_dir)
    return {
        "users": User.select().count(),
        "subscriptions": Subscription.select().where(Subscription.active == True).count(),  # noqa: E712
        "recordings": Recording.select().count(),
        "disk": {
            "total": usage.total,
            "used": usage.used,
            "percent": round(usage.used / usage.total * 100, 1),
        },
    }
