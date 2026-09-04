"""Parse-level checks for the Linux installer's argument handling.

Without bash on this host the scripts cannot be executed, so confirm the
option table and the substitution wiring by reading them directly. A typo in a
case label silently ignores a flag, which is the failure this catches.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL = (ROOT / "scripts" / "install-worker-systemd.sh").read_text(encoding="utf-8")
UNINSTALL = (ROOT / "scripts" / "uninstall-worker-systemd.sh").read_text(encoding="utf-8")
BUILD = (ROOT / "scripts" / "build-worker.sh").read_text(encoding="utf-8")


def _case_labels(source: str) -> set[str]:
    """Options the while/case loop actually accepts."""
    body = source.split("while [[ $# -gt 0 ]]", 1)[1]
    labels: set[str] = set()
    for line in body.splitlines():
        match = re.match(r"\s*((?:--?[a-z|-]+)+)\)", line)
        if match:
            labels.update(match.group(1).split("|"))
        if line.strip() == "done":
            break
    return labels


def test_installer_accepts_every_advertised_option():
    labels = _case_labels(INSTALL)
    assert {"--server", "--token", "--binary", "--user", "--prefix"} <= labels

    # Anything in the usage text must be handled, or the flag is a lie.
    usage = INSTALL.split("<<'USAGE'", 1)[1].split("USAGE", 1)[0]
    for advertised in set(re.findall(r"^\s+(--[a-z-]+)", usage, re.MULTILINE)):
        assert advertised in labels, f"usage documents unhandled {advertised}"


def test_uninstaller_accepts_every_advertised_option():
    labels = _case_labels(UNINSTALL)
    assert {"--prefix", "--user"} <= labels

    usage = UNINSTALL.split("<<'USAGE'", 1)[1].split("USAGE", 1)[0]
    for advertised in set(re.findall(r"^\s+(--[a-z-]+)", usage, re.MULTILINE)):
        assert advertised in labels, f"usage documents unhandled {advertised}"


def test_installer_rejects_unknown_options():
    assert "unknown option" in INSTALL
    assert "unknown option" in UNINSTALL


def test_installer_requires_both_credentials():
    assert '[[ -n "$server" && -n "$token" ]] || usage' in INSTALL


def test_installer_requires_root_before_writing():
    assert INSTALL.index("id -u") < INSTALL.index("install -d")
    assert UNINSTALL.index("id -u") < UNINSTALL.index("rm -rf")


def test_unit_substitution_covers_all_placeholders():
    unit = (ROOT / "deploy" / "linux" / "archiver-worker.service").read_text(encoding="utf-8")
    substituted = set(re.findall(r"s\|(__[A-Z_]+__)\|", INSTALL))
    assert set(re.findall(r"__[A-Z_]+__", unit)) == substituted


def test_build_script_cleans_up_its_virtualenv():
    assert "rm -rf \"$venv\"" in BUILD
    assert "--onefile" in BUILD
    assert "--name archiver-worker" in BUILD

