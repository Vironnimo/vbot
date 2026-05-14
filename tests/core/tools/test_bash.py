"""Tests for the bash tool's process-manager integration."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

import core.tools.bash as bash_module
from core.tools.bash import BASH_TOOL_PARAMETERS, bash_handler, register_bash_tool
from core.tools.process_manager import ProcessManager
from core.tools.tools import ToolContext, ToolRegistry

AGENT_ID = "agent-a"
RUN_ID = "run-a"


@pytest.fixture(autouse=True)
def shell_env_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bash_module, "_cached_shell_env", {"PATH": "original-path"})


@pytest.fixture
def manager() -> ProcessManager:
    return ProcessManager(sweep_interval_seconds=3600)


def make_context(
    tmp_path: Path,
    *,
    emit_hook: Any = None,
    cancellation_hook: Any = None,
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
        emit_hook=emit_hook,
        cancellation_hook=cancellation_hook,
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
    assert result["data"]["stdout"].replace("\r\n", "\n") == "hello\n"
    assert result["data"]["stderr"] == ""
    assert "hello" in result["data"]["output"]
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
    assert "bad" in result["data"]["stderr"]


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
    assert "allowed" in result["data"]["stdout"]
    assert "original-path" in result["data"]["stdout"]
    assert "blocked-path" not in result["data"]["stdout"]


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
    assert result["data"]["stdout"].strip() == str(tmp_path)


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
async def test_large_foreground_stdout_is_bounded_and_truncated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ProcessManager(buffer_cap_bytes=32, sweep_interval_seconds=3600)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import sys; sys.stdout.write('a' * 64); sys.stdout.flush()"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["stdout"] == "a" * 32
    assert result["data"]["output"] == "a" * 32
    assert result["data"]["truncated"] is True


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
