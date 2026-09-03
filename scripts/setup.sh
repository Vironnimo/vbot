#!/usr/bin/env bash
# Internal vBot checkout setup for Linux.
# Called by scripts/install.sh after it has selected a checkout and isolated
# virtual environment. Owns the editable package install, WebUI build, data-dir
# initialization without overwriting valid existing files, and optional autostart.
# Autostart uses a systemd user unit plus login lingering instead of the
# Windows Task Scheduler.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WEBUI_DIR="${PROJECT_ROOT}/webui"

DATA_DIR="${HOME}/.vbot"
HOST="127.0.0.1"
PORT=8420
PORT_PROVIDED=0
DEFAULT_AGENT_TEMPERATURE="0.1"
DEFAULT_AGENT_THINKING_EFFORT="high"
DEV=0
DESKTOP=0
DESKTOP_CLIENT=0
SKIP_WEBUI_BUILD=0
NO_AUTOSTART=0
SERVICE_NAME="vbot"

# Freedesktop application-menu entry for the Desktop accessor. Kept identical in
# scripts/uninstall.sh so the uninstaller removes exactly what this writes.
DESKTOP_ENTRY_PATH="${HOME}/.local/share/applications/vbot-desktop.desktop"

usage() {
    cat <<USAGE
Usage: scripts/setup.sh [options]

Options:
  --data-dir <path>      Data directory (default: ~/.vbot)
  --host <host>          Server host (default: 127.0.0.1)
  --port <port>          Server port (default: 8420, or existing settings.json value)
  --dev                  Install the dev dependency group instead of server+cli
  --desktop              Also install the desktop accessor and create an
                         application-menu entry that launches 'vbot desktop'
                         (added on top of the normal server install)
  --desktop-client       Install only the desktop accessor (no server stack):
                         installs .[cli,desktop], skips the WebUI build, the
                         data-dir init, and autostart, and creates the
                         application-menu entry. Use on a client machine that
                         connects to a remote vBot server.
  --no-autostart         Do not enable autostart or start the server after install
  --skip-webui-build     Use an existing webui/dist instead of building (for
                         low-memory hosts such as a Pi 3 — build elsewhere and
                         copy webui/dist over before running this)
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

# Write a freedesktop .desktop launcher for the Desktop accessor. Exec points at
# the resolved vbot command with the 'desktop' subcommand; the menu entry is the
# only shortcut created (no autostart — the Desktop is user-launched only).
write_desktop_entry() {
    local vbot_command="$1"
    local escaped_command
    escaped_command="${vbot_command//%/%%}"
    escaped_command="${escaped_command//\\/\\\\}"
    escaped_command="${escaped_command//\"/\\\"}"
    local entry_dir
    entry_dir="$(dirname "$DESKTOP_ENTRY_PATH")"
    mkdir -p "$entry_dir"
    cat > "$DESKTOP_ENTRY_PATH" <<DESKTOPEOF
[Desktop Entry]
Type=Application
Name=vBot Desktop
Comment=vBot desktop accessor
Exec="${escaped_command}" desktop
Terminal=false
Categories=Utility;
DESKTOPEOF
    echo "Created application-menu entry ${DESKTOP_ENTRY_PATH}."
}

while [ $# -gt 0 ]; do
    case "$1" in
        --data-dir) DATA_DIR="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; PORT_PROVIDED=1; shift 2 ;;
        --dev) DEV=1; shift ;;
        --desktop) DESKTOP=1; shift ;;
        --desktop-client) DESKTOP_CLIENT=1; shift ;;
        --no-autostart) NO_AUTOSTART=1; shift ;;
        --skip-webui-build) SKIP_WEBUI_BUILD=1; shift ;;
        --service-name) SERVICE_NAME="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Unknown option: $1" ;;
    esac
done

if [ "$DESKTOP" -eq 1 ] && [ "$DESKTOP_CLIENT" -eq 1 ]; then
    fail "--desktop and --desktop-client are mutually exclusive: --desktop adds the accessor to a full server install, --desktop-client installs the accessor with no server stack."
fi
if [ "$DESKTOP_CLIENT" -eq 1 ] && [ "$DEV" -eq 1 ]; then
    fail "--desktop-client and --dev are mutually exclusive: --desktop-client installs the accessor with no server stack, --dev installs the full development environment."
fi
if [ "$DESKTOP_CLIENT" -eq 0 ]; then
    [ "${#SERVICE_NAME}" -le 200 ] || fail "--service-name must be at most 200 characters."
    case "$SERVICE_NAME" in
        "" | [!A-Za-z0-9]* | *[!A-Za-z0-9_.@-]* | *.service) fail "--service-name must start with a letter or number, then contain only letters, numbers, '.', '_', '@', or '-', without a .service suffix." ;;
    esac
fi

# The desktop-client mode installs only the accessor: it owns no server data dir,
# so its normalization and creation are skipped along with the server steps below.
if [ "$DESKTOP_CLIENT" -eq 0 ]; then
    case "$DATA_DIR" in
        "~") DATA_DIR="$HOME" ;;
        "~/"*) DATA_DIR="${HOME}/${DATA_DIR#\~/}" ;;
    esac
fi

resolve_python() {
    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
    elif command -v python >/dev/null 2>&1; then
        echo "python"
    else
        fail "Python 3.11 or newer is required, but neither 'python3' nor 'python' was found."
    fi
}

PYTHON="$(resolve_python)"

step "Checking prerequisites"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || fail "Python 3.11 or newer is required; found $("$PYTHON" --version 2>&1)."

# PEP 668: Debian/Raspberry Pi OS block pip installs into the system
# interpreter. Fail early with venv instructions instead of mid-install.
if ! "$PYTHON" - <<'PYEOF'
import os
import sys
import sysconfig

in_venv = sys.prefix != sys.base_prefix
marker = os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")
sys.exit(1 if not in_venv and os.path.exists(marker) else 0)
PYEOF
then
    fail "This Python is externally managed (PEP 668). Create a venv and re-run inside it:
  ${PYTHON} -m venv ~/vbot-venv
  source ~/vbot-venv/bin/activate
  scripts/setup.sh [options]"
fi

if [ "$DESKTOP_CLIENT" -eq 0 ] && [ "$SKIP_WEBUI_BUILD" -eq 0 ]; then
    command -v node >/dev/null 2>&1 || fail "Node.js is required to build the WebUI. Install it, or build webui/dist on another machine and re-run with --skip-webui-build."
    command -v npm >/dev/null 2>&1 || fail "npm is required to build the WebUI. Install it, or build webui/dist on another machine and re-run with --skip-webui-build."
    node --version
    npm --version
fi

read_settings_port() {
    "$PYTHON" - "$1" <<'PYEOF'
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

path = Path(sys.argv[1])
if not path.exists():
    sys.exit(0)
try:
    settings = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    print("invalid settings.json", file=sys.stderr)
    sys.exit(2)
if not isinstance(settings, dict):
    print("invalid settings.json", file=sys.stderr)
    sys.exit(2)
for key in ("server_port", "SERVER_PORT", "port", "PORT"):
    if key not in settings:
        continue
    value = settings[key]
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        print(f"settings.json value '{key}' must be an integer port between 1 and 65535.", file=sys.stderr)
        sys.exit(2)
    print(value)
    sys.exit(0)
PYEOF
}

# Write an explicit --port into an existing settings.json, updating the port key
# the app actually reads (first present wins, like the server's resolver). Keeps
# the autostart entry and later flag-less commands (server status/stop) on the
# same port. Prints the updated key, or nothing when the port already matches.
sync_settings_port() {
    "$PYTHON" - "$1" "$2" <<'PYEOF'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
port = int(sys.argv[2])
lock_path = path.with_name(f".{path.name}.setup.lock")
deadline = time.monotonic() + 10.0
while True:
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(lock_fd)
        break
    except FileExistsError:
        if time.monotonic() >= deadline:
            print("settings.json is busy; another setup may be updating it", file=sys.stderr)
            sys.exit(2)
        time.sleep(0.05)
temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
try:
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("invalid settings.json", file=sys.stderr)
        sys.exit(2)
    if not isinstance(settings, dict):
        print("invalid settings.json", file=sys.stderr)
        sys.exit(2)
    keys = ("server_port", "SERVER_PORT", "port", "PORT")
    key = next((k for k in keys if k in settings), "server_port")
    if settings.get(key) == port:
        sys.exit(0)
    settings[key] = port
    mode = path.stat().st_mode & 0o777
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(settings, indent=4, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp_path, mode)
    os.replace(temp_path, path)
    if os.name == "posix":
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    print(key)
finally:
    temp_path.unlink(missing_ok=True)
    lock_path.unlink(missing_ok=True)
PYEOF
}

# Server data-dir steps (port resolution, settings.json, canonical layout): skipped for the
# desktop-client mode, which connects to a remote server and owns no local one.
if [ "$DESKTOP_CLIENT" -eq 0 ]; then
    SETTINGS_PATH="${DATA_DIR}/settings.json"
    settings_was_missing=0
    if [ ! -f "$SETTINGS_PATH" ]; then
        settings_was_missing=1
    fi

    "$PYTHON" "${PROJECT_ROOT}/core/storage/layout.py" "$DATA_DIR" \
        --resources-dir "${PROJECT_ROOT}/resources" \
        || fail "Could not initialize the canonical data-directory layout: ${DATA_DIR}"
    DATA_DIR="$(cd "$DATA_DIR" && pwd)"
    SETTINGS_PATH="${DATA_DIR}/settings.json"

    if [ "$PORT_PROVIDED" -eq 0 ]; then
        configured_port="$(read_settings_port "$SETTINGS_PATH")" \
            || fail "Existing settings.json is not usable and was not overwritten: ${SETTINGS_PATH}"
        if [ -n "$configured_port" ]; then
            PORT="$configured_port"
            echo "Using port ${PORT} from existing settings.json. Pass --port to override installer commands."
        fi
    else
        case "$PORT" in
            ''|*[!0-9]*) fail "--port must be an integer between 1 and 65535." ;;
        esac
        [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] \
            || fail "--port must be an integer between 1 and 65535."
    fi

    step "Preparing data directory: ${DATA_DIR}"
    if [ "$settings_was_missing" -eq 1 ]; then
        printf '{\n    "server_port": %s,\n    "defaults": {\n        "agent": {\n            "temperature": %s,\n            "thinking_effort": "%s"\n        }\n    }\n}\n' \
            "$PORT" "$DEFAULT_AGENT_TEMPERATURE" "$DEFAULT_AGENT_THINKING_EFFORT" > "$SETTINGS_PATH"
        echo "Created settings.json with server_port ${PORT} and fresh-install Agent defaults."
    elif [ "$PORT_PROVIDED" -eq 1 ]; then
        updated_key="$(sync_settings_port "$SETTINGS_PATH" "$PORT")" \
            || fail "Existing settings.json is not valid JSON and was not updated: ${SETTINGS_PATH}"
        if [ -n "$updated_key" ]; then
            echo "Updated ${updated_key} to ${PORT} in existing settings.json (--port)."
        else
            echo "Keeping existing settings.json (already at port ${PORT})."
        fi
    else
        # Validity was already checked while resolving the port.
        echo "Keeping existing valid settings.json."
    fi
fi

# --dev swaps the base groups; --desktop stays an add-on on top of either base,
# so a dev install with the desktop accessor gets both dependency groups.
# --desktop-client is its own accessor-only shape and excludes --dev.
if [ "$DESKTOP_CLIENT" -eq 1 ]; then
    INSTALL_GROUPS=(cli desktop)
    INSTALL_SHAPE="desktop-client"
elif [ "$DEV" -eq 1 ] && [ "$DESKTOP" -eq 1 ]; then
    INSTALL_GROUPS=(dev desktop)
    INSTALL_SHAPE="server-desktop"
elif [ "$DEV" -eq 1 ]; then
    INSTALL_GROUPS=(dev)
    INSTALL_SHAPE="server"
elif [ "$DESKTOP" -eq 1 ]; then
    INSTALL_GROUPS=(server cli desktop)
    INSTALL_SHAPE="server-desktop"
else
    INSTALL_GROUPS=(server cli)
    INSTALL_SHAPE="server"
fi
GROUP_LIST="$(IFS=,; echo "${INSTALL_GROUPS[*]}")"
EXTRA=".[${GROUP_LIST}]"
step "Installing Python package in editable mode: ${EXTRA}"
(cd "$PROJECT_ROOT" && "$PYTHON" -m pip install -e "$EXTRA")

# The desktop-client mode loads the WebUI from a remote server, so it builds no
# local WebUI bundle.
if [ "$DESKTOP_CLIENT" -eq 1 ]; then
    step "Skipping WebUI build (desktop client connects to a remote server)"
elif [ "$SKIP_WEBUI_BUILD" -eq 1 ]; then
    step "Skipping WebUI build (--skip-webui-build)"
    [ -f "${WEBUI_DIR}/dist/index.html" ] \
        || fail "webui/dist/index.html not found. Build the WebUI on another machine (cd webui && npm ci && npm run build) and copy webui/dist here, or re-run without --skip-webui-build."
    echo "Using existing webui/dist."
else
    step "Installing WebUI dependencies"
    (cd "$WEBUI_DIR" && npm ci)
    step "Building WebUI"
    (cd "$WEBUI_DIR" && npm run build)
    [ -f "${WEBUI_DIR}/dist/index.html" ] || fail "WebUI build did not create webui/dist/index.html."
fi

SCRIPTS_PATH="$("$PYTHON" -c "import sysconfig; print(sysconfig.get_path('scripts'))")"
VBOT_ON_ORIGINAL_PATH="$(command -v vbot || true)"
# Prepend so this session — including the autostart registration triggered
# below — resolves the just-installed vbot, not a stale one elsewhere on PATH.
export PATH="${SCRIPTS_PATH}:${PATH}"

# Prefer the just-installed command directly over whatever PATH resolves first.
if [ -x "${SCRIPTS_PATH}/vbot" ]; then
    VBOT_PATH="${SCRIPTS_PATH}/vbot"
else
    VBOT_PATH="$(command -v vbot || true)"
fi
if [ -z "$VBOT_PATH" ]; then
    fail "The vbot command was not found after installation. Check pip output for installation errors."
fi

if [ "$DESKTOP_CLIENT" -eq 1 ]; then
    step "Verifying vBot command"
    "$VBOT_PATH" --help >/dev/null
else
    step "Verifying vBot command and settings"
    "$VBOT_PATH" --help >/dev/null
    "$VBOT_PATH" doctor settings --data-dir "$DATA_DIR"
fi

# Preserve the selected environment entry point. Some symlink-based venvs
# report the base binary through sys.executable even though pip installs into
# the venv, which would make a later uninstall target the wrong environment.
PYTHON_EXECUTABLE="$(command -v "$PYTHON")"
step "Recording installation shape: ${INSTALL_SHAPE}"
INSTALL_STATE_ARGS=(
    --root "$PROJECT_ROOT"
    --shape "$INSTALL_SHAPE"
    --groups "${INSTALL_GROUPS[@]}"
    --python-executable "$PYTHON_EXECUTABLE"
)
if [ "$DESKTOP_CLIENT" -eq 0 ]; then
    INSTALL_STATE_ARGS+=(
        --server-host "$HOST"
        --server-port "$PORT"
        --server-data-directory "$DATA_DIR"
    )
fi
(cd "$PROJECT_ROOT" && "$PYTHON" -m cli.install_state write "${INSTALL_STATE_ARGS[@]}")

if [ -z "$VBOT_ON_ORIGINAL_PATH" ]; then
    echo "Note: ${SCRIPTS_PATH} is not on your PATH. Add it to your shell profile to use 'vbot' directly."
fi

# Application-menu entry for the Desktop accessor. Created for both the add-on
# (--desktop) and the server-less client (--desktop-client). Never autostarted.
if [ "$DESKTOP" -eq 1 ] || [ "$DESKTOP_CLIENT" -eq 1 ]; then
    step "Creating desktop application-menu entry"
    write_desktop_entry "$VBOT_PATH"
fi

# Autostart applies only to the server; the desktop client has none to start.
if [ "$DESKTOP_CLIENT" -eq 0 ] && [ "$NO_AUTOSTART" -eq 0 ]; then
    step "Enabling autostart and starting the server"
    "$VBOT_PATH" autostart enable --host "$HOST" --port "$PORT" --data-dir "$DATA_DIR" --service-name "$SERVICE_NAME" \
        || echo "Warning: enabling autostart failed (see message above)."
fi

step "Installation complete"
echo "vBot command: ${VBOT_PATH}"
if [ "$DESKTOP_CLIENT" -eq 1 ]; then
    echo "Desktop client installed (no local server)."
    echo "Launch the desktop accessor: vbot desktop"
    echo "It will prompt for the vBot server to connect to on first launch."
else
    echo "Data directory: ${DATA_DIR}"
    echo "Server URL: http://${HOST}:${PORT}"
    if [ "$NO_AUTOSTART" -eq 0 ]; then
        echo "Autostart: systemctl --user status ${SERVICE_NAME}"
    fi
    if [ "$DESKTOP" -eq 1 ]; then
        echo "Launch the desktop accessor: vbot desktop"
    fi
    echo "Try: vbot server status --host ${HOST} --port ${PORT} --data-dir \"${DATA_DIR}\""
fi
