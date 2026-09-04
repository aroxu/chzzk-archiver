"""Password hashing, session tokens, cookie encryption and auth dependencies."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from cryptography.fernet import Fernet
from fastapi import Cookie, Depends, Header, HTTPException
from pwdlib import PasswordHash

from .config import settings
from .db import db
from .models import ApiToken, AuditLog, User, as_utc

password_hash = PasswordHash.recommended()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def fernet() -> Fernet:
    key = settings.cookie_encryption_key
    if not key:
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()).decode()
    return Fernet(key.encode())


def session_token(user_id: int) -> str:
    raw = f"{user_id}:{int((datetime.now(UTC) + timedelta(days=7)).timestamp())}"
    sig = hashlib.sha256(f"{raw}:{settings.secret_key}".encode()).hexdigest()
    return f"{raw}:{sig}"


def decode_session(token: str | None) -> int | None:
    if not token:
        return None
    try:
        uid, expiry, signature = token.split(":")
        expected = hashlib.sha256(f"{uid}:{expiry}:{settings.secret_key}".encode()).hexdigest()
        if not secrets.compare_digest(signature, expected) or int(expiry) < datetime.now(UTC).timestamp():
            return None
        return int(uid)
    except (ValueError, TypeError):
        return None


def current_user(
    archiver_session: Annotated[str | None, Cookie()] = None,
    authorization: Annotated[str | None, Header()] = None,
    _connection=Depends(db),
) -> User:
    uid = decode_session(archiver_session)
    if authorization and authorization.startswith("Bearer "):
        # Pairing codes are single-use exchange material, not API credentials.
        token = ApiToken.get_or_none(
            ApiToken.token_hash == digest(authorization[7:]),
            ApiToken.kind == "extension",
        )
        if token:
            expires_at = as_utc(token.expires_at)
            if not expires_at or expires_at > datetime.now(UTC):
                uid = token.user_id
    user = User.get_or_none(User.id == uid) if uid else None
    if not user or not user.active:
        raise HTTPException(401, "로그인이 필요합니다")
    return user


def admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "관리자 권한이 필요합니다")
    return user


def audit(actor: int | None, action: str, **detail) -> None:
    AuditLog.create(actor_id=actor, action=action, detail=json.dumps(detail, ensure_ascii=False))
