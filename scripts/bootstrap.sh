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
INSTALLER_ARGS=()

usage() {
    cat <<USAGE
Usage: bootstrap.sh [options] [-- <installer options>]

Options:
  --dir <path>   Where to clone vBot (default: ~/vbot, or \$VBOT_DIR)
  --dev          Dev track: clone main and build the WebUI locally (needs Node)
  -h, --help     Show this help

Anything after -- is forwarded to scripts/install.sh, e.g.:
  bootstrap.sh -- --enable-autostart --start-server
USAGE
}

step() { echo "==> $1"; }
fail() { echo "Error: $1" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) INSTALL_DIR="$2"; shift 2 ;;
        --dev) DEV=1; shift ;;
        --) shift; INSTALLER_ARGS=("$@"); break ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Unknown option: $1" ;;
    esac
done

case "$INSTALL_DIR" in
    "~") INSTALL_DIR="$HOME" ;;
    "~/"*) INSTALL_DIR="${HOME}/${INSTALL_DIR#\~/}" ;;
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
    [ -e "$INSTALL_DIR" ] && fail "$INSTALL_DIR already exists. Remove it or pass --dir to choose another location."
    if [ "$DEV" -eq 1 ]; then
        step "Cloning ${REPO_URL} (main) into ${INSTALL_DIR}"
        git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    else
        TAG="$(latest_release_tag || true)"
        [ -n "$TAG" ] || fail "Could not determine the latest release. Use --dev to install from main."
        step "Cloning ${REPO_URL} (${TAG}) into ${INSTALL_DIR}"
        git clone --depth 1 --branch "$TAG" "$REPO_URL" "$INSTALL_DIR"
    fi
}

fetch_prebuilt_webui() {
    step "Fetching prebuilt WebUI for ${TAG}"
    local url
    url="$(curl -fsSL "${API_BASE}/releases/tags/${TAG}" \
        | grep -o '"browser_download_url":[[:space:]]*"[^"]*webui-dist\.tar\.gz"' \
        | head -n1 \
        | sed -E 's/.*"(https:[^"]+)"/\1/' || true)"
    [ -n "$url" ] || fail "Release ${TAG} has no webui-dist.tar.gz asset yet. Use --dev to build locally, or wait for the release workflow to finish."
    mkdir -p "${INSTALL_DIR}/webui"
    curl -fsSL "$url" -o "${INSTALL_DIR}/webui-dist.tar.gz"
    tar -xzf "${INSTALL_DIR}/webui-dist.tar.gz" -C "${INSTALL_DIR}/webui"
    rm -f "${INSTALL_DIR}/webui-dist.tar.gz"
    [ -f "${INSTALL_DIR}/webui/dist/index.html" ] || fail "Prebuilt WebUI did not unpack to webui/dist."
}

run_installer() {
    step "Creating virtual environment at ${INSTALL_DIR}/.venv"
    python3 -m venv "${INSTALL_DIR}/.venv"
    # shellcheck disable=SC1091
    . "${INSTALL_DIR}/.venv/bin/activate"

    local args=()
    [ "$DEV" -eq 0 ] && args+=(--skip-webui-build)
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

ensure_git
ensure_python
[ "$DEV" -eq 1 ] && ensure_node

clone_repo
[ "$DEV" -eq 0 ] && fetch_prebuilt_webui
run_installer
link_vbot

step "vBot bootstrap complete"
echo "Installed at: ${INSTALL_DIR}"
echo "Data dir:     ${HOME}/.vbot"
case ":${PATH}:" in
    *":${HOME}/.local/bin:"*) echo "Run: vbot server status" ;;
    *) echo "Add ${HOME}/.local/bin to your PATH, or run: ${INSTALL_DIR}/.venv/bin/vbot server status" ;;
esac
