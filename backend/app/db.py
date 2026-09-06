"""Peewee database handle and connection lifecycle helpers."""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse

from peewee import SqliteDatabase

from .config import settings


def sqlite_path(database_url: str) -> str:
    """Translate a sqlite URL into a filesystem path.

    Both the historic sqlite:///./data/archiver.db relative form and the
    absolute sqlite:////var/lib/archiver.db form are accepted so existing
    deployments and .env files keep working after the ORM migration.
    """
    if not database_url.startswith("sqlite:"):
        raise ValueError(f"지원하지 않는 데이터베이스 URL입니다: {database_url}")
    path = unquote(urlparse(database_url).path)
    # sqlite:///relative and sqlite:////absolute both leave a leading slash that
    # is part of the URL grammar rather than the path itself.
    if path.startswith("//"):
        path = path[1:]
    elif re.match(r"^/(\.\.?/|[A-Za-z]:)", path):
        path = path[1:]
    if not path or path == ":memory:":
        return ":memory:"
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return str(resolved)


database = SqliteDatabase(
    sqlite_path(settings.database_url),
    pragmas={"journal_mode": "wal", "foreign_keys": 1, "synchronous": 1},
    check_same_thread=False,
    autoconnect=True,
    # WAL allows one writer at a time. Capture tasks publish progress from
    # threads while HTTP requests write too, so a bare connection would raise
    # "database is locked" instead of waiting its turn.
    timeout=settings.database_timeout,
)


@contextmanager
def session():
    """Yield a connection bound to the calling thread.

    Peewee models resolve their database at call time, so callers only need a
    live connection rather than a session object. The context manager mirrors
    the previous session ergonomics and keeps connection cleanup explicit.
    """
    if database.is_closed():
        database.connect()
        opened = True
    else:
        opened = False
    try:
        yield database
    finally:
        if opened and not database.is_closed():
            database.close()


def db():
    """FastAPI dependency that keeps one connection open per request."""
    with session() as connection:
        yield connection
