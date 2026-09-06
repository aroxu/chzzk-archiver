"""Application settings loaded from the environment."""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARCHIVER_", env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/archiver.db"
    database_timeout: float = 15.0
    recordings_dir: Path = Path("./recordings")
    secret_key: str = "change-me-in-production"
    cookie_encryption_key: str | None = None
    secure_cookies: bool = False
    poll_interval: int = 60
    live_probe_concurrency: int = 8
    live_probe_timeout: float = 3.0
    live_probe_retries: int = 10
    max_recordings: int = 2
    download_connections: int = 16
    hls_download_concurrency: int = 16
    web_dist: Path = Path("./web/dist")


settings = Settings()
settings.recordings_dir.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("uvicorn.error")
