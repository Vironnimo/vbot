"""The Identity-Agent Project Context Tool through production dispatch."""

from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.permissions import BoundaryContext, PermissionBoundary, normalize_permission_rules
from core.projects import ProjectStore
from core.prompts.blocks import INLINE_FILE_MAX_BYTES
from core.tools import (
    FileReadState,
    StaleReason,
    ToolContext,
    ToolDefinitionProfileContext,
    ToolRegistry,
)
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.project import PROJECT_TOOL_NAME, register_project_tool
from core.utils.paths import model_path
from tests.core.prompts.prompts_test_support import _manager
from tests.core.tools.tools_test_support import dispatch_as_executor


def _context(
    tmp_path: Path,
    *,
    project_id: str | None = None,
    tool_denial_resolver: Any = None,
    permissions: PermissionBoundary = PermissionBoundary.UNRESTRICTED,
) -> ToolContext:
    return ToolContext(
        agent_id="coder",
        session_id="session-one",
        run_id="run-one",
        tool_call_id="call-one",
        tool_name=PROJECT_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path / "workspace",
        vbot_root=tmp_path,
        data_root=tmp_path,
        project_id=project_id,
        tool_denial_resolver=tool_denial_resolver,
        permissions=permissions,
    )


def _registry(
    projects: ProjectStore,
    file_state: FileReadState | None = None,
    skills: list[Any] | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    renderer = _manager(projects.data_dir)
    register_project_tool(
        registry,
        projects,
        lambda: renderer,
        lambda _project_id: list(skills or []),
        file_state or FileReadState(),
    )
    return registry


def _call(
    tmp_path: Path,
    projects: ProjectStore,
    arguments: Any,
    *,
    file_state: FileReadState | None = None,
    skills: list[Any] | None = None,
    project_id: str | None = None,
    permissions: PermissionBoundary = PermissionBoundary.UNRESTRICTED,
) -> dict[str, Any]:
    registry = _registry(projects, file_state, skills)
    context = _context(tmp_path, project_id=project_id, permissions=permissions)
    return asyncio.run(dispatch_as_executor(registry, context, arguments))


def test_project_tool_exposes_open_model_schema(tmp_path: Path) -> None:
    tool = _registry(ProjectStore(tmp_path / "data")).get(PROJECT_TOOL_NAME)

    assert tool.parameters["required"] == ["project_id"]
    assert tool.open_input_schema is True


def test_project_tool_loads_context_skills_and_stamps_files_read(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    repo = tmp_path / "repo"
    repo.mkdir()
    # CLAUDE.md is the only instruction file at creation; the AGENTS.md added
    # later replaces it as the main instruction file.
    (repo / "CLAUDE.md").write_text("Claude rules.", encoding="utf-8")
    skill_path = repo / ".opencode" / "skills" / "review" / "SKILL.md"
    projects = ProjectStore(data_dir)
    projects.create("vbot", "vBot", repo)
    agents_file = repo / "AGENTS.md"
    agents_file.write_text("Follow the Project rules.", encoding="utf-8")
    file_state = FileReadState()
    skill = SimpleNamespace(name="review", description="Review changes.", path=skill_path)

    result = _call(
        tmp_path, projects, {"project_id": "vbot"}, file_state=file_state, skills=[skill]
    )
    data = cast(dict[str, Any], result["data"])

    project_path = model_path(repo.resolve())
    assert result["ok"] is True
    # Files and Skills appear once, in content; Chat recovers the Project from
    # status and project_id.
    assert data == {
        "status": "loaded",
        "project_id": "vbot",
        "project_path": project_path,
        "content": data["content"],
    }
    assert data["content"].count("Follow the Project rules.") == 1
    assert "Claude rules." not in data["content"]
    assert data["content"].count("- review: Review changes.") == 1
    assert model_path(skill_path) not in data["content"]
    assert file_state.check_stale("session-one", agents_file.resolve()) is None
    # The user sees where the Project lives and the context the Agent received.
    display = _registry(projects).display_for_call(
        PROJECT_TOOL_NAME, {"project_id": "vbot"}, result=result
    )
    assert display["details"] == [
        {
            "type": "notice",
            "level": "info",
            "text": "Project Context loaded.",
            "subject": project_path,
        },
        {
            "type": "text",
            "label": "content",
            "source": {"from": "result", "path": ["data", "content"]},
        },
    ]


@pytest.mark.parametrize(
    ("allowed_tools", "denied_tool", "read_offered"),
    [
        (["project", "read"], None, True),
        (["project"], None, False),
        (["project", "read"], "read", False),
    ],
    ids=["run-allows-read", "run-lacks-read", "run-denies-read"],
)
def test_project_tool_lets_file_notices_name_read_only_when_the_run_can_call_it(
    tmp_path: Path, allowed_tools: list[str], denied_tool: str | None, read_offered: bool
) -> None:
    # An oversized auto-load file becomes a notice; the renderer asks which Tools the
    # notice may name, and the answer follows this Run's dispatch rules.
    repo = tmp_path / "repo"
    repo.mkdir()
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "vBot", repo)
    agents_file = repo / "AGENTS.md"
    agents_file.write_bytes(b"x" * (INLINE_FILE_MAX_BYTES + 1))
    file_state = FileReadState()
    context = _context(
        tmp_path,
        tool_denial_resolver=lambda name: "Denied here." if name == denied_tool else None,
    )

    result = asyncio.run(
        _registry(projects, file_state).dispatch(context, {"project_id": "vbot"}, allowed_tools)
    )

    assert result["ok"] is True
    content = result["data"]["content"]
    assert "automatically loaded files" in content
    assert ("`read` Tool" in content) is read_offered
    assert result["data"]["note"]
    assert file_state.check_stale("session-one", agents_file.resolve()) is StaleReason.NEVER_READ


def test_project_tool_returns_context_for_bare_project(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    projects = ProjectStore(tmp_path / "data")
    projects.create("empty", "Empty", repo)
    projects.update("empty", auto_load=[])

    result = _call(tmp_path, projects, {"project_id": "empty"})
    data = cast(dict[str, Any], result["data"])

    assert result["ok"] is True
    # No instruction files, so the result claims none.
    assert "Empty" in data["content"]
    assert "Follow the files" not in data["content"]
    assert "note" not in data


def test_project_tool_rejects_unknown_project(tmp_path: Path) -> None:
    result = _call(tmp_path, ProjectStore(tmp_path / "data"), {"project_id": "missing"})

    assert result["ok"] is False
    assert result["error"]["code"] == "project_not_found"
    assert result["error"]["retryable"] is False
    assert '"missing"' in result["error"]["message"]
    assert "No loadable Projects" in result["error"]["message"]


@pytest.mark.parametrize("project_id", ["vBot", "VBOT", "the vbot project", "../vbot"])
def test_project_tool_never_guesses_a_project_and_names_the_ids(
    tmp_path: Path, project_id: str
) -> None:
    # A display name, another casing, or an invalid id is not a Project id: the
    # failure lists the exact ids instead of loading a near match.
    projects = ProjectStore(tmp_path / "data")
    for key, name in (
        ("vbot", "vBot"),
        ("docs", "Docs Site"),
        ("alpha", "Alpha"),
        ("beta", "Beta"),
    ):
        (tmp_path / key).mkdir()
        projects.create(key, name, tmp_path / key)

    result = _call(tmp_path, projects, {"project_id": project_id})

    assert result["ok"] is False
    assert result["error"]["code"] == "project_not_found"
    assert project_id in result["error"]["message"]
    assert "- vbot: vBot" in result["error"]["message"]
    assert (
        len([line for line in result["error"]["message"].splitlines() if line.startswith("- ")])
        <= 3
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"project": "vbot"},
        {"id": "vbot"},
        {"name": "vbot"},
        {"projectId": "vbot"},
        {"Project-ID": "vbot"},
    ],
)
def test_project_tool_reads_other_spellings_of_project_id(
    tmp_path: Path, arguments: dict[str, str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "vBot", repo)

    result = _call(tmp_path, projects, arguments)

    assert result["ok"] is True, result
    assert result["data"]["project_id"] == "vbot"


def test_project_tool_refuses_conflicting_project_ids(tmp_path: Path) -> None:
    result = _call(
        tmp_path, ProjectStore(tmp_path / "data"), {"project_id": "vbot", "project": "docs"}
    )

    assert result["error"]["code"] == "invalid_arguments"
    assert "Conflicting values for project_id" in result["error"]["message"]


def test_project_tool_rejects_unknown_argument_before_loading(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    agents_file = repo / "AGENTS.md"
    agents_file.write_text("Follow the Project rules.", encoding="utf-8")
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "vBot", repo)
    file_state = FileReadState()

    result = _call(tmp_path, projects, {"project_id": "vbot", "force": True}, file_state=file_state)

    assert result["error"]["code"] == "invalid_arguments"
    assert '"force" is not a parameter' in result["error"]["message"]
    assert file_state.check_stale("session-one", agents_file.resolve()) is StaleReason.NEVER_READ


@pytest.mark.parametrize("project_id", [None, ""])
def test_project_tool_rejects_missing_or_empty_project_id(
    tmp_path: Path, project_id: object
) -> None:
    arguments = {} if project_id is None else {"project_id": project_id}

    result = _call(tmp_path, ProjectStore(tmp_path / "data"), arguments)

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert "project_id" in result["error"]["message"]


@pytest.mark.parametrize("is_file", [False, True], ids=["missing", "file"])
def test_project_tool_rejects_unreachable_project_path(tmp_path: Path, is_file: bool) -> None:
    projects = ProjectStore(tmp_path / "data")
    path = tmp_path / "not-a-directory"
    projects.create("missing", "Missing", path)
    if is_file:
        path.write_text("file", encoding="utf-8")

    result = _call(tmp_path, projects, {"project_id": "missing"})

    assert result["ok"] is False
    code = "project_path_not_directory" if is_file else "project_path_missing"
    assert result["error"]["code"] == code
    assert model_path(path) in result["error"]["message"]
    assert "No new Project Context" in result["error"]["message"]


def test_project_tool_refuses_a_project_run(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "vBot", repo)

    result = _call(tmp_path, projects, {"project_id": "vbot"}, project_id="vbot")

    assert result["ok"] is False
    assert result["error"]["code"] == "project_identity_required"


def test_project_tool_does_not_stamp_missing_auto_load_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "vBot", repo)
    file_state = FileReadState()

    result = _call(tmp_path, projects, {"project_id": "vbot"}, file_state=file_state)

    assert result["ok"] is True
    assert "note" not in result["data"]
    assert (
        file_state.check_stale("session-one", (repo / "AGENTS.md").resolve())
        is StaleReason.NEVER_READ
    )


@pytest.mark.parametrize("available", [False, True])
def test_project_result_guidance_names_only_available_tools(
    tmp_path: Path, available: bool
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "vBot", repo)
    skill = SimpleNamespace(name="review", description="Review changes.")
    registry = _registry(projects, skills=[skill])
    allowed = ["project", "read", "bash", "skill"] if available else ["project"]

    result = asyncio.run(registry.dispatch(_context(tmp_path), {"project_id": "vbot"}, allowed))

    assert result["ok"] is True
    content = result["data"]["content"]
    assert "- review: Review changes." in content
    assert ("`skill` Tool" in content) is available
    assert (f"`{SHELL_MODEL_NAME}` call" in content) is available
    assert ("absolute file paths" in content) is available
    if available:
        assert "For shell work in this Project" in content


@pytest.mark.parametrize("problem", ["missing", "encoding", "directory", "denied", "io"])
def test_project_load_keeps_readable_instructions_and_reports_omissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    good = repo / "AGENTS.md"
    good.write_text("GOOD_INSTRUCTIONS", encoding="utf-8")
    bad = repo / "other.md"
    if problem == "encoding":
        bad.write_bytes(b"\xff")
    elif problem == "directory":
        bad.mkdir()
    elif problem in {"denied", "io"}:
        bad.write_text("UNREAD_INSTRUCTIONS", encoding="utf-8")
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "vBot", repo, auto_load=["other.md"])
    original = Path.open

    def open_file(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == bad and problem in {"denied", "io"}:
            raise OSError(errno.EACCES if problem == "denied" else errno.EIO, "RAW_OS_ERROR")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    file_state = FileReadState()

    result = _call(tmp_path, projects, {"project_id": "vbot"}, file_state=file_state)

    assert result["ok"] is True
    assert result["data"]["status"] == "loaded"
    assert "GOOD_INSTRUCTIONS" in result["data"]["content"]
    assert "UNREAD_INSTRUCTIONS" not in str(result)
    note = result["data"]["note"]
    assert model_path(bad) in note
    expected = {
        "missing": "does not exist",
        "encoding": "not valid UTF-8",
        "directory": "not a readable file",
        "denied": "access was denied",
        "io": "could not be read",
    }
    assert expected[problem] in note
    assert "RAW_OS_ERROR" not in str(result)
    assert "reload the Project" in note
    assert file_state.check_stale("session-one", good.resolve()) is None
    assert file_state.check_stale("session-one", bad.resolve()) is StaleReason.NEVER_READ
    details = _registry(projects).display_for_call(
        "project", {"project_id": "vbot"}, result=result
    )["details"]
    assert any(detail.get("level") == "warning" and detail["text"] == note for detail in details)


@pytest.mark.parametrize("denied", ["repo", "file"])
def test_project_load_reads_only_what_the_permission_rules_permit(
    tmp_path: Path, denied: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("GOOD_INSTRUCTIONS", encoding="utf-8")
    other = repo / "other.md"
    other.write_text("UNREAD_INSTRUCTIONS", encoding="utf-8")
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "vBot", repo, auto_load=["AGENTS.md", "other.md"])
    pattern = repo if denied == "repo" else other
    boundary = PermissionBoundary.build(
        normalize_permission_rules({"deny": [f"Read({pattern.as_posix()})"]}),
        BoundaryContext(
            workdir=tmp_path / "workspace", home=tmp_path / "home", data_dir=tmp_path / "data"
        ),
    )
    file_state = FileReadState()

    result = _call(
        tmp_path, projects, {"project_id": "vbot"}, file_state=file_state, permissions=boundary
    )

    assert "UNREAD_INSTRUCTIONS" not in str(result)
    assert file_state.check_stale("session-one", other.resolve()) is StaleReason.NEVER_READ
    if denied == "repo":
        assert result["error"]["code"] == "permission_denied"
        assert "GOOD_INSTRUCTIONS" not in str(result)
    else:
        assert "GOOD_INSTRUCTIONS" in result["data"]["content"]
        assert model_path(other) in result["data"]["note"]
        assert "permission rules" in result["data"]["note"]


@pytest.mark.parametrize(
    ("problem", "location"),
    [
        ("invalid", "config"),
        ("denied", "config"),
        ("transient", "config"),
        ("io", "config"),
        ("internal", "config"),
        ("denied", "cwd"),
    ],
)
def test_project_load_failures_explain_recovery_without_claiming_a_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str, location: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "vBot", repo)
    config = projects.data_dir / "projects" / "vbot" / "project.json"
    if problem == "invalid":
        config.write_text("{broken json", encoding="utf-8")
    original = Path.read_text

    def read_file(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == config and location == "config" and problem not in {"invalid", "internal"}:
            code = {"denied": errno.EACCES, "transient": errno.ETIMEDOUT, "io": errno.EINVAL}[
                problem
            ]
            raise OSError(code, "RAW_OS_ERROR")
        if path == config and problem == "internal":
            raise RuntimeError("INTERNAL_DETAIL")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_file)
    original_stat = os.stat

    def stat_path(path: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(path, (str, Path)) and Path(path) == repo and location == "cwd":
            raise PermissionError(errno.EACCES, "RAW_OS_ERROR")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat_path)

    result = _call(tmp_path, projects, {"project_id": "vbot"})

    assert result["ok"] is False
    assert result["data"] is None
    assert (
        result["error"]["code"]
        == {
            "invalid": "project_configuration_invalid",
            "denied": "project_access_denied",
            "transient": "project_io_error",
            "io": "project_io_error",
            "internal": "project_load_failed",
        }[problem]
    )
    message = result["error"]["message"]
    assert "No new Project Context was loaded" in message
    assert "vbot" in message
    assert "RAW_OS_ERROR" not in message and "INTERNAL_DETAIL" not in message
    assert result["error"]["retryable"] is (problem in {"transient", "internal"})


def test_unavailable_project_catalog_is_not_an_empty_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "vBot", tmp_path / "repo")
    config = projects.data_dir / "projects" / "vbot" / "project.json"
    original = Path.read_text

    def read_file(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == config:
            raise PermissionError(errno.EACCES, "RAW_OS_ERROR")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_file)
    registry = _registry(projects)
    definitions = registry.provider_definitions(
        ["project"], profile_context=ToolDefinitionProfileContext("coder")
    )
    description = definitions[0]["parameters"]["properties"]["project_id"]["description"]
    assert "unavailable" in description
    assert "No loadable Projects" not in description

    result = asyncio.run(
        dispatch_as_executor(registry, _context(tmp_path), {"project_id": "unknown"})
    )

    assert result["error"]["code"] == "project_not_found"
    assert "could not be read" in result["error"]["message"]
    assert "No loadable Projects" not in result["error"]["message"]
    monkeypatch.setattr(Path, "read_text", original)
    recovered = registry.provider_definitions(
        ["project"], profile_context=ToolDefinitionProfileContext("coder")
    )
    assert "- vbot: vBot" in recovered[0]["parameters"]["properties"]["project_id"]["description"]
