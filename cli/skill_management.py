"""Skill management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cli.formatting import string_or_default as _string_or_default
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def list_skills(instance: ServerInstance) -> CommandResult:
    """Return formatted Skill catalog output from `skill.list` RPC."""

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


def skill_read(instance: ServerInstance, scope: str) -> CommandResult:
    """Read editable Skills and their complete SKILL.md content in one scope."""

    payload = _rpc_call(instance, "skill.read", {"scope": scope})
    if not payload.ok:
        return payload.to_command_result()
    skills = payload.data.get("skills")
    if not isinstance(skills, list):
        return CommandResult(ok=False, message="RPC result missing skills list", instance=instance)
    return CommandResult(ok=True, message=_format_editable_skills(scope, skills), instance=instance)


def skill_create(
    instance: ServerInstance,
    scope: str,
    name: str,
    content: str,
    source: str | None,
) -> CommandResult:
    """Create a Skill in an editable scope."""

    params = {"scope": scope, "name": name, "content": content}
    if source is not None:
        params["source"] = source
    return _skill_write_result(instance, "skill.create", params)


def skill_update(
    instance: ServerInstance,
    scope: str,
    name: str,
    content: str,
    source: str | None,
) -> CommandResult:
    """Replace a Skill's SKILL.md in an editable scope."""

    params = {"scope": scope, "name": name, "content": content}
    if source is not None:
        params["source"] = source
    return _skill_write_result(instance, "skill.update", params)


def skill_delete(instance: ServerInstance, scope: str, name: str, confirm: bool) -> CommandResult:
    """Delete a Skill after explicit confirmation."""

    if not confirm:
        return CommandResult(
            ok=False,
            message=(
                f"refusing to delete skill {name} from {scope} without confirmation; "
                "re-run with --yes"
            ),
            instance=instance,
        )
    return _skill_write_result(instance, "skill.delete", {"scope": scope, "name": name})


def skill_write_file(
    instance: ServerInstance,
    scope: str,
    name: str,
    path: str,
    content: str,
) -> CommandResult:
    """Write one supporting file inside an editable Skill."""

    return _skill_write_result(
        instance,
        "skill.write_file",
        {"scope": scope, "name": name, "path": path, "content": content},
    )


def skill_remove_file(
    instance: ServerInstance,
    scope: str,
    name: str,
    path: str,
    confirm: bool,
) -> CommandResult:
    """Remove one supporting file after explicit confirmation."""

    if not confirm:
        return CommandResult(
            ok=False,
            message=(
                f"refusing to remove {path} from skill {name} in {scope} without confirmation; "
                "re-run with --yes"
            ),
            instance=instance,
        )
    return _skill_write_result(
        instance,
        "skill.remove_file",
        {"scope": scope, "name": name, "path": path},
    )


def skill_inventory(instance: ServerInstance) -> CommandResult:
    """Return formatted manager inventory output from `skill.inventory` RPC."""

    payload = _rpc_call(instance, "skill.inventory", {})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_inventory(payload.data),
        instance=instance,
    )


def skill_set_disabled(instance: ServerInstance, name: str, disabled: bool) -> CommandResult:
    """Toggle the policy disable switch for one skill name."""

    payload = _rpc_call(instance, "skill.set_disabled", {"name": name, "disabled": disabled})
    if not payload.ok:
        return payload.to_command_result()
    state = "disabled" if disabled else "enabled"
    return CommandResult(ok=True, message=f"{state} skill {name}", instance=instance)


def skill_share(
    instance: ServerInstance,
    agent_id: str,
    name: str,
    receivers: Sequence[str],
) -> CommandResult:
    """Share one agent's private skill with specific receiver agents."""

    payload = _rpc_call(
        instance,
        "skill.share",
        {"agent_id": agent_id, "name": name, "shared": True, "receivers": list(receivers)},
    )
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=(
            f"shared skill {name} from {agent_id} to: "
            f"{_format_receiver_list(payload.data.get('receivers'), receivers)}"
        ),
        instance=instance,
    )


def skill_unshare(instance: ServerInstance, agent_id: str, name: str) -> CommandResult:
    """Stop sharing one agent's private skill."""

    payload = _rpc_call(
        instance,
        "skill.share",
        {"agent_id": agent_id, "name": name, "shared": False},
    )
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True, message=f"unshared skill {name} from {agent_id}", instance=instance
    )


def _skill_write_result(
    instance: ServerInstance, method: str, params: dict[str, str]
) -> CommandResult:
    payload = _rpc_call(instance, method, params)
    if not payload.ok:
        return payload.to_command_result()
    name = _string_or_default(payload.data.get("name"), params.get("name", "?"))
    operation = _string_or_default(payload.data.get("operation"), method.removeprefix("skill."))
    warnings = _string_list(payload.data.get("warnings"))
    warning_text = "; ".join(warnings) if warnings else "-"
    return CommandResult(
        ok=True,
        message=(
            f"{operation} skill {name}\nscope: {params.get('scope', '?')}\nwarnings: {warning_text}"
        ),
        instance=instance,
    )


def _format_inventory(data: object) -> str:
    """Render the manager inventory payload as deterministic plain text."""

    if not isinstance(data, dict):
        return "skill inventory unavailable: unexpected RPC result"
    skills = data.get("skills")
    if not isinstance(skills, list):
        skills = []
    lines: list[str] = []
    if not skills:
        lines.append("no skills found in any source")
    else:
        lines.append("skills:")
        for skill in skills:
            lines.append(_format_inventory_row(skill))
    stale_shared = data.get("stale_shared")
    if isinstance(stale_shared, list) and stale_shared:
        lines.append("")
        lines.append("stale shared entries (owner or package no longer exists):")
        for entry in stale_shared:
            if isinstance(entry, dict):
                owner_id = _string_or_default(entry.get("agent_id"), "?")
                name = _string_or_default(entry.get("name"), "?")
                lines.append(f"- {owner_id}: {name}")
            else:
                lines.append("- invalid stale entry")
    policy_diagnostics = data.get("policy_diagnostics")
    if isinstance(policy_diagnostics, list) and policy_diagnostics:
        lines.append("")
        lines.append("policy diagnostics:")
        for diagnostic in policy_diagnostics:
            lines.append(f"- {_string_or_default(diagnostic, 'unknown problem')}")
    return "\n".join(lines)


def _format_inventory_row(skill: object) -> str:
    if not isinstance(skill, dict):
        return "- invalid inventory entry"
    name = _string_or_default(skill.get("name"), "?")
    description = _string_or_default(skill.get("description"), "?")
    origin = _string_or_default(skill.get("origin"), "-")
    owner_id = _string_or_default(skill.get("owner_id"), "-")
    status = _string_or_default(skill.get("status"), "?")
    shared_with = ", ".join(_string_list(skill.get("shared_with"))) or "-"
    details = [f"status: {status}", f"owner: {owner_id}", f"shared_with: {shared_with}"]
    missing = _string_list(skill.get("missing"))
    optional_missing = _string_list(skill.get("optional_missing"))
    if status == "unavailable" and missing:
        details.append(f"missing: {'; '.join(missing)}")
    if optional_missing:
        details.append(f"optional missing: {'; '.join(optional_missing)}")
    warnings = _string_list(skill.get("warnings"))
    if warnings:
        details.append(f"warnings: {'; '.join(warnings)}")
    return f"- {name}  {description}  [{origin}]\n    " + "; ".join(details)


def _format_receiver_list(value: object, fallback: Sequence[str]) -> str:
    receivers = value if isinstance(value, list) else list(fallback)
    return ", ".join(receivers) if receivers else "-"


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


def _format_editable_skills(scope: str, skills: Sequence[object]) -> str:
    if not skills:
        return f"no editable skills in {scope}"
    lines = [f"editable skills in {scope}:"]
    for skill in skills:
        if not isinstance(skill, dict):
            lines.append("- invalid skill entry")
            continue
        name = _string_or_default(skill.get("name"), "?")
        description = _string_or_default(skill.get("description"), "?")
        raw_content = skill.get("content")
        content = raw_content if isinstance(raw_content, str) else ""
        lines.extend([f"--- {name} ---", f"description: {description}", content])
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
