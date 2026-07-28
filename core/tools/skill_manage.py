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
from core.tools.arguments import (
    ToolArgumentError,
    optional_bool,
    optional_string,
    required_string,
)
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
    "Create, edit, patch, or delete one writable vBot Skill, or write/remove one "
    "UTF-8 support file. Set action to create, edit, patch, write_file, remove_file, "
    "or delete. Omit scope for your private Skill home; use scope='global' only when "
    "the user explicitly requested a Skill shared by all Agents. Project and bundled "
    "Skills are read-only here; use the skill tool to list or read Skills."
)

_ACTIONS = ("create", "edit", "patch", "write_file", "remove_file", "delete")
_OWN_SCOPE = "own"
_GLOBAL_SCOPE = "global"
_SCOPES = (_OWN_SCOPE, _GLOBAL_SCOPE)
_LOGGER = get_logger("tools.skill_manage")

_ACTION_FIELDS: dict[str, frozenset[str]] = {
    "create": frozenset({"action", "name", "scope", "content"}),
    "edit": frozenset({"action", "name", "scope", "content"}),
    "patch": frozenset(
        {
            "action",
            "name",
            "scope",
            "file_path",
            "old_string",
            "new_string",
            "replace_all",
        }
    ),
    "write_file": frozenset({"action", "name", "scope", "file_path", "file_content"}),
    "remove_file": frozenset({"action", "name", "scope", "file_path"}),
    "delete": frozenset({"action", "name", "scope"}),
}

SKILL_MANAGE_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ACTIONS),
            "description": "The Skill mutation to perform.",
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_SKILL_NAME_LENGTH,
            "pattern": f"^{SKILL_NAME_CHARSET_FRAGMENT}$",
            "description": (
                "Skill directory and front-matter name. It must start with a letter "
                "or digit and otherwise use only letters, digits, '-' or '_'."
            ),
        },
        "scope": {
            "type": "string",
            "enum": list(_SCOPES),
            "default": _OWN_SCOPE,
            "description": (
                "Writable Skill home. Omit for your private home. Use 'global' only "
                "when the user explicitly requested a shared Skill."
            ),
        },
        "content": {
            "type": "string",
            "description": "Complete SKILL.md content for create or edit.",
        },
        "file_path": {
            "type": "string",
            "minLength": 1,
            "pattern": r"^(SKILL\.md|(?:scripts|references|assets)/.+)$",
            "description": (
                "Skill-relative file path. patch defaults to SKILL.md; write_file "
                "and remove_file require a path below scripts/, references/, or assets/."
            ),
        },
        "file_content": {
            "type": "string",
            "description": "Complete UTF-8 text for write_file.",
        },
        "old_string": {
            "type": "string",
            "minLength": 1,
            "description": "Exact text to replace with patch.",
        },
        "new_string": {
            "type": "string",
            "description": "Replacement text for patch; may be empty.",
        },
        "replace_all": {
            "type": "boolean",
            "default": False,
            "description": ("Replace every match. Omit or set false to require exactly one match."),
        },
    },
    "required": ["action", "name"],
    "additionalProperties": False,
}


def make_skill_manage_handler(
    authoring: SkillAuthoringService,
    resolve_agent_skills_dir: Callable[[str], Path],
    invalidate_agent_skills: Callable[[str], None],
    resolve_global_skills_dir: Callable[[], Path],
    reload_skills: Callable[[], None],
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
            scope = optional_string(arguments.get("scope"), field_name="scope") or _OWN_SCOPE
            if scope not in _SCOPES:
                raise ToolArgumentError(f"scope must be one of: {', '.join(_SCOPES)}")
            target_root = (
                resolve_global_skills_dir()
                if scope == _GLOBAL_SCOPE
                else resolve_agent_skills_dir(context.agent_id)
            )
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

        if scope == _GLOBAL_SCOPE:
            reload_skills()
        else:
            invalidate_agent_skills(context.agent_id)
        _LOGGER.info(
            "Skill mutated (skill=%s scope=%s action=%s actor_agent=%s)",
            result.name,
            scope,
            action,
            context.agent_id,
        )
        data: JsonObject = {
            "action": action,
            "name": result.name,
            "scope": scope,
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
        old_string = required_string(
            arguments.get("old_string"),
            field_name="old_string",
            strip=False,
        )
        new_string = _exact_string(arguments.get("new_string"), field_name="new_string")
        replace_all = optional_bool(
            arguments.get("replace_all"),
            field_name="replace_all",
            default=False,
        )
        result = authoring.patch(
            target_root,
            name,
            old_string,
            new_string,
            author="agent",
            relative_path=file_path,
            replace_all=replace_all,
        )
        return result, file_path.replace("\\", "/")
    if action == "write_file":
        file_path = required_string(arguments.get("file_path"), field_name="file_path")
        file_content = _exact_string(
            arguments.get("file_content"),
            field_name="file_content",
        )
        result = authoring.write_file(target_root, name, file_path, file_content)
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
    resolve_global_skills_dir: Callable[[], Path],
    reload_skills: Callable[[], None],
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
            resolve_global_skills_dir,
            reload_skills,
        ),
        result_schema={"type": "object", "required": ["scope"]},
        display=ToolDisplay(summary_builder=_skill_manage_display_summary),
    )


def _skill_manage_display_summary(arguments: JsonObject) -> str | None:
    parts: list[str] = []
    for field in ("action", "name", "scope"):
        value = arguments.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return " · ".join(parts) or None


__all__ = [
    "SKILL_MANAGE_TOOL_DESCRIPTION",
    "SKILL_MANAGE_TOOL_NAME",
    "SKILL_MANAGE_TOOL_PARAMETERS",
    "make_skill_manage_handler",
    "register_skill_manage_tool",
]
