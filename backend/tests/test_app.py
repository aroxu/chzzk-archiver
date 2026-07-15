import os
import asyncio
from pathlib import Path
import pytest

os.environ["ARCHIVER_DATABASE_URL"] = "sqlite:///./data/test.db"
os.environ["ARCHIVER_SECRET_KEY"] = "test-secret"

from fastapi.testclient import TestClient
import app.main as main_module
from app.main import Base, Broadcast, Channel, Entitlement, Recording, SessionLocal, Subscription, User, _playback_from_json, _progressive_from_mpd, engine, app, parse_content_url


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def mock_channel_profiles(monkeypatch):
    async def fake_profile(channel_id, _client=None):
        return {"name": f"streamer-{channel_id[:4]}", "image": f"https://img.example/{channel_id}.jpg"}

    monkeypatch.setattr(main_module, "fetch_channel_profile", fake_profile)


def test_setup_subscribe_and_isolation():
    with TestClient(app) as admin:
        assert admin.post("/api/auth/setup", json={"username":"admin","password":"very-secret"}).status_code == 200
        invite = admin.post("/api/admin/invites").json()["token"]
        cid = "a" * 32
        assert admin.post("/api/subscriptions", json={"channel":cid}).status_code == 200
        with TestClient(app) as user:
            assert user.post("/api/auth/register",json={"username":"viewer","password":"very-secret","invite":invite}).status_code == 200
            assert user.get("/api/subscriptions").json() == []
            assert user.post("/api/subscriptions",json={"channel":cid}).status_code == 200
            assert len(user.get("/api/subscriptions").json()) == 1
        assert len(admin.get("/api/subscriptions").json()) == 1


def test_invite_is_single_use():
    with TestClient(app) as client:
        client.post("/api/auth/setup",json={"username":"admin","password":"secret"})
        token=client.post("/api/admin/invites").json()["token"]
    with TestClient(app) as user:
        assert user.post("/api/auth/register",json={"username":"one","password":"secret","invite":token}).status_code==200
    with TestClient(app) as other:
        assert other.post("/api/auth/register",json={"username":"two","password":"secret","invite":token}).status_code==400


def test_manual_url_types():
    assert parse_content_url("https://chzzk.naver.com/live/" + "a" * 32)[0] == "live"
    assert parse_content_url("https://chzzk.naver.com/video/123456")[0] == "vod"
    assert parse_content_url("https://chzzk.naver.com/clips/ABCDEF")[0] == "clip"


def test_channel_profile_parser_uses_canonical_metadata(monkeypatch):
    import httpx

    channel_id = "32dbf442f78a051a1ef4a5ac0bcf9773"
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"code": 200, "content": {
        "channelId": channel_id,
        "channelName": "example streamer",
        "channelImageUrl": "https://img.example/profile.jpg",
    }}))

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await main_module.fetch_channel_profile(channel_id, client)

    monkeypatch.undo()
    assert asyncio.run(run()) == {"name": "example streamer", "image": "https://img.example/profile.jpg"}


def test_rejects_non_chzzk_manual_url():
    from fastapi import HTTPException
    try:
        parse_content_url("https://example.com/video/123")
        assert False
    except HTTPException as exc:
        assert exc.status_code == 422


def test_extracts_hls_from_rewind_playback_json():
    playback = '{"media":[{"protocol":"DASH","path":"https://example/dash"},{"protocol":"HLS","path":"https://example/master.m3u8"}]}'
    assert _playback_from_json(playback) == "https://example/master.m3u8"


def test_selects_highest_progressive_mp4_up_to_1080p():
    mpd = b'''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet>
      <Representation mimeType="video/mp4" codecs="avc1,mp4a" height="720"><BaseURL>720.mp4</BaseURL></Representation>
      <Representation mimeType="video/mp4" codecs="avc1,mp4a" height="1080"><BaseURL>1080.mp4</BaseURL></Representation>
      <Representation mimeType="video/mp4" codecs="avc1,mp4a" height="2160"><BaseURL>2160.mp4</BaseURL></Representation>
    </AdaptationSet></Period></MPD>'''
    assert _progressive_from_mpd(mpd, "https://cdn.example/path/manifest.mpd") == "https://cdn.example/path/1080.mp4"


def test_authenticated_range_streaming(tmp_path):
    media = tmp_path / "sample.mp4"
    media.write_bytes(bytes(range(100)))
    with TestClient(app) as client:
        created = client.post("/api/auth/setup", json={"username":"admin","password":"secret"}).json()
        with SessionLocal() as session:
            channel = Channel(chzzk_id="b" * 32, name="테스트 채널")
            session.add(channel); session.flush()
            broadcast = Broadcast(channel_id=channel.id, broadcast_id="live:1", source_type="live", source_url="https://chzzk.naver.com/live/" + "b" * 32, title="테스트 영상")
            session.add(broadcast); session.flush()
            recording = Recording(broadcast_id=broadcast.id, state="completed", path=str(media), size=100)
            session.add(recording); session.flush()
            session.add(Entitlement(user_id=created["id"], recording_id=recording.id)); session.commit()
            recording_id = recording.id
        response = client.get(f"/api/media/{recording_id}", headers={"Range":"bytes=10-19"})
        assert response.status_code == 206
        assert response.content == bytes(range(10, 20))
        assert response.headers["content-range"] == "bytes 10-19/100"


def test_user_can_cancel_owned_queued_download():
    with TestClient(app) as client:
        created = client.post("/api/auth/setup", json={"username":"admin","password":"secret"}).json()
        with SessionLocal() as session:
            channel = Channel(chzzk_id="cancel-sample", name="취소 테스트")
            session.add(channel); session.flush()
            broadcast = Broadcast(channel_id=channel.id, broadcast_id="vod:cancel", source_type="vod", source_url="https://chzzk.naver.com/video/cancel", title="취소 영상")
            session.add(broadcast); session.flush()
            recording = Recording(broadcast_id=broadcast.id, state="queued")
            session.add(recording); session.flush()
            session.add(Entitlement(user_id=created["id"], recording_id=recording.id)); session.commit()
            recording_id = recording.id
        assert client.post(f"/api/recordings/{recording_id}/cancel").status_code == 204
        with SessionLocal() as session:
            assert session.get(Recording, recording_id).state == "canceled"


def test_live_probe_uses_open_status_and_stable_id():
    import httpx

    async def run(payload):
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        async with httpx.AsyncClient(transport=transport) as client:
            return await main_module.fetch_live("channel-id", client)

    assert asyncio.run(run({"code": 200, "content": None})) is None
    assert asyncio.run(run({"code": 200, "content": {"status": "CLOSE"}})) is None
    live = asyncio.run(run({"code": 200, "content": {
        "status": "OPEN", "liveId": 1234, "liveTitle": "test live", "openDate": "2026-07-13 12:00:00",
        "liveImageUrl": "https://img/{type}.jpg", "channel": {"channelId": "channel-id", "channelName": "streamer"},
    }}))
    assert live["id"] == "1234"
    assert live["thumbnail"] == "https://img/1080.jpg"


def test_monitor_deduplicates_shared_channel_recording(monkeypatch):
    with SessionLocal() as session:
        first = User(username="first", password="x")
        second = User(username="second", password="x")
        channel = Channel(chzzk_id="shared-channel", name="shared")
        session.add_all([first, second, channel]); session.flush()
        session.add_all([
            Subscription(user_id=first.id, channel_id=channel.id, active=True, auto_record=True),
            Subscription(user_id=second.id, channel_id=channel.id, active=True, auto_record=True),
        ])
        session.commit()

    async def fake_fetch_live(_chzzk_id, _client):
        return {"id": "live-77", "title": "shared live", "author": "shared", "channel_image": None, "category": None, "thumbnail": None}

    async def fake_recording(_recording_id):
        return None

    monkeypatch.setattr(main_module, "fetch_live", fake_fetch_live)
    monkeypatch.setattr(main_module, "run_recording", fake_recording)
    first_started = asyncio.run(main_module.monitor_live_channels_once())
    second_started = asyncio.run(main_module.monitor_live_channels_once())

    assert len(first_started) == 1
    assert second_started == []
    with SessionLocal() as session:
        recording = session.get(Recording, first_started[0])
        recording.state = "failed"
        recording.error = "temporary streamlink failure"
        recording.finished_at = main_module.datetime.now(main_module.UTC)
        session.commit()
    retried = asyncio.run(main_module.monitor_live_channels_once())
    assert retried == first_started
    with SessionLocal() as session:
        assert session.scalar(main_module.select(main_module.func.count(Broadcast.id))) == 1
        assert session.scalar(main_module.select(main_module.func.count(Recording.id))) == 1
        assert session.scalar(main_module.select(main_module.func.count(Entitlement.id))) == 2
        assert session.scalar(main_module.select(Channel.last_live)) is True
        assert session.get(Recording, first_started[0]).state == "queued"


def test_live_recording_runs_streamlink_and_remuxes(monkeypatch, tmp_path):
    calls = []
    captured_transport = []
    existing_partial = tmp_path / "existing-live.ts"
    existing_partial.write_bytes(b"existing-")

    class FakeProcess:
        def __init__(self, args, stdout=None):
            self.args = args
            self.stdout = stdout
            self.returncode = 0

        async def communicate(self):
            if self.args[0] == "streamlink":
                self.stdout.write(b"transport-stream")
                self.stdout.flush()
            elif self.args[0] == "ffmpeg":
                captured_transport.append(Path(self.args[self.args.index("-i") + 1]).read_bytes())
                Path(self.args[-1]).write_bytes(b"remuxed-mp4")
            return b"", b""

    async def fake_subprocess(*args, **kwargs):
        calls.append(args)
        return FakeProcess(args, kwargs.get("stdout"))

    monkeypatch.setattr(main_module.settings, "recordings_dir", tmp_path)
    monkeypatch.setattr(main_module.asyncio, "create_subprocess_exec", fake_subprocess)
    with SessionLocal() as session:
        user = User(username="recorder", password="x")
        channel = Channel(chzzk_id="live-channel-id", name="live channel")
        session.add_all([user, channel]); session.flush()
        broadcast = Broadcast(
            channel_id=channel.id,
            broadcast_id="live-123",
            source_type="live",
            source_url="https://chzzk.naver.com/live/live-channel-id",
            title="live title",
        )
        session.add(broadcast); session.flush()
        recording = Recording(broadcast_id=broadcast.id, state="queued", path=str(existing_partial), size=existing_partial.stat().st_size)
        session.add(recording); session.flush()
        session.add(Entitlement(user_id=user.id, recording_id=recording.id))
        session.commit()
        recording_id = recording.id

    asyncio.run(main_module.run_recording(recording_id))

    assert calls[0][0] == "streamlink"
    assert "--stdout" in calls[0]
    assert calls[0][-2:] == ("https://chzzk.naver.com/live/live-channel-id", "1080p60,1080p,best")
    assert calls[1][0] == "ffmpeg"
    assert captured_transport == [b"existing-transport-stream"]
    with SessionLocal() as session:
        recording = session.get(Recording, recording_id)
        assert recording.state == "completed"
        assert recording.started_at is not None
        assert Path(recording.path).read_bytes() == b"remuxed-mp4"
        assert recording.size == len(b"remuxed-mp4")
