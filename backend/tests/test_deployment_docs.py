"""The README must describe the scripts that actually exist."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_documented_script_paths_exist():
    referenced = set(re.findall(r"scripts/[a-z0-9-]+\.(?:ps1|sh)", README))
    referenced |= {
        path.replace("\\", "/")
        for path in re.findall(r"scripts\\\\[a-z0-9-]+\.(?:ps1|sh)", README)
    }
    assert referenced, "README stopped documenting the worker scripts"
    for relative in sorted(referenced):
        assert (ROOT / relative).is_file(), f"README references missing {relative}"


def test_documented_linux_flags_are_accepted():
    installer = (ROOT / "scripts" / "install-worker-systemd.sh").read_text(encoding="utf-8")
    for flag in ("--server", "--token"):
        assert f'{flag})' in installer, f"installer does not accept {flag}"
        assert flag in README


def test_documented_windows_parameters_exist():
    service = (ROOT / "scripts" / "install-worker-service.ps1").read_text(encoding="utf-8")
    for parameter in ("Server", "Token", "WinSW", "ServiceAccount", "ServicePassword"):
        assert f"${parameter}" in service, parameter

    uninstall = (ROOT / "scripts" / "uninstall-worker.ps1").read_text(encoding="utf-8")
    for switch in ("Service", "Task", "RemoveFiles"):
        assert f"[switch]${switch}" in uninstall, switch


def test_docker_instructions_match_the_dockerfile():
    dockerfile_path = re.search(r"docker build -f (\S+)", README)
    assert dockerfile_path, "README no longer shows how to build the image"
    assert (ROOT / dockerfile_path.group(1)).is_file()

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    profile = re.search(r"--profile (\S+)", README)
    assert profile and profile.group(1) in compose
