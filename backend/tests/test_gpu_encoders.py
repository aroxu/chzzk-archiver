"""GPU encoder detection and selection.

This host advertises QSV, AMF and VAAPI in `ffmpeg -encoders` while only NVENC
actually runs, so trusting the advertised list would pick an encoder that fails
at encode time. These tests pin the probe-and-fallback behaviour.
"""

import subprocess

import pytest

from app import encoding_commands as ec
from app.worker import WorkerSettings, configured_encoders


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


ADVERTISED = (
    " V....D libx265              libx265 H.265 / HEVC\n"
    " V....D hevc_amf             AMD AMF HEVC encoder\n"
    " V....D hevc_nvenc           NVIDIA NVENC hevc encoder\n"
    " V..... hevc_qsv             HEVC (Intel Quick Sync Video)\n"
    " V....D hevc_vaapi           H.265/HEVC (VAAPI)\n"
)


def _fake_ffmpeg(monkeypatch, working: set[str]):
    """Advertise everything, but only let `working` survive the trial encode."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "-encoders" in command:
            return FakeCompleted(stdout=ADVERTISED)
        encoder = command[command.index("-c:v") + 1]
        return FakeCompleted(returncode=0 if encoder in working else 1)

    monkeypatch.setattr(ec.subprocess, "run", fake_run)
    return calls


def test_detection_drops_advertised_but_unusable_encoders(monkeypatch):
    _fake_ffmpeg(monkeypatch, working={"hevc_nvenc"})
    assert ec.detect_hevc_encoders() == ["hevc_nvenc", "libx265"]


def test_detection_puts_hardware_before_software(monkeypatch):
    _fake_ffmpeg(monkeypatch, working={"hevc_nvenc", "hevc_qsv"})
    detected = ec.detect_hevc_encoders()
    assert detected[0] == "hevc_nvenc"
    assert detected[-1] == "libx265"


def test_software_encoder_is_never_probed(monkeypatch):
    """libx265 always works; spending a trial encode on it wastes startup time."""
    calls = _fake_ffmpeg(monkeypatch, working=set())
    assert ec.detect_hevc_encoders() == ["libx265"]
    probed = [c[c.index("-c:v") + 1] for c in calls if "-c:v" in c]
    assert "libx265" not in probed


def test_detection_survives_a_hanging_probe(monkeypatch):
    def fake_run(command, **kwargs):
        if "-encoders" in command:
            return FakeCompleted(stdout=ADVERTISED)
        raise subprocess.TimeoutExpired(cmd=command, timeout=30)

    monkeypatch.setattr(ec.subprocess, "run", fake_run)
    assert ec.detect_hevc_encoders() == ["libx265"]


def test_auto_prefers_the_gpu():
    assert ec.choose_encoder("auto", ["hevc_nvenc", "libx265"]) == "hevc_nvenc"
    assert ec.choose_encoder("auto", ["libx265"]) == "libx265"


def test_unavailable_request_falls_back_instead_of_failing():
    """A slower encode beats losing the recording."""
    assert ec.choose_encoder("hevc_qsv", ["hevc_nvenc", "libx265"]) == "libx265"


def test_no_encoder_at_all_is_an_error():
    with pytest.raises(RuntimeError):
        ec.choose_encoder("auto", [])


def test_presets_are_translated_per_encoder():
    """NVENC rejects "medium" and libx265 rejects "p5"."""
    assert ec.resolve_preset("hevc_nvenc", "auto") == "p5"
    assert ec.resolve_preset("hevc_nvenc", "medium") == "p5"
    assert ec.resolve_preset("hevc_nvenc", "p7") == "p7"
    assert ec.resolve_preset("libx265", "auto") == "medium"
    assert ec.resolve_preset("libx265", "p5") == "medium"
    assert ec.resolve_preset("libx265", "veryslow") == "veryslow"
    assert ec.resolve_preset("hevc_amf", "auto") == "balanced"


def test_auto_preset_produces_a_valid_command_for_every_encoder():
    from pathlib import Path

    for encoder in ("libx265", "hevc_nvenc", "hevc_qsv", "hevc_amf"):
        command = ec.ffmpeg_encode_command(
            Path("in.ts"), Path("out.mp4"), encoder=encoder, quality=23, preset="auto"
        )
        flag = "-quality" if encoder == "hevc_amf" else "-preset"
        assert command[command.index(flag) + 1] in ec.VALID_PRESETS[encoder]
        assert "auto" not in command


def test_nvenc_uses_constant_quantiser_at_the_requested_quality():
    from pathlib import Path

    command = ec.ffmpeg_encode_command(
        Path("in.ts"), Path("out.mp4"), encoder="hevc_nvenc", quality=23
    )
    assert command[command.index("-rc") + 1] == "constqp"
    assert command[command.index("-qp") + 1] == "23"


def test_worker_auto_advertises_all_working_encoders():
    detected = ["hevc_nvenc", "libx265"]
    assert configured_encoders(detected, "auto") == detected


def test_worker_explicit_encoder_restricts_job_leases():
    detected = ["hevc_nvenc", "libx265"]
    assert configured_encoders(detected, "hevc_nvenc") == ["hevc_nvenc"]
    assert configured_encoders(detected, "hevc_qsv") == []


def test_worker_rejects_unknown_encoder():
    with pytest.raises(ValueError, match="지원하지 않는"):
        configured_encoders(["libx265"], "made_up_encoder")


def test_worker_reads_encoder_and_connection_from_dotenv(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "ARCHIVER_WORKER_SERVER=https://archive.example\n"
        "ARCHIVER_WORKER_TOKEN=secret\n"
        "ARCHIVER_ENCODING_VIDEO_ENCODER=hevc_nvenc\n",
        encoding="utf-8",
    )
    config = WorkerSettings(_env_file=dotenv)
    assert config.worker_server == "https://archive.example"
    assert config.worker_token == "secret"
    assert config.encoding_video_encoder == "hevc_nvenc"
