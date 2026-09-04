#!/usr/bin/env bash
# Install the CHZZK Archive encoder worker as a systemd service.
#
# Usage:
#   sudo ./scripts/install-worker-systemd.sh \
#       --server https://archive.example --token SECRET
set -euo pipefail

server=""
token=""
worker_bin=""
service_user="chzzk-worker"
prefix="/opt/chzzk-archiver-worker"
unit_name="archiver-worker"
encoder="auto"

usage() {
    cat >&2 <<'USAGE'
Usage: install-worker-systemd.sh --server URL --token TOKEN [options]

  --server URL     Controller base URL (required)
  --token TOKEN    Shared worker token (required)
  --binary PATH    Worker binary (default: dist/worker/archiver-worker)
  --user NAME      Service account to create/use (default: chzzk-worker)
  --prefix PATH    Install directory (default: /opt/chzzk-archiver-worker)
  --encoder NAME   auto, hevc_nvenc, hevc_qsv, hevc_amf, hevc_vaapi, or libx265
USAGE
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --server) server="${2:-}"; shift 2 ;;
        --token) token="${2:-}"; shift 2 ;;
        --binary) worker_bin="${2:-}"; shift 2 ;;
        --user) service_user="${2:-}"; shift 2 ;;
        --prefix) prefix="${2:-}"; shift 2 ;;
        --encoder) encoder="${2:-}"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "error: unknown option '$1'" >&2; usage ;;
    esac
done

[[ -n "$server" && -n "$token" ]] || usage
case "$encoder" in
    auto|hevc_nvenc|hevc_qsv|hevc_amf|hevc_vaapi|libx265) ;;
    *) echo "error: unsupported encoder '$encoder'" >&2; usage ;;
esac

if [[ "$(id -u)" -ne 0 ]]; then
    echo "error: run with sudo" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "error: systemd is required" >&2
    exit 1
fi

workspace="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${worker_bin:=$workspace/dist/worker/archiver-worker}"

if [[ ! -x "$worker_bin" ]]; then
    echo "error: worker binary not found at '$worker_bin'" >&2
    echo "       build it first: ./scripts/build-worker.sh" >&2
    exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "warning: ffmpeg is not on PATH; the worker cannot encode without it" >&2
fi

if ! id "$service_user" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$service_user"
    echo "Created service account '$service_user'"
fi
# GPU encoders need these groups; missing groups are not fatal.
for group in video render; do
    getent group "$group" >/dev/null 2>&1 && usermod -aG "$group" "$service_user" || true
done

install -d -m 0755 "$prefix"
install -m 0755 "$worker_bin" "$prefix/archiver-worker"

env_file="$prefix/worker.env"
umask 077
cat >"$env_file" <<EOF
ARCHIVER_WORKER_SERVER=$server
ARCHIVER_WORKER_TOKEN=$token
ARCHIVER_ENCODING_VIDEO_ENCODER=$encoder
EOF
chown root:"$service_user" "$env_file"
chmod 0640 "$env_file"

unit_path="/etc/systemd/system/$unit_name.service"
sed \
    -e "s|__WORKER_USER__|$service_user|g" \
    -e "s|__WORKER_BIN__|$prefix/archiver-worker|g" \
    -e "s|__WORKER_ENV__|$env_file|g" \
    "$workspace/deploy/linux/archiver-worker.service" >"$unit_path"
chmod 0644 "$unit_path"

systemctl daemon-reload
systemctl enable --now "$unit_name.service"

echo "Installed: $unit_name.service"
echo "  binary : $prefix/archiver-worker"
echo "  config : $env_file (root:$service_user 0640)"
echo "  status : systemctl status $unit_name"
echo "  logs   : journalctl -u $unit_name -f"
echo "  encoder: $encoder"
echo "Remove with: sudo ./scripts/uninstall-worker-systemd.sh"
