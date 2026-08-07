"""Tests for the built-in grep tool."""

import inspect
import io
from pathlib import Path
from typing import Any

import pytest

import core.tools.grep as grep_module
import core.tools.search as search_module
from core.tools.grep import (
    GREP_TOOL_NAME,
    GREP_TOOL_PARAMETERS,
    MAX_OUTPUT_BYTES,
    grep_handler,
    register_grep_tool,
)
from core.tools.search import RESULTS_LIMITED_MARKER, SEARCH_TIMEOUT_MARKER
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope


def make_context(
    workspace: Path,
    tool_name: str = GREP_TOOL_NAME,
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


def force_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: None)


class FakeRgProcess:
    """Streaming stand-in for the ripgrep Popen handle."""

    def __init__(
        self, command: list[str], cwd: str, stdout_text: str, stderr_text: str, returncode: int
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.returncode = returncode
        self.killed = False
        self._finished = False
        self.creationflags = 0

    def poll(self) -> int | None:
        return self.returncode if self._finished else None

    def kill(self) -> None:
        self.killed = True
        self._finished = True

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self._finished = True
        return ("", self.stderr.read())


def install_fake_rg(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout_text: str = "",
    stderr_text: str = "",
    returncode: int = 0,
) -> list[FakeRgProcess]:
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: "rg")
    created: list[FakeRgProcess] = []

    def fake_popen(command: list[str], cwd: str | None = None, **_kwargs: Any) -> FakeRgProcess:
        process = FakeRgProcess(command, cwd or "", stdout_text, stderr_text, returncode)
        process.creationflags = int(_kwargs.get("creationflags", 0))
        created.append(process)
        return process

    monkeypatch.setattr(grep_module.subprocess, "Popen", fake_popen)
    return created


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
    assert parameters["properties"]["glob"]["description"] == (
        "Optional file glob filter for candidate files, relative to the search path."
    )
    assert "only for content" in parameters["properties"]["output_mode"]["description"]
    assert "default 0" in parameters["properties"]["context"]["description"]
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
    ("arguments", "message"),
    [
        ({"context": -1}, "context must be >= 0"),
        ({"context": True}, "context must be an integer"),
        ({"context": 1.5}, "context must be an integer"),
        ({"limit": 0}, "limit must be >= 1"),
        ({"limit": True}, "limit must be an integer"),
        ({"limit": 1.5}, "limit must be an integer"),
        ({"ignore_case": "maybe"}, "ignore_case must be a boolean"),
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


@pytest.mark.parametrize("output_mode", ["files_with_matches", "count"])
def test_grep_rejects_context_outside_content_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_mode: str,
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("one.txt").write_text("hit\n", encoding="utf-8")

    result = grep_handler(
        make_context(workspace),
        {"pattern": "hit", "output_mode": output_mode, "context": 1},
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert error["message"] == "context is only valid when output_mode is content"


def test_grep_literal_and_ignore_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("Alpha.1\nalphaX1\n", encoding="utf-8")

    regex_result = grep_handler(make_context(workspace), {"pattern": "Alpha.1"})
    literal_result = grep_handler(
        make_context(workspace), {"pattern": "Alpha.1", "literal": True, "ignore_case": True}
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

    context = make_context(workspace)
    result = grep_handler(context, {"pattern": "hit", "limit": 2})
    data = assert_success_envelope(result)
    assert data["content"] == (
        "notes.txt:1: hit\nnotes.txt:2: hit\n[Results limited to 2 matches.]"
    )
    assert context.presentation_facts == [
        {"kind": "count", "value": 2, "unit": "matches", "at_least": True}
    ]


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


def test_grep_returns_cancelled_failure_when_user_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace, user_cancelled=True), {"pattern": "needle"})

    assert_failure_envelope(result, "cancelled_by_user")


def test_grep_marks_timed_out_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(search_module, "SEARCH_TIMEOUT_SECONDS", -1.0)

    result = grep_handler(make_context(workspace), {"pattern": "needle"})

    content = get_success_content(result)
    assert SEARCH_TIMEOUT_MARKER in content


def test_grep_uses_python_fallback_when_rg_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("fallback hit\n", encoding="utf-8")

    def fail_if_called(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("subprocess.Popen should not be called without rg")

    monkeypatch.setattr(grep_module.subprocess, "Popen", fail_if_called)

    result = grep_handler(make_context(workspace), {"pattern": "fallback"})
    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:1: fallback hit"


def test_grep_returns_failure_for_rg_nonzero_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")
    install_fake_rg(monkeypatch, stderr_text="some unexpected rg failure", returncode=2)

    result = grep_handler(make_context(workspace), {"pattern": "hello"})
    error = assert_failure_envelope(result, "grep_error")
    assert error["message"] == "some unexpected rg failure"


def test_grep_returns_failure_for_discovered_rg_execution_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: "rg")

    def raise_oserror(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(grep_module.subprocess, "Popen", raise_oserror)

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
    monkeypatch.setattr(grep_module, "subprocess_creation_flags", lambda: 123)
    created = install_fake_rg(monkeypatch, stdout_text="notes.txt:1:hello\n")

    result = grep_handler(make_context(workspace), {"pattern": "hello"})
    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:1: hello"
    assert created[0].creationflags == 123


def test_grep_stops_reading_rg_output_at_limit_and_kills_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stdout_text = "".join(f"notes.txt:{index}:hit\n" for index in range(1, 6))
    created = install_fake_rg(monkeypatch, stdout_text=stdout_text)

    result = grep_handler(make_context(workspace), {"pattern": "hit", "limit": 2})

    data = assert_success_envelope(result)
    assert data["content"] == (
        f"notes.txt:1: hit\nnotes.txt:2: hit\n{RESULTS_LIMITED_MARKER.format(limit=2)}"
    )
    assert len(created) == 1
    assert created[0].killed is True


def test_grep_renders_rg_paths_relative_to_cwd_or_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    install_fake_rg(monkeypatch, stdout_text="sub\\a.txt:1:alpha\n")

    inside_result = grep_handler(make_context(workspace), {"pattern": "alpha"})
    outside_result = grep_handler(
        make_context(workspace), {"pattern": "alpha", "path": str(outside)}
    )

    assert get_success_content(inside_result) == "sub/a.txt:1: alpha"
    assert get_success_content(outside_result) == (
        f"{(outside / 'sub' / 'a.txt').resolve().as_posix()}:1: alpha"
    )


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


def test_grep_skips_gitignored_files_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath(".gitignore").write_text("node_modules/\n", encoding="utf-8")
    workspace.joinpath("node_modules").mkdir()
    workspace.joinpath("node_modules", "lib.js").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("app.js").write_text("needle\n", encoding="utf-8")

    default_result = grep_handler(make_context(workspace), {"pattern": "needle"})
    opted_in_result = grep_handler(
        make_context(workspace), {"pattern": "needle", "include_ignored": True}
    )

    assert get_success_content(default_result) == "app.js:1: needle"
    opted_in_content = get_success_content(opted_in_result)
    assert "app.js:1: needle" in opted_in_content
    assert "node_modules/lib.js:1: needle" in opted_in_content


def test_grep_absolute_repo_path_glob_does_not_override_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.joinpath(".git").mkdir()
    repo.joinpath(".gitignore").write_text(".opencode/\n", encoding="utf-8")
    ignored = repo / ".opencode" / "node_modules"
    ignored.mkdir(parents=True)
    ignored.joinpath("lib.js").write_text("needle\n", encoding="utf-8")
    repo.joinpath("app.js").write_text("needle\n", encoding="utf-8")
    created = install_fake_rg(
        monkeypatch,
        stdout_text=".opencode/node_modules/lib.js:1:needle\n",
    )

    result = grep_handler(
        make_context(workspace),
        {"pattern": "needle", "path": str(repo), "glob": "**/*"},
    )

    assert get_success_content(result) == f"{(repo / 'app.js').resolve().as_posix()}:1: needle"
    assert created == []


def test_grep_honors_nested_gitignore_negation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deeper .gitignore files win over shallower ones, git-style: the nested
    # negation re-includes a file the root pattern ignored.
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath(".gitignore").write_text("*.log\n", encoding="utf-8")
    workspace.joinpath("root.log").write_text("needle\n", encoding="utf-8")
    sub = workspace / "sub"
    sub.mkdir()
    sub.joinpath(".gitignore").write_text("!keep.log\n", encoding="utf-8")
    sub.joinpath("keep.log").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle"})

    assert get_success_content(result) == "sub/keep.log:1: needle"


def test_grep_always_skips_git_internals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git_dir = workspace / ".git"
    git_dir.mkdir()
    git_dir.joinpath("config").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("app.txt").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "include_ignored": True})

    assert get_success_content(result) == "app.txt:1: needle"


def test_grep_searches_explicitly_named_ignored_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath(".gitignore").write_text("secret.txt\n", encoding="utf-8")
    workspace.joinpath("secret.txt").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "path": "secret.txt"})

    assert get_success_content(result) == "secret.txt:1: needle"


def test_grep_searches_explicitly_targeted_ignored_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath(".git").mkdir()
    workspace.joinpath(".gitignore").write_text("vendor/\n", encoding="utf-8")
    vendor = workspace / "vendor"
    vendor.mkdir()
    vendor.joinpath("lib.js").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "path": "vendor"})

    assert get_success_content(result) == "vendor/lib.js:1: needle"


def test_grep_rg_command_uses_ignore_respecting_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    created = install_fake_rg(monkeypatch)

    grep_handler(make_context(workspace), {"pattern": "needle"})

    command = created[0].command
    assert "--no-ignore" not in command
    assert "--hidden" in command
    assert "--no-require-git" in command
    assert "--glob-case-insensitive" in command
    exclusion_index = command.index("!**/.git")
    assert command[exclusion_index - 1] == "--glob"


def test_grep_rg_command_disables_ignore_rules_on_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    created = install_fake_rg(monkeypatch)

    grep_handler(
        make_context(workspace),
        {"pattern": "needle", "glob": "**/*", "include_ignored": True},
    )

    command = created[0].command
    assert "--no-ignore" in command
    assert "!**/.git" in command
    assert "**/*" in command


def test_grep_rg_command_enables_multiline_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    created = install_fake_rg(monkeypatch)

    grep_handler(make_context(workspace), {"pattern": "alpha.beta", "multiline": True})

    command = created[0].command
    assert "--multiline" in command
    assert "--multiline-dotall" in command


def test_grep_multiline_matches_across_lines_in_python_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "alpha.beta", "multiline": True})

    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:1: alpha\nnotes.txt:2: beta"


def test_grep_multiline_counts_matches_not_lines_in_python_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("alpha\nbeta\nalpha\nbeta\n", encoding="utf-8")

    result = grep_handler(
        make_context(workspace),
        {"pattern": "alpha.beta", "multiline": True, "output_mode": "count"},
    )

    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:2"


def test_grep_pages_results_with_offset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hit\nhit\nhit\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "hit", "offset": 1, "limit": 1})

    data = assert_success_envelope(result)
    assert data["content"] == (f"notes.txt:2: hit\n{RESULTS_LIMITED_MARKER.format(limit=1)}")


def test_grep_reports_offset_beyond_total_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("hit\nhit\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "hit", "offset": 5})

    data = assert_success_envelope(result)
    assert data["content"] == "No results at offset 5; 2 matches total."


def test_grep_rg_offset_skips_streamed_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stdout_text = "".join(f"notes.txt:{index}:hit\n" for index in range(1, 4))
    install_fake_rg(monkeypatch, stdout_text=stdout_text)

    result = grep_handler(make_context(workspace), {"pattern": "hit", "offset": 1, "limit": 1})

    data = assert_success_envelope(result)
    assert data["content"] == (f"notes.txt:2: hit\n{RESULTS_LIMITED_MARKER.format(limit=1)}")


def test_grep_maps_rg_regex_parse_error_to_invalid_regex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    install_fake_rg(
        monkeypatch,
        stderr_text="regex parse error:\n    (?=x)\nerror: look-around is not supported",
        returncode=2,
    )

    result = grep_handler(make_context(workspace), {"pattern": "(?=x)"})

    error = assert_failure_envelope(result, "invalid_regex")
    assert "regex parse error" in error["message"]


def test_grep_runs_rg_even_when_python_rejects_the_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # \p{Lu} is valid Rust regex but invalid Python re: the executing engine
    # decides validity, so with rg available the search must succeed.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    install_fake_rg(monkeypatch, stdout_text="notes.txt:1:Xyz\n")

    result = grep_handler(make_context(workspace), {"pattern": r"\p{Lu}"})

    data = assert_success_envelope(result)
    assert data["content"] == "notes.txt:1: Xyz"


def test_grep_reports_invalid_regex_without_rg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("Xyz\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": r"\p{Lu}"})

    error = assert_failure_envelope(result, "invalid_regex")
    assert "invalid regex pattern" in error["message"]


def test_grep_searches_worktree_under_ignored_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Worktrees typically live in a gitignored .worktrees/ folder of the main
    # repo. A worktree carries its own .git pointer *file*, which bounds the
    # gitignore evaluation: the main repo's ".worktrees/" rule must not blank
    # out searches running inside the worktree, while the worktree's own
    # checked-out .gitignore still applies.
    force_python_fallback(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.joinpath(".git").mkdir()
    repo.joinpath(".gitignore").write_text(".worktrees/\nnode_modules/\n", encoding="utf-8")
    worktree = repo / ".worktrees" / "task"
    worktree.mkdir(parents=True)
    worktree.joinpath(".git").write_text("gitdir: ../../.git/worktrees/task\n", encoding="utf-8")
    worktree.joinpath(".gitignore").write_text(".worktrees/\nnode_modules/\n", encoding="utf-8")
    worktree.joinpath("app.py").write_text("needle in worktree\n", encoding="utf-8")
    worktree.joinpath("node_modules").mkdir()
    worktree.joinpath("node_modules", "lib.js").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(worktree), {"pattern": "needle"})

    assert get_success_content(result) == "app.py:1: needle in worktree"


def test_grep_never_surfaces_git_pointer_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath(".git").write_text("gitdir: ../elsewhere\n", encoding="utf-8")
    workspace.joinpath("app.py").write_text("gitdir mention\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "gitdir"})

    assert get_success_content(result) == "app.py:1: gitdir mention"


def test_grep_glob_filter_matches_case_insensitively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("keep.py").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("skip.txt").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "glob": "*.PY"})

    data = assert_success_envelope(result)
    assert data["content"] == "keep.py:1: needle"
