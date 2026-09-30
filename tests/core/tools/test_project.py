"""The Identity-Agent Project Context Tool through production dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.projects import ProjectStore
from core.tools import FileReadState, StaleReason, ToolContext, ToolRegistry
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.project import PROJECT_TOOL_NAME, register_project_tool
from core.utils.paths import model_path
from tests.core.tools.tools_test_support import dispatch_as_executor


class _Renderer:
    def render_project_files(self, project_context: Any, *, on_read: Any = None) -> str:
        blocks: list[str] = []
        for name in project_context.auto_load:
            path = (project_context.cwd / name).resolve()
            if not path.is_file():
                continue
            if on_read is not None:
                on_read(path)
            blocks.append(f'<file name="{name}">\n{path.read_text(encoding="utf-8")}\n</file>')
        return "\n".join(blocks)

    def render_project_skills(self, project_name: str, skills: Sequence[Any]) -> str:
        if not skills:
            return ""
        lines = [f"Skills from project '{project_name}':"]
        lines.extend(f"- {skill.name}: {skill.description}" for skill in skills)
        return "\n".join(lines)


def _context(tmp_path: Path, *, project_id: str | None = None) -> ToolContext:
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
    )


def _registry(
    projects: ProjectStore,
    file_state: FileReadState | None = None,
    skills: list[Any] | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    renderer = _Renderer()
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
) -> dict[str, Any]:
    registry = _registry(projects, file_state, skills)
    context = _context(tmp_path, project_id=project_id)
    return asyncio.run(dispatch_as_executor(registry, context, arguments))


def test_project_tool_exposes_open_model_schema(tmp_path: Path) -> None:
    tool = _registry(ProjectStore(tmp_path / "data")).get(PROJECT_TOOL_NAME)

    assert tool.parameters["required"] == ["project_id"]
    assert tool.open_input_schema is True


def test_project_tool_loads_context_skills_and_stamps_files_read(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    repo = tmp_path / "repo"
    repo.mkdir()
    agents_file = repo / "AGENTS.md"
    agents_file.write_text("Follow the Project rules.", encoding="utf-8")
    skill_path = repo / ".opencode" / "skills" / "review" / "SKILL.md"
    projects = ProjectStore(data_dir)
    projects.create("vbot", "vBot", repo)
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
    assert data["content"] == (
        "Project Context loaded for 'vBot'. The files below are this Project's instructions: "
        "follow them for all work in this Project. Your working directory, Workspace, and "
        "permissions are unchanged, so use absolute paths for file Tools and set `workdir` to "
        f"'{project_path}' on every `{SHELL_MODEL_NAME}` call.\n\n"
        '<file name="AGENTS.md">\nFollow the Project rules.\n</file>\n\n'
        "Skills from project 'vBot':\n- review: Review changes."
    )
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
    assert data["content"] == (
        "Project Context loaded for 'Empty'. Your working directory, Workspace, and "
        "permissions are unchanged, so use absolute paths for file Tools and set `workdir` to "
        f"'{model_path(repo.resolve())}' on every `{SHELL_MODEL_NAME}` call."
    )


def test_project_tool_rejects_unknown_project(tmp_path: Path) -> None:
    result = _call(tmp_path, ProjectStore(tmp_path / "data"), {"project_id": "missing"})

    assert result["ok"] is False
    assert result["error"] == {
        "code": "project_not_found",
        "message": (
            "Project not found: missing. No Projects are registered, so there is no Project "
            "Context to load."
        ),
        "retryable": False,
    }


@pytest.mark.parametrize("project_id", ["vBot", "VBOT", "the vbot project", "../vbot"])
def test_project_tool_never_guesses_a_project_and_names_the_ids(
    tmp_path: Path, project_id: str
) -> None:
    # A display name, another casing, or an invalid id is not a Project id: the
    # failure lists the exact ids instead of loading a near match.
    projects = ProjectStore(tmp_path / "data")
    for key, name in (("vbot", "vBot"), ("docs", "Docs Site")):
        (tmp_path / key).mkdir()
        projects.create(key, name, tmp_path / key)

    result = _call(tmp_path, projects, {"project_id": project_id})

    assert result["error"] == {
        "code": "project_not_found",
        "message": (
            f"Project not found: {project_id}. Use one of these registered Project ids "
            "exactly: docs (Docs Site), vbot (vBot)."
        ),
        "retryable": False,
    }


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

    result = _call(
        tmp_path, projects, {"project_id": "vbot", "unexpected": True}, file_state=file_state
    )

    assert result["error"]["code"] == "invalid_arguments"
    assert '"unexpected" is not a parameter' in result["error"]["message"]
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


def test_project_tool_rejects_unreachable_project_path(tmp_path: Path) -> None:
    projects = ProjectStore(tmp_path / "data")
    projects.create("missing", "Missing", tmp_path / "does-not-exist")

    result = _call(tmp_path, projects, {"project_id": "missing"})

    assert result["ok"] is False
    assert result["error"]["code"] == "project_unavailable"
    assert "no reachable Project path" in result["error"]["message"]


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
    assert (
        file_state.check_stale("session-one", (repo / "AGENTS.md").resolve())
        is StaleReason.NEVER_READ
    )
