"""Tests for the built-in read2 tool."""

from pathlib import Path

from core.tools.read2 import READ2_TOOL_NAME, read2_handler, register_read2_tool
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope


def make_context(workspace: Path, tool_name: str = READ2_TOOL_NAME) -> ToolContext:
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


def test_register_read2_tool_exposes_provider_schema_without_description_property() -> None:
    registry = ToolRegistry()

    register_read2_tool(registry)

    tool = registry.get("read2")
    assert tool.name == "read2"
    definitions = registry.provider_definitions(["read2"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert set(definition) == {"name", "description", "parameters"}
    assert definition["name"] == "read2"

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["path"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"path", "offset", "limit"}
    assert "description" not in parameters["properties"]


def test_read2_reads_relative_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"hello\nworkspace\n")

    result = read2_handler(make_context(workspace), {"path": "notes.txt"})

    data = assert_success_envelope(result)
    assert data["content"] == "hello\nworkspace\n"
    assert data["path"] == str(workspace.joinpath("notes.txt").resolve())


def test_read2_reads_absolute_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt"
    target.write_bytes(b"absolute\npath\n")

    result = read2_handler(make_context(workspace), {"path": str(target)})

    data = assert_success_envelope(result)
    assert data["content"] == "absolute\npath\n"
    assert data["path"] == str(target.resolve())


def test_read2_returns_failure_envelope_for_missing_path_argument(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read2_handler(make_context(workspace), {})

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "path" in error["message"]


def test_read2_returns_failure_envelope_for_unknown_argument(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"hello\n")

    result = read2_handler(
        make_context(workspace),
        {"path": "notes.txt", "description": "display-only label"},
    )
    error = assert_failure_envelope(result, "invalid_arguments")
    assert "description" in error["message"]


def test_read2_returns_failure_envelope_for_missing_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read2_handler(make_context(workspace), {"path": "missing.txt"})
    error = assert_failure_envelope(result, "file_not_found")
    assert "missing.txt" in error["message"]


def test_read2_returns_failure_envelope_for_directory_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("folder").mkdir()

    result = read2_handler(make_context(workspace), {"path": "folder"})
    error = assert_failure_envelope(result, "not_a_file")
    assert "folder" in error["message"]


def test_read2_applies_line_offset_and_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"one\ntwo\nthree\nfour\n")

    result = read2_handler(make_context(workspace), {"path": "notes.txt", "offset": 2, "limit": 2})

    data = assert_success_envelope(result)
    assert data["content"] == "two\nthree\n[Showing lines 2-3 of 4. Use offset=4 to continue.]"


def test_read2_returns_eof_notice_when_offset_is_past_end(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"one\ntwo\n")

    result = read2_handler(make_context(workspace), {"path": "notes.txt", "offset": 5})

    data = assert_success_envelope(result)
    assert data["content"] == "[Offset 5 is beyond end of file (2 lines). Nothing to show.]"


def test_read2_returns_failure_envelope_for_invalid_line_controls(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"one\n")

    result = read2_handler(make_context(workspace), {"path": "notes.txt", "limit": 0})

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "limit" in error["message"]
