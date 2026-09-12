"""Tests for update management release."""

from __future__ import annotations

from pathlib import Path

import httpx
import respx

from cli.update_management import (
    CommandRun,
    ReleaseInfo,
    run_update,
)
from tests.cli.update_management_test_support import (
    ScriptedRunner,
    _err,
    _instance,
    _ok,
    _recording_restart,
    _webui_tar_bytes,
    _write_state,
)


def test_release_track_requires_webui_asset(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path, track="release", revision="old", webui_revision="old")
    revisions = iter(["old", "new"])

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _err()
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(revisions))
        return _ok("")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        latest_release=lambda: ReleaseInfo(tag="v9.9.9", webui_asset_url=None),
    )

    assert not result.ok
    assert not runner.ran("git", "checkout", "--force", "v9.9.9")
    assert events == []


def test_release_track_does_not_require_missing_asset_for_intact_current_tag(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    dist = tmp_path / "webui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    _write_state(tmp_path, track="release", revision="same", webui_revision="same")

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _err()
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "rev-parse"]:
            return _ok("same")
        if command[:2] == ["git", "describe"]:
            return _ok("v1.0.0")
        return _ok("")

    result = run_update(
        _instance(),
        runner=ScriptedRunner(handler),
        root=tmp_path,
        restart=False,
        latest_release=lambda: ReleaseInfo(tag="v1.0.0", webui_asset_url=None),
    )

    assert result.ok, result.message


@respx.mock
def test_release_track_downloads_prebuilt_webui(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path, track="release", revision="old", webui_revision="old")
    asset_url = "https://example.com/webui-dist.tar.gz"
    respx.get(asset_url).mock(return_value=httpx.Response(200, content=_webui_tar_bytes()))
    revisions = iter(["old", "new", "new"])

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _err()
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(revisions))
        return _ok("")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        latest_release=lambda: ReleaseInfo(tag="v9.9.9", webui_asset_url=asset_url),
    )

    assert result.ok, result.message
    assert (tmp_path / "webui" / "dist" / "index.html").is_file()
    assert events == ["stop", "start"]


@respx.mock
def test_release_track_skips_download_when_up_to_date(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    dist = tmp_path / "webui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    _write_state(tmp_path, track="release", webui_revision="samesha")
    asset_url = "https://example.com/webui-dist.tar.gz"
    route = respx.get(asset_url).mock(return_value=httpx.Response(200, content=_webui_tar_bytes()))

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _err()  # detached HEAD -> release track
        if command[:2] == ["git", "status"]:
            return _ok("")
        return _ok("samesha") if command[:2] == ["git", "rev-parse"] else _ok("")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        latest_release=lambda: ReleaseInfo(tag="v1.0.0", webui_asset_url=asset_url),
    )

    assert result.ok, result.message
    assert route.called is False
    assert events == ["stop", "start"]


@respx.mock
def test_release_track_redownloads_when_dist_missing(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path, track="release", webui_revision="samesha")
    asset_url = "https://example.com/webui-dist.tar.gz"
    route = respx.get(asset_url).mock(return_value=httpx.Response(200, content=_webui_tar_bytes()))

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _err()
        if command[:2] == ["git", "status"]:
            return _ok("")
        return _ok("samesha") if command[:2] == ["git", "rev-parse"] else _ok("")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        latest_release=lambda: ReleaseInfo(tag="v1.0.0", webui_asset_url=asset_url),
    )

    assert result.ok, result.message
    assert route.called is True
    assert (tmp_path / "webui" / "dist" / "index.html").is_file()


@respx.mock
def test_release_download_replaces_stale_dist(tmp_path: Path) -> None:
    # The new bundle replaces dist wholesale; hashed bundles from an older
    # release must not survive the update.
    (tmp_path / ".git").mkdir()
    dist = tmp_path / "webui" / "dist"
    dist.mkdir(parents=True)
    (dist / "stale-bundle.js").write_text("old", encoding="utf-8")
    _write_state(tmp_path, track="release", revision="old", webui_revision="old")
    asset_url = "https://example.com/webui-dist.tar.gz"
    respx.get(asset_url).mock(return_value=httpx.Response(200, content=_webui_tar_bytes()))
    revisions = iter(["old", "new", "new"])

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _err()
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(revisions))
        return _ok("")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        latest_release=lambda: ReleaseInfo(tag="v9.9.9", webui_asset_url=asset_url),
    )

    assert result.ok, result.message
    assert (dist / "index.html").is_file()
    assert not (dist / "stale-bundle.js").exists()


@respx.mock
def test_release_download_keeps_dist_on_corrupt_archive(tmp_path: Path) -> None:
    # A corrupt download must fail the update without costing the existing dist.
    (tmp_path / ".git").mkdir()
    dist = tmp_path / "webui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    _write_state(tmp_path, track="release", revision="old", webui_revision="old")
    asset_url = "https://example.com/webui-dist.tar.gz"
    respx.get(asset_url).mock(return_value=httpx.Response(200, content=b"not a tarball"))
    revisions = iter(["old", "new", "new"])

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _err()
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(revisions))
        return _ok("")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        latest_release=lambda: ReleaseInfo(tag="v9.9.9", webui_asset_url=asset_url),
    )

    assert not result.ok
    assert (dist / "index.html").is_file()
    assert not (tmp_path / "webui" / "dist.staging").exists()
    assert events == []
