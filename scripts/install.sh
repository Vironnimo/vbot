#!/usr/bin/env bash
# vBot installer for Linux (Raspberry Pi OS 64-bit and other ARM64 or x86-64 systems).
#
# Downloads the signed vBot server package for this machine, installs it as a
# per-user application, links the `vbot` command into ~/.local/bin and runs the
# server as the systemd user unit vbot.service. The package brings its own
# Python; the machine needs only curl and python3.
#   curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash
# Safer: download it, read it, then run it.
set -euo pipefail

# Release downloads, unlike the GitHub API, have no anonymous request limit.
DOWNLOADS="https://github.com/Vironnimo/vbot/releases"
IDENTITY="vbot-release.json"
# Updates verify every package against this key.
RELEASE_PUBLIC_KEY="8gLB0IOKj1jlmhDxtNq3tRS1HgYwIv+tntzyVMiUiuQ="

INSTALL_DIR="${VBOT_DIR:-${HOME}/.local/share/vbot}"
DATA_DIR="${HOME}/.vbot"
HOST="127.0.0.1"
PORT="8420"
CHANNEL="release"
VERSION=""
NO_AUTOSTART=0
BIN_DIR="${HOME}/.local/bin"

usage() {
    cat <<USAGE
Usage: install.sh [options]

Options:
  --dir <path>        Application directory (default: ~/.local/share/vbot or \$VBOT_DIR)
  --main              Install the newest main build; updates then follow main
  --version <tag>     Install a specific release (for example v0.2.0)
  --data-dir <path>   Server data directory (default: ~/.vbot)
  --host <host>       Server bind host (default: 127.0.0.1)
  --port <port>       Server port (default: 8420)
  --no-autostart      Do not register the systemd user unit or start the server
  -h, --help          Show this help
USAGE
}

status_line() {
    local state="$1" message="$2" color="36"
    case "$state" in
        OK) color="32" ;;
        WARN) color="33" ;;
        ERROR) color="31" ;;
    esac
    if [ -t 1 ] && [ "${TERM:-}" != "dumb" ] && [ -z "${NO_COLOR:-}" ]; then
        printf '\033[%sm%s\033[0m %s\n' "$color" "$state" "$message"
    else
        printf '%s %s\n' "$state" "$message"
    fi
}

fail() {
    status_line ERROR "$1" >&2
    exit 1
}

require_value() {
    [ "$#" -ge 2 ] && [ -n "$2" ] || fail "$1 needs a value"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dir) require_value "$@"; INSTALL_DIR="$2"; shift 2 ;;
        --main) CHANNEL="main"; shift ;;
        --version) require_value "$@"; VERSION="$2"; shift 2 ;;
        --data-dir) require_value "$@"; DATA_DIR="$2"; shift 2 ;;
        --host) require_value "$@"; HOST="$2"; shift 2 ;;
        --port) require_value "$@"; PORT="$2"; shift 2 ;;
        --no-autostart) NO_AUTOSTART=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Unknown option: $1" ;;
    esac
done

[ "$CHANNEL" = "main" ] && [ -n "$VERSION" ] && fail "Choose either --main or --version"
case "$VERSION" in
    .* | *[!A-Za-z0-9._+-]*) fail "--version needs a release tag such as v0.2.0" ;;
esac
case "$PORT" in
    "" | *[!0-9]*) fail "--port must be a number" ;;
esac
[ "$(uname -s)" = "Linux" ] || fail "This installer is for Linux; use install.ps1 on Windows"
case "$(uname -m)" in
    aarch64 | arm64) PLATFORM="linux-aarch64" ;;
    x86_64 | amd64) PLATFORM="linux-x86_64" ;;
    *) fail "vBot packages exist for 64-bit ARM and x86-64 Linux, not $(uname -m). On a Raspberry Pi, install the 64-bit Raspberry Pi OS." ;;
esac
command -v curl >/dev/null || fail "curl is missing; install it first (sudo apt install curl)"
command -v python3 >/dev/null || fail "python3 is missing; install it first (sudo apt install python3)"

INSTALL_DIR="$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$INSTALL_DIR")"
DATA_DIR="$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$DATA_DIR")"
if [ -f "$INSTALL_DIR/application.json" ]; then
    status_line OK "vBot is already installed at $INSTALL_DIR"
    echo "Update it with: $INSTALL_DIR/vbot update"
    exit 0
fi

if [ "$CHANNEL" = "main" ]; then
    BASE="$DOWNLOADS/download/main-build"
    RELEASE="the newest main build"
elif [ -n "$VERSION" ]; then
    BASE="$DOWNLOADS/download/$VERSION"
    RELEASE="release $VERSION"
else
    BASE="$DOWNLOADS/latest/download"
    RELEASE="the latest release"
fi
ASSET="vbot-${PLATFORM}-server.zip"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

status_line WORK "Looking up $RELEASE"
curl -fsSL "$BASE/$IDENTITY" -o "$WORK/$IDENTITY" \
    || fail "Could not read $IDENTITY of $RELEASE from $BASE: the release does not exist, publishes no $IDENTITY, or GitHub cannot be reached"
# The release identity names the version and records the SHA256 digest of each
# asset; this prints the version and the package's digest, or - for none.
FACTS="$(python3 - "$WORK/$IDENTITY" "$ASSET" <<'PY'
import json, re, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        identity = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(1)
if not isinstance(identity, dict) or identity.get("schema_version") != 1:
    raise SystemExit(1)
version = identity.get("version")
assets = identity.get("assets")
digest = assets.get(sys.argv[2]) if isinstance(assets, dict) else None
if not isinstance(version, str) or not re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", version):
    raise SystemExit(1)
if digest is not None and not (isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
    raise SystemExit(1)
print(version, digest.lower() if digest else "-")
PY
)" || fail "$BASE/$IDENTITY is not a valid vBot release identity"
read -r RELEASE_VERSION ASSET_SHA256 <<<"$FACTS"

status_line WORK "Downloading $ASSET of vBot $RELEASE_VERSION"
curl -fL --progress-bar "$BASE/$ASSET" -o "$WORK/$ASSET" \
    || fail "Could not download $BASE/$ASSET: the release publishes no package for $PLATFORM, or GitHub cannot be reached"
if [ "$ASSET_SHA256" != "-" ]; then
    ACTUAL="$(python3 -c 'import hashlib, sys; print(hashlib.file_digest(open(sys.argv[1], "rb"), "sha256").hexdigest())' "$WORK/$ASSET" 2>/dev/null \
        || sha256sum "$WORK/$ASSET" | cut -d " " -f 1)"
    [ "$ACTUAL" = "$ASSET_SHA256" ] \
        || fail "The downloaded package does not match the SHA256 digest in $IDENTITY. A newer build may have replaced the release files meanwhile (main builds are replaced in place); run the installer again"
else
    status_line WARN "$IDENTITY records no digest for $ASSET; it was downloaded over HTTPS only"
fi

status_line WORK "Unpacking the package"
python3 - "$WORK/$ASSET" "$WORK/payload" <<'PY'
import os, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as bundle:
    for item in bundle.infolist():
        target = bundle.extract(item, sys.argv[2])
        if (item.external_attr >> 16) & 0o111:
            os.chmod(target, 0o755)
PY

status_line WORK "Installing vBot into $INSTALL_DIR"
"$WORK/payload/runtime/bin/python3" -I -B -X utf8 -m cli.application.install \
    --root "$INSTALL_DIR" --payload "$WORK/payload" --shape server \
    --host "$HOST" --port "$PORT" --data-dir "$DATA_DIR" \
    --public-key "$RELEASE_PUBLIC_KEY" --channel "$CHANNEL" \
    || fail "The installation failed"
VBOT="$INSTALL_DIR/vbot"
status_line OK "vBot installed at $INSTALL_DIR"

mkdir -p "$BIN_DIR"
if [ ! -e "$BIN_DIR/vbot" ] || [ -L "$BIN_DIR/vbot" ]; then
    ln -sfn "$VBOT" "$BIN_DIR/vbot"
    status_line OK "Linked the vbot command into $BIN_DIR"
else
    status_line WARN "$BIN_DIR/vbot already exists and was left alone; run $VBOT directly"
fi

if [ "$NO_AUTOSTART" -eq 0 ]; then
    "$VBOT" autostart enable >/dev/null || fail "The systemd user unit could not be registered; inspect: $VBOT autostart enable"
    status_line OK "Registered the systemd user unit vbot.service"
    # Lingering starts the server at boot, before anyone logs in.
    if [ "$(loginctl show-user "$(id -un)" --property=Linger --value 2>/dev/null)" != "yes" ]; then
        if command -v sudo >/dev/null && sudo loginctl enable-linger "$(id -un)"; then
            status_line OK "Enabled login lingering, so vBot starts at boot"
        else
            status_line WARN "Login lingering is off, so vBot starts only once you log in. Enable it with: sudo loginctl enable-linger $(id -un)"
        fi
    fi
    "$VBOT" server start >/dev/null || fail "The server did not start; inspect: $VBOT server status"
    status_line OK "The server is running at http://$HOST:$PORT"
fi

echo
echo "Data: $DATA_DIR"
echo "Commands: vbot server status | vbot update | vbot uninstall"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "Open a new login shell so that $BIN_DIR is on your PATH, or run $VBOT directly." ;;
esac
