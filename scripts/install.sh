#!/usr/bin/env bash
# vBot installer for Linux (Raspberry Pi and other Debian-like systems).
# Mirrors scripts/install.ps1: editable pip install, WebUI build, data-dir
# bootstrap without overwriting valid existing files, optional autostart.
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
DEV=0
SKIP_WEBUI_BUILD=0
NO_AUTOSTART=0
SERVICE_NAME="vbot"

usage() {
    cat <<USAGE
Usage: scripts/install.sh [options]

Options:
  --data-dir <path>      Data directory (default: ~/.vbot)
  --host <host>          Server host (default: 127.0.0.1)
  --port <port>          Server port (default: 8420, or existing settings.json value)
  --dev                  Install the dev dependency group instead of server+cli
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

while [ $# -gt 0 ]; do
    case "$1" in
        --data-dir) DATA_DIR="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; PORT_PROVIDED=1; shift 2 ;;
        --dev) DEV=1; shift ;;
        --no-autostart) NO_AUTOSTART=1; shift ;;
        --skip-webui-build) SKIP_WEBUI_BUILD=1; shift ;;
        --service-name) SERVICE_NAME="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Unknown option: $1" ;;
    esac
done

case "$DATA_DIR" in
    "~") DATA_DIR="$HOME" ;;
    "~/"*) DATA_DIR="${HOME}/${DATA_DIR#\~/}" ;;
esac
mkdir -p "$DATA_DIR"
DATA_DIR="$(cd "$DATA_DIR" && pwd)"

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
  scripts/install.sh [options]"
fi

if [ "$SKIP_WEBUI_BUILD" -eq 0 ]; then
    command -v node >/dev/null 2>&1 || fail "Node.js is required to build the WebUI. Install it, or build webui/dist on another machine and re-run with --skip-webui-build."
    command -v npm >/dev/null 2>&1 || fail "npm is required to build the WebUI. Install it, or build webui/dist on another machine and re-run with --skip-webui-build."
    node --version
    npm --version
fi

read_settings_port() {
    "$PYTHON" - "$1" <<'PYEOF'
import json
import sys
from pathlib import Path

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
if [ ! -f "$SETTINGS_PATH" ]; then
    printf '{\n    "server_port": %s\n}\n' "$PORT" > "$SETTINGS_PATH"
    echo "Created settings.json with server_port ${PORT}."
else
    # Validity was already checked while resolving the port.
    echo "Keeping existing valid settings.json."
fi

ENV_PATH="${DATA_DIR}/.env"
if [ ! -f "$ENV_PATH" ]; then
    cat > "$ENV_PATH" <<'ENVEOF'
# vBot provider credentials
# OPENAI_API_KEY=...
# OPENROUTER_API_KEY=...
# ANTHROPIC_API_KEY=...
ENVEOF
    echo "Created .env template."
else
    echo "Keeping existing .env."
fi

EXTRA=".[server,cli]"
if [ "$DEV" -eq 1 ]; then
    EXTRA=".[dev]"
fi
step "Installing Python package in editable mode: ${EXTRA}"
(cd "$PROJECT_ROOT" && "$PYTHON" -m pip install -e "$EXTRA")

if [ "$SKIP_WEBUI_BUILD" -eq 1 ]; then
    step "Skipping WebUI build (--skip-webui-build)"
    [ -f "${WEBUI_DIR}/dist/index.html" ] \
        || fail "webui/dist/index.html not found. Build the WebUI on another machine (cd webui && npm install && npm run build) and copy webui/dist here, or re-run without --skip-webui-build."
    echo "Using existing webui/dist."
else
    step "Installing WebUI dependencies"
    (cd "$WEBUI_DIR" && npm install)
    step "Building WebUI"
    (cd "$WEBUI_DIR" && npm run build)
    [ -f "${WEBUI_DIR}/dist/index.html" ] || fail "WebUI build did not create webui/dist/index.html."
fi

SCRIPTS_PATH="$("$PYTHON" -c "import sysconfig; print(sysconfig.get_path('scripts'))")"
VBOT_ON_ORIGINAL_PATH="$(command -v vbot || true)"
export PATH="${PATH}:${SCRIPTS_PATH}"

VBOT_PATH="$(command -v vbot || true)"
if [ -z "$VBOT_PATH" ]; then
    fail "The vbot command was not found after installation. Check pip output for installation errors."
fi

step "Verifying vBot command and settings"
"$VBOT_PATH" --help >/dev/null
"$VBOT_PATH" doctor settings --data-dir "$DATA_DIR"

if [ -z "$VBOT_ON_ORIGINAL_PATH" ]; then
    echo "Note: ${SCRIPTS_PATH} is not on your PATH. Add it to your shell profile to use 'vbot' directly."
fi

if [ "$NO_AUTOSTART" -eq 0 ]; then
    step "Enabling autostart and starting the server"
    "$VBOT_PATH" autostart enable --host "$HOST" --port "$PORT" --data-dir "$DATA_DIR" --service-name "$SERVICE_NAME" \
        || echo "Warning: enabling autostart failed (see message above)."
fi

step "Installation complete"
echo "vBot command: ${VBOT_PATH}"
echo "Data directory: ${DATA_DIR}"
echo "Server URL: http://${HOST}:${PORT}"
if [ "$NO_AUTOSTART" -eq 0 ]; then
    echo "Autostart: systemctl --user status ${SERVICE_NAME}"
fi
echo "Try: vbot server status --host ${HOST} --port ${PORT} --data-dir \"${DATA_DIR}\""
