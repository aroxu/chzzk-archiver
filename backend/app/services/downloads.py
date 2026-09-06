"""Progressive (direct MP4) download strategies with progress reporting."""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import httpx

from ..config import logger
from ..db import session
from ..models import Recording
from .state import DownloadCancelled, active_processes


def update_progress(recording_id: int, downloaded: int, total: int = 0, speed_bps: int = 0) -> None:
    """Publish progress without clobbering a concurrent state change.

    A plain ``save()`` would UPDATE every column, so a tick that read the row
    before a cancel landed would resurrect the old state. The predicate keeps
    the write scoped to recordings that are still running.
    """
    fields = {
        "size": downloaded,
        "speed_bps": max(0, speed_bps),
        "eta_seconds": max(0, int((total - downloaded) / speed_bps)) if total and speed_bps else None,
    }
    if total:
        fields["total_size"] = total
    with session():
        Recording.update(**fields).where(
            Recording.id == recording_id, Recording.state == "recording"
        ).execute()


def cancel_requested(recording_id: int) -> bool:
    with session():
        recording = Recording.get_or_none(Recording.id == recording_id)
        return bool(recording and recording.state == "canceled")


async def download_progressive_aria2(
    url: str,
    destination: Path,
    cookies: dict[str, str],
    referer: str,
    recording_id: int,
    total_size: int,
) -> None:
    cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
    connections = 16
    args = [
        "aria2c", "--continue=true",
        f"--max-connection-per-server={connections}", f"--split={connections}",
        "--min-split-size=4M", "--file-allocation=none", "--summary-interval=0",
        "--console-log-level=warn", "--user-agent=Mozilla/5.0",
        f"--header=Referer: {referer}",
        f"--dir={destination.parent}",
        f"--out={destination.name}",
    ]
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
        if cancel_requested(recording_id):
            process.terminate()
            await process.wait()
            raise DownloadCancelled
        current_size = destination.stat().st_size if destination.exists() else previous_size
        now = time.monotonic()
        instant_speed = max(0, current_size - previous_size) / max(0.001, now - previous_at)
        smoothed_speed = instant_speed if not smoothed_speed else smoothed_speed * 0.75 + instant_speed * 0.25
        update_progress(recording_id, current_size, total_size, int(smoothed_speed))
        previous_size, previous_at = current_size, now
    _, error = await process.communicate()
    active_processes.pop(recording_id, None)
    if process.returncode != 0:
        raise RuntimeError(error.decode(errors="replace")[-500:])
    Path(f"{destination}.aria2").unlink(missing_ok=True)


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
            expected = (
                int(content_range.rsplit("/", 1)[1])
                if "/" in content_range
                else offset + int(response.headers.get("content-length") or 0)
            )
            logger.info("recording=%s progressive download started offset=%s expected_bytes=%s", recording_id, offset, expected)
            downloaded = offset
            last_reported_at = time.monotonic()
            last_reported_bytes = offset
            smoothed_speed = 0.0
            last_logged_at = last_reported_at
            with destination.open("ab" if offset else "wb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    if cancel_requested(recording_id):
                        raise DownloadCancelled
                    output.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if now - last_reported_at >= 0.5:
                        interval = now - last_reported_at
                        instant_speed = (downloaded - last_reported_bytes) / interval
                        smoothed_speed = instant_speed if not smoothed_speed else smoothed_speed * 0.75 + instant_speed * 0.25
                        update_progress(recording_id, downloaded, expected, int(smoothed_speed))
                        last_reported_bytes = downloaded
                        last_reported_at = now
                    if now - last_logged_at >= 5:
                        logger.info("recording=%s downloaded_bytes=%s expected_bytes=%s", recording_id, downloaded, expected)
                        last_logged_at = now
            update_progress(recording_id, downloaded, expected, int(smoothed_speed))
            logger.info("recording=%s progressive download finished bytes=%s", recording_id, downloaded)
