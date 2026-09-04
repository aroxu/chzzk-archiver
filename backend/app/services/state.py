"""Process registry and concurrency limits shared by the recorder."""

from __future__ import annotations

import asyncio

from ..config import settings


class DownloadCancelled(Exception):
    """Raised when a user cancels an in-flight recording."""


recording_semaphore = asyncio.Semaphore(settings.max_recordings)
active_processes: dict[int, asyncio.subprocess.Process] = {}
