"""Byte-exact mirroring of a selected remote HLS media playlist."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from ..config import logger
from ..db import session
from ..models import Recording
from .chzzk import _hls_variant
from .state import DownloadCancelled, active_captures

URI_ATTRIBUTE = re.compile(r'URI="([^"]+)"')


def _safe_url(value: str) -> str:
    """Keep query signatures/tokens out of logs while retaining diagnostics."""
    parsed = urlparse(value)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


@dataclass
class Segment:
    sequence: int
    url: str
    lines: list[str]
    filename: str
    byte_range: str | None = None


def _extension(url: str, default: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix and len(suffix) <= 8 else default


def _filename(url: str, sequence: int) -> str:
    """Keep the CDN segment basename/extension while preventing path escape."""
    name = Path(urlparse(url).path).name
    if not name or name in {".", ".."}:
        return f"segment_{sequence:010d}.bin"
    return name


def _snapshot(text: str, playlist_url: str) -> tuple[list[str], list[Segment], bool, float]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    sequence = 0
    target_duration = 2.0
    headers = ["#EXTM3U"]
    pending: list[str] = []
    segments: list[Segment] = []
    ended = "#EXT-X-ENDLIST" in lines
    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.partition(":")[2])
            except ValueError:
                sequence = 0
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            with_value = line.partition(":")[2]
            try:
                target_duration = float(with_value)
            except ValueError:
                pass
            headers.append(line)
        elif line.startswith(("#EXT-X-VERSION:", "#EXT-X-INDEPENDENT-SEGMENTS")):
            if line not in headers:
                headers.append(line)
        elif line.startswith("#EXT-X-MAP:"):
            headers.append(line)
        elif line.startswith("#EXT-X-ENDLIST"):
            continue
        elif line.startswith("#"):
            if line != "#EXTM3U" and not line.startswith("#EXT-X-PLAYLIST-TYPE:"):
                pending.append(line)
        else:
            index = len(segments)
            byte_range = next((item.partition(":")[2] for item in pending if item.startswith("#EXT-X-BYTERANGE:")), None)
            segments.append(
                Segment(
                    sequence=sequence + index,
                    url=urljoin(playlist_url, line),
                    lines=pending,
                    filename=_filename(line, sequence + index),
                    byte_range=byte_range,
                )
            )
            pending = []
    return headers, segments, ended, target_duration


async def _download(client: httpx.AsyncClient, url: str, destination: Path, byte_range: str | None = None) -> None:
    if destination.is_file() and destination.stat().st_size > 0:
        logger.debug("hls segment cache hit file=%s bytes=%s", destination.name, destination.stat().st_size)
        return
    headers = {}
    if byte_range:
        length, _, offset = byte_range.partition("@")
        if length.isdigit():
            start = int(offset) if offset.isdigit() else 0
            headers["Range"] = f"bytes={start}-{start + int(length) - 1}"
    error: Exception | None = None
    for attempt in range(3):
        try:
            response = await client.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            temporary = destination.with_name(f".{destination.name}.part")
            temporary.write_bytes(response.content)
            temporary.replace(destination)
            logger.debug("hls segment downloaded file=%s bytes=%s", destination.name, destination.stat().st_size)
            return
        except (httpx.HTTPError, OSError) as exc:
            error = exc
            logger.warning("hls segment retry file=%s attempt=%s error=%s", destination.name, attempt + 1, type(exc).__name__)
            await asyncio.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"HLS 조각 다운로드 실패: {type(error).__name__}")


async def _localize_header(client: httpx.AsyncClient, line: str, playlist_url: str, destination: Path) -> str:
    match = URI_ATTRIBUTE.search(line)
    if not match:
        return line
    remote = urljoin(playlist_url, match.group(1))
    kind = "init" if line.startswith("#EXT-X-MAP:") else "key"
    digest = hashlib.sha256(urlparse(remote).path.encode()).hexdigest()[:10]
    local = f"{kind}-{digest}{_extension(remote, '.bin')}"
    await _download(client, remote, destination / local)
    return line[: match.start(1)] + local + line[match.end(1) :]


def _duration(entries: list[Segment]) -> float:
    total = 0.0
    for entry in entries:
        for line in entry.lines:
            if line.startswith("#EXTINF:"):
                try:
                    total += float(line.partition(":")[2].partition(",")[0])
                except ValueError:
                    pass
    return total


def _write_playlist(destination: Path, headers: list[str], entries: list[Segment], *, ended: bool) -> None:
    lines = list(dict.fromkeys(headers))
    if entries:
        lines.append(f"#EXT-X-MEDIA-SEQUENCE:{entries[0].sequence}")
    for entry in entries:
        lines.extend(entry.lines)
        lines.append(entry.filename)
    if ended:
        lines.append("#EXT-X-ENDLIST")
    temporary = destination / ".master.m3u8.part"
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(destination / "master.m3u8")


async def mirror_hls(
    source_url: str,
    destination: Path,
    *,
    recording_id: int,
    referer: str,
    cookies: dict[str, str],
    live: bool,
    concurrency: int = 10,
    max_segments: int | None = None,
) -> tuple[Path, int, float]:
    """Mirror original init/segment bytes and write a fully local playlist."""
    destination.mkdir(parents=True, exist_ok=True)
    logger.info(
        "hls mirror started recording=%s source=%s live=%s concurrency=%s",
        recording_id, _safe_url(source_url), live, concurrency,
    )
    client_headers = {"User-Agent": "Mozilla/5.0", "Referer": referer}
    semaphore = asyncio.Semaphore(max(1, min(16, concurrency)))
    captured: dict[int, Segment] = {}
    active_captures.add(recording_id)
    failures = 0
    try:
        async with httpx.AsyncClient(headers=client_headers, cookies=cookies, follow_redirects=True) as client:
            response = await client.get(source_url, timeout=20)
            response.raise_for_status()
            variant_url, _ = _hls_variant(response.content, str(response.url))
            playlist_url = variant_url or str(response.url)
            logger.info(
                "hls master resolved recording=%s status=%s selected_variant=%s",
                recording_id, response.status_code, _safe_url(playlist_url),
            )
            while True:
                with session():
                    if Recording.get_by_id(recording_id).state == "canceled":
                        raise DownloadCancelled
                try:
                    playlist = await client.get(playlist_url, timeout=20)
                    playlist.raise_for_status()
                    headers, current, ended, target_duration = _snapshot(playlist.text, str(playlist.url))
                    failures = 0
                    logger.info(
                        "hls playlist polled recording=%s sequence=%s segments=%s ended=%s target_duration=%.2fs",
                        recording_id,
                        current[0].sequence if current else "-",
                        len(current),
                        ended,
                        target_duration,
                    )
                except httpx.HTTPError:
                    failures += 1
                    if failures >= 5 and captured:
                        break
                    if failures >= 5:
                        raise
                    await asyncio.sleep(1)
                    continue

                localized_headers = [
                    await _localize_header(client, line, str(playlist.url), destination)
                    for line in headers
                ]
                missing = [entry for entry in current if entry.sequence not in captured]
                if max_segments is not None:
                    missing = missing[: max(0, max_segments - len(captured))]

                async def fetch(entry: Segment) -> None:
                    async with semaphore:
                        await _download(client, entry.url, destination / entry.filename, entry.byte_range)

                await asyncio.gather(*(fetch(entry) for entry in missing))
                for entry in missing:
                    localized_lines = []
                    for line in entry.lines:
                        localized_lines.append(await _localize_header(client, line, str(playlist.url), destination))
                    entry.lines = localized_lines
                    captured[entry.sequence] = entry
                entries = [captured[key] for key in sorted(captured)]
                _write_playlist(destination, localized_headers, entries, ended=ended or (not live))
                size = sum(path.stat().st_size for path in destination.iterdir() if path.is_file())
                with session():
                    Recording.update(size=size, speed_bps=0).where(
                        Recording.id == recording_id, Recording.state == "recording"
                    ).execute()
                logger.info(
                    "hls batch stored recording=%s new_segments=%s total_segments=%s bytes=%s",
                    recording_id, len(missing), len(captured), size,
                )
                if max_segments is not None and len(captured) >= max_segments:
                    break
                if ended or not live:
                    break
                await asyncio.sleep(max(1.0, min(5.0, target_duration / 2)))
        entries = [captured[key] for key in sorted(captured)]
        if not entries:
            raise RuntimeError("HLS 재생목록에 미디어 조각이 없습니다")
        _write_playlist(destination, localized_headers, entries, ended=True)
        size = sum(path.stat().st_size for path in destination.iterdir() if path.is_file())
        logger.info(
            "hls mirror completed recording=%s segments=%s bytes=%s duration=%.2fs",
            recording_id, len(entries), size, _duration(entries),
        )
        return destination / "master.m3u8", size, _duration(entries)
    finally:
        active_captures.discard(recording_id)
