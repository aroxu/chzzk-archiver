"""Static checks for the worker deployment scripts.

The Linux scripts cannot be executed on this Windows host, so verify the
properties that actually break deployments: LF endings, shebangs, strict mode,
placeholder wiring between the unit file and the installer, and the absence of
secrets on any command line.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
DEPLOY = ROOT / "deploy"

SHELL_SCRIPTS = [
    SCRIPTS / "build-worker.sh",
    SCRIPTS / "install-worker-systemd.sh",
    SCRIPTS / "uninstall-worker-systemd.sh",
]
POWERSHELL_SCRIPTS = [
    SCRIPTS / "build-worker.ps1",
    SCRIPTS / "install-worker-service.ps1",
    SCRIPTS / "install-worker-task.ps1",
    SCRIPTS / "uninstall-worker.ps1",
]


def test_every_documented_script_exists():
    for path in [*SHELL_SCRIPTS, *POWERSHELL_SCRIPTS]:
        assert path.is_file(), path

    assert (DEPLOY / "linux" / "archiver-worker.service").is_file()
    assert (DEPLOY / "windows" / "archiver-worker.xml").is_file()
    assert (ROOT / "worker" / "Dockerfile").is_file()


def test_shell_scripts_use_lf_and_strict_mode():
    """CRLF makes a shell script unrunnable on Linux with a cryptic error."""
    for path in SHELL_SCRIPTS:
        raw = path.read_bytes()
        assert b"\r\n" not in raw, f"{path.name} has CRLF line endings"
        text = raw.decode("utf-8")
        assert text.startswith("#!/usr/bin/env bash"), path.name
        assert "set -euo pipefail" in text, path.name


def test_systemd_unit_uses_lf_and_declares_placeholders():
    unit = DEPLOY / "linux" / "archiver-worker.service"
    raw = unit.read_bytes()
    assert b"\r\n" not in raw
    text = raw.decode("utf-8")

    placeholders = set(re.findall(r"__[A-Z_]+__", text))
    assert placeholders == {"__WORKER_USER__", "__WORKER_BIN__", "__WORKER_ENV__"}

    installer = (SCRIPTS / "install-worker-systemd.sh").read_text(encoding="utf-8")
    for placeholder in placeholders:
        assert placeholder in installer, f"installer never substitutes {placeholder}"

    # A worker that dies must come back without manual intervention.
    assert "Restart=always" in text
    assert "EnvironmentFile=" in text


def test_windows_service_config_placeholders_are_substituted():
    config = (DEPLOY / "windows" / "archiver-worker.xml").read_text(encoding="utf-8")
    placeholders = set(re.findall(r"__[A-Z_]+__", config))
    assert placeholders == {
        "__ARCHIVER_WORKER_SERVER__",
        "__ARCHIVER_WORKER_TOKEN__",
        "__ARCHIVER_ENCODING_VIDEO_ENCODER__",
    }

    installer = (SCRIPTS / "install-worker-service.ps1").read_text(encoding="utf-8")
    for placeholder in placeholders:
        assert placeholder in installer, f"installer never substitutes {placeholder}"


def test_token_never_reaches_a_command_line():
    """Arguments are world-readable in the process list; env vars are not."""
    config = (DEPLOY / "windows" / "archiver-worker.xml").read_text(encoding="utf-8")
    assert "--token" not in config
    assert 'name="ARCHIVER_WORKER_TOKEN"' in config

    unit = (DEPLOY / "linux" / "archiver-worker.service").read_text(encoding="utf-8")
    assert "--token" not in unit

    task = (SCRIPTS / "install-worker-task.ps1").read_text(encoding="utf-8")
    assert "-Argument" not in task


def test_installers_fail_before_touching_the_system():
    """A missing binary must be reported, not discovered halfway through."""
    linux = (SCRIPTS / "install-worker-systemd.sh").read_text(encoding="utf-8")
    assert linux.index("worker binary not found") < linux.index("useradd")

    windows = (SCRIPTS / "install-worker-service.ps1").read_text(encoding="utf-8")
    assert windows.index("Worker binary not found") < windows.index("New-Item")


def test_removal_scripts_guard_against_broad_deletes():
    linux = (SCRIPTS / "uninstall-worker-systemd.sh").read_text(encoding="utf-8")
    assert "refusing to delete" in linux
    for dangerous in ("/", "/usr", "/etc", "/opt", "/home", "/var", "/root"):
        assert dangerous in linux

    windows = (SCRIPTS / "uninstall-worker.ps1").read_text(encoding="utf-8")
    assert "Refusing to delete" in windows
    assert "CHZZKArchiveWorker$" in windows


def test_env_file_is_not_world_readable():
    installer = (SCRIPTS / "install-worker-systemd.sh").read_text(encoding="utf-8")
    assert "chmod 0640" in installer
    assert "umask 077" in installer


def test_every_worker_install_path_persists_the_encoder_setting():
    expected = "ARCHIVER_ENCODING_VIDEO_ENCODER"
    assert expected in (DEPLOY / "windows" / "archiver-worker.xml").read_text(encoding="utf-8")
    assert expected in (SCRIPTS / "install-worker-task.ps1").read_text(encoding="utf-8")
    assert expected in (SCRIPTS / "install-worker-systemd.sh").read_text(encoding="utf-8")

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    worker_service = compose.split("encoder-worker:", 1)[1]
    assert expected in worker_service


def test_remote_worker_guide_covers_every_distribution():
    guide = (ROOT / "docs" / "remote-worker-guide.md").read_text(encoding="utf-8")
    for topic in ("Windows", "Linux", "Docker", "systemd", "hevc_nvenc", "--doctor"):
        assert topic in guide


def test_docker_image_runs_unprivileged_with_a_healthcheck():
    dockerfile = (ROOT / "worker" / "Dockerfile").read_text(encoding="utf-8")
    assert "USER worker" in dockerfile
    assert dockerfile.index("USER worker") < dockerfile.index("ENTRYPOINT")
    assert "HEALTHCHECK" in dockerfile
    assert "--doctor" in dockerfile
    assert "ffmpeg" in dockerfile


def test_gitattributes_pins_line_endings():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attributes
    assert "*.service text eol=lf" in attributes
    assert "*.ps1 text eol=crlf" in attributes
