"""The Linux installer against fake GitHub release downloads and a fake package."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = PROJECT_ROOT / "scripts" / "install.sh"
DOWNLOADS = "https://github.com/Vironnimo/vbot/releases"
ASSET = "vbot-linux-aarch64-server.zip"
IDENTITY = "vbot-release.json"

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("python3") is None,
    reason="the Linux installer runs bash, curl and python3",
)

# Each fake records its call in $FAKE_LOG. curl serves URLs from $FAKE_WEB.
_FAKES = {
    "curl": r"""
out=""; url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        -H) shift 2 ;;
        -*) shift ;;
        *) url="$1"; shift ;;
    esac
done
echo "curl $url" >> "$FAKE_LOG"
file="$FAKE_WEB/$(printf %s "$url" | sed 's#^https://##; s#[/:]#_#g')"
[ -f "$file" ] || exit 22
cp "$file" "$out"
""",
    "uname": '[ "$1" = -m ] && echo aarch64 || echo Linux\n',
    "loginctl": 'echo "loginctl $*" >> "$FAKE_LOG"; [ "$1" = show-user ] && echo no; exit 0\n',
    "sudo": '"$@"\n',
}
# The package's interpreter stands in for the application installer: it lays
# out an installation whose bootstrap records every vBot command.
_INTERPRETER = r"""
echo "install $*" >> "$FAKE_LOG"
while [ $# -gt 0 ]; do [ "$1" = --root ] && root="$2"; shift; done
mkdir -p "$root"
echo '{}' > "$root/application.json"
printf '#!/bin/sh\necho "vbot $*" >> "$FAKE_LOG"\n' > "$root/vbot"
chmod 755 "$root/vbot"
"""


def _web_name(url: str) -> str:
    return re.sub(r"[/:]", "_", url.removeprefix("https://"))


def _publish(
    web: Path,
    base: str,
    package: bytes,
    digest: str | None = None,
    *,
    identity: bool = True,
    version: str = "1.2.3",
) -> None:
    """Publish the identity under ``base`` and the package of the release it names.

    The identity records the package's digest, or none for ``digest=""``. The
    latest release's assets live under the tag its identity names.
    """
    web.mkdir(exist_ok=True)
    asset_base = f"download/v{version}" if base == "latest/download" else base
    (web / _web_name(f"{DOWNLOADS}/{asset_base}/{ASSET}")).write_bytes(package)
    recorded = hashlib.sha256(package).hexdigest() if digest is None else digest
    assets = {ASSET: recorded} if recorded else {}
    if identity:
        (web / _web_name(f"{DOWNLOADS}/{base}/{IDENTITY}")).write_text(
            json.dumps({"schema_version": 1, "version": version, "assets": assets}), "utf-8"
        )


def _package(path: Path) -> bytes:
    with zipfile.ZipFile(path, "w") as bundle:
        interpreter = zipfile.ZipInfo("runtime/bin/python3")
        interpreter.external_attr = 0o100755 << 16
        bundle.writestr(interpreter, "#!/bin/sh\n" + _INTERPRETER)
        bundle.writestr("release.json", "{}")
    return path.read_bytes()


def _environment(tmp_path: Path) -> dict[str, str]:
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    for name, body in _FAKES.items():
        (fakes / name).write_text("#!/bin/sh\n" + body, encoding="utf-8")
        (fakes / name).chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    return {
        "PATH": f"{fakes}:/usr/bin:/bin",
        "HOME": str(home),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_WEB": str(tmp_path / "web"),
    }


def _install(environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _calls(environment: dict[str, str]) -> list[str]:
    log = Path(environment["FAKE_LOG"])
    return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []


@pytest.mark.parametrize(
    ("arguments", "base", "asset_base", "channel", "autostart", "digest"),
    [
        # The package comes from the tag the latest identity names, so a release
        # published meanwhile cannot mix in.
        pytest.param((), "latest/download", "download/v1.2.3", "release", True, None, id="release"),
        pytest.param(
            ("--main", "--no-autostart"),
            "download/main-build",
            "download/main-build",
            "main",
            False,
            None,
            id="main",
        ),
        # A release whose identity records no digest installs over HTTPS only.
        pytest.param(
            ("--version", "v1.2.3", "--no-autostart"),
            "download/v1.2.3",
            "download/v1.2.3",
            "release",
            False,
            "",
            id="version-without-digest",
        ),
    ],
)
def test_installer_installs_the_verified_package_links_the_command_and_starts_the_unit(
    tmp_path: Path,
    arguments: tuple[str, ...],
    base: str,
    asset_base: str,
    channel: str,
    autostart: bool,
    digest: str | None,
) -> None:
    environment = _environment(tmp_path)
    _publish(tmp_path / "web", base, _package(tmp_path / "package.zip"), digest)

    result = _install(environment, "--port", "8500", *arguments)

    assert result.returncode == 0, result.stdout + result.stderr
    assert ("WARN" in result.stdout) == (digest == "")
    root = Path(environment["HOME"]) / ".local" / "share" / "vbot"
    link = Path(environment["HOME"]) / ".local" / "bin" / "vbot"
    assert link.is_symlink() and link.resolve() == root / "vbot"
    calls = _calls(environment)
    # Only release downloads, never the GitHub API.
    assert [call for call in calls if call.startswith("curl")] == [
        f"curl {DOWNLOADS}/{base}/{IDENTITY}",
        f"curl {DOWNLOADS}/{asset_base}/{ASSET}",
    ]
    (install,) = [call for call in calls if call.startswith("install ")]
    assert f"--root {root} " in install and "--shape server" in install
    assert "--port 8500" in install and f"--channel {channel}" in install
    assert re.search(r"--public-key [A-Za-z0-9+/]{43}=", install)
    vbot = [call for call in calls if not call.startswith(("curl", "install"))]
    if autostart:
        # Lingering, enabled through sudo when off, starts the unit at boot.
        assert vbot == [
            "vbot autostart enable",
            f"loginctl show-user {_user()} --property=Linger --value",
            f"loginctl enable-linger {_user()}",
            "vbot server start",
        ]
    else:
        assert vbot == []

    again = _install(environment, *arguments)

    assert again.returncode == 0 and "already installed" in again.stdout
    assert _calls(environment) == calls


@pytest.mark.parametrize(
    ("arguments", "digest", "identity", "version", "message"),
    [
        pytest.param(
            (), "0" * 64, True, "1.2.3", "does not match the SHA256 digest", id="digest-mismatch"
        ),
        pytest.param((), None, False, "1.2.3", f"Could not read {IDENTITY}", id="no-identity"),
        # The tag, given or named by the latest identity, becomes part of the download URL.
        pytest.param(
            ("--version", "v1/../../x"), None, True, "1.2.3", "--version needs", id="unsafe-tag"
        ),
        pytest.param(
            (), None, True, "1.2.3-x", "names no valid release version", id="unsafe-version"
        ),
    ],
)
def test_installer_refuses_a_package_it_cannot_verify_against_the_release_identity(
    tmp_path: Path,
    arguments: tuple[str, ...],
    digest: str | None,
    identity: bool,
    version: str,
    message: str,
) -> None:
    environment = _environment(tmp_path)
    package = _package(tmp_path / "package.zip")
    _publish(
        tmp_path / "web", "latest/download", package, digest, identity=identity, version=version
    )

    result = _install(environment, *arguments)

    assert result.returncode == 1
    assert message in result.stderr
    assert not [call for call in _calls(environment) if not call.startswith("curl")]
    assert not (Path(environment["HOME"]) / ".local" / "share" / "vbot").exists()


def _user() -> str:
    return subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
