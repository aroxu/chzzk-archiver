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
    # VOD/clip acceleration. Streamlink supports at most 10 segment workers;
    # aria2 supports at most 16 connections to one origin.
    download_segment_threads: int = 10
    download_connections: int = 16
    encoding_mode: str = "local"
    # auto prefers a working GPU encoder and falls back to libx265.
    encoding_video_encoder: str = "auto"
    encoding_quality: int = 23
    # Interpreted per encoder: libx265 presets, NVENC p1-p7, QSV/AMF names.
    # "auto" lets each encoder pick its own balanced default.
    encoding_preset: str = "auto"
    encoding_audio: str = "copy"
    max_encodings: int = 1
    worker_token: str | None = None
    worker_lease_seconds: int = 90
    worker_stream_host: str | None = None
    worker_stream_port: int = 8011
    worker_stream_chunk_size: int = 1024 * 1024
    web_dist: Path = Path("./web/dist")


settings = Settings()
settings.recordings_dir.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("uvicorn.error")
