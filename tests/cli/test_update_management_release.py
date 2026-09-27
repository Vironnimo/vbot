"""``vbot update`` on a detached release checkout and its prebuilt WebUI asset."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

import cli.update_management as update_management
from cli._update_types import UpdateResult
from cli.update_management import CommandRun, ReleaseInfo, run_update
from tests.cli.update_management_test_support import (
    ScriptedRunner,
    _err,
    _instance,
    _ok,
    _recording_restart,
    _recording_snapshot,
    _webui_tar_bytes,
    _write_state,
    checkout,
    only_reads,
    write_webui_build,
)

ASSET_URL = "https://example.com/webui-dist.tar.gz"


@pytest.fixture(autouse=True)
def no_running_launchers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the Windows launcher guards away from this machine's real processes."""

    monkeypatch.setattr(update_management, "_running_process_id", lambda *_args, **_kw: None)


def test_release_without_a_webui_asset_is_refused_before_the_checkout_changes(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    write_webui_build(tmp_path, "old")
    _write_state(tmp_path, track="release", revision="old", webui_revision="old")
    runner = ScriptedRunner(
        checkout(
            heads="old",
            branch=False,
            answer=only_reads(
                "symbolic-ref", "rev-parse", "status", describe=_err("not the current release tag")
            ),
        )
    )
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
    assert "release v9.9.9 has no" in result.message
    assert "the checkout was left unchanged" in result.message
    assert events == []


def test_release_track_at_the_intact_current_tag_changes_nothing(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    write_webui_build(tmp_path)
    _write_state(tmp_path, track="release", revision="same", webui_revision="same")

    def describe(command: list[str]) -> CommandRun | None:
        return _ok("v1.0.0") if command[:2] == ["git", "describe"] else None

    runner = ScriptedRunner(checkout(heads="same", branch=False, answer=describe))
    events, stop, start = _recording_restart()
    snapshots, snapshot = _recording_snapshot()

    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        data_snapshot_fn=snapshot,
        latest_release=lambda: ReleaseInfo(tag="v1.0.0", webui_asset_url=None),
    )

    assert isinstance(result, UpdateResult)
    assert result.ok, result.message
    assert result.restart_state == "unchanged"
    assert snapshots == []
    assert events == []
    assert not runner.ran("git", "fetch")
    assert not runner.ran("git", "checkout")


@respx.mock
def test_release_track_replaces_the_whole_webui_with_the_new_release_asset(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    dist = tmp_path / "webui" / "dist"
    dist.mkdir(parents=True)
    # Hashed bundles from an older release must not survive the update.
    (dist / "stale-bundle.js").write_text("old", encoding="utf-8")
    _write_state(tmp_path, track="release", revision="old", webui_revision="old")
    respx.get(ASSET_URL).mock(return_value=httpx.Response(200, content=_webui_tar_bytes()))
    runner = ScriptedRunner(checkout(heads=["old", "new"], branch=False))
    events, stop, start = _recording_restart()

    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        latest_release=lambda: ReleaseInfo(tag="v9.9.9", webui_asset_url=ASSET_URL),
    )

    assert result.ok, result.message
    assert runner.ran("git", "checkout", "--force", "v9.9.9")
    assert (dist / "index.html").is_file()
    assert not (dist / "stale-bundle.js").exists()
    assert events == ["stop", "start"]


@respx.mock
@pytest.mark.parametrize("dist_present", [True, False], ids=["intact", "dist-missing"])
def test_release_track_downloads_the_asset_only_when_the_installed_webui_is_missing(
    tmp_path: Path, dist_present: bool
) -> None:
    (tmp_path / ".git").mkdir()
    if dist_present:
        write_webui_build(tmp_path)
    _write_state(tmp_path, track="release", webui_revision="samesha")
    route = respx.get(ASSET_URL).mock(return_value=httpx.Response(200, content=_webui_tar_bytes()))
    events, stop, start = _recording_restart()

    result = run_update(
        _instance(),
        runner=ScriptedRunner(checkout(branch=False)),
        root=tmp_path,
        stop=stop,
        start=start,
        latest_release=lambda: ReleaseInfo(tag="v1.0.0", webui_asset_url=ASSET_URL),
    )

    assert result.ok, result.message
    assert route.called is not dist_present
    assert (tmp_path / "webui" / "dist" / "index.html").is_file()
    assert events == ["stop", "start"]


@respx.mock
def test_release_download_keeps_dist_on_corrupt_archive(tmp_path: Path) -> None:
    # A corrupt download must fail the update without costing the existing dist.
    (tmp_path / ".git").mkdir()
    dist = write_webui_build(tmp_path)
    _write_state(tmp_path, track="release", revision="old", webui_revision="old")
    respx.get(ASSET_URL).mock(return_value=httpx.Response(200, content=b"not a tarball"))
    events, stop, start = _recording_restart()

    result = run_update(
        _instance(),
        runner=ScriptedRunner(checkout(heads=["old", "new"], branch=False)),
        root=tmp_path,
        stop=stop,
        start=start,
        latest_release=lambda: ReleaseInfo(tag="v9.9.9", webui_asset_url=ASSET_URL),
    )

    assert not result.ok
    assert (dist / "index.html").is_file()
    assert not (tmp_path / "webui" / "dist.staging").exists()
    assert events == []
