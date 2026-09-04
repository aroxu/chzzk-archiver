"""Lightweight additive SQLite migrations run at startup."""

from __future__ import annotations

from .db import database
from .models import ALL_MODELS

RECORDING_COLUMNS = {
    "total_size": "INTEGER DEFAULT 0",
    "speed_bps": "INTEGER DEFAULT 0",
    "eta_seconds": "INTEGER",
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
    database.execute_sql(
        "UPDATE recordings SET started_at = created_at "
        "WHERE started_at IS NULL AND state IN ('queued', 'recording', 'interrupted')"
    )
    database.execute_sql("UPDATE recordings SET total_size = size WHERE state = 'completed' AND total_size = 0")
