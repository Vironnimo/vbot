"""Skill management RPC commands for the vBot CLI."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from cli.formatting import record_fields
from cli.formatting import string_or_default as _string_or_default
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def skill_install(
    instance: ServerInstance,
    source: str,
    scope: str,
    *,
    path: str | None = None,
    ref: str | None = None,
    dry_run: bool = False,
    replace: bool = False,
    confirm: bool = False,
) -> CommandResult:
    """Import a package through the server's shared Skill write owner."""
    if replace and not confirm and not dry_run:
        return CommandResult(
            ok=False,
            instance=instance,
            message=(
                "Replacing a complete Skill requires --replace --yes; "
                "inspect it with --dry-run first."
            ),
        )
    local = instance.host in {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}
    if scope == "own":
        agent_id = os.environ.get("VBOT_RUN_AGENT_ID", "").strip()
        if (
            not local
            or not agent_id
            or not os.environ.get("VBOT_RUN_SESSION_ID")
            or os.environ.get("VBOT_RUN_PROJECT_ID")
        ):
            return CommandResult(
                ok=False,
                instance=instance,
                message=(
                    "--scope own requires an Identity Agent's vBot Run on this server. "
                    "Use --scope global or --scope agent:<id> for an explicit target."
                ),
            )
        scope = f"agent:{agent_id}"
    if local and not source.lower().startswith(("http://", "https://")):
        source = str(Path(source).expanduser().absolute())
    # Remote paths are server-native, not parsed with the client's OS path grammar.
    params: dict[str, Any] = {
        "scope": scope,
        "source": source,
        "replace": replace,
        "dry_run": dry_run,
    }
    if path is not None:
        params["path"] = path
    if ref is not None:
        params["ref"] = ref
    payload = _rpc_call(instance, "skill.install", params)
    if not payload.ok:
        return payload.to_command_result()
    data = payload.data
    operation = data.get("operation")
    if operation not in {"installed", "replaced", "unchanged", "preview", "candidates"}:
        return CommandResult(
            ok=False,
            instance=instance,
            message=(
                "Skill install returned no valid outcome; inspect skill inventory before retrying."
            ),
        )
    lines = [
        f"{operation} skill {data.get('name') or 'packages'}",
        f"scope: {scope}",
        f"source: {data.get('source', '-')}",
    ]
    if data.get("name"):
        lines.extend(
            [
                f"path: {data.get('package_path')}",
                f"files: {data.get('files')}",
                f"sha256: {data.get('sha256')}",
            ]
        )
    candidates = data.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, dict):
                lines.append(
                    f"- {candidate.get('path')}: {candidate.get('name')} - "
                    f"{candidate.get('description')}"
                )
                if "exists" in candidate:
                    lines.append(
                        f"  exists: {candidate['exists']}; unchanged: {candidate.get('unchanged')}"
                    )
    if operation in {"preview", "candidates"}:
        lines.append(
            "No files were written. Select --path when needed, then omit --dry-run to install."
        )
    else:
        lines.append(
            "Package saved and Skill catalog refreshed. Availability still follows disable "
            "policy, requirements and the Agent's Skill selection; "
            "check the skill Tool in the target Agent."
        )
    warnings = _string_list(data.get("warnings"))
    lines.extend(f"warning: {warning}" for warning in warnings)
    return CommandResult(
        ok=True, instance=instance, message="\n".join(lines), attention=tuple(warnings)
    )


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
        attention=("Some Skills could not be loaded; see diagnostics",) if invalid else (),
    )


def skill_read(instance: ServerInstance, scope: str, name: str | None = None) -> CommandResult:
    """Read editable Skills and their complete SKILL.md content in one scope."""

    payload = _rpc_call(instance, "skill.read", {"scope": scope})
    if not payload.ok:
        return payload.to_command_result()
    skills = payload.data.get("skills")
    if not isinstance(skills, list):
        return CommandResult(ok=False, message="RPC result missing skills list", instance=instance)
    if name is not None:
        skills = [item for item in skills if isinstance(item, dict) and item.get("name") == name]
        if not skills:
            return CommandResult(
                ok=False,
                message=f"skill not found in {scope}: {name}; use vbot skill inventory",
                instance=instance,
            )
    return CommandResult(ok=True, message=_format_editable_skills(scope, skills), instance=instance)


def skill_inspect(instance: ServerInstance, entry_id: str) -> CommandResult:
    """Read an exact source package from the inventory without activating it."""
    payload = _rpc_call(instance, "skill.inspect", {"id": entry_id})
    if not payload.ok:
        return payload.to_command_result()
    content = payload.data.get("content")
    if not isinstance(content, str):
        return CommandResult(
            ok=False, message="RPC result missing Skill content", instance=instance
        )
    return CommandResult(ok=True, message=f"id: {entry_id}\n{content}", instance=instance)


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
        if "unknown skill" in payload.message:
            return _skill_name_suggestions(instance, payload.to_command_result())
        return payload.to_command_result()
    state = "disabled" if disabled else "enabled"
    return CommandResult(ok=True, message=f"{state} skill {name}", instance=instance)


def _skill_name_suggestions(instance: ServerInstance, failed: CommandResult) -> CommandResult:
    """Attach known skill names to an unknown-name failure for one-retry fixes."""

    inventory_payload = _rpc_call(instance, "skill.inventory", {})
    names: list[str] = []
    if inventory_payload.ok:
        skills = inventory_payload.data.get("skills")
        if isinstance(skills, list):
            names = sorted(
                set(
                    _string_list([skill.get("name") for skill in skills if isinstance(skill, dict)])
                )
            )
    close = get_close_matches(_first_quoted(failed.message), names, n=1)
    lines = [failed.message]
    if close:
        lines.append(f"did you mean: {close[0]}")
    if names:
        lines.append(f"known skills: {', '.join(names)}")
    return CommandResult(
        ok=False, message="\n".join(lines), instance=instance, failure=failed.failure
    )


def _first_quoted(message: str) -> str:
    start = message.find("'")
    if start == -1:
        return message
    end = message.find("'", start + 1)
    return message[start + 1 : end] if end != -1 else message


def _agent_id_suggestions(instance: ServerInstance, failed: CommandResult) -> CommandResult:
    """Attach known agent ids to an unknown-agent failure."""

    listing = _rpc_call(instance, "agent.list", {})
    agents = listing.data.get("agents") if listing.ok else None
    names: list[str] = []
    if isinstance(agents, list):
        for agent in agents:
            if isinstance(agent, dict) and isinstance(agent.get("id"), str):
                names.append(agent["id"])
    close = get_close_matches(_first_quoted(failed.message), names, n=1)
    lines = [failed.message]
    if close:
        lines.append(f"did you mean: {close[0]}")
    if names:
        lines.append(f"available agents: {', '.join(names)}")
    return CommandResult(
        ok=False, message="\n".join(lines), instance=instance, failure=failed.failure
    )


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
        return _share_failure_result(instance, agent_id, name, payload.to_command_result())
    return CommandResult(
        ok=True,
        message=(
            f"shared skill {name} from {agent_id} to: "
            f"{_format_receiver_list(payload.data.get('receivers'), receivers)}"
        ),
        instance=instance,
    )


def _share_failure_result(
    instance: ServerInstance,
    agent_id: str,
    name: str,
    failed: CommandResult,
) -> CommandResult:
    """Route share/unshare failures to the matching candidate suggestion."""

    if "unknown agent" in failed.message or "unknown receiver" in failed.message:
        return _agent_id_suggestions(instance, failed)
    if f"owns no private skill named {name!r}" in failed.message:
        inventory_payload = _rpc_call(instance, "skill.inventory", {})
        if not inventory_payload.ok:
            return CommandResult(
                ok=False,
                message=f"{failed.message}\ninventory lookup failed: {inventory_payload.message}",
                instance=instance,
                failure=failed.failure,
            )
        skills = inventory_payload.data.get("skills")
        if not isinstance(skills, list):
            return CommandResult(
                ok=False,
                message=f"{failed.message}\ninventory lookup returned no valid Skill list",
                instance=instance,
                failure=failed.failure,
            )
        owned = sorted(
            set(
                _string_list(
                    [
                        skill.get("name")
                        for skill in skills
                        if isinstance(skill, dict) and skill.get("owner_id") == agent_id
                    ]
                )
            )
        )
        lines = [failed.message]
        if owned:
            lines.append(f"{agent_id}'s private skills: {', '.join(owned)}")
        else:
            lines.append(f"{agent_id} owns no private skills")
        return CommandResult(
            ok=False, message="\n".join(lines), instance=instance, failure=failed.failure
        )
    return failed


def skill_unshare(instance: ServerInstance, agent_id: str, name: str) -> CommandResult:
    """Stop sharing one agent's private skill."""

    payload = _rpc_call(
        instance,
        "skill.share",
        {"agent_id": agent_id, "name": name, "shared": False},
    )
    if not payload.ok:
        return _share_failure_result(instance, agent_id, name, payload.to_command_result())
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
    details = [
        f"id: {skill.get('id', '?')}",
        f"editable_scope: {skill.get('editable_scope') or 'read-only'}",
        f"status: {status}",
        f"owner: {owner_id}",
        f"shared_with: {shared_with}",
    ]
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
    return record_fields([f"- {name}", f"{description}{suffix}"], separator="  ")


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
