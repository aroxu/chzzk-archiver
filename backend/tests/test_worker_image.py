"""Structural checks for the worker container image.

No container runtime is available on this host, so validate the parts that
break a build regardless of engine: stage wiring, copied paths, and the order
of privilege-dropping instructions.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "worker" / "Dockerfile").read_text(encoding="utf-8")


def _instructions(source: str) -> list[tuple[str, str]]:
    """Return (instruction, argument) pairs with continuations joined."""
    joined = re.sub(r"\\\s*\n\s*", " ", source)
    found = []
    for line in joined.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        keyword, _, rest = stripped.partition(" ")
        found.append((keyword.upper(), rest.strip()))
    return found


def test_only_known_instructions_are_used():
    allowed = {
        "FROM", "RUN", "COPY", "ENV", "WORKDIR", "USER",
        "ENTRYPOINT", "HEALTHCHECK", "ARG", "LABEL", "EXPOSE", "VOLUME",
    }
    for keyword, _ in _instructions(DOCKERFILE):
        assert keyword in allowed, keyword


def test_build_stage_is_declared_before_it_is_copied_from():
    instructions = _instructions(DOCKERFILE)
    stages = {
        match.group(1)
        for keyword, rest in instructions
        if keyword == "FROM" and (match := re.search(r"\bAS\s+(\S+)", rest, re.I))
    }
    for keyword, rest in instructions:
        if keyword == "COPY" and (match := re.search(r"--from=(\S+)", rest)):
            assert match.group(1) in stages, match.group(1)


def test_copied_build_context_paths_exist():
    for keyword, rest in _instructions(DOCKERFILE):
        if keyword != "COPY" or "--from=" in rest:
            continue
        source = rest.split()[0]
        assert (ROOT / source).exists(), f"COPY references missing {source}"


def test_container_drops_root_before_the_entrypoint():
    order = [keyword for keyword, _ in _instructions(DOCKERFILE)]
    assert "USER" in order, "the worker would run as root"
    assert order.index("USER") < order.index("ENTRYPOINT")


def test_entrypoint_matches_the_packaged_console_script():
    entrypoint = next(rest for keyword, rest in _instructions(DOCKERFILE) if keyword == "ENTRYPOINT")
    command = re.findall(r'"([^"]+)"', entrypoint)[0]
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f"{command} =" in pyproject, f"{command} is not a declared console script"


def test_healthcheck_uses_the_read_only_diagnostic():
    healthcheck = next(
        (rest for keyword, rest in _instructions(DOCKERFILE) if keyword == "HEALTHCHECK"), None
    )
    assert healthcheck, "a silently broken ffmpeg would go unnoticed"
    assert "--doctor" in healthcheck

