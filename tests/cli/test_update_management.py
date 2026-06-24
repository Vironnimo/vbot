"""Tests for the local `vbot update` command logic."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import respx

from cli.main import dispatch_update_command
from cli.parser import parse_args
from cli.server_management import CommandResult, ServerInstance
from cli.update_management import CommandRun, ReleaseInfo, run_update


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

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok(" M core/foo.py")
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
    assert "dependencies reinstalled ([dev])" in result.message
    assert "webui rebuilt" in result.message
    assert runner.ran("-m", "pip", "install", "-e", ".[dev]")
    assert any("npm" in call for call in runner.calls)
    assert events == ["stop", "start"]


def test_release_track_requires_webui_asset(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
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
    assert runner.ran("git", "checkout", "--force", "v9.9.9")
    assert events == []


@respx.mock
def test_release_track_downloads_prebuilt_webui(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    asset_url = "https://example.com/webui-dist.tar.gz"
    respx.get(asset_url).mock(return_value=httpx.Response(200, content=_webui_tar_bytes()))
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
        latest_release=lambda: ReleaseInfo(tag="v9.9.9", webui_asset_url=asset_url),
    )

    assert result.ok, result.message
    assert (tmp_path / "webui" / "dist" / "index.html").is_file()
    assert "updated old -> new" in result.message
    assert events == ["stop", "start"]


def test_stash_conflict_fails_before_restart(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
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
    ) -> CommandResult:
        captured.update(discard=discard, stash=stash, restart=restart)
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
    assert captured == {"discard": False, "stash": True, "restart": False}
