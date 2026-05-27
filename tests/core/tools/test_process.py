"""Tests for the process tool."""

from __future__ import annotations

import inspect
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio

from core.tools.process import PROCESS_TOOL_NAME, make_process_handler, register_process_tool
from core.tools.process_manager import ProcessManager
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure, tool_success

AGENT_A = "agent-a"
AGENT_B = "agent-b"
RUN_A = "run-a"


@pytest_asyncio.fixture
async def manager() -> AsyncIterator[ProcessManager]:
    manager = ProcessManager(sweep_interval_seconds=3600)
    try:
        yield manager
    finally:
        await manager.aclose()


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    return make_context(tmp_path)


def make_context(tmp_path: Path, *, agent_id: str = AGENT_A) -> ToolContext:
    return ToolContext(
        agent_id=agent_id,
        session_id="chat-session-a",
        run_id=RUN_A,
        tool_call_id="tool-call-a",
        tool_name=PROCESS_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        app_root=tmp_path,
        data_root=tmp_path,
    )


async def call_process(
    manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    handler = make_process_handler(manager)
    result = handler(context, arguments)
    if inspect.isawaitable(result):
        result = await result
    return cast(JsonObject, result)


async def spawn_python(manager: ProcessManager, script: str, *, agent_id: str = AGENT_A) -> str:
    return await manager.spawn(
        RUN_A,
        agent_id,
        [sys.executable, "-c", script],
        env=None,
        cwd=None,
    )


async def wait_for_terminal(manager: ProcessManager, session_id: str) -> None:
    for _ in range(20):
        result = await manager.poll(session_id, AGENT_A, timeout_ms=500)
        if result["status"] != "running":
            return
    raise AssertionError("process did not finish")


@pytest.mark.asyncio
async def test_register_process_tool_registers_schema(manager: ProcessManager) -> None:
    registry = ToolRegistry()

    register_process_tool(registry, manager)

    tool = registry.get(PROCESS_TOOL_NAME)
    assert tool.name == PROCESS_TOOL_NAME
    assert tool.parameters["additionalProperties"] is False


@pytest.mark.asyncio
async def test_list_action_returns_empty_sessions(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    result = await call_process(manager, context, {"action": "list"})

    assert result == tool_success({"sessions": []})


@pytest.mark.asyncio
async def test_list_action_returns_owned_sessions_only(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    owned_session_id = await spawn_python(manager, "import time; time.sleep(30)")
    hidden_session_id = await spawn_python(manager, "import time; time.sleep(30)", agent_id=AGENT_B)

    result = await call_process(manager, context, {"action": "list"})
    await manager.kill(owned_session_id, AGENT_A)
    await manager.kill(hidden_session_id, AGENT_B)

    assert result["ok"] is True
    assert result["data"] == {
        "sessions": [
            {
                "session_id": owned_session_id,
                "status": "running",
                "exit_code": None,
                "started_at": manager.get_session(owned_session_id, AGENT_A).started_at.isoformat(),
                "finished_at": None,
            }
        ]
    }


@pytest.mark.asyncio
async def test_poll_action_returns_new_output_and_timeout(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    session_id = await spawn_python(
        manager,
        "import sys, time; time.sleep(0.1); print('later'); sys.stdout.flush()",
    )

    result = await call_process(
        manager,
        context,
        {"action": "poll", "session_id": session_id, "timeout_ms": 2000},
    )

    assert result["ok"] is True
    assert result["data"] == {
        "session_id": session_id,
        "status": "running",
        "output": "later\r\n" if sys.platform == "win32" else "later\n",
        "waiting_for_input": False,
    }
    await wait_for_terminal(manager, session_id)


@pytest.mark.asyncio
async def test_poll_timeout_is_capped(
    manager: ProcessManager,
    context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_timeout_ms: list[int] = []

    async def fake_poll(session_id: str, agent_id: str, timeout_ms: int = 0) -> JsonObject:
        captured_timeout_ms.append(timeout_ms)
        return {
            "session_id": session_id,
            "status": "running",
            "output": "",
            "waiting_for_input": False,
        }

    monkeypatch.setattr(manager, "poll", fake_poll)

    result = await call_process(
        manager,
        context,
        {"action": "poll", "session_id": "session-a", "timeout_ms": 60_000},
    )

    assert result == tool_success(
        {
            "session_id": "session-a",
            "status": "running",
            "output": "",
            "waiting_for_input": False,
        }
    )
    assert captured_timeout_ms == [30_000]


@pytest.mark.asyncio
async def test_poll_waiting_for_input_fires_after_idle_period(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    session_id = await spawn_python(manager, "import time; time.sleep(30)")
    session = manager.get_session(session_id, AGENT_A)
    session.started_at = datetime.now(UTC) - timedelta(seconds=16)

    result = await call_process(
        manager,
        context,
        {"action": "poll", "session_id": session_id},
    )
    await manager.kill(session_id, AGENT_A)

    assert result["ok"] is True
    assert result["data"]["waiting_for_input"] is True


@pytest.mark.asyncio
async def test_poll_waiting_for_input_stays_false_when_output_is_recent(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    session_id = await spawn_python(manager, "import time; time.sleep(30)")
    session = manager.get_session(session_id, AGENT_A)
    session.last_output_at = datetime.now(UTC)

    result = await call_process(
        manager,
        context,
        {"action": "poll", "session_id": session_id},
    )
    await manager.kill(session_id, AGENT_A)

    assert result["ok"] is True
    assert result["data"]["waiting_for_input"] is False


@pytest.mark.asyncio
async def test_log_action_returns_windowed_output(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    session_id = await spawn_python(
        manager,
        "import sys; sys.stdout.write('one\\ntwo\\nthree\\n'); sys.stdout.flush()",
    )
    await wait_for_terminal(manager, session_id)

    result = await call_process(
        manager,
        context,
        {"action": "log", "session_id": session_id, "offset": 1, "limit": 1},
    )

    output = result["data"]["output"]

    assert result["ok"] is True
    assert isinstance(output, str)
    assert output.replace("\r\n", "\n") == "two\n"
    assert result["data"]["session_id"] == session_id
    assert result["data"]["total_lines"] == 3


@pytest.mark.asyncio
async def test_log_action_uses_default_limit(
    manager: ProcessManager,
    context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_limit: list[int | None] = []

    async def fake_log(
        session_id: str,
        agent_id: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> JsonObject:
        captured_limit.append(limit)
        return {"session_id": session_id, "output": "", "total_lines": 0}

    monkeypatch.setattr(manager, "log", fake_log)

    await call_process(manager, context, {"action": "log", "session_id": "session-a"})

    assert captured_limit == [200]


@pytest.mark.asyncio
async def test_write_action_writes_stdin_and_returns_written_count(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    script = "import sys; data = sys.stdin.read(); print('read:' + data)"
    session_id = await spawn_python(manager, script)

    result = await call_process(
        manager,
        context,
        {"action": "write", "session_id": session_id, "data": "payload", "eof": True},
    )
    poll_result = await manager.poll(session_id, AGENT_A, timeout_ms=2000)

    assert result == tool_success({"session_id": session_id, "written": 7})
    assert "read:payload" in str(poll_result["output"])


@pytest.mark.asyncio
async def test_submit_action_sends_current_line(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    script = "import sys; line = sys.stdin.readline(); print('got:' + line.strip())"
    session_id = await spawn_python(manager, script)
    await manager.write(session_id, AGENT_A, "value")

    result = await call_process(manager, context, {"action": "submit", "session_id": session_id})
    poll_result = await manager.poll(session_id, AGENT_A, timeout_ms=2000)

    assert result == tool_success({"session_id": session_id})
    assert "got:value" in str(poll_result["output"])


@pytest.mark.asyncio
async def test_kill_action_stops_process(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    session_id = await spawn_python(manager, "import time; time.sleep(30)")

    result = await call_process(manager, context, {"action": "kill", "session_id": session_id})
    poll_result = await manager.poll(session_id, AGENT_A, timeout_ms=5000)

    assert result == tool_success({"session_id": session_id})
    assert poll_result["status"] == "killed"


@pytest.mark.asyncio
async def test_clear_action_removes_finished_session(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    session_id = await spawn_python(manager, "print('done')")
    await wait_for_terminal(manager, session_id)

    result = await call_process(manager, context, {"action": "clear", "session_id": session_id})

    assert result == tool_success({"session_id": session_id})
    assert manager.list_sessions(AGENT_A) == []


@pytest.mark.asyncio
async def test_missing_session_returns_session_not_found(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    result = await call_process(
        manager,
        context,
        {"action": "poll", "session_id": "missing-session"},
    )

    assert result == tool_failure("session_not_found", "Process session not found")


@pytest.mark.asyncio
async def test_clear_running_session_returns_session_still_running(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    session_id = await spawn_python(manager, "import time; time.sleep(30)")

    result = await call_process(manager, context, {"action": "clear", "session_id": session_id})
    await manager.kill(session_id, AGENT_A)

    assert result == tool_failure("session_still_running", "Process session is still running")


@pytest.mark.asyncio
async def test_invalid_params_return_invalid_arguments(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    result = await call_process(manager, context, {"action": "write", "session_id": "x"})

    assert result == tool_failure("invalid_arguments", "data must be a string")


@pytest.mark.parametrize("action", ["poll", "log", "write", "submit", "kill", "clear"])
@pytest.mark.asyncio
async def test_cross_agent_session_access_returns_session_not_found(
    manager: ProcessManager,
    tmp_path: Path,
    action: str,
) -> None:
    session_id = await spawn_python(manager, "import time; time.sleep(30)")
    arguments: JsonObject = {"action": action, "session_id": session_id}
    if action == "write":
        arguments["data"] = "value"

    result = await call_process(manager, make_context(tmp_path, agent_id=AGENT_B), arguments)
    await manager.kill(session_id, AGENT_A)

    assert result == tool_failure("session_not_found", "Process session not found")
