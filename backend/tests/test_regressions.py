"""Regressions found by auditing the modular/Peewee refactor."""

import asyncio
import itertools
from pathlib import Path

from fastapi.testclient import TestClient

from app import lifecycle
from app.main import app
from app.models import ApiToken, Broadcast, Channel, Recording, User
from app.security import digest
from app.services import recorder
from app.services.downloads import cancel_requested, update_progress
from tests.test_recordings import _fixture_recording


_counter = itertools.count()


def _recording(state: str = "recording") -> Recording:
    n = next(_counter)
    channel = Channel.create(chzzk_id=f"race-channel-{n}", name="race")
    broadcast = Broadcast.create(channel=channel.id, broadcast_id=f"race-{n}")
    return Recording.create(broadcast=broadcast.id, state=state)


def test_channel_profile_backfill_is_persisted_and_virtual_channels_are_skipped(monkeypatch):
    actual = Channel.create(chzzk_id="a" * 32, name="채널 unknown")
    virtual = Channel.create(chzzk_id="vod:14697712", name="VOD 작성자")
    calls = []

    async def profile(channel_id, _client=None):
        calls.append(channel_id)
        return {"name": "실제 채널", "image": None}

    monkeypatch.setattr(lifecycle.chzzk, "fetch_channel_profile", profile)

    asyncio.run(lifecycle.backfill_channel_profiles())
    asyncio.run(lifecycle.backfill_channel_profiles())

    assert calls == [actual.chzzk_id]
    actual = Channel.get_by_id(actual.id)
    virtual = Channel.get_by_id(virtual.id)
    assert actual.name == "실제 채널"
    assert actual.image_url is None
    assert actual.profile_backfilled is True
    assert virtual.profile_backfilled is True


def test_progress_tick_cannot_resurrect_a_cancelled_recording():
    """A tick that read the row before the cancel must not undo it."""
    rec = _recording()
    stale_state = Recording.get_by_id(rec.id).state
    assert stale_state == "recording"

    Recording.update(state="canceled").where(Recording.id == rec.id).execute()
    update_progress(rec.id, 5000, 10000, 100)

    assert Recording.get_by_id(rec.id).state == "canceled"
    assert cancel_requested(rec.id) is True


def test_completion_does_not_overwrite_a_cancel(monkeypatch, tmp_path):
    """Cancelling during thumbnail generation must still end up cancelled."""
    monkeypatch.setattr(recorder.settings, "recordings_dir", tmp_path)
    monkeypatch.setattr(
        recorder.chzzk,
        "resolve_direct",
        lambda _url, _cookies: {"protocol": "progressive", "playback_url": "https://cdn/x.mp4", "total_size": 5},
    )

    channel = Channel.create(chzzk_id="vod:late-cancel", name="늦은 취소")
    broadcast = Broadcast.create(
        channel=channel.id, broadcast_id="vod:late", source_type="vod", source_url="https://chzzk.naver.com/video/9"
    )
    rec = Recording.create(broadcast=broadcast.id, state="queued")

    async def fake_download(_url, destination, *_args):
        destination.write_bytes(b"media")

    def fake_package(_source, destination):
        (destination / "master.m3u8").write_text("#EXTM3U\n")
        return destination / "master.m3u8"

    monkeypatch.setattr(recorder, "_download_progressive_source", fake_download)
    monkeypatch.setattr(recorder, "package_media_as_hls", fake_package)

    def cancel_during_thumbnail(_path):
        Recording.update(state="canceled").where(Recording.id == rec.id).execute()

    monkeypatch.setattr(recorder, "generate_thumbnail", cancel_during_thumbnail)

    asyncio.run(recorder.run_recording(rec.id))

    assert Recording.get_by_id(rec.id).state == "canceled"


def test_pairing_code_cannot_authenticate_as_api_token():
    """Pairing codes are exchange material, not bearer credentials."""
    with TestClient(app) as client:
        client.post("/api/auth/setup", json={"username": "admin", "password": "secret"})
        code = client.post("/api/me/pair").json()["code"]
        assert ApiToken.get(ApiToken.token_hash == digest(code)).kind == "pairing"

    with TestClient(app) as anon:
        assert anon.get("/api/me", headers={"Authorization": f"Bearer {code}"}).status_code == 401


def test_range_requests_handle_suffix_and_invalid_values(tmp_path):
    media = tmp_path / "ranged.mp4"
    media.write_bytes(bytes(range(100)))
    with TestClient(app) as client:
        created = client.post("/api/auth/setup", json={"username": "admin", "password": "secret"}).json()
        rid = _fixture_recording(media, user_id=created["id"]).id

        suffix = client.get(f"/api/media/{rid}", headers={"Range": "bytes=-10"})
        assert suffix.status_code == 206
        assert suffix.content == bytes(range(90, 100))
        assert suffix.headers["content-range"] == "bytes 90-99/100"

        assert client.get(f"/api/media/{rid}", headers={"Range": "bytes=0-"}).status_code == 206

        for bad in ("bytes=abc-", "bytes=", "bytes=99999-", "bytes=5-2"):
            response = client.get(f"/api/media/{rid}", headers={"Range": bad})
            assert response.status_code == 416, bad
            assert response.headers["content-range"] == "bytes */100"


def test_duplicate_username_is_a_conflict_not_a_crash():
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "admin", "password": "secret"})
        invite = admin.post("/api/admin/invites").json()["token"]
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/register", json={"username": "admin", "password": "secret", "invite": invite}
        )
        assert response.status_code == 409


def test_queued_recordings_are_rescheduled_after_restart():
    """Nothing survives the process, so a queued row needs a worker again."""
    queued = _recording(state="queued")
    running = _recording(state="recording")
    done = _recording(state="completed")

    resumed = lifecycle.requeue_interrupted()

    assert set(resumed) == {queued.id, running.id}
    assert Recording.get_by_id(done.id).state == "completed"


def test_error_messages_hide_cookies_and_signing_keys():
    leaky = "failed https://cdn/x.mp4?key=abc123 cookie NID_AUT=secretvalue; NID_SES=other"
    cleaned = recorder.redact(leaky)
    assert "secretvalue" not in cleaned
    assert "other" not in cleaned
    assert "abc123" not in cleaned
    assert "cdn/x.mp4" not in cleaned


def test_failed_request_does_not_leave_partial_rows():
    """A handler that raises midway must not commit half its work."""
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "admin", "password": "secret"})
        invite = admin.post("/api/admin/invites").json()["token"]
    with TestClient(app) as client:
        client.post("/api/auth/register", json={"username": "admin", "password": "secret", "invite": invite})
    from app.models import Invite

    assert Invite.get(Invite.token_hash == digest(invite)).used_at is None
    assert User.select().count() == 1


def test_writes_from_worker_threads_do_not_deadlock():
    """Progress ticks run in threads while requests write; both must survive."""
    import threading

    rec = _recording()
    errors: list[Exception] = []

    def hammer():
        try:
            for i in range(40):
                update_progress(rec.id, i * 10, 400, 50)
        except Exception as exc:  # pragma: no cover - only on regression
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert Recording.get_by_id(rec.id).state == "recording"
