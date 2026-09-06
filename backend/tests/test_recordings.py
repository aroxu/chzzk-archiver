import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.models import AuditLog, Broadcast, Channel, Entitlement, Recording, User
from app.services import media as media_service
from app.services import recorder
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


def test_radio_aac_redirects_to_audio_only_hls(monkeypatch, tmp_path):
    from app.routers import media as media_router

    bundle = tmp_path / "sample.hls"
    bundle.mkdir()
    master = bundle / "master.m3u8"
    master.write_text("#EXTM3U\n")
    monkeypatch.setattr(media_router, "generate_aac_hls", lambda _path: bundle / "audio.m3u8")
    with TestClient(app) as client:
        user = client.post("/api/auth/setup", json={"username": "admin", "password": "secret"}).json()
        recording_id = _fixture_recording(master, user_id=user["id"]).id
        response = client.get(f"/api/media/{recording_id}/audio?format=aac", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == f"/api/hls/{recording_id}/audio.m3u8"


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
