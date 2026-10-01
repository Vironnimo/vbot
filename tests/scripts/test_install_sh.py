"""The Linux installer against a fake GitHub release and a fake package."""

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
API = "https://api.github.com/repos/Vironnimo/vbot"
ASSET = "vbot-linux-aarch64-server.zip"

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


def _publish(web: Path, release: str, package: bytes, digest: str | None = None) -> None:
    web.mkdir(exist_ok=True)
    download = f"https://github.com/Vironnimo/vbot/releases/download/x/{ASSET}"
    (web / _web_name(download)).write_bytes(package)
    asset = {
        "name": ASSET,
        "browser_download_url": download,
        "digest": f"sha256:{digest or hashlib.sha256(package).hexdigest()}",
    }
    (web / _web_name(f"{API}/{release}")).write_text(json.dumps({"assets": [asset]}), "utf-8")


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
    ("arguments", "release", "channel", "autostart"),
    [
        pytest.param((), "releases/latest", "release", True, id="release"),
        pytest.param(
            ("--main", "--no-autostart"), "releases/tags/main-build", "main", False, id="main"
        ),
    ],
)
def test_installer_installs_the_verified_package_links_the_command_and_starts_the_unit(
    tmp_path: Path, arguments: tuple[str, ...], release: str, channel: str, autostart: bool
) -> None:
    environment = _environment(tmp_path)
    _publish(tmp_path / "web", release, _package(tmp_path / "package.zip"))

    result = _install(environment, "--port", "8500", *arguments)

    assert result.returncode == 0, result.stdout + result.stderr
    root = Path(environment["HOME"]) / ".local" / "share" / "vbot"
    link = Path(environment["HOME"]) / ".local" / "bin" / "vbot"
    assert link.is_symlink() and link.resolve() == root / "vbot"
    calls = _calls(environment)
    assert calls[0] == f"curl {API}/{release}"
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


def test_installer_refuses_a_package_that_does_not_match_its_published_digest(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    _publish(tmp_path / "web", "releases/latest", _package(tmp_path / "package.zip"), "0" * 64)

    result = _install(environment)

    assert result.returncode == 1
    assert "does not match its published SHA256 digest" in result.stderr
    assert not [call for call in _calls(environment) if not call.startswith("curl")]
    assert not (Path(environment["HOME"]) / ".local" / "share" / "vbot").exists()


def _user() -> str:
    return subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
