import asyncio
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.models import AuditLog, Broadcast, Channel, Entitlement, Recording, User
from app.services import media as media_service
from app.services import recorder
from app.services import hls_mirror
from app.services.hls_mirror import _publish_progress
from app.services.media import STORAGE_VERSION, thumbnail_path


def _fixture_recording(media: Path | None = None, state: str = "completed", user_id: int | None = None):
    channel = Channel.create(chzzk_id="b" * 32, name="테스트 채널")
    broadcast = Broadcast.create(
        channel=channel.id, broadcast_id="live:1", source_type="live",
        source_url="https://chzzk.naver.com/live/" + "b" * 32, title="테스트 영상",
    )
    recording = Recording.create(
        broadcast=broadcast.id, state=state, path=str(media) if media else None,
        size=media.stat().st_size if media and media.is_file() else 0,
    )
    if user_id:
        Entitlement.create(user=user_id, recording=recording.id)
    return recording


def test_authenticated_range_streaming(tmp_path):
    media = tmp_path / "sample.mp4"
    media.write_bytes(bytes(range(100)))
    thumbnail_path(media).write_bytes(b"jpeg")
    with TestClient(app) as client:
        user = client.post("/api/auth/setup", json={"username": "admin", "password": "secret"}).json()
        recording_id = _fixture_recording(media, user_id=user["id"]).id
        response = client.get(f"/api/media/{recording_id}", headers={"Range": "bytes=10-19"})
        assert response.status_code == 206
        assert response.content == bytes(range(10, 20))


def test_missing_thumbnail_is_generated_lazily(monkeypatch, tmp_path):
    bundle = tmp_path / "sample.hls"
    bundle.mkdir()
    master = bundle / "master.m3u8"
    master.write_text("#EXTM3U\n")
    calls = []

    def fake_thumbnail(path):
        calls.append(path)
        destination = path.parent / "thumbnail.jpg"
        destination.write_bytes(b"jpeg")
        return destination

    monkeypatch.setattr("app.routers.media.generate_thumbnail", fake_thumbnail)
    with TestClient(app) as client:
        user = client.post("/api/auth/setup", json={"username": "admin", "password": "secret"}).json()
        recording_id = _fixture_recording(master, user_id=user["id"]).id
        recording = Recording.get_by_id(recording_id)
        Recording.update(state="completed").where(Recording.id == recording.id).execute()
        listed = client.get("/api/recordings").json()
        assert listed[0]["thumbnail"] == f"/api/thumbnails/{recording_id}"
        response = client.get(f"/api/thumbnails/{recording_id}")
        assert response.status_code == 200
        assert response.content == b"jpeg"
    assert calls == [master]


def test_radio_aac_redirects_to_audio_only_hls(monkeypatch, tmp_path):
    from app.routers import media as media_router

    bundle = tmp_path / "sample.hls"
    bundle.mkdir()
    master = bundle / "master.m3u8"
    master.write_text("#EXTM3U\n")

    def fake_generate(_path):
        playlist = bundle / "audio.m3u8"
        playlist.write_text("#EXTM3U\n")
        return playlist

    monkeypatch.setattr(media_router, "generate_aac_hls", fake_generate)
    with TestClient(app) as client:
        user = client.post("/api/auth/setup", json={"username": "admin", "password": "secret"}).json()
        recording_id = _fixture_recording(master, user_id=user["id"]).id
        response = client.get(f"/api/media/{recording_id}/audio?format=aac", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == f"/api/hls/{recording_id}/audio.m3u8"


def test_aac_hls_generation_uses_bundle_as_ffmpeg_working_directory(monkeypatch, tmp_path):
    bundle = tmp_path / "sample.hls"
    bundle.mkdir()
    master = bundle / "master.m3u8"
    master.write_text("#EXTM3U\n")
    observed = {}

    def fake_run(command, **kwargs):
        observed["cwd"] = kwargs["cwd"]
        Path(kwargs["cwd"], command[command.index("-hls_fmp4_init_filename") + 1]).write_bytes(b"init")
        Path(command[-1]).write_text("#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\nseg.m4s\n")
        Path(kwargs["cwd"], ".audio-test-segment_00000.m4s").write_bytes(b"segment")
        return type("Result", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(media_service.threading, "get_ident", lambda: "test")
    monkeypatch.setattr(media_service.subprocess, "run", fake_run)
    playlist = media_service.generate_aac_hls(master)
    assert playlist == bundle / "audio.m3u8"
    assert observed["cwd"] == bundle
    assert (bundle / "audio-init.mp4").is_file()


def test_flac_is_generated_only_when_requested_and_then_cached(monkeypatch, tmp_path):
    bundle = tmp_path / "sample.hls"
    bundle.mkdir()
    master = bundle / "master.m3u8"
    master.write_text("#EXTM3U\n")
    (bundle / "audio.m3u8").write_text("#EXTM3U\n")
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"flac")
        return type("Result", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(media_service.subprocess, "run", fake_run)
    first = media_service.generate_flac_asset(master)
    second = media_service.generate_flac_asset(master)
    assert first == second
    assert first.read_bytes() == b"flac"
    assert len(calls) == 1
    assert "12" in calls[0]


def test_download_mp4_is_lazy_cached_stream_copy(monkeypatch, tmp_path):
    bundle = tmp_path / "sample.hls"
    bundle.mkdir()
    master = bundle / "master.m3u8"
    master.write_text("#EXTM3U\n")
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"mp4")
        return type("Result", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(media_service.subprocess, "run", fake_run)
    assert media_service.generate_download_mp4(master).read_bytes() == b"mp4"
    media_service.generate_download_mp4(master)
    assert len(calls) == 1
    assert calls[0][calls[0].index("-c") + 1] == "copy"


def test_capture_command_copies_video_and_aac_directly(tmp_path):
    command = recorder._capture_command("https://cdn/master.m3u8", "https://chzzk/live/x", {}, tmp_path, "live")
    assert command.count("copy") == 2
    assert "libx264" not in command
    assert "libx265" not in command
    assert "video.m3u8" in command and "audio.m3u8" in command


def test_hls_mirror_publishes_speed_and_eta_while_segments_are_written(tmp_path):
    recording = _fixture_recording(state="recording")
    stopped = asyncio.Event()

    async def exercise():
        task = asyncio.create_task(
            _publish_progress(recording.id, tmp_path, 10 * 1024 * 1024, stopped)
        )
        await asyncio.sleep(0.05)
        (tmp_path / ".segment.m4s.part").write_bytes(b"x" * 1024 * 1024)
        await asyncio.sleep(0.55)
        stopped.set()
        await task

    asyncio.run(exercise())
    stored = Recording.get_by_id(recording.id)
    assert stored.size >= 1024 * 1024
    assert stored.speed_bps > 0
    assert stored.eta_seconds is not None
    assert stored.eta_seconds > 0


def test_progressive_source_uses_parallel_aria2_when_available(monkeypatch, tmp_path):
    recording = _fixture_recording(state="recording")
    observed = {}

    async def fake_aria(url, destination, cookies, referer, recording_id, total_size, connections):
        observed.update(
            url=url,
            destination=destination,
            cookies=cookies,
            referer=referer,
            recording_id=recording_id,
            total_size=total_size,
            connections=connections,
        )

    monkeypatch.setattr(recorder.shutil, "which", lambda _name: "aria2c")
    monkeypatch.setattr(recorder, "download_progressive_aria2", fake_aria)
    monkeypatch.setattr(recorder.settings, "download_connections", 24)
    destination = tmp_path / "source.mp4"
    asyncio.run(
        recorder._download_progressive_source(
            "https://cdn.example/video.mp4",
            destination,
            {"session": "cookie"},
            "https://chzzk.naver.com/video/1",
            recording.id,
            123456,
        )
    )

    assert observed["destination"] == destination
    assert observed["recording_id"] == recording.id
    assert observed["total_size"] == 123456
    assert observed["connections"] == 16


def test_vod_hls_segments_are_downloaded_concurrently(monkeypatch, tmp_path):
    recording = _fixture_recording(state="recording")
    real_client = httpx.AsyncClient
    active = 0
    peak = 0

    async def handler(request: httpx.Request):
        nonlocal active, peak
        if request.url.path.endswith(".m4s"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1
            return httpx.Response(200, content=b"segment")
        playlist = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n" + "".join(
            f"#EXTINF:6,\nsegment-{index}.m4s\n" for index in range(8)
        ) + "#EXT-X-ENDLIST\n"
        return httpx.Response(200, text=playlist)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        hls_mirror.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    master, _, duration = asyncio.run(
        hls_mirror.mirror_hls(
            "https://cdn.example/master.m3u8",
            tmp_path,
            recording_id=recording.id,
            referer="https://chzzk.naver.com/video/1",
            cookies={},
            live=False,
            concurrency=4,
        )
    )

    assert master.is_file()
    assert duration == 48
    assert peak == 4
    assert len(list(tmp_path.glob("segment-*.m4s"))) == 8


def test_h264_transcode_command_is_libx264_crf23(monkeypatch, tmp_path):
    source = tmp_path / "legacy-hevc.mp4"
    source.write_bytes(b"hevc")
    observed = []

    def fake_run(command, **kwargs):
        observed.append(command)
        cwd = Path(kwargs["cwd"])
        (cwd / "video.m3u8").write_text("#EXTM3U\n")
        (cwd / "video-init.mp4").write_bytes(b"init")
        (cwd / "video-segment_00000.m4s").write_bytes(b"segment")
        return type("Result", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(media_service.subprocess, "run", fake_run)
    media_service._run_variant(source, tmp_path, "video", transcode_h264=True)
    command = observed[0]
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-crf") + 1] == "23"
    assert command[command.index("-preset") + 1] == "medium"


def test_v2_hevc_migration_transcodes_to_h264(monkeypatch, tmp_path):
    monkeypatch.setattr(media_service.settings, "recordings_dir", tmp_path)
    source = tmp_path / "legacy.mp4"
    source.write_bytes(b"hevc")
    recording = _fixture_recording(source)
    Recording.update(storage_version=2).where(Recording.id == recording.id).execute()
    observed = {}

    monkeypatch.setattr(media_service, "_probe_codecs", lambda path: ("hevc", "aac"))
    monkeypatch.setattr(media_service.shutil, "disk_usage", lambda _path: type("Usage", (), {"free": 10**12})())
    monkeypatch.setattr(media_service, "probe_duration", lambda _path: 42.0)
    monkeypatch.setattr(media_service, "generate_thumbnail", lambda _path: None)

    def fake_package(src, destination, **kwargs):
        observed.update(kwargs)
        destination.mkdir()
        master = destination / "master.m3u8"
        master.write_text("#EXTM3U\n")
        return master

    monkeypatch.setattr(media_service, "package_media_as_hls", fake_package)
    master = media_service.migrate_legacy_recording(recording.id)
    stored = Recording.get_by_id(recording.id)
    assert observed["transcode_h264"] is True
    assert stored.path == str(master)
    assert stored.storage_version == STORAGE_VERSION


def test_v2_h264_migration_uses_stream_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(media_service.settings, "recordings_dir", tmp_path)
    source = tmp_path / "legacy.mp4"
    source.write_bytes(b"h264")
    recording = _fixture_recording(source)
    Recording.update(storage_version=2).where(Recording.id == recording.id).execute()
    observed = {}
    monkeypatch.setattr(media_service, "_probe_codecs", lambda path: ("h264", "aac"))
    monkeypatch.setattr(media_service.shutil, "disk_usage", lambda _path: type("Usage", (), {"free": 10**12})())
    monkeypatch.setattr(media_service, "probe_duration", lambda _path: 42.0)
    monkeypatch.setattr(media_service, "generate_thumbnail", lambda _path: None)

    def fake_package(src, destination, **kwargs):
        observed.update(kwargs)
        destination.mkdir()
        master = destination / "master.m3u8"
        master.write_text("#EXTM3U\n")
        return master

    monkeypatch.setattr(media_service, "package_media_as_hls", fake_package)
    media_service.migrate_legacy_recording(recording.id)
    assert observed["transcode_h264"] is False


def test_admin_can_permanently_delete_hls_archive(monkeypatch, tmp_path):
    monkeypatch.setattr(recorder.settings, "recordings_dir", tmp_path)
    bundle = tmp_path / "shared.hls"
    bundle.mkdir()
    master = bundle / "master.m3u8"
    master.write_text("#EXTM3U\n")
    with TestClient(app) as client:
        owner = client.post("/api/auth/setup", json={"username": "admin", "password": "secret"}).json()
        viewer = User.create(username="viewer", password="unused")
        recording = _fixture_recording(master, user_id=owner["id"])
        Entitlement.create(user=viewer.id, recording=recording.id)
        assert client.delete(f"/api/admin/recordings/{recording.id}").status_code == 204
    assert not bundle.exists()
    assert Recording.get_or_none(Recording.id == recording.id) is None
    assert AuditLog.get().action == "recording.purge"
