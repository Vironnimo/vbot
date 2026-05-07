"""Tests for the built-in read tool."""

from pathlib import Path

import pytest

from core.tools import (
    READ_TOOL_DESCRIPTION,
    READ_TOOL_NAME,
    READ_TOOL_PARAMETERS,
    ToolContext,
    ToolRegistry,
    read_handler,
    register_builtin_tools,
    tool_failure,
    tool_success,
)


def make_context(workspace: Path) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name="read",
        tool_call_index=0,
        workspace=workspace,
        app_root=workspace.parent,
        data_root=workspace.parent / "data",
    )


def test_register_builtin_tools_adds_read_with_compact_schema() -> None:
    registry = ToolRegistry()

    register_builtin_tools(registry)

    tool = registry.get("read")
    assert tool.name == READ_TOOL_NAME
    assert tool.description == READ_TOOL_DESCRIPTION
    assert tool.parameters == READ_TOOL_PARAMETERS
    assert set(tool.parameters["properties"]) == {"path", "offset", "limit", "description"}
    assert tool.parameters["required"] == ["path"]
    assert tool.parameters["additionalProperties"] is False


def test_description_is_not_required() -> None:
    """description should be optional, not listed in required."""
    assert "description" not in READ_TOOL_PARAMETERS["required"]


@pytest.mark.asyncio
async def test_read_with_description_succeeds(tmp_path: Path) -> None:
    """Calling read with a description argument should not cause an error."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    registry = ToolRegistry()
    register_builtin_tools(registry)

    result = await registry.dispatch(
        make_context(workspace),
        {"path": "file.txt", "description": "reading the file"},
        ["*"],
    )

    assert result == tool_success({"content": "hello"})


@pytest.mark.asyncio
async def test_read_relative_path_resolves_from_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "SOUL.md").write_text("hello", encoding="utf-8")
    registry = ToolRegistry()
    register_builtin_tools(registry)

    result = await registry.dispatch(make_context(workspace), {"path": "SOUL.md"}, ["*"])

    assert result == tool_success({"content": "hello"})


def test_read_absolute_path_is_allowed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    absolute_file = tmp_path / "outside.txt"
    absolute_file.write_text("outside", encoding="utf-8")

    result = read_handler(make_context(workspace), {"path": str(absolute_file)})

    assert result == tool_success({"content": "outside"})


def test_read_offset_is_one_indexed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = read_handler(make_context(workspace), {"path": "lines.txt", "offset": 2})

    assert result == tool_success({"content": "two\nthree\n"})


def test_read_limit_caps_returned_line_count(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lines.txt").write_text("zero\none\ntwo\n", encoding="utf-8")

    result = read_handler(make_context(workspace), {"path": "lines.txt", "limit": 2})

    assert result["ok"] is True
    content = result["data"]["content"]
    assert content.startswith("zero\none\n")


def test_read_offset_and_limit_slice_lines(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lines.txt").write_text("zero\none\ntwo\nthree\n", encoding="utf-8")

    result = read_handler(make_context(workspace), {"path": "lines.txt", "offset": 2, "limit": 2})

    assert result["ok"] is True
    content = result["data"]["content"]
    assert content.startswith("one\ntwo\n")


def test_read_missing_file_returns_failure_envelope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_handler(make_context(workspace), {"path": "missing.txt"})

    assert result == tool_failure("not_found", "File not found")


def test_read_directory_path_returns_failure_envelope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "directory").mkdir()

    result = read_handler(make_context(workspace), {"path": "directory"})

    assert result == tool_failure("not_file", "Path is not a file")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "path must be a non-empty string"),
        ({"path": ""}, "path must be a non-empty string"),
        ({"path": 7}, "path must be a non-empty string"),
        ({"path": "file.txt", "offset": -1}, "offset must be a positive integer"),
        ({"path": "file.txt", "offset": 0}, "offset must be a positive integer"),
        ({"path": "file.txt", "offset": "1"}, "offset must be a positive integer"),
        ({"path": "file.txt", "limit": -1}, "limit must be a positive integer"),
        ({"path": "file.txt", "limit": True}, "limit must be a positive integer"),
        ({"path": "file.txt", "encoding": "utf-8"}, "Unsupported argument(s): encoding"),
    ],
)
def test_read_invalid_arguments_return_failure_envelope(
    tmp_path: Path,
    arguments: dict[str, object],
    message: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_handler(make_context(workspace), arguments)

    assert result == tool_failure("invalid_arguments", message)


def test_read_reports_all_unsupported_arguments_before_reading(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("content", encoding="utf-8")

    result = read_handler(
        make_context(workspace),
        {"path": "file.txt", "encoding": "utf-8", "mode": "text"},
    )

    assert result == tool_failure("invalid_arguments", "Unsupported argument(s): encoding, mode")


def test_read_invalid_utf8_uses_replacement_character(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "invalid.txt").write_bytes(b"valid\xfftext")

    result = read_handler(make_context(workspace), {"path": "invalid.txt"})

    assert result == tool_success({"content": "valid\ufffdtext"})


def test_read_does_not_inject_line_numbers(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "plain.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    result = read_handler(make_context(workspace), {"path": "plain.txt"})

    assert result == tool_success({"content": "alpha\nbeta\n"})


def test_read_default_limit_truncates_large_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lines = "".join(f"line{i}\n" for i in range(1, 2002))
    (workspace / "big.txt").write_text(lines, encoding="utf-8")

    result = read_handler(make_context(workspace), {"path": "big.txt"})

    assert result["ok"] is True
    content = result["data"]["content"]
    assert "[Showing lines 1-2000 of 2001." in content
    assert "line2001" not in content


def test_read_offset_beyond_eof_returns_notice(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "short.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = read_handler(make_context(workspace), {"path": "short.txt", "offset": 10})

    assert result["ok"] is True
    content = result["data"]["content"]
    assert "[Offset 10 is beyond end of file (3 lines)." in content


def test_read_byte_limit_truncates_output(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # single line exceeding 50 KB
    (workspace / "huge.txt").write_bytes(("x" * 60000 + "\n").encode("utf-8"))

    result = read_handler(make_context(workspace), {"path": "huge.txt"})

    assert result["ok"] is True
    content = result["data"]["content"]
    assert len(content.encode("utf-8")) <= 50 * 1024 + 500  # allow for hint overhead
    assert "Output truncated at 50 KB" in content


def test_read_float_offset_is_accepted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result_float = read_handler(make_context(workspace), {"path": "lines.txt", "offset": 2.0})
    result_int = read_handler(make_context(workspace), {"path": "lines.txt", "offset": 2})

    assert result_float == result_int


def test_read_float_limit_is_accepted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result_float = read_handler(make_context(workspace), {"path": "lines.txt", "limit": 2.0})
    result_int = read_handler(make_context(workspace), {"path": "lines.txt", "limit": 2})

    assert result_float == result_int


def test_read_float_non_integer_offset_returns_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_handler(make_context(workspace), {"path": "file.txt", "offset": 1.5})

    assert result == tool_failure("invalid_arguments", "offset must be a positive integer")


def test_read_empty_file_returns_empty_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "empty.txt").write_text("", encoding="utf-8")

    result = read_handler(make_context(workspace), {"path": "empty.txt"})

    assert result == tool_success({"content": ""})
