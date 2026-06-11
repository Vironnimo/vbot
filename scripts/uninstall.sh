#!/usr/bin/env bash
# vBot uninstaller for Linux. Mirrors scripts/uninstall.ps1: removes the pip
# package and optionally the systemd user unit; never touches the data dir.
set -euo pipefail

PACKAGE_NAME="vbot"
REMOVE_AUTOSTART=0
SERVICE_NAME="vbot"

usage() {
    cat <<USAGE
Usage: scripts/uninstall.sh [options]

Options:
  --package-name <name>  pip package to uninstall (default: vbot)
  --remove-autostart     Disable and remove the systemd user unit
  --service-name <name>  systemd unit name (default: vbot)
  -h, --help             Show this help
USAGE
}

step() {
    echo "==> $1"
}

fail() {
    echo "Error: $1" >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --package-name) PACKAGE_NAME="$2"; shift 2 ;;
        --remove-autostart) REMOVE_AUTOSTART=1; shift ;;
        --service-name) SERVICE_NAME="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Unknown option: $1" ;;
    esac
done

if command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON="python"
else
    fail "Python is required to uninstall the pip package, but neither 'python3' nor 'python' was found."
fi

step "Uninstalling pip package: ${PACKAGE_NAME}"
"$PYTHON" -m pip uninstall -y "$PACKAGE_NAME"

UNIT_FILE="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"
if [ "$REMOVE_AUTOSTART" -eq 1 ]; then
    step "Removing systemd user unit"
    if [ -f "$UNIT_FILE" ]; then
        systemctl --user disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
        rm -f "$UNIT_FILE"
        systemctl --user daemon-reload
        echo "Removed systemd user unit '${SERVICE_NAME}'."
    else
        echo "No systemd user unit named '${SERVICE_NAME}' exists."
    fi
elif [ -f "$UNIT_FILE" ]; then
    echo "Warning: systemd user unit '${SERVICE_NAME}' still exists. Re-run with --remove-autostart to remove it." >&2
fi

step "Uninstall complete"
echo "Data directories such as ~/.vbot were not modified."
echo "Source files, webui/node_modules, and webui/dist were not removed."
