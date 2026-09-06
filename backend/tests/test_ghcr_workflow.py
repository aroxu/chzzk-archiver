"""Static guarantees for the GHCR release path."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-containers.yml"


def test_publish_workflow_is_valid_yaml_and_has_package_permission():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # YAML 1.1 parsers interpret the key "on" as True; accept both forms.
    assert workflow.get("on", workflow.get(True))
    assert workflow["permissions"]["packages"] == "write"


def test_publish_workflow_builds_controller_and_pushes_to_ghcr():
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "backend/Dockerfile" in source
    assert "worker/Dockerfile" not in source
    assert "ghcr.io/${{ github.repository }}" in source
    assert "push: true" in source
    assert "linux/amd64,linux/arm64" in source
    assert "sbom: true" in source


def test_compose_can_pull_published_images_without_building():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "ghcr.io/aroxu/chzzk-archiver:latest" in compose
    assert "chzzk-archiver-worker" not in compose


def test_frontend_build_runs_once_on_the_native_build_platform():
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM --platform=$BUILDPLATFORM node:" in dockerfile
    assert "pnpm install --frozen-lockfile" in dockerfile
