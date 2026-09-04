"""Browser-extension pairing and cookie synchronisation."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from ..db import db
from ..models import ApiToken, Credential, User, as_utc
from ..schemas import CookieBody
from ..security import current_user, digest, fernet

router = APIRouter()

ALLOWED_COOKIES = {"NID_AUT", "NID_SES"}


@router.post("/api/me/pair")
def pair(user: User = Depends(current_user), _=Depends(db)):
    raw = secrets.token_urlsafe(32)
    ApiToken.create(
        user=user.id,
        token_hash=digest(raw),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        kind="pairing",
    )
    return {"code": raw, "expires_in": 600}


@router.post("/api/extension/exchange")
def exchange(code: str, _=Depends(db)):
    pairing = ApiToken.get_or_none(ApiToken.token_hash == digest(code), ApiToken.kind == "pairing")
    if not pairing or not pairing.expires_at or as_utc(pairing.expires_at) < datetime.now(UTC):
        raise HTTPException(400, "페어링 코드가 만료되었습니다")
    raw = secrets.token_urlsafe(40)
    ApiToken.create(user=pairing.user_id, token_hash=digest(raw))
    pairing.delete_instance()
    return {"token": raw}


@router.put("/api/extension/cookies")
def update_cookies(body: CookieBody, user: User = Depends(current_user), _=Depends(db)):
    allowed = {k: v for k, v in body.cookies.items() if k in ALLOWED_COOKIES}
    if not allowed:
        raise HTTPException(422, "허용된 인증 쿠키가 없습니다")
    encrypted = fernet().encrypt(json.dumps(allowed).encode()).decode()
    cred = Credential.get_or_none(Credential.user == user.id)
    if cred:
        cred.encrypted = encrypted
        cred.valid = True
        cred.updated_at = datetime.now(UTC)
        cred.save()
    else:
        Credential.create(user=user.id, encrypted=encrypted)
    return {"status": "synced", "names": list(allowed)}
