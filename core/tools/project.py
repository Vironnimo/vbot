"""Explicit Project Context loading for Identity Agents."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import stat
from collections.abc import Callable, Sequence
from difflib import get_close_matches
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from core.projects import (
    InvalidProjectIdError,
    Project,
    ProjectError,
    ProjectNotFoundError,
    ProjectStore,
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
    ToolDefinitionProfile,
    ToolDefinitionProfileContext,
    ToolDisplay,
    ToolDisplayField,
    ToolRegistry,
    display_notice,
    display_text,
    offload_tool_handler,
    tool_failure,
    tool_success,
)
from core.utils.file_status import stat_or_none
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger
from core.utils.paths import model_path

if TYPE_CHECKING:
    from core.prompts.blocks import PromptFileIssue

_LOGGER = get_logger("tools.project")
PROJECT_TOOL_NAME = "project"
PROJECT_TOOL_DESCRIPTION = (
    "Load a registered Project's instructions, path, and Skill catalog before working in it, "
    "unless it is already your Working Project. Wait for the result before dependent Tool "
    "calls; your working directory stays unchanged."
)
PROJECT_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "project_id": {"type": "string", "description": "ID of the Project to load."},
    },
    "required": ["project_id"],
}
_NO_LOAD = "No new Project Context was loaded."
_FIELD_ALIASES = SpellingAliases({"project_id": ("project", "id", "name", "project_name")})
_PROJECT_CONTRACT = compile_tool_contract(
    name=PROJECT_TOOL_NAME, input_schema=PROJECT_TOOL_PARAMETERS, require_closed_input=False
)
_TRANSIENT_ERRNOS = {
    errno.EAGAIN,
    errno.EBUSY,
    errno.EINTR,
    errno.EIO,
    errno.ETIMEDOUT,
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.ECONNRESET,
    errno.ECONNABORTED,
}


class ProjectContextRenderer(Protocol):
    """The shared file and Skill rendering supplied by the prompt owner."""

    def render_project_files(
        self,
        project_context: Any,
        *,
        tool_available: Callable[[str], bool],
        on_read: Callable[[Path], None] | None = None,
        on_issue: Callable[[PromptFileIssue], None] | None = None,
    ) -> str: ...

    def render_project_skills(
        self, project_name: str, skills: Sequence[Any], *, tool_available: Callable[[str], bool]
    ) -> str: ...


ProjectContextRendererResolver = Callable[[], ProjectContextRenderer]
ProjectSkillsResolver = Callable[[str], list[Any]]


def make_project_handler(
    projects: ProjectStore,
    get_renderer: ProjectContextRendererResolver,
    list_project_skills: ProjectSkillsResolver,
    file_state: FileReadState,
) -> Any:
    """Create the loader; only a successful persisted result activates its context."""

    def load(context: ToolContext, project: Project) -> JsonObject:
        from core.prompts import ProjectPromptContext

        project_path = model_path(project.cwd)
        status = stat_or_none(project.cwd)
        if status is None:
            return tool_failure(
                "project_path_missing",
                f"{_NO_LOAD} Project '{project.project_id}' points to a missing directory: "
                f"{project_path}. Restore the directory or update the Project's path in vBot, "
                "then load this Project again.",
                retryable=False,
            )
        if not stat.S_ISDIR(status.st_mode):
            return tool_failure(
                "project_path_not_directory",
                f"{_NO_LOAD} Project '{project.project_id}' points to a file, not a directory: "
                f"{project_path}. Update the Project's path to its repository directory in "
                "vBot, then load this Project again.",
                retryable=False,
            )

        renderer = get_renderer()
        read_paths: list[Path] = []
        issues: list[PromptFileIssue] = []
        rendered_files = renderer.render_project_files(
            ProjectPromptContext.from_project(
                project.project_id, project.display_name, project.cwd, project.instruction_files
            ),
            tool_available=context.can_call,
            on_read=read_paths.append,
            on_issue=issues.append,
        )
        # AGENTS.md is seeded even in a bare repository. Its absence is normal.
        issues = [
            issue
            for issue in issues
            if not (issue.reason == "missing" and issue.name == "AGENTS.md")
        ]
        skills = list_project_skills(project.project_id)
        rendered_skills = renderer.render_project_skills(
            project.display_name, skills, tool_available=context.can_call
        )
        content = _render_project_context(
            context, project, bool(read_paths), rendered_files, rendered_skills
        )
        data: JsonObject = {
            "status": "loaded",
            "project_id": project.project_id,
            "project_path": project_path,
            "content": content,
        }
        if issues:
            notices = [_file_issue_text(issue) for issue in issues if issue.reason != "too_large"]
            # Oversized files already carry their size and reading hint in content.
            data["note"] = "Some Project instruction files were not included." + (
                "\n" + "\n".join(notices) if notices else " See the file notices below."
            )
        for path in read_paths:
            file_state.record_read(context.session_id, path)
        return tool_success(data)

    def project_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        if context.project_id is not None:
            return tool_failure(
                "project_identity_required",
                f"{_NO_LOAD} This Tool loads context for Identity Agents. Use the context "
                "of your Working Project; to work in another Project, ask the user to start "
                "a Session there.",
                retryable=False,
            )
        try:
            project_id = required_string(arguments.get("project_id"), field_name="project_id")
        except ValueError:
            return tool_failure(
                "invalid_arguments",
                f"{_NO_LOAD} project_id is empty. Copy an id from the available Projects in "
                "project_id's description and pass it as project_id.",
                retryable=False,
            )
        subject = f"Project '{project_id}'"
        try:
            if PROJECT_ID_PATTERN.fullmatch(project_id) is None:
                raise ProjectNotFoundError(project_id)
            project = projects.get(project_id)
            subject += f" at {model_path(project.cwd)}"
            return load(context, project)
        except InvalidProjectIdError, ProjectNotFoundError:
            return tool_failure(
                "project_not_found",
                _project_not_found_message(projects, project_id),
                retryable=False,
            )
        except (ProjectError, OSError) as error:
            _LOGGER.warning("Project context load failed (project=%s)", project_id, exc_info=True)
            return _load_failure(error, subject)
        except Exception:
            _LOGGER.exception("Project context load failed unexpectedly (project=%s)", project_id)
            return tool_failure(
                "project_load_failed",
                f"{_NO_LOAD} {subject} could not be loaded because of an internal error. "
                "Retry this load once; if it fails again, report the failure to the user "
                "so the vBot logs can be inspected.",
                retryable=True,
            )

    return project_handler


def _load_failure(error: Exception, subject: str) -> JsonObject:
    cause: BaseException | None = error
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, OSError):
            transient = cause.errno in _TRANSIENT_ERRNOS or getattr(cause, "winerror", None) in {
                32,
                33,
                53,
                64,
                121,
                1231,
            }
            if isinstance(cause, PermissionError) and not transient:
                return tool_failure(
                    "project_access_denied",
                    f"{_NO_LOAD} Access to {subject} was denied. Restore read access for "
                    "the vBot server, then load this Project again.",
                    retryable=False,
                )
            remedy = (
                "Retry this load once after the storage or connection is available; if it "
                "fails again, report the failure to the user."
                if transient
                else "Restore access to the Project's storage, then load this Project again."
            )
            return tool_failure(
                "project_io_error",
                f"{_NO_LOAD} {subject} could not be read. {remedy}",
                retryable=transient,
            )
        cause = cause.__cause__
    return tool_failure(
        "project_configuration_invalid",
        f"{_NO_LOAD} {subject} has invalid or incompatible Project configuration. "
        "Repair or restore its Project configuration in vBot, then load the same id again.",
        retryable=False,
    )


def _file_issue_text(issue: PromptFileIssue) -> str:
    problem, remedy = {
        "missing": (
            "does not exist",
            "Restore it or remove it from the Project's instruction files, "
            "then reload the Project.",
        ),
        "access_denied": (
            "could not be read because access was denied",
            "Restore read access for the vBot server, then reload the Project.",
        ),
        "not_file": (
            "is not a readable file",
            "Select an instruction file in the Project configuration, then reload the Project.",
        ),
        "encoding": (
            "is not valid UTF-8 text",
            "Restore the instructions as UTF-8 text, then reload the Project.",
        ),
        "unreadable": (
            "could not be read",
            "Check the file's availability and read access, then reload the Project.",
        ),
        "too_large": ("exceeds the automatic loading limit", "See its file notice below."),
    }[issue.reason]
    return f"Not included: {model_path(issue.path)} {problem}. {remedy}"


def _project_detail_blocks(arguments: JsonObject, result: JsonObject | None) -> list[JsonObject]:
    data = result.get("data") if isinstance(result, dict) and result.get("ok") is True else None
    if not isinstance(data, dict):
        return []
    path = data.get("project_path")
    return [
        display_notice(
            "info", "Project Context loaded.", subject=path if isinstance(path, str) else None
        ),
        *([display_notice("warning", data["note"])] if data.get("note") else []),
        display_text("content", source="result", path=("data", "content")),
    ]


def register_project_tool(
    registry: ToolRegistry,
    projects: ProjectStore,
    get_renderer: ProjectContextRendererResolver,
    list_project_skills: ProjectSkillsResolver,
    file_state: FileReadState,
) -> None:
    """Register the loader with its Project choices in the Tool definition."""
    conditions = LoggedConditions()

    def profile(_context: ToolDefinitionProfileContext) -> ToolDefinitionProfile:
        try:
            registered = projects.list()
        except Exception:
            if conditions.started("catalog", "unavailable"):
                _LOGGER.warning("Project Tool catalog could not be read", exc_info=True)
            listing = (
                "The Project list is unavailable. Pass a known exact Project id, or ask the "
                "user to restore access to the Project configuration."
            )
        else:
            if conditions.ended("catalog"):
                _LOGGER.info("Project Tool catalog became readable again")
            listing = (
                "Available Projects:\n"
                + "\n".join(_project_choice(project) for project in registered)
                if registered
                else "No loadable Projects are available."
            )
        parameters = copy.deepcopy(PROJECT_TOOL_PARAMETERS)
        parameters["properties"]["project_id"]["description"] += " " + listing
        return ToolDefinitionProfile(
            key=hashlib.sha256(listing.encode("utf-8")).hexdigest(),
            description=PROJECT_TOOL_DESCRIPTION,
            parameters=parameters,
        )

    registry.register(
        PROJECT_TOOL_NAME,
        PROJECT_TOOL_DESCRIPTION,
        PROJECT_TOOL_PARAMETERS,
        offload_tool_handler(
            make_project_handler(projects, get_renderer, list_project_skills, file_state)
        ),
        summary="Load context for a registered Project.",
        constraints=("identity_agent",),
        open_input_schema=True,
        argument_normalizer=_normalize_project_arguments,
        definition_profile_resolver=profile,
        announce_changes=("parameters.project_id",),
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


def _project_choice(project: Project) -> str:
    name = " ".join(project.display_name.split())
    return f"- {project.project_id}" + (f": {name}" if name != project.project_id else "")


def _render_project_context(
    context: ToolContext,
    project: Project,
    has_instructions: bool,
    rendered_files: str,
    rendered_skills: str,
) -> str:
    preamble = f"Project Context loaded for '{project.display_name}'."
    if has_instructions:
        preamble += " Follow the files below when working in this Project."
    if any(
        context.can_call(name) for name in ("read", "search_files", "edit", "write", "apply_patch")
    ):
        preamble += " For file work in this Project, use absolute file paths."
    if context.can_call("bash"):
        preamble += (
            " For shell work in this Project, set `workdir` to `project_path` "
            f"on each `{SHELL_MODEL_NAME}` call."
        )
    if context.can_call("skill") and context.skill_project_id != project.project_id:
        preamble += " Project Skills now resolve from this Project."
        if context.skill_project_id is not None:
            preamble += " This replaces the previous Project Skill context."
    return "\n\n".join(
        section for section in (preamble, rendered_files, rendered_skills) if section.strip()
    )


def _normalize_project_arguments(arguments: Any) -> Any:
    return normalize_call_arguments(_PROJECT_CONTRACT, arguments, field_aliases=_FIELD_ALIASES)


def _project_not_found_message(projects: ProjectStore, project_id: str) -> str:
    start = f"{_NO_LOAD} Project id {json.dumps(project_id, ensure_ascii=False)} was not found."
    try:
        registered = projects.list()
    except Exception:
        _LOGGER.warning("Project choices could not be read after an unknown id", exc_info=True)
        return (
            f"{start} The available Project ids could not be read. Restore access to the "
            "Project configuration, then use an exact registered id."
        )
    if not registered:
        return (
            f"{start} No loadable Projects are available. Register or restore the intended "
            "Project in vBot before loading it."
        )
    by_label: dict[str, set[str]] = {}
    for project in registered:
        for label in (project.project_id, project.display_name):
            by_label.setdefault(label.casefold(), set()).add(project.project_id)
    matches = get_close_matches(project_id.casefold(), list(by_label), n=3, cutoff=0.5)
    matches.extend(word for word in project_id.casefold().split() if word in by_label)
    selected_ids = {project_id for match in matches for project_id in by_label[match]}
    choices = [project for project in registered if project.project_id in selected_ids][
        :3
    ] or registered[:3]
    listing = "\n".join(_project_choice(project) for project in choices)
    more = (
        "\nOther choices are in project_id's description." if len(registered) > len(choices) else ""
    )
    return (
        f"{start} Choose the intended Project and call project with its exact id "
        f"as project_id:\n{listing}{more}"
    )


__all__ = [
    "PROJECT_TOOL_DESCRIPTION",
    "PROJECT_TOOL_NAME",
    "PROJECT_TOOL_PARAMETERS",
    "make_project_handler",
    "register_project_tool",
]
