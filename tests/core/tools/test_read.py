"""Tests for the canonical built-in read tool."""

from pathlib import Path

import pytest

from core.tools import (
    READ_TOOL_NAME,
    READ_TOOL_PARAMETERS,
    ToolContext,
    ToolRegistry,
    is_tool_result_envelope,
    read_handler,
    register_read_tool,
)


def make_context(workspace: Path, tool_name: str = READ_TOOL_NAME) -> ToolContext:
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
    assert set(data) == {"content"}
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


def test_register_read_tool_exposes_provider_schema_without_description_property() -> None:
    registry = ToolRegistry()

    register_read_tool(registry)

    tool = registry.get("read")
    assert tool.name == READ_TOOL_NAME == "read"
    assert tool.parameters == READ_TOOL_PARAMETERS

    definitions = registry.provider_definitions(["read"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert set(definition) == {"name", "description", "parameters"}
    assert definition["name"] == "read"

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["path"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"path", "offset", "limit"}
    assert "description" not in parameters["properties"]


def test_read_reads_relative_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"hello\nworkspace\n")

    result = read_handler(make_context(workspace), {"path": "notes.txt"})

    data = assert_success_envelope(result)
    assert data["content"] == "hello\nworkspace\n"


def test_read_reads_absolute_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt"
    target.write_bytes(b"absolute\npath\n")

    result = read_handler(make_context(workspace), {"path": str(target)})

    data = assert_success_envelope(result)
    assert data["content"] == "absolute\npath\n"


def test_read_returns_failure_envelope_for_missing_path_argument(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_handler(make_context(workspace), {})

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "path" in error["message"]


def test_read_returns_failure_envelope_for_unknown_argument(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"hello\n")

    result = read_handler(
        make_context(workspace),
        {"path": "notes.txt", "description": "display-only label"},
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "description" in error["message"]


def test_read_returns_failure_envelope_for_missing_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_handler(make_context(workspace), {"path": "missing.txt"})

    error = assert_failure_envelope(result, "file_not_found")
    assert "missing.txt" in error["message"]


def test_read_returns_failure_envelope_for_directory_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("folder").mkdir()

    result = read_handler(make_context(workspace), {"path": "folder"})

    error = assert_failure_envelope(result, "not_a_file")
    assert "folder" in error["message"]


def test_read_returns_failure_envelope_for_read_time_filesystem_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"hello\n")

    def raise_permission_error(self: Path) -> bytes:
        raise PermissionError("access denied while reading")

    monkeypatch.setattr(Path, "read_bytes", raise_permission_error)

    result = read_handler(make_context(workspace), {"path": "notes.txt"})

    error = assert_failure_envelope(result, "file_read_error")
    assert str(target.resolve()) in error["message"]
    assert "access denied while reading" in error["message"]


def test_read_applies_line_offset_and_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"one\ntwo\nthree\nfour\n")

    result = read_handler(make_context(workspace), {"path": "notes.txt", "offset": 2, "limit": 2})

    data = assert_success_envelope(result)
    assert data["content"] == "two\nthree\n[Showing lines 2-3 of 4. Use offset=4 to continue.]"


def test_read_returns_eof_notice_when_offset_is_past_end(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"one\ntwo\n")

    result = read_handler(make_context(workspace), {"path": "notes.txt", "offset": 5})

    data = assert_success_envelope(result)
    assert data["content"] == "[Offset 5 is beyond end of file (2 lines). Nothing to show.]"


@pytest.mark.parametrize(
    ("line_control", "message"),
    [
        ({"limit": 0}, "limit must be >= 1"),
        ({"limit": True}, "limit must be a positive integer"),
        ({"limit": 1.5}, "limit must be a positive integer"),
        ({"offset": 0}, "offset must be >= 1"),
        ({"offset": True}, "offset must be a positive integer"),
        ({"offset": 1.5}, "offset must be a positive integer"),
    ],
)
def test_read_returns_failure_envelope_for_invalid_line_controls(
    tmp_path: Path,
    line_control: dict[str, object],
    message: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"one\n")

    result = read_handler(make_context(workspace), {"path": "notes.txt", **line_control})

    error = assert_failure_envelope(result, "invalid_arguments")
    assert error["message"] == message


def test_read_accepts_integer_valued_float_offset(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result_float = read_handler(make_context(workspace), {"path": "lines.txt", "offset": 2.0})
    result_int = read_handler(make_context(workspace), {"path": "lines.txt", "offset": 2})

    assert result_float == result_int


def test_read_accepts_integer_valued_float_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result_float = read_handler(make_context(workspace), {"path": "lines.txt", "limit": 2.0})
    result_int = read_handler(make_context(workspace), {"path": "lines.txt", "limit": 2})

    assert result_float == result_int


def test_read_default_limit_truncates_large_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lines = "".join(f"line{i}\n" for i in range(1, 2002))
    workspace.joinpath("big.txt").write_text(lines, encoding="utf-8")

    result = read_handler(make_context(workspace), {"path": "big.txt"})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "[Showing lines 1-2000 of 2001." in content
    assert "line2001" not in content


def test_read_byte_limit_truncates_output(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("huge.txt").write_bytes(("x" * 60000 + "\n").encode("utf-8"))

    result = read_handler(make_context(workspace), {"path": "huge.txt"})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert len(content.encode("utf-8")) <= 50 * 1024 + 500
    assert "Output truncated at 50 KB" in content


def test_read_invalid_utf8_uses_replacement_character(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("invalid.txt").write_bytes(b"valid\xfftext")

    result = read_handler(make_context(workspace), {"path": "invalid.txt"})

    data = assert_success_envelope(result)
    assert data["content"] == "valid\ufffdtext"


def test_read_empty_file_returns_empty_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("empty.txt").write_text("", encoding="utf-8")

    result = read_handler(make_context(workspace), {"path": "empty.txt"})

    data = assert_success_envelope(result)
    assert data["content"] == ""
