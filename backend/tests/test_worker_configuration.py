"""Remote worker environment and encoder policy tests."""

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
