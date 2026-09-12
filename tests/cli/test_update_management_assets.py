"""Tests for update management assets."""

from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import httpx
import pytest
import respx

from cli import _update_assets
from cli._update_assets import _extract_within
from cli.update_management import (
    UNKNOWN_VBOT_VERSION,
    _default_runner,
    read_checkout_version,
)
from tests.cli.update_management_test_support import (
    _webui_tar_bytes,
)


def test_read_checkout_version_uses_live_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "vbot"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )

    assert read_checkout_version(tmp_path) == "1.2.3"


def test_read_checkout_version_reports_unknown_for_invalid_project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    assert read_checkout_version(tmp_path) == UNKNOWN_VBOT_VERSION


@respx.mock
def test_release_download_installs_bundled_pages_without_overwriting_sources(
    tmp_path: Path,
) -> None:
    extension = tmp_path / "resources" / "extensions" / "alpha"
    extension.mkdir(parents=True)
    (extension / "extension.py").write_text("checkout source", encoding="utf-8")
    old_web = extension / "web"
    old_web.mkdir()
    (old_web / "obsolete.js").write_text("old asset", encoding="utf-8")
    removed = tmp_path / "resources" / "extensions" / "removed" / "web"
    removed.mkdir(parents=True)
    (removed / "page.html").write_text("removed page", encoding="utf-8")
    asset_url = "https://example.com/webui-dist.tar.gz"
    respx.get(asset_url).mock(
        return_value=httpx.Response(
            200,
            content=_webui_tar_bytes(
                {
                    "webui/dist/index.html": b"new app",
                    "resources/extensions/alpha/extension.py": b"archived source",
                    "resources/extensions/alpha/web/page.html": b"alpha page",
                    "resources/extensions/alpha/web/assets/new.js": b"alpha script",
                    "resources/extensions/beta/web/page.html": b"beta page",
                }
            ),
        )
    )

    result = _update_assets._download_webui(asset_url, tmp_path)

    assert result.ok
    assert (tmp_path / "webui" / "dist" / "index.html").read_bytes() == b"new app"
    assert (old_web / "assets" / "new.js").read_bytes() == b"alpha script"
    assert not (old_web / "obsolete.js").exists()
    assert not removed.exists()
    assert (extension / "extension.py").read_text(encoding="utf-8") == "checkout source"
    assert (extension.parent / "beta" / "web" / "page.html").read_bytes() == b"beta page"


def test_asset_swap_failure_restores_app_and_every_previous_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webui = tmp_path / "webui"
    originals = {
        "webui/dist/index.html": b"old app",
        "resources/extensions/alpha/web/page.html": b"old alpha",
        "resources/extensions/beta/web/page.html": b"old beta",
    }
    for name, content in originals.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    rename = Path.rename

    def fail_last_asset(source: Path, target: Path) -> Path:
        if "dist.staging" in source.parts and source.parts[-2:] == ("beta", "web"):
            raise OSError("injected asset swap failure")
        return rename(source, target)

    monkeypatch.setattr(Path, "rename", fail_last_asset)
    with pytest.raises(OSError, match="injected asset swap failure"):
        _update_assets._unpack_webui_archive(
            _webui_tar_bytes(dict.fromkeys(originals, b"replacement")),
            webui,
        )

    for name, content in originals.items():
        assert (tmp_path / name).read_bytes() == content
    assert not (webui / "dist.staging").exists()
    assert not (webui / "dist.backup").exists()


def test_legacy_asset_archive_keeps_existing_extension_assets(tmp_path: Path) -> None:
    page = tmp_path / "resources" / "extensions" / "alpha" / "web" / "page.html"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"retained page")

    _update_assets._unpack_webui_archive(_webui_tar_bytes(), tmp_path / "webui")

    assert page.read_bytes() == b"retained page"
    assert (tmp_path / "webui" / "dist" / "index.html").is_file()


def test_incomplete_new_asset_archive_keeps_all_installed_assets(tmp_path: Path) -> None:
    index = tmp_path / "webui" / "dist" / "index.html"
    index.parent.mkdir(parents=True)
    index.write_bytes(b"retained app")
    with pytest.raises(ValueError, match="dist/index.html"):
        _update_assets._unpack_webui_archive(
            _webui_tar_bytes(
                {
                    "webui/dist/assets/bundle.js": b"no entry",
                    "resources/extensions/alpha/web/page.html": b"new page",
                }
            ),
            tmp_path / "webui",
        )

    assert index.read_bytes() == b"retained app"
    assert not (tmp_path / "resources").exists()


def test_extract_within_extracts_benign_archive(tmp_path: Path) -> None:
    # The same-tree fallback path used on Pythons without tarfile's data filter.
    destination = tmp_path / "webui"
    destination.mkdir()

    with tarfile.open(fileobj=io.BytesIO(_webui_tar_bytes()), mode="r:gz") as archive:
        _extract_within(archive, destination)

    assert (destination / "dist" / "index.html").is_file()


def test_extract_within_rejects_path_escape(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = b"x"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    buffer.seek(0)
    destination = tmp_path / "webui"
    destination.mkdir()

    with tarfile.open(fileobj=buffer, mode="r:gz") as archive, pytest.raises(tarfile.TarError):
        _extract_within(archive, destination)
    assert not (tmp_path / "escape.txt").exists()


def test_extract_within_rejects_link_members(tmp_path: Path) -> None:
    # A symlink member could redirect later members outside the tree after the
    # name-based pre-check has passed, so the fallback refuses links outright.
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        link = tarfile.TarInfo("dist/evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        archive.addfile(link)
    buffer.seek(0)
    destination = tmp_path / "webui"
    destination.mkdir()

    with tarfile.open(fileobj=buffer, mode="r:gz") as archive, pytest.raises(tarfile.TarError):
        _extract_within(archive, destination)
    assert not (destination / "dist" / "evil").exists()


def test_extract_within_rejects_special_members(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        fifo = tarfile.TarInfo("dist/special")
        fifo.type = tarfile.FIFOTYPE
        archive.addfile(fifo)
    buffer.seek(0)
    destination = tmp_path / "webui"
    destination.mkdir()

    with tarfile.open(fileobj=buffer, mode="r:gz") as archive, pytest.raises(tarfile.TarError):
        _extract_within(archive, destination)
    assert not (destination / "dist" / "special").exists()


def test_default_runner_disables_git_prompt(tmp_path: Path) -> None:
    result = _default_runner(
        [sys.executable, "-c", "import os; print(os.environ.get('GIT_TERMINAL_PROMPT', 'unset'))"],
        tmp_path,
    )

    assert result.returncode == 0
    assert result.stdout == "0"


def test_default_runner_prefers_utf8_output(tmp_path: Path) -> None:
    result = _default_runner(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write('Łódź'.encode('utf-8'))",
        ],
        tmp_path,
    )

    assert result.returncode == 0
    assert result.stdout == "Łódź"


def test_default_runner_preserves_undecodable_output(tmp_path: Path) -> None:
    result = _default_runner(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(bytes([0x81]) + b'tail')",
        ],
        tmp_path,
    )

    assert result.returncode == 0
    assert result.stdout == r"\x81tail"


def test_default_runner_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cli.update_management._COMMAND_TIMEOUT_SECONDS", 0.2)

    result = _default_runner([sys.executable, "-c", "import time; time.sleep(5)"], tmp_path)

    assert result.returncode == 124
    assert "timed out" in result.stderr
