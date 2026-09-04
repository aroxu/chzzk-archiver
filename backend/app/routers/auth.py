"""First-run setup, login, invite registration and session identity."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from peewee import IntegrityError

from ..config import settings
from ..db import database, db
from ..models import Credential, Invite, User, as_utc
from ..schemas import LoginBody, RegisterBody, SetupBody
from ..security import audit, current_user, digest, password_hash, session_token

router = APIRouter()


def _issue_session(response: Response, user: User) -> dict:
    response.set_cookie(
        "archiver_session",
        session_token(user.id),
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
    )
    return {"id": user.id, "username": user.username, "role": user.role}


def _create_user(username: str, password: str, role: str = "user") -> User:
    """Create a user, turning the unique-username collision into a 409."""
    try:
        return User.create(username=username, password=password_hash.hash(password), role=role)
    except IntegrityError as exc:
        raise HTTPException(409, "이미 사용 중인 사용자 이름입니다") from exc


@router.get("/api/auth/status")
def auth_status(_=Depends(db)):
    return {"setup_required": not User.select().exists()}


@router.post("/api/auth/setup")
def setup(body: SetupBody, response: Response, _=Depends(db)):
    if User.select().exists():
        raise HTTPException(409, "초기 설정이 완료되었습니다")
    user = _create_user(body.username, body.password, role="admin")
    return _issue_session(response, user)


@router.post("/api/auth/login")
def login(body: LoginBody, response: Response, _=Depends(db)):
    user = User.get_or_none(User.username == body.username)
    if not user or not user.password or not password_hash.verify(body.password, user.password):
        raise HTTPException(401, "로그인 정보가 올바르지 않습니다")
    audit(user.id, "login")
    return _issue_session(response, user)


@router.post("/api/auth/register")
def register(body: RegisterBody, response: Response, _=Depends(db)):
    # Consuming the invite and creating the user must succeed or fail together,
    # otherwise a rejected signup can still burn a single-use invite.
    with database.atomic():
        invite = Invite.get_or_none(Invite.token_hash == digest(body.invite), Invite.used_at.is_null(True))
        if not invite or as_utc(invite.expires_at) < datetime.now(UTC):
            raise HTTPException(400, "유효하지 않은 초대입니다")
        user = _create_user(body.username, body.password)
        invite.used_at = datetime.now(UTC)
        invite.save()
    return _issue_session(response, user)


@router.post("/api/auth/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie("archiver_session")


@router.get("/api/me")
def me(user: User = Depends(current_user), _=Depends(db)):
    cred = Credential.get_or_none(Credential.user == user.id)
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "cookie_status": "valid" if cred and cred.valid else "missing",
    }
