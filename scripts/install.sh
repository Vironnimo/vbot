#!/usr/bin/env bash
# vBot installer for Linux (Raspberry Pi and other Debian-like systems).
#
# Installs prerequisites, selects a release or the current checkout, creates an
# isolated virtual environment, and hands off to the internal scripts/setup.sh.
# Release installs fetch the matching prebuilt WebUI, so the target needs no Node.
#   curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash
# Safer: download it, read it, then run it.
set -euo pipefail

REPO_OWNER="Vironnimo"
REPO_NAME="vbot"
REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"
API_BASE="https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}"

INSTALL_DIR="${VBOT_DIR:-${HOME}/vbot}"
INSTALL_DIR_PROVIDED=0
DEV=0
TAG=""
VERSION=""
SETUP_ARGS=()
DESKTOP=0
DESKTOP_CLIENT=0
SKIP_WEBUI_BUILD=0
NO_AUTOSTART=0
SERVICE_NAME="vbot"
VERBOSE=0
USE_EXISTING_CHECKOUT=0
LOCAL_CHECKOUT=""
WEBUI_ASSET_URL=""
ASSET_WAIT_SECONDS=300
ASSET_POLL_SECONDS=10
ROOT_MARKER=".vbot-install-root"
VENV_MARKER=".vbot-install-venv"
LEGACY_ROOT_MARKER=".vbot-bootstrap"

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
if [ -n "$SCRIPT_SOURCE" ] && [ -f "$SCRIPT_SOURCE" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
    CANDIDATE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
    if [ -f "${CANDIDATE_ROOT}/pyproject.toml" ] && [ -f "${CANDIDATE_ROOT}/scripts/setup.sh" ]; then
        LOCAL_CHECKOUT="$CANDIDATE_ROOT"
    fi
fi

usage() {
    cat <<USAGE
Usage: install.sh [options]

Options:
  --dir <path>          Installation directory (default: ~/vbot or \$VBOT_DIR;
                        when run from a checkout, that checkout is the default)
  --version <tag>       Install a specific release (for example v0.1.2)
  --dev                 Fresh install: track main; current checkout: add development dependencies
  --data-dir <path>     Runtime data directory (default: ~/.vbot)
  --host <host>         Server bind host (default: 127.0.0.1)
  --port <port>         Server port (default: 8420 or existing settings.json value)
  --desktop             Add the Desktop accessor to the server install
  --desktop-client      Install only CLI and Desktop for a remote-server client
  --no-autostart        Do not enable autostart or start the server
  --skip-webui-build    Reuse an existing webui/dist (automatic for releases)
  --service-name <name> systemd user unit name (default: vbot)
  --verbose             Show technical setup output instead of logging it only
  -h, --help            Show this help
USAGE
}

step() { echo "==> $1"; }
INSTALL_LOG=""
PRESERVE_INSTALL_LOG=0
FAILURE_REPORTED=0

fail() {
    local message="$1"
    echo "Error: ${message}" >&2
    if [ -n "$INSTALL_LOG" ]; then
        printf 'Error: %s\n' "$message" >> "$INSTALL_LOG"
        PRESERVE_INSTALL_LOG=1
        FAILURE_REPORTED=1
        echo "Technical details: ${INSTALL_LOG}" >&2
    fi
    exit 1
}

finish_install() {
    local status="$1"
    if [ "$status" -ne 0 ] && [ "$FAILURE_REPORTED" -eq 0 ] && [ -n "$INSTALL_LOG" ]; then
        PRESERVE_INSTALL_LOG=1
        echo "vBot installation failed." >&2
        echo "Technical details: ${INSTALL_LOG}" >&2
    fi
    if [ "$status" -eq 0 ] && [ "$PRESERVE_INSTALL_LOG" -eq 0 ] && [ -n "$INSTALL_LOG" ]; then
        rm -f "$INSTALL_LOG"
    fi
}

start_install_log() {
    INSTALL_LOG="$(mktemp "${TMPDIR:-/tmp}/vbot-install.XXXXXX.log")"
    printf 'vBot installation log\n' > "$INSTALL_LOG"
    trap 'finish_install "$?"' EXIT
}

have() { command -v "$1" >/dev/null 2>&1; }
require_value() {
    [ "$#" -ge 2 ] && [ -n "$2" ] || fail "$1 requires a value."
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) require_value "$@"; INSTALL_DIR="$2"; INSTALL_DIR_PROVIDED=1; shift 2 ;;
        --dev) DEV=1; shift ;;
        --version) require_value "$@"; VERSION="$2"; shift 2 ;;
        --data-dir|--host|--port)
            require_value "$@"
            SETUP_ARGS+=("$1" "$2")
            shift 2
            ;;
        --service-name)
            require_value "$@"
            SERVICE_NAME="$2"
            SETUP_ARGS+=("$1" "$2")
            shift 2
            ;;
        --desktop) DESKTOP=1; SETUP_ARGS+=("$1"); shift ;;
        --desktop-client) DESKTOP_CLIENT=1; SETUP_ARGS+=("$1"); shift ;;
        --no-autostart) NO_AUTOSTART=1; SETUP_ARGS+=("$1"); shift ;;
        --skip-webui-build) SKIP_WEBUI_BUILD=1; SETUP_ARGS+=("$1"); shift ;;
        --verbose) VERBOSE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Unknown option: $1" ;;
    esac
done

if [ "$DESKTOP" -eq 1 ] && [ "$DESKTOP_CLIENT" -eq 1 ]; then
    fail "--desktop and --desktop-client are mutually exclusive."
fi
if [ "$DESKTOP_CLIENT" -eq 1 ] && [ "$DEV" -eq 1 ]; then
    fail "--desktop-client and --dev are mutually exclusive."
fi
if [ "$DEV" -eq 1 ] && [ -n "$VERSION" ]; then
    fail "--version selects a specific release tag and cannot be combined with --dev."
fi

if [ -n "$LOCAL_CHECKOUT" ] && [ "$INSTALL_DIR_PROVIDED" -eq 0 ] && [ -z "$VERSION" ]; then
    INSTALL_DIR="$LOCAL_CHECKOUT"
    USE_EXISTING_CHECKOUT=1
fi

case "$INSTALL_DIR" in
    "~") INSTALL_DIR="$HOME" ;;
    "~/"*) INSTALL_DIR="${HOME}/${INSTALL_DIR#\~/}" ;;
esac
case "$INSTALL_DIR" in
    /*) ;;
    *) INSTALL_DIR="$(pwd)/${INSTALL_DIR}" ;;
esac
if [ "$USE_EXISTING_CHECKOUT" -eq 1 ]; then
    [ -f "${INSTALL_DIR}/pyproject.toml" ] && [ -f "${INSTALL_DIR}/scripts/setup.sh" ] \
        || fail "The current checkout is incomplete: ${INSTALL_DIR}."
else
    [ -e "$INSTALL_DIR" ] && fail "$INSTALL_DIR already exists. To update an existing install run 'vbot update'; otherwise remove it or pass --dir to choose another location."
fi
# Accept a bare version (0.1.2) as well as the tag form (v0.1.2).
case "$VERSION" in
    "" | v*) ;;
    *) VERSION="v${VERSION}" ;;
esac

start_install_log
step "Checking system requirements"

# --- prerequisites -----------------------------------------------------------

SUDO=""
if [ "$(id -u)" -ne 0 ] && have sudo; then
    SUDO="sudo"
fi

apt_install() {
    have apt-get || fail "No supported package manager (apt) found. Install these manually and re-run: $*"
    if [ "$(id -u)" -ne 0 ] && [ -z "$SUDO" ]; then
        fail "Root (or sudo) is required to install: $*. Install them manually and re-run."
    fi
    step "Installing via apt: $*"
    $SUDO apt-get update -y
    $SUDO apt-get install -y "$@"
}

ensure_git() {
    have git && return
    apt_install git
    have git || fail "git installation did not put git on PATH."
}

ensure_curl() {
    have curl && return
    apt_install curl
    have curl || fail "curl installation did not put curl on PATH."
}

ensure_python() {
    if have python3 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        return
    fi
    apt_install python3 python3-venv python3-pip
    have python3 || fail "Python installation did not put python3 on PATH."
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
        || fail "Installed Python is older than 3.11; install a newer Python and re-run."
}

ensure_venv_support() {
    if python3 -c 'import ensurepip, venv' >/dev/null 2>&1; then
        return
    fi
    apt_install python3-venv python3-pip
    python3 -c 'import ensurepip, venv' >/dev/null 2>&1 \
        || fail "Python is available, but its venv/ensurepip support is missing. Install the venv package matching $(python3 --version) and re-run."
}

ensure_node() {
    { have node && have npm; } && return
    apt_install nodejs npm
    { have node && have npm; } || fail "Node.js installation did not put node/npm on PATH."
}

# --- code --------------------------------------------------------------------

latest_release_tag() {
    curl -fsSL "${API_BASE}/releases/latest" \
        | grep -m1 '"tag_name"' \
        | sed -E 's/.*"tag_name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/'
}

clone_repo() {
    [ -e "$INSTALL_DIR" ] && fail "$INSTALL_DIR already exists. To update an existing install run 'vbot update'; otherwise remove it or pass --dir to choose another location."
    if [ "$DEV" -eq 1 ]; then
        step "Downloading vBot from main"
        git clone --quiet --depth 1 "$REPO_URL" "$INSTALL_DIR" >> "$INSTALL_LOG" 2>&1 \
            || fail "Could not download vBot from main."
    else
        step "Downloading vBot ${TAG}"
        git clone --quiet --depth 1 --branch "$TAG" "$REPO_URL" "$INSTALL_DIR" >> "$INSTALL_LOG" 2>&1 \
            || fail "Could not download vBot ${TAG}."
    fi
    INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd)"
}

release_asset_url() {
    curl -fsSL "${API_BASE}/releases/tags/${TAG}" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
for asset in payload.get("assets", []):
    if asset.get("name") == "webui-dist.tar.gz":
        print(asset.get("browser_download_url", ""))
        break
'
}

wait_for_webui_asset() {
    step "Preparing vBot ${TAG}"
    local waited=0
    while [ "$waited" -lt "$ASSET_WAIT_SECONDS" ]; do
        if ! WEBUI_ASSET_URL="$(release_asset_url 2>/dev/null)"; then
            fail "Could not query release ${TAG} while waiting for its WebUI asset."
        fi
        [ -n "$WEBUI_ASSET_URL" ] && return
        sleep "$ASSET_POLL_SECONDS"
        waited=$((waited + ASSET_POLL_SECONDS))
    done
    fail "Release ${TAG} still has no webui-dist.tar.gz asset after ${ASSET_WAIT_SECONDS} seconds. The install directory was not created; re-run once the release workflow finishes."
}

fetch_prebuilt_webui() {
    [ -n "$WEBUI_ASSET_URL" ] || fail "No preflighted WebUI asset URL is available."
    mkdir -p "${INSTALL_DIR}/webui"
    local archive="${INSTALL_DIR}/webui-dist.tar.gz"
    curl -fsSL "$WEBUI_ASSET_URL" -o "$archive" >> "$INSTALL_LOG" 2>&1 \
        || fail "Could not download the WebUI for ${TAG}."
    if ! python3 - "$archive" "${INSTALL_DIR}/webui" <<'PY'
import sys
import tarfile
from pathlib import Path

archive_path = Path(sys.argv[1])
destination = Path(sys.argv[2])
root = destination.resolve()
with tarfile.open(archive_path, mode='r:gz') as archive:
    for member in archive.getmembers():
        if not (member.isdir() or member.isfile()):
            raise SystemExit(f'unsafe member type in WebUI archive: {member.name}')
        target = (destination / member.name).resolve()
        if not target.is_relative_to(root):
            raise SystemExit(f'unsafe path in WebUI archive: {member.name}')
    archive.extractall(destination)
PY
    then
        rm -f "$archive"
        fail "Refusing to unpack the WebUI archive: it contains an unsafe path or member type."
    fi
    rm -f "$archive"
    [ -f "${INSTALL_DIR}/webui/dist/index.html" ] || fail "Prebuilt WebUI did not unpack to webui/dist."
}

run_setup() {
    step "Installing and configuring vBot"
    python3 -m venv "${INSTALL_DIR}/.venv" >> "$INSTALL_LOG" 2>&1 \
        || fail "Could not create vBot's private Python environment."
    # shellcheck disable=SC1091
    . "${INSTALL_DIR}/.venv/bin/activate"

    local args=()
    if [ "$DEV" -eq 1 ]; then
        args+=(--dev)
    elif [ "$USE_EXISTING_CHECKOUT" -eq 0 ]; then
        args+=(--skip-webui-build)
    fi
    [ "${#SETUP_ARGS[@]}" -gt 0 ] && args+=("${SETUP_ARGS[@]}")

    local setup_script="${INSTALL_DIR}/scripts/setup.sh"
    if [ ! -f "$setup_script" ]; then
        local legacy_setup="${INSTALL_DIR}/scripts/install.sh"
        if [ "$USE_EXISTING_CHECKOUT" -eq 0 ] && [ -f "$legacy_setup" ] \
            && grep -q 'editable pip install' "$legacy_setup"; then
            setup_script="$legacy_setup"
            step "Using the checkout installer contract from release ${TAG}"
        else
            fail "The selected checkout has no usable internal setup script."
        fi
    fi

    local setup_exit=0
    if [ "$VERBOSE" -eq 1 ]; then
        set +e
        if [ "${#args[@]}" -gt 0 ]; then
            bash "$setup_script" "${args[@]}" 2>&1 | tee -a "$INSTALL_LOG"
        else
            bash "$setup_script" 2>&1 | tee -a "$INSTALL_LOG"
        fi
        setup_exit=${PIPESTATUS[0]}
        set -e
    else
        set +e
        if [ "${#args[@]}" -gt 0 ]; then
            bash "$setup_script" "${args[@]}" >> "$INSTALL_LOG" 2>&1
        else
            bash "$setup_script" >> "$INSTALL_LOG" 2>&1
        fi
        setup_exit=$?
        set -e
    fi
    [ "$setup_exit" -eq 0 ] || fail "vBot setup did not complete successfully."
}

link_vbot() {
    local target="${INSTALL_DIR}/.venv/bin/vbot"
    [ -x "$target" ] || return 0
    mkdir -p "${HOME}/.local/bin"
    ln -sf "$target" "${HOME}/.local/bin/vbot"
}

# A fresh installer-owned clone can be removed wholesale by uninstall.sh.
write_root_marker() {
    cat > "${INSTALL_DIR}/${ROOT_MARKER}" <<'MARKER'
# vBot managed install marker.
# This directory is a self-contained vBot install created by scripts/install.sh
# (it has its own virtual environment in .venv). Running scripts/uninstall.sh
# (uninstall.ps1 on Windows) removes this entire directory, the 'vbot' launcher,
# and the autostart entry. Your data directory is never touched.
MARKER

    # Releases published before install.sh became the public entrypoint contain
    # an uninstaller that recognizes only the retired marker. Write it only for
    # those checkouts so installing an older explicit/latest tag still uninstalls
    # completely with the Uninstaller bundled in that tag.
    if ! grep -q '\.vbot-install-root' "${INSTALL_DIR}/scripts/uninstall.sh" 2>/dev/null; then
        cat > "${INSTALL_DIR}/${LEGACY_ROOT_MARKER}" <<'MARKER'
# Compatibility marker for a vBot release with the previous Uninstaller contract.
MARKER
    fi
}

# An existing user checkout is never deleted; only its installer-owned .venv and
# launcher are removed during uninstall.
write_venv_marker() {
    cat > "${INSTALL_DIR}/${VENV_MARKER}" <<'MARKER'
# vBot managed virtual-environment marker.
# scripts/install.sh created .venv in this existing checkout. Uninstall removes
# the managed environment and launcher but preserves the checkout and data.
MARKER
}

manifest_field() {
    local field="$1"
    "${INSTALL_DIR}/.venv/bin/python" - "${INSTALL_DIR}/.vbot-install.json" "$field" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = manifest.get(sys.argv[2])
if value is None:
    raise SystemExit(f"installation manifest has no {sys.argv[2]}")
print(value)
PY
}

CAPTURED_OUTPUT=""
CAPTURED_STATUS=0

capture_command() {
    set +e
    CAPTURED_OUTPUT="$("$@" 2>&1)"
    CAPTURED_STATUS=$?
    set -e
    if [ -n "$CAPTURED_OUTPUT" ]; then
        printf '%s\n' "$CAPTURED_OUTPUT" >> "$INSTALL_LOG"
        if [ "$VERBOSE" -eq 1 ]; then
            printf '%s\n' "$CAPTURED_OUTPUT"
        fi
    fi
}

finish_with_summary() {
    local vbot_path="${INSTALL_DIR}/.venv/bin/vbot"
    local vbot_command="$vbot_path"
    case ":${PATH}:" in
        *":${HOME}/.local/bin:"*) vbot_command="vbot" ;;
    esac

    step "Verifying the installation"
    if [ "$DESKTOP_CLIENT" -eq 1 ]; then
        echo
        echo "vBot is ready."
        echo "Open vBot Desktop from the application menu, or run: ${vbot_command} desktop"
        return
    fi

    local summary_host summary_port summary_data_dir
    summary_host="$(manifest_field server_host)" || fail "Could not read the installed server address."
    summary_port="$(manifest_field server_port)" || fail "Could not read the installed server port."
    summary_data_dir="$(manifest_field server_data_directory)" || fail "Could not read the installed data directory."

    capture_command "$vbot_path" server status \
        --host "$summary_host" --port "$summary_port" --data-dir "$summary_data_dir"
    local server_status=$CAPTURED_STATUS
    local server_output="$CAPTURED_OUTPUT"
    local server_running=0
    local port_conflict=0
    grep -q '^running: yes[[:space:]]*$' <<< "$server_output" && server_running=1
    grep -q '^conflict: port occupied by non-vBot process[[:space:]]*$' <<< "$server_output" && port_conflict=1

    local autostart_enabled=0
    local autostart_known=1
    if [ "$NO_AUTOSTART" -eq 0 ]; then
        capture_command "$vbot_path" autostart status \
            --host "$summary_host" --port "$summary_port" --data-dir "$summary_data_dir" \
            --service-name "$SERVICE_NAME"
        [ "$CAPTURED_STATUS" -eq 0 ] || autostart_known=0
        grep -q '^autostart: enabled\b' <<< "$CAPTURED_OUTPUT" && autostart_enabled=1
    fi

    echo
    if [ "$server_running" -eq 1 ] && { [ "$NO_AUTOSTART" -eq 1 ] || [ "$autostart_enabled" -eq 1 ]; }; then
        echo "vBot is ready."
        echo "Open: http://${summary_host}:${summary_port}/"
        [ "$DESKTOP" -eq 0 ] || echo "Desktop: ${vbot_command} desktop"
        return
    fi

    if [ "$NO_AUTOSTART" -eq 1 ] && [ "$server_running" -eq 0 ]; then
        echo "vBot is installed."
        echo "Autostart was not requested, so the server was not started."
        echo "Start it with: ${vbot_command} server start --host ${summary_host} --port ${summary_port} --data-dir \"${summary_data_dir}\""
        return
    fi

    PRESERVE_INSTALL_LOG=1
    echo "vBot was installed, but it needs attention."
    if [ "$server_running" -eq 1 ]; then
        echo "The server is running, but Autostart is not enabled."
        echo "Open: http://${summary_host}:${summary_port}/"
    elif [ "$port_conflict" -eq 1 ]; then
        echo "The server could not start because port ${summary_port} is used by another process."
    elif [ "$server_status" -ne 0 ]; then
        echo "The final server check did not complete successfully."
    else
        echo "The server is not running yet."
    fi
    if [ "$autostart_known" -eq 0 ]; then
        echo "Autostart status could not be verified."
    elif [ "$autostart_enabled" -eq 0 ]; then
        echo "Enable Autostart and start vBot with:"
        echo "  ${vbot_command} autostart enable --host ${summary_host} --port ${summary_port} --data-dir \"${summary_data_dir}\" --service-name ${SERVICE_NAME}"
    fi
    echo "Technical details: ${INSTALL_LOG}"
}

[ "$USE_EXISTING_CHECKOUT" -eq 0 ] && ensure_git
ensure_python
ensure_venv_support
if [ "$USE_EXISTING_CHECKOUT" -eq 0 ] && [ "$DEV" -eq 0 ]; then
    ensure_curl
fi
if [ "$DESKTOP_CLIENT" -eq 0 ] && [ "$SKIP_WEBUI_BUILD" -eq 0 ] \
    && { [ "$DEV" -eq 1 ] || [ "$USE_EXISTING_CHECKOUT" -eq 1 ]; }; then
    ensure_node
fi

if [ "$USE_EXISTING_CHECKOUT" -eq 0 ] && [ "$DEV" -eq 0 ]; then
    if [ -n "$VERSION" ]; then
        TAG="$VERSION"
    else
        TAG="$(latest_release_tag || true)"
        [ -n "$TAG" ] || fail "Could not determine the latest release. Use --dev to install from main."
    fi
    if [ "$DESKTOP_CLIENT" -eq 0 ]; then
        wait_for_webui_asset
    fi
fi

if [ "$USE_EXISTING_CHECKOUT" -eq 0 ]; then
    clone_repo
    write_root_marker
elif [ ! -f "${INSTALL_DIR}/${ROOT_MARKER}" ] && [ ! -f "${INSTALL_DIR}/${LEGACY_ROOT_MARKER}" ]; then
    write_venv_marker
fi
if [ "$USE_EXISTING_CHECKOUT" -eq 0 ] && [ "$DEV" -eq 0 ] && [ "$DESKTOP_CLIENT" -eq 0 ]; then
    fetch_prebuilt_webui
fi
run_setup
link_vbot

finish_with_summary
