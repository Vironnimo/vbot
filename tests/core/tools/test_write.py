"""Tests for the built-in write tool."""

from pathlib import Path

import pytest

from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope
from core.tools.write import (
    WRITE_TOOL_NAME,
    WRITE_TOOL_PARAMETERS,
    register_write_tool,
    write_handler,
)


def make_context(workspace: Path, tool_name: str = WRITE_TOOL_NAME) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=workspace,
        app_root=workspace.parent,
        data_root=workspace.parent / "data",
    )


def assert_success_envelope(result: dict[str, object]) -> dict[str, object]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    assert set(data) == {"path", "bytes", "message"}
    assert isinstance(data["path"], str)
    assert isinstance(data["bytes"], int)
    assert isinstance(data["message"], str)
    return data


def assert_failure_envelope(result: dict[str, object], code: str) -> dict[str, str]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is False
    assert result["data"] is None
    assert result["artifacts"] == []
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    return error  # type: ignore[return-value]


def test_register_write_tool_exposes_provider_schema() -> None:
    registry = ToolRegistry()

    register_write_tool(registry)

    tool = registry.get("write")
    assert tool.name == WRITE_TOOL_NAME == "write"
    assert tool.parameters == WRITE_TOOL_PARAMETERS

    definitions = registry.provider_definitions(["write"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert set(definition) == {"name", "description", "parameters"}
    assert definition["name"] == "write"

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["path", "content"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"path", "content"}
    assert parameters["properties"]["path"]["type"] == "string"
    assert parameters["properties"]["content"]["type"] == "string"


def test_write_writes_relative_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "hello\nworkspace\n"},
    )

    data = assert_success_envelope(result)
    target = workspace / "notes.txt"
    assert target.read_text(encoding="utf-8") == "hello\nworkspace\n"
    assert data["path"] == str(target.resolve())
    assert data["bytes"] == len(b"hello\nworkspace\n")
    assert data["message"] == f"OK: written {data['bytes']} bytes to {target.resolve()}"


def test_write_writes_absolute_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt"

    result = write_handler(
        make_context(workspace),
        {"path": str(target), "content": "absolute\npath\n"},
    )

    data = assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "absolute\npath\n"
    assert data["path"] == str(target.resolve())


def test_write_creates_parent_directories(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "nested" / "deeper" / "notes.txt"

    result = write_handler(
        make_context(workspace),
        {"path": "nested/deeper/notes.txt", "content": "created parents"},
    )

    assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "created parents"


def test_write_replaces_full_file_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("old content that should disappear", encoding="utf-8")

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "new"},
    )

    assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "path must be a non-empty string"),
        ({"path": "notes.txt"}, "content must be a string"),
        ({"content": "hello"}, "path must be a non-empty string"),
        ({"path": "", "content": "hello"}, "path must be a non-empty string"),
        ({"path": 1, "content": "hello"}, "path must be a non-empty string"),
        ({"path": "notes.txt", "content": 1}, "content must be a string"),
        ({"path": "notes.txt", "content": None}, "content must be a string"),
    ],
)
def test_write_returns_failure_envelope_for_invalid_arguments(
    tmp_path: Path,
    arguments: dict[str, object],
    message: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = write_handler(make_context(workspace), arguments)

    error = assert_failure_envelope(result, "invalid_arguments")
    assert error["message"] == message


def test_write_returns_failure_envelope_for_unknown_argument(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "hello", "filePath": "legacy.txt"},
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "filePath" in error["message"]


def test_write_returns_failure_envelope_for_filesystem_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"

    def raise_permission_error(self: Path, data: str, encoding: str | None = None) -> int:
        raise PermissionError("access denied while writing")

    monkeypatch.setattr(Path, "write_text", raise_permission_error)

    result = write_handler(
        make_context(workspace),
        {"path": "notes.txt", "content": "hello"},
    )

    error = assert_failure_envelope(result, "file_write_error")
    assert str(target.resolve()) in error["message"]
    assert "access denied while writing" in error["message"]


def test_write_success_and_failure_results_are_valid_envelopes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    success = write_handler(make_context(workspace), {"path": "notes.txt", "content": "hello"})
    failure = write_handler(make_context(workspace), {"path": "notes.txt", "content": 1})

    assert is_tool_result_envelope(success) is True
    assert is_tool_result_envelope(failure) is True
