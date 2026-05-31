"""Skill management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def skill_list(instance: ServerInstance) -> CommandResult:
    """Return formatted skill list output from `skill.list` RPC."""

    payload = _rpc_call(instance, "skill.list", {})
    if not payload.ok:
        return payload.to_command_result()
    skills = payload.data.get("skills")
    invalid = payload.data.get("invalid_skills")
    if not isinstance(skills, list):
        return CommandResult(ok=False, message="RPC result missing skills list", instance=instance)
    return CommandResult(
        ok=True,
        message=_format_skill_output(skills, invalid or []),
        instance=instance,
    )


def _format_skill_output(skills: Sequence[object], invalid_skills: Sequence[object]) -> str:
    parsed_invalid = invalid_skills if isinstance(invalid_skills, list) else []
    if not skills and not parsed_invalid:
        return "no skills configured"

    lines: list[str] = []
    if skills:
        lines.append("skills:")
        for skill in skills:
            lines.append(_format_skill_row(skill))

    if parsed_invalid:
        lines.append("")
        lines.append("invalid skills:")
        for diagnostic in parsed_invalid:
            lines.append(_format_invalid_skill_row(diagnostic))

    return "\n".join(lines)


def _format_skill_row(skill: object) -> str:
    if not isinstance(skill, dict):
        return "- ?  ?"

    name = _string_or_default(skill.get("name"), "?")
    description = _string_or_default(skill.get("description"), "?")
    suffix = _format_requirement_suffix(skill)
    return f"- {name}  {description}{suffix}"


def _format_requirement_suffix(skill: Mapping[str, Any]) -> str:
    state = _string_or_default(skill.get("state"), "available")
    requirements = skill.get("requirements")
    if not isinstance(requirements, dict):
        requirements = {}

    missing = _string_list(requirements.get("missing"))
    optional_missing = _string_list(requirements.get("optional_missing"))
    parts: list[str] = []
    if state != "available":
        detail = "; ".join(missing) if missing else state
        parts.append(f"{state}: {detail}")
    if optional_missing:
        parts.append(f"optional missing: {'; '.join(optional_missing)}")
    if not parts:
        return ""
    return f" ({'; '.join(parts)})"


def _format_invalid_skill_row(diagnostic: object) -> str:
    if not isinstance(diagnostic, dict):
        return "- ? (?): unknown error"

    name = _string_or_default(diagnostic.get("name"), "?")
    path = _string_or_default(diagnostic.get("path"), "?")
    warning = _first_warning(diagnostic.get("warnings"))
    return f"- {name} ({path}): {warning}"


def _first_warning(warnings: object) -> str:
    if isinstance(warnings, list) and warnings:
        first = warnings[0]
        if isinstance(first, str) and first:
            return first
    return "unknown error"


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _string_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default
