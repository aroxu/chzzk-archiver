from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urljoin

import httpx
import streamlink
from cryptography.fernet import Fernet
from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pwdlib import PasswordHash
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event, func, select, text as sql_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARCHIVER_", env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/archiver.db"
    recordings_dir: Path = Path("./recordings")
    secret_key: str = "change-me-in-production"
    cookie_encryption_key: str | None = None
    secure_cookies: bool = False
    poll_interval: int = 60
    live_probe_concurrency: int = 8
    live_probe_timeout: float = 3.0
    live_probe_retries: int = 10
    max_recordings: int = 2
    web_dist: Path = Path("./web/dist")


settings = Settings()
settings.recordings_dir.mkdir(parents=True, exist_ok=True)
if settings.database_url.startswith("sqlite:///./"):
    Path(settings.database_url.removeprefix("sqlite:///./")).parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def sqlite_pragmas(dbapi_connection, _):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(engine, expire_on_commit=False)
password_hash = PasswordHash.recommended()
logger = logging.getLogger("uvicorn.error")


class DownloadCancelled(Exception):
    pass


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    oidc_issuer: Mapped[str | None] = mapped_column(String(255))
    oidc_subject: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    chzzk_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="알 수 없는 채널")
    image_url: Mapped[str | None] = mapped_column(Text)
    last_live: Mapped[bool] = mapped_column(Boolean, default=False)
    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="channel")


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "channel_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_record: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    channel: Mapped[Channel] = relationship(back_populates="subscriptions")


class Broadcast(Base):
    __tablename__ = "broadcasts"
    __table_args__ = (UniqueConstraint("channel_id", "broadcast_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    broadcast_id: Mapped[str] = mapped_column(String(100))
    source_type: Mapped[str] = mapped_column(String(16), default="live")
    source_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(300), default="라이브 방송")
    category: Mapped[str | None] = mapped_column(String(120))
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel: Mapped[Channel] = relationship()


class Recording(Base):
    __tablename__ = "recordings"
    id: Mapped[int] = mapped_column(primary_key=True)
    broadcast_id: Mapped[int] = mapped_column(ForeignKey("broadcasts.id", ondelete="CASCADE"), unique=True, index=True)
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    path: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer, default=0)
    total_size: Mapped[int] = mapped_column(Integer, default=0)
    speed_bps: Mapped[int] = mapped_column(Integer, default=0)
    eta_seconds: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    broadcast: Mapped[Broadcast] = relationship()


class Entitlement(Base):
    __tablename__ = "recording_entitlements"
    __table_args__ = (UniqueConstraint("user_id", "recording_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recording_id: Mapped[int] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(20), default="subscription")


class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    encrypted: Mapped[str] = mapped_column(Text)
    valid: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="extension")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class LoginBody(BaseModel):
    username: str
    password: str


class SetupBody(LoginBody):
    pass


class RegisterBody(LoginBody):
    invite: str


class SubscribeBody(BaseModel):
    channel: str
    auto_record: bool = True


class ManualDownloadBody(BaseModel):
    url: str


class UnsubscribeBody(BaseModel):
    remove_recordings: bool = False


class CookieBody(BaseModel):
    cookies: dict[str, str]


def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def fernet() -> Fernet:
    key = settings.cookie_encryption_key
    if not key:
        key = __import__("base64").urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()).decode()
    return Fernet(key.encode())


def session_token(user_id: int) -> str:
    raw = f"{user_id}:{int((datetime.now(UTC)+timedelta(days=7)).timestamp())}"
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


def current_user(archiver_session: Annotated[str | None, Cookie()] = None, authorization: Annotated[str | None, Header()] = None, s: Session = Depends(db)) -> User:
    uid = decode_session(archiver_session)
    if authorization and authorization.startswith("Bearer "):
        token = s.scalar(select(ApiToken).where(ApiToken.token_hash == digest(authorization[7:])))
        if token and (not token.expires_at or token.expires_at.replace(tzinfo=UTC) > datetime.now(UTC)):
            uid = token.user_id
    user = s.get(User, uid) if uid else None
    if not user or not user.active:
        raise HTTPException(401, "로그인이 필요합니다")
    return user


def admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "관리자 권한이 필요합니다")
    return user


def audit(s: Session, actor: int | None, action: str, **detail):
    s.add(AuditLog(actor_id=actor, action=action, detail=json.dumps(detail, ensure_ascii=False)))


def channel_id(value: str) -> str:
    match = re.search(r"(?:live/)?([a-fA-F0-9]{20,64})", value)
    if not match:
        raise HTTPException(422, "올바른 치지직 채널 URL 또는 ID가 아닙니다")
    return match.group(1)


def recording_json(r: Recording):
    reported_size = r.size
    if r.state == "recording" and r.path:
        with suppress(OSError):
            reported_size = Path(r.path).stat().st_size
    progress = round(reported_size / r.total_size * 100, 1) if r.total_size else None
    process = active_processes.get(r.id)
    process_active = bool(process and process.returncode is None)
    recorded_seconds = 0
    if r.started_at:
        started_at = r.started_at if r.started_at.tzinfo else r.started_at.replace(tzinfo=UTC)
        recorded_seconds = max(0, int(((r.finished_at or datetime.now(UTC)).replace(tzinfo=UTC) - started_at).total_seconds()))
    return {"id": r.id, "state": r.state, "type": r.broadcast.source_type, "title": r.broadcast.title, "channel": r.broadcast.channel.name, "channel_id": r.broadcast.channel.chzzk_id, "thumbnail": r.broadcast.thumbnail_url, "size": reported_size, "total_size": r.total_size, "progress": progress, "speed_bps": r.speed_bps, "eta_seconds": r.eta_seconds, "recorded_seconds": recorded_seconds, "recording_active": process_active, "created_at": r.created_at, "finished_at": r.finished_at, "error": r.error}


CONTENT_RE = re.compile(r"^https?://chzzk\.naver\.com/(?P<kind>live|video|clips)/(?P<id>[^/?#]+)")


def parse_content_url(value: str) -> tuple[str, str, str]:
    match = CONTENT_RE.match(value.strip())
    if not match:
        raise HTTPException(422, "치지직 라이브, VOD 또는 클립 URL을 입력하세요")
    kind = {"video": "vod", "clips": "clip"}.get(match["kind"], "live")
    canonical_kind = {"vod": "video", "clip": "clips"}.get(kind, "live")
    return kind, match["id"], f"https://chzzk.naver.com/{canonical_kind}/{match['id']}"


def user_cookies(s: Session, user_ids: list[int]) -> list[dict[str, str]]:
    if not user_ids:
        return []
    rows = s.scalars(select(Credential).where(Credential.user_id.in_(user_ids), Credential.valid).order_by(Credential.updated_at.desc())).all()
    result = []
    for row in rows:
        try:
            result.append(json.loads(fernet().decrypt(row.encrypted.encode())))
        except Exception:
            continue
    return result


def resolve_streamlink(url: str, cookies: dict[str, str]) -> dict:
    session = streamlink.Streamlink()
    session.http.cookies.update(cookies)
    _, plugin_class, resolved_url = session.resolve_url(url)
    plugin = plugin_class(session, resolved_url)
    streams = plugin.streams()
    if not streams:
        raise RuntimeError("다운로드 가능한 스트림이 없습니다")
    available = [name for name in streams if name not in {"best", "worst"}]
    quality = next((q for q in ("1080p60", "1080p") if q in streams), None) or ("best" if "best" in streams else available[-1])
    return {"id": str(plugin.id or hashlib.sha256(url.encode()).hexdigest()[:20]), "title": plugin.title or "치지직 영상", "author": plugin.author or "치지직", "category": plugin.category, "quality": quality}


def _playback_from_json(value: str | dict | None) -> str | None:
    if not value:
        return None
    data = json.loads(value) if isinstance(value, str) else value
    for media in data.get("media", []):
        if media.get("protocol") == "HLS" and media.get("path"):
            return media["path"]
    return None


def _progressive_from_mpd(data: bytes, manifest_url: str) -> str | None:
    root = ET.fromstring(data)
    candidates: list[tuple[int, str]] = []
    for representation in root.iter():
        if representation.tag.rsplit("}", 1)[-1] != "Representation":
            continue
        mime_type = representation.attrib.get("mimeType", "")
        codecs = representation.attrib.get("codecs", "")
        height = int(representation.attrib.get("height") or 0)
        if mime_type != "video/mp4" or "mp4a" not in codecs or height > 1080:
            continue
        for child in representation:
            if child.tag.rsplit("}", 1)[-1] == "BaseURL" and child.text:
                candidates.append((height, urljoin(manifest_url, child.text.strip())))
                break
    return max(candidates, default=(0, ""), key=lambda item: item[0])[1] or None


def resolve_direct(url: str, cookies: dict[str, str]) -> dict:
    kind, content_id, _ = parse_content_url(url)
    if kind == "vod":
        api_url = f"https://api.chzzk.naver.com/service/v2/videos/{content_id}"
    elif kind == "clip":
        api_url = f"https://api.chzzk.naver.com/service/v1/play-info/clip/{content_id}"
    else:
        raise ValueError("직접 다운로드는 VOD와 클립만 지원합니다")
    response = httpx.get(
        api_url,
        cookies=cookies,
        headers={"User-Agent": "Mozilla/5.0", "Referer": url, "Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    content = response.json().get("content") or {}
    in_key = content.get("inKey")
    video_id = content.get("videoId")
    playback_url = None
    protocol = None
    if in_key and video_id:
        playback_url = f"https://apis.naver.com/neonplayer/vodplay/v2/playback/{video_id}?key={in_key}"
        protocol = "dash"
        manifest = httpx.get(
            playback_url,
            cookies=cookies,
            headers={"User-Agent": "Mozilla/5.0", "Referer": url, "Accept": "application/dash+xml"},
            timeout=20,
        )
        manifest.raise_for_status()
        progressive = _progressive_from_mpd(manifest.content, str(manifest.url))
        if progressive:
            playback_url = progressive
            protocol = "progressive"
            head = httpx.head(progressive, cookies=cookies, headers={"User-Agent": "Mozilla/5.0", "Referer": url}, follow_redirects=True, timeout=20)
            total_size = int(head.headers.get("content-length") or 0)
    else:
        playback_url = _playback_from_json(content.get("liveRewindPlaybackJson") or content.get("playbackJson"))
        protocol = "hls" if playback_url else None
    if not playback_url:
        reason = "성인 인증이 필요합니다" if content.get("adult") else "재생 URL을 찾을 수 없습니다"
        raise RuntimeError(reason)
    channel = content.get("channel") or content.get("ownerChannel") or {}
    return {
        "id": str(content.get("videoNo") or content.get("contentId") or video_id or content_id),
        "title": content.get("videoTitle") or content.get("contentTitle") or "치지직 영상",
        "author": channel.get("channelName") or "치지직",
        "category": content.get("videoCategory"),
        "thumbnail": content.get("thumbnailImageUrl"),
        "playback_url": playback_url,
        "protocol": protocol,
        "total_size": total_size if protocol == "progressive" else 0,
    }


async def download_progressive_aria2(url: str, destination: Path, cookies: dict[str, str], referer: str, recording_id: int, total_size: int) -> None:
    cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
    args = ["aria2c", "--continue=true", "--max-connection-per-server=8", "--split=8", "--min-split-size=4M", "--file-allocation=none", "--summary-interval=0", "--console-log-level=warn", "--user-agent=Mozilla/5.0", f"--header=Referer: {referer}", f"--dir={destination.parent}", f"--out={destination.name}"]
    if cookie_header:
        args.append(f"--header=Cookie: {cookie_header}")
    args.append(url)
    process = await asyncio.create_subprocess_exec(*args, stdout=subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    active_processes[recording_id] = process
    previous_size = destination.stat().st_size if destination.exists() else 0
    previous_at = time.monotonic()
    smoothed_speed = 0.0
    while process.returncode is None:
        await asyncio.sleep(0.5)
        if _cancel_requested(recording_id):
            process.terminate()
            await process.wait()
            raise DownloadCancelled
        current_size = destination.stat().st_size if destination.exists() else previous_size
        now = time.monotonic()
        instant_speed = max(0, current_size - previous_size) / max(0.001, now - previous_at)
        smoothed_speed = instant_speed if not smoothed_speed else smoothed_speed * 0.75 + instant_speed * 0.25
        _update_progress(recording_id, current_size, total_size, int(smoothed_speed))
        previous_size, previous_at = current_size, now
    _, error = await process.communicate()
    active_processes.pop(recording_id, None)
    if process.returncode != 0:
        raise RuntimeError(error.decode(errors="replace")[-500:])
    Path(f"{destination}.aria2").unlink(missing_ok=True)


def _update_progress(recording_id: int, downloaded: int, total: int = 0, speed_bps: int = 0) -> None:
    with SessionLocal() as session:
        recording = session.get(Recording, recording_id)
        if recording and recording.state == "recording":
            recording.size = downloaded
            if total:
                recording.total_size = total
            recording.speed_bps = max(0, speed_bps)
            recording.eta_seconds = max(0, int((total - downloaded) / speed_bps)) if total and speed_bps else None
            session.commit()


def _cancel_requested(recording_id: int) -> bool:
    with SessionLocal() as session:
        return session.scalar(select(Recording.state).where(Recording.id == recording_id)) == "canceled"


def download_progressive(url: str, destination: Path, cookies: dict[str, str], referer: str, recording_id: int) -> None:
    offset = destination.stat().st_size if destination.exists() else 0
    request_headers = {"User-Agent": "Mozilla/5.0", "Referer": referer}
    if offset:
        request_headers["Range"] = f"bytes={offset}-"
    with httpx.Client(
        cookies=cookies,
        headers=request_headers,
        follow_redirects=True,
        timeout=httpx.Timeout(30, read=120),
    ) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            if offset and response.status_code != 206:
                offset = 0
            content_range = response.headers.get("content-range", "")
            expected = int(content_range.rsplit("/", 1)[1]) if "/" in content_range else offset + int(response.headers.get("content-length") or 0)
            logger.info("recording=%s progressive download started offset=%s expected_bytes=%s", recording_id, offset, expected)
            downloaded = offset
            last_reported_at = time.monotonic()
            last_reported_bytes = offset
            smoothed_speed = 0.0
            last_logged_at = last_reported_at
            with destination.open("ab" if offset else "wb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    if _cancel_requested(recording_id):
                        raise DownloadCancelled
                    output.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if now - last_reported_at >= 0.5:
                        interval = now - last_reported_at
                        instant_speed = (downloaded - last_reported_bytes) / interval
                        smoothed_speed = instant_speed if not smoothed_speed else smoothed_speed * 0.75 + instant_speed * 0.25
                        _update_progress(recording_id, downloaded, expected, int(smoothed_speed))
                        last_reported_bytes = downloaded
                        last_reported_at = now
                    if now - last_logged_at >= 5:
                        logger.info("recording=%s downloaded_bytes=%s expected_bytes=%s", recording_id, downloaded, expected)
                        last_logged_at = now
            _update_progress(recording_id, downloaded, expected, int(smoothed_speed))
            logger.info("recording=%s progressive download finished bytes=%s", recording_id, downloaded)


LIVE_PROBE_FAILED = object()


async def fetch_channel_profile(chzzk_id: str, client: httpx.AsyncClient | None = None) -> dict:
    """Resolve canonical channel metadata before storing a subscription."""
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(headers={"User-Agent": "chzzk-archiver/0.1"})
    try:
        response = await client.get(
            f"https://api.chzzk.naver.com/service/v1/channels/{chzzk_id}",
            timeout=10,
        )
        response.raise_for_status()
        content = response.json().get("content") or {}
        if content.get("channelId") != chzzk_id or not content.get("channelName"):
            raise ValueError("channel metadata missing")
        return {
            "name": content["channelName"],
            "image": content.get("channelImageUrl"),
        }
    finally:
        if owns_client:
            await client.aclose()


async def fetch_live(chzzk_id: str, client: httpx.AsyncClient) -> dict | None | object:
    """Return live metadata, None for a confirmed offline channel, or a failure sentinel."""
    url = f"https://api.chzzk.naver.com/service/v3/channels/{chzzk_id}/live-detail"
    for attempt in range(settings.live_probe_retries + 1):
        try:
            response = await client.get(url, timeout=settings.live_probe_timeout)
            response.raise_for_status()
            payload = response.json()
        except (httpx.TimeoutException, asyncio.TimeoutError):
            if attempt < settings.live_probe_retries:
                continue
            logger.warning("live probe timed out channel=%s attempts=%s", chzzk_id, attempt + 1)
            return LIVE_PROBE_FAILED
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("live probe failed channel=%s error=%s", chzzk_id, type(exc).__name__)
            return LIVE_PROBE_FAILED

        content = payload.get("content")
        if content is None or content.get("status") != "OPEN":
            return None
        channel = content.get("channel") or {}
        if not channel.get("channelId"):
            logger.warning("live probe missing channel metadata channel=%s", chzzk_id)
            return LIVE_PROBE_FAILED
        broadcast_id = content.get("liveId") or content.get("openDate")
        if not broadcast_id:
            logger.warning("live probe missing stable broadcast id channel=%s", chzzk_id)
            return LIVE_PROBE_FAILED
        thumbnail = content.get("liveImageUrl") or channel.get("channelImageUrl")
        if thumbnail:
            thumbnail = thumbnail.replace("{type}", "1080")
        return {
            "id": str(broadcast_id),
            "title": content.get("liveTitle") or "라이브 방송",
            "author": channel.get("channelName"),
            "channel_image": channel.get("channelImageUrl"),
            "category": content.get("liveCategoryValue") or content.get("liveCategory"),
            "thumbnail": thumbnail,
            "started_at": content.get("openDate"),
        }
    return LIVE_PROBE_FAILED


def ensure_recording(s: Session, ch: Channel, live: dict, users: list[int]) -> tuple[Recording, bool]:
    if live.get("author"):
        ch.name = live["author"]
    broadcast = s.scalar(select(Broadcast).where(Broadcast.channel_id == ch.id, Broadcast.broadcast_id == live["id"]))
    created = False
    if not broadcast:
        broadcast = Broadcast(channel_id=ch.id, broadcast_id=live["id"], source_type="live", source_url=f"https://chzzk.naver.com/live/{ch.chzzk_id}", title=live["title"], category=live.get("category"), thumbnail_url=live.get("thumbnail"), started_at=datetime.now(UTC))
        s.add(broadcast); s.flush()
    recording = s.scalar(select(Recording).where(Recording.broadcast_id == broadcast.id))
    if not recording:
        recording = Recording(broadcast_id=broadcast.id)
        s.add(recording)
        try:
            s.flush(); created = True
        except IntegrityError:
            s.rollback()
            broadcast = s.scalar(select(Broadcast).where(Broadcast.channel_id == ch.id, Broadcast.broadcast_id == live["id"]))
            recording = s.scalar(select(Recording).where(Recording.broadcast_id == broadcast.id))
    elif recording.state == "failed":
        recording.state = "queued"
        recording.error = None
        recording.finished_at = None
        recording.started_at = None
        recording.speed_bps = 0
        recording.eta_seconds = None
        created = True
    for uid in users:
        if not s.scalar(select(Entitlement).where(Entitlement.user_id == uid, Entitlement.recording_id == recording.id)):
            s.add(Entitlement(user_id=uid, recording_id=recording.id))
    s.commit()
    return recording, created


recording_semaphore = asyncio.Semaphore(settings.max_recordings)
active_processes: dict[int, asyncio.subprocess.Process] = {}


async def monitor_live_progress(recording_id: int, path: Path, process: asyncio.subprocess.Process):
    previous_size = path.stat().st_size if path.exists() else 0
    previous_at = time.monotonic()
    monitor_start_size = previous_size
    monitor_started_at = previous_at
    last_growth_at: float | None = None
    while process.returncode is None:
        await asyncio.sleep(1)
        current_size = path.stat().st_size if path.exists() else 0
        now = time.monotonic()
        elapsed = max(0.001, now - previous_at)
        instant_speed = max(0, current_size - previous_size) / elapsed
        if instant_speed > 0:
            last_growth_at = now
        average_speed = max(0, current_size - monitor_start_size) / max(0.001, now - monitor_started_at)
        recently_writing = last_growth_at is not None and now - last_growth_at <= 15
        with SessionLocal() as session:
            recording = session.get(Recording, recording_id)
            if not recording or recording.state != "recording":
                return
            recording.size = current_size
            recording.speed_bps = int(average_speed) if recently_writing else 0
            recording.eta_seconds = None
            session.commit()
        previous_size = current_size
        previous_at = now


async def run_recording(recording_id: int):
    async with recording_semaphore:
        with SessionLocal() as s:
            rec = s.get(Recording, recording_id)
            if not rec or rec.state != "queued": return
            rec.state = "recording"
            if not rec.started_at:
                rec.started_at = datetime.now(UTC)
            s.commit()
            safe = re.sub(r"[^\w.-]+", "_", rec.broadcast.channel.name, flags=re.UNICODE)[:80]
            folder = settings.recordings_dir / safe / str(datetime.now().year) / f"{datetime.now().month:02d}"
            folder.mkdir(parents=True, exist_ok=True)
            safe_broadcast_id = re.sub(r"[^\w.-]+", "_", rec.broadcast.broadcast_id, flags=re.UNICODE)[:100]
            base = f"{datetime.now():%Y%m%d-%H%M%S}-{safe_broadcast_id}"
            temp, final = folder / f"{base}.ts", folder / f"{base}.mp4"
            partials = list(folder.glob(f"*-{safe_broadcast_id}.ts"))
            if rec.path and rec.path.endswith(".ts") and Path(rec.path).exists():
                partials.append(Path(rec.path))
            if partials:
                temp = max(set(partials), key=lambda path: path.stat().st_size)
                final = temp.with_suffix(".mp4")
            owners = list(s.scalars(select(Entitlement.user_id).where(Entitlement.recording_id == rec.id)))
            cookie_candidates = user_cookies(s, owners) or [{}]
            url = rec.broadcast.source_url or f"https://chzzk.naver.com/live/{rec.broadcast.channel.chzzk_id}"
            source_type = rec.broadcast.source_type
            rec.path = str(temp); rec.size = temp.stat().st_size if temp.exists() else 0; s.commit()
            logger.info("recording=%s started type=%s title=%s", recording_id, source_type, rec.broadcast.title)
        try:
            errors = []
            for cookies in cookie_candidates:
                output_handle = None
                stdout_target = subprocess.DEVNULL
                if source_type == "live":
                    args = ["streamlink", "--stdout"]
                    for key, value in cookies.items():
                        args += ["--http-cookie", f"{key}={value}"]
                    args += [url, "1080p60,1080p,best"]
                    output_handle = temp.open("ab")
                    stdout_target = output_handle
                else:
                    try:
                        direct = await asyncio.to_thread(resolve_direct, url, cookies)
                    except Exception as exc:
                        errors.append(str(exc))
                        continue
                    if direct["protocol"] == "progressive":
                        try:
                            if shutil.which("aria2c"):
                                logger.info("recording=%s using aria2c connections=8", recording_id)
                                await download_progressive_aria2(direct["playback_url"], temp, cookies, url, recording_id, direct.get("total_size", 0))
                            else:
                                await asyncio.to_thread(download_progressive, direct["playback_url"], temp, cookies, url, recording_id)
                            break
                        except Exception as exc:
                            errors.append(str(exc))
                            continue
                    temp.unlink(missing_ok=True)
                    cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
                    headers = f"Referer: {url}\r\nAccept: application/dash+xml,application/vnd.apple.mpegurl,*/*\r\n"
                    if cookie_header:
                        headers += f"Cookie: {cookie_header}\r\n"
                    args = [
                        "ffmpeg", "-y", "-loglevel", "warning", "-user_agent", "Mozilla/5.0",
                        "-headers", headers, "-i", direct["playback_url"], "-c", "copy", "-f", "mpegts", str(temp),
                    ]
                progress_task = None
                try:
                    proc = await asyncio.create_subprocess_exec(*args, stdout=stdout_target, stderr=asyncio.subprocess.PIPE)
                    active_processes[recording_id] = proc
                    if source_type == "live":
                        progress_task = asyncio.create_task(monitor_live_progress(recording_id, temp, proc))
                    _, err = await proc.communicate()
                finally:
                    active_processes.pop(recording_id, None)
                    if progress_task:
                        progress_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await progress_task
                    if output_handle:
                        output_handle.close()
                if _cancel_requested(recording_id):
                    raise DownloadCancelled
                if proc.returncode == 0:
                    if source_type == "live":
                        with SessionLocal() as s:
                            current = s.get(Recording, recording_id)
                            current.size = temp.stat().st_size if temp.exists() else 0
                            current.speed_bps = 0
                            s.commit()
                    break
                errors.append(err.decode(errors="replace")[-500:])
            else:
                raise RuntimeError("; ".join(errors))
            remux = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-i", str(temp), "-c", "copy", "-movflags", "+faststart", str(final), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            active_processes[recording_id] = remux
            _, err = await remux.communicate()
            active_processes.pop(recording_id, None)
            if _cancel_requested(recording_id):
                raise DownloadCancelled
            if remux.returncode != 0: raise RuntimeError(err.decode(errors="replace")[-1000:])
            temp.unlink(missing_ok=True)
            with SessionLocal() as s:
                rec = s.get(Recording, recording_id); rec.state="completed"; rec.path=str(final); rec.size=final.stat().st_size; rec.total_size=rec.size; rec.speed_bps=0; rec.eta_seconds=0; rec.finished_at=datetime.now(UTC); s.commit()
            logger.info("recording=%s completed bytes=%s", recording_id, final.stat().st_size)
        except DownloadCancelled:
            active_processes.pop(recording_id, None)
            temp.unlink(missing_ok=True)
            Path(f"{temp}.aria2").unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            with SessionLocal() as s:
                rec = s.get(Recording, recording_id); rec.state="canceled"; rec.path=None; rec.size=0; rec.total_size=0; rec.speed_bps=0; rec.eta_seconds=None; rec.error=None; rec.finished_at=datetime.now(UTC); s.commit()
            logger.info("recording=%s canceled", recording_id)
        except Exception as exc:
            active_processes.pop(recording_id, None)
            with SessionLocal() as s:
                rec = s.get(Recording, recording_id); rec.state="failed"; rec.path=str(temp) if temp.exists() else None; rec.size=temp.stat().st_size if temp.exists() else 0; rec.speed_bps=0; rec.eta_seconds=None; rec.error=re.sub(r"https?://\S+", "[redacted-url]", str(exc))[-1000:]; rec.finished_at=datetime.now(UTC); s.commit()
            logger.error("recording=%s failed error=%s", recording_id, re.sub(r"https?://\S+", "[redacted-url]", str(exc))[-500:])


async def monitor_live_channels_once() -> list[int]:
    """Probe every unique auto-recorded channel once and enqueue new broadcasts."""
    with SessionLocal() as session:
        channels = list(session.execute(
            select(Channel.id, Channel.chzzk_id)
            .join(Subscription)
            .where(Subscription.active, Subscription.auto_record)
            .distinct()
        ))
    if not channels:
        return []

    semaphore = asyncio.Semaphore(max(1, settings.live_probe_concurrency))
    async with httpx.AsyncClient(headers={"User-Agent": "chzzk-archiver/0.1"}) as client:
        async def probe(channel_id: int, chzzk_id: str):
            async with semaphore:
                return channel_id, await fetch_live(chzzk_id, client)

        results = await asyncio.gather(*(probe(channel_id, chzzk_id) for channel_id, chzzk_id in channels))

    started: list[int] = []
    for channel_id, live in results:
        if live is LIVE_PROBE_FAILED:
            continue
        with SessionLocal() as session:
            channel = session.get(Channel, channel_id)
            if not channel:
                continue
            if live is None:
                if channel.last_live:
                    channel.last_live = False
                    session.commit()
                continue

            channel.last_live = True
            if live.get("author"):
                channel.name = live["author"]
            if live.get("channel_image"):
                channel.image_url = live["channel_image"]
            users = list(session.scalars(
                select(Subscription.user_id).where(
                    Subscription.channel_id == channel.id,
                    Subscription.active,
                    Subscription.auto_record,
                )
            ))
            recording, created = ensure_recording(session, channel, live, users)
            if created:
                started.append(recording.id)
                asyncio.create_task(run_recording(recording.id))
    return started


async def scheduler():
    while True:
        started_at = time.monotonic()
        try:
            await monitor_live_channels_once()
        except Exception:
            logger.exception("live monitor cycle failed")
        elapsed = time.monotonic() - started_at
        await asyncio.sleep(max(0.0, max(30, settings.poll_interval) - elapsed))


async def backfill_thumbnails():
    with SessionLocal() as session:
        pending = [(row.id, row.source_url) for row in session.scalars(select(Broadcast).where(Broadcast.thumbnail_url.is_(None), Broadcast.source_type.in_(["vod", "clip"]))).all() if row.source_url]
    for broadcast_id, source_url in pending:
        try:
            with SessionLocal() as session:
                recording_id = session.scalar(select(Recording.id).where(Recording.broadcast_id == broadcast_id))
                owners = list(session.scalars(select(Entitlement.user_id).where(Entitlement.recording_id == recording_id))) if recording_id else []
                cookies = (user_cookies(session, owners) or [{}])[0]
            metadata = await asyncio.to_thread(resolve_direct, source_url, cookies)
            with SessionLocal() as session:
                broadcast = session.get(Broadcast, broadcast_id)
                if broadcast:
                    broadcast.thumbnail_url = metadata.get("thumbnail")
                    session.commit()
        except Exception as exc:
            logger.warning("thumbnail backfill failed broadcast=%s error=%s", broadcast_id, str(exc)[:200])


async def backfill_channel_profiles():
    with SessionLocal() as session:
        channels = [(row.id, row.chzzk_id) for row in session.scalars(select(Channel)).all() if row.name.startswith("채널 ") or not row.image_url]
    if not channels:
        return
    async with httpx.AsyncClient(headers={"User-Agent": "chzzk-archiver/0.1"}) as client:
        for channel_id_value, chzzk_id_value in channels:
            try:
                profile = await fetch_channel_profile(chzzk_id_value, client)
                with SessionLocal() as session:
                    channel = session.get(Channel, channel_id_value)
                    if channel:
                        channel.name = profile["name"]
                        channel.image_url = profile.get("image")
                        session.commit()
            except Exception as exc:
                logger.warning("channel profile backfill failed channel=%s error=%s", chzzk_id_value, type(exc).__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    if settings.database_url.startswith("sqlite"):
        with engine.begin() as connection:
            columns = {row[1] for row in connection.execute(sql_text("PRAGMA table_info(recordings)"))}
            if "total_size" not in columns:
                connection.execute(sql_text("ALTER TABLE recordings ADD COLUMN total_size INTEGER DEFAULT 0"))
            if "speed_bps" not in columns:
                connection.execute(sql_text("ALTER TABLE recordings ADD COLUMN speed_bps INTEGER DEFAULT 0"))
            if "eta_seconds" not in columns:
                connection.execute(sql_text("ALTER TABLE recordings ADD COLUMN eta_seconds INTEGER"))
            if "started_at" not in columns:
                connection.execute(sql_text("ALTER TABLE recordings ADD COLUMN started_at DATETIME"))
            connection.execute(
                sql_text(
                    "UPDATE recordings SET started_at = created_at "
                    "WHERE started_at IS NULL AND state IN ('queued', 'recording', 'interrupted')"
                )
            )
            connection.execute(sql_text("UPDATE recordings SET total_size = size WHERE state = 'completed' AND total_size = 0"))
    with SessionLocal() as s:
        resume_ids = []
        for rec in s.scalars(select(Recording).where(Recording.state.in_(["recording", "interrupted"]))):
            rec.state = "queued"
            resume_ids.append(rec.id)
        s.commit()
    task = asyncio.create_task(scheduler())
    asyncio.create_task(backfill_thumbnails())
    asyncio.create_task(backfill_channel_profiles())
    for recording_id in resume_ids:
        asyncio.create_task(run_recording(recording_id))
    yield
    task.cancel()


app = FastAPI(title="CHZZK Archive", lifespan=lifespan)


@app.get("/health/live")
def health(): return {"status": "ok"}


@app.get("/api/auth/status")
def auth_status(s: Session = Depends(db)): return {"setup_required": not bool(s.scalar(select(User.id).limit(1)))}


@app.post("/api/auth/setup")
def setup(body: SetupBody, response: Response, s: Session = Depends(db)):
    if s.scalar(select(User.id).limit(1)): raise HTTPException(409, "초기 설정이 완료되었습니다")
    user = User(username=body.username, password=password_hash.hash(body.password), role="admin")
    s.add(user); s.commit(); response.set_cookie("archiver_session", session_token(user.id), httponly=True, secure=settings.secure_cookies, samesite="lax")
    return {"id": user.id, "username": user.username, "role": user.role}


@app.post("/api/auth/login")
def login(body: LoginBody, response: Response, s: Session = Depends(db)):
    user = s.scalar(select(User).where(User.username == body.username))
    if not user or not user.password or not password_hash.verify(body.password, user.password): raise HTTPException(401, "로그인 정보가 올바르지 않습니다")
    response.set_cookie("archiver_session", session_token(user.id), httponly=True, secure=settings.secure_cookies, samesite="lax")
    audit(s, user.id, "login"); s.commit(); return {"id": user.id, "username": user.username, "role": user.role}


@app.post("/api/auth/register")
def register(body: RegisterBody, response: Response, s: Session = Depends(db)):
    inv = s.scalar(select(Invite).where(Invite.token_hash == digest(body.invite), Invite.used_at.is_(None)))
    if not inv or inv.expires_at.replace(tzinfo=UTC) < datetime.now(UTC): raise HTTPException(400, "유효하지 않은 초대입니다")
    user = User(username=body.username, password=password_hash.hash(body.password)); s.add(user); s.flush(); inv.used_at=datetime.now(UTC); s.commit()
    response.set_cookie("archiver_session", session_token(user.id), httponly=True, secure=settings.secure_cookies, samesite="lax"); return {"id": user.id, "username": user.username, "role": user.role}


@app.post("/api/auth/logout", status_code=204)
def logout(response: Response): response.delete_cookie("archiver_session")


@app.get("/api/me")
def me(user: User = Depends(current_user), s: Session = Depends(db)):
    cred = s.scalar(select(Credential).where(Credential.user_id == user.id))
    return {"id": user.id, "username": user.username, "role": user.role, "cookie_status": "valid" if cred and cred.valid else "missing"}


@app.get("/api/subscriptions")
def subscriptions(user: User = Depends(current_user), s: Session = Depends(db)):
    rows = s.scalars(select(Subscription).where(Subscription.user_id == user.id, Subscription.active)).all()
    return [{"id": x.id, "channel_id": x.channel.chzzk_id, "name": x.channel.name, "image": x.channel.image_url, "live": x.channel.last_live, "auto_record": x.auto_record} for x in rows]


@app.post("/api/subscriptions")
async def subscribe(body: SubscribeBody, user: User = Depends(current_user), s: Session = Depends(db)):
    cid = channel_id(body.channel)
    try:
        profile = await fetch_channel_profile(cid)
    except Exception as exc:
        logger.warning("channel profile lookup failed channel=%s error=%s", cid, type(exc).__name__)
        raise HTTPException(502, "치지직 채널 정보를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
    ch = s.scalar(select(Channel).where(Channel.chzzk_id == cid))
    if not ch: ch=Channel(chzzk_id=cid, name=profile["name"], image_url=profile.get("image")); s.add(ch); s.flush()
    else: ch.name=profile["name"]; ch.image_url=profile.get("image")
    sub=s.scalar(select(Subscription).where(Subscription.user_id==user.id, Subscription.channel_id==ch.id))
    if sub: sub.active=True; sub.auto_record=body.auto_record
    else: sub=Subscription(user_id=user.id, channel_id=ch.id, auto_record=body.auto_record); s.add(sub); s.flush()
    recordings=s.scalars(select(Recording).join(Broadcast).where(Broadcast.channel_id==ch.id)).all()
    for rec in recordings:
        if not s.scalar(select(Entitlement).where(Entitlement.user_id==user.id, Entitlement.recording_id==rec.id)): s.add(Entitlement(user_id=user.id, recording_id=rec.id))
    audit(s,user.id,"subscribe",channel=cid); s.commit(); return {"id":sub.id,"channel_id":cid,"name":ch.name,"image":ch.image_url,"auto_record":sub.auto_record}


@app.post("/api/subscriptions/{subscription_id}/unsubscribe", status_code=204)
def unsubscribe(subscription_id:int, body:UnsubscribeBody, user:User=Depends(current_user), s:Session=Depends(db)):
    sub=s.scalar(select(Subscription).where(Subscription.id==subscription_id, Subscription.user_id==user.id))
    if not sub: raise HTTPException(404)
    sub.active=False
    if body.remove_recordings:
        ids=list(s.scalars(select(Recording.id).join(Broadcast).where(Broadcast.channel_id==sub.channel_id)))
        ents=s.scalars(select(Entitlement).where(Entitlement.user_id==user.id, Entitlement.recording_id.in_(ids))).all() if ids else []
        for ent in ents: s.delete(ent)
    audit(s,user.id,"unsubscribe",channel_id=sub.channel_id,remove_recordings=body.remove_recordings); s.commit()


@app.get("/api/recordings")
def recordings(user:User=Depends(current_user), s:Session=Depends(db)):
    rows=s.scalars(select(Recording).join(Entitlement).where(Entitlement.user_id==user.id).order_by(Recording.created_at.desc())).unique().all()
    return [recording_json(x) for x in rows]


@app.post("/api/recordings/manual")
async def manual(body:ManualDownloadBody, background:BackgroundTasks, user:User=Depends(current_user), s:Session=Depends(db)):
    kind, content_id, url = parse_content_url(body.url)
    cookies = user_cookies(s, [user.id])
    try:
        resolver = resolve_streamlink if kind == "live" else resolve_direct
        metadata = await asyncio.to_thread(resolver, url, cookies[0] if cookies else {})
    except Exception as exc:
        raise HTTPException(409, f"영상을 열 수 없습니다: {str(exc)}") from exc
    if kind == "live":
        cid = content_id
        ch = s.scalar(select(Channel).where(Channel.chzzk_id == cid))
        if not ch:
            ch = Channel(chzzk_id=cid, name=metadata["author"]); s.add(ch); s.flush()
        users = [user.id, *s.scalars(select(Subscription.user_id).where(Subscription.channel_id==ch.id,Subscription.active,Subscription.auto_record))]
    else:
        virtual_id = f"{kind}:{content_id}"[:64]
        ch = s.scalar(select(Channel).where(Channel.chzzk_id == virtual_id))
        if not ch:
            ch = Channel(chzzk_id=virtual_id, name=metadata["author"]); s.add(ch); s.flush()
        users = [user.id]
    live = {"id": f"{kind}:{metadata['id']}", "title": metadata["title"], "category": metadata["category"], "thumbnail": metadata.get("thumbnail")}
    rec, created = ensure_recording(s, ch, live, list(set(users)))
    rec.broadcast.title = metadata["title"]
    rec.broadcast.thumbnail_url = metadata.get("thumbnail")
    ch.name = metadata["author"]
    s.commit()
    should_start = created or rec.state in {"failed", "interrupted"}
    if should_start:
        if rec.path and not rec.path.endswith(".ts"):
            Path(rec.path).unlink(missing_ok=True)
            rec.path = None; rec.size = 0; rec.total_size = 0
        rec.state = "queued"; rec.speed_bps = 0; rec.eta_seconds = None; rec.error = None; rec.finished_at = None
        rec.broadcast.source_type = kind; rec.broadcast.source_url = url; s.commit()
        background.add_task(run_recording,rec.id)
    return recording_json(rec)


def entitled(s:Session, user:User, rid:int)->Recording:
    rec=s.get(Recording,rid)
    if not rec: raise HTTPException(404)
    if user.role!="admin" and not s.scalar(select(Entitlement.id).where(Entitlement.user_id==user.id,Entitlement.recording_id==rid)): raise HTTPException(404)
    return rec


@app.get("/api/media/{recording_id}")
def media(recording_id:int, request:Request, user:User=Depends(current_user), s:Session=Depends(db)):
    rec=entitled(s,user,recording_id)
    if rec.state != "completed": raise HTTPException(409,"다운로드가 완료되지 않았습니다")
    if not rec.path or not Path(rec.path).exists(): raise HTTPException(404,"파일이 없습니다")
    path=Path(rec.path); range_header=request.headers.get("range")
    if not range_header: return FileResponse(path, media_type="video/mp4", headers={"Accept-Ranges":"bytes"})
    start_s,end_s=range_header.removeprefix("bytes=").split("-",1); start=int(start_s); end=min(int(end_s) if end_s else start+1024*1024-1,path.stat().st_size-1)
    def chunks():
        with path.open("rb") as f: f.seek(start); yield f.read(end-start+1)
    return StreamingResponse(chunks(),206,{"Content-Range":f"bytes {start}-{end}/{path.stat().st_size}","Accept-Ranges":"bytes","Content-Length":str(end-start+1)},"video/mp4")


@app.delete("/api/recordings/{recording_id}",status_code=204)
def remove_recording(recording_id:int,user:User=Depends(current_user),s:Session=Depends(db)):
    rec=entitled(s,user,recording_id); ent=s.scalar(select(Entitlement).where(Entitlement.user_id==user.id,Entitlement.recording_id==recording_id)); s.delete(ent); s.flush()
    if not s.scalar(select(Entitlement.id).where(Entitlement.recording_id==recording_id)) and rec.state not in ("queued","recording"):
        if rec.path: Path(rec.path).unlink(missing_ok=True)
        s.delete(rec)
    s.commit()


@app.post("/api/recordings/{recording_id}/cancel", status_code=204)
def cancel_recording(recording_id:int,user:User=Depends(current_user),s:Session=Depends(db)):
    rec=entitled(s,user,recording_id)
    if rec.state not in {"queued","recording"}: raise HTTPException(409,"진행 중인 작업이 아닙니다")
    rec.state="canceled"; rec.finished_at=datetime.now(UTC); s.commit()
    process=active_processes.get(recording_id)
    if process and process.returncode is None:
        process.terminate()


@app.post("/api/me/pair")
def pair(user:User=Depends(current_user),s:Session=Depends(db)):
    raw=secrets.token_urlsafe(32); s.add(ApiToken(user_id=user.id,token_hash=digest(raw),expires_at=datetime.now(UTC)+timedelta(minutes=10),kind="pairing")); s.commit(); return {"code":raw,"expires_in":600}


@app.post("/api/extension/exchange")
def exchange(code:str,s:Session=Depends(db)):
    pair=s.scalar(select(ApiToken).where(ApiToken.token_hash==digest(code),ApiToken.kind=="pairing"))
    if not pair or not pair.expires_at or pair.expires_at.replace(tzinfo=UTC)<datetime.now(UTC): raise HTTPException(400,"페어링 코드가 만료되었습니다")
    raw=secrets.token_urlsafe(40); s.add(ApiToken(user_id=pair.user_id,token_hash=digest(raw))); s.delete(pair); s.commit(); return {"token":raw}


@app.put("/api/extension/cookies")
def update_cookies(body:CookieBody,user:User=Depends(current_user),s:Session=Depends(db)):
    allowed={k:v for k,v in body.cookies.items() if k in {"NID_AUT","NID_SES"}}
    if not allowed: raise HTTPException(422,"허용된 인증 쿠키가 없습니다")
    encrypted=fernet().encrypt(json.dumps(allowed).encode()).decode(); cred=s.scalar(select(Credential).where(Credential.user_id==user.id))
    if cred: cred.encrypted=encrypted; cred.valid=True; cred.updated_at=datetime.now(UTC)
    else: s.add(Credential(user_id=user.id,encrypted=encrypted))
    s.commit(); return {"status":"synced","names":list(allowed)}


@app.post("/api/admin/invites")
def create_invite(minutes:int=1440,user:User=Depends(admin),s:Session=Depends(db)):
    raw=secrets.token_urlsafe(24); s.add(Invite(token_hash=digest(raw),expires_at=datetime.now(UTC)+timedelta(minutes=min(minutes,10080)))); audit(s,user.id,"invite.create"); s.commit(); return {"token":raw,"expires_in":minutes*60}


@app.get("/api/admin/overview")
def overview(_:User=Depends(admin),s:Session=Depends(db)):
    usage=shutil.disk_usage(settings.recordings_dir)
    return {"users":s.scalar(select(func.count(User.id))),"subscriptions":s.scalar(select(func.count(Subscription.id)).where(Subscription.active)),"recordings":s.scalar(select(func.count(Recording.id))),"disk":{"total":usage.total,"used":usage.used,"percent":round(usage.used/usage.total*100,1)}}


if settings.web_dist.exists():
    app.mount("/assets",StaticFiles(directory=settings.web_dist/"assets"),name="assets")
    @app.get("/{path:path}")
    def spa(path:str): return FileResponse(settings.web_dist/"index.html")
