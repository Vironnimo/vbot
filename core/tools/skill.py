"""Internal skill activation tool."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from pathlib import Path
from typing import Any

from core.skills.skills import FRONT_MATTER_DELIMITER, SkillRegistry, _scan_skill_resources
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)

SKILL_TOOL_NAME = "skill"
SKILL_TOOL_DESCRIPTION = (
    "Load an allowed skill by name and add its instructions to session context."
)
SKILL_STATUS_LOADED = "loaded"
SKILL_STATUS_ALREADY_ACTIVE = "already_active"
SKILL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Name of the skill to activate from the available skills catalog.",
        }
    },
    "required": ["name"],
    "additionalProperties": False,
}


def make_skill_handler(skill_registry: SkillRegistry) -> Any:
    """Return a skill handler bound to a skill registry."""

    def skill_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        skill_name = arguments.get("name")
        if not isinstance(skill_name, str) or not skill_name:
            return tool_failure("invalid_arguments", "name must be a non-empty string")

        unknown_arguments = set(arguments) - {"name"}
        if unknown_arguments:
            names = ", ".join(sorted(unknown_arguments))
            return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

        try:
            skill = skill_registry.get(skill_name)
        except KeyError:
            return tool_failure("skill_not_found", f"Skill not found: {skill_name}")

        if not _is_skill_allowed(skill_registry, skill_name, context.allowed_skills):
            return tool_failure(
                "skill_not_found",
                f"Skill not found or not allowed for this agent: {skill_name}",
            )

        unavailable_message = _unavailable_skill_message(
            skill_registry,
            skill_name,
            context.allowed_skills,
        )
        if unavailable_message is not None:
            return tool_failure("skill_unavailable", unavailable_message)

        try:
            data = load_skill_content(skill_name, skill.path)
        except OSError as error:
            return tool_failure(
                "skill_read_error",
                f"Failed to read skill '{skill_name}': {error}",
            )
        except ValueError as error:
            return tool_failure("skill_read_error", str(error))

        stored_result = context.activate_skill(skill_name, data)
        if stored_result is not None:
            return _skill_activation_result(skill_name, stored_result, data)
        return _minimal_skill_result(skill_name, data, already_active=False)

    return skill_handler


def register_skill_tool(registry: ToolRegistry, skill_registry: SkillRegistry) -> None:
    """Register the internal skill activation tool."""
    registry.register(
        SKILL_TOOL_NAME,
        SKILL_TOOL_DESCRIPTION,
        SKILL_TOOL_PARAMETERS,
        make_skill_handler(skill_registry),
        internal=True,
        display=ToolDisplay(summary_fields=("name",)),
    )


def load_skill_content(skill_name: str, skill_file: Path) -> JsonObject:
    """Load and wrap activation content for one skill file."""
    body = _read_skill_body(skill_file)
    resources = _scan_skill_resources(skill_file.parent)
    return {"content": _wrap_skill_content(skill_name, body, resources), "resources": resources}


def _skill_activation_result(
    skill_name: str,
    stored_result: JsonObject,
    data: JsonObject,
) -> JsonObject:
    if stored_result.get("ok") is not True:
        return stored_result

    stored_data = stored_result.get("data")
    already_active = isinstance(stored_data, dict) and stored_data.get("already_active") is True
    return _minimal_skill_result(skill_name, data, already_active=already_active)


def _minimal_skill_result(
    skill_name: str,
    data: JsonObject,
    *,
    already_active: bool,
) -> JsonObject:
    resources = data.get("resources", [])
    if not isinstance(resources, list):
        resources = []

    status = SKILL_STATUS_ALREADY_ACTIVE if already_active else SKILL_STATUS_LOADED
    message = (
        f"Skill '{skill_name}' was already active in this session."
        if already_active
        else f"Skill '{skill_name}' loaded into session context."
    )
    return tool_success(
        {
            "name": skill_name,
            "status": status,
            "message": message,
            "resources": list(resources),
        }
    )


def _allowed_skill_names(
    skill_registry: SkillRegistry,
    allowed_skills: Sequence[str] | None,
) -> set[str]:
    allowed = ["*"] if allowed_skills is None else list(allowed_skills)
    return {skill.name for skill in skill_registry.filter_allowed(allowed)}


def _is_skill_allowed(
    skill_registry: SkillRegistry,
    skill_name: str,
    allowed_skills: Sequence[str] | None,
) -> bool:
    is_allowed = getattr(skill_registry, "is_allowed", None)
    if callable(is_allowed):
        return bool(is_allowed(skill_name, allowed_skills))
    return skill_name in _allowed_skill_names(skill_registry, allowed_skills)


def _unavailable_skill_message(
    skill_registry: SkillRegistry,
    skill_name: str,
    allowed_skills: Sequence[str] | None,
) -> str | None:
    availability_for = getattr(skill_registry, "availability_for", None)
    if not callable(availability_for):
        return None

    availability = availability_for(skill_name, allowed_skills)
    if getattr(availability, "state", "available") == "available":
        return None
    missing = list(getattr(availability, "missing", ()))
    detail = "; ".join(missing) if missing else str(getattr(availability, "state", "unavailable"))
    return f"Skill '{skill_name}' is unavailable: {detail}"


def _read_skill_body(skill_file: Path) -> str:
    content = skill_file.read_text(encoding="utf-8")
    lines = content.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
        raise ValueError(f"Skill metadata missing front matter: {skill_file}")

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == FRONT_MATTER_DELIMITER:
            return "\n".join(lines[index + 1 :]).strip()

    raise ValueError(f"Skill metadata front matter is not closed: {skill_file}")


def _wrap_skill_content(skill_name: str, body: str, resources: list[str]) -> str:
    lines = [f'<skill_content name="{escape(skill_name, quote=True)}">']
    if resources:
        lines.append("<resources>")
        lines.extend(f"- {resource}" for resource in resources)
        lines.append("</resources>")
    if body:
        lines.append(body)
    lines.append("</skill_content>")
    return "\n".join(lines)


__all__ = [
    "SKILL_TOOL_DESCRIPTION",
    "SKILL_TOOL_NAME",
    "SKILL_TOOL_PARAMETERS",
    "make_skill_handler",
    "load_skill_content",
    "register_skill_tool",
]
