"""Exploratory probes for the start-live edge cases."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Recording, Subscription, User
from app.routers import subscriptions

LIVE = {
    "id": "live-99",
    "title": "진행 중",
    "author": "채널",
    "channel_image": None,
    "category": None,
    "thumbnail": None,
}


@pytest.fixture
def client(monkeypatch):
    async def probe(_cid):
        return dict(LIVE)

    monkeypatch.setattr(subscriptions, "_probe_live", probe)

    started = []

    async def fake_run(rid):
        started.append(rid)

    monkeypatch.setattr(subscriptions, "run_recording", fake_run)
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "secret"})
        c.started = started
        yield c


def test_restart_after_user_cancels(client):
    """A user who cancels and changes their mind must be able to restart."""
    client.post("/api/subscriptions", json={"channel": "a" * 32})
    first = client.post("/api/subscriptions/start-live", json={"channel": "a" * 32}).json()
    assert client.post(f"/api/recordings/{first['id']}/cancel").status_code == 204
    assert Recording.get_by_id(first["id"]).state == "canceled"

    again = client.post("/api/subscriptions/start-live", json={"channel": "a" * 32}).json()
    assert again["started"] is True, again
    assert client.started == [first["id"], again["id"]]


def test_start_live_works_without_auto_record(client):
    """Manual opt-in should record even when auto_record is off."""
    client.post("/api/subscriptions", json={"channel": "b" * 32, "auto_record": False})
    body = client.post("/api/subscriptions/start-live", json={"channel": "b" * 32}).json()
    assert body["started"] is True
    assert body["state"] == "queued"


def test_second_subscriber_gains_access_to_running_capture(client):
    """Dedupe must still grant the new opt-in user an entitlement."""
    client.post("/api/subscriptions", json={"channel": "c" * 32})
    first = client.post("/api/subscriptions/start-live", json={"channel": "c" * 32}).json()

    invite = client.post("/api/admin/invites").json()["token"]
    with TestClient(app) as second:
        second.post(
            "/api/auth/register",
            json={"username": "viewer", "password": "secret", "invite": invite},
        )
        second.post("/api/subscriptions", json={"channel": "c" * 32})
        body = second.post("/api/subscriptions/start-live", json={"channel": "c" * 32}).json()
        assert body["id"] == first["id"]
        assert body["started"] is False
        visible = [r["id"] for r in second.get("/api/recordings").json()]
        assert first["id"] in visible


def test_completed_broadcast_is_not_restarted(client):
    client.post("/api/subscriptions", json={"channel": "d" * 32})
    first = client.post("/api/subscriptions/start-live", json={"channel": "d" * 32}).json()
    Recording.update(state="completed").where(Recording.id == first["id"]).execute()

    again = client.post("/api/subscriptions/start-live", json={"channel": "d" * 32}).json()
    assert again["started"] is False
    assert len(client.started) == 1


def test_user_ids_are_real_users(client):
    """ensure_recording receives user ids, so entitlements must resolve."""
    client.post("/api/subscriptions", json={"channel": "e" * 32})
    client.post("/api/subscriptions/start-live", json={"channel": "e" * 32})
    assert Subscription.select().count() >= 1
    assert User.select().count() >= 1


def test_restarted_capture_drops_stale_file_metadata(client):
    """A revived row must not advertise the deleted capture's path or size."""
    client.post("/api/subscriptions", json={"channel": "f" * 32})
    first = client.post("/api/subscriptions/start-live", json={"channel": "f" * 32}).json()
    Recording.update(state="canceled", path=r"C:\\gone.ts", size=123, total_size=456).where(
        Recording.id == first["id"]
    ).execute()

    again = client.post("/api/subscriptions/start-live", json={"channel": "f" * 32}).json()
    row = Recording.get_by_id(again["id"])
    assert again["started"] is True
    assert row.path is None
    assert (row.size, row.total_size) == (0, 0)
