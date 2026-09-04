"""CHZZK URL parsing and public API lookups."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import httpx
import streamlink
from fastapi import HTTPException

from ..config import logger, settings

CONTENT_RE = re.compile(r"^https?://chzzk\.naver\.com/(?P<kind>live|video|clips)/(?P<id>[^/?#]+)")
ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>[\d.]+)D)?(?:T(?:(?P<hours>[\d.]+)H)?(?:(?P<minutes>[\d.]+)M)?(?:(?P<seconds>[\d.]+)S)?)?$"
)

LIVE_PROBE_FAILED = object()


def channel_id(value: str) -> str:
    match = re.search(r"(?:live/)?([a-fA-F0-9]{20,64})", value)
    if not match:
        raise HTTPException(422, "올바른 치지직 채널 URL 또는 ID가 아닙니다")
    return match.group(1)


def parse_content_url(value: str) -> tuple[str, str, str]:
    match = CONTENT_RE.match(value.strip())
    if not match:
        raise HTTPException(422, "치지직 라이브, VOD 또는 클립 URL을 입력하세요")
    kind = {"video": "vod", "clips": "clip"}.get(match["kind"], "live")
    canonical_kind = {"vod": "video", "clip": "clips"}.get(kind, "live")
    return kind, match["id"], f"https://chzzk.naver.com/{canonical_kind}/{match['id']}"


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
    return {
        "id": str(plugin.id or hashlib.sha256(url.encode()).hexdigest()[:20]),
        "title": plugin.title or "치지직 영상",
        "author": plugin.author or "치지직",
        "category": plugin.category,
        "quality": quality,
    }


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


def _mpd_estimated_size(data: bytes) -> int:
    """Estimate selected A/V bytes from DASH duration and representation bitrates."""
    root = ET.fromstring(data)
    duration_text = root.attrib.get("mediaPresentationDuration", "")
    if not duration_text:
        period = next((node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "Period"), None)
        duration_text = period.attrib.get("duration", "") if period is not None else ""
    match = ISO_DURATION_RE.match(duration_text)
    if not match:
        return 0
    values = {key: float(value or 0) for key, value in match.groupdict().items()}
    duration = (
        values["days"] * 86400
        + values["hours"] * 3600
        + values["minutes"] * 60
        + values["seconds"]
    )
    if duration <= 0:
        return 0

    videos: list[tuple[int, int]] = []
    audios: list[int] = []
    for adaptation in root.iter():
        if adaptation.tag.rsplit("}", 1)[-1] != "AdaptationSet":
            continue
        inherited_mime = adaptation.attrib.get("mimeType", "")
        inherited_type = adaptation.attrib.get("contentType", "")
        for representation in adaptation:
            if representation.tag.rsplit("}", 1)[-1] != "Representation":
                continue
            bitrate = int(representation.attrib.get("bandwidth") or 0)
            if bitrate <= 0:
                continue
            mime = representation.attrib.get("mimeType", inherited_mime)
            content_type = representation.attrib.get("contentType", inherited_type)
            height = int(representation.attrib.get("height") or 0)
            if content_type == "audio" or mime.startswith("audio/"):
                audios.append(bitrate)
            elif content_type == "video" or mime.startswith("video/") or height:
                if not height or height <= 1080:
                    videos.append((height, bitrate))
    if not videos:
        return 0
    video_bitrate = max(videos, key=lambda item: (item[0], item[1]))[1]
    audio_bitrate = max(audios, default=0)
    # The staging file is MPEG-TS, which is slightly larger than DASH fMP4.
    return int(duration * (video_bitrate + audio_bitrate) / 8 * 1.08)


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
    total_size = 0
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
        total_size = _mpd_estimated_size(manifest.content)
        progressive = _progressive_from_mpd(manifest.content, str(manifest.url))
        if progressive:
            playback_url = progressive
            protocol = "progressive"
            head = httpx.head(
                progressive,
                cookies=cookies,
                headers={"User-Agent": "Mozilla/5.0", "Referer": url},
                follow_redirects=True,
                timeout=20,
            )
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
        "total_size": total_size,
    }


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
        return {"name": content["channelName"], "image": content.get("channelImageUrl")}
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
