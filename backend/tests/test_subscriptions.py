"""Subscribing to a channel that is already live."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Channel, Recording, Subscription
from app.routers import subscriptions
from app.services import chzzk

LIVE = {
    "id": "live-42",
    "title": "지금 방송 중",
    "author": "라이브 채널",
    "channel_image": None,
    "category": None,
    "thumbnail": None,
}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        test_client.post("/api/auth/setup", json={"username": "admin", "password": "secret"})
        yield test_client


def _offline(monkeypatch):
    async def probe(_chzzk_id):
        return None

    monkeypatch.setattr(subscriptions, "_probe_live", probe)


def _online(monkeypatch):
    async def probe(_chzzk_id):
        return dict(LIVE)

    monkeypatch.setattr(subscriptions, "_probe_live", probe)


def test_subscribe_reports_live_so_the_ui_can_offer_recording(client, monkeypatch):
    _online(monkeypatch)
    body = client.post("/api/subscriptions", json={"channel": "a" * 32}).json()
    assert body["live"] is True
    assert body["live_title"] == "지금 방송 중"
    # Offering is not recording: nothing starts until the user opts in.
    assert Recording.select().count() == 0
    assert Channel.get(Channel.chzzk_id == "a" * 32).last_live is True


def test_subscribe_to_offline_channel_does_not_prompt(client, monkeypatch):
    _offline(monkeypatch)
    body = client.post("/api/subscriptions", json={"channel": "b" * 32}).json()
    assert body["live"] is False
    assert body["live_title"] is None


def test_start_live_captures_the_running_broadcast(client, monkeypatch):
    _online(monkeypatch)
    started: list[int] = []

    async def fake_run(recording_id):
        started.append(recording_id)

    monkeypatch.setattr(subscriptions, "run_recording", fake_run)
    client.post("/api/subscriptions", json={"channel": "c" * 32})
    body = client.post("/api/subscriptions/start-live", json={"channel": "c" * 32}).json()

    assert body["started"] is True
    assert body["title"] == "지금 방송 중"
    assert started == [body["id"]]


def test_start_live_is_idempotent_for_the_same_broadcast(client, monkeypatch):
    _online(monkeypatch)
    started: list[int] = []

    async def fake_run(recording_id):
        started.append(recording_id)

    monkeypatch.setattr(subscriptions, "run_recording", fake_run)
    client.post("/api/subscriptions", json={"channel": "d" * 32})
    first = client.post("/api/subscriptions/start-live", json={"channel": "d" * 32}).json()
    second = client.post("/api/subscriptions/start-live", json={"channel": "d" * 32}).json()

    assert first["id"] == second["id"]
    assert second["started"] is False
    assert started == [first["id"]]
    assert Recording.select().count() == 1


def test_start_live_rejects_an_offline_channel(client, monkeypatch):
    _online(monkeypatch)
    client.post("/api/subscriptions", json={"channel": "e" * 32})
    _offline(monkeypatch)
    response = client.post("/api/subscriptions/start-live", json={"channel": "e" * 32})

    assert response.status_code == 409
    assert Recording.select().count() == 0
    assert Channel.get(Channel.chzzk_id == "e" * 32).last_live is False


def test_start_live_requires_an_active_subscription(client, monkeypatch):
    _online(monkeypatch)
    assert client.post("/api/subscriptions/start-live", json={"channel": "f" * 32}).status_code == 404


def test_subscribe_survives_a_failing_live_probe(client, monkeypatch):
    """A CHZZK outage must not roll back the subscription itself."""

    async def failing_probe(_chzzk_id, _client):
        return chzzk.LIVE_PROBE_FAILED

    monkeypatch.setattr(chzzk, "fetch_live", failing_probe)
    # Channel ids are hexadecimal, so fixtures must stay within [0-9a-f].
    response = client.post("/api/subscriptions", json={"channel": "0" * 32})
    body = response.json()
    assert response.status_code == 200, body

    assert body["live"] is False
    assert Subscription.select().count() == 1
