"""Tests for the bash tool's process-manager integration."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import core.tools.bash as bash_module
from core.tools.bash import (
    BASH_TOOL_PARAMETERS,
    _resolve_workdir,
    _resolve_yield_after,
    bash_handler,
    register_bash_tool,
)
from core.tools.process_manager import ProcessManager
from core.tools.tools import ToolContext, ToolRegistry

AGENT_ID = "agent-a"
RUN_ID = "run-a"


@pytest.fixture(autouse=True)
def shell_env_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bash_module, "_cached_shell_env", {"PATH": "original-path"})


@pytest_asyncio.fixture
async def manager() -> AsyncIterator[ProcessManager]:
    manager = ProcessManager(sweep_interval_seconds=3600)
    try:
        yield manager
    finally:
        await manager.aclose()


def make_context(
    tmp_path: Path,
    *,
    cwd: Path | None = None,
    emit_hook: Any = None,
    cancellation_hook: Any = None,
    cancel_registration_hook: Any = None,
    cancel_check_hook: Any = None,
    nesting_depth: int = 0,
    project_id: str | None = None,
) -> ToolContext:
    return ToolContext(
        agent_id=AGENT_ID,
        session_id="session-a",
        run_id=RUN_ID,
        tool_call_id="call-a",
        tool_name="bash",
        tool_call_index=0,
        workspace=tmp_path,
        app_root=tmp_path,
        data_root=tmp_path,
        cwd=cwd,
        emit_hook=emit_hook,
        cancellation_hook=cancellation_hook,
        cancel_registration_hook=cancel_registration_hook,
        cancel_check_hook=cancel_check_hook,
        nesting_depth=nesting_depth,
        project_id=project_id,
    )


def python_command(command: str) -> list[str]:
    return [sys.executable, "-c", command]


async def kill_background(manager: ProcessManager, result: dict[str, Any]) -> None:
    data = result["data"]
    assert isinstance(data, dict)
    session_id = data["session_id"]
    assert isinstance(session_id, str)
    await manager.kill(session_id, AGENT_ID)


@pytest.mark.asyncio
async def test_short_command_completes_and_streams_stdout(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit_hook(event_type: str, payload: dict[str, Any]) -> None:
        events.append((event_type, payload))

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, emit_hook=emit_hook)

    result = await bash_handler(context, {"command": "print('hello')"}, manager)

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 0
    assert result["data"]["output"].replace("\r\n", "\n") == "hello\n"
    assert "stdout" not in result["data"]
    assert "stderr" not in result["data"]
    assert events == [
        (
            "tool_call_stdout",
            {
                "tool_call_id": "call-a",
                "session_id": events[0][1]["session_id"],
                "data": events[0][1]["data"],
            },
        )
    ]
    assert events[0][1]["data"].replace("\r\n", "\n") == "hello\n"


@pytest.mark.asyncio
async def test_background_flag_returns_running_session(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "background": True},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    assert isinstance(result["data"]["session_id"], str)

    await kill_background(manager, result)


@pytest.mark.asyncio
async def test_background_trigger_fires_when_trigger_service_provided(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    trigger_called = asyncio.Event()

    class MockTriggerService:
        async def trigger_run(
            self,
            agent_id: str,
            message: str,
            *,
            session_id: str,
            internal: bool,
            project_id: str | None = None,
        ) -> None:
            calls.append(
                {
                    "agent_id": agent_id,
                    "message": message,
                    "session_id": session_id,
                    "internal": internal,
                }
            )
            trigger_called.set()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import sys; sys.exit(0)", "background": True},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    assert len(calls) == 1
    assert calls[0]["agent_id"] == AGENT_ID
    assert calls[0]["session_id"] == context.session_id
    assert calls[0]["internal"] is True


@pytest.mark.asyncio
async def test_background_trigger_not_spawned_when_trigger_service_is_none(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher_started = asyncio.Event()

    async def unexpected_watch(*_args: Any, **_kwargs: Any) -> None:
        watcher_started.set()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(bash_module, "_watch_background_process", unexpected_watch)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "background": True},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    assert isinstance(result["data"]["session_id"], str)
    await asyncio.sleep(0)
    assert watcher_started.is_set() is False

    await kill_background(manager, result)


@pytest.mark.asyncio
async def test_yield_after_expiry_triggers_background_completion_when_trigger_service_present(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    trigger_called = asyncio.Event()

    class MockTriggerService:
        async def trigger_run(
            self,
            agent_id: str,
            message: str,
            *,
            session_id: str,
            internal: bool,
            project_id: str | None = None,
        ) -> None:
            calls.append(
                {
                    "agent_id": agent_id,
                    "message": message,
                    "session_id": session_id,
                    "internal": internal,
                }
            )
            trigger_called.set()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "import time; print('yield-marker'); time.sleep(0.2)",
            "yield_after": 0.01,
        },
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    assert len(calls) == 1
    assert calls[0]["agent_id"] == AGENT_ID
    assert calls[0]["session_id"] == context.session_id
    assert calls[0]["internal"] is True
    assert "yield-marker" in calls[0]["message"]


@pytest.mark.asyncio
async def test_background_trigger_message_contains_command_exit_code_and_output(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[str] = []
    trigger_called = asyncio.Event()

    class MockTriggerService:
        async def trigger_run(
            self,
            _agent_id: str,
            message: str,
            *,
            session_id: str,
            internal: bool,
            project_id: str | None = None,
        ) -> None:
            messages.append(message)
            assert session_id
            assert internal is True
            trigger_called.set()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)
    command = "import sys; print('result-marker'); sys.exit(3)"

    result = await bash_handler(
        context,
        {"command": command, "background": True},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    assert len(messages) == 1
    assert f"Command: {command}" in messages[0]
    assert "Exit code: 3" in messages[0]
    assert "result-marker" in messages[0]


@pytest.mark.asyncio
async def test_background_completion_trigger_carries_project_id(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project-scoped background completion wakes the parent run under its project."""
    captured: list[str | None] = []
    trigger_called = asyncio.Event()

    class MockTriggerService:
        async def trigger_run(
            self,
            _agent_id: str,
            _message: str,
            *,
            session_id: str,
            internal: bool,
            project_id: str | None = None,
        ) -> None:
            assert session_id
            assert internal is True
            captured.append(project_id)
            trigger_called.set()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, project_id="acme")

    result = await bash_handler(
        context,
        {"command": "import sys; sys.exit(0)", "background": True},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    await asyncio.wait_for(trigger_called.wait(), timeout=2)
    assert captured == ["acme"]


@pytest.mark.asyncio
async def test_background_watcher_does_not_consume_process_poll_output(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trigger_called = asyncio.Event()

    class MockTriggerService:
        async def trigger_run(
            self,
            _agent_id: str,
            _message: str,
            *,
            session_id: str,
            internal: bool,
            project_id: str | None = None,
        ) -> None:
            assert session_id
            assert internal is True
            trigger_called.set()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import time; print('poll-marker'); time.sleep(0.05)", "background": True},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    data = result["data"]
    assert isinstance(data, dict)
    session_id = data["session_id"]
    assert isinstance(session_id, str)

    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    poll_result = await manager.poll(session_id, AGENT_ID, timeout_ms=0)
    output = poll_result.get("output")
    assert isinstance(output, str)
    assert "poll-marker" in output


@pytest.mark.asyncio
async def test_yield_after_expiry_backgrounds_running_process(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "yield_after": 0.01},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"

    await kill_background(manager, result)


@pytest.mark.asyncio
async def test_explicit_background_at_depth_is_rejected_without_spawning(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-agent's explicit background request fails before any process spawns."""
    watcher_calls: list[Any] = []
    trigger_calls: list[str] = []

    def record_watcher(*args: Any, **kwargs: Any) -> None:
        watcher_calls.append((args, kwargs))

    class RecordingTriggerService:
        async def trigger_run(self, *_args: Any, **_kwargs: Any) -> None:
            trigger_calls.append("called")

    monkeypatch.setattr(bash_module, "_maybe_spawn_completion_watcher", record_watcher)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=1)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "background": True},
        manager,
        trigger_service=RecordingTriggerService(),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == bash_module.BACKGROUND_AT_DEPTH_FAILURE_CODE
    assert watcher_calls == []
    await asyncio.sleep(0)
    assert trigger_calls == []


@pytest.mark.asyncio
async def test_automatic_background_at_depth_kills_process_and_fails(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At depth a foreground command that outruns yield_after is killed, not backgrounded."""
    watcher_calls: list[Any] = []
    kill_calls: list[tuple[str, str]] = []

    def record_watcher(*args: Any, **kwargs: Any) -> None:
        watcher_calls.append((args, kwargs))

    original_kill = manager.kill

    async def tracking_kill(session_id: str, agent_id: str) -> None:
        kill_calls.append((session_id, agent_id))
        await original_kill(session_id, agent_id)

    monkeypatch.setattr(bash_module, "_maybe_spawn_completion_watcher", record_watcher)
    monkeypatch.setattr(manager, "kill", tracking_kill)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=1)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "yield_after": 0.01},
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == bash_module.BACKGROUND_AT_DEPTH_FAILURE_CODE
    assert "did not finish" in result["error"]["message"]
    assert watcher_calls == []
    assert kill_calls, "the still-running process should have been killed"


@pytest.mark.asyncio
async def test_fast_foreground_command_at_depth_succeeds(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-agent command finishing within yield_after still succeeds synchronously."""
    watcher_calls: list[Any] = []

    def record_watcher(*args: Any, **kwargs: Any) -> None:
        watcher_calls.append((args, kwargs))

    monkeypatch.setattr(bash_module, "_maybe_spawn_completion_watcher", record_watcher)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=1)

    result = await bash_handler(
        context,
        {"command": "print('quick')"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert "quick" in result["data"]["output"]
    assert watcher_calls == []


@pytest.mark.asyncio
async def test_background_at_top_level_is_not_blocked(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: at depth 0 an explicit background request still backgrounds and watches."""
    trigger_called = asyncio.Event()

    class MockTriggerService:
        async def trigger_run(
            self,
            _agent_id: str,
            _message: str,
            *,
            session_id: str,
            internal: bool,
            project_id: str | None = None,
        ) -> None:
            assert session_id
            assert internal is True
            trigger_called.set()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=0)

    result = await bash_handler(
        context,
        {"command": "import sys; sys.exit(0)", "background": True},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    await asyncio.wait_for(trigger_called.wait(), timeout=2)


@pytest.mark.asyncio
async def test_non_zero_exit_code_is_successful_tool_result(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import sys; print('bad', file=sys.stderr); raise SystemExit(7)"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 7
    assert "bad" in result["data"]["output"]


@pytest.mark.asyncio
async def test_spawn_failure_returns_failure_envelope(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", lambda command: ["missing-vbot-shell"])
    context = make_context(tmp_path)

    result = await bash_handler(context, {"command": "ignored"}, manager)

    assert result["ok"] is False
    assert result["error"]["code"] == "process_spawn_failed"


def test_resolve_workdir_defaults_to_cwd_not_workspace(tmp_path: Path) -> None:
    # A project session sets cwd to the repo; with no workdir argument, bash
    # must default its working directory to the cwd, not the agent workspace.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    context = make_context(workspace, cwd=repo)

    assert _resolve_workdir(context, None) == repo.resolve()


def test_resolve_workdir_defaults_to_workspace_without_cwd(tmp_path: Path) -> None:
    # No project cwd: the working directory stays the workspace, today's behavior.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = make_context(workspace)

    assert _resolve_workdir(context, None) == workspace.resolve()


def test_resolve_yield_after_uses_generous_default_inside_subagent(tmp_path: Path) -> None:
    # Top level: an omitted yield_after keeps the short background-hand-off default.
    top = make_context(tmp_path, nesting_depth=0)
    assert _resolve_yield_after(top, None) == bash_module.DEFAULT_YIELD_AFTER_SECONDS
    # Sub-agent: an omitted yield_after gets the generous foreground window instead of
    # the 30s default, so a normal pytest/build is not killed before it finishes.
    sub = make_context(tmp_path, nesting_depth=1)
    assert _resolve_yield_after(sub, None) == bash_module.DEFAULT_SUBAGENT_YIELD_AFTER_SECONDS
    assert bash_module.DEFAULT_SUBAGENT_YIELD_AFTER_SECONDS >= 600.0


def test_resolve_yield_after_honors_explicit_value_at_any_depth(tmp_path: Path) -> None:
    # An explicit yield_after wins at both levels, so the caller can still bound tighter.
    assert _resolve_yield_after(make_context(tmp_path, nesting_depth=0), 5.0) == 5.0
    assert _resolve_yield_after(make_context(tmp_path, nesting_depth=1), 5.0) == 5.0


def test_resolve_workdir_resolves_relative_workdir_against_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    context = make_context(workspace, cwd=repo)

    assert _resolve_workdir(context, "sub") == (repo / "sub").resolve()


@pytest.mark.asyncio
async def test_bash_runs_in_cwd_when_no_workdir_argument(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end: the spawned process runs in the cwd, so a relative-path write
    # lands in the repo (cwd), not the agent workspace.
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    context = make_context(workspace, cwd=repo)

    result = await bash_handler(
        context,
        {"command": "open('marker.txt', 'w').write('here')"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 0
    assert (repo / "marker.txt").read_text(encoding="utf-8") == "here"
    assert not (workspace / "marker.txt").exists()


@pytest.mark.asyncio
async def test_env_overrides_are_sanitised(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)
    command = "import os; print(os.environ['SAFE_VALUE']); print(os.environ['PATH'])"

    result = await bash_handler(
        context,
        {"command": command, "env": {"SAFE_VALUE": "allowed", "PATH": "blocked-path"}},
        manager,
    )

    assert result["ok"] is True
    assert "allowed" in result["data"]["output"]
    assert "original-path" in result["data"]["output"]
    assert "blocked-path" not in result["data"]["output"]


@pytest.mark.asyncio
async def test_workdir_defaults_to_workspace(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(context, {"command": "import os; print(os.getcwd())"}, manager)

    assert result["ok"] is True
    assert result["data"]["output"].strip() == str(tmp_path)


@pytest.mark.asyncio
async def test_timeout_kills_process(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "timeout": 0.01, "yield_after": 1},
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "process_timeout"


@pytest.mark.asyncio
async def test_natural_completion_at_deadline_not_reported_as_timeout(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that exits on its own as the timer fires reports success.

    Reproduces the deadline race: the timeout flag is already set (the timer
    elapsed) but the process completes naturally, so its kill is a no-op and the
    session ends "completed". The tool must surface that success, not a timeout.
    """
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    def already_timed_out(
        process_manager: ProcessManager,
        session_id: str,
        agent_id: str,
        timeout: float | None,
    ) -> tuple[None, dict[str, bool]]:
        return None, {"timed_out": True}

    monkeypatch.setattr(bash_module, "_schedule_timeout", already_timed_out)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "print('done')", "timeout": 0.01, "yield_after": 1},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert "done" in result["data"]["output"]


@pytest.mark.asyncio
async def test_large_foreground_stdout_is_bounded_and_truncated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ProcessManager(buffer_cap_bytes=32, sweep_interval_seconds=3600)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        context = make_context(tmp_path)

        result = await bash_handler(
            context,
            {"command": "import sys; sys.stdout.write('a' * 64); sys.stdout.flush()"},
            manager,
        )

        assert result["ok"] is True
        assert result["data"]["output"] == "a" * 32
        assert result["data"]["truncated"] is True
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_cancellation_exits_foreground_without_waiting_for_poll_interval(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(bash_module, "FOREGROUND_POLL_INTERVAL_SECONDS", 10.0)
    context = make_context(tmp_path, cancellation_hook=lambda: True)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "yield_after": 30},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"

    await kill_background(manager, result)


def test_shell_detection_uses_native_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bash_module.sys, "platform", "win32")

    assert bash_module._shell_argv("Write-Output hello") == [
        "pwsh",
        "-Command",
        "Write-Output hello",
    ]

    monkeypatch.setattr(bash_module.sys, "platform", "linux")

    assert bash_module._shell_argv("echo hello") == ["bash", "-c", "echo hello"]


@pytest.mark.asyncio
async def test_shell_env_probe_timeout_terminates_and_reaps_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed_process_groups: list[tuple[int, int]] = []

    class HungProbe:
        pid = 12345
        returncode = None

        def __init__(self) -> None:
            self.communicate_calls = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                await asyncio.Future()
            self.returncode = -9
            return b"", b""

    probe = HungProbe()

    async def create_probe(*_args: Any, **_kwargs: Any) -> HungProbe:
        return probe

    monkeypatch.setattr(bash_module.sys, "platform", "linux")
    monkeypatch.setattr(bash_module.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(bash_module, "SHELL_ENV_PROBE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(bash_module.asyncio, "create_subprocess_exec", create_probe)
    monkeypatch.setattr(
        bash_module.os,
        "killpg",
        lambda process_group_id, signal_number: killed_process_groups.append(
            (process_group_id, signal_number)
        ),
        raising=False,
    )
    monkeypatch.setenv("VBOT_PROBE_FALLBACK", "fallback")

    env = await bash_module._probe_shell_env()

    assert env["VBOT_PROBE_FALLBACK"] == "fallback"
    assert killed_process_groups == [(12345, 9)]
    assert probe.communicate_calls == 2


def test_register_bash_tool() -> None:
    registry = ToolRegistry()
    manager = ProcessManager(sweep_interval_seconds=3600)

    register_bash_tool(registry, manager)

    tool = registry.get("bash")
    assert tool.parameters == BASH_TOOL_PARAMETERS
    assert tool.parameters["additionalProperties"] is False


@pytest.mark.asyncio
async def test_user_cancel_during_foreground_returns_cancelled_by_user_envelope(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-cancel kills the process and returns a ``cancelled_by_user`` envelope."""
    user_cancelled = False
    kill_calls: list[tuple[str, str]] = []
    kill_event = asyncio.Event()
    registered_callbacks: list[Callable[[], None]] = []
    cancelled_sessions: set[str] = set()

    monkeypatch.setattr(bash_module, "_user_cancelled_session_ids", cancelled_sessions)

    def cancel_check_hook() -> bool:
        return user_cancelled

    def cancel_registration_hook(callback: Callable[[], None]) -> None:
        registered_callbacks.append(callback)
        # Simulate the runtime marking the call as user-cancelled and
        # firing the cancel callback (which schedules the kill).
        nonlocal user_cancelled
        user_cancelled = True
        callback()

    original_kill = manager.kill

    async def tracking_kill(session_id: str, agent_id: str) -> None:
        kill_calls.append((session_id, agent_id))
        try:
            await original_kill(session_id, agent_id)
        finally:
            kill_event.set()

    monkeypatch.setattr(manager, "kill", tracking_kill)

    context = make_context(
        tmp_path,
        cancel_registration_hook=cancel_registration_hook,
        cancel_check_hook=cancel_check_hook,
    )
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)"},
        manager,
    )

    await asyncio.wait_for(kill_event.wait(), timeout=2)

    assert result["ok"] is False
    assert result["error"]["code"] == "cancelled_by_user"
    assert "aborted" in result["error"]["message"].lower()
    assert kill_calls, "process_manager.kill should have been called"
    session_id_used, agent_id_used = kill_calls[0]
    assert agent_id_used == AGENT_ID
    assert isinstance(session_id_used, str) and session_id_used
    assert cancelled_sessions == {session_id_used}
    assert len(registered_callbacks) == 1


@pytest.mark.asyncio
async def test_foreground_completion_unaffected_when_user_cancel_check_is_false(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new check is a no-op when ``was_cancelled_by_user`` returns False."""
    user_cancelled = False
    registered_callbacks: list[Callable[[], None]] = []

    def cancel_check_hook() -> bool:
        return user_cancelled

    def cancel_registration_hook(callback: Callable[[], None]) -> None:
        registered_callbacks.append(callback)

    context = make_context(
        tmp_path,
        cancel_registration_hook=cancel_registration_hook,
        cancel_check_hook=cancel_check_hook,
    )
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await bash_handler(
        context,
        {"command": "import sys; print('keep-going')"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 0
    assert "keep-going" in result["data"]["output"]
    # The cancel callback was registered but never fired.
    assert len(registered_callbacks) == 1


@pytest.mark.asyncio
async def test_background_watcher_reports_aborted_by_user_when_session_is_user_cancelled(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watcher uses 'aborted by the user' wording for user-killed sessions."""
    messages: list[str] = []
    trigger_called = asyncio.Event()
    cancelled_sessions: set[str] = set()
    monkeypatch.setattr(bash_module, "_user_cancelled_session_ids", cancelled_sessions)

    class MockTriggerService:
        async def trigger_run(
            self,
            _agent_id: str,
            message: str,
            *,
            session_id: str,
            internal: bool,
            project_id: str | None = None,
        ) -> None:
            assert session_id
            assert internal is True
            messages.append(message)
            trigger_called.set()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "background": True},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    data = result["data"]
    assert isinstance(data, dict)
    session_id = data["session_id"]
    assert isinstance(session_id, str) and session_id

    # Simulate the runtime firing the user-cancel callback for this tool call.
    cancelled_sessions.add(session_id)
    await manager.kill(session_id, AGENT_ID)

    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    assert len(messages) == 1
    message = messages[0]
    assert "aborted by the user" in message
    assert "Background process completed." not in message
    assert "Exit code:" not in message
    assert session_id not in message  # we don't include the raw id, but ensure the marker is there


@pytest.mark.asyncio
async def test_background_watcher_keeps_completion_wording_for_natural_exit(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Natural completion keeps the original 'Background process completed' wording."""
    messages: list[str] = []
    trigger_called = asyncio.Event()
    cancelled_sessions: set[str] = set()
    monkeypatch.setattr(bash_module, "_user_cancelled_session_ids", cancelled_sessions)

    class MockTriggerService:
        async def trigger_run(
            self,
            _agent_id: str,
            message: str,
            *,
            session_id: str,
            internal: bool,
            project_id: str | None = None,
        ) -> None:
            assert session_id
            assert internal is True
            messages.append(message)
            trigger_called.set()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    command = "import sys; print('done'); sys.exit(0)"
    result = await bash_handler(
        context,
        {"command": command, "background": True},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"

    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    assert len(messages) == 1
    message = messages[0]
    assert "Background process completed." in message
    assert "aborted by the user" not in message
    assert "Exit code: 0" in message
    assert "done" in message


@pytest.mark.asyncio
async def test_background_watcher_discards_user_cancelled_session_id_after_consuming(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watcher removes the session id from `_user_cancelled_session_ids` once consumed."""
    trigger_called = asyncio.Event()
    cancelled_sessions: set[str] = set()
    monkeypatch.setattr(bash_module, "_user_cancelled_session_ids", cancelled_sessions)

    class MockTriggerService:
        async def trigger_run(
            self,
            _agent_id: str,
            _message: str,
            *,
            session_id: str,
            internal: bool,
            project_id: str | None = None,
        ) -> None:
            assert session_id
            assert internal is True
            trigger_called.set()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "background": True},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    data = result["data"]
    assert isinstance(data, dict)
    session_id = data["session_id"]
    assert isinstance(session_id, str) and session_id

    # Simulate the runtime firing the user-cancel callback for this tool call.
    cancelled_sessions.add(session_id)
    await manager.kill(session_id, AGENT_ID)

    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    # The watcher must have consumed and discarded the entry, so the set no longer holds it.
    assert session_id not in bash_module._user_cancelled_session_ids


@pytest.mark.asyncio
async def test_user_cancel_kill_failure_is_logged(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing user-cancel kill task is surfaced through the done-callback log.

    The cancel callback schedules ``process_manager.kill`` on the running loop and
    attaches ``_log_background_task_result`` as a done-callback. When that kill
    raises, the failure must be logged at error level with a traceback.
    """
    kill_failed = asyncio.Event()
    monkeypatch.setattr(bash_module, "_user_cancelled_session_ids", set())

    async def failing_kill(session_id: str, agent_id: str) -> None:
        kill_failed.set()
        raise RuntimeError("kill exploded")

    captured_callback: list[Callable[[], None]] = []

    def cancel_registration_hook(callback: Callable[[], None]) -> None:
        captured_callback.append(callback)

    monkeypatch.setattr(manager, "kill", failing_kill)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    context = make_context(tmp_path, cancel_registration_hook=cancel_registration_hook)
    # Register the user-cancel callback through the handler's wiring without
    # spawning a real process by exercising the registrar directly.
    bash_module._register_user_cancel_callback(manager, context, "session-x")
    assert captured_callback, "cancel callback should have been registered"

    with caplog.at_level(logging.ERROR, logger="vbot.tools.bash"):
        # Fire the cancel callback: it schedules the failing kill task and
        # attaches the logging done-callback.
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
    assert kill_errors, "expected an error log for the failing user-cancel kill task"
    assert kill_errors[0].exc_info is not None
