#!/usr/bin/env bash
# vBot one-shot bootstrap for Linux (Raspberry Pi and other Debian-like systems).
#
# Installs prerequisites (Python + git; Node only on the dev track), clones the
# repo, and hands off to scripts/install.sh. On the default release track it
# fetches the prebuilt WebUI from the matching GitHub release, so the target
# needs no Node. Run it with:
#   curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/bootstrap.sh | bash
# Safer: download it, read it, then run it.
set -euo pipefail

REPO_OWNER="Vironnimo"
REPO_NAME="vbot"
REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"
API_BASE="https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}"

INSTALL_DIR="${VBOT_DIR:-${HOME}/vbot}"
DEV=0
TAG=""
VERSION=""
INSTALLER_ARGS=()
WEBUI_ASSET_URL=""
ASSET_WAIT_SECONDS=300
ASSET_POLL_SECONDS=10

usage() {
    cat <<USAGE
Usage: bootstrap.sh [options] [-- <installer options>]

Options:
  --dir <path>     Where to clone vBot (default: ~/vbot, or \$VBOT_DIR)
  --dev            Dev track: clone main and build the WebUI locally (needs Node)
  --version <tag>  Install a specific release instead of the latest (e.g. v0.1.2).
                   Release track only; cannot be combined with --dev.
  -h, --help       Show this help

Anything after -- is forwarded to scripts/install.sh, e.g.:
  bootstrap.sh -- --no-autostart
  bootstrap.sh -- --port 9000
USAGE
}

step() { echo "==> $1"; }
fail() { echo "Error: $1" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) INSTALL_DIR="$2"; shift 2 ;;
        --dev) DEV=1; shift ;;
        --version) VERSION="$2"; shift 2 ;;
        --) shift; INSTALLER_ARGS=("$@"); break ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Unknown option: $1" ;;
    esac
done

case "$INSTALL_DIR" in
    "~") INSTALL_DIR="$HOME" ;;
    "~/"*) INSTALL_DIR="${HOME}/${INSTALL_DIR#\~/}" ;;
esac
case "$INSTALL_DIR" in
    /*) ;;
    *) INSTALL_DIR="$(pwd)/${INSTALL_DIR}" ;;
esac
[ -e "$INSTALL_DIR" ] && fail "$INSTALL_DIR already exists. To update an existing install run 'vbot update'; otherwise remove it or pass --dir to choose another location."

if [ "$DEV" -eq 1 ] && [ -n "$VERSION" ]; then
    fail "--version selects a specific release tag and cannot be combined with --dev."
fi
# Accept a bare version (0.1.2) as well as the tag form (v0.1.2).
case "$VERSION" in
    "" | v*) ;;
    *) VERSION="v${VERSION}" ;;
esac

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
        step "Cloning ${REPO_URL} (main) into ${INSTALL_DIR}"
        git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    else
        step "Cloning ${REPO_URL} (${TAG}) into ${INSTALL_DIR}"
        git clone --depth 1 --branch "$TAG" "$REPO_URL" "$INSTALL_DIR"
    fi
    INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd)"
}

installer_has_argument() {
    local expected="$1"
    local argument
    for argument in "${INSTALLER_ARGS[@]}"; do
        [ "$argument" = "$expected" ] && return 0
    done
    return 1
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
    step "Waiting for the prebuilt WebUI for ${TAG}"
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
    step "Fetching prebuilt WebUI for ${TAG}"
    [ -n "$WEBUI_ASSET_URL" ] || fail "No preflighted WebUI asset URL is available."
    mkdir -p "${INSTALL_DIR}/webui"
    local archive="${INSTALL_DIR}/webui-dist.tar.gz"
    curl -fsSL "$WEBUI_ASSET_URL" -o "$archive"
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

run_installer() {
    step "Creating virtual environment at ${INSTALL_DIR}/.venv"
    python3 -m venv "${INSTALL_DIR}/.venv"
    # shellcheck disable=SC1091
    . "${INSTALL_DIR}/.venv/bin/activate"

    local args=()
    if [ "$DEV" -eq 1 ]; then
        args+=(--dev)
    else
        args+=(--skip-webui-build)
    fi
    [ "${#INSTALLER_ARGS[@]}" -gt 0 ] && args+=("${INSTALLER_ARGS[@]}")

    step "Running installer: scripts/install.sh ${args[*]:-}"
    if [ "${#args[@]}" -gt 0 ]; then
        bash "${INSTALL_DIR}/scripts/install.sh" "${args[@]}"
    else
        bash "${INSTALL_DIR}/scripts/install.sh"
    fi
}

link_vbot() {
    local target="${INSTALL_DIR}/.venv/bin/vbot"
    [ -x "$target" ] || return 0
    mkdir -p "${HOME}/.local/bin"
    ln -sf "$target" "${HOME}/.local/bin/vbot"
    step "Linked vbot into ${HOME}/.local/bin"
}

# Mark this directory as a self-contained bootstrap install so uninstall.sh knows
# it may remove the whole tree (venv + source), not just a pip package. Written
# right after the clone, so a bootstrap that fails mid-install still leaves a
# marked tree that uninstall.sh can remove wholesale.
write_marker() {
    cat > "${INSTALL_DIR}/.vbot-bootstrap" <<'MARKER'
# vBot bootstrap install marker.
# This directory is a self-contained vBot install created by the bootstrap script
# (it has its own virtual environment in .venv). Running scripts/uninstall.sh
# (uninstall.ps1 on Windows) removes this entire directory, the 'vbot' launcher,
# and the autostart entry. Your data directory is never touched.
MARKER
}

ensure_git
ensure_python
ensure_venv_support
[ "$DEV" -eq 1 ] && ensure_node

if [ "$DEV" -eq 0 ]; then
    if [ -n "$VERSION" ]; then
        TAG="$VERSION"
    else
        TAG="$(latest_release_tag || true)"
        [ -n "$TAG" ] || fail "Could not determine the latest release. Use --dev to install from main."
    fi
    if ! installer_has_argument "--desktop-client"; then
        wait_for_webui_asset
    fi
fi

clone_repo
write_marker
if [ "$DEV" -eq 0 ] && ! installer_has_argument "--desktop-client"; then
    fetch_prebuilt_webui
fi
run_installer
link_vbot

step "vBot bootstrap complete"
echo "Installed at: ${INSTALL_DIR}"
echo "The installer output above shows the configured data directory (Desktop Client has none)."
if installer_has_argument "--desktop-client"; then
    NEXT_COMMAND="desktop"
else
    NEXT_COMMAND="server status"
fi
case ":${PATH}:" in
    *":${HOME}/.local/bin:"*) echo "Run: vbot ${NEXT_COMMAND}" ;;
    *) echo "Add ${HOME}/.local/bin to your PATH, or run: ${INSTALL_DIR}/.venv/bin/vbot ${NEXT_COMMAND}" ;;
esac
