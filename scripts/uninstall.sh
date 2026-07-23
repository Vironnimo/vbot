#!/usr/bin/env bash
# vBot uninstaller for Linux. Mirrors scripts/uninstall.ps1. Managed fresh
# installs remove their complete installer-owned tree; managed installations in
# an existing checkout remove only the installer-owned venv and launcher; direct
# internal setup installs uninstall only the pip package.
# The data dir is preserved unless --remove-data is explicitly supplied.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROOT_MARKER="${PROJECT_ROOT}/.vbot-install-root"
VENV_MARKER="${PROJECT_ROOT}/.vbot-install-venv"
LEGACY_ROOT_MARKER="${PROJECT_ROOT}/.vbot-bootstrap"
INSTALL_MANIFEST="${PROJECT_ROOT}/.vbot-install.json"

PACKAGE_NAME="vbot"
REMOVE_AUTOSTART=0
SERVICE_NAME="vbot"
REMOVE_DATA=0
DATA_DIR="${HOME}/.vbot"
SERVER_HOST=""
SERVER_PORT=0

# Freedesktop application-menu entry written by scripts/setup.sh (--desktop /
# --desktop-client). Kept identical here so this removes exactly what was created.
DESKTOP_ENTRY_PATH="${HOME}/.local/share/applications/vbot-desktop.desktop"

usage() {
    cat <<USAGE
Usage: scripts/uninstall.sh [options]

A managed fresh install is removed wholesale. A managed install in an existing
checkout removes its .venv and launcher but preserves the checkout. The data dir
(~/.vbot) is preserved unless --remove-data is supplied.

Options:
  --package-name <name>  pip package to uninstall (default: vbot; direct setup only)
  --remove-autostart     Disable and remove the systemd user unit (direct setup
                         only; managed installs always remove the unit)
  --service-name <name>  systemd unit name (default: vbot; all modes — pass the
                         same name the install used)
  --remove-data          Permanently delete the selected vBot data directory
  --data-dir <path>      Exact data directory to delete (default: ~/.vbot)
  --host <host>          Server host used for the pre-removal stop
  --port <port>          Server port used for the pre-removal stop
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

resolve_path() {
    local python_path="${PROJECT_ROOT}/.venv/bin/python"
    if [ ! -x "$python_path" ]; then
        if command -v python3 >/dev/null 2>&1; then
            python_path="$(command -v python3)"
        elif command -v python >/dev/null 2>&1; then
            python_path="$(command -v python)"
        else
            fail "Python is required to validate the data-directory path."
        fi
    fi
    "$python_path" - "$1" <<'PY'
import sys
from pathlib import Path

print(Path(sys.argv[1]).expanduser().resolve())
PY
}

remove_data_directory() {
    local data_full home_full current_full
    data_full="$(resolve_path "$DATA_DIR")"
    home_full="$(resolve_path "$HOME")"
    current_full="$(resolve_path "$PWD")"
    case "$data_full" in
        "" | "/" | "$home_full" | "$PROJECT_ROOT") fail "Refusing to remove unsafe data directory '${data_full}'." ;;
    esac
    case "${PROJECT_ROOT}/" in
        "${data_full}/"*) fail "Refusing to remove data directory '${data_full}' because it contains the vBot installation." ;;
    esac
    case "${current_full}/" in
        "${data_full}/"*) fail "The current directory is inside '${data_full}'. Change directory before removing it." ;;
    esac
    if [ -e "$data_full" ] && [ ! -d "$data_full" ]; then
        fail "The data-directory path is not a directory: ${data_full}"
    fi
    if [ -d "$data_full" ]; then
        rm -rf -- "$data_full"
        echo "Removed vBot data directory ${data_full}."
    else
        echo "No vBot data directory exists at ${data_full}."
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --package-name) PACKAGE_NAME="$2"; shift 2 ;;
        --remove-autostart) REMOVE_AUTOSTART=1; shift ;;
        --service-name) SERVICE_NAME="$2"; shift 2 ;;
        --remove-data) REMOVE_DATA=1; shift ;;
        --data-dir) DATA_DIR="$2"; shift 2 ;;
        --host) SERVER_HOST="$2"; shift 2 ;;
        --port) SERVER_PORT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Unknown option: $1" ;;
    esac
done

[ "${#SERVICE_NAME}" -le 200 ] || fail "--service-name must be at most 200 characters."
case "$SERVICE_NAME" in
    "" | [!A-Za-z0-9]* | *[!A-Za-z0-9_.@-]* | *.service) fail "--service-name must start with a letter or number, then contain only letters, numbers, '.', '_', '@', or '-', without a .service suffix." ;;
esac
case "$SERVER_PORT" in
    ''|*[!0-9]*) fail "--port must be an integer between 1 and 65535." ;;
esac
if [ "$SERVER_PORT" -ne 0 ] && { [ "$SERVER_PORT" -lt 1 ] || [ "$SERVER_PORT" -gt 65535 ]; }; then
    fail "--port must be an integer between 1 and 65535."
fi

UNIT_FILE="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"
UNIT_WANTS_LINK="${HOME}/.config/systemd/user/default.target.wants/${SERVICE_NAME}.service"

remove_systemd_unit() {
    if systemctl --user disable --now "${SERVICE_NAME}.service"; then
        echo "Disabled systemd user unit '${SERVICE_NAME}'."
    else
        echo "Warning: systemctl could not disable '${SERVICE_NAME}'; removing its exact user-unit files directly." >&2
    fi
    rm -f "$UNIT_WANTS_LINK"
    rm -f "$UNIT_FILE"
    if ! systemctl --user daemon-reload; then
        echo "Warning: systemctl daemon-reload failed. The unit files are removed, but the user manager may retain stale state until the next login." >&2
    fi
}

# --- managed installs --------------------------------------------------------

resolve_manifest_python() {
    local parser="${PROJECT_ROOT}/.venv/bin/python"
    if [ ! -x "$parser" ]; then
        if command -v python3 >/dev/null 2>&1; then
            parser="$(command -v python3)"
        elif command -v python >/dev/null 2>&1; then
            parser="$(command -v python)"
        else
            return 1
        fi
    fi
    "$parser" - "$INSTALL_MANIFEST" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as manifest_file:
    state = json.load(manifest_file)
python_executable = state.get("python_executable")
if not isinstance(python_executable, str) or not python_executable.strip():
    raise SystemExit(1)
print(python_executable)
PY
}

install_owns_desktop_entry() {
    if [ -f "$INSTALL_MANIFEST" ]; then
        local parser=""
        parser="$(resolve_manifest_python 2>/dev/null || true)"
        if [ -z "$parser" ] || [ ! -x "$parser" ]; then
            if command -v python3 >/dev/null 2>&1; then
                parser="$(command -v python3)"
            elif command -v python >/dev/null 2>&1; then
                parser="$(command -v python)"
            else
                return 1
            fi
        fi
        "$parser" - "$INSTALL_MANIFEST" <<'PY' 2>/dev/null
import json
import sys

with open(sys.argv[1], encoding="utf-8") as manifest_file:
    shape = json.load(manifest_file).get("install_shape")
raise SystemExit(0 if shape in {"server-desktop", "desktop-client"} else 1)
PY
        return
    fi

    [ -x "${PROJECT_ROOT}/.venv/bin/vbot-desktop" ]
}

resolve_vbot_path() {
    local recorded_python=""
    if [ -f "$INSTALL_MANIFEST" ]; then
        recorded_python="$(resolve_manifest_python 2>/dev/null || true)"
    fi
    if [ -n "$recorded_python" ]; then
        local recorded_vbot
        recorded_vbot="$(dirname "$recorded_python")/vbot"
        if [ -x "$recorded_vbot" ]; then
            echo "$recorded_vbot"
            return
        fi
    fi

    local conventional_vbot="${PROJECT_ROOT}/.venv/bin/vbot"
    if [ -x "$conventional_vbot" ]; then
        echo "$conventional_vbot"
        return
    fi
    command -v vbot 2>/dev/null || true
}

stop_vbot_server() {
    local vbot_path
    vbot_path="$(resolve_vbot_path)"
    if [ -z "$vbot_path" ]; then
        fail "Could not locate vbot to stop the server before removing the application."
    fi
    if [ -n "$SERVER_HOST" ] && [ "$SERVER_PORT" -ne 0 ]; then
        if ! "$vbot_path" server stop --host "$SERVER_HOST" --port "$SERVER_PORT" --data-dir "$DATA_DIR" >/dev/null 2>&1; then
            fail "Could not stop the vBot server; no files were removed."
        fi
        echo "Verified that the vBot server target is stopped."
        return
    fi

    local manifest_python=""
    if [ -f "$INSTALL_MANIFEST" ]; then
        manifest_python="$(resolve_manifest_python 2>/dev/null || true)"
    fi
    if [ -x "$manifest_python" ] && [ -f "$INSTALL_MANIFEST" ]; then
        local stop_status=0
        "$manifest_python" - "$INSTALL_MANIFEST" "$vbot_path" <<'PY' >/dev/null 2>&1 || stop_status=$?
import json
import subprocess
import sys

manifest_path, vbot_path = sys.argv[1:]
arguments = [vbot_path, "server", "stop"]
try:
    with open(manifest_path, encoding="utf-8") as manifest_file:
        state = json.load(manifest_file)
    host = state.get("server_host")
    port = state.get("server_port")
    data_directory = state.get("server_data_directory")
    if host is not None and port is not None and data_directory is not None:
        arguments.extend(
            ["--host", str(host), "--port", str(port), "--data-dir", str(data_directory)]
        )
except (OSError, ValueError, TypeError):
    pass
completed = subprocess.run(
    arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
)
raise SystemExit(completed.returncode)
PY
        if [ "$stop_status" -ne 0 ]; then
            fail "Could not stop the vBot server; no files were removed."
        fi
    else
        if ! "$vbot_path" server stop --data-dir "$DATA_DIR" >/dev/null 2>&1; then
            fail "Could not stop the vBot server; no files were removed."
        fi
    fi
    echo "Verified that the vBot server target is stopped."
}

managed_cleanup() {
    local remove_desktop=0
    if install_owns_desktop_entry; then
        remove_desktop=1
    fi

    # Removing an active environment is unsafe. The stop is mandatory and runs
    # before any launcher, unit, package, or application file is removed.
    stop_vbot_server

    if [ -f "$UNIT_FILE" ] || [ -L "$UNIT_WANTS_LINK" ]; then
        step "Removing systemd user unit '${SERVICE_NAME}'"
        remove_systemd_unit
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

    if [ "$remove_desktop" -eq 1 ]; then
        remove_desktop_entry
    fi
}

managed_root_uninstall() {
    # Guard against ever removing something that isn't a real, marked install dir.
    case "$PROJECT_ROOT" in
        "" | "/" | "$HOME") fail "Refusing to remove '${PROJECT_ROOT}'." ;;
    esac

    step "Removing managed install at ${PROJECT_ROOT}"
    managed_cleanup

    # Removing PROJECT_ROOT deletes this running script's file; bash has already
    # read it, so this is safe. Step out of the tree first so the cwd survives.
    cd "$HOME"
    if [ "$REMOVE_DATA" -eq 1 ]; then
        remove_data_directory
    fi
    rm -rf "$PROJECT_ROOT"

    step "Uninstall complete"
    echo "Removed ${PROJECT_ROOT} (including its virtual environment)."
    if [ "$REMOVE_DATA" -eq 0 ]; then
        echo "Data directories such as ~/.vbot were not modified."
    fi
}

managed_venv_uninstall() {
    step "Removing managed vBot environment from ${PROJECT_ROOT}"
    managed_cleanup

    cd "$HOME"
    if [ "$REMOVE_DATA" -eq 1 ]; then
        remove_data_directory
    fi
    rm -rf "${PROJECT_ROOT}/.venv"
    rm -f "$INSTALL_MANIFEST"
    rm -f "$VENV_MARKER"

    step "Uninstall complete"
    echo "Removed the installer-managed virtual environment; preserved ${PROJECT_ROOT}."
    if [ "$REMOVE_DATA" -eq 0 ]; then
        echo "Data directories such as ~/.vbot were not modified."
    fi
}

# --- manual/editable install: uninstall the pip package -----------------------

manual_uninstall() {
    local remove_desktop=0
    if install_owns_desktop_entry; then
        remove_desktop=1
    fi

    stop_vbot_server
    if command -v python3 >/dev/null 2>&1; then
        PYTHON="python3"
    elif command -v python >/dev/null 2>&1; then
        PYTHON="python"
    else
        fail "Python is required to uninstall the pip package, but neither 'python3' nor 'python' was found."
    fi

    if [ -f "$INSTALL_MANIFEST" ]; then
        recorded_python="$(cd "$PROJECT_ROOT" && "$PYTHON" -m cli.install_state python --root "$PROJECT_ROOT" 2>/dev/null || true)"
        if [ -n "$recorded_python" ] && [ -x "$recorded_python" ]; then
            PYTHON="$recorded_python"
            echo "Using the Python interpreter recorded by the installer: ${PYTHON}"
        else
            echo "Warning: the installation manifest's Python interpreter is unavailable; falling back to PATH." >&2
        fi
    fi

    step "Uninstalling pip package: ${PACKAGE_NAME}"
    "$PYTHON" -m pip uninstall -y "$PACKAGE_NAME"
    if [ -f "$INSTALL_MANIFEST" ]; then
        rm -f "$INSTALL_MANIFEST"
        echo "Removed installation manifest."
    fi

    if [ "$REMOVE_AUTOSTART" -eq 1 ]; then
        step "Removing systemd user unit"
        if [ -f "$UNIT_FILE" ] || [ -L "$UNIT_WANTS_LINK" ]; then
            remove_systemd_unit
            echo "Removed systemd user unit '${SERVICE_NAME}'."
        else
            echo "No systemd user unit named '${SERVICE_NAME}' exists. If you installed with a custom --service-name, pass the same one here."
        fi
    elif [ -f "$UNIT_FILE" ] || [ -L "$UNIT_WANTS_LINK" ]; then
        echo "Warning: systemd user unit '${SERVICE_NAME}' still exists. Re-run with --remove-autostart to remove it." >&2
    fi

    if [ "$remove_desktop" -eq 1 ]; then
        remove_desktop_entry
    fi

    if [ "$REMOVE_DATA" -eq 1 ]; then
        cd "$HOME"
        remove_data_directory
    fi

    step "Uninstall complete"
    if [ "$REMOVE_DATA" -eq 0 ]; then
        echo "Data directories such as ~/.vbot were not modified."
    fi
    echo "Source files, webui/node_modules, and webui/dist were not removed."
}

if [ -f "$ROOT_MARKER" ] || [ -f "$LEGACY_ROOT_MARKER" ]; then
    managed_root_uninstall
elif [ -f "$VENV_MARKER" ]; then
    managed_venv_uninstall
else
    manual_uninstall
fi
