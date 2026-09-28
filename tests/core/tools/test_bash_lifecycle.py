"""Shell Tool command lifecycle: cancellation, timeouts, output limits, and process exit."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-untyped]
import pytest

import core.tools.bash as bash_module
from core.tools.bash import bash_handler
from core.tools.process_manager import ProcessManager
from tests.core.tools.bash_test_support import (
    AGENT_ID,
    RecordingTrigger,
    make_context,
    make_spool_manager,
    python_command,
)
from tests.core.tools.bash_test_support import manager as manager
from tests.core.tools.bash_test_support import shell_env_cache as shell_env_cache

_SLEEP = "import time; time.sleep(30)"


# --- Cancellation ----------------------------------------------------------


@pytest.mark.asyncio
async def test_run_cancellation_stops_foreground_without_handoff(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher_calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(bash_module, "FOREGROUND_POLL_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(
        bash_module,
        "_maybe_spawn_completion_watcher",
        lambda *args, **kwargs: watcher_calls.append(args),
    )
    context = make_context(tmp_path, cancellation_hook=lambda: True)

    # Cancellation also stops a command whose call removed the time limit.
    result = await bash_handler(context, {"command": _SLEEP, "timeout": 0}, manager)

    assert result["ok"] is False
    assert result["error"]["code"] == bash_module.RUN_CANCELLED_FAILURE_CODE
    assert watcher_calls == []
    assert all(process.proc.returncode is not None for process in manager.list_processes(AGENT_ID))


@pytest.mark.asyncio
async def test_direct_process_cancel_returns_user_abort_without_run_cancel(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    ready = asyncio.Event()
    context = replace(
        make_context(tmp_path), background_registration_hook=lambda _callback: ready.set()
    )
    task = asyncio.create_task(bash_handler(context, {"command": _SLEEP}, manager))
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        (process,) = manager.list_processes(AGENT_ID)
        await manager.cancel_for_user(process.process_id, AGENT_ID)
        result = await asyncio.wait_for(task, timeout=5)
        assert not context.is_cancelled()
        assert not context.was_cancelled_by_user()
        assert result["ok"] is False
        assert result["error"]["code"] == "cancelled_by_user"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_user_cancel_during_foreground_returns_cancelled_by_user_envelope(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_cancelled = False
    cancel_calls: list[tuple[str, str]] = []
    kill_event = asyncio.Event()
    registered_callbacks: list[Callable[[], None]] = []

    def cancel_registration_hook(callback: Callable[[], None]) -> None:
        registered_callbacks.append(callback)
        # The runtime marks the call as user-cancelled and fires the callback.
        nonlocal user_cancelled
        user_cancelled = True
        callback()

    original_cancel_for_user = manager.cancel_for_user

    async def tracking_cancel_for_user(
        process_id: str, agent_id: str, *, project_id: str | None = None
    ) -> Any:
        cancel_calls.append((process_id, agent_id))
        try:
            return await original_cancel_for_user(process_id, agent_id, project_id=project_id)
        finally:
            kill_event.set()

    monkeypatch.setattr(manager, "cancel_for_user", tracking_cancel_for_user)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(
        tmp_path,
        cancel_registration_hook=cancel_registration_hook,
        cancel_check_hook=lambda: user_cancelled,
    )

    result = await bash_handler(context, {"command": _SLEEP, "mode": "foreground"}, manager)
    await asyncio.wait_for(kill_event.wait(), timeout=2)

    assert result["ok"] is False
    assert result["error"]["code"] == "cancelled_by_user"
    assert result["error"]["message"].startswith("Command aborted by the user after ")
    ((process_id, agent_id),) = cancel_calls
    assert agent_id == AGENT_ID
    assert manager.get_process(process_id, AGENT_ID).cancelled_by_user is True
    assert len(registered_callbacks) == 1


@pytest.mark.asyncio
async def test_user_cancel_kill_failure_is_logged(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    kill_failed = asyncio.Event()

    async def failing_cancel_for_user(
        process_id: str, agent_id: str, *, project_id: str | None = None
    ) -> None:
        kill_failed.set()
        raise RuntimeError("kill exploded")

    captured_callback: list[Callable[[], None]] = []
    monkeypatch.setattr(manager, "cancel_for_user", failing_cancel_for_user)
    context = make_context(
        tmp_path,
        cancel_registration_hook=captured_callback.append,
        cancel_check_hook=lambda: True,
    )
    # Register the user-cancel callback through the handler's wiring without
    # spawning a real process.
    bash_module._register_user_cancel_callback(manager, context, "process-x")
    assert captured_callback

    with caplog.at_level(logging.ERROR, logger="vbot.tools.bash"):
        captured_callback[0]()
        await asyncio.wait_for(kill_failed.wait(), timeout=2)
        # Let the scheduled kill task finish so its done-callback runs.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    kill_errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and "user-cancel kill failed" in record.getMessage()
    ]
    assert kill_errors
    assert kill_errors[0].exc_info is not None


# --- Timeouts --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("foreground", [180]), ("background", [None])])
async def test_omitted_timeout_resolves_mode_default_and_retires_after_exit(
    manager, tmp_path, monkeypatch, mode, expected
):
    observed = []
    original = bash_module._schedule_timeout

    def schedule(manager, context, process_id, timeout):
        observed.append(timeout)
        return original(manager, context, process_id, timeout)

    monkeypatch.setattr(bash_module, "_schedule_timeout", schedule)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    result = await bash_handler(
        make_context(tmp_path), {"command": "print('done')", "mode": mode}, manager
    )
    assert result["ok"]
    assert observed == expected
    tracked = manager.list_processes(AGENT_ID)[0]
    await asyncio.wait_for(asyncio.shield(tracked.wait_task), 5)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert tracked.status == "completed"
    assert not any(
        task.get_name() == f"bash-timeout:{tracked.process_id}" for task in asyncio.all_tasks()
    )
    for parameters in (
        bash_module.BASH_TOOL_PARAMETERS,
        bash_module.BASH_SUBAGENT_TOOL_PARAMETERS,
    ):
        assert "default" not in parameters["properties"]["timeout"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("depth", "noisy", "default_timeout"),
    [
        (0, False, 0.05),
        # Output is no heartbeat: a command that keeps printing still ends.
        (1, True, 0.7),
    ],
)
async def test_omitted_timeout_ends_silent_and_noisy_foreground_commands(
    manager, tmp_path, monkeypatch, depth, noisy, default_timeout
):
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(bash_module, "DEFAULT_TIMEOUT_SECONDS", default_timeout)
    command = (
        "import time\nwhile True:\n    print('working', flush=True)\n    time.sleep(0.02)"
        if noisy
        else _SLEEP
    )

    result = await asyncio.wait_for(
        bash_handler(make_context(tmp_path, nesting_depth=depth), {"command": command}, manager),
        10,
    )

    tracked = manager.list_processes(AGENT_ID)[0]
    await asyncio.wait_for(asyncio.shield(tracked.wait_task), 10)
    assert tracked.status == "killed"
    assert tracked.proc.returncode is not None
    assert result["error"]["code"] == "process_timeout"
    if noisy:
        assert "working" in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, 5])
async def test_explicit_timeout_allows_silent_work_beyond_default(
    manager, tmp_path, monkeypatch, timeout
):
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(bash_module, "DEFAULT_TIMEOUT_SECONDS", 0.01)
    result = await asyncio.wait_for(
        bash_handler(
            make_context(tmp_path),
            {"command": "import time; time.sleep(0.1); print('finished')", "timeout": timeout},
            manager,
        ),
        10,
    )
    assert result["ok"]
    assert result["data"]["status"] == "completed"
    assert "finished" in result["data"]["output"]


@pytest.mark.asyncio
async def test_omitted_timeout_leaves_background_commands_unbounded(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(bash_module, "DEFAULT_TIMEOUT_SECONDS", 0.01)
    result = await bash_handler(
        make_context(tmp_path),
        {
            "command": "import time; print('ready', flush=True); time.sleep(30)",
            "mode": "background",
        },
        manager,
    )
    process_id = result["data"]["process_id"]

    # The command prints long after a foreground default would have killed it.
    async with asyncio.timeout(10):
        while "ready" not in str((await manager.snapshot(process_id, AGENT_ID))["output"]):
            await asyncio.sleep(0.01)

    assert manager.get_process(process_id, AGENT_ID).status == "running"
    await manager.kill(process_id, AGENT_ID)


@pytest.mark.asyncio
async def test_explicit_timeout_kills_a_background_command_and_explains_the_deadline(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    trigger = RecordingTrigger()

    result = await bash_handler(
        make_context(tmp_path),
        {"command": _SLEEP, "mode": "background", "timeout": 0.1},
        manager,
        trigger_service=trigger,
    )

    assert result["ok"]
    body = await trigger.body()
    assert manager.list_processes(AGENT_ID)[0].status == "killed"
    assert "### Bash process — killed" in body
    assert "tool timeout of 0.1 s elapsed and killed the process" in body
    assert "timeout: 0 for no limit" in body
    assert "Exit code:" not in body


@pytest.mark.asyncio
async def test_timeout_remains_active_after_foreground_yields_to_background(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bash_module, "FOREGROUND_HANDOFF_SECONDS", 0.01)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await bash_handler(
        make_context(tmp_path),
        {"command": _SLEEP, "mode": "foreground", "timeout": 0.1},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    process_id = result["data"]["process_id"]
    outcome, _line = await manager.wait(process_id, AGENT_ID, timeout_seconds=2)
    assert (outcome, manager.get_process(process_id, AGENT_ID).status) == ("exited", "killed")


@pytest.mark.asyncio
async def test_natural_completion_at_deadline_not_reported_as_timeout(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timer already elapsed, but the process ended on its own: its result stands."""
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    def already_timed_out(*_args: Any) -> tuple[None, dict[str, bool]]:
        return None, {"timed_out": True}

    monkeypatch.setattr(bash_module, "_schedule_timeout", already_timed_out)

    result = await bash_handler(
        make_context(tmp_path),
        {"command": "print('done')", "mode": "foreground", "timeout": 0.01},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert "done" in result["data"]["output"]


@pytest.mark.asyncio
async def test_failed_timeout_kill_returns_error_without_losing_process(
    manager, tmp_path, monkeypatch
):
    import core.tools.process_manager as manager_module

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    async def denied(proc, **kwargs):
        raise PermissionError("test-owned denied timeout kill")

    with monkeypatch.context() as patch:
        patch.setattr(manager_module, "kill_process_tree_async", denied)
        result = await asyncio.wait_for(
            bash_handler(make_context(tmp_path), {"command": _SLEEP, "timeout": 0.05}, manager),
            3,
        )
    assert result["ok"] is False
    assert result["error"]["code"] == "process_kill_failed"
    tracked = manager.list_processes(AGENT_ID)[0]
    assert tracked.status == "running"
    assert tracked.process_id in result["error"]["message"]
    await manager.kill(tracked.process_id, AGENT_ID)
    assert tracked.status == "killed"


@pytest.mark.asyncio
async def test_timeout_failure_carries_the_output_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool_manager = make_spool_manager(tmp_path)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        output = "x" * 9000 + "diag-marker"

        result = await bash_handler(
            make_context(tmp_path, nesting_depth=1),
            {
                "command": f"print({output!r}, flush=True); {_SLEEP}",
                "mode": "foreground",
                # Long enough for the output to exist before the kill.
                "timeout": 1.5,
            },
            spool_manager,
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "process_timeout"
        message = result["error"]["message"]
        tail, log_pointer = message.split("Output tail:\n", 1)[1].split("\nComplete output: ", 1)
        assert "diag-marker" in tail
        assert Path(log_pointer.strip()).name.endswith(".log")
        assert len(tail) <= 8000
    finally:
        await spool_manager.aclose()


# --- Output limits ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "exit_code", "large_line"),
    [
        ("foreground", 3, True),
        ("foreground", 0, False),
        ("background", 0, True),
        ("background", 3, False),
    ],
)
async def test_final_output_limits_preserve_exit_and_complete_log(
    tmp_path, monkeypatch, mode, exit_code, large_line
):
    manager = make_spool_manager(tmp_path)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    output = "x" * 12000 + "END" if large_line else "\n".join(f"line-{i}" for i in range(200))
    trigger = RecordingTrigger()

    try:
        result = await bash_handler(
            make_context(tmp_path),
            {"command": f"import sys; print({output!r}); sys.exit({exit_code})", "mode": mode},
            manager,
            trigger_service=trigger,
        )
        assert result["ok"] is True
        if mode == "background":
            body = await trigger.body()
            assert f"Exit code: {exit_code}" in body
            tail = body.split("Output:\n", 1)[1]
        else:
            assert result["data"]["exit_code"] == exit_code
            assert result["data"]["truncated"] is True
            tail = result["data"]["output"]
        assert len(tail) <= 8000
        newest = "END" if large_line else "line-199"
        assert tail.index("[earlier output truncated") < tail.index(newest)
        if large_line:
            assert tail.rstrip().endswith("x" * 100 + "END")
        else:
            assert tail.splitlines()[1:] == [f"line-{i}" for i in range(100, 200)]
        log_file = Path(result["data"]["log_file"])
        assert log_file.read_text(encoding="utf-8") == output + "\n"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_large_foreground_stdout_is_bounded_and_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ProcessManager(buffer_cap_bytes=32, sweep_interval_seconds=3600)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)

        result = await bash_handler(
            make_context(tmp_path),
            {
                "command": "import sys; sys.stdout.write('a' * 64); sys.stdout.flush()",
                "mode": "foreground",
            },
            manager,
        )

        assert result["ok"] is True
        # No spool dir on this manager: the marker announces the head drop
        # without a log pointer, followed by the surviving newest bytes.
        assert result["data"]["output"] == "[earlier output truncated]\n" + "a" * 32
        assert result["data"]["truncated"] is True
    finally:
        await manager.aclose()


# --- Process exit ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "exit_code"), [("foreground", 7), ("background", 0)])
async def test_descendant_pipe_does_not_hide_exit_or_block_timeout(
    manager, tmp_path, monkeypatch, mode, exit_code, caplog
):
    """A command that exits while its timeout fires and readers drain keeps its result."""
    caplog.set_level(logging.DEBUG, logger="vbot.tools.process_manager")
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    original_spawn = manager.spawn
    original_readers = manager._await_reader_tasks
    original_kill = manager.kill
    draining = asyncio.Event()
    deadline_entered = asyncio.Event()

    async def delayed_readers(tracked):
        draining.set()
        await deadline_entered.wait()
        await original_readers(tracked)

    async def spawn_exited(scope_key, agent_id, argv, **kwargs):
        process_id = await original_spawn(scope_key, agent_id, argv, **kwargs)
        # Observe real OS exit separately from pipe EOF before Bash starts its
        # deadline. Interpreter startup speed is not part of this regression.
        await draining.wait()
        return process_id

    async def observe_kill(process_id, agent_id, **kwargs):
        tracked = manager.get_process(process_id, agent_id, **kwargs)
        assert tracked.proc.returncode == exit_code
        assert tracked.status == "running"
        deadline_entered.set()
        await original_kill(process_id, agent_id, **kwargs)

    monkeypatch.setattr(manager, "spawn", spawn_exited)
    monkeypatch.setattr(manager, "_await_reader_tasks", delayed_readers)
    monkeypatch.setattr(manager, "kill", observe_kill)
    pid_path = tmp_path / "descendant.pid"
    command = (
        "import subprocess, sys; from pathlib import Path; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"Path({str(pid_path)!r}).write_text(str(child.pid)); "
        "print('parent-output', flush=True); "
        "print('parent-error', file=sys.stderr, flush=True); "
        f"raise SystemExit({exit_code})"
    )
    context = make_context(tmp_path)
    task = asyncio.create_task(
        bash_handler(context, {"command": command, "mode": mode, "timeout": 0.01}, manager)
    )
    try:
        result = await asyncio.wait_for(asyncio.shield(task), 5)
        tracked = manager.list_processes(context.agent_id)[0]
        await asyncio.wait_for(asyncio.shield(tracked.wait_task), 5)
        assert deadline_entered.is_set()
        assert tracked.proc.returncode == exit_code
        assert tracked.exit_code == exit_code
        assert tracked.status == ("completed" if exit_code == 0 else "failed")
        assert not tracked.wait_task.cancelled()
        assert tracked.stdout_task.done() and tracked.stderr_task.done()
        assert not tracked.truncated
        diagnostics = [
            record
            for record in caplog.records
            if record.name == "vbot.tools.process_manager" and record.args == (tracked.process_id,)
        ]
        assert len(diagnostics) == 1
        assert diagnostics[0].levelno == logging.DEBUG
        assert result["ok"] is True
        if mode == "foreground":
            assert result["data"]["status"] == "completed"
            assert result["data"]["exit_code"] == exit_code
            assert "parent-output" in result["data"]["output"]
            assert "parent-error" in result["data"]["output"]
        # The CLI's own exit completes the command; no arbitrary descendant kill.
        assert psutil.Process(int(pid_path.read_text())).is_running()
    finally:
        deadline_entered.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        pid_text = pid_path.read_text().strip() if pid_path.exists() else ""
        if pid_text.isdecimal():
            with contextlib.suppress(psutil.NoSuchProcess):
                child = psutil.Process(int(pid_text))
                child.kill()
                await asyncio.to_thread(child.wait, timeout=5)


@pytest.mark.asyncio
async def test_cancelling_kill_waiter_cannot_cancel_process_cleanup(manager, tmp_path, monkeypatch):
    context = make_context(tmp_path)
    draining = asyncio.Event()
    release = asyncio.Event()
    original = manager._await_reader_tasks

    async def delayed_readers(tracked):
        draining.set()
        await release.wait()
        await original(tracked)

    monkeypatch.setattr(manager, "_await_reader_tasks", delayed_readers)
    process_id = await manager.spawn(
        context.run_id,
        context.agent_id,
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=None,
        cwd=tmp_path,
    )
    tracked = manager.get_process(process_id, context.agent_id)
    kill = asyncio.create_task(manager.kill(process_id, context.agent_id))
    try:
        await asyncio.wait_for(draining.wait(), 5)
        # Bash cancels its timeout waiter only after the verified kill changes
        # status. OS exit can precede Windows tree verification completing.
        async with asyncio.timeout(5):
            while tracked.status == "running":
                await asyncio.sleep(0.01)
        kill.cancel()
        with pytest.raises(asyncio.CancelledError):
            await kill
        assert not tracked.wait_task.done()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.shield(tracked.wait_task), 5)
    assert tracked.finished_at is not None
    assert tracked.status == "killed"
