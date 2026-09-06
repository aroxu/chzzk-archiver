import os
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("ARCHIVER_DATABASE_URL", f"sqlite:///{DATA_DIR / 'test.db'}")
os.environ.setdefault("ARCHIVER_SECRET_KEY", "test-secret")
import pytest

from app.db import database
from app.models import ALL_MODELS
from app.services import chzzk


@pytest.fixture(autouse=True)
def fresh_database():
    """Give every test an empty schema on a shared connection."""
    if database.is_closed():
        database.connect()
    database.drop_tables(ALL_MODELS, safe=True)
    database.create_tables(ALL_MODELS)
    yield database


@pytest.fixture(autouse=True)
def mock_channel_profiles(monkeypatch):
    async def fake_profile(channel_id, _client=None):
        return {"name": f"streamer-{channel_id[:4]}", "image": f"https://img.example/{channel_id}.jpg"}

    monkeypatch.setattr(chzzk, "fetch_channel_profile", fake_profile)
