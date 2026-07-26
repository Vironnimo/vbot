"""Tests for the built-in glob tool."""

import inspect
import os
from pathlib import Path

import pytest

import core.tools.search as search_module
from core.tools.glob import (
    DEFAULT_GLOB_LIMIT,
    GLOB_TOOL_NAME,
    GLOB_TOOL_PARAMETERS,
    glob_handler,
    register_glob_tool,
)
from core.tools.search import RESULTS_LIMITED_MARKER, SEARCH_TIMEOUT_MARKER
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope

BASE_MTIME = 1_700_000_000


def make_context(
    workspace: Path,
    tool_name: str = GLOB_TOOL_NAME,
    *,
    cwd: Path | None = None,
    user_cancelled: bool = False,
) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=workspace,
        vbot_root=workspace.parent,
        data_root=workspace.parent / "data",
        cwd=cwd,
        cancel_check_hook=(lambda: True) if user_cancelled else None,
    )


def set_mtime(path: Path, offset_seconds: int) -> None:
    os.utime(path, (BASE_MTIME + offset_seconds, BASE_MTIME + offset_seconds))


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


def test_glob_default_search_root_is_cwd_not_workspace(tmp_path: Path) -> None:
    # With no path argument, glob searches the working directory; a project
    # session points that at the repo (cwd), not the agent workspace.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("only_in_workspace.py").write_text("", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.joinpath("only_in_repo.py").write_text("", encoding="utf-8")

    content = get_success_content(
        glob_handler(make_context(workspace, cwd=repo), {"pattern": "*.py"})
    )

    assert content == "only_in_repo.py"


def test_register_glob_tool_exposes_provider_schema() -> None:
    registry = ToolRegistry()

    register_glob_tool(registry)

    tool = registry.get("glob")
    assert tool.name == GLOB_TOOL_NAME == "glob"
    assert tool.parameters == GLOB_TOOL_PARAMETERS
    # The registered handler must run the sync search off the event loop.
    assert inspect.iscoroutinefunction(tool.handler)

    definitions = registry.provider_definitions(["glob"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert set(definition) == {"name", "description", "parameters"}
    assert definition["name"] == "glob"

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["pattern"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {
        "pattern",
        "path",
        "limit",
        "offset",
        "include_ignored",
    }
    assert "description" not in parameters["properties"]


def test_glob_renders_relative_path_argument_results_from_cwd(tmp_path: Path) -> None:
    # Results from a subdirectory search stay working-directory-relative so
    # they round-trip directly into a follow-up read call.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("src").mkdir()
    workspace.joinpath("src", "app.py").write_text("print('hello')\n", encoding="utf-8")

    result = glob_handler(make_context(workspace), {"pattern": "*.py", "path": "src"})

    assert get_success_content(result) == "src/app.py"


def test_glob_renders_absolute_paths_outside_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.joinpath("notes.md").write_text("# Notes\n", encoding="utf-8")

    result = glob_handler(
        make_context(workspace),
        {"pattern": "*.md", "path": str(outside)},
    )

    assert get_success_content(result) == (outside / "notes.md").resolve().as_posix()


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


def test_glob_returns_failure_for_invalid_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = glob_handler(make_context(workspace), {"pattern": "*.py", "limit": 0})
    error = assert_failure_envelope(result, "invalid_arguments")
    assert error["message"] == "limit must be >= 1"


def test_glob_suffixes_directories_and_special_cases_double_star(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("src").mkdir()
    workspace.joinpath("src", "nested").mkdir()
    workspace.joinpath("src", "app.py").write_text("print('hello')\n", encoding="utf-8")
    set_mtime(workspace / "src" / "app.py", 30)
    set_mtime(workspace / "src" / "nested", 20)
    set_mtime(workspace / "src", 10)

    result = glob_handler(make_context(workspace), {"pattern": "**"})

    assert get_success_content(result).splitlines() == ["src/app.py", "src/nested/", "src/"]


def test_glob_sorts_by_modification_time_newest_first(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("old.txt").write_text("old\n", encoding="utf-8")
    workspace.joinpath("newest.txt").write_text("newest\n", encoding="utf-8")
    workspace.joinpath("middle.txt").write_text("middle\n", encoding="utf-8")
    set_mtime(workspace / "old.txt", 0)
    set_mtime(workspace / "middle.txt", 10)
    set_mtime(workspace / "newest.txt", 20)

    result = glob_handler(make_context(workspace), {"pattern": "*.txt"})

    assert get_success_content(result).splitlines() == ["newest.txt", "middle.txt", "old.txt"]


def test_glob_breaks_equal_modification_time_ties_alphabetically(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("b.txt", "a.txt", "c.txt"):
        workspace.joinpath(name).write_text("x\n", encoding="utf-8")
        set_mtime(workspace / name, 0)

    result = glob_handler(make_context(workspace), {"pattern": "*.txt"})

    assert get_success_content(result).splitlines() == ["a.txt", "b.txt", "c.txt"]


def test_glob_marks_results_cut_by_default_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(DEFAULT_GLOB_LIMIT + 1):
        file_path = workspace / f"file-{index:03}.txt"
        file_path.write_text("x\n", encoding="utf-8")
        set_mtime(file_path, 0)

    result = glob_handler(make_context(workspace), {"pattern": "*.txt"})

    lines = get_success_content(result).splitlines()
    assert len(lines) == DEFAULT_GLOB_LIMIT + 1
    assert lines[0] == "file-000.txt"
    assert lines[-2] == "file-099.txt"
    assert lines[-1] == RESULTS_LIMITED_MARKER.format(limit=DEFAULT_GLOB_LIMIT)
    assert "file-100.txt" not in lines


def test_glob_applies_explicit_limit_keeping_newest_matches(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for offset, name in enumerate(("old.txt", "middle.txt", "newest.txt")):
        file_path = workspace / name
        file_path.write_text("x\n", encoding="utf-8")
        set_mtime(file_path, offset * 10)

    result = glob_handler(make_context(workspace), {"pattern": "*.txt", "limit": 2})

    assert get_success_content(result).splitlines() == [
        "newest.txt",
        "middle.txt",
        RESULTS_LIMITED_MARKER.format(limit=2),
    ]


def test_glob_returns_cancelled_failure_when_user_cancels(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("a.txt").write_text("x\n", encoding="utf-8")

    result = glob_handler(make_context(workspace, user_cancelled=True), {"pattern": "*.txt"})

    assert_failure_envelope(result, "cancelled_by_user")


def test_glob_marks_timed_out_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("a.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(search_module, "SEARCH_TIMEOUT_SECONDS", -1.0)

    result = glob_handler(make_context(workspace), {"pattern": "*.txt"})

    content = get_success_content(result)
    assert SEARCH_TIMEOUT_MARKER in content


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


def test_glob_bare_star_pattern_matches_top_level_only(tmp_path: Path) -> None:
    # Standard glob semantics: '*.py' is anchored to the search root; matching
    # at any depth needs '**/*.py'.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("main.py").write_text("", encoding="utf-8")
    workspace.joinpath("src").mkdir()
    workspace.joinpath("src", "app.py").write_text("", encoding="utf-8")

    top_level = glob_handler(make_context(workspace), {"pattern": "*.py"})
    any_depth = glob_handler(make_context(workspace), {"pattern": "**/*.py"})

    assert get_success_content(top_level).splitlines() == ["main.py"]
    assert sorted(get_success_content(any_depth).splitlines()) == ["main.py", "src/app.py"]


def test_glob_pattern_matches_case_insensitively(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("app.py").write_text("", encoding="utf-8")

    result = glob_handler(make_context(workspace), {"pattern": "*.PY"})

    assert get_success_content(result) == "app.py"


def test_glob_skips_gitignored_paths_by_default(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath(".gitignore").write_text("node_modules/\n", encoding="utf-8")
    workspace.joinpath("node_modules").mkdir()
    workspace.joinpath("node_modules", "lib.js").write_text("", encoding="utf-8")
    workspace.joinpath("app.js").write_text("", encoding="utf-8")

    default_result = glob_handler(make_context(workspace), {"pattern": "**/*.js"})
    opted_in_result = glob_handler(
        make_context(workspace), {"pattern": "**/*.js", "include_ignored": True}
    )

    assert get_success_content(default_result).splitlines() == ["app.js"]
    assert sorted(get_success_content(opted_in_result).splitlines()) == [
        "app.js",
        "node_modules/lib.js",
    ]


def test_glob_always_skips_git_internals(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git_dir = workspace / ".git"
    git_dir.mkdir()
    git_dir.joinpath("config.txt").write_text("", encoding="utf-8")
    workspace.joinpath("app.txt").write_text("", encoding="utf-8")

    result = glob_handler(make_context(workspace), {"pattern": "**/*.txt", "include_ignored": True})

    assert get_success_content(result).splitlines() == ["app.txt"]


def test_glob_searches_explicitly_targeted_ignored_directory(tmp_path: Path) -> None:
    # Explicitly targeting an ignored directory is intent to search it; the
    # ignore rules must not produce a misleading empty result.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath(".git").mkdir()
    workspace.joinpath(".gitignore").write_text("vendor/\n", encoding="utf-8")
    vendor = workspace / "vendor"
    vendor.mkdir()
    vendor.joinpath("lib.js").write_text("", encoding="utf-8")

    result = glob_handler(make_context(workspace), {"pattern": "*.js", "path": "vendor"})

    assert get_success_content(result) == "vendor/lib.js"


def test_glob_matches_worktree_under_ignored_directory(tmp_path: Path) -> None:
    # A worktree's .git pointer file bounds the gitignore evaluation: the main
    # repo's ".worktrees/" rule must not blank out matching inside the
    # worktree, and the pointer file itself is never listed.
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.joinpath(".git").mkdir()
    repo.joinpath(".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    worktree = repo / ".worktrees" / "task"
    worktree.mkdir(parents=True)
    worktree.joinpath(".git").write_text("gitdir: ../../.git/worktrees/task\n", encoding="utf-8")
    worktree.joinpath("app.py").write_text("", encoding="utf-8")

    result = glob_handler(make_context(worktree), {"pattern": "**"})

    assert get_success_content(result) == "app.py"


def test_glob_pages_results_with_offset(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for offset, name in enumerate(("old.txt", "middle.txt", "newest.txt")):
        file_path = workspace / name
        file_path.write_text("x\n", encoding="utf-8")
        set_mtime(file_path, offset * 10)

    result = glob_handler(make_context(workspace), {"pattern": "*.txt", "offset": 1, "limit": 1})

    assert get_success_content(result).splitlines() == [
        "middle.txt",
        RESULTS_LIMITED_MARKER.format(limit=1),
    ]


def test_glob_reports_offset_beyond_total_matches(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("a.txt").write_text("x\n", encoding="utf-8")

    result = glob_handler(make_context(workspace), {"pattern": "*.txt", "offset": 5})

    assert get_success_content(result) == "No results at offset 5; 1 matches total."
