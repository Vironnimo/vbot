"""Tool for an identity agent to author skills in its own private home or the global pool.

Unlike the ``skill`` activation tool, ``skill_manage`` *writes*. It is the agent's
single seam onto the shared skill authoring write core. A ``scope`` argument chooses
the target: ``own`` (default) writes the agent's own home
``<data_dir>/agents/<agent_id>/skills/``; ``global`` writes the shared user pool
``<data_dir>/skills/``. The default is private, and the tool instructs the agent to
target ``global`` only when the user explicitly asked to make the skill global — a
guideline, not a hard gate (a global write is reversible and visible in the WebUI
skill editor). The project/repo scope is never a target here; repo skills are
authored with the ordinary file tools.

A normal allow-list tool that can be toggled per agent, but **identity-only**: it is
offered only to an agent that owns a Workspace (see :data:`IDENTITY_ONLY_TOOLS`), so a
config/project agent never authors skills even under a wildcard allow-list. It is not
gated on the agent already having a skill, so an agent with none can create its first.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from core.skills.authoring import SkillAuthoringError, SkillAuthoringService, SkillWriteResult
from core.tools.arguments import ToolArgumentError, optional_string, required_string
from core.tools.availability import SKILL_MANAGE_TOOL_NAME
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

SKILL_MANAGE_TOOL_DESCRIPTION = (
    "Author skills: create, edit, patch, or delete a skill (and its "
    "scripts/references/assets support files). Defaults to your own private skill "
    "home; pass scope='global' to write the shared global pool ONLY when the user "
    "explicitly asked to make the skill global. New and changed skills become usable "
    "immediately, by name, in the same session."
)

_OPERATIONS = ("create", "edit", "patch", "delete", "write_file", "remove_file")
_KNOWN_FIELDS = frozenset(
    {"operation", "name", "content", "old_string", "new_string", "path", "source", "scope"}
)

_OWN_SCOPE = "own"
_GLOBAL_SCOPE = "global"
_SCOPES = (_OWN_SCOPE, _GLOBAL_SCOPE)
_SCOPE_LOCATIONS = {_OWN_SCOPE: "your private home", _GLOBAL_SCOPE: "the global pool"}
_LOGGER = get_logger("tools.skill_manage")

SKILL_MANAGE_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": list(_OPERATIONS),
            "description": (
                "create / edit (full SKILL.md) / patch (one unique old→new edit) / "
                "delete the skill; write_file / remove_file for a support file."
            ),
        },
        "scope": {
            "type": "string",
            "enum": list(_SCOPES),
            "description": (
                "Where to write. 'own' (default) is your private skill home. "
                "'global' is the shared global pool across all your identity agents — "
                "use it ONLY when the user explicitly asked to make the skill global."
            ),
        },
        "name": {
            "type": "string",
            "description": "The skill name; also its directory. Required for every operation.",
        },
        "content": {
            "type": "string",
            "description": (
                "For create/edit: the full SKILL.md (YAML front matter with name + "
                "description, then the body). For write_file: the support file's content."
            ),
        },
        "old_string": {
            "type": "string",
            "description": "patch: the exact existing text to replace (must be unique).",
        },
        "new_string": {
            "type": "string",
            "description": "patch: the replacement text (may be empty to delete the match).",
        },
        "path": {
            "type": "string",
            "description": (
                "write_file/remove_file: the support-file path under scripts/, "
                "references/, or assets/."
            ),
        },
        "source": {
            "type": "string",
            "description": "Optional: where this skill came from, recorded as provenance.",
        },
    },
    "required": ["operation", "name"],
    "additionalProperties": False,
}


def make_skill_manage_handler(
    authoring: SkillAuthoringService,
    resolve_agent_skills_dir: Callable[[str], Path],
    invalidate_agent_skills: Callable[[str], None],
    resolve_global_skills_dir: Callable[[], Path],
    reload_skills: Callable[[], None],
) -> Callable[[ToolContext, JsonObject], JsonObject]:
    """Return a handler that authors skills into the calling agent's home or the global pool.

    ``resolve_agent_skills_dir`` maps an agent id to its private skill home and
    ``resolve_global_skills_dir`` returns the shared user pool (the runtime owns the
    data-dir layout). After a write, the matching invalidation makes it live in the
    same session: a private write drops that agent's cached registry
    (``invalidate_agent_skills``); a global write reloads the whole skill registry
    (``reload_skills``), since the global pool is layered under every project/agent
    registry.
    """

    def skill_manage_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        unknown_arguments = set(arguments) - _KNOWN_FIELDS
        if unknown_arguments:
            names = ", ".join(sorted(unknown_arguments))
            return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

        operation = arguments.get("operation")
        if not isinstance(operation, str) or operation not in _OPERATIONS:
            allowed = ", ".join(_OPERATIONS)
            return tool_failure("invalid_arguments", f"operation must be one of: {allowed}")

        scope = arguments.get("scope", _OWN_SCOPE)
        if not isinstance(scope, str) or scope not in _SCOPES:
            allowed = ", ".join(_SCOPES)
            return tool_failure("invalid_arguments", f"scope must be one of: {allowed}")

        if scope == _GLOBAL_SCOPE:
            target_root = resolve_global_skills_dir()
        else:
            target_root = resolve_agent_skills_dir(context.agent_id)
        try:
            result = _apply_operation(authoring, target_root, operation, arguments)
        except ToolArgumentError as error:
            return tool_failure("invalid_arguments", str(error))
        except SkillAuthoringError as error:
            return tool_failure("skill_write_rejected", "; ".join(error.diagnostics))
        except OSError as error:
            return tool_failure("skill_write_error", str(error))

        if scope == _GLOBAL_SCOPE:
            reload_skills()
        else:
            invalidate_agent_skills(context.agent_id)
        _LOGGER.info(
            "Skill mutated (skill=%s scope=%s operation=%s actor_agent=%s)",
            result.name,
            scope,
            result.operation,
            context.agent_id,
        )
        return tool_success(
            {
                "name": result.name,
                "operation": result.operation,
                "scope": scope,
                "message": (
                    f"Skill '{result.name}' {result.operation} succeeded in "
                    f"{_SCOPE_LOCATIONS[scope]}."
                ),
                "warnings": list(result.warnings),
            }
        )

    return skill_manage_handler


def _apply_operation(
    authoring: SkillAuthoringService,
    target_root: Path,
    operation: str,
    arguments: JsonObject,
) -> SkillWriteResult:
    name = required_string(arguments.get("name"), field_name="name")
    source = optional_string(arguments.get("source"), field_name="source")

    if operation == "create":
        content = required_string(arguments.get("content"), field_name="content", strip=False)
        return authoring.create(target_root, name, content, author="agent", source=source)
    if operation == "edit":
        content = required_string(arguments.get("content"), field_name="content", strip=False)
        return authoring.edit(target_root, name, content, author="agent", source=source)
    if operation == "patch":
        old_string = required_string(
            arguments.get("old_string"), field_name="old_string", strip=False
        )
        new_string = _exact_string(arguments.get("new_string"), field_name="new_string")
        return authoring.patch(
            target_root, name, old_string, new_string, author="agent", source=source
        )
    if operation == "delete":
        return authoring.delete(target_root, name)
    if operation == "write_file":
        path = required_string(arguments.get("path"), field_name="path")
        content = _exact_string(arguments.get("content"), field_name="content")
        return authoring.write_file(target_root, name, path, content)
    # remove_file — the only remaining validated operation.
    path = required_string(arguments.get("path"), field_name="path")
    return authoring.remove_file(target_root, name, path)


def _exact_string(value: object, *, field_name: str) -> str:
    """Return a string verbatim (may be empty), rejecting non-string values.

    Used where an empty value is meaningful — a patch ``new_string`` that deletes
    the match, or an intentionally empty support file — so blank is kept, not
    treated as omitted.
    """
    if not isinstance(value, str):
        raise ToolArgumentError(f"{field_name} must be a string")
    return value


def register_skill_manage_tool(
    registry: ToolRegistry,
    authoring: SkillAuthoringService,
    resolve_agent_skills_dir: Callable[[str], Path],
    invalidate_agent_skills: Callable[[str], None],
    resolve_global_skills_dir: Callable[[], Path],
    reload_skills: Callable[[], None],
) -> None:
    """Register the private/global skill authoring tool (identity-only, allow-list gated)."""
    registry.register(
        SKILL_MANAGE_TOOL_NAME,
        SKILL_MANAGE_TOOL_DESCRIPTION,
        SKILL_MANAGE_TOOL_PARAMETERS,
        make_skill_manage_handler(
            authoring,
            resolve_agent_skills_dir,
            invalidate_agent_skills,
            resolve_global_skills_dir,
            reload_skills,
        ),
        display=ToolDisplay(summary_fields=("operation", "name", "scope")),
    )


__all__ = [
    "SKILL_MANAGE_TOOL_DESCRIPTION",
    "SKILL_MANAGE_TOOL_NAME",
    "SKILL_MANAGE_TOOL_PARAMETERS",
    "make_skill_manage_handler",
    "register_skill_manage_tool",
]
