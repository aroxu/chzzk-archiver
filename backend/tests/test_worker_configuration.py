"""Remote worker environment and encoder policy tests."""

from io import BytesIO

from app import worker


class FakeResponse:
    status_code = 204


class FakeClient:
    def __init__(self):
        self.payload = None

    def post(self, _url, *, headers, json):
        self.payload = json
        return FakeResponse()


def test_precomputed_encoders_are_reused_between_worker_polls(monkeypatch):
    """Idle polling must not launch several trial FFmpeg encodes every five seconds."""
    monkeypatch.setattr(
        worker, "detect_hevc_encoders", lambda _ffmpeg: (_ for _ in ()).throw(AssertionError())
    )
    client = FakeClient()
    assert not worker.lease_once(
        client,
        "https://archive.example",
        "secret",
        "gpu-1",
        "ffmpeg",
        "hevc_nvenc",
        ["hevc_nvenc"],
    )
    assert client.payload["encoders"] == ["hevc_nvenc"]


def test_worker_reads_machine_progress_from_ffmpeg_stderr():
    reports = []
    errors = []
    worker._read_ffmpeg_progress(
        BytesIO(
            b"frame=123\n"
            b"out_time=00:02:07.800000\n"
            b"speed=1.75x\n"
            b"progress=continue\n"
        ),
        lambda processed, speed: reports.append((processed, speed)),
        errors,
    )
    assert reports == [(127.8, 1.75)]
    assert errors == ["frame=123"]


def test_worker_normalizes_hostname_only_controller_urls():
    assert worker.normalize_server_url("ca.example.test/") == "https://ca.example.test"
    assert worker.normalize_server_url("localhost:8010") == "http://localhost:8010"
    assert worker.normalize_server_url("http://127.0.0.1:8010/") == "http://127.0.0.1:8010"
