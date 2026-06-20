"""Tests for the built-in grep tool."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

import core.tools.grep as grep_module
from core.tools.grep import (
    GREP_TOOL_NAME,
    GREP_TOOL_PARAMETERS,
    MAX_OUTPUT_BYTES,
    grep_handler,
    register_grep_tool,
)
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope


def make_context(
    workspace: Path, tool_name: str = GREP_TOOL_NAME, *, cwd: Path | None = None
) -> ToolContext:
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
        cwd=cwd,
    )


def force_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: None)


def get_success_content(result: dict[str, object]) -> str:
    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    return content


def test_grep_default_search_root_is_cwd_not_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With no path argument, grep searches the working directory; a project
    # session points that at the repo (cwd), not the agent workspace.
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("ws.txt").write_text("needle in workspace\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.joinpath("repo.txt").write_text("needle in repo\n", encoding="utf-8")

    content = get_success_content(
        grep_handler(make_context(workspace, cwd=repo), {"pattern": "needle"})
    )

    assert "repo.txt" in content
    assert "ws.txt" not in content


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


def test_register_grep_tool_exposes_provider_schema() -> None:
    registry = ToolRegistry()

    register_grep_tool(registry)

    tool = registry.get("grep")
    assert tool.name == GREP_TOOL_NAME == "grep"
    assert tool.parameters == GREP_TOOL_PARAMETERS

    definitions = registry.provider_definitions(["grep"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert set(definition) == {"name", "description", "parameters"}
    assert definition["name"] == "grep"

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["pattern"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {
        "pattern",
        "path",
        "glob",
        "ignoreCase",
        "literal",
        "context",
        "limit",
        "output_mode",
    }
    assert "description" not in parameters["properties"]


def test_grep_searches_relative_workspace_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\nmatch here\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "match", "path": "notes.txt"})

    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:2: match here"


def test_grep_defaults_to_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("target\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "target"})

    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:1: target"


def test_grep_searches_absolute_file_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("absolute hit\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "hit", "path": str(target)})

    data = assert_success_envelope(result)
    assert data["content"] == "outside.txt:1: absolute hit"


def test_grep_searches_absolute_directory_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = tmp_path / "outside"
    directory.mkdir()
    directory.joinpath("a.txt").write_text("alpha\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "alpha", "path": str(directory)})

    data = assert_success_envelope(result)
    assert data["content"] == "a.txt:1: alpha"


def test_grep_returns_failure_for_invalid_regex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "[", "path": "notes.txt"})

    error = assert_failure_envelope(result, "invalid_regex")
    assert "invalid regex pattern" in error["message"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"context": -1}, "context must be >= 0"),
        ({"context": True}, "context must be an integer"),
        ({"context": 1.5}, "context must be an integer"),
        ({"limit": 0}, "limit must be >= 1"),
        ({"limit": True}, "limit must be an integer"),
        ({"limit": 1.5}, "limit must be an integer"),
        ({"ignoreCase": "maybe"}, "ignoreCase must be a boolean"),
        ({"literal": "maybe"}, "literal must be a boolean"),
    ],
)
def test_grep_returns_failure_for_invalid_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
    message: str,
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "hello", **arguments})

    error = assert_failure_envelope(result, "invalid_arguments")
    assert error["message"] == message


def test_grep_accepts_string_encoded_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Models often encode numbers and booleans as strings; accept them.
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("Hello\nhello\n", encoding="utf-8")

    string_result = grep_handler(
        make_context(workspace),
        {"pattern": "hello", "ignoreCase": "true", "limit": "5", "context": "0"},
    )
    typed_result = grep_handler(
        make_context(workspace),
        {"pattern": "hello", "ignoreCase": True, "limit": 5, "context": 0},
    )

    assert string_result["ok"] is True
    assert string_result == typed_result


def test_grep_output_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("one.txt").write_text("hit\nmiss\nhit\n", encoding="utf-8")
    workspace.joinpath("two.txt").write_text("hit\n", encoding="utf-8")

    files_result = grep_handler(
        make_context(workspace), {"pattern": "hit", "output_mode": "files_with_matches"}
    )
    count_result = grep_handler(make_context(workspace), {"pattern": "hit", "output_mode": "count"})

    files_data = assert_success_envelope(files_result)
    count_data = assert_success_envelope(count_result)
    assert files_data["content"] == "one.txt\ntwo.txt"
    assert count_data["content"] == "one.txt:2\ntwo.txt:1"


def test_grep_literal_and_ignore_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("Alpha.1\nalphaX1\n", encoding="utf-8")

    regex_result = grep_handler(make_context(workspace), {"pattern": "Alpha.1"})
    literal_result = grep_handler(
        make_context(workspace), {"pattern": "Alpha.1", "literal": True, "ignoreCase": True}
    )

    regex_data = assert_success_envelope(regex_result)
    literal_data = assert_success_envelope(literal_result)
    assert regex_data["content"] == "notes.txt:1: Alpha.1"
    assert literal_data["content"] == "notes.txt:1: Alpha.1"


def test_grep_glob_filter_limits_candidate_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("keep.py").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("skip.txt").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "glob": "*.py"})
    data = assert_success_envelope(result)
    assert data["content"] == "keep.py:1: needle"


@pytest.mark.parametrize("glob_pattern", ["/absolute/*.py", "../*.py"])
def test_grep_rejects_invalid_glob_filter_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, glob_pattern: str
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = grep_handler(make_context(workspace), {"pattern": "needle", "glob": glob_pattern})

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "glob" in error["message"]


def test_grep_context_lines_in_python_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("before\nneedle\nafter\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "context": 1})
    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:1: before\nnotes.txt:2: needle\nnotes.txt:3: after"


def test_grep_no_matches_returns_success_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "missing"})
    data = assert_success_envelope(result)
    assert data["content"] == "No matches found for pattern: missing"


def test_grep_adds_limit_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hit\nhit\nhit\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "hit", "limit": 2})
    data = assert_success_envelope(result)
    assert data["content"] == (
        "notes.txt:1: hit\nnotes.txt:2: hit\n[Results limited to 2 matches.]"
    )


def test_grep_truncates_long_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    long_line = "needle" + "x" * 600
    workspace.joinpath("notes.txt").write_text(long_line, encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle"})
    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert content.startswith("notes.txt:1: needle")
    assert content.endswith("...[truncated]")
    assert len(content) < len("notes.txt:1: " + long_line)


def test_grep_caps_large_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text(
        "\n".join(f"needle {index} " + "x" * 490 for index in range(130)),
        encoding="utf-8",
    )

    result = grep_handler(make_context(workspace), {"pattern": "needle", "limit": 130})
    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "[... output truncated ...]" in content
    assert len(content.encode("utf-8")) <= MAX_OUTPUT_BYTES


def test_grep_skips_read_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bad = workspace / "bad.txt"
    good = workspace / "good.txt"
    bad.write_text("needle\n", encoding="utf-8")
    good.write_text("needle\n", encoding="utf-8")
    original_read_text = Path.read_text

    def read_text_or_fail(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == bad:
            raise PermissionError("blocked")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text_or_fail)

    result = grep_handler(make_context(workspace), {"pattern": "needle"})
    data = assert_success_envelope(result)
    assert data["content"] == "good.txt:1: needle"


def test_grep_uses_python_fallback_when_rg_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("fallback hit\n", encoding="utf-8")

    def fail_if_called(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess.run should not be called without rg")

    monkeypatch.setattr(grep_module.subprocess, "run", fail_if_called)

    result = grep_handler(make_context(workspace), {"pattern": "fallback"})
    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:1: fallback hit"


def test_grep_returns_failure_for_rg_nonzero_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: "rg")

    def fail_rg(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="regex parse error")

    monkeypatch.setattr(grep_module.subprocess, "run", fail_rg)

    result = grep_handler(make_context(workspace), {"pattern": "hello"})
    error = assert_failure_envelope(result, "grep_error")
    assert error["message"] == "regex parse error"


def test_grep_returns_failure_for_discovered_rg_execution_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: "rg")

    def raise_oserror(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("permission denied")

    monkeypatch.setattr(grep_module.subprocess, "run", raise_oserror)

    result = grep_handler(make_context(workspace), {"pattern": "hello"})
    error = assert_failure_envelope(result, "grep_error")
    assert "failed to execute ripgrep" in error["message"]
    assert "permission denied" in error["message"]


def test_grep_uses_rg_success_output_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: "rg")

    def succeed_rg(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="notes.txt:1:hello\n", stderr="")

    monkeypatch.setattr(grep_module.subprocess, "run", succeed_rg)

    result = grep_handler(make_context(workspace), {"pattern": "hello"})
    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:1: hello"


def test_grep_rejects_unknown_arguments(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = grep_handler(make_context(workspace), {"pattern": "x", "description": "label"})
    error = assert_failure_envelope(result, "invalid_arguments")
    assert "description" in error["message"]


def test_grep_failure_envelope_is_valid_for_missing_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = grep_handler(make_context(workspace), {"pattern": "x", "path": "missing"})
    error = assert_failure_envelope(result, "path_not_found")
    assert "missing" in error["message"]
