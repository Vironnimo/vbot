#!/usr/bin/env bash
# vBot uninstaller for Linux. Mirrors scripts/uninstall.ps1. Two modes, picked by
# whether this is a self-contained bootstrap install:
#   - bootstrap install (a .vbot-bootstrap marker sits next to scripts/): remove
#     the whole tree (venv + source), the 'vbot' launcher, and the autostart unit.
#   - manual/editable install (no marker): uninstall the pip package from the
#     active interpreter and optionally remove the systemd user unit.
# Either way the data dir (~/.vbot) is never touched.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MARKER="${PROJECT_ROOT}/.vbot-bootstrap"

PACKAGE_NAME="vbot"
REMOVE_AUTOSTART=0
SERVICE_NAME="vbot"

# Freedesktop application-menu entry written by scripts/install.sh (--desktop /
# --desktop-client). Kept identical here so this removes exactly what was created.
DESKTOP_ENTRY_PATH="${HOME}/.local/share/applications/vbot-desktop.desktop"

usage() {
    cat <<USAGE
Usage: scripts/uninstall.sh [options]

A bootstrap install (installed via the one-line bootstrap) is removed wholesale —
its directory, the 'vbot' launcher, and the autostart unit — regardless of the
options below. The data dir (~/.vbot) is always left untouched.

Options (manual/editable installs only):
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

# Remove the Desktop accessor's application-menu entry if present. Data-dir
# preserving like the rest of the uninstaller; a missing entry is a no-op.
remove_desktop_entry() {
    if [ -f "$DESKTOP_ENTRY_PATH" ]; then
        rm -f "$DESKTOP_ENTRY_PATH"
        echo "Removed application-menu entry ${DESKTOP_ENTRY_PATH}."
    fi
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

UNIT_FILE="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"

# --- bootstrap install: remove the whole self-contained tree ------------------

bootstrap_uninstall() {
    # Guard against ever removing something that isn't a real, marked install dir.
    case "$PROJECT_ROOT" in
        "" | "/" | "$HOME") fail "Refusing to remove '${PROJECT_ROOT}'." ;;
    esac

    step "Removing bootstrap install at ${PROJECT_ROOT}"

    if [ -f "$UNIT_FILE" ]; then
        step "Removing systemd user unit '${SERVICE_NAME}'"
        systemctl --user disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
        rm -f "$UNIT_FILE"
        systemctl --user daemon-reload 2>/dev/null || true
    fi

    # Stop any server still holding files in the venv (no-op if already stopped
    # above or never running).
    local venv_vbot="${PROJECT_ROOT}/.venv/bin/vbot"
    if [ -x "$venv_vbot" ]; then
        "$venv_vbot" server stop >/dev/null 2>&1 || true
    fi

    # Remove the ~/.local/bin/vbot launcher only if it points into this install.
    local launcher="${HOME}/.local/bin/vbot"
    if [ -L "$launcher" ]; then
        local target
        target="$(readlink -f "$launcher" 2>/dev/null || true)"
        case "$target" in
            "${PROJECT_ROOT}/"*) rm -f "$launcher"; echo "Removed launcher ${launcher}." ;;
        esac
    fi

    remove_desktop_entry

    # Removing PROJECT_ROOT deletes this running script's file; bash has already
    # read it, so this is safe. Step out of the tree first so the cwd survives.
    cd "$HOME"
    rm -rf "$PROJECT_ROOT"

    step "Uninstall complete"
    echo "Removed ${PROJECT_ROOT} (including its virtual environment)."
    echo "Data directories such as ~/.vbot were not modified."
}

# --- manual/editable install: uninstall the pip package -----------------------

manual_uninstall() {
    if command -v python3 >/dev/null 2>&1; then
        PYTHON="python3"
    elif command -v python >/dev/null 2>&1; then
        PYTHON="python"
    else
        fail "Python is required to uninstall the pip package, but neither 'python3' nor 'python' was found."
    fi

    step "Uninstalling pip package: ${PACKAGE_NAME}"
    "$PYTHON" -m pip uninstall -y "$PACKAGE_NAME"

    if [ "$REMOVE_AUTOSTART" -eq 1 ]; then
        step "Removing systemd user unit"
        if [ -f "$UNIT_FILE" ]; then
            systemctl --user disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
            rm -f "$UNIT_FILE"
            systemctl --user daemon-reload
            echo "Removed systemd user unit '${SERVICE_NAME}'."
        else
            echo "No systemd user unit named '${SERVICE_NAME}' exists. If you installed with a custom --service-name, pass the same one here."
        fi
    elif [ -f "$UNIT_FILE" ]; then
        echo "Warning: systemd user unit '${SERVICE_NAME}' still exists. Re-run with --remove-autostart to remove it." >&2
    fi

    remove_desktop_entry

    step "Uninstall complete"
    echo "Data directories such as ~/.vbot were not modified."
    echo "Source files, webui/node_modules, and webui/dist were not removed."
}

if [ -f "$MARKER" ]; then
    bootstrap_uninstall
else
    manual_uninstall
fi
