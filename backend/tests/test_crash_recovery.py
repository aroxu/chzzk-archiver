"""Regressions for the abandoned 9.42 GB capture.

A worker that died mid-broadcast left three defects behind: the stored size
stopped tracking the file, a retry threw away footage that was already on
disk, and the library showed an unplayable .ts with only a raw streamlink
error to explain it.
"""

import asyncio
from pathlib import Path

from app import lifecycle
from app.models import Broadcast, Channel, Entitlement, Recording, User
from app.services import recorder


def _live_recording(tmp_path: Path, captured: bytes = b"", state: str = "recording"):
    channel = Channel.create(chzzk_id="c" * 32, name="스트리머")
    broadcast = Broadcast.create(
        channel=channel.id,
        broadcast_id="live-crash",
        source_type="live",
        source_url="https://chzzk.naver.com/live/" + "c" * 32,
        title="중단된 방송",
    )
    partial = tmp_path / "capture.ts"
    if captured:
        partial.write_bytes(captured)
    return Recording.create(
        broadcast=broadcast.id,
        state=state,
        path=str(partial),
        # The crashed monitor never caught up with the real file size.
        size=len(captured) // 4,
    ), partial


def test_restart_resyncs_size_with_the_file_on_disk(tmp_path):
    recording, partial = _live_recording(tmp_path, b"x" * 4096)
    assert Recording.get_by_id(recording.id).size == 1024

    assert lifecycle.requeue_interrupted() == [recording.id]

    stored = Recording.get_by_id(recording.id)
    assert stored.state == "queued"
    assert stored.size == partial.stat().st_size == 4096


def test_restart_survives_a_capture_file_that_vanished(tmp_path):
    recording, partial = _live_recording(tmp_path, b"y" * 512)
    partial.unlink()

    assert lifecycle.requeue_interrupted() == [recording.id]
    # Nothing to resync against, so the stored value is left untouched.
    assert Recording.get_by_id(recording.id).size == 128


def test_ended_live_stream_salvages_the_bytes_already_captured(monkeypatch, tmp_path):
    """The broadcast ended, so retrying is futile; keep the footage."""
    monkeypatch.setattr(recorder.settings, "recordings_dir", tmp_path)
    monkeypatch.setattr(recorder, "generate_thumbnail", lambda _path: None)
    monkeypatch.setattr(recorder, "enqueue_encoding", lambda *_a, **_k: None)
    monkeypatch.setattr(recorder.settings, "encoding_mode", "disabled")

    class FakeProcess:
        def __init__(self, args, stdout=None):
            self.args = args
            self.stdout = stdout
            # streamlink fails because the stream is gone; ffmpeg still runs.
            self.returncode = 1 if args[0] == "streamlink" else 0

        async def communicate(self):
            if self.args[0] == "ffmpeg":
                Path(self.args[-1]).write_bytes(b"remuxed")
                return b"", b""
            return b"", b"The stream is unavailable"

    async def fake_subprocess(*args, **kwargs):
        return FakeProcess(args, kwargs.get("stdout"))

    monkeypatch.setattr(recorder.asyncio, "create_subprocess_exec", fake_subprocess)

    user = User.create(username="viewer", password="x")
    channel = Channel.create(chzzk_id="d" * 32, name="스트리머")
    broadcast = Broadcast.create(
        channel=channel.id,
        broadcast_id="live-ended",
        source_type="live",
        source_url="https://chzzk.naver.com/live/" + "d" * 32,
        title="종료된 방송",
    )
    partial = tmp_path / "already-captured.ts"
    partial.write_bytes(b"hours-of-video")
    recording = Recording.create(
        broadcast=broadcast.id, state="queued", path=str(partial), size=partial.stat().st_size
    )
    Entitlement.create(user=user.id, recording=recording.id)

    asyncio.run(recorder.run_recording(recording.id))

    stored = Recording.get_by_id(recording.id)
    assert stored.state == "completed", stored.error
    assert Path(stored.path).suffix == ".mp4"
    assert Path(stored.path).read_bytes() == b"remuxed"


def test_failure_without_any_bytes_still_reports_failed(monkeypatch, tmp_path):
    """Salvage must not mask a capture that never started."""
    monkeypatch.setattr(recorder.settings, "recordings_dir", tmp_path)
    monkeypatch.setattr(recorder, "generate_thumbnail", lambda _path: None)

    class FakeProcess:
        returncode = 1

        def __init__(self, args, stdout=None):
            self.args = args
            self.stdout = stdout

        async def communicate(self):
            return b"", b"No playable streams found"

    async def fake_subprocess(*args, **kwargs):
        return FakeProcess(args, kwargs.get("stdout"))

    monkeypatch.setattr(recorder.asyncio, "create_subprocess_exec", fake_subprocess)

    channel = Channel.create(chzzk_id="e" * 32, name="스트리머")
    broadcast = Broadcast.create(
        channel=channel.id,
        broadcast_id="live-nothing",
        source_type="live",
        source_url="https://chzzk.naver.com/live/" + "e" * 32,
        title="시작 못한 방송",
    )
    recording = Recording.create(broadcast=broadcast.id, state="queued")

    asyncio.run(recorder.run_recording(recording.id))

    stored = Recording.get_by_id(recording.id)
    assert stored.state == "failed"
    assert stored.size == 0


def test_partial_failure_explains_itself_in_korean(monkeypatch, tmp_path):
    """The card previously showed only a raw streamlink error."""
    monkeypatch.setattr(recorder.settings, "recordings_dir", tmp_path)
    monkeypatch.setattr(recorder, "generate_thumbnail", lambda _path: None)

    class FakeProcess:
        returncode = 0

        def __init__(self, args, stdout=None):
            self.args = args
            self.stdout = stdout

        async def communicate(self):
            if self.args[0] == "streamlink":
                self.stdout.write(b"z" * 2048)
                self.stdout.flush()
                return b"", b""
            raise RuntimeError("remux exploded")

    async def fake_subprocess(*args, **kwargs):
        return FakeProcess(args, kwargs.get("stdout"))

    monkeypatch.setattr(recorder.asyncio, "create_subprocess_exec", fake_subprocess)

    channel = Channel.create(chzzk_id="f" * 32, name="스트리머")
    broadcast = Broadcast.create(
        channel=channel.id,
        broadcast_id="live-partial",
        source_type="live",
        source_url="https://chzzk.naver.com/live/" + "f" * 32,
        title="부분 방송",
    )
    recording = Recording.create(broadcast=broadcast.id, state="queued")

    asyncio.run(recorder.run_recording(recording.id))

    stored = Recording.get_by_id(recording.id)
    assert stored.state == "failed"
    assert "부분 파일" in (stored.error or "")
    assert "재생 불가" in (stored.error or "")
    assert stored.size > 0

