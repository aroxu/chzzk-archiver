import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import lifecycle
from app.main import app
from app.models import AuditLog, Broadcast, Channel, EncodingJob, Entitlement, Recording, Subscription, User
from app.services import recorder
from app.services.media import audio_asset_path, hls_directory, recording_json, thumbnail_path


def _fixture_recording(media: Path | None = None, state: str = "completed", user_id: int | None = None):
    channel = Channel.create(chzzk_id="b" * 32, name="테스트 채널")
    broadcast = Broadcast.create(
        channel=channel.id,
        broadcast_id="live:1",
        source_type="live",
        source_url="https://chzzk.naver.com/live/" + "b" * 32,
        title="테스트 영상",
    )
    recording = Recording.create(
        broadcast=broadcast.id,
        state=state,
        path=str(media) if media else None,
        size=media.stat().st_size if media else 0,
    )
    if user_id:
        Entitlement.create(user=user_id, recording=recording.id)
    return recording


def test_authenticated_range_streaming(tmp_path):
    media = tmp_path / "sample.mp4"
    media.write_bytes(bytes(range(100)))
    thumbnail_path(media).write_bytes(b"jpeg")
    with TestClient(app) as client:
        created = client.post("/api/auth/setup", json={"username": "admin", "password": "secret"}).json()
        recording_id = _fixture_recording(media, user_id=created["id"]).id
        response = client.get(f"/api/media/{recording_id}", headers={"Range": "bytes=10-19"})
        assert response.status_code == 206
        assert response.content == bytes(range(10, 20))
        assert response.headers["content-range"] == "bytes 10-19/100"
        thumbnail = client.get(f"/api/thumbnails/{recording_id}")
        assert thumbnail.status_code == 200
        assert thumbnail.content == b"jpeg"


def test_radio_stream_uses_the_users_selected_audio_asset(monkeypatch, tmp_path):
    from app.routers import media as media_router

    media = tmp_path / "sample.mp4"
    media.write_bytes(b"video")
    aac = audio_asset_path(media, "aac")
    flac = audio_asset_path(media, "flac")
    aac.write_bytes(b"aac-only")
    flac.write_bytes(b"flac-only")
    monkeypatch.setattr(media_router, "generate_audio_assets", lambda _path: {"aac": aac, "flac": flac})
    with TestClient(app) as client:
        created = client.post("/api/auth/setup", json={"username": "admin", "password": "secret"}).json()
        recording_id = _fixture_recording(media, user_id=created["id"]).id
        default_audio = client.get(f"/api/media/{recording_id}/audio")
        assert default_audio.content == b"aac-only"
        assert default_audio.headers["content-type"].startswith("audio/mp4")
        client.patch("/api/me/preferences", json={"audio_format": "flac"})
        lossless_audio = client.get(f"/api/media/{recording_id}/audio?format=flac")
        assert lossless_audio.content == b"flac-only"
        assert lossless_audio.headers["content-type"].startswith("audio/flac")
        assert client.get(f"/api/media/{recording_id}/audio?format=mp3").status_code == 422


def test_hls_assets_are_entitled_and_path_safe(monkeypatch, tmp_path):
    from app.routers import media as media_router

    media = tmp_path / "sample.mp4"
    media.write_bytes(b"video")
    aac = audio_asset_path(media, "aac")
    aac.write_bytes(b"aac")
    root = hls_directory(media)
    root.mkdir()
    master = root / "master.m3u8"
    master.write_text("#EXTM3U\n", encoding="utf-8")
    monkeypatch.setattr(media_router, "generate_audio_assets", lambda _path: {"aac": aac})
    monkeypatch.setattr(media_router, "generate_hls_bundle", lambda _video, _aac: master)
    with TestClient(app) as client:
        created = client.post("/api/auth/setup", json={"username": "admin", "password": "secret"}).json()
        recording_id = _fixture_recording(media, user_id=created["id"]).id
        response = client.get(f"/api/hls/{recording_id}/master.m3u8")
        assert response.status_code == 200
        assert response.text.splitlines() == ["#EXTM3U"]
        assert response.headers["content-type"].startswith("application/vnd.apple.mpegurl")
        assert client.get(f"/api/hls/{recording_id}/%2e%2e%2fsample.mp4").status_code == 404


def test_hls_bundle_uses_portable_flat_output_paths(monkeypatch, tmp_path):
    from app.services import media as media_service

    video = tmp_path / "채널" / "sample.mp4"
    video.parent.mkdir()
    video.write_bytes(b"video")
    audio = audio_asset_path(video, "aac")
    audio.write_bytes(b"audio")
    observed = {}

    def fake_run(command, **kwargs):
        workdir = Path(kwargs["cwd"])
        observed["command"] = command
        observed["cwd"] = workdir
        (workdir / "master.m3u8").write_text("#EXTM3U\nvideo.m3u8\n", encoding="utf-8")
        (workdir / "video.m3u8").write_text(
            '#EXTM3U\n#EXT-X-MAP:URI="video-init.mp4"\nvideo-segment_00000.m4s\n',
            encoding="utf-8",
        )
        (workdir / "audio.m3u8").write_text(
            '#EXTM3U\n#EXT-X-MAP:URI="audio-init.mp4"\naudio-segment_00000.m4s\n',
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(media_service.subprocess, "run", fake_run)
    master = media_service.generate_hls_bundle(video, audio)

    assert master == hls_directory(video) / "master.m3u8"
    assert master.exists()
    assert observed["cwd"].name.endswith(".part")
    command = observed["command"]
    assert command[command.index("-hls_fmp4_init_filename") + 1] == "%v-init.mp4"
    assert command[command.index("-hls_segment_filename") + 1] == "%v-segment_%05d.m4s"
    assert command[-1] == "%v.m3u8"


def test_legacy_combined_archive_is_migrated_to_split_v2_storage(monkeypatch, tmp_path):
    from app.services import media as media_service

    source = tmp_path / "legacy.ts"
    source.write_bytes(b"combined-transport")
    recording = _fixture_recording(source)
    monkeypatch.setattr(media_service.settings, "recordings_dir", tmp_path)
    monkeypatch.setattr(media_service, "_stream_codecs", lambda _path: ("hevc", True))
    monkeypatch.setattr(media_service, "probe_duration", lambda _path: 123.5)
    monkeypatch.setattr(media_service, "generate_thumbnail", lambda _path: None)

    class Result:
        returncode = 0
        stderr = b""

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"video-only")
        return Result()

    def fake_audio(video_path, source_path=None):
        assert source_path == source
        assets = {
            audio_format: audio_asset_path(video_path, audio_format)
            for audio_format in ("aac", "flac")
        }
        assets["aac"].write_bytes(b"aac")
        assets["flac"].write_bytes(b"flac")
        return assets

    def fake_hls(video_path, _aac_path):
        root = hls_directory(video_path)
        root.mkdir()
        master = root / "master.m3u8"
        master.write_text("#EXTM3U\n")
        return master

    monkeypatch.setattr(media_service.subprocess, "run", fake_run)
    monkeypatch.setattr(media_service, "generate_audio_assets", fake_audio)
    monkeypatch.setattr(media_service, "generate_hls_bundle", fake_hls)

    final = media_service.migrate_legacy_recording(recording.id)
    stored = Recording.get_by_id(recording.id)
    assert final == source.with_suffix(".mp4")
    assert final.read_bytes() == b"video-only"
    assert not source.exists()
    assert audio_asset_path(final, "aac").read_bytes() == b"aac"
    assert audio_asset_path(final, "flac").read_bytes() == b"flac"
    assert (hls_directory(final) / "master.m3u8").exists()
    assert stored.path == str(final)
    assert stored.duration_seconds == 123.5
    assert stored.storage_version == 2


def test_media_is_hidden_from_users_without_entitlement(tmp_path):
    media = tmp_path / "private.mp4"
    media.write_bytes(b"secret-bytes")
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "admin", "password": "secret"})
        invite = admin.post("/api/admin/invites").json()["token"]
        recording_id = _fixture_recording(media).id
    with TestClient(app) as viewer:
        viewer.post("/api/auth/register", json={"username": "viewer", "password": "secret", "invite": invite})
        assert viewer.get(f"/api/media/{recording_id}").status_code == 404
        assert viewer.get("/api/recordings").json() == []


def test_recording_uses_local_thumbnail_only(tmp_path):
    media = tmp_path / "sample.mp4"
    media.write_bytes(b"video")
    channel = Channel.create(chzzk_id="thumbnail-channel", name="thumbnail channel")
    broadcast = Broadcast.create(
        channel=channel.id,
        broadcast_id="thumbnail-video",
        thumbnail_url="https://akamai.example/remote.jpg",
    )
    recording = Recording.create(broadcast=broadcast.id, state="completed", path=str(media), size=5)
    assert recording_json(recording)["thumbnail"] is None
    thumbnail_path(media).write_bytes(b"jpeg")
    assert recording_json(recording)["thumbnail"] == f"/api/thumbnails/{recording.id}"


def test_recording_uses_media_duration_instead_of_wall_clock(tmp_path):
    media = tmp_path / "duration.mp4"
    media.write_bytes(b"video")
    recording = _fixture_recording(media)
    Recording.update(duration_seconds=3723.5).where(Recording.id == recording.id).execute()

    payload = recording_json(Recording.get_by_id(recording.id))

    assert payload["duration_seconds"] == 3723.5
    assert payload["recorded_seconds"] == 3723


def test_duration_backfill_reads_completed_media(monkeypatch, tmp_path):
    media = tmp_path / "legacy.mp4"
    media.write_bytes(b"legacy-video")
    recording = _fixture_recording(media)
    monkeypatch.setattr(lifecycle, "probe_duration", lambda _path: 456.75)

    asyncio.run(lifecycle.backfill_durations())

    assert Recording.get_by_id(recording.id).duration_seconds == 456.75


def test_user_can_cancel_owned_queued_download():
    with TestClient(app) as client:
        created = client.post("/api/auth/setup", json={"username": "admin", "password": "secret"}).json()
        recording_id = _fixture_recording(state="queued", user_id=created["id"]).id
        assert client.post(f"/api/recordings/{recording_id}/cancel").status_code == 204
        assert Recording.get_by_id(recording_id).state == "canceled"
        assert client.post(f"/api/recordings/{recording_id}/cancel").status_code == 409


def test_deleting_last_entitlement_removes_file(tmp_path):
    media = tmp_path / "removable.mp4"
    media.write_bytes(b"video")
    thumbnail_path(media).write_bytes(b"jpeg")
    audio_asset_path(media, "aac").write_bytes(b"aac")
    audio_asset_path(media, "flac").write_bytes(b"flac")
    hls_directory(media).mkdir()
    (hls_directory(media) / "master.m3u8").write_text("#EXTM3U\n")
    with TestClient(app) as client:
        created = client.post("/api/auth/setup", json={"username": "admin", "password": "secret"}).json()
        recording_id = _fixture_recording(media, user_id=created["id"]).id
        assert client.delete(f"/api/recordings/{recording_id}").status_code == 204
    assert Recording.get_or_none(Recording.id == recording_id) is None
    assert not media.exists()
    assert not thumbnail_path(media).exists()
    assert not audio_asset_path(media, "aac").exists()
    assert not audio_asset_path(media, "flac").exists()
    assert not hls_directory(media).exists()


def test_shared_recording_survives_one_user_deleting_it(tmp_path):
    media = tmp_path / "shared.mp4"
    media.write_bytes(b"video")
    with TestClient(app) as admin:
        owner = admin.post("/api/auth/setup", json={"username": "admin", "password": "secret"}).json()
        invite = admin.post("/api/admin/invites").json()["token"]
        recording = _fixture_recording(media, user_id=owner["id"])
    with TestClient(app) as viewer:
        second = viewer.post(
            "/api/auth/register", json={"username": "viewer", "password": "secret", "invite": invite}
        ).json()
        Entitlement.create(user=second["id"], recording=recording.id)
        assert viewer.delete(f"/api/recordings/{recording.id}").status_code == 204
    assert Recording.get_or_none(Recording.id == recording.id) is not None
    assert media.exists()


def test_admin_can_permanently_delete_shared_archive(monkeypatch, tmp_path):
    monkeypatch.setattr(recorder.settings, "recordings_dir", tmp_path)
    media = tmp_path / "shared.mp4"
    source = tmp_path / "shared.ts"
    upload = tmp_path / ".shared.encoded.part.mp4"
    for path in (media, source, upload):
        path.write_bytes(b"video")
    thumbnail_path(media).write_bytes(b"jpeg")

    with TestClient(app) as client:
        owner = client.post(
            "/api/auth/setup", json={"username": "admin", "password": "secret"}
        ).json()
        viewer = User.create(username="viewer", password="unused")
        recording = _fixture_recording(media, user_id=owner["id"])
        Entitlement.create(user=viewer.id, recording=recording.id)
        EncodingJob.create(
            recording=recording.id,
            state="failed",
            source_path=str(source),
            upload_path=str(upload),
        )

        assert client.delete(f"/api/admin/recordings/{recording.id}").status_code == 204

    assert Recording.get_or_none(Recording.id == recording.id) is None
    assert not Entitlement.select().where(Entitlement.recording == recording.id).exists()
    assert not EncodingJob.select().where(EncodingJob.recording == recording.id).exists()
    assert all(not path.exists() for path in (media, source, upload, thumbnail_path(media)))
    assert AuditLog.get().action == "recording.purge"


def test_admin_must_stop_active_archive_before_permanent_delete(tmp_path):
    with TestClient(app) as client:
        owner = client.post(
            "/api/auth/setup", json={"username": "admin", "password": "secret"}
        ).json()
        recording = _fixture_recording(state="recording", user_id=owner["id"])
        response = client.delete(f"/api/admin/recordings/{recording.id}")
    assert response.status_code == 409
    assert Recording.get_or_none(Recording.id == recording.id) is not None


def test_monitor_deduplicates_shared_channel_recording(monkeypatch):
    first = User.create(username="first", password="x")
    second = User.create(username="second", password="x")
    channel = Channel.create(chzzk_id="shared-channel", name="shared")
    Subscription.create(user=first.id, channel=channel.id, active=True, auto_record=True)
    Subscription.create(user=second.id, channel=channel.id, active=True, auto_record=True)

    async def fake_fetch_live(_chzzk_id, _client):
        return {
            "id": "live-77",
            "title": "shared live",
            "author": "shared",
            "channel_image": None,
            "category": None,
            "thumbnail": None,
        }

    async def fake_recording(_recording_id):
        return None

    monkeypatch.setattr(recorder.chzzk, "fetch_live", fake_fetch_live)
    monkeypatch.setattr(recorder, "run_recording", fake_recording)
    first_started = asyncio.run(recorder.monitor_live_channels_once())
    second_started = asyncio.run(recorder.monitor_live_channels_once())

    assert len(first_started) == 1
    assert second_started == []

    recording = Recording.get_by_id(first_started[0])
    recording.state = "failed"
    recording.error = "temporary streamlink failure"
    recording.save()
    retried = asyncio.run(recorder.monitor_live_channels_once())
    assert retried == first_started

    assert Broadcast.select().count() == 1
    assert Recording.select().count() == 1
    assert Entitlement.select().count() == 2
    assert Channel.get_by_id(channel.id).last_live is True
    assert Recording.get_by_id(first_started[0]).state == "queued"


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

    monkeypatch.setattr(recorder.settings, "recordings_dir", tmp_path)
    monkeypatch.setattr(recorder.asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(recorder, "generate_thumbnail", lambda _path: None)

    user = User.create(username="recorder", password="x")
    channel = Channel.create(chzzk_id="live-channel-id", name="live channel")
    broadcast = Broadcast.create(
        channel=channel.id,
        broadcast_id="live-123",
        source_type="live",
        source_url="https://chzzk.naver.com/live/live-channel-id",
        title="live title",
    )
    recording = Recording.create(
        broadcast=broadcast.id,
        state="queued",
        path=str(existing_partial),
        size=existing_partial.stat().st_size,
    )
    Entitlement.create(user=user.id, recording=recording.id)

    asyncio.run(recorder.run_recording(recording.id))

    assert calls[0][0] == "streamlink"
    assert "--stdout" in calls[0]
    assert calls[0][-2:] == ("https://chzzk.naver.com/live/live-channel-id", "1080p60,1080p,best")
    assert calls[1][0] == "ffmpeg"
    assert captured_transport == [b"existing-transport-stream"]

    stored = Recording.get_by_id(recording.id)
    assert stored.state == "completed"
    assert stored.started_at is not None
    assert Path(stored.path).read_bytes() == b"remuxed-mp4"
    assert stored.size == len(b"remuxed-mp4")


def test_segmented_capture_reports_estimated_eta(monkeypatch, tmp_path):
    media = tmp_path / "growing.ts"
    media.write_bytes(b"")
    recording = _fixture_recording(media, state="recording")

    class FakeProcess:
        returncode = None

    process = FakeProcess()
    writes = iter((1000, 2000))
    real_sleep = asyncio.sleep

    async def fake_sleep(_seconds):
        await real_sleep(0.01)
        length = next(writes)
        media.write_bytes(b"x" * length)
        if length == 2000:
            process.returncode = 0

    monkeypatch.setattr(recorder.asyncio, "sleep", fake_sleep)

    asyncio.run(recorder.monitor_live_progress(recording.id, media, process, total_size=4000))

    stored = Recording.get_by_id(recording.id)
    assert stored.total_size == 4000
    assert stored.size == 2000
    assert stored.speed_bps > 0
    assert stored.eta_seconds is not None


def test_remote_vod_waits_for_faststart_file_and_allows_m4v_segments(monkeypatch, tmp_path):
    calls = []
    queued_paths = []
    monitored_paths = []

    class FakeProcess:
        returncode = 0

        def __init__(self, args):
            self.args = args

        async def communicate(self):
            Path(self.args[-1]).write_bytes(b"media")
            await asyncio.sleep(0)
            return b"", b""

    async def fake_subprocess(*args, **_kwargs):
        calls.append(args)
        return FakeProcess(args)

    def fake_enqueue(_recording_id, source, **_kwargs):
        assert Path(source).exists()
        queued_paths.append(Path(source))
        return SimpleNamespace(id=1)

    async def fake_monitor(_recording_id, path, _process, total_size=0):
        monitored_paths.append(Path(path))

    monkeypatch.setattr(recorder.settings, "recordings_dir", tmp_path)
    monkeypatch.setattr(recorder.settings, "encoding_mode", "remote")
    monkeypatch.setattr(recorder.chzzk, "resolve_direct", lambda *_args: {
        "protocol": "dash", "playback_url": "https://cdn.example/manifest.mpd", "total_size": 0,
    })
    monkeypatch.setattr(recorder.asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(recorder, "monitor_live_progress", fake_monitor)
    monkeypatch.setattr(recorder, "enqueue_encoding", fake_enqueue)
    monkeypatch.setattr(recorder, "generate_thumbnail", lambda _path: None)

    channel = Channel.create(chzzk_id="vod:remote-channel", name="원격 VOD")
    broadcast = Broadcast.create(
        channel=channel.id,
        broadcast_id="vod:remote-1",
        source_type="vod",
        source_url="https://chzzk.naver.com/video/123",
        title="원격 VOD",
    )
    recording = Recording.create(broadcast=broadcast.id, state="queued")

    asyncio.run(recorder.run_recording(recording.id))

    assert calls[0][calls[0].index("-extension_picky") + 1] == "false"
    assert monitored_paths and monitored_paths[0].suffix == ".ts"
    assert not monitored_paths[0].exists()
    assert queued_paths == [Path(Recording.get_by_id(recording.id).path)]
    assert queued_paths[0].suffix == ".mp4"


def test_hls_vod_uses_parallel_streamlink_segments(monkeypatch, tmp_path):
    calls = []

    class FakeProcess:
        returncode = 0

        def __init__(self, args, stdout):
            self.args = args
            self.stdout = stdout

        async def communicate(self):
            if hasattr(self.stdout, "write"):
                self.stdout.write(b"parallel-hls")
                self.stdout.flush()
            else:
                Path(self.args[-1]).write_bytes(b"remuxed")
            await asyncio.sleep(0)
            return b"", b""

    async def fake_subprocess(*args, **kwargs):
        calls.append(args)
        return FakeProcess(args, kwargs.get("stdout"))

    monkeypatch.setattr(recorder.settings, "recordings_dir", tmp_path)
    monkeypatch.setattr(recorder.settings, "encoding_mode", "disabled")
    monkeypatch.setattr(recorder.settings, "download_segment_threads", 10)
    monkeypatch.setattr(recorder.chzzk, "resolve_direct", lambda *_args: {
        "protocol": "hls",
        "playback_url": "https://cdn.example/master.m3u8",
        "total_size": 10_000,
    })
    monkeypatch.setattr(recorder.asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(recorder, "generate_thumbnail", lambda _path: None)

    channel = Channel.create(chzzk_id="vod:hls-channel", name="HLS VOD")
    broadcast = Broadcast.create(
        channel=channel.id,
        broadcast_id="vod:hls-1",
        source_type="vod",
        source_url="https://chzzk.naver.com/video/2",
        title="HLS VOD",
    )
    recording = Recording.create(broadcast=broadcast.id, state="queued")

    asyncio.run(recorder.run_recording(recording.id))

    assert calls[0][:3] == (recorder.sys.executable, "-m", "streamlink")
    assert calls[0][calls[0].index("--stream-segment-threads") + 1] == "10"
    assert calls[0][-2:] == ("hls://https://cdn.example/master.m3u8", "best")
    assert Recording.get_by_id(recording.id).state == "completed"


def test_cancelling_a_download_is_not_reported_as_failure(monkeypatch, tmp_path):
    """A user cancel must land in `canceled`, never in `failed`."""
    monkeypatch.setattr(recorder.settings, "recordings_dir", tmp_path)
    monkeypatch.setattr(recorder, "generate_thumbnail", lambda _path: None)
    monkeypatch.setattr(recorder.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        recorder.chzzk,
        "resolve_direct",
        lambda _url, _cookies: {"protocol": "progressive", "playback_url": "https://cdn/x.mp4", "total_size": 10},
    )

    def cancelled_download(*_args, **_kwargs):
        raise recorder.DownloadCancelled

    monkeypatch.setattr(recorder, "download_progressive", cancelled_download)

    channel = Channel.create(chzzk_id="vod:cancel-channel", name="취소 채널")
    broadcast = Broadcast.create(
        channel=channel.id,
        broadcast_id="vod:cancel-1",
        source_type="vod",
        source_url="https://chzzk.naver.com/video/1",
        title="취소될 영상",
    )
    recording = Recording.create(broadcast=broadcast.id, state="queued")

    asyncio.run(recorder.run_recording(recording.id))

    stored = Recording.get_by_id(recording.id)
    assert stored.state == "canceled"
    assert stored.error is None
    assert stored.path is None


def test_requeue_interrupted_moves_running_recordings_back():
    channel = Channel.create(chzzk_id="resume-channel", name="resume")
    broadcast = Broadcast.create(channel=channel.id, broadcast_id="resume-1")
    recording = Recording.create(broadcast=broadcast.id, state="recording")
    assert lifecycle.requeue_interrupted() == [recording.id]
    assert Recording.get_by_id(recording.id).state == "queued"
