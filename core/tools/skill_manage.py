"""Direct authoring tool for an Identity Agent's writable vBot Skills."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from core.skills.authoring import (
    SkillAuthoringError,
    SkillAuthoringService,
    SkillWriteResult,
)
from core.skills.skill_validator import (
    MAX_SKILL_NAME_LENGTH,
    SKILL_NAME_CHARSET_FRAGMENT,
)
from core.tools.arguments import ToolArgumentError, optional_string, required_string
from core.tools.availability import SKILL_MANAGE_TOOL_NAME
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

SKILL_MANAGE_TOOL_DESCRIPTION = (
    "Create, edit, patch, or delete one private Skill, or write/remove one UTF-8 support "
    "file. Read an existing target with skill before changing it. content is the complete "
    "text for create, edit, and write_file, and the replacement text for patch; match is "
    "the exact text patch replaces. Global, Project, and bundled Skills are read-only here."
)

_ACTIONS = ("create", "edit", "patch", "write_file", "remove_file", "delete")
_LOGGER = get_logger("tools.skill_manage")

_ACTION_FIELDS: dict[str, frozenset[str]] = {
    "create": frozenset({"action", "name", "content"}),
    "edit": frozenset({"action", "name", "content"}),
    "patch": frozenset({"action", "name", "file_path", "match", "content"}),
    "write_file": frozenset({"action", "name", "file_path", "content"}),
    "remove_file": frozenset({"action", "name", "file_path"}),
    "delete": frozenset({"action", "name"}),
}

_NAME_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "maxLength": MAX_SKILL_NAME_LENGTH,
    "pattern": f"^{SKILL_NAME_CHARSET_FRAGMENT}$",
    "description": (
        "Skill directory and front-matter name. It must start with a letter or digit "
        "and otherwise use only letters, digits, '-' or '_'."
    ),
}
_CONTENT_PARAMETER: JsonObject = {
    "type": "string",
    "description": (
        "Text to write. Complete SKILL.md for create/edit; replacement text for patch; "
        "complete UTF-8 file for write_file. May be empty for patch or write_file."
    ),
}
_FILE_PATH_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "pattern": r"^(SKILL\.md|(?:scripts|references|assets)/.+)$",
    "description": (
        "Skill-relative target. Omit when patching SKILL.md; required when patching, "
        "writing, or removing a support file under scripts, references, or assets."
    ),
}
_MATCH_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Exact non-empty text to replace once for patch. Use a larger unique passage when "
        "the short text occurs more than once."
    ),
}

SKILL_MANAGE_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ACTIONS),
            "description": (
                "Operation to perform: create/edit use content as complete SKILL.md; patch "
                "replaces match with content; write_file uses file_path and content; "
                "remove_file/delete need no content."
            ),
        },
        "name": _NAME_PARAMETER,
        "content": _CONTENT_PARAMETER,
        "file_path": _FILE_PATH_PARAMETER,
        "match": _MATCH_PARAMETER,
    },
    "required": ["action", "name"],
}


def make_skill_manage_handler(
    authoring: SkillAuthoringService,
    resolve_agent_skills_dir: Callable[[str], Path],
    invalidate_agent_skills: Callable[[str], None],
) -> Callable[[ToolContext, JsonObject], JsonObject]:
    """Return the direct Skill-management handler."""

    def skill_manage_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        try:
            action = required_string(arguments.get("action"), field_name="action")
            if action not in _ACTIONS:
                raise ToolArgumentError(f"action must be one of: {', '.join(_ACTIONS)}")
            unexpected = set(arguments) - _ACTION_FIELDS[action]
            if unexpected:
                names = ", ".join(sorted(unexpected))
                raise ToolArgumentError(f"Unknown {action} argument(s): {names}")

            name = required_string(arguments.get("name"), field_name="name")
            target_root = resolve_agent_skills_dir(context.agent_id)
            result, file_path = _apply_action(
                authoring,
                target_root,
                action,
                name,
                arguments,
            )
        except ToolArgumentError as error:
            return tool_failure("invalid_arguments", str(error), retryable=False)
        except SkillAuthoringError as error:
            return tool_failure(
                "skill_write_rejected",
                "; ".join(error.diagnostics),
                retryable=False,
            )
        except OSError as error:
            return tool_failure("skill_write_error", str(error))

        invalidate_agent_skills(context.agent_id)
        _LOGGER.info(
            "Skill mutated (skill=%s scope=own action=%s actor_agent=%s)",
            result.name,
            action,
            context.agent_id,
        )
        data: JsonObject = {
            "action": action,
            "name": result.name,
            "scope": "own",
            "warnings": list(result.warnings),
            "message": _success_message(action, result.name),
        }
        if file_path is not None:
            data["file_path"] = file_path
        return tool_success(data)

    return skill_manage_handler


def _apply_action(
    authoring: SkillAuthoringService,
    target_root: Path,
    action: str,
    name: str,
    arguments: JsonObject,
) -> tuple[SkillWriteResult, str | None]:
    if action == "create":
        content = _exact_string(arguments.get("content"), field_name="content")
        return authoring.create(target_root, name, content, author="agent"), "SKILL.md"
    if action == "edit":
        content = _exact_string(arguments.get("content"), field_name="content")
        return authoring.edit(target_root, name, content, author="agent"), "SKILL.md"
    if action == "patch":
        file_path = (
            optional_string(arguments.get("file_path"), field_name="file_path") or "SKILL.md"
        )
        match = required_string(
            arguments.get("match"),
            field_name="match",
            strip=False,
        )
        content = _exact_string(arguments.get("content"), field_name="content")
        result = authoring.patch(
            target_root,
            name,
            match,
            content,
            author="agent",
            relative_path=file_path,
        )
        return result, file_path.replace("\\", "/")
    if action == "write_file":
        file_path = required_string(arguments.get("file_path"), field_name="file_path")
        content = _exact_string(arguments.get("content"), field_name="content")
        result = authoring.write_file(target_root, name, file_path, content)
        return result, file_path.replace("\\", "/")
    if action == "remove_file":
        file_path = required_string(arguments.get("file_path"), field_name="file_path")
        result = authoring.remove_file(target_root, name, file_path)
        return result, file_path.replace("\\", "/")
    return authoring.delete(target_root, name), None


def _exact_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ToolArgumentError(f"{field_name} must be a string")
    return value


def _success_message(action: str, name: str) -> str:
    verbs = {
        "create": "created",
        "edit": "updated",
        "patch": "patched",
        "write_file": "file written",
        "remove_file": "file removed",
        "delete": "deleted",
    }
    return f"Skill '{name}' {verbs[action]}."


def register_skill_manage_tool(
    registry: ToolRegistry,
    authoring: SkillAuthoringService,
    resolve_agent_skills_dir: Callable[[str], Path],
    invalidate_agent_skills: Callable[[str], None],
) -> None:
    """Register identity-only direct Skill management."""
    registry.register(
        SKILL_MANAGE_TOOL_NAME,
        SKILL_MANAGE_TOOL_DESCRIPTION,
        SKILL_MANAGE_TOOL_PARAMETERS,
        make_skill_manage_handler(
            authoring,
            resolve_agent_skills_dir,
            invalidate_agent_skills,
        ),
        family="skills",
        constraints=("identity_agent",),
        open_input_schema=True,
        result_schema={"type": "object", "required": ["scope"]},
        display=ToolDisplay(parts_builder=_skill_manage_display_parts),
    )


def _skill_manage_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    action = arguments.get("action")
    if not isinstance(action, str) or not action.strip():
        return ()
    parts = [ToolDisplayPart(action.strip(), truncate="never", tooltip="none")]
    value = arguments.get("name")
    if isinstance(value, str) and value.strip():
        parts.append(ToolDisplayPart(value.strip()))
    return tuple(parts)


__all__ = [
    "SKILL_MANAGE_TOOL_DESCRIPTION",
    "SKILL_MANAGE_TOOL_NAME",
    "SKILL_MANAGE_TOOL_PARAMETERS",
    "make_skill_manage_handler",
    "register_skill_manage_tool",
]
