"""Tests for update management."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import cli.update_management as update_management
from cli import _update_assets
from cli._update_types import UpdateResult, _SnapshotStep
from cli.install_state import (
    dependency_digest,
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
from core.database import MarkerEntry, write_bootstrap_marker
from core.database.marker import register_database
from tests.cli.update_management_test_support import (
    ScriptedRunner,
    _err,
    _instance,
    _ok,
    _recording_restart,
    _recording_snapshot,
    _upstream,
    _write_state,
)


def test_update_refuses_non_git_checkout(tmp_path: Path) -> None:
    def runner(command: list[str], cwd: Path) -> CommandRun:
        raise AssertionError(f"runner should not run before the git check: {command}")

    events, stop, start = _recording_restart()
    result = run_update(_instance(), runner=runner, root=tmp_path, stop=stop, start=start)

    assert not result.ok
    assert events == []


def _stopped_instance(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> ServerInstance:
    monkeypatch.setattr(
        update_management,
        "probe_health",
        lambda _instance: HealthProbeResult(reachable=False, is_vbot=False),
    )
    return ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=data_dir,
        url="http://127.0.0.1:8420",
        log_path=data_dir / "server.log",
    )


def test_update_snapshot_preflight_captures_current_format_data_when_server_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_bootstrap_marker(tmp_path)
    manager = ChatSessionManager(tmp_path)
    manager.create("coder", session_id="session-one").append(ChatMessage.user("protected"))
    manager.close()

    # An updater still runs the previous release after an offline conversion.
    # It must copy the databases without opening them through its own owners.
    def refuse_open(*_args, **_kwargs):
        raise AssertionError("the updater must not open a database to back it up")

    monkeypatch.setattr("core.database.database.open_database", refuse_open)
    monkeypatch.setattr("core.database.database.open_offline_database", refuse_open)
    instance = _stopped_instance(tmp_path, monkeypatch)

    result = update_management._ensure_update_data_snapshot(instance)

    assert result.ok is True
    assert "pre-update data snapshot:" in result.message


def test_update_snapshot_preflight_skips_a_bootstrap_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_bootstrap_marker(tmp_path)

    result = update_management._ensure_update_data_snapshot(
        _stopped_instance(tmp_path, monkeypatch)
    )

    assert result.ok is True
    assert result.message == ""


def test_update_snapshot_preflight_refuses_databases_without_a_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "sessions.db").write_bytes(b"")

    result = update_management._ensure_update_data_snapshot(
        _stopped_instance(tmp_path, monkeypatch)
    )

    assert result.ok is False
    assert "without a current-format data-store marker" in result.message


def test_update_snapshot_preflight_refuses_a_missing_registered_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_bootstrap_marker(tmp_path)
    register_database(tmp_path, "sessions", MarkerEntry(database_id="a" * 32, format_generation=1))
    register_database(
        tmp_path, "ext.gone.state", MarkerEntry(database_id="b" * 32, format_generation=1)
    )

    result = update_management._ensure_update_data_snapshot(
        _stopped_instance(tmp_path, monkeypatch)
    )

    assert result.ok is False
    assert "the registered database sessions has no file; starting vBot restores it" in (
        result.message
    )
    assert "`vbot data-store unregister ext.gone.state --yes` releases it" in result.message


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
    snapshots, snapshot = _recording_snapshot()
    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        data_snapshot_fn=snapshot,
    )

    assert not result.ok
    assert "--discard" in result.message
    assert events == []
    assert snapshots == []


def test_update_discard_resets_then_updates(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path)

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok(" M x.py")
        if command[:2] == ["git", "rev-list"]:
            return _upstream()
        return _ok("samesha") if command[:2] == ["git", "rev-parse"] else _ok("")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(
        _instance(), discard=True, runner=runner, root=tmp_path, stop=stop, start=start
    )

    assert result.ok, result.message
    assert runner.ran("git", "reset", "--hard", "HEAD")
    assert events == ["stop", "start"]


def _applied_checkout(root: Path) -> None:
    """A branch checkout whose dependencies and WebUI match its current commit."""

    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("same", encoding="utf-8")
    dist = root / "webui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    _write_state(root, webui_revision="samesha")


@pytest.mark.parametrize("ahead", [0, 2], ids=["current", "local-commits-ahead"])
@pytest.mark.parametrize("caller", ["human", "no-restart", "agent"])
def test_an_update_with_nothing_new_neither_snapshots_nor_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ahead: int, caller: str
) -> None:
    _applied_checkout(tmp_path)

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "rev-list"]:
            return _upstream(ahead=ahead)
        return _ok("samesha") if command[:2] == ["git", "rev-parse"] else _ok("")

    def schedule(instance: ServerInstance, *, service_name: str) -> CommandResult:
        raise AssertionError("an unchanged installation must not schedule a restart")

    monkeypatch.setattr(update_management, "has_vbot_run_context", lambda: caller == "agent")
    monkeypatch.setattr(update_management, "schedule_server_restart", schedule)
    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    snapshots, snapshot = _recording_snapshot()
    result = run_update(
        _instance(),
        restart=caller != "no-restart",
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        data_snapshot_fn=snapshot,
    )

    assert isinstance(result, UpdateResult)
    assert result.ok, result.message
    assert result.restart_state == "unchanged"
    assert "already up to date at samesha" in result.message
    assert snapshots == []
    assert events == []
    assert runner.ran("git", "fetch")
    assert not runner.ran("git", "merge")
    assert not runner.ran("pip")


def test_new_upstream_commits_are_applied_after_the_snapshot(tmp_path: Path) -> None:
    _applied_checkout(tmp_path)
    heads = iter(["samesha", "newsha"])
    order: list[str] = []

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(heads))
        if command[:2] == ["git", "rev-list"]:
            return _upstream(behind=3)
        if command[:2] == ["git", "merge"]:
            order.append("merge")
        return _ok()

    def snapshot(instance: ServerInstance) -> _SnapshotStep:
        order.append("snapshot")
        return _SnapshotStep(True, "pre-update data snapshot: s-1", "s-1")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        data_snapshot_fn=snapshot,
    )

    assert result.ok, result.message
    assert "updated samesha -> newsha" in result.message
    assert order == ["snapshot", "merge"]
    assert runner.ran("git", "merge", "--ff-only", "@{upstream}")
    assert events == ["stop", "start"]
    state = read_install_state(tmp_path)
    assert state is not None
    assert state.applied_revision == "newsha"


@pytest.mark.parametrize(
    ("fetch", "comparison", "reason"),
    [
        (_err("could not resolve host"), _upstream(behind=1), "'git fetch' failed"),
        (
            _ok(),
            _upstream(ahead=1, behind=2),
            "diverged from its upstream (1 local and 2 upstream commits)",
        ),
        (_ok(), _err("no upstream configured for branch 'main'"), "no upstream configured"),
    ],
    ids=["offline", "diverged", "no-upstream"],
)
def test_an_update_that_cannot_fast_forward_is_refused_before_the_snapshot(
    tmp_path: Path, fetch: CommandRun, comparison: CommandRun, reason: str
) -> None:
    _applied_checkout(tmp_path)

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "rev-parse"]:
            return _ok("samesha")
        if command[:2] == ["git", "fetch"]:
            return fetch
        if command[:2] == ["git", "rev-list"]:
            return comparison
        raise AssertionError(f"unexpected command after refusal: {command}")

    events, stop, start = _recording_restart()
    snapshots, snapshot = _recording_snapshot()
    result = run_update(
        _instance(),
        runner=ScriptedRunner(handler),
        root=tmp_path,
        stop=stop,
        start=start,
        data_snapshot_fn=snapshot,
    )

    assert not result.ok
    assert reason in result.message
    assert snapshots == []
    assert events == []


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
        if command[:2] == ["git", "rev-list"]:
            return _upstream()
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
        if command[:2] == ["git", "rev-list"]:
            return _upstream()
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
        if command[:2] == ["git", "rev-list"]:
            return _upstream()
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
        if command[:2] == ["git", "rev-list"]:
            return _upstream(behind=1)
        if command[:2] == ["git", "merge"]:
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
        if command[0] != "node":
            builds.append(command)
        return _ok()

    result = _update_assets._refresh_dev_webui(runner, tmp_path, before, after)

    assert result.ok
    assert builds == (
        [_update_assets._npm_command(["ci"]), _update_assets._npm_command(["run", "build"])]
        if rebuild
        else []
    )


def test_dev_webui_reuses_installed_packages_while_their_inputs_are_unchanged(
    tmp_path: Path,
) -> None:
    webui = tmp_path / "webui"
    webui.mkdir()
    (webui / "package.json").write_text('{"name": "webui"}', encoding="utf-8")
    (webui / "package-lock.json").write_text('{"lock": 1}', encoding="utf-8")
    npm_calls: list[list[str]] = []

    def runner(command: list[str], cwd: Path) -> CommandRun:
        if command[:3] == ["git", "diff", "--quiet"]:
            return _err()
        if command == ["node", "--version"]:
            return _ok("v22.0.0")
        npm_calls.append(command)
        if command == _update_assets._npm_command(["ci"]):
            (webui / "node_modules").mkdir(exist_ok=True)
        else:
            (webui / "dist").mkdir(exist_ok=True)
            (webui / "dist" / "index.html").write_text("built", encoding="utf-8")
        return _ok()

    first = _update_assets._refresh_dev_webui(runner, tmp_path, None, "first")
    second = _update_assets._refresh_dev_webui(runner, tmp_path, "first", "second")

    assert first.ok, first.message
    assert second.ok, second.message
    npm_ci = _update_assets._npm_command(["ci"])
    npm_build = _update_assets._npm_command(["run", "build"])
    assert npm_calls == [npm_ci, npm_build, npm_build]


def _write_build(root: Path, content: str) -> None:
    """Write a WebUI build and the build of one Extension page."""

    dist = root / "webui" / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(content, encoding="utf-8")
    page = root / "resources" / "extensions" / "swarm"
    (page / "ui").mkdir(parents=True, exist_ok=True)
    (page / "ui" / "page.html").write_text("page source", encoding="utf-8")
    (page / "web").mkdir(exist_ok=True)
    (page / "web" / "index.html").write_text(content, encoding="utf-8")


def _break_build(root: Path) -> None:
    """Leave every output tree the way a build failing part-way does."""

    pages = (root / "resources" / "extensions").glob("*/ui/page.html")
    for tree in [root / "webui" / "dist", *(page.parent.parent / "web" for page in pages)]:
        shutil.rmtree(tree, ignore_errors=True)
        tree.mkdir(parents=True)
        (tree / "partial.js").write_text("partial", encoding="utf-8")


def _checkout_files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in root.rglob("*")
        if path.is_file() and ".previous-build" not in path.parts
    }


def test_dev_webui_build_failure_keeps_the_previous_build(tmp_path: Path) -> None:
    _write_build(tmp_path, "old")
    added = tmp_path / "resources" / "extensions" / "added" / "ui"
    added.mkdir(parents=True)
    (added / "page.html").write_text("new page source", encoding="utf-8")
    before = _checkout_files(tmp_path)

    def runner(command: list[str], cwd: Path) -> CommandRun:
        if command[:3] == ["git", "diff", "--quiet"]:
            return _err()
        if command == _update_assets._npm_command(["run", "build"]):
            _break_build(tmp_path)
            return _err("render failed")
        return _ok()

    result = _update_assets._refresh_dev_webui(runner, tmp_path, "old", "new")

    assert not result.ok
    assert result.message == "webui build failed: render failed\nthe previous WebUI was kept"
    assert _checkout_files(tmp_path) == before
    assert not (tmp_path / "webui" / ".previous-build").exists()


def test_dev_webui_interrupted_build_is_restored_before_the_next_build(tmp_path: Path) -> None:
    _write_build(tmp_path, "old")
    before = _checkout_files(tmp_path)

    def interrupted(command: list[str], cwd: Path) -> CommandRun:
        if command[:3] == ["git", "diff", "--quiet"]:
            return _err()
        if command == _update_assets._npm_command(["run", "build"]):
            _break_build(tmp_path)
            raise KeyboardInterrupt
        return _ok()

    with pytest.raises(KeyboardInterrupt):
        _update_assets._refresh_dev_webui(interrupted, tmp_path, "old", "new")
    assert _checkout_files(tmp_path) != before
    seen_by_build: list[dict[str, str]] = []

    def resumed(command: list[str], cwd: Path) -> CommandRun:
        if command[:3] == ["git", "diff", "--quiet"]:
            return _err()
        if command == _update_assets._npm_command(["run", "build"]):
            seen_by_build.append(_checkout_files(tmp_path))
            _write_build(tmp_path, "new")
        return _ok()

    result = _update_assets._refresh_dev_webui(resumed, tmp_path, "old", "new")

    assert result.ok, result.message
    assert seen_by_build == [before]
    assert (tmp_path / "webui" / "dist" / "index.html").read_text(encoding="utf-8") == "new"
    assert not (tmp_path / "webui" / ".previous-build").exists()


@pytest.mark.parametrize("leftover", ["other_revision", "incomplete"])
def test_dev_webui_discards_a_copy_it_cannot_trust(tmp_path: Path, leftover: str) -> None:
    _write_build(tmp_path, "older")
    _update_assets._save_previous_build(
        tmp_path, "older" if leftover == "other_revision" else "current"
    )
    if leftover == "incomplete":
        (tmp_path / "webui" / ".previous-build" / "trees.json").unlink()
    _write_build(tmp_path, "current")

    result = _update_assets._refresh_dev_webui(
        lambda _command, _cwd: _ok(), tmp_path, "current", "current"
    )

    assert result.ok, result.message
    assert (tmp_path / "webui" / "dist" / "index.html").read_text(encoding="utf-8") == "current"
    assert not (tmp_path / "webui" / ".previous-build").exists()


def test_dev_webui_reports_which_npm_step_failed(tmp_path: Path) -> None:
    def runner(command: list[str], cwd: Path) -> CommandRun:
        if command == _update_assets._npm_command(["ci"]):
            return CommandRun(returncode=1, stdout="", stderr="lock mismatch")
        return _ok()

    result = _update_assets._refresh_dev_webui(runner, tmp_path, None, "target")

    assert not result.ok
    assert result.message == (
        "webui dependency install failed: lock mismatch\nthe previous WebUI was kept"
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
        if command[:2] == ["git", "rev-list"]:
            return _upstream(behind=1)
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
        if command[:2] == ["git", "rev-list"]:
            return _upstream()
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


def _dev_update_handler(
    tmp_path: Path, *, pyproject_after: str, search_runtime: CommandRun | None = None
) -> Callable[[list[str]], CommandRun]:
    """Answer one dev update from ``old`` to ``new`` whose merge rewrites pyproject.toml."""

    revisions = iter(["old", "new"])

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(revisions))
        if command[:2] == ["git", "rev-list"]:
            return _upstream(behind=1)
        if command[:2] == ["git", "merge"]:
            (tmp_path / "pyproject.toml").write_bytes(pyproject_after.encode())
            return _ok()
        if search_runtime is not None and command[1:] == ["-m", "cli.search_runtime"]:
            return search_runtime
        return _ok()

    return handler


def test_version_bump_does_not_reinstall_dependencies(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    project = '[project]\nname = "vbot"\nversion = "1.0.0"\ndependencies = ["httpx"]\n'
    (tmp_path / "pyproject.toml").write_bytes(project.encode())
    dist = tmp_path / "webui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("existing build", encoding="utf-8")
    _write_state(tmp_path, revision="old", webui_revision="old")

    runner = ScriptedRunner(
        _dev_update_handler(tmp_path, pyproject_after=project.replace("1.0.0", "1.0.1"))
    )
    events, stop, start = _recording_restart()
    result = run_update(_instance(), runner=runner, root=tmp_path, stop=stop, start=start)

    assert result.ok, result.message
    assert not runner.ran("-m", "pip")
    assert events == ["stop", "start"]


def test_installed_dependencies_are_recorded_when_a_later_step_fails(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("before", encoding="utf-8")
    _write_state(tmp_path, revision="old", webui_revision="old")

    runner = ScriptedRunner(
        _dev_update_handler(tmp_path, pyproject_after="after", search_runtime=_err("offline"))
    )
    events, stop, start = _recording_restart()
    failed = run_update(_instance(), runner=runner, root=tmp_path, stop=stop, start=start)

    assert not failed.ok
    assert runner.ran("-m", "pip", "install")
    state = read_install_state(tmp_path)
    assert state is not None
    assert state.dependency_digest == dependency_digest(tmp_path)
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
        if command[:2] == ["git", "rev-list"]:
            return _upstream(behind=1)
        if command[:2] == ["git", "merge"]:
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
        if command[:2] == ["git", "rev-list"]:
            return _upstream()
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


def test_stash_is_restored_when_the_fast_forward_fails(tmp_path: Path) -> None:
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
        if command[:2] == ["git", "rev-list"]:
            return _upstream(behind=1)
        if command[:2] == ["git", "merge"]:
            return _err("not possible to fast-forward")
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
        if command[:2] == ["git", "rev-list"]:
            return _upstream(behind=1)
        if command[:2] == ["git", "merge"]:
            return _err("not possible to fast-forward")
        return _ok()

    runner = ScriptedRunner(handler)
    result = run_update(_instance(), stash=True, runner=runner, root=tmp_path)

    assert not result.ok
    assert not runner.ran("git", "stash", "apply")
    assert not runner.ran("git", "stash", "pop")


@pytest.mark.parametrize("restart_ok", [True, False])
def test_a_failed_restart_names_the_unrestored_snapshot_and_the_previous_revision(
    tmp_path: Path, restart_ok: bool
) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path)
    heads = iter(("oldrevision1", "newrevision2"))

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return _ok(next(heads))
        if command[:2] == ["git", "rev-list"]:
            return _upstream(behind=1)
        return _ok("")

    def start(instance: ServerInstance) -> CommandResult:
        return CommandResult(ok=restart_ok, message="start result", instance=instance)

    _events, stop, _start = _recording_restart()
    result = run_update(
        _instance(),
        runner=ScriptedRunner(handler),
        root=tmp_path,
        stop=stop,
        start=start,
        data_snapshot_fn=lambda _instance: _SnapshotStep(
            True, "pre-update data snapshot: s-1", "s-1"
        ),
    )

    assert result.ok is restart_ok
    note = "pre-update data snapshot s-1 was not restored automatically"
    if restart_ok:
        assert note not in result.message
    else:
        assert note in result.message
        assert "check out oldrevisi" in result.message
        assert "`vbot data-store snapshot restore s-1 --all --yes`" in result.message
