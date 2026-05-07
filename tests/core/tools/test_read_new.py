"""Tests for the read_new tool (ported from vControl)."""

from pathlib import Path

import pytest

from core.tools import (
    READ_NEW_TOOL_DESCRIPTION,
    READ_NEW_TOOL_NAME,
    READ_NEW_TOOL_PARAMETERS,
    ToolContext,
    ToolRegistry,
    read_new_handler,
    register_read_new_tool,
    tool_failure,
    tool_success,
)


def make_context(workspace: Path) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name="read_new",
        tool_call_index=0,
        workspace=workspace,
        app_root=workspace.parent,
        data_root=workspace.parent / "data",
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registration_correct_name_description_parameters() -> None:
    registry = ToolRegistry()
    register_read_new_tool(registry)

    tool = registry.get(READ_NEW_TOOL_NAME)
    assert tool.name == READ_NEW_TOOL_NAME
    assert tool.description == READ_NEW_TOOL_DESCRIPTION
    assert tool.parameters == READ_NEW_TOOL_PARAMETERS
    assert set(tool.parameters["properties"]) == {"path", "offset", "limit"}
    assert tool.parameters["required"] == ["path"]
    assert tool.parameters["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_relative_path_resolves_from_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")

    result = read_new_handler(make_context(workspace), {"path": "file.txt"})

    assert result == tool_success({"content": "hello"})


def test_absolute_path_works(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    absolute_file = tmp_path / "outside.txt"
    absolute_file.write_text("outside", encoding="utf-8")

    result = read_new_handler(make_context(workspace), {"path": str(absolute_file)})

    assert result == tool_success({"content": "outside"})


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_file_not_found_returns_not_found_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_new_handler(make_context(workspace), {"path": "missing.txt"})

    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"
    assert "file not found" in result["error"]["message"]


def test_directory_path_returns_not_file_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "subdir").mkdir()

    result = read_new_handler(make_context(workspace), {"path": "subdir"})

    assert result["ok"] is False
    assert result["error"]["code"] == "not_file"
    assert "not a file" in result["error"]["message"]


def test_unknown_argument_returns_invalid_arguments(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_new_handler(make_context(workspace), {"path": "file.txt", "encoding": "utf-8"})

    assert result == tool_failure("invalid_arguments", "Unsupported argument(s): encoding")


def test_multiple_unknown_arguments_sorted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_new_handler(
        make_context(workspace),
        {"path": "file.txt", "mode": "text", "encoding": "utf-8"},
    )

    assert result == tool_failure("invalid_arguments", "Unsupported argument(s): encoding, mode")


def test_missing_path_returns_invalid_arguments(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_new_handler(make_context(workspace), {})

    assert result == tool_failure("invalid_arguments", "path must be a non-empty string")


def test_non_string_path_returns_invalid_arguments(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_new_handler(make_context(workspace), {"path": 42})

    assert result == tool_failure("invalid_arguments", "path must be a non-empty string")


def test_empty_string_path_returns_invalid_arguments(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_new_handler(make_context(workspace), {"path": ""})

    assert result == tool_failure("invalid_arguments", "path must be a non-empty string")


# ---------------------------------------------------------------------------
# Content reading
# ---------------------------------------------------------------------------


def test_small_file_returns_exact_content_no_hint(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("line1\nline2\n", encoding="utf-8")

    result = read_new_handler(make_context(workspace), {"path": "file.txt"})

    assert result == tool_success({"content": "line1\nline2\n"})


def test_offset_skips_to_correct_line(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = read_new_handler(make_context(workspace), {"path": "lines.txt", "offset": 2})

    assert result == tool_success({"content": "two\nthree\n"})


def test_limit_caps_line_count(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = read_new_handler(make_context(workspace), {"path": "lines.txt", "limit": 2})

    assert result["ok"] is True
    content = result["data"]["content"]
    assert "one\ntwo\n" in content


def test_offset_and_limit_together(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lines.txt").write_text("zero\none\ntwo\nthree\n", encoding="utf-8")

    result = read_new_handler(
        make_context(workspace), {"path": "lines.txt", "offset": 2, "limit": 2}
    )

    assert result["ok"] is True
    content = result["data"]["content"]
    assert "one\ntwo\n" in content


def test_default_limit_truncates_at_2000_lines(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lines = "".join(f"line{i}\n" for i in range(1, 2002))
    (workspace / "big.txt").write_text(lines, encoding="utf-8")

    result = read_new_handler(make_context(workspace), {"path": "big.txt"})

    assert result["ok"] is True
    content = result["data"]["content"]
    assert "[Showing lines 1-2000 of 2001." in content
    assert "line2001" not in content


def test_offset_beyond_eof_returns_notice(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "short.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = read_new_handler(make_context(workspace), {"path": "short.txt", "offset": 10})

    assert result["ok"] is True
    content = result["data"]["content"]
    assert "[Offset 10 is beyond end of file (3 lines)." in content


def test_byte_limit_truncates_and_includes_notice(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "huge.txt").write_bytes(("x" * 60000 + "\n").encode("utf-8"))

    result = read_new_handler(make_context(workspace), {"path": "huge.txt"})

    assert result["ok"] is True
    content = result["data"]["content"]
    assert "Output truncated at 50 KB" in content


def test_float_offset_accepted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result_float = read_new_handler(make_context(workspace), {"path": "lines.txt", "offset": 2.0})
    result_int = read_new_handler(make_context(workspace), {"path": "lines.txt", "offset": 2})

    assert result_float == result_int


def test_float_limit_accepted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result_float = read_new_handler(make_context(workspace), {"path": "lines.txt", "limit": 2.0})
    result_int = read_new_handler(make_context(workspace), {"path": "lines.txt", "limit": 2})

    assert result_float == result_int


def test_non_integer_float_offset_returns_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_new_handler(make_context(workspace), {"path": "file.txt", "offset": 1.5})

    assert result == tool_failure("invalid_arguments", "offset must be a positive integer")


def test_non_integer_float_limit_returns_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_new_handler(make_context(workspace), {"path": "file.txt", "limit": 1.5})

    assert result == tool_failure("invalid_arguments", "limit must be a positive integer")


def test_empty_file_returns_empty_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "empty.txt").write_text("", encoding="utf-8")

    result = read_new_handler(make_context(workspace), {"path": "empty.txt"})

    assert result == tool_success({"content": ""})


def test_invalid_utf8_uses_replacement_character(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "invalid.txt").write_bytes(b"valid\xfftext")

    result = read_new_handler(make_context(workspace), {"path": "invalid.txt"})

    assert result == tool_success({"content": "valid\ufffdtext"})


def test_crlf_normalized_to_lf(tmp_path: Path) -> None:
    """\\r\\n line endings are normalized to \\n."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "crlf.txt").write_bytes(b"line1\r\nline2\r\n")

    result = read_new_handler(make_context(workspace), {"path": "crlf.txt"})

    assert result == tool_success({"content": "line1\nline2\n"})


@pytest.mark.asyncio
async def test_registration_and_dispatch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("dispatched", encoding="utf-8")
    registry = ToolRegistry()
    register_read_new_tool(registry)

    result = await registry.dispatch(
        make_context(workspace),
        {"path": "file.txt"},
        ["*"],
    )

    assert result == tool_success({"content": "dispatched"})
