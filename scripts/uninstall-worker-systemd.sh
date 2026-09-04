#!/usr/bin/env bash
# Remove the systemd-installed CHZZK Archive encoder worker.
#
# Only the worker installation is removed; recorded media is never touched.
set -euo pipefail

prefix="/opt/chzzk-archiver-worker"
unit_name="archiver-worker"
service_user=""
remove_user=false

usage() {
    cat >&2 <<'USAGE'
Usage: uninstall-worker-systemd.sh [options]

  --prefix PATH    Install directory (default: /opt/chzzk-archiver-worker)
  --user NAME      Also delete this service account (implies removal)
USAGE
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix) prefix="${2:-}"; shift 2 ;;
        --user) service_user="${2:-}"; remove_user=true; shift 2 ;;
        -h|--help) usage ;;
        *) echo "error: unknown option '$1'" >&2; usage ;;
    esac
done

if [[ "$(id -u)" -ne 0 ]]; then
    echo "error: run with sudo" >&2
    exit 1
fi

if systemctl list-unit-files | grep -q "^$unit_name.service"; then
    systemctl disable --now "$unit_name.service" || true
    rm -f "/etc/systemd/system/$unit_name.service"
    systemctl daemon-reload
    echo "Removed $unit_name.service"
else
    echo "No $unit_name.service unit installed."
fi

# Refuse to delete anything that is not clearly a worker install directory.
case "$prefix" in
    /|/usr|/etc|/opt|/home|/var|/root)
        echo "error: refusing to delete '$prefix'" >&2
        exit 1
        ;;
esac

if [[ -d "$prefix" && -f "$prefix/archiver-worker" ]]; then
    rm -rf "$prefix"
    echo "Deleted $prefix"
elif [[ -d "$prefix" ]]; then
    echo "warning: '$prefix' does not look like a worker install; left in place" >&2
fi

if [[ "$remove_user" == true && -n "$service_user" ]]; then
    if id "$service_user" >/dev/null 2>&1; then
        userdel "$service_user"
        echo "Deleted service account '$service_user'"
    fi
fi

