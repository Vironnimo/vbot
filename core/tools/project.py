"""Explicit Project Context loading for Identity Agents."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from html import escape
from pathlib import Path
from typing import Any, Protocol

from core.projects import (
    InvalidProjectIdError,
    ProjectError,
    ProjectNotFoundError,
    ProjectStore,
    cwd_exists,
)
from core.tools.arguments import required_string
from core.tools.file_state import FileReadState
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolPromptBlockRegistry,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.paths import model_path

PROJECT_TOOL_NAME = "project"
PROJECT_TOOL_DESCRIPTION = (
    "Load a registered Project's current instructions, absolute cwd, and Project Skills. "
    "Call it alone and wait for the result before taking dependent actions in that Project. "
    "Loading Project Context does not change the current working directory."
)
PROJECT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "project_id": {
            "type": "string",
            "minLength": 1,
            "description": "Registered Project id from the Projects list in the System Prompt.",
        }
    },
    "required": ["project_id"],
}

PROJECT_PROMPT_BLOCK_HEADER = (
    "## Projects\n\n"
    "Projects are registered execution contexts. Before working on a registered Project "
    "that is not already your current working Project, call `project` with its exact id. "
    "Call it alone and wait for the result before any dependent file, search, edit, or shell "
    "Tool call; sibling Tool calls may run concurrently.\n\n"
    "The `project` Tool loads the Project's current instructions, absolute cwd, and Project "
    "Skills. It does not change your cwd, Rooting, Workspace, Session ownership, Skill scope, "
    "or permissions. After loading, use absolute paths for file Tools. Set `workdir` to the "
    "returned cwd on every `bash` call; each call starts a new shell and does not retain cwd "
    "changes from earlier calls.\n\n"
    "Registered Projects:"
)


class ProjectContextRenderer(Protocol):
    """Prompt rendering methods needed by the Project Tool."""

    def render_project_files(
        self,
        project_context: Any,
        *,
        on_read: Callable[[Path], None] | None = None,
    ) -> str:
        """Render configured Project files and report successfully read paths."""
        ...

    def render_project_skills(self, project_name: str, skills: Sequence[Any]) -> str:
        """Render a path-bearing Project Skill list."""
        ...


ProjectContextRendererResolver = Callable[[], ProjectContextRenderer]
ProjectSkillsResolver = Callable[[str], list[Any]]


def make_project_handler(
    projects: ProjectStore,
    get_renderer: ProjectContextRendererResolver,
    list_project_skills: ProjectSkillsResolver,
    file_state: FileReadState,
) -> Any:
    """Create the explicit Project Context loader bound to runtime services."""

    def project_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        unknown_arguments = set(arguments) - {"project_id"}
        if unknown_arguments:
            names = ", ".join(sorted(unknown_arguments))
            return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

        # The generic availability layer enforces this at prompt and dispatch time.
        # Keep the handler guard too: direct callers and a future wiring regression
        # must not turn an Identity-only capability into cross-Project access for a
        # Config Agent.
        if context.project_id is not None:
            return tool_failure(
                "project_identity_required",
                "The project tool is available only to Identity Agents.",
                retryable=False,
            )

        try:
            project_id = required_string(arguments.get("project_id"), field_name="project_id")
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error), retryable=False)

        try:
            project = projects.get(project_id)
        except InvalidProjectIdError as error:
            return tool_failure("invalid_arguments", str(error), retryable=False)
        except ProjectNotFoundError:
            return tool_failure(
                "project_not_found",
                f"Project not found: {project_id}",
                retryable=False,
            )
        except (ProjectError, OSError) as error:
            return tool_failure(
                "project_unavailable",
                f"Project '{project_id}' could not be loaded: {error}",
                retryable=False,
            )

        if not cwd_exists(project.cwd):
            return tool_failure(
                "project_unavailable",
                f"Project '{project.project_id}' has no reachable cwd: {model_path(project.cwd)}",
                retryable=False,
            )

        try:
            from core.prompts import ProjectPromptContext

            renderer = get_renderer()
            read_paths: list[Path] = []
            rendered_files = renderer.render_project_files(
                ProjectPromptContext.from_project(
                    project.project_id,
                    project.display_name,
                    project.cwd,
                    project.auto_load,
                ),
                on_read=read_paths.append,
            )
            skills = list_project_skills(project.project_id)
            rendered_skills = renderer.render_project_skills(project.display_name, skills)
        except (ProjectError, OSError) as error:
            return tool_failure(
                "project_unavailable",
                f"Project '{project.project_id}' context could not be loaded: {error}",
                retryable=False,
            )

        for path in read_paths:
            file_state.record_read(context.session_id, path)

        displayed_cwd = model_path(project.cwd)
        content = _render_project_context(
            project.project_id,
            project.display_name,
            displayed_cwd,
            rendered_files,
            rendered_skills,
        )
        return tool_success(
            {
                "status": "loaded",
                "project_id": project.project_id,
                "display_name": project.display_name,
                "cwd": displayed_cwd,
                "content": content,
                "loaded_files": [model_path(path) for path in read_paths],
                "skills": [_skill_payload(skill) for skill in skills],
            }
        )

    return project_handler


def register_project_tool(
    registry: ToolRegistry,
    projects: ProjectStore,
    get_renderer: ProjectContextRendererResolver,
    list_project_skills: ProjectSkillsResolver,
    file_state: FileReadState,
    prompt_blocks: ToolPromptBlockRegistry | None = None,
) -> None:
    """Register the Identity-only Project Context loader and its prompt block."""
    registry.register(
        PROJECT_TOOL_NAME,
        PROJECT_TOOL_DESCRIPTION,
        PROJECT_TOOL_PARAMETERS,
        make_project_handler(projects, get_renderer, list_project_skills, file_state),
        open_input_schema=True,
        result_schema={
            "type": "object",
            "required": ["status", "project_id", "display_name", "cwd", "content"],
        },
        display=ToolDisplay(summary_fields=("project_id",)),
        parallel_safe=True,
    )
    if prompt_blocks is not None:
        prompt_blocks.register(
            PROJECT_TOOL_NAME,
            render=lambda context: _render_project_prompt_block(context, projects),
        )


def _render_project_prompt_block(context: Any, projects: ProjectStore) -> str:
    active_project_id = getattr(context.agent, "root_project_id", None)
    project_lines = [
        _project_prompt_line(project, active_project_id=active_project_id)
        for project in projects.list()
    ]
    if not project_lines:
        project_lines.append("**No Projects are currently registered.**")
    return f"{PROJECT_PROMPT_BLOCK_HEADER}\n\n" + "\n".join(project_lines)


def _project_prompt_line(project: Any, *, active_project_id: str | None) -> str:
    attributes = [
        f'id="{escape(project.project_id, quote=True)}"',
        f'name="{escape(_single_line(project.display_name), quote=True)}"',
        f'cwd="{escape(model_path(project.cwd), quote=True)}"',
        f'available="{str(cwd_exists(project.cwd)).lower()}"',
    ]
    if project.project_id == active_project_id:
        attributes.append('active="true"')
    return f"<project {' '.join(attributes)} />"


def _render_project_context(
    project_id: str,
    display_name: str,
    cwd: str,
    rendered_files: str,
    rendered_skills: str,
) -> str:
    preamble = (
        f"Project Context loaded for '{display_name}' (id: '{project_id}') at '{cwd}'. "
        "The auto-loaded files below are this Project's instructions. Follow them for every "
        "action that affects this Project while this context is relevant in the Session. "
        "They apply only to this Project. This call did not change your home Workspace, cwd, "
        "Rooting, Session ownership, or permissions. The Project Skills listed below are now "
        "available through the `skill` Tool in this Session. Use absolute paths for file Tools. "
        f"Set `workdir` to '{cwd}' on every `bash` call; each call starts a new shell and does "
        "not retain cwd changes from an earlier call."
    )
    sections = [preamble]
    sections.extend(section for section in (rendered_files, rendered_skills) if section.strip())
    return "\n\n".join(sections)


def _skill_payload(skill: Any) -> JsonObject:
    return {
        "name": str(skill.name),
        "description": str(skill.description),
        "path": model_path(skill.path),
    }


def _single_line(value: str) -> str:
    return " ".join(value.split())


__all__ = [
    "PROJECT_PROMPT_BLOCK_HEADER",
    "PROJECT_TOOL_DESCRIPTION",
    "PROJECT_TOOL_NAME",
    "PROJECT_TOOL_PARAMETERS",
    "make_project_handler",
    "register_project_tool",
]
