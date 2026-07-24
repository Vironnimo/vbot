"""Tests for the explicit Identity-Agent Project Context Tool."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from core.projects import ProjectStore
from core.tools import FileReadState, StaleReason, ToolContext
from core.tools.project import PROJECT_TOOL_NAME, make_project_handler


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
        lines.extend(f"- {skill.name}: {skill.description} ({skill.path})" for skill in skills)
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
        app_root=tmp_path,
        data_root=tmp_path,
        project_id=project_id,
    )


def _handler(
    projects: ProjectStore,
    file_state: FileReadState,
    skills: list[Any] | None = None,
) -> Any:
    renderer = _Renderer()
    return make_project_handler(
        projects,
        lambda: renderer,
        lambda _project_id: list(skills or []),
        file_state,
    )


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

    result = _handler(projects, file_state, [skill])(_context(tmp_path), {"project_id": "vbot"})
    data = cast(dict[str, Any], result["data"])

    assert result["ok"] is True
    assert data["status"] == "loaded"
    assert data["project_id"] == "vbot"
    assert data["display_name"] == "vBot"
    assert data["cwd"] == str(repo.resolve())
    assert "Project Context loaded for 'vBot'" in data["content"]
    assert "This call did not change your home Workspace, cwd, Rooting" in data["content"]
    assert "available through the `skill` Tool in this Session" in data["content"]
    assert "on every `bash` call; each call starts a new shell" in data["content"]
    assert "Follow the Project rules." in data["content"]
    assert "Skills from project 'vBot'" in data["content"]
    assert data["loaded_files"] == [str(agents_file.resolve())]
    assert data["skills"] == [
        {"name": "review", "description": "Review changes.", "path": str(skill_path)}
    ]
    assert file_state.check_stale("session-one", agents_file.resolve()) is None


def test_project_tool_returns_context_for_bare_project(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    projects = ProjectStore(tmp_path / "data")
    projects.create("empty", "Empty", repo)
    projects.update("empty", auto_load=[])

    result = _handler(projects, FileReadState())(_context(tmp_path), {"project_id": "empty"})
    data = cast(dict[str, Any], result["data"])

    assert result["ok"] is True
    assert data["loaded_files"] == []
    assert data["skills"] == []
    assert "Project Context loaded for 'Empty'" in data["content"]
    assert str(repo.resolve()) in data["content"]


def test_project_tool_rejects_unknown_project(tmp_path: Path) -> None:
    result = _handler(ProjectStore(tmp_path / "data"), FileReadState())(
        _context(tmp_path), {"project_id": "missing"}
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "project_not_found"
    assert result["error"]["retryable"] is False


def test_project_tool_rejects_unreachable_project_cwd(tmp_path: Path) -> None:
    projects = ProjectStore(tmp_path / "data")
    projects.create("missing", "Missing", tmp_path / "does-not-exist")

    result = _handler(projects, FileReadState())(_context(tmp_path), {"project_id": "missing"})

    assert result["ok"] is False
    assert result["error"]["code"] == "project_unavailable"
    assert "no reachable cwd" in result["error"]["message"]


def test_project_tool_rejects_config_agent_even_if_called_directly(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "vBot", repo)

    result = _handler(projects, FileReadState())(
        _context(tmp_path, project_id="vbot"), {"project_id": "vbot"}
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "project_identity_required"


def test_project_tool_does_not_stamp_missing_auto_load_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "vBot", repo)
    file_state = FileReadState()

    result = _handler(projects, file_state)(_context(tmp_path), {"project_id": "vbot"})

    assert result["ok"] is True
    assert (
        file_state.check_stale("session-one", (repo / "AGENTS.md").resolve())
        is StaleReason.NEVER_READ
    )
