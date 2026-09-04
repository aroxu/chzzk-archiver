"""Guard the list endpoints against N+1 query regressions."""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.db import database
from app.main import app
from app.models import Broadcast, Channel, Entitlement, Recording, Subscription

ROWS = 25


@contextmanager
def counted_queries():
    """Count SQL statements issued while the block runs."""
    counter = {"n": 0}
    original = database.execute_sql

    def counting(sql, params=None, **kwargs):
        counter["n"] += 1
        return original(sql, params, **kwargs)

    database.execute_sql = counting
    try:
        yield counter
    finally:
        database.execute_sql = original


def _seed(user_id: int) -> None:
    for i in range(ROWS):
        channel = Channel.create(chzzk_id=f"perf-{i}", name=f"채널 이름 {i}", image_url="https://img/x.jpg")
        broadcast = Broadcast.create(channel=channel.id, broadcast_id=f"perf-b-{i}", title=f"영상 {i}")
        recording = Recording.create(broadcast=broadcast.id, state="completed", size=100)
        Entitlement.create(user=user_id, recording=recording.id)
        Subscription.create(user=user_id, channel=channel.id)


@pytest.mark.parametrize("path", ["/api/recordings", "/api/subscriptions"])
def test_list_endpoints_do_not_scale_queries_with_rows(path):
    """Serialising N rows must not cost N extra lookups."""
    with TestClient(app) as client:
        user = client.post("/api/auth/setup", json={"username": "perf", "password": "secret1"}).json()
        _seed(user["id"])

        client.get(path)
        with counted_queries() as counter:
            response = client.get(path)

        assert response.status_code == 200
        assert len(response.json()) == ROWS
        # One query for the authenticated user, one for the joined list.
        assert counter["n"] <= 4, f"{path} issued {counter['n']} queries for {ROWS} rows"
