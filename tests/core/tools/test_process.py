"""Tests for Agent-facing control of background bash processes."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
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


async def wait_for_terminal(manager: ProcessManager, process_id: str) -> None:
    for _ in range(20):
        result = await manager.poll(process_id, AGENT_A, timeout_ms=500)
        if result["status"] != "running":
            return
    raise AssertionError("process did not finish")


def test_schema_exposes_small_flat_action_contract() -> None:
    assert PROCESS_TOOL_DESCRIPTION
    assert PROCESS_TOOL_PARAMETERS["type"] == "object"
    assert "oneOf" not in PROCESS_TOOL_PARAMETERS
    properties = cast(dict[str, Any], PROCESS_TOOL_PARAMETERS["properties"])
    assert properties["action"]["enum"] == list(PROCESS_ACTIONS)
    assert set(properties) == {"action", "process_id"}
    assert PROCESS_ACTIONS == ("status", "kill")
    assert PROCESS_TOOL_PARAMETERS["required"] == ["action"]
    assert "additionalProperties" not in PROCESS_TOOL_PARAMETERS
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in properties.values()
    )


@pytest.mark.asyncio
async def test_status_without_process_id_lists_owned_processes_only(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    owned_process_id = await spawn_python(manager, "import time; time.sleep(30)")
    hidden_process_id = await spawn_python(manager, "import time; time.sleep(30)", agent_id=AGENT_B)

    result = await call_process(manager, context, {"action": "status"})
    await manager.kill(owned_process_id, AGENT_A)
    await manager.kill(hidden_process_id, AGENT_B)

    assert result["ok"] is True
    tracked_processes = cast(dict[str, Any], result["data"])["processes"]
    assert tracked_processes == [
        {
            "process_id": owned_process_id,
            "status": "running",
            "exit_code": None,
            "started_at": manager.get_process(owned_process_id, AGENT_A).started_at.isoformat(),
            "finished_at": None,
            "log_file": None,
        }
    ]
    registry = ToolRegistry()
    register_process_tool(registry, manager)
    display = registry.display_for_call(PROCESS_TOOL_NAME, {"action": "status"}, result=result)
    assert display["facts"] == [{"kind": "count", "value": 1, "unit": "results", "at_least": False}]


@pytest.mark.asyncio
async def test_status_with_process_id_returns_non_consuming_snapshot(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    process_id = await spawn_python(manager, "print('snapshot-output')")
    await wait_for_terminal(manager, process_id)

    first = await call_process(
        manager,
        context,
        {"action": "status", "process_id": process_id},
    )
    second = await call_process(
        manager,
        context,
        {"action": "status", "process_id": process_id},
    )

    first_data = cast(dict[str, Any], first["data"])
    second_data = cast(dict[str, Any], second["data"])
    assert first_data == second_data
    assert first_data["process_id"] == process_id
    assert first_data["status"] == "completed"
    assert first_data["exit_code"] == 0
    assert first_data["output_tail"].strip() == "snapshot-output"
    assert first_data["output_truncated"] is False
    assert "stdin_open" not in first_data
    assert "waiting_for_input" not in first_data
    assert first_data["log_file"] is None


@pytest.mark.asyncio
async def test_status_caps_output_tail(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    process_id = await spawn_python(manager, "print('x' * 9000 + 'END-MARKER')")
    await wait_for_terminal(manager, process_id)

    result = await call_process(
        manager,
        context,
        {"action": "status", "process_id": process_id},
    )

    data = cast(dict[str, Any], result["data"])
    assert len(data["output_tail"]) <= 8000
    assert data["output_tail"].rstrip().endswith("END-MARKER")
    assert data["output_truncated"] is True


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("line_count", [0, 1, 100, 101, 200])
@pytest.mark.parametrize("has_log", [False, True])
def test_output_line_budget_preserves_text_and_log_reference(newline, line_count, has_log):
    lines = [f"line-{index}{newline}" for index in range(line_count)]
    output = "".join(lines)
    log_file = "C:/logs/command.log" if has_log else None
    fields = process_module.shape_process_output(output, log_file=log_file)
    assert fields["truncated"] is (line_count > 100)
    assert len(fields["output"]) <= 8000
    if line_count > 100:
        marker, tail = fields["output"].split("\n", 1)
        assert tail == "".join(lines[-100:])
        if has_log:
            assert fields["log_file"] == log_file
            assert log_file in marker
    else:
        assert fields["output"] == output


@pytest.mark.parametrize("size", [0, 7999, 8000, 8001, 20000])
@pytest.mark.parametrize("already_truncated", [False, True])
def test_output_character_budget_includes_marker(size, already_truncated):
    output = "x" * size
    fields = process_module.shape_process_output(output, truncated=already_truncated)
    assert fields["truncated"] is (already_truncated or size > 8000)
    assert len(fields["output"]) <= 8000
    if fields["truncated"]:
        assert output.endswith(fields["output"].split("\n", 1)[1])
    else:
        assert fields["output"] == output


@pytest.mark.asyncio
async def test_status_line_limit_preserves_complete_log_and_is_non_consuming(tmp_path):
    from core.storage import TemporaryFileManager

    manager = ProcessManager(temporary_files=TemporaryFileManager(tmp_path))
    try:
        process_id = await spawn_python(
            manager, "print('\\n'.join(f'line-{i}' for i in range(200)))"
        )
        await wait_for_terminal(manager, process_id)
        context = make_context(tmp_path)
        arguments = {"action": "status", "process_id": process_id}
        first = await call_process(manager, context, arguments)
        second = await call_process(manager, context, arguments)
        assert first == second
        data = first["data"]
        assert data["exit_code"] == 0
        assert data["output_truncated"] is True
        assert data["output_tail"].splitlines()[1:] == [f"line-{i}" for i in range(100, 200)]
        log_file = Path(data["log_file"])
        assert log_file.read_text(encoding="utf-8").splitlines() == [
            f"line-{i}" for i in range(200)
        ]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_kill_stops_a_process(
    manager: ProcessManager,
    context: ToolContext,
) -> None:
    process_id = await spawn_python(manager, "import time; time.sleep(30)")

    result = await call_process(
        manager,
        context,
        {"action": "kill", "process_id": process_id},
    )

    assert result == tool_success({"process_id": process_id, "status": "killed"})


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
    process_id = await spawn_python(manager, script)
    if action == "status":
        await wait_for_terminal(manager, process_id)

    notification_release = asyncio.Event()

    async def pending_notification() -> None:
        await notification_release.wait()

    notification_task = asyncio.create_task(pending_notification())
    manager.register_completion_notification(process_id, AGENT_A, notification_task)

    result = await call_process(
        manager,
        context,
        {"action": action, "process_id": process_id},
    )

    assert result["ok"] is True
    assert len(callbacks) == 1
    assert notification_task.done() is False

    callbacks.pop()()
    await asyncio.sleep(0)

    assert notification_task.cancelled() is True
    assert manager.get_process(process_id, AGENT_A).completion_acknowledged is True


@pytest.mark.parametrize(
    "arguments",
    (
        {"request": {"operation": "poll", "process_id": "process-a"}},
        {"poll": {"process_id": "process-a"}},
        {"action": "input", "process_id": "process-a", "text": "value"},
        {"action": "list"},
        {"action": "poll", "process_id": "process-a"},
        {"action": "log", "process_id": "process-a"},
        {"action": "write", "process_id": "process-a", "data": "value"},
        {"action": "submit", "process_id": "process-a"},
        {"action": "clear", "process_id": "process-a"},
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
@pytest.mark.parametrize("dispatch", [False, True])
async def test_removed_input_action_cannot_affect_a_running_process(manager, context, dispatch):
    process_id = await spawn_python(manager, "import time; time.sleep(30)")
    invoke = dispatch_process if dispatch else call_process
    result = await invoke(
        manager,
        context,
        {"action": "input", "process_id": process_id, "text": "value", "eof": True},
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert manager.get_process(process_id, AGENT_A).status == "running"
    assert manager.get_process(process_id, AGENT_A).proc.stdin is None
    await manager.kill(process_id, AGENT_A)


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
    process_id = await spawn_python(manager, "import time; time.sleep(30)")
    arguments: JsonObject = {"action": action, "process_id": process_id}

    result = await call_process(
        manager,
        make_context(tmp_path, agent_id=AGENT_B),
        arguments,
    )
    await manager.kill(process_id, AGENT_A)

    assert result == tool_failure(
        "process_not_found",
        "Process not found",
        retryable=False,
    )


@pytest.mark.asyncio
async def test_same_agent_process_access_returns_not_found_across_project_scopes(
    manager: ProcessManager, tmp_path: Path
) -> None:
    process_id = await manager.spawn(
        RUN_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        project_id="project-a",
        env=None,
        cwd=None,
    )
    context = replace(make_context(tmp_path, agent_id=AGENT_A), project_id="project-b")

    result = await call_process(manager, context, {"action": "status", "process_id": process_id})
    await manager.kill(process_id, AGENT_A, project_id="project-a")

    assert result == tool_failure("process_not_found", "Process not found", retryable=False)
