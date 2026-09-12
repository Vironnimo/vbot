"""Grep: contract behavior."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from core.tools.grep import (
    GREP_TOOL_NAME,
    GREP_TOOL_PARAMETERS,
    grep_handler,
    register_grep_tool,
)
from core.tools.search import RESULTS_LIMITED_MARKER
from core.tools.tools import ToolRegistry
from tests.core.tools.grep_helpers import (
    assert_failure_envelope,
    assert_success_envelope,
    force_python_fallback,
    get_success_content,
    make_context,
)


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


def test_register_grep_tool_exposes_provider_schema() -> None:
    registry = ToolRegistry()

    register_grep_tool(registry)

    tool = registry.get("grep")
    assert tool.name == GREP_TOOL_NAME == "grep"
    assert tool.parameters == GREP_TOOL_PARAMETERS
    # The registered handler must run the sync search off the event loop.
    assert inspect.iscoroutinefunction(tool.handler)

    definitions = registry.provider_definitions(["grep"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert set(definition) == {"name", "description", "parameters"}
    assert definition["name"] == "grep"

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert "oneOf" not in parameters
    expected_common = {
        "pattern",
        "path",
        "glob",
        "ignore_case",
        "literal",
        "multiline",
        "limit",
        "offset",
        "include_ignored",
        "output_mode",
        "context",
    }
    assert set(parameters["properties"]) == expected_common
    assert parameters["required"] == ["pattern"]
    assert "additionalProperties" not in parameters
    assert parameters["properties"]["output_mode"]["enum"] == [
        "content",
        "files_with_matches",
        "count",
    ]
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in parameters["properties"].values()
    )
    display = registry.display_for_call(
        "grep",
        {
            "description": "Find every version variable",
            "pattern": "VERSION_[A-Z_]+",
            "path": "src",
        },
    )
    assert display["primary"][0]["value"] == "Find every version variable"
    assert display["primary"][0]["kind"] == "description"
    assert display["summary"] == "Find every version variable"
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


def test_grep_renders_absolute_path_for_file_outside_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("absolute hit\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "hit", "path": str(target)})

    data = assert_success_envelope(result)
    assert data["content"] == f"{target.resolve().as_posix()}:1: absolute hit"


def test_grep_renders_absolute_paths_for_directory_outside_cwd(
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
    assert data["content"] == f"{(directory / 'a.txt').resolve().as_posix()}:1: alpha"


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
    "arguments",
    [
        {"context": -1},
        {"context": True},
        {"context": 1.5},
        {"limit": 0},
        {"limit": True},
        {"limit": 1.5},
        {"ignore_case": "maybe"},
        {"literal": "maybe"},
    ],
)
def test_grep_returns_failure_for_invalid_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "hello", **arguments})

    assert_failure_envelope(result, "invalid_arguments")


def test_grep_rejects_aliases_and_string_encoded_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        {"pattern": "hello", "ignore_case": True, "limit": 5, "context": 0},
    )

    error = assert_failure_envelope(string_result, "invalid_arguments")
    assert "ignoreCase" in error["message"]
    assert typed_result["ok"] is True


def test_grep_accepts_head_aliases_as_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Models trained on other harnesses send head/head_limit for limit; both
    # spellings are silently accepted and behave exactly like limit.
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hit\nhit\nhit\n", encoding="utf-8")

    head_result = grep_handler(make_context(workspace), {"pattern": "hit", "head": 2})
    string_result = grep_handler(make_context(workspace), {"pattern": "hit", "head_limit": "2"})

    expected = f"notes.txt:1: hit\nnotes.txt:2: hit\n{RESULTS_LIMITED_MARKER.format(limit=2)}"
    assert get_success_content(head_result) == expected
    assert get_success_content(string_result) == expected


@pytest.mark.parametrize("arguments", [{"limit": 3, "head": 5}, {"head": 5, "head_limit": 7}])
def test_grep_rejects_conflicting_limit_alias_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hit\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "hit", **arguments})

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "Conflicting limit arguments" in error["message"]


def test_grep_rejects_non_integer_head_alias_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hit\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "hit", "head": "many"})

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "head" in error["message"]
