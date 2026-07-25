"""Tests for the local `vbot update` command logic."""

from __future__ import annotations

import io
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import respx

from cli.install_state import (
    INSTALL_STATE_SCHEMA_VERSION,
    InstallState,
    file_digest,
    write_install_state,
)
from cli.main import dispatch_update_command
from cli.parser import parse_args
from cli.server_management import CommandResult, ServerInstance
from cli.update_management import (
    UNKNOWN_APP_VERSION,
    CommandRun,
    ReleaseInfo,
    _default_runner,
    _extract_within,
    read_checkout_version,
    run_update,
)


def _instance() -> ServerInstance:
    return ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=Path("/data"),
        url="http://127.0.0.1:8420",
        log_path=Path("/data/logs/today.log"),
    )


def _ok(stdout: str = "") -> CommandRun:
    return CommandRun(returncode=0, stdout=stdout, stderr="")


def test_read_checkout_version_uses_live_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "vbot"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )

    assert read_checkout_version(tmp_path) == "1.2.3"


def test_read_checkout_version_reports_unknown_for_invalid_project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    assert read_checkout_version(tmp_path) == UNKNOWN_APP_VERSION


def _err(stderr: str = "boom") -> CommandRun:
    return CommandRun(returncode=1, stdout="", stderr=stderr)


class ScriptedRunner:
    """Records command invocations and answers from a per-command handler."""

    def __init__(self, handler: Callable[[list[str]], CommandRun]) -> None:
        self._handler = handler
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], cwd: Path) -> CommandRun:
        self.calls.append(list(command))
        return self._handler(list(command))

    def ran(self, *needle: str) -> bool:
        target = list(needle)
        return any(
            call[index : index + len(target)] == target
            for call in self.calls
            for index in range(len(call) - len(target) + 1)
        )


def _recording_restart() -> tuple[
    list[str], Callable[..., CommandResult], Callable[..., CommandResult]
]:
    events: list[str] = []

    def stop(instance: ServerInstance) -> CommandResult:
        events.append("stop")
        return CommandResult(ok=True, message="stopped", instance=instance)

    def start(instance: ServerInstance) -> CommandResult:
        events.append("start")
        return CommandResult(ok=True, message="started", instance=instance)

    return events, stop, start


def _webui_tar_bytes() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = b"<!doctype html>"
        info = tarfile.TarInfo("dist/index.html")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _write_state(
    root: Path,
    *,
    track: str = "dev",
    revision: str = "samesha",
    shape: str = "server",
    groups: tuple[str, ...] | None = None,
    python_executable: str | None = None,
    dependency_digest: str | None = None,
    webui_revision: str | None = None,
) -> None:
    if groups is None:
        if shape == "desktop-client":
            groups = ("cli", "desktop")
        elif shape == "server-desktop":
            groups = ("server", "cli", "desktop")
        else:
            groups = ("server", "cli")
    write_install_state(
        root,
        InstallState(
            schema_version=INSTALL_STATE_SCHEMA_VERSION,
            install_shape=shape,
            dependency_groups=groups,
            python_executable=python_executable or sys.executable,
            source_track=track,
            applied_revision=revision,
            dependency_digest=(
                file_digest(root / "pyproject.toml")
                if dependency_digest is None
                else dependency_digest
            ),
            webui_revision=None if shape == "desktop-client" else webui_revision,
        ),
    )


def test_update_refuses_non_git_checkout(tmp_path: Path) -> None:
    def runner(command: list[str], cwd: Path) -> CommandRun:
        raise AssertionError(f"runner should not run before the git check: {command}")

    events, stop, start = _recording_restart()
    result = run_update(_instance(), runner=runner, root=tmp_path, stop=stop, start=start)

    assert not result.ok
    assert "not a git checkout" in result.message
    assert events == []


def test_update_refuses_dirty_without_flags(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path)

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok(" M core/foo.py")
        if command[:2] == ["git", "rev-parse"]:
            return _ok("samesha")
        raise AssertionError(f"unexpected command after refusal: {command}")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(_instance(), runner=runner, root=tmp_path, stop=stop, start=start)

    assert not result.ok
    assert "local changes" in result.message
    assert events == []
    assert not runner.ran("git", "pull")


def test_update_discard_resets_then_updates(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path)

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok(" M x.py")
        return _ok("samesha") if command[:2] == ["git", "rev-parse"] else _ok("")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(
        _instance(), discard=True, runner=runner, root=tmp_path, stop=stop, start=start
    )

    assert result.ok, result.message
    assert runner.ran("git", "reset", "--hard", "HEAD")
    assert events == ["stop", "start"]


def test_dev_track_up_to_date_restarts(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path)

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok("")
        return _ok("samesha") if command[:2] == ["git", "rev-parse"] else _ok("")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(_instance(), runner=runner, root=tmp_path, stop=stop, start=start)

    assert result.ok, result.message
    assert "already up to date" in result.message
    assert "server: restarted" in result.message
    assert events == ["stop", "start"]


def test_dev_track_reinstalls_deps_and_rebuilds_webui(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("before", encoding="utf-8")
    _write_state(tmp_path, revision="beforesha", webui_revision="beforesha")
    revisions = iter(["beforesha", "aftersha"])

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(revisions))
        if command[:2] == ["git", "pull"]:
            (tmp_path / "pyproject.toml").write_text("after", encoding="utf-8")
            return _ok("")
        if command[:3] == ["git", "diff", "--quiet"]:
            return _err()
        return _ok("")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(_instance(), runner=runner, root=tmp_path, stop=stop, start=start)

    assert result.ok, result.message
    assert "updated beforesha -> aftersha" in result.message
    assert "dependencies reinstalled ([server,cli])" in result.message
    assert "webui rebuilt" in result.message
    assert runner.ran("-m", "pip", "install", "-e", ".[server,cli]")
    assert any("npm" in call for call in runner.calls)
    assert events == ["stop", "start"]


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
    assert "no webui-dist.tar.gz asset" in result.message
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
    assert "already up to date" in result.message


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
    assert "updated old -> new" in result.message
    assert events == ["stop", "start"]


def test_stash_conflict_fails_before_restart(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path, revision="old", webui_revision="old")
    revisions = iter(["old", "new"])

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok(" M x.py")
        if command[:3] == ["git", "stash", "pop"]:
            return _err("conflict")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(revisions))
        return _ok("")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(
        _instance(), stash=True, runner=runner, root=tmp_path, stop=stop, start=start
    )

    assert not result.ok
    assert "conflict" in result.message.lower()
    assert events == []


def test_no_restart_skips_server(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path)

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok("")
        return _ok("samesha") if command[:2] == ["git", "rev-parse"] else _ok("")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(
        _instance(), restart=False, runner=runner, root=tmp_path, stop=stop, start=start
    )

    assert result.ok, result.message
    assert "not restarted" in result.message
    assert events == []


def test_parse_args_update_flags() -> None:
    args = parse_args(["update", "--discard"])

    assert args.area == "update"
    assert args.discard is True
    assert args.stash is False
    assert args.no_restart is False


def test_parse_args_update_rejects_discard_with_stash() -> None:
    with pytest.raises(SystemExit):
        parse_args(["update", "--discard", "--stash"])


def test_dispatch_update_passes_flags_through() -> None:
    captured: dict[str, object] = {}

    def fake_run_update(
        instance: ServerInstance,
        *,
        discard: bool,
        stash: bool,
        restart: bool,
        stop: Callable[..., CommandResult],
        start: Callable[..., CommandResult],
        service_name: str,
    ) -> CommandResult:
        captured.update(discard=discard, stash=stash, restart=restart, service_name=service_name)
        return CommandResult(ok=True, message="done", instance=instance)

    def noop(instance: ServerInstance) -> CommandResult:
        return CommandResult(ok=True, message="ok", instance=instance)

    args = parse_args(["update", "--stash", "--no-restart"])
    result = dispatch_update_command(
        args,
        resolve=lambda **_kwargs: _instance(),
        stop=noop,
        start=noop,
        run_update_fn=fake_run_update,
    )

    assert result.ok
    assert captured == {
        "discard": False,
        "stash": True,
        "restart": False,
        "service_name": "vbot",
    }


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
    assert "already up to date" in result.message
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
    assert "unpacking the prebuilt WebUI failed" in result.message
    assert (dist / "index.html").is_file()
    assert not (tmp_path / "webui" / "dist.staging").exists()
    assert events == []


def test_dependency_failure_is_retried_after_head_already_advanced(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("before", encoding="utf-8")
    _write_state(tmp_path, revision="old", webui_revision="old")
    first_revisions = iter(["old", "new"])

    def first_handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(first_revisions))
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "pull"]:
            (tmp_path / "pyproject.toml").write_text("after", encoding="utf-8")
            return _ok()
        if "pip" in command:
            return _err("pip failed")
        return _ok()

    events, stop, start = _recording_restart()
    first = run_update(
        _instance(),
        runner=ScriptedRunner(first_handler),
        root=tmp_path,
        stop=stop,
        start=start,
    )
    assert not first.ok
    assert "dependency update failed" in first.message

    def retry_handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "rev-parse"]:
            return _ok("new")
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:3] == ["git", "diff", "--quiet"]:
            return _ok()
        return _ok()

    retry_runner = ScriptedRunner(retry_handler)
    retried = run_update(_instance(), runner=retry_runner, root=tmp_path, stop=stop, start=start)

    assert retried.ok, retried.message
    assert retry_runner.ran("-m", "pip", "install", "-e", ".[server,cli]")


def test_release_asset_preflight_can_be_retried_without_poisoning_checkout(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    dist = tmp_path / "webui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("old", encoding="utf-8")
    _write_state(tmp_path, track="release", revision="old", webui_revision="old")

    def first_handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _err()
        if command[:2] == ["git", "rev-parse"]:
            return _ok("old")
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "describe"]:
            return _err("not the current release tag")
        raise AssertionError(f"checkout must not advance without the asset: {command}")

    first = run_update(
        _instance(),
        runner=ScriptedRunner(first_handler),
        root=tmp_path,
        restart=False,
        latest_release=lambda: ReleaseInfo("v2", None),
    )
    assert not first.ok
    assert "left unchanged" in first.message


def test_stash_is_restored_when_git_pull_fails(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path, revision="old", webui_revision="old")

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "rev-parse"]:
            return _ok("old")
        if command[:2] == ["git", "status"]:
            return _ok(" M local.py")
        if command[:2] == ["git", "pull"]:
            return _err("offline")
        return _ok()

    runner = ScriptedRunner(handler)
    result = run_update(_instance(), stash=True, runner=runner, root=tmp_path)

    assert not result.ok
    assert "local changes reapplied" in result.message
    assert runner.ran("git", "stash", "pop", "stash@{0}")


def test_desktop_client_update_keeps_exact_shape_and_never_starts_server(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("before", encoding="utf-8")
    _write_state(
        tmp_path,
        revision="old",
        shape="desktop-client",
        groups=("cli", "desktop"),
    )
    revisions = iter(["old", "new"])

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(revisions))
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "pull"]:
            (tmp_path / "pyproject.toml").write_text("after", encoding="utf-8")
        return _ok()

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        platform_name="posix",
    )

    assert result.ok, result.message
    assert runner.ran("-m", "pip", "install", "-e", ".[cli,desktop]")
    assert not any("npm" in call for call in runner.calls)
    assert "not applicable (desktop-client install)" in result.message
    assert events == []


def test_windows_desktop_update_refreshes_shortcut_to_gui_launcher(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("before", encoding="utf-8")
    setup_script = tmp_path / "scripts" / "setup.ps1"
    setup_script.parent.mkdir()
    setup_script.write_text("# shortcut mode", encoding="utf-8")
    dist = tmp_path / "webui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    scripts_dir = tmp_path / ".venv" / "Scripts"
    scripts_dir.mkdir(parents=True)
    python_executable = scripts_dir / "python.exe"
    python_executable.write_bytes(b"")
    desktop_launcher = scripts_dir / "vbot-desktop.exe"
    desktop_launcher.write_bytes(b"")
    _write_state(
        tmp_path,
        revision="old",
        shape="server-desktop",
        python_executable=str(python_executable),
        webui_revision="old",
    )
    revisions = iter(["old", "new"])

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(revisions))
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "pull"]:
            (tmp_path / "pyproject.toml").write_text("after", encoding="utf-8")
        return _ok("")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()

    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        platform_name="nt",
    )

    assert result.ok, result.message
    assert "desktop shortcut refreshed" in result.message
    assert any(
        call[:7]
        == [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
        ]
        and call[-2:] == ["-DesktopShortcutTarget", str(desktop_launcher.resolve())]
        for call in runner.calls
    )
    assert events == ["stop", "start"]


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
