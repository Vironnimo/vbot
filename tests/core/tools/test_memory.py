"""Tests for the built-in memory tool."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.memory import MemoryService
from core.tools.memory import (
    _MAX_MEMORY_FAILURES_PER_RUN,
    MEMORY_TOOL_DESCRIPTION,
    MEMORY_TOOL_NAME,
    MEMORY_TOOL_PARAMETERS,
    _MemoryThrashTracker,
    register_memory_tool,
)
from core.tools.memory import (
    memory_handler as _memory_handler,
)
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope

JsonObject = dict[str, Any]


def memory_handler(
    context: ToolContext,
    arguments: JsonObject,
    service: MemoryService,
    tracker: _MemoryThrashTracker | None = None,
) -> JsonObject:
    return _memory_handler(context, arguments, service, tracker)


def make_context(data_root: Path) -> ToolContext:
    workspace = data_root / "workspace"
    workspace.mkdir(exist_ok=True)
    return ToolContext(
        agent_id="main",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=MEMORY_TOOL_NAME,
        tool_call_index=0,
        workspace=workspace,
        vbot_root=data_root.parent,
        data_root=data_root,
    )


def assert_success(result: JsonObject) -> JsonObject:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    return data


def assert_failure(result: JsonObject, code: str) -> dict[str, str]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is False
    assert result["data"] is None
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    return error  # type: ignore[return-value]


def test_register_memory_tool_exposes_provider_schema(tmp_path: Path) -> None:
    registry = ToolRegistry()

    register_memory_tool(registry, MemoryService())

    tool = registry.get(MEMORY_TOOL_NAME)
    assert tool.name == "memory"
    assert tool.description == MEMORY_TOOL_DESCRIPTION
    assert tool.description
    assert tool.parameters == MEMORY_TOOL_PARAMETERS
    definition = registry.provider_definitions(["memory"])[0]
    assert definition["name"] == "memory"
    parameters = definition["parameters"]
    assert set(parameters["properties"]) == {"action", "scope", "content", "entry_id"}
    assert parameters["properties"]["action"]["enum"] == ["list", "add", "replace", "remove"]
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in parameters["properties"].values()
    )
    assert parameters["required"] == ["action", "scope"]
    assert "oneOf" not in parameters
    assert "additionalProperties" not in parameters
    assert tool.open_input_schema is True
    list_display = registry.display_for_call(
        MEMORY_TOOL_NAME,
        {"action": "list", "scope": "user"},
        result={
            "ok": True,
            "data": {"entries": [{"id": 1}]},
            "error": None,
            "artifacts": [],
        },
    )
    mutation_display = registry.display_for_call(
        MEMORY_TOOL_NAME,
        {"action": "add", "scope": "user"},
        result={
            "ok": True,
            "data": {"entries": [{"id": 1}]},
            "error": None,
            "artifacts": [],
        },
    )
    assert list_display["facts"] == [
        {"kind": "count", "value": 1, "unit": "results", "at_least": False}
    ]
    assert mutation_display["facts"] == []


def test_memory_tool_adds_and_lists_user_entries(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    service = MemoryService()

    add_result = memory_handler(
        context,
        {"action": "add", "scope": "user", "content": "Prefers direct answers."},
        service,
    )
    list_result = memory_handler(context, {"action": "list", "scope": "user"}, service)

    add_data = assert_success(add_result)
    list_data = assert_success(list_result)
    assert add_data["entry"] == {
        "id": 1,
        "scope": "user",
        "content": "Prefers direct answers.",
    }
    assert list_data["entries"] == [add_data["entry"]]
    assert "Prefers direct answers." in (context.workspace / "USER.md").read_text(encoding="utf-8")


def test_memory_add_handler_rejects_entry_id(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_memory_tool(registry, MemoryService())
    context = make_context(tmp_path)

    result = asyncio.run(
        registry.dispatch(
            context,
            {
                "action": "add",
                "scope": "user",
                "content": "Prefers direct answers.",
                "entry_id": 1,
            },
            [MEMORY_TOOL_NAME],
        )
    )

    error = assert_failure(result, "invalid_arguments")
    assert "entry_id" in error["message"]
    assert not (context.workspace / "USER.md").exists()


@pytest.mark.parametrize(
    "arguments",
    (
        {"action": "list"},
        {"action": "add", "scope": "user"},
        {"action": "replace", "scope": "agent", "entry_id": 1},
        {"action": "replace", "scope": "agent", "content": "new"},
        {"action": "remove", "scope": "agent"},
    ),
)
def test_memory_handler_rejects_missing_action_fields(
    tmp_path: Path, arguments: JsonObject
) -> None:
    result = memory_handler(make_context(tmp_path), arguments, MemoryService())

    assert_failure(result, "invalid_arguments")


def test_memory_tool_rejects_retired_operation_shapes(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    service = MemoryService()

    nested_result = memory_handler(
        context,
        {
            "request": {
                "operation": "add",
                "scope": "user",
                "content": "Prefers direct answers.",
            }
        },
        service,
    )
    operation_key_result = memory_handler(
        context,
        {"add": {"scope": "user", "content": "Prefers direct answers."}},
        service,
    )

    for result in (nested_result, operation_key_result):
        error = assert_failure(result, "invalid_arguments")
        assert "action must be one of" in error["message"]


def test_memory_tool_replaces_and_removes_agent_entries(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    service = MemoryService()
    memory_handler(context, {"action": "add", "scope": "agent", "content": "old"}, service)

    replace_result = memory_handler(
        context,
        {"action": "replace", "scope": "agent", "entry_id": 1, "content": "new"},
        service,
    )
    remove_result = memory_handler(
        context,
        {"action": "remove", "scope": "agent", "entry_id": 1},
        service,
    )

    replace_data = assert_success(replace_result)
    remove_data = assert_success(remove_result)
    assert replace_data["entry"]["content"] == "new"
    assert remove_data["entry"]["content"] == "new"
    assert remove_data["entries"] == []


def test_memory_tool_rejects_invalid_arguments(tmp_path: Path) -> None:
    context = make_context(tmp_path)

    result = memory_handler(
        context,
        {"action": "add", "scope": "user", "unknown": True},
        MemoryService(),
    )

    error = assert_failure(result, "invalid_arguments")
    assert "Unknown add argument" in error["message"]


def test_memory_tool_returns_memory_errors(tmp_path: Path) -> None:
    context = make_context(tmp_path)

    result = memory_handler(
        context,
        {"action": "remove", "scope": "agent", "entry_id": 1},
        MemoryService(),
    )

    error = assert_failure(result, "memory_error")
    assert "entry_id" in error["message"]


def test_thrash_guard_cuts_off_repeated_mutation_failures(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    service = MemoryService()
    tracker = _MemoryThrashTracker()
    failing_remove = {"action": "remove", "scope": "agent", "entry_id": 1}

    # Below the cap: the underlying recoverable error, flagged retryable.
    for _ in range(_MAX_MEMORY_FAILURES_PER_RUN):
        error = assert_failure(
            memory_handler(context, failing_remove, service, tracker), "memory_error"
        )
        assert "entry_id" in error["message"]
        assert error["retryable"] is True

    # At the cap the failure flips terminal: stop-retrying message, non-retryable.
    error = assert_failure(
        memory_handler(context, failing_remove, service, tracker), "memory_error"
    )
    assert error["retryable"] is False
    assert "Stop retrying" in error["message"]
    assert error["attempts_made"] == _MAX_MEMORY_FAILURES_PER_RUN + 1


def test_thrash_guard_resets_on_successful_mutation(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    service = MemoryService()
    tracker = _MemoryThrashTracker()
    failing_remove = {"action": "remove", "scope": "agent", "entry_id": 1}

    for _ in range(_MAX_MEMORY_FAILURES_PER_RUN):
        assert_failure(memory_handler(context, failing_remove, service, tracker), "memory_error")

    # A successful mutation clears the run's streak, so the guard starts over.
    assert_success(
        memory_handler(
            context, {"action": "add", "scope": "agent", "content": "x"}, service, tracker
        )
    )

    # An out-of-range id fails whether or not the scope holds entries, so the streak
    # restarts from a plain recoverable failure rather than the terminal cutoff.
    out_of_range = {"action": "remove", "scope": "agent", "entry_id": 99}
    error = assert_failure(memory_handler(context, out_of_range, service, tracker), "memory_error")
    assert error["retryable"] is True
    assert "entry_id" in error["message"]


def test_thrash_guard_is_scoped_per_run(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    service = MemoryService()
    tracker = _MemoryThrashTracker()
    failing_remove = {"action": "remove", "scope": "agent", "entry_id": 1}

    for _ in range(_MAX_MEMORY_FAILURES_PER_RUN + 1):
        memory_handler(context, failing_remove, service, tracker)

    # A different run keeps its own streak: the first failure there is still recoverable.
    other_run = ToolContext(
        agent_id="main",
        session_id="session-1",
        run_id="run-2",
        tool_call_id="call-1",
        tool_name=MEMORY_TOOL_NAME,
        tool_call_index=0,
        workspace=context.workspace,
        vbot_root=context.vbot_root,
        data_root=context.data_root,
    )
    error = assert_failure(
        memory_handler(other_run, failing_remove, service, tracker), "memory_error"
    )
    assert error["retryable"] is True
