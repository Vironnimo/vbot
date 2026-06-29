"""Internal tool for an agent to author skills in its own private home.

Unlike the ``skill`` activation tool, ``skill_manage`` *writes*. It is the agent's
single seam onto the shared skill authoring write core, restricted by construction
to the authoring agent's own home ``<data_dir>/agents/<agent_id>/skills/`` — no
scope parameter, no project or global targets (those are user-only surfaces). It is
always available (``internal=True``, registered unconditionally) and **not** gated
on the agent already having a skill, so an agent with none can create its first.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from core.skills.authoring import SkillAuthoringError, SkillAuthoringService, SkillWriteResult
from core.tools.arguments import ToolArgumentError, optional_string, required_string
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)

SKILL_MANAGE_TOOL_NAME = "skill_manage"
SKILL_MANAGE_TOOL_DESCRIPTION = (
    "Author your own private skills: create, edit, patch, or delete a skill (and its "
    "scripts/references support files) in your personal skill home. New and changed "
    "skills become usable immediately, by name, in the same session."
)

_OPERATIONS = ("create", "edit", "patch", "delete", "write_file", "remove_file")
_KNOWN_FIELDS = frozenset(
    {"operation", "name", "content", "old_string", "new_string", "path", "source"}
)

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
                "write_file/remove_file: the support-file path under scripts/ or references/."
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
) -> Callable[[ToolContext, JsonObject], JsonObject]:
    """Return a handler that authors skills into the calling agent's own home.

    ``resolve_agent_skills_dir`` maps an agent id to its private skill home (the
    runtime owns the data-dir layout); ``invalidate_agent_skills`` drops that
    agent's cached registry so the write is live in the same session.
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

        target_root = resolve_agent_skills_dir(context.agent_id)
        try:
            result = _apply_operation(authoring, target_root, operation, arguments)
        except ToolArgumentError as error:
            return tool_failure("invalid_arguments", str(error))
        except SkillAuthoringError as error:
            return tool_failure("skill_write_rejected", "; ".join(error.diagnostics))
        except OSError as error:
            return tool_failure("skill_write_error", str(error))

        invalidate_agent_skills(context.agent_id)
        return tool_success(
            {
                "name": result.name,
                "operation": result.operation,
                "message": f"Skill '{result.name}' {result.operation} succeeded.",
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
) -> None:
    """Register the internal, home-only skill authoring tool."""
    registry.register(
        SKILL_MANAGE_TOOL_NAME,
        SKILL_MANAGE_TOOL_DESCRIPTION,
        SKILL_MANAGE_TOOL_PARAMETERS,
        make_skill_manage_handler(authoring, resolve_agent_skills_dir, invalidate_agent_skills),
        internal=True,
        display=ToolDisplay(summary_fields=("operation", "name")),
    )


__all__ = [
    "SKILL_MANAGE_TOOL_DESCRIPTION",
    "SKILL_MANAGE_TOOL_NAME",
    "SKILL_MANAGE_TOOL_PARAMETERS",
    "make_skill_manage_handler",
    "register_skill_manage_tool",
]
