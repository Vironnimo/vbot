"""Tests for the built-in glob tool."""

from pathlib import Path

from core.tools.glob import (
    GLOB_TOOL_NAME,
    GLOB_TOOL_PARAMETERS,
    MAX_GLOB_MATCHES,
    glob_handler,
    register_glob_tool,
)
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope


def make_context(workspace: Path, tool_name: str = GLOB_TOOL_NAME) -> ToolContext:
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


def get_success_content(result: dict[str, object]) -> str:
    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    return content


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


def test_register_glob_tool_exposes_provider_schema() -> None:
    registry = ToolRegistry()

    register_glob_tool(registry)

    tool = registry.get("glob")
    assert tool.name == GLOB_TOOL_NAME == "glob"
    assert tool.parameters == GLOB_TOOL_PARAMETERS

    definitions = registry.provider_definitions(["glob"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert set(definition) == {"name", "description", "parameters"}
    assert definition["name"] == "glob"

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["pattern"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"pattern", "path"}
    assert "description" not in parameters["properties"]


def test_glob_searches_relative_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("src").mkdir()
    workspace.joinpath("src", "app.py").write_text("print('hello')\n", encoding="utf-8")

    result = glob_handler(make_context(workspace), {"pattern": "*.py", "path": "src"})

    assert get_success_content(result) == "app.py"


def test_glob_searches_absolute_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.joinpath("notes.md").write_text("# Notes\n", encoding="utf-8")

    result = glob_handler(
        make_context(workspace),
        {"pattern": "*.md", "path": str(outside)},
    )

    assert get_success_content(result) == "notes.md"


def test_glob_defaults_to_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("README.md").write_text("hello\n", encoding="utf-8")

    result = glob_handler(make_context(workspace), {"pattern": "*.md"})

    assert get_success_content(result) == "README.md"


def test_glob_returns_failure_for_missing_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = glob_handler(make_context(workspace), {"pattern": "*.py", "path": "missing"})

    error = assert_failure_envelope(result, "path_not_found")
    assert "missing" in error["message"]


def test_glob_returns_failure_for_non_directory_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("file.txt").write_text("content\n", encoding="utf-8")

    result = glob_handler(make_context(workspace), {"pattern": "*.txt", "path": "file.txt"})
    error = assert_failure_envelope(result, "not_a_directory")
    assert "file.txt" in error["message"]


def test_glob_returns_failure_for_empty_pattern(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = glob_handler(make_context(workspace), {"pattern": "   "})
    error = assert_failure_envelope(result, "invalid_arguments")
    assert "pattern" in error["message"]


def test_glob_returns_failure_for_invalid_pattern_values(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    for pattern in ("/absolute/*.py", "../*.py"):
        result = glob_handler(make_context(workspace), {"pattern": pattern})
        error = assert_failure_envelope(result, "invalid_arguments")
        assert "pattern" in error["message"]


def test_glob_suffixes_directories_and_special_cases_double_star(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("src").mkdir()
    workspace.joinpath("src", "nested").mkdir()
    workspace.joinpath("src", "app.py").write_text("print('hello')\n", encoding="utf-8")

    result = glob_handler(make_context(workspace), {"pattern": "**"})

    assert get_success_content(result).splitlines() == ["src/", "src/app.py", "src/nested/"]


def test_glob_returns_matches_in_sorted_order(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("b.txt").write_text("b\n", encoding="utf-8")
    workspace.joinpath("folder").mkdir()
    workspace.joinpath("a.txt").write_text("a\n", encoding="utf-8")
    workspace.joinpath("folder", "c.txt").write_text("c\n", encoding="utf-8")

    result = glob_handler(make_context(workspace), {"pattern": "**/*.txt"})

    assert get_success_content(result).splitlines() == ["a.txt", "b.txt", "folder/c.txt"]


def test_glob_caps_matches_at_one_hundred(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(MAX_GLOB_MATCHES + 1):
        workspace.joinpath(f"file-{index:03}.txt").write_text("x\n", encoding="utf-8")

    result = glob_handler(make_context(workspace), {"pattern": "*.txt"})

    matches = get_success_content(result).splitlines()
    assert len(matches) == MAX_GLOB_MATCHES
    assert matches[0] == "file-000.txt"
    assert matches[-1] == "file-099.txt"
    assert "file-100.txt" not in matches


def test_glob_returns_failure_for_unknown_argument(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = glob_handler(
        make_context(workspace),
        {"pattern": "*.py", "description": "display-only label"},
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "description" in error["message"]


def test_glob_no_match_returns_success_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = glob_handler(make_context(workspace), {"pattern": "*.missing"})

    assert get_success_content(result) == "No paths matched pattern: *.missing"
