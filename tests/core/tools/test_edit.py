"""Tests for the built-in edit tool."""

from pathlib import Path

import pytest

from core.tools.edit import (
    EDIT_TOOL_NAME,
    EDIT_TOOL_PARAMETERS,
    edit_handler,
    register_edit_tool,
)
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope


def make_context(workspace: Path, tool_name: str = EDIT_TOOL_NAME) -> ToolContext:
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
    assert set(data) == {"message", "path", "first_changed_line", "replacements"}
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


def test_register_edit_tool_exposes_provider_schema() -> None:
    registry = ToolRegistry()

    register_edit_tool(registry)

    tool = registry.get("edit")
    assert tool.name == EDIT_TOOL_NAME == "edit"
    assert tool.parameters == EDIT_TOOL_PARAMETERS

    definitions = registry.provider_definitions(["edit"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert set(definition) == {"name", "description", "parameters"}
    assert definition["name"] == "edit"

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["path", "old_string", "new_string"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"path", "old_string", "new_string", "replace_all"}
    assert "filePath" not in parameters["properties"]


def test_edit_replaces_text_in_relative_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("hello workspace\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "workspace", "new_string": "agent"},
    )

    data = assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "hello agent\n"
    assert data["replacements"] == 1
    assert data["first_changed_line"] == 1
    assert data["path"] == str(target.resolve())
    assert isinstance(data["message"], str)
    assert "OK: updated" in data["message"]


def test_edit_replaces_text_in_absolute_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("absolute path\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": str(target), "old_string": "absolute", "new_string": "direct"},
    )

    data = assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "direct path\n"
    assert data["path"] == str(target.resolve())


def test_edit_returns_failure_envelope_for_missing_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = edit_handler(
        make_context(workspace),
        {"path": "missing.txt", "old_string": "old", "new_string": "new"},
    )

    error = assert_failure_envelope(result, "file_not_found")
    assert "missing.txt" in error["message"]


def test_edit_returns_failure_envelope_for_directory_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("folder").mkdir()

    result = edit_handler(
        make_context(workspace),
        {"path": "folder", "old_string": "old", "new_string": "new"},
    )

    error = assert_failure_envelope(result, "not_a_file")
    assert "folder" in error["message"]


def test_edit_returns_failure_for_empty_old_string(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "", "new_string": "new"},
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert error["message"] == "old_string must not be empty"


def test_edit_returns_failure_for_identical_strings(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "hello", "new_string": "hello"},
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "identical" in error["message"]


def test_edit_returns_failure_for_not_found_text(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "missing", "new_string": "new"},
    )

    error = assert_failure_envelope(result, "text_not_found")
    assert "old_string not found" in error["message"]


def test_edit_returns_ambiguous_match_without_replace_all(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("same\nother\nsame\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "same", "new_string": "changed"},
    )

    error = assert_failure_envelope(result, "ambiguous_match")
    assert "Found 2 occurrences" in error["message"]
    assert target.read_text(encoding="utf-8") == "same\nother\nsame\n"


def test_edit_replaces_ambiguous_matches_with_replace_all(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("same\nother\nsame\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "same", "new_string": "changed", "replace_all": True},
    )

    data = assert_success_envelope(result)
    assert target.read_text(encoding="utf-8") == "changed\nother\nchanged\n"
    assert data["replacements"] == 2
    assert data["first_changed_line"] == 1


def test_edit_normalizes_newlines_for_matching_and_replacement(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

    result = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "alpha\nbeta", "new_string": "one\ntwo"},
    )

    data = assert_success_envelope(result)
    assert target.read_bytes() == b"one\r\ntwo\r\ngamma\r\n"
    assert data["replacements"] == 1


def test_edit_returns_failure_for_unknown_argument(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")

    result = edit_handler(
        make_context(workspace),
        {
            "path": "notes.txt",
            "old_string": "hello",
            "new_string": "hi",
            "filePath": "notes.txt",
        },
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "filePath" in error["message"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"old_string": "old", "new_string": "new"}, "path must be a non-empty string"),
        (
            {"path": 123, "old_string": "old", "new_string": "new"},
            "path must be a non-empty string",
        ),
        ({"path": "notes.txt", "new_string": "new"}, "old_string must be a string"),
        (
            {"path": "notes.txt", "old_string": 123, "new_string": "new"},
            "old_string must be a string",
        ),
        ({"path": "notes.txt", "old_string": "old"}, "new_string must be a string"),
        (
            {"path": "notes.txt", "old_string": "old", "new_string": 123},
            "new_string must be a string",
        ),
        (
            {"path": "notes.txt", "old_string": "old", "new_string": "new", "replace_all": "true"},
            "replace_all must be a boolean",
        ),
    ],
)
def test_edit_returns_failure_for_invalid_argument_types(
    tmp_path: Path,
    arguments: dict[str, object],
    message: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("old\n", encoding="utf-8")

    result = edit_handler(make_context(workspace), arguments)

    error = assert_failure_envelope(result, "invalid_arguments")
    assert error["message"] == message


def test_edit_success_and_failure_results_are_valid_envelopes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("old\n", encoding="utf-8")

    success = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "old", "new_string": "new"},
    )
    failure = edit_handler(
        make_context(workspace),
        {"path": "notes.txt", "old_string": "missing", "new_string": "replacement"},
    )

    assert is_tool_result_envelope(success) is True
    assert is_tool_result_envelope(failure) is True
