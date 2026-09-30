"""``vbot update`` on a branch checkout: preflight, snapshot, code, dependencies and restart."""

from __future__ import annotations

import shlex
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import cli.update_management as update_management
from cli import _update_assets, autostart_management
from cli._update_types import UpdateResult, _SnapshotStep
from cli.install_state import dependency_digest, read_install_state
from cli.main import dispatch_update_command
from cli.parser import parse_args
from cli.server_management import CommandResult, ServerInstance, ServerState
from cli.update_management import (
    UNKNOWN_VBOT_VERSION,
    CommandRun,
    _default_runner,
    read_checkout_version,
    run_update,
)
from core.chat import ChatMessage, ChatSessionManager
from core.database import MarkerEntry, write_bootstrap_marker
from core.database.marker import register_database
from core.database.snapshots import snapshot_root
from tests.cli.update_management_test_support import (
    ScriptedRunner,
    _err,
    _instance,
    _ok,
    _recording_restart,
    _recording_snapshot,
    _upstream,
    _write_state,
    checkout,
    only_reads,
    write_webui_build,
)


@pytest.fixture(autouse=True)
def no_running_launchers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the Windows launcher guards away from this machine's real processes."""

    monkeypatch.setattr(update_management, "_running_process_id", lambda *_args, **_kw: None)


def test_update_refuses_non_git_checkout(tmp_path: Path) -> None:
    def runner(command: list[str], cwd: Path) -> CommandRun:
        raise AssertionError(f"runner should not run before the git check: {command}")

    events, stop, start = _recording_restart()
    result = run_update(_instance(), runner=runner, root=tmp_path, stop=stop, start=start)

    assert not result.ok
    assert events == []


def _stopped_instance(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: ServerState = "absent",
    claimed_ports: tuple[int, ...] = (),
) -> ServerInstance:
    monkeypatch.setattr(update_management, "classify_server", lambda _instance: state)
    monkeypatch.setattr(update_management, "live_server_ports", lambda _data_dir: claimed_ports)
    return ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=data_dir,
        url="http://127.0.0.1:8420",
        log_path=data_dir / "server.log",
    )


@pytest.mark.parametrize(
    ("server", "refusal"),
    [
        pytest.param({}, None, id="server-down"),
        # A busy server still has its files open; a copy would not be consistent.
        pytest.param({"state": "unresponsive"}, "does not answer its health check", id="busy"),
        pytest.param({"claimed_ports": (9000,)}, "(port 9000)", id="server-on-another-port"),
    ],
)
def test_update_snapshot_preflight_copies_current_format_data_only_when_no_server_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: dict[str, Any], refusal: str | None
) -> None:
    write_bootstrap_marker(tmp_path)
    manager = ChatSessionManager(tmp_path)
    manager.create("coder", session_id="session-one").append(ChatMessage.user("protected"))
    manager.close()

    # An updater still runs the previous release after an offline conversion.
    # It must copy the databases without opening them through its own owners.
    def refuse_open(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the updater must not open a database to back it up")

    monkeypatch.setattr("core.database.database.open_database", refuse_open)
    monkeypatch.setattr("core.database.database.open_offline_database", refuse_open)
    instance = _stopped_instance(tmp_path, monkeypatch, **server)

    result = update_management._ensure_update_data_snapshot(instance)

    if refusal is not None:
        assert result.ok is False
        assert refusal in result.message
        assert not snapshot_root(tmp_path).exists()
        return
    assert result.ok is True
    assert "pre-update data snapshot:" in result.message


def _register_missing_databases(root: Path) -> None:
    write_bootstrap_marker(root)
    register_database(root, "sessions", MarkerEntry(database_id="a" * 32, format_generation=1))
    register_database(
        root, "ext.gone.state", MarkerEntry(database_id="b" * 32, format_generation=1)
    )


@pytest.mark.parametrize(
    ("prepare", "ok", "fragments"),
    [
        pytest.param(write_bootstrap_marker, True, [], id="bootstrap-marker-without-databases"),
        pytest.param(
            lambda root: (root / "sessions.db").write_bytes(b""),
            False,
            ["without a current-format data-store marker"],
            id="databases-without-marker",
        ),
        pytest.param(
            _register_missing_databases,
            False,
            [
                "the registered database sessions has no file; starting vBot restores it",
                "`vbot data-store unregister ext.gone.state --yes` releases it",
            ],
            id="missing-registered-database",
        ),
    ],
)
def test_update_snapshot_preflight_skips_empty_data_and_refuses_unverifiable_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepare: Callable[[Path], object],
    ok: bool,
    fragments: list[str],
) -> None:
    prepare(tmp_path)

    result = update_management._ensure_update_data_snapshot(
        _stopped_instance(tmp_path, monkeypatch)
    )

    assert result.ok is ok
    if ok:
        assert result.message == ""
    for fragment in fragments:
        assert fragment in result.message


def test_update_refuses_dirty_without_flags(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path)
    runner = ScriptedRunner(
        checkout(status=" M core/foo.py", answer=only_reads("symbolic-ref", "status", "rev-parse"))
    )
    events, stop, start = _recording_restart()
    snapshots, snapshot = _recording_snapshot()

    result = run_update(
        _instance(), runner=runner, root=tmp_path, stop=stop, start=start, data_snapshot_fn=snapshot
    )

    assert not result.ok
    assert "--discard" in result.message
    assert events == []
    assert snapshots == []


def test_update_discard_resets_then_updates(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path)
    runner = ScriptedRunner(checkout(status=" M x.py"))
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
    write_webui_build(root)
    _write_state(root, webui_revision="samesha")


@pytest.mark.parametrize("ahead", [0, 2], ids=["current", "local-commits-ahead"])
@pytest.mark.parametrize("caller", ["human", "no-restart", "agent"])
def test_an_update_with_nothing_new_neither_snapshots_nor_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ahead: int, caller: str
) -> None:
    _applied_checkout(tmp_path)

    def schedule(instance: ServerInstance, *, service_name: str) -> CommandResult:
        raise AssertionError("an unchanged installation must not schedule a restart")

    monkeypatch.setattr(update_management, "has_vbot_run_context", lambda: caller == "agent")
    monkeypatch.setattr(update_management, "schedule_server_restart", schedule)
    runner = ScriptedRunner(checkout(upstream=_upstream(ahead=ahead)))
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


@pytest.mark.parametrize("rewrite_ok", [True, False], ids=["rewritten", "rewrite-failed"])
def test_a_posix_update_lets_the_updated_code_rewrite_the_autostart_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rewrite_ok: bool
) -> None:
    _applied_checkout(tmp_path)
    data_dir = tmp_path / "data"
    instance = ServerInstance(
        host="127.0.0.1",
        port=9123,
        data_dir=data_dir,
        url="http://127.0.0.1:9123",
        log_path=data_dir / "logs" / "today.log",
    )
    rewrites: list[list[str]] = []

    def rewrite(command: list[str]) -> CommandRun | None:
        if command[1:4] != ["-m", "cli.autostart_management", "--refresh-unit"]:
            return None
        rewrites.append(command)
        return _ok("test-owned rewrite") if rewrite_ok else _err("test-owned reload failure")

    result = run_update(
        instance,
        runner=ScriptedRunner(checkout(answer=rewrite)),
        root=tmp_path,
        platform_name="posix",
        service_name="vbot-alt",
    )

    # Even an update with nothing new lets the installed code repair the unit; a
    # failed rewrite does not stop the update, and the result names it.
    assert result.ok, result.message
    assert ("test-owned rewrite" in result.message) is rewrite_ok
    assert ("test-owned reload failure" in result.message) is not rewrite_ok
    assert bool(result.attention) is not rewrite_ok
    [command] = rewrites
    assert command[0] == sys.executable  # the recorded interpreter runs the updated code

    # The refresh entrypoint accepts exactly what the updater sends.
    received: list[tuple[ServerInstance, str]] = []

    def refresh(target: ServerInstance, *, service_name: str) -> CommandResult:
        received.append((target, service_name))
        return CommandResult(ok=True, message="", instance=target)

    monkeypatch.setattr(autostart_management, "refresh_autostart_unit", refresh)
    assert autostart_management._run_refresh_unit(command[3:]) == 0
    [(target, service_name)] = received
    assert (target.host, target.port, target.data_dir, service_name) == (
        "127.0.0.1",
        9123,
        data_dir.resolve(),
        "vbot-alt",
    )


def test_new_upstream_commits_are_applied_after_the_snapshot(tmp_path: Path) -> None:
    _applied_checkout(tmp_path)
    order: list[str] = []

    def snapshot(instance: ServerInstance) -> _SnapshotStep:
        order.append("snapshot")
        return _SnapshotStep(True, "pre-update data snapshot: s-1", "s-1")

    runner = ScriptedRunner(
        checkout(
            heads=["samesha", "newsha"],
            upstream=_upstream(behind=3),
            on_merge=lambda: order.append("merge"),
        )
    )
    events, stop, start = _recording_restart()

    result = run_update(
        _instance(), runner=runner, root=tmp_path, stop=stop, start=start, data_snapshot_fn=snapshot
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
        (_ok(), _ok("not counts"), "unexpected upstream comparison output: 'not counts'"),
    ],
    ids=["offline", "diverged", "no-upstream", "unreadable-comparison"],
)
def test_an_update_that_cannot_fast_forward_is_refused_before_the_snapshot(
    tmp_path: Path, fetch: CommandRun, comparison: CommandRun, reason: str
) -> None:
    _applied_checkout(tmp_path)
    handler = checkout(
        upstream=comparison,
        answer=only_reads("symbolic-ref", "status", "rev-parse", "rev-list", fetch=fetch),
    )
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


@pytest.mark.parametrize("explicit", [False, True], ids=["recorded-target", "explicit-target"])
def test_update_restarts_the_explicit_or_else_the_installer_recorded_server(
    tmp_path: Path, explicit: bool
) -> None:
    (tmp_path / ".git").mkdir()
    recorded_data_dir = tmp_path / "recorded-data"
    _write_state(
        tmp_path,
        server_host="0.0.0.0",
        server_port=9123,
        server_data_directory=str(recorded_data_dir),
    )
    target: dict[str, Any] = (
        {"host": "127.0.0.2", "port": 9456, "data_dir": tmp_path / "explicit-data"}
        if explicit
        else {}
    )
    resolved_targets: list[dict[str, object]] = []
    restarted: list[tuple[str, int, Path]] = []

    def resolve(**fields: Any) -> ServerInstance:
        resolved_targets.append(fields)
        data_dir = Path(str(fields["data_dir"]))
        return ServerInstance(
            host=fields["host"],
            port=fields["port"],
            data_dir=data_dir,
            url=f"http://{fields['host']}:{fields['port']}",
            log_path=data_dir / "logs" / "today.log",
        )

    def record(label: str) -> Callable[[ServerInstance], CommandResult]:
        def lifecycle(instance: ServerInstance) -> CommandResult:
            restarted.append((instance.host, instance.port, instance.data_dir))
            return CommandResult(ok=True, message=label, instance=instance)

        return lifecycle

    result = run_update(
        _instance(),
        runner=ScriptedRunner(checkout()),
        root=tmp_path,
        resolve=resolve,
        stop=record("stopped"),
        start=record("started"),
        **target,
    )

    expected = target or {"host": "0.0.0.0", "port": 9123, "data_dir": str(recorded_data_dir)}
    selected = (expected["host"], expected["port"], Path(expected["data_dir"]))
    assert result.ok, result.message
    assert resolved_targets == [expected]
    assert (result.instance.host, result.instance.port, result.instance.data_dir) == selected
    assert restarted == [selected, selected]


def test_default_runner_disables_the_git_prompt_and_decodes_utf8(tmp_path: Path) -> None:
    script = (
        "import os, sys; sys.stdout.buffer.write("
        "(os.environ.get('GIT_TERMINAL_PROMPT', 'unset') + ' Łódź\\n').encode('utf-8'))"
    )

    result = _default_runner([sys.executable, "-c", script], tmp_path)

    assert (result.returncode, result.stdout) == (0, "0 Łódź")


def test_command_timeout_does_not_wait_for_helpers_holding_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Slow by design (about 1 s): a real shell must start its helper before the timeout,
    # because only a helper that holds the pipes proves the runner stops waiting for it.
    helper = [sys.executable, "-c", "import time; time.sleep(15)"]
    # A shell running a helper as its own child, as ``cmd /c npm`` runs node.
    command = (
        ["cmd", "/c", *helper]
        if sys.platform == "win32"
        else ["sh", "-c", f"{shlex.join(helper)}; true"]
    )
    monkeypatch.setattr(update_management, "_COMMAND_TIMEOUT_SECONDS", 1.0)

    started = time.monotonic()
    result = _default_runner(command, tmp_path)

    assert result.returncode == 124
    assert "timed out" in result.stderr
    assert time.monotonic() - started < 8


@pytest.mark.parametrize(
    ("project", "version"),
    [
        ('[project]\nname = "vbot"\nversion = "1.2.3"\n', "1.2.3"),
        ("[project]\n", UNKNOWN_VBOT_VERSION),
    ],
    ids=["version", "invalid-project"],
)
def test_read_checkout_version_uses_the_live_pyproject(
    tmp_path: Path, project: str, version: str
) -> None:
    (tmp_path / "pyproject.toml").write_text(project, encoding="utf-8")

    assert read_checkout_version(tmp_path) == version


_PROJECT = '[project]\nname = "vbot"\nversion = "1.0.0"\ndependencies = ["httpx"]\n'


@pytest.mark.parametrize(
    ("after", "reinstall"),
    [
        pytest.param(_PROJECT.replace("httpx", "httpx>=0.28"), True, id="dependencies-changed"),
        pytest.param(_PROJECT.replace("1.0.0", "1.0.1"), False, id="version-bump"),
    ],
)
def test_dev_update_reinstalls_dependencies_only_when_their_inputs_change(
    tmp_path: Path, after: str, reinstall: bool
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_bytes(_PROJECT.encode())
    write_webui_build(tmp_path)
    _write_state(tmp_path, revision="old", webui_revision="old")
    runner = ScriptedRunner(
        checkout(
            heads=["old", "new"],
            upstream=_upstream(behind=1),
            on_merge=lambda: (tmp_path / "pyproject.toml").write_bytes(after.encode()),
        )
    )
    events, stop, start = _recording_restart()

    result = run_update(_instance(), runner=runner, root=tmp_path, stop=stop, start=start)

    assert result.ok, result.message
    assert runner.ran("-m", "pip", "install", "-e", ".[server,cli]") is reinstall
    assert events == ["stop", "start"]


def test_installed_dependencies_are_recorded_when_a_later_step_fails(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("before", encoding="utf-8")
    _write_state(tmp_path, revision="old", webui_revision="old")

    def search_runtime(command: list[str]) -> CommandRun | None:
        return _err("offline") if command[1:] == ["-m", "cli.search_runtime"] else None

    runner = ScriptedRunner(
        checkout(
            heads=["old", "new"],
            upstream=_upstream(behind=1),
            on_merge=lambda: (tmp_path / "pyproject.toml").write_text("after", encoding="utf-8"),
            answer=search_runtime,
        )
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
    first_runner = ScriptedRunner(
        checkout(
            heads=["old", "new"],
            upstream=_upstream(behind=1),
            on_merge=lambda: (tmp_path / "pyproject.toml").write_text("after", encoding="utf-8"),
            answer=lambda command: _err("pip failed") if "pip" in command else None,
        )
    )
    events, stop, start = _recording_restart()

    first = run_update(
        _instance(), runner=first_runner, root=tmp_path, stop=stop, start=start, platform_name="nt"
    )

    assert not first.ok
    expected_recovery = (
        f"Set-Location -LiteralPath '{tmp_path.resolve()}'; & '{sys.executable}' -m cli.main update"
    )
    assert f"resume update: {expected_recovery}" in first.message

    retry_runner = ScriptedRunner(checkout(heads="new"))
    retried = run_update(_instance(), runner=retry_runner, root=tmp_path, stop=stop, start=start)

    assert retried.ok, retried.message
    assert retry_runner.ran("-m", "pip", "install", "-e", ".[server,cli]")


def test_dev_webui_build_failure_preserves_revision_for_retry(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("unchanged", encoding="utf-8")
    write_webui_build(tmp_path, "existing build")
    _write_state(tmp_path, revision="old", webui_revision="old")
    build_results = iter([_err("build failed"), _ok()])

    def webui(command: list[str]) -> CommandRun | None:
        if command[:3] == ["git", "diff", "--quiet"]:
            assert command[3:5] == ["old", "new"]
            return _err()
        if command == _update_assets._npm_command(["run", "build"]):
            return next(build_results)
        return None

    runner = ScriptedRunner(
        checkout(heads=["old", "new"], upstream=_upstream(behind=1), answer=webui)
    )
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


def _stash_answer(
    object_id: str, *, apply: CommandRun | None = None
) -> Callable[[list[str]], CommandRun | None]:
    def answer(command: list[str]) -> CommandRun | None:
        if command[:3] == ["git", "stash", "create"]:
            return _ok(object_id)
        if command[:3] == ["git", "stash", "apply"] and apply is not None:
            return apply
        return None

    return answer


def test_a_stash_conflict_after_the_update_leaves_the_server_unrestarted(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path, revision="old", webui_revision="old")
    runner = ScriptedRunner(
        checkout(
            heads=["old", "new"],
            status=" M x.py",
            upstream=_upstream(behind=1),
            answer=_stash_answer("updater-stash-object", apply=_err("conflict")),
        )
    )
    events, stop, start = _recording_restart()

    result = run_update(
        _instance(), stash=True, runner=runner, root=tmp_path, stop=stop, start=start
    )

    assert not result.ok
    assert "reapplying stashed changes hit a conflict" in result.message
    assert "the server was not restarted" in result.message
    assert runner.ran("git", "merge", "--ff-only", "@{upstream}")
    assert events == []


def test_stash_is_restored_when_the_fast_forward_fails(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path, revision="old", webui_revision="old")

    def answer(command: list[str]) -> CommandRun | None:
        if command[:2] == ["git", "merge"]:
            return _err("not possible to fast-forward")
        return _stash_answer("updater-stash-object")(command)

    runner = ScriptedRunner(
        checkout(heads="old", status=" M local.py", upstream=_upstream(behind=1), answer=answer)
    )

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

    def answer(command: list[str]) -> CommandRun | None:
        if command[:2] == ["git", "merge"]:
            return _err("not possible to fast-forward")
        return _stash_answer("")(command)

    runner = ScriptedRunner(
        checkout(heads="old", status=" M local.py", upstream=_upstream(behind=1), answer=answer)
    )

    result = run_update(_instance(), stash=True, runner=runner, root=tmp_path)

    assert not result.ok
    assert not runner.ran("git", "stash", "apply")
    assert not runner.ran("git", "stash", "pop")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["update", "--discard"], {"discard": True, "stash": False, "restart": True}),
        (
            ["update", "--stash", "--no-restart"],
            {"discard": False, "stash": True, "restart": False},
        ),
    ],
    ids=["discard", "stash-no-restart"],
)
def test_dispatch_update_passes_flags_through(argv: list[str], expected: dict[str, bool]) -> None:
    captured: dict[str, object] = {}

    def fake_run_update(instance: ServerInstance, **kwargs: object) -> CommandResult:
        captured.update(kwargs)
        return CommandResult(ok=True, message="done", instance=instance)

    def noop(instance: ServerInstance) -> CommandResult:
        return CommandResult(ok=True, message="ok", instance=instance)

    def resolve_target(**_target: object) -> ServerInstance:
        return _instance()

    result = dispatch_update_command(
        parse_args(argv),
        resolve=resolve_target,
        stop=noop,
        start=noop,
        run_update_fn=fake_run_update,
    )

    assert result.ok
    assert captured.pop("resolve") is resolve_target
    assert (captured.pop("stop"), captured.pop("start")) == (noop, noop)
    assert captured == {
        **expected,
        "service_name": "vbot",
        "host": None,
        "port": None,
        "data_dir": None,
    }


@pytest.mark.parametrize("restart_ok", [True, False])
def test_a_failed_restart_names_the_unrestored_snapshot_and_the_previous_revision(
    tmp_path: Path, restart_ok: bool
) -> None:
    (tmp_path / ".git").mkdir()
    _write_state(tmp_path)

    def start(instance: ServerInstance) -> CommandResult:
        return CommandResult(ok=restart_ok, message="start result", instance=instance)

    _events, stop, _start = _recording_restart()
    result = run_update(
        _instance(),
        runner=ScriptedRunner(
            checkout(heads=["oldrevision1", "newrevision2"], upstream=_upstream(behind=1))
        ),
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
