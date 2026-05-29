"""Tests for the built-in memory tool."""

from pathlib import Path
from typing import Any

from core.memory import MemoryService
from core.tools.memory import (
    MEMORY_TOOL_DESCRIPTION,
    MEMORY_TOOL_NAME,
    MEMORY_TOOL_PARAMETERS,
    memory_handler,
    register_memory_tool,
)
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope

JsonObject = dict[str, Any]


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
        app_root=data_root.parent,
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
    assert tool.parameters == MEMORY_TOOL_PARAMETERS
    definition = registry.provider_definitions(["memory"])[0]
    assert definition["name"] == "memory"
    assert set(definition["parameters"]["properties"]) == {
        "action",
        "content",
        "entry_id",
        "scope",
    }


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
    assert "Unknown argument" in error["message"]


def test_memory_tool_returns_memory_errors(tmp_path: Path) -> None:
    context = make_context(tmp_path)

    result = memory_handler(
        context,
        {"action": "remove", "scope": "agent", "entry_id": 1},
        MemoryService(),
    )

    error = assert_failure(result, "memory_error")
    assert "entry_id" in error["message"]
