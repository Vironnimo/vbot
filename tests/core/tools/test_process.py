"""Tests for Agent-facing control of background bash Process Sessions."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

import core.tools.process as process_module
from core.tools.process import (
    PROCESS_ACTIONS,
    PROCESS_TOOL_DESCRIPTION,
    PROCESS_TOOL_NAME,
    PROCESS_TOOL_PARAMETERS,
    make_process_handler,
    register_process_tool,
)
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


def make_context(
    tmp_path: Path,
    *,
    agent_id: str = AGENT_A,
    result_persisted_hook: Callable[[Callable[[], None]], None] | None = None,
) -> ToolContext:
    return ToolContext(
        agent_id=agent_id,
        session_id="chat-session-a",
        run_id=RUN_A,
        tool_call_id="tool-call-a",
        tool_name=PROCESS_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        result_persisted_hook=result_persisted_hook,
    )


async def call_process(
    manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    return cast(JsonObject, await make_process_handler(manager)(context, arguments))


async def dispatch_process(
    manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    registry = ToolRegistry()
    register_process_tool(registry, manager)
    try:
        return await registry.dispatch(context, arguments, [PROCESS_TOOL_NAME])
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error), retryable=False)


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


def test_schema_exposes_small_flat_action_contract() -> None:
    assert PROCESS_TOOL_DESCRIPTION == (
        "Inspect or control your own background Process Sessions created by the `bash` Tool. "
        "Use the session_id returned when a bash call continues in the background; this Tool "
        "cannot access arbitrary operating-system processes. Bash output is only a capped "
        "snapshot; when log_file is present, it receives the complete combined stdout/stderr "
        "stream live through exit. Completion is delivered automatically, so use status only "
        "for an immediate snapshot, input to send stdin, and kill to stop a Process Session."
    )
    assert PROCESS_TOOL_PARAMETERS["type"] == "object"
    branches = {
        branch["properties"]["action"]["enum"][0]: branch
        for branch in PROCESS_TOOL_PARAMETERS["oneOf"]
    }
    assert set(branches) == set(PROCESS_ACTIONS)
    assert set(branches["status"]["properties"]) == {"action", "session_id"}
    assert branches["status"]["required"] == ["action"]
    assert set(branches["input"]["properties"]) == {
        "action",
        "session_id",
        "text",
        "newline",
        "eof",
    }
    assert branches["input"]["required"] == ["action", "session_id", "text"]
    assert set(branches["kill"]["properties"]) == {"action", "session_id"}
    assert branches["kill"]["required"] == ["action", "session_id"]
    assert all(branch["additionalProperties"] is False for branch in branches.values())
    input_properties = cast(dict[str, Any], branches["input"]["properties"])
    assert "returned by the bash Tool" in input_properties["session_id"]["description"]
    assert input_properties["newline"]["default"] is True
    assert input_properties["eof"]["default"] is False


@pytest.mark.asyncio
async def test_status_without_session_id_lists_owned_sessions_only(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    owned_session_id = await spawn_python(manager, "import time; time.sleep(30)")
    hidden_session_id = await spawn_python(manager, "import time; time.sleep(30)", agent_id=AGENT_B)

    result = await call_process(manager, context, {"action": "status"})
    await manager.kill(owned_session_id, AGENT_A)
    await manager.kill(hidden_session_id, AGENT_B)

    assert result["ok"] is True
    sessions = cast(dict[str, Any], result["data"])["sessions"]
    assert sessions == [
        {
            "session_id": owned_session_id,
            "status": "running",
            "exit_code": None,
            "started_at": manager.get_session(owned_session_id, AGENT_A).started_at.isoformat(),
            "finished_at": None,
            "log_file": None,
        }
    ]


@pytest.mark.asyncio
async def test_status_with_session_id_returns_non_consuming_snapshot(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    session_id = await spawn_python(manager, "print('snapshot-output')")
    await wait_for_terminal(manager, session_id)

    first = await call_process(
        manager,
        context,
        {"action": "status", "session_id": session_id},
    )
    second = await call_process(
        manager,
        context,
        {"action": "status", "session_id": session_id},
    )

    first_data = cast(dict[str, Any], first["data"])
    second_data = cast(dict[str, Any], second["data"])
    assert first_data == second_data
    assert first_data["session_id"] == session_id
    assert first_data["status"] == "completed"
    assert first_data["exit_code"] == 0
    assert first_data["output_tail"].strip() == "snapshot-output"
    assert first_data["output_truncated"] is False
    assert first_data["waiting_for_input"] is False
    assert first_data["log_file"] is None


@pytest.mark.asyncio
async def test_status_caps_output_tail(
    manager: ProcessManager,
    context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_module, "PROCESS_STATUS_OUTPUT_CAP_CHARS", 5)
    session_id = await spawn_python(manager, "print('123456789')")
    await wait_for_terminal(manager, session_id)

    result = await call_process(
        manager,
        context,
        {"action": "status", "session_id": session_id},
    )

    data = cast(dict[str, Any], result["data"])
    assert data["output_tail"] in {"6789\n", "789\r\n"}
    assert data["output_truncated"] is True


@pytest.mark.asyncio
async def test_status_reports_waiting_for_input_after_idle_period(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    session_id = await spawn_python(manager, "import time; time.sleep(30)")
    session = manager.get_session(session_id, AGENT_A)
    session.started_at = datetime.now(UTC) - timedelta(seconds=16)

    result = await call_process(
        manager,
        context,
        {"action": "status", "session_id": session_id},
    )
    await manager.kill(session_id, AGENT_A)

    assert cast(dict[str, Any], result["data"])["waiting_for_input"] is True


@pytest.mark.asyncio
async def test_input_sends_one_line_by_default(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    script = "import sys; line = sys.stdin.readline(); print('got:' + line.strip())"
    session_id = await spawn_python(manager, script)

    result = await call_process(
        manager,
        context,
        {"action": "input", "session_id": session_id, "text": "value"},
    )
    terminal = await manager.poll(session_id, AGENT_A, timeout_ms=2000)

    assert result == tool_success(
        {
            "session_id": session_id,
            "characters_sent": 5,
            "newline": True,
            "eof": False,
        }
    )
    assert "got:value" in str(terminal["output"])


@pytest.mark.asyncio
async def test_input_can_send_raw_text_and_close_stdin(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    script = "import sys; data = sys.stdin.read(); print('read:' + data)"
    session_id = await spawn_python(manager, script)

    result = await call_process(
        manager,
        context,
        {
            "action": "input",
            "session_id": session_id,
            "text": "payload",
            "newline": False,
            "eof": True,
        },
    )
    terminal = await manager.poll(session_id, AGENT_A, timeout_ms=2000)

    assert result == tool_success(
        {
            "session_id": session_id,
            "characters_sent": 7,
            "newline": False,
            "eof": True,
        }
    )
    assert "read:payload" in str(terminal["output"])


@pytest.mark.asyncio
async def test_input_rejects_a_noop(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    result = await call_process(
        manager,
        context,
        {
            "action": "input",
            "session_id": "session-a",
            "text": "",
            "newline": False,
            "eof": False,
        },
    )

    assert result == tool_failure(
        "invalid_arguments",
        "input must send text, append a newline, or close stdin with eof",
        retryable=False,
    )


@pytest.mark.asyncio
async def test_kill_stops_a_process(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    session_id = await spawn_python(manager, "import time; time.sleep(30)")

    result = await call_process(
        manager,
        context,
        {"action": "kill", "session_id": session_id},
    )

    assert result == tool_success({"session_id": session_id, "status": "killed"})


@pytest.mark.parametrize("action", ["status", "kill"])
@pytest.mark.asyncio
async def test_terminal_manual_result_cancels_pending_completion_after_persistence(
    manager: ProcessManager,
    tmp_path: Path,
    action: str,
) -> None:
    callbacks: list[Callable[[], None]] = []
    context = make_context(
        tmp_path,
        result_persisted_hook=lambda callback: callbacks.append(callback),
    )
    script = "print('done')" if action == "status" else "import time; time.sleep(30)"
    session_id = await spawn_python(manager, script)
    if action == "status":
        await wait_for_terminal(manager, session_id)

    notification_release = asyncio.Event()

    async def pending_notification() -> None:
        await notification_release.wait()

    notification_task = asyncio.create_task(pending_notification())
    manager.register_completion_notification(session_id, AGENT_A, notification_task)

    result = await call_process(
        manager,
        context,
        {"action": action, "session_id": session_id},
    )

    assert result["ok"] is True
    assert len(callbacks) == 1
    assert notification_task.done() is False

    callbacks.pop()()
    await asyncio.sleep(0)

    assert notification_task.cancelled() is True
    assert manager.get_session(session_id, AGENT_A).completion_acknowledged is True


@pytest.mark.parametrize(
    "arguments",
    (
        {"request": {"operation": "poll", "session_id": "process-a"}},
        {"poll": {"session_id": "process-a"}},
        {"action": "list"},
        {"action": "poll", "session_id": "process-a"},
        {"action": "log", "session_id": "process-a"},
        {"action": "write", "session_id": "process-a", "data": "value"},
        {"action": "submit", "session_id": "process-a"},
        {"action": "clear", "session_id": "process-a"},
    ),
)
@pytest.mark.asyncio
async def test_retired_process_calls_are_rejected(
    manager: ProcessManager,
    context: ToolContext,
    arguments: JsonObject,
) -> None:
    result = await dispatch_process(manager, context, arguments)

    assert result["ok"] is False
    assert cast(dict[str, Any], result["error"])["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_action_inapplicable_fields_are_rejected(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    result = await call_process(
        manager,
        context,
        {"action": "status", "text": "not valid for status"},
    )

    assert result == tool_failure(
        "invalid_arguments",
        "Action 'status' does not accept: text",
        retryable=False,
    )


@pytest.mark.parametrize("action", PROCESS_ACTIONS)
@pytest.mark.asyncio
async def test_cross_agent_process_access_returns_not_found(
    manager: ProcessManager,
    tmp_path: Path,
    action: str,
) -> None:
    session_id = await spawn_python(manager, "import time; time.sleep(30)")
    arguments: JsonObject = {"action": action, "session_id": session_id}
    if action == "input":
        arguments["text"] = "value"

    result = await call_process(
        manager,
        make_context(tmp_path, agent_id=AGENT_B),
        arguments,
    )
    await manager.kill(session_id, AGENT_A)

    assert result == tool_failure(
        "session_not_found",
        "Process Session not found",
        retryable=False,
    )
