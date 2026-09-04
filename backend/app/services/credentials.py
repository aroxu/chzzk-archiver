"""Decryption helpers for stored CHZZK session cookies."""

from __future__ import annotations

import json

from ..models import Credential
from ..security import fernet


def user_cookies(user_ids: list[int]) -> list[dict[str, str]]:
    """Return decrypted cookie jars for the given users, newest first."""
    if not user_ids:
        return []
    rows = (
        Credential.select()
        .where(Credential.user_id.in_(user_ids), Credential.valid == True)  # noqa: E712
        .order_by(Credential.updated_at.desc())
    )
    result: list[dict[str, str]] = []
    for row in rows:
        try:
            result.append(json.loads(fernet().decrypt(row.encrypted.encode())))
        except Exception:
            continue
    return result
