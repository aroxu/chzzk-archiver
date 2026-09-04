#!/usr/bin/env bash
# Build a self-contained Linux worker binary with PyInstaller.
#
# The result only needs ffmpeg on the target host; Python is bundled. Build on
# the oldest distribution you intend to support, because the binary links
# against the build machine's glibc.
set -euo pipefail

workspace="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
output="${OUTPUT:-$workspace/dist/worker}"
venv="$workspace/.build-venv"

cd "$workspace"

if ! command -v "$python_bin" >/dev/null 2>&1; then
    echo "error: $python_bin not found" >&2
    exit 1
fi

"$python_bin" -m venv "$venv"
"$venv/bin/pip" install --quiet --upgrade pip
"$venv/bin/pip" install --quiet . pyinstaller

"$venv/bin/pyinstaller" \
    --noconfirm \
    --clean \
    --onefile \
    --name archiver-worker \
    --paths "$workspace/backend" \
    --distpath "$output" \
    --workpath "$workspace/build" \
    --specpath "$workspace/build" \
    "$workspace/backend/worker_entry.py"

rm -rf "$venv"

echo "Worker binary: $output/archiver-worker"
"$output/archiver-worker" --doctor || true

