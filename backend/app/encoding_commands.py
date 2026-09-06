"""Small FFprobe helpers retained by capture and migration services."""

from __future__ import annotations

import subprocess
from pathlib import Path


def probe_duration(path: Path, ffprobe: str = "ffprobe") -> float:
    """Return media duration, or zero for an unfinished/live input."""
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, timeout=30,
        )
        duration = float(result.stdout.strip())
        return duration if duration > 0 else 0.0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0
