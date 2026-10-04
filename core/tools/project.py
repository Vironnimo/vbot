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
from core.settings import PROJECT_ID_PATTERN
from core.tools.arguments import required_string
from core.tools.call_syntax import SpellingAliases, normalize_call_arguments
from core.tools.contracts import compile_tool_contract
from core.tools.file_state import FileReadState
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayField,
    ToolPromptBlockRegistry,
    ToolRegistry,
    display_notice,
    display_text,
    offload_tool_handler,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger
from core.utils.paths import model_path

_LOGGER = get_logger("tools.project")

PROJECT_TOOL_NAME = "project"
PROJECT_TOOL_DESCRIPTION = (
    "Load a registered Project's instructions, absolute Project path, and Project Skills into "
    "this Session. It does not change your working directory, Workspace, or permissions."
)
PROJECT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "project_id": {
            "type": "string",
            "description": "Exact id of a Project listed under Registered Projects.",
        }
    },
    "required": ["project_id"],
}

PROJECT_PROMPT_BLOCK_HEADER = (
    "## Projects\n\n"
    "Before working on a registered Project, call `project` with its exact id, unless it is "
    "the Project in the Working Project section of this System Prompt. Call it alone and wait "
    "for its result before any dependent Tool call, because sibling calls run concurrently. "
    "Loading does not change your working directory: use absolute paths for file Tools, and "
    f"set `workdir` to the returned `project_path` on every `{SHELL_MODEL_NAME}` call.\n\n"
    "Registered Projects:"
)
# Names the Project list when vBot tells the Model that it changed mid-Session.
PROJECT_CATALOG_TITLE = "Registered Projects"
# ``choices`` lists registered ids with their display names.
_PROJECT_NOT_FOUND_MESSAGE_TEMPLATE = (
    "Project not found: {project_id}. Use one of these registered Project ids exactly: {choices}."
)
_NO_PROJECTS_MESSAGE_TEMPLATE = (
    "Project not found: {project_id}. No Projects are registered, so there is no Project "
    "Context to load."
)
_PROJECT_CHOICE_LIMIT = 30
# A display name sent under one of these keys fails with the exact ids; it never
# selects a Project.
_FIELD_ALIASES = SpellingAliases({"project_id": ("project", "id", "name", "project_name")})
_PROJECT_CONTRACT = compile_tool_contract(
    name=PROJECT_TOOL_NAME, input_schema=PROJECT_TOOL_PARAMETERS, require_closed_input=False
)


class ProjectContextRenderer(Protocol):
    """Prompt rendering methods needed by the Project Tool."""

    def render_project_files(
        self,
        project_context: Any,
        *,
        tool_available: Callable[[str], bool],
        on_read: Callable[[Path], None] | None = None,
    ) -> str:
        """Render configured Project files and report successfully read paths.

        *tool_available* answers which Tools the receiving Agent can call, by
        registry name; a notice for an oversized file names only those.
        """
        ...

    def render_project_skills(self, project_name: str, skills: Sequence[Any]) -> str:
        """Render a Project Skill list containing names and descriptions."""
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
            # An id that cannot exist is as missing as one that does not.
            if PROJECT_ID_PATTERN.fullmatch(project_id) is None:
                raise ProjectNotFoundError(project_id)
            project = projects.get(project_id)
        except InvalidProjectIdError, ProjectNotFoundError:
            return tool_failure(
                "project_not_found",
                _project_not_found_message(projects, project_id),
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
                f"Project '{project.project_id}' has no reachable Project path: "
                f"{model_path(project.cwd)}",
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
                tool_available=context.can_call,
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

        project_path = model_path(project.cwd)
        content = _render_project_context(
            project.display_name, project_path, rendered_files, rendered_skills
        )
        # ``status`` and ``project_id`` are what Chat recovers the loaded Project from;
        # the files and Skills are listed once, in ``content``.
        return tool_success(
            {
                "status": "loaded",
                "project_id": project.project_id,
                "project_path": project_path,
                "content": content,
            }
        )

    return project_handler


def _project_detail_blocks(arguments: JsonObject, result: JsonObject | None) -> list[JsonObject]:
    """Show the user the Project path and the Project Context the Agent received."""
    data = result.get("data") if isinstance(result, dict) and result.get("ok") is True else None
    if not isinstance(data, dict):
        return []
    path = data.get("project_path")
    return [
        display_notice(
            "info", "Project Context loaded.", subject=path if isinstance(path, str) else None
        ),
        display_text("content", source="result", path=("data", "content")),
    ]


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
        offload_tool_handler(
            make_project_handler(projects, get_renderer, list_project_skills, file_state)
        ),
        constraints=("identity_agent",),
        open_input_schema=True,
        argument_normalizer=_normalize_project_arguments,
        result_schema={
            "type": "object",
            "required": ["status", "project_id", "project_path", "content"],
        },
        display=ToolDisplay(
            primary_candidates=(
                ToolDisplayField("project_id", kind="identifier", truncate="middle"),
            ),
            detail_builder=_project_detail_blocks,
        ),
        parallel_safe=True,
    )
    if prompt_blocks is not None:
        prompt_blocks.register(
            PROJECT_TOOL_NAME,
            render=lambda context: _render_project_prompt_block(context, projects),
        )


def _render_project_prompt_block(context: Any, projects: ProjectStore) -> Any:
    """Render the registered Projects as a catalog, one entry per Project id.

    Nothing here depends on the Agent or on the disk state of a Project path: the
    block stays the same for every Agent until a Project is registered, changed
    or removed. A path that is unreachable fails the ``project`` call instead.
    """
    from core.prompts import BlockCatalog, RenderedBlock

    del context
    entries = tuple(
        (project.project_id, _project_prompt_line(project)) for project in projects.list()
    )
    lines = [line for _key, line in entries] or ["**No Projects are currently registered.**"]
    return RenderedBlock(
        text=f"{PROJECT_PROMPT_BLOCK_HEADER}\n\n" + "\n".join(lines),
        catalog=BlockCatalog(
            title=PROJECT_CATALOG_TITLE, entries=entries, frame=PROJECT_PROMPT_BLOCK_HEADER
        ),
    )


def _project_prompt_line(project: Any) -> str:
    attributes = [
        f'id="{escape(project.project_id, quote=True)}"',
        f'name="{escape(_single_line(project.display_name), quote=True)}"',
        f'project_path="{escape(model_path(project.cwd), quote=True)}"',
    ]
    return f"<project {' '.join(attributes)} />"


def _render_project_context(
    display_name: str,
    project_path: str,
    rendered_files: str,
    rendered_skills: str,
) -> str:
    instructions = (
        " The files below are this Project's instructions: follow them for all work in this "
        "Project."
        if rendered_files.strip()
        else ""
    )
    preamble = (
        f"Project Context loaded for '{display_name}'.{instructions} Your working directory, "
        "Workspace, and permissions are unchanged, so use absolute paths for file Tools and set "
        f"`workdir` to '{project_path}' on every `{SHELL_MODEL_NAME}` call."
    )
    sections = [preamble]
    sections.extend(section for section in (rendered_files, rendered_skills) if section.strip())
    return "\n\n".join(sections)


def _normalize_project_arguments(arguments: Any) -> Any:
    return normalize_call_arguments(_PROJECT_CONTRACT, arguments, field_aliases=_FIELD_ALIASES)


def _project_not_found_message(projects: ProjectStore, project_id: str) -> str:
    try:
        registered = sorted(projects.list(), key=lambda project: project.project_id)
    except ProjectError, OSError:
        _LOGGER.warning("Failed to list Projects for a project_not_found message", exc_info=True)
        registered = []
    if not registered:
        return _NO_PROJECTS_MESSAGE_TEMPLATE.format(project_id=project_id)
    choices = [
        f"{project.project_id} ({_single_line(project.display_name)})"
        for project in registered[:_PROJECT_CHOICE_LIMIT]
    ]
    if len(registered) > _PROJECT_CHOICE_LIMIT:
        choices.append(
            f"and {len(registered) - _PROJECT_CHOICE_LIMIT} more under Registered Projects"
        )
    return _PROJECT_NOT_FOUND_MESSAGE_TEMPLATE.format(
        project_id=project_id, choices=", ".join(choices)
    )


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
