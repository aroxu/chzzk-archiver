import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.encoding_commands import (
    ffmpeg_encode_command,
    ffmpeg_stream_command,
    output_extension,
    parse_ffmpeg_time,
    with_progress,
)
from app.main import app
from app.models import Broadcast, Channel, EncodingJob, Recording, WorkerNode
from app.services import encoding
from app.services.stream_transport import handle_stream


def _recording(path: Path, state: str = "processing") -> Recording:
    channel = Channel.create(chzzk_id="encode-channel", name="encoder")
    broadcast = Broadcast.create(channel=channel.id, broadcast_id="encode-1")
    return Recording.create(
        broadcast=broadcast.id,
        state=state,
        path=str(path),
        size=path.stat().st_size,
    )


def test_codec_commands_use_crf_for_cpu_and_cqp_for_nvenc():
    cpu = ffmpeg_encode_command(
        Path("in.ts"), Path("out.mp4"), encoder="libx265", quality=23
    )
    gpu = ffmpeg_encode_command(
        Path("in.ts"), Path("out.mp4"), encoder="hevc_nvenc", quality=23
    )
    stream = ffmpeg_stream_command(encoder="libx265", quality=23)
    assert cpu[cpu.index("-crf") + 1] == "23"
    assert gpu[gpu.index("-rc") + 1 : gpu.index("-rc") + 4] == ["constqp", "-qp", "23"]
    assert stream[-3:] == ["-f", "mpegts", "pipe:1"]
    flac_stream = ffmpeg_stream_command(encoder="libx265", audio_mode="flac24")
    assert flac_stream[-1] == "pipe:1"
    assert flac_stream[flac_stream.index("-f") + 1] == "mp4"
    assert "frag_keyframe+empty_moov+default_base_moof" in flac_stream
    assert "matroska" not in flac_stream
    assert output_extension("flac24") == ".mp4"
    flac_file = ffmpeg_encode_command(
        Path("in.ts"), Path("out.mp4"), encoder="libx265", audio_mode="flac24"
    )
    assert flac_file[flac_file.index("-sample_fmt:a") + 1] == "s32"
    assert flac_file[flac_file.index("-bits_per_raw_sample:a") + 1] == "24"
    assert flac_file[flac_file.index("-strict") + 1] == "experimental"


def test_ffmpeg_progress_helpers():
    command = with_progress(["ffmpeg", "-i", "in.ts", "out.mp4"])
    assert command[:4] == ["ffmpeg", "-nostats", "-progress", "pipe:2"]
    assert parse_ffmpeg_time("01:02:03.500000") == 3723.5
    assert parse_ffmpeg_time("N/A") is None


def test_encoding_heartbeat_calculates_progress_and_eta(tmp_path):
    source = tmp_path / "capture.ts"
    source.write_bytes(b"transport")
    recording = _recording(source)
    worker = WorkerNode.create(id="progress-1", hostname="linux", platform="Linux")
    job = EncodingJob.create(
        recording=recording.id,
        state="leased",
        worker=worker.id,
        duration_seconds=100,
        lease_expires_at=datetime.now(UTC),
    )

    assert encoding.heartbeat_job(
        job.id,
        worker.id,
        processed_seconds=25,
        encoding_speed=2,
    )
    stored = EncodingJob.get_by_id(job.id)
    assert stored.progress == 25
    assert stored.processed_seconds == 25
    assert stored.encoding_speed == 2
    assert stored.eta_seconds == 37

    EncodingJob.update(state="uploading").where(EncodingJob.id == job.id).execute()
    assert encoding.heartbeat_job(job.id, worker.id, processed_seconds=30, encoding_speed=2)
    assert EncodingJob.get_by_id(job.id).state == "uploading"


def test_thumbnail_survives_missing_duration(monkeypatch, tmp_path):
    """Some streamed containers have no header duration; ffprobe answers N/A."""
    from app.services import media

    video = tmp_path / "streamed.mkv"
    video.write_bytes(b"matroska")
    captured: list[list[str]] = []

    class Result:
        stdout = "N/A\n"

    def fake_run(command, **_kwargs):
        captured.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"jpeg")
        return Result()

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    assert media.generate_thumbnail(video) == media.thumbnail_path(video)
    seek = captured[1][captured[1].index("-ss") + 1]
    assert float(seek) == 0.0


def test_worker_lease_advertises_tcp_data_plane(monkeypatch, tmp_path):
    source = tmp_path / "capture.ts"
    source.write_bytes(b"transport")
    recording = _recording(source, state="recording")
    monkeypatch.setattr(encoding.settings, "encoding_mode", "remote")
    monkeypatch.setattr(encoding.settings, "worker_token", "worker-secret")
    job = encoding.enqueue_encoding(recording.id, source, mark_processing=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/worker/lease",
            headers={"Authorization": "Bearer worker-secret", "X-Worker-ID": "win-1"},
            json={
                "worker_id": "win-1",
                "hostname": "encode-pc",
                "platform": "Windows",
                "encoders": ["libx265"],
                "version": "test",
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == job.id
    assert payload["stream_port"] == encoding.settings.worker_stream_port
    assert "input_url" not in payload
    assert "upload_url" not in payload


def test_full_duplex_tcp_streams_source_and_receives_encoded(monkeypatch, tmp_path):
    source = tmp_path / "capture.ts"
    source.write_bytes(b"growing-transport-stream")
    recording = _recording(source)
    worker = WorkerNode.create(id="linux-1", hostname="linux", platform="Linux")
    job = EncodingJob.create(
        recording=recording.id,
        state="leased",
        worker=worker.id,
        source_path=str(source),
        lease_expires_at=datetime.now(UTC),
    )
    monkeypatch.setattr(encoding.settings, "worker_token", "tcp-secret")
    finalized = []

    def fake_finalize(job_id, worker_id):
        finalized.append((job_id, worker_id))
        return Path(EncodingJob.get_by_id(job_id).upload_path)

    monkeypatch.setattr("app.services.stream_transport.complete_uploaded_job", fake_finalize)

    async def scenario():
        server = await asyncio.start_server(handle_stream, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            json.dumps({"token": "tcp-secret", "worker_id": worker.id, "job_id": job.id}).encode()
            + b"\n"
        )
        await writer.drain()
        assert await reader.readline() == b"OK\n"
        received = await reader.read()
        assert received == source.read_bytes()
        writer.write(b"encoded-result")
        await writer.drain()
        writer.write_eof()
        await asyncio.sleep(0.1)
        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())
    stored = EncodingJob.get_by_id(job.id)
    assert stored.state == "uploading"
    assert Path(stored.upload_path).read_bytes() == b"encoded-result"
    assert finalized == [(job.id, worker.id)]


def test_late_worker_failure_cannot_delete_a_finalizing_result(monkeypatch, tmp_path):
    source = tmp_path / "capture.ts"
    source.write_bytes(b"transport")
    uploaded = tmp_path / ".worker-result.mp4"
    uploaded.write_bytes(b"encoded")
    recording = _recording(source)
    worker = WorkerNode.create(id="safe-finalize", hostname="win", platform="Windows")
    job = EncodingJob.create(
        recording=recording.id,
        state="finalizing",
        worker=worker.id,
        source_path=str(source),
        upload_path=str(uploaded),
    )
    monkeypatch.setattr(encoding.settings, "worker_token", "worker-secret")
    with TestClient(app) as client:
        response = client.post(
            f"/api/worker/jobs/{job.id}/fail",
            headers={"Authorization": "Bearer worker-secret", "X-Worker-ID": worker.id},
            json={"error": "late completion request timeout"},
        )
    assert response.status_code == 204
    assert EncodingJob.get_by_id(job.id).state == "finalizing"
    assert uploaded.exists()
