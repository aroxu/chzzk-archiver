"""Lightweight additive SQLite migrations run at startup."""

from __future__ import annotations

from .db import database
from .models import ALL_MODELS

RECORDING_COLUMNS = {
    "total_size": "INTEGER DEFAULT 0",
    "speed_bps": "INTEGER DEFAULT 0",
    "eta_seconds": "INTEGER",
    "duration_seconds": "REAL DEFAULT 0",
    "storage_version": "INTEGER NOT NULL DEFAULT 0",
    "started_at": "DATETIME",
}

ENCODING_COLUMNS = {
    "source_path": "TEXT",
    "used_encoder": "VARCHAR(40)",
    "progress": "REAL DEFAULT 0",
    "processed_seconds": "REAL DEFAULT 0",
    "duration_seconds": "REAL DEFAULT 0",
    "encoding_speed": "REAL DEFAULT 0",
    "eta_seconds": "INTEGER",
}

USER_COLUMNS = {
    "audio_format": "VARCHAR(8) NOT NULL DEFAULT 'aac'",
}

CHANNEL_COLUMNS = {
    "profile_backfilled": "INTEGER NOT NULL DEFAULT 0",
}


def migrate() -> None:
    """Create missing tables and backfill columns added after the first release."""
    database.create_tables(ALL_MODELS)
    # create_tables() only adds indexes for tables it creates, so existing
    # deployments need this one applied explicitly.
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS recordings_created_at ON recordings (created_at)"
    )
    existing = {row[1] for row in database.execute_sql("PRAGMA table_info(recordings)")}
    for column, definition in RECORDING_COLUMNS.items():
        if column not in existing:
            database.execute_sql(f"ALTER TABLE recordings ADD COLUMN {column} {definition}")
    encoding_existing = {
        row[1] for row in database.execute_sql("PRAGMA table_info(encoding_jobs)")
    }
    for column, definition in ENCODING_COLUMNS.items():
        if column not in encoding_existing:
            database.execute_sql(f"ALTER TABLE encoding_jobs ADD COLUMN {column} {definition}")
    user_existing = {row[1] for row in database.execute_sql("PRAGMA table_info(users)")}
    for column, definition in USER_COLUMNS.items():
        if column not in user_existing:
            database.execute_sql(f"ALTER TABLE users ADD COLUMN {column} {definition}")
    channel_existing = {row[1] for row in database.execute_sql("PRAGMA table_info(channels)")}
    added_profile_backfilled = "profile_backfilled" not in channel_existing
    for column, definition in CHANNEL_COLUMNS.items():
        if column not in channel_existing:
            database.execute_sql(f"ALTER TABLE channels ADD COLUMN {column} {definition}")
    if added_profile_backfilled:
        # Preserve successful work from older releases and permanently exclude
        # virtual VOD/clip owners, which are not CHZZK channel identifiers.
        database.execute_sql(
            "UPDATE channels SET profile_backfilled = 1 "
            "WHERE (image_url IS NOT NULL AND image_url != '') "
            "OR chzzk_id LIKE 'vod:%' OR chzzk_id LIKE 'clip:%'"
        )
    database.execute_sql(
        "UPDATE encoding_jobs SET output_extension = '.mp4' "
        "WHERE audio_mode = 'flac24' "
        "AND state IN ('queued', 'leased', 'encoding', 'uploading')"
    )
    database.execute_sql(
        "UPDATE recordings SET started_at = created_at "
        "WHERE started_at IS NULL AND state IN ('queued', 'recording', 'interrupted')"
    )
    database.execute_sql("UPDATE recordings SET total_size = size WHERE state = 'completed' AND total_size = 0")
