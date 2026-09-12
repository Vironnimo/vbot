"""Tests for update management."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import cli.update_management as update_management
from cli import _update_assets
from cli.install_state import (
    read_install_state,
)
from cli.main import dispatch_update_command
from cli.parser import parse_args
from cli.server_management import CommandResult, HealthProbeResult, ServerInstance
from cli.update_management import (
    CommandRun,
    ReleaseInfo,
    _default_runner,
    run_update,
)
from core.chat import ChatMessage, ChatSessionManager
from core.sessions.format import write_bootstrap_marker
from tests.cli.update_management_test_support import (
    ScriptedRunner,
    _err,
    _instance,
    _ok,
    _recording_restart,
    _write_state,
)


def test_update_refuses_non_git_checkout(tmp_path: Path) -> None:
    def runner(command: list[str], cwd: Path) -> CommandRun:
        raise AssertionError(f"runner should not run before the git check: {command}")

    events, stop, start = _recording_restart()
    result = run_update(_instance(), runner=runner, root=tmp_path, stop=stop, start=start)

    assert not result.ok
    assert events == []


def test_update_snapshot_preflight_captures_current_format_store_when_server_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_bootstrap_marker(tmp_path)
    manager = ChatSessionManager(tmp_path)
    manager.create("coder", session_id="session-one").append(ChatMessage.user("protected"))
    manager.close()
    instance = ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=tmp_path,
        url="http://127.0.0.1:8420",
        log_path=tmp_path / "server.log",
    )
    monkeypatch.setattr(
        update_management,
        "probe_health",
        lambda _instance: HealthProbeResult(reachable=False, is_vbot=False),
    )

    result = update_management._ensure_update_session_snapshot(instance)

    assert result.ok is True
    assert "pre-update Session snapshot:" in result.message


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
    assert events == ["stop", "start"]


def test_agent_update_schedules_internal_restart_without_inline_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path)

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok("")
        return _ok("samesha") if command[:2] == ["git", "rev-parse"] else _ok("")

    scheduled: list[tuple[ServerInstance, str]] = []

    def schedule(instance: ServerInstance, *, service_name: str) -> CommandResult:
        scheduled.append((instance, service_name))
        return CommandResult(ok=True, message="restart scheduled", instance=instance)

    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "main")
    monkeypatch.setenv("VBOT_RUN_SESSION_ID", "session-1")
    monkeypatch.setattr(update_management, "schedule_server_restart", schedule)
    events, stop, start = _recording_restart()

    result = run_update(
        _instance(),
        runner=ScriptedRunner(handler),
        root=tmp_path,
        stop=stop,
        start=start,
    )

    assert result.ok, result.message
    assert scheduled == [(_instance(), "vbot")]
    assert events == []


def test_update_restarts_the_installer_recorded_server_target(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    recorded_data_dir = tmp_path / "custom-data"
    _write_state(
        tmp_path,
        server_host="0.0.0.0",
        server_port=9123,
        server_data_directory=str(recorded_data_dir),
    )

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok("")
        return _ok("samesha") if command[:2] == ["git", "rev-parse"] else _ok("")

    resolved_targets: list[dict[str, object]] = []

    def resolve(**target: object) -> ServerInstance:
        resolved_targets.append(target)
        assert isinstance(target["host"], str)
        assert isinstance(target["port"], int)
        data_dir = Path(str(target["data_dir"]))
        return ServerInstance(
            host=target["host"],
            port=target["port"],
            data_dir=data_dir,
            url=f"http://{target['host']}:{target['port']}",
            log_path=data_dir / "logs" / "today.log",
        )

    restarted_targets: list[ServerInstance] = []

    def stop(instance: ServerInstance) -> CommandResult:
        restarted_targets.append(instance)
        return CommandResult(ok=True, message="stopped", instance=instance)

    def start(instance: ServerInstance) -> CommandResult:
        restarted_targets.append(instance)
        return CommandResult(ok=True, message="started", instance=instance)

    result = run_update(
        _instance(),
        runner=ScriptedRunner(handler),
        root=tmp_path,
        resolve=resolve,
        stop=stop,
        start=start,
    )

    assert result.ok, result.message
    assert resolved_targets == [
        {
            "host": "0.0.0.0",
            "port": 9123,
            "data_dir": str(recorded_data_dir),
        }
    ]
    assert [(target.host, target.port, target.data_dir) for target in restarted_targets] == [
        ("0.0.0.0", 9123, recorded_data_dir),
        ("0.0.0.0", 9123, recorded_data_dir),
    ]


def test_update_explicit_target_fields_override_the_installation_manifest(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(
        tmp_path,
        server_host="0.0.0.0",
        server_port=9123,
        server_data_directory=str(tmp_path / "recorded-data"),
    )

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok("")
        return _ok("samesha") if command[:2] == ["git", "rev-parse"] else _ok("")

    explicit_data_dir = tmp_path / "explicit-data"
    resolved_targets: list[dict[str, object]] = []

    def resolve(**target: object) -> ServerInstance:
        resolved_targets.append(target)
        assert isinstance(target["host"], str)
        assert isinstance(target["port"], int)
        data_dir = Path(str(target["data_dir"]))
        return ServerInstance(
            host=target["host"],
            port=target["port"],
            data_dir=data_dir,
            url=f"http://{target['host']}:{target['port']}",
            log_path=data_dir / "logs" / "today.log",
        )

    result = run_update(
        _instance(),
        runner=ScriptedRunner(handler),
        root=tmp_path,
        resolve=resolve,
        host="127.0.0.2",
        port=9456,
        data_dir=explicit_data_dir,
        restart=False,
    )

    assert result.ok, result.message
    assert resolved_targets == [
        {
            "host": "127.0.0.2",
            "port": 9456,
            "data_dir": explicit_data_dir,
        }
    ]
    assert result.instance.host == "127.0.0.2"
    assert result.instance.port == 9456
    assert result.instance.data_dir == explicit_data_dir


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
    assert runner.ran("-m", "pip", "install", "-e", ".[server,cli]")
    assert any("npm" in call for call in runner.calls)
    assert events == ["stop", "start"]


@pytest.mark.parametrize(
    ("changed_path", "change", "rebuild"),
    [
        ("resources/extensions/swarm/ui/ProfileEditor.svelte", "edit", True),
        ("resources/extensions/other/ui/nested/component.js", "edit", True),
        ("resources/extensions/new/ui/page.html", "add", True),
        ("resources/extensions/old/ui/page.html", "delete", True),
        ("webui/src/lib/shared.js", "edit", True),
        ("tests/fixtures/extension-pages/alpha/ui/page.html", "edit", True),
        ("resources/extensions/swarm/backend.py", "edit", False),
        ("core/example.py", "edit", False),
    ],
)
def test_dev_webui_detects_build_inputs_with_git(
    tmp_path: Path, changed_path: str, change: str, rebuild: bool
) -> None:
    def git(*args: str) -> str:
        result = _default_runner(["git", *args], tmp_path)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    git("init", "--quiet")
    (tmp_path / "README.md").write_text("test repository", encoding="utf-8")
    source = tmp_path / changed_path
    source.parent.mkdir(parents=True, exist_ok=True)
    if change != "add":
        source.write_text("before", encoding="utf-8")
    git("add", ".")
    before = git("write-tree")
    if change == "delete":
        source.unlink()
    else:
        source.write_text("after", encoding="utf-8")
    git("add", "--all")
    after = git("write-tree")
    dist = tmp_path / "webui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("existing build", encoding="utf-8")
    builds: list[list[str]] = []

    def runner(command: list[str], cwd: Path) -> CommandRun:
        if command[0] == "git":
            return _default_runner(command, cwd)
        assert cwd == tmp_path / "webui"
        builds.append(command)
        return _ok()

    result = _update_assets._refresh_dev_webui(runner, tmp_path, before, after)

    assert result.ok
    assert builds == (
        [_update_assets._npm_command(["ci"]), _update_assets._npm_command(["run", "build"])]
        if rebuild
        else []
    )


def test_dev_webui_build_failure_preserves_revision_for_retry(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("unchanged", encoding="utf-8")
    dist = tmp_path / "webui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("existing build", encoding="utf-8")
    _write_state(tmp_path, revision="old", webui_revision="old")
    revisions = iter(["old", "new", "new", "new"])
    build_results = iter([_err("build failed"), _ok()])

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(revisions))
        if command[:3] == ["git", "diff", "--quiet"]:
            assert command[3:5] == ["old", "new"]
            return _err()
        if command == _update_assets._npm_command(["run", "build"]):
            return next(build_results)
        return _ok()

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    failed = run_update(_instance(), runner=runner, root=tmp_path, stop=stop, start=start)

    assert not failed.ok
    state = read_install_state(tmp_path)
    assert state is not None
    assert state.webui_revision == "old"
    assert events == []

    retried = run_update(_instance(), runner=runner, root=tmp_path, stop=stop, start=start)

    assert retried.ok, retried.message
    state = read_install_state(tmp_path)
    assert state is not None
    assert state.webui_revision == "new"
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
        if command[:3] == ["git", "stash", "create"]:
            return _ok("updater-stash-object")
        if command[:3] == ["git", "stash", "apply"]:
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
    assert events == []


def test_parse_args_update_flags() -> None:
    args = parse_args(["update", "--discard"])

    assert args.area == "update"
    assert args.host is None
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
        resolve: Callable[..., ServerInstance],
        host: str | None,
        port: int | None,
        data_dir: str | None,
    ) -> CommandResult:
        captured.update(
            discard=discard,
            stash=stash,
            restart=restart,
            service_name=service_name,
            resolve=resolve,
            host=host,
            port=port,
            data_dir=data_dir,
        )
        return CommandResult(ok=True, message="done", instance=instance)

    def noop(instance: ServerInstance) -> CommandResult:
        return CommandResult(ok=True, message="ok", instance=instance)

    def resolve_target(**_target: object) -> ServerInstance:
        return _instance()

    args = parse_args(["update", "--stash", "--no-restart"])
    result = dispatch_update_command(
        args,
        resolve=resolve_target,
        stop=noop,
        start=noop,
        run_update_fn=fake_run_update,
    )

    assert result.ok
    assert captured.pop("resolve") is resolve_target
    assert captured == {
        "discard": False,
        "stash": True,
        "restart": False,
        "service_name": "vbot",
        "host": None,
        "port": None,
        "data_dir": None,
    }


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
        platform_name="nt",
    )
    assert not first.ok
    expected_recovery = (
        f"Set-Location -LiteralPath '{tmp_path.resolve()}'; & '{sys.executable}' -m cli.main update"
    )
    assert f"resume update: {expected_recovery}" in first.message

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
        if command[:3] == ["git", "stash", "create"]:
            return _ok("updater-stash-object")
        if command[:2] == ["git", "pull"]:
            return _err("offline")
        return _ok()

    runner = ScriptedRunner(handler)
    result = run_update(_instance(), stash=True, runner=runner, root=tmp_path)

    assert not result.ok
    retained_refs = [
        call[2]
        for call in runner.calls
        if call[:2] == ["git", "update-ref"] and len(call) == 4 and call[2] != "-d"
    ]
    assert len(retained_refs) == 1
    retained_ref = retained_refs[0]
    assert retained_ref.startswith("refs/vbot/update-stashes/")
    assert runner.ran("git", "stash", "apply", "--index", retained_ref)
    assert runner.ran("git", "update-ref", "-d", retained_ref, "updater-stash-object")
    assert not runner.ran("stash@{0}")


def test_stash_flag_does_not_restore_an_existing_stash_when_changes_disappear(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path, revision="old", webui_revision="old")

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "rev-parse"]:
            return _ok("old")
        if command[:2] == ["git", "status"]:
            return _ok(" M local.py")
        if command[:3] == ["git", "stash", "create"]:
            return _ok("")
        if command[:2] == ["git", "pull"]:
            return _err("offline")
        return _ok()

    runner = ScriptedRunner(handler)
    result = run_update(_instance(), stash=True, runner=runner, root=tmp_path)

    assert not result.ok
    assert not runner.ran("git", "stash", "apply")
    assert not runner.ran("git", "stash", "pop")
