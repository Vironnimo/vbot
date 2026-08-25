"""Agent management RPC commands for the vBot CLI."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from difflib import get_close_matches
from typing import Any

from cli.formatting import bool_text as _bool_text
from cli.formatting import format_string_list as _format_string_list
from cli.formatting import string_or_default as _string_or_default
from cli.formatting import value_text as _value_text
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

AGENT_UPDATE_FLAGS = (
    "--name",
    "--model",
    "--clear-model",
    "--fallback-model",
    "--clear-fallback-model",
    "--temperature",
    "--clear-temperature",
    "--thinking-effort",
    "--clear-thinking-effort",
    "--memory-prompt-mode",
    "--custom-system-prompt",
    "--tool-access-mode",
    "--tool-allow",
    "--tool-deny",
    "--allowed-skills",
    "--subagent-allow",
    "--compaction-policy",
    "--clear-compaction-policy",
    "--workspace",
    "--default-workspace",
    "--copy-workspace-files",
    "--project",
    "--clear-project",
    "--current-session-id",
)


def agent_list(instance: ServerInstance) -> CommandResult:
    """Return formatted agent list output from `agent.list` RPC."""

    payload = _rpc_call(instance, "agent.list", {})
    if not payload.ok:
        return payload.to_command_result()
    agents = payload.data.get("agents")
    if not isinstance(agents, list):
        return CommandResult(ok=False, message="RPC result missing agents list", instance=instance)
    return CommandResult(ok=True, message=_format_agent_rows(agents), instance=instance)


def agent_show(instance: ServerInstance, agent_id: str) -> CommandResult:
    """Return one formatted agent from `agent.get` RPC."""

    payload = _rpc_call(instance, "agent.get", {"id": agent_id})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(ok=True, message=_format_agent_detail(payload.data), instance=instance)


def agent_create(
    instance: ServerInstance,
    agent_id: str,
    name: str,
    changes: Mapping[str, Any],
) -> CommandResult:
    """Create an agent via `agent.create` RPC."""

    params = {"id": agent_id, "name": name, **dict(changes)}
    payload = _rpc_call(instance, "agent.create", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_agent_operation("created", payload.data, agent_id),
        instance=instance,
    )


def agent_update(
    instance: ServerInstance,
    agent_id: str,
    changes: Mapping[str, Any],
) -> CommandResult:
    """Update an agent via `agent.update` RPC."""

    if changes.get("copy_workspace_identity_files") is True and "workspace" not in changes:
        return CommandResult(
            ok=False,
            message="--copy-workspace-files requires --workspace or --default-workspace",
            instance=instance,
        )
    if not changes:
        return CommandResult(
            ok=False,
            message=f"no agent fields provided; use one of: {', '.join(AGENT_UPDATE_FLAGS)}",
            instance=instance,
        )
    params = {"id": agent_id, **dict(changes)}
    payload = _rpc_call(instance, "agent.update", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_agent_operation("updated", payload.data, agent_id),
        instance=instance,
    )


def agent_rename(
    instance: ServerInstance,
    agent_id: str,
    new_agent_id: str,
) -> CommandResult:
    """Rename an Identity Agent via ``agent.rename`` RPC."""
    if agent_id == new_agent_id:
        return CommandResult(
            ok=False,
            message="new agent id must differ from the current id",
            instance=instance,
        )
    payload = _rpc_call(
        instance,
        "agent.rename",
        {"id": agent_id, "new_id": new_agent_id},
    )
    if not payload.ok:
        return payload.to_command_result()
    resolved_id = _string_or_default(payload.data.get("id"), new_agent_id)
    return CommandResult(
        ok=True,
        message=f"renamed {agent_id} -> {resolved_id}",
        instance=instance,
    )


def agent_delete(instance: ServerInstance, agent_id: str) -> CommandResult:
    """Delete an agent via `agent.delete` RPC."""

    payload = _rpc_call(instance, "agent.delete", {"id": agent_id})
    if not payload.ok:
        return payload.to_command_result()
    deleted_id = _string_or_default(payload.data.get("agent_id"), agent_id)
    return CommandResult(ok=True, message=f"deleted {deleted_id}", instance=instance)


def agent_reorder(instance: ServerInstance, agent_ids: Sequence[str]) -> CommandResult:
    """Reorder the Identity Agent roster via `agent.reorder` RPC.

    Reads the current listing first so the caller does not need to know the
    optimistic-concurrency revision; agents not passed keep their relative
    order after the listed ones. Unknown ids fail before any mutation with
    close-match suggestions so a wrong id can be corrected in one retry.
    """

    if len(set(agent_ids)) != len(agent_ids):
        return CommandResult(
            ok=False,
            message="duplicate agent ids in reorder list",
            instance=instance,
        )
    listing = _rpc_call(instance, "agent.list", {})
    if not listing.ok:
        return listing.to_command_result()
    current_agents = listing.data.get("agents")
    current_ids: list[str] = []
    if isinstance(current_agents, list):
        for agent in current_agents:
            if isinstance(agent, dict) and isinstance(agent.get("id"), str):
                current_ids.append(agent["id"])
    known = set(current_ids)
    unknown_ids = [agent_id for agent_id in agent_ids if agent_id not in known]
    if unknown_ids:
        return CommandResult(
            ok=False,
            message=_unknown_agent_error(unknown_ids[0], unknown_ids, current_ids),
            instance=instance,
        )
    ordered_ids = list(agent_ids) + [
        agent_id for agent_id in current_ids if agent_id not in set(agent_ids)
    ]
    params: dict[str, Any] = {
        "agent_ids": ordered_ids,
        "expected_revision": listing.data.get("order_revision"),
    }
    payload = _rpc_call(instance, "agent.reorder", params)
    if not payload.ok:
        return payload.to_command_result()
    revision = _value_text(payload.data.get("order_revision"))
    return CommandResult(
        ok=True,
        message=f"agent roster reordered (revision {revision}): {', '.join(ordered_ids)}",
        instance=instance,
    )


def _unknown_agent_error(
    first_unknown: str,
    unknown_ids: Sequence[str],
    candidates: Sequence[str],
) -> str:
    close = get_close_matches(first_unknown, list(candidates), n=1)
    lines = [f"unknown agent id: {first_unknown}"]
    if close:
        lines.append(f"did you mean: {close[0]}")
    if candidates:
        lines.append(f"available agents: {', '.join(candidates)}")
    extra = len(unknown_ids) - 1
    if extra > 0:
        lines.append(f"(plus {extra} more unknown id(s) in the reorder list)")
    return "\n".join(lines)


def _format_agent_rows(agents: Sequence[object]) -> str:
    if not agents:
        return "no agents configured"

    lines = ["agents:"]
    for agent in agents:
        lines.append(_format_agent_row(agent))
    return "\n".join(lines)


def _format_agent_row(agent: object) -> str:
    if not isinstance(agent, dict):
        return "- invalid agent entry"

    agent_id = _string_or_default(agent.get("id"), "?")
    name = _string_or_default(agent.get("name"), "?")
    model = _string_or_default(agent.get("model"), "-")
    fallback_model = _string_or_default(agent.get("fallback_model"), "-")
    temperature = _value_text(agent.get("temperature"))
    thinking_effort = _value_text(agent.get("thinking_effort"))
    current_session_id = _string_or_default(agent.get("current_session_id"), "-")
    context_window = _value_text(agent.get("context_window"))
    return (
        f"- id={agent_id}"
        f" name={name}"
        f" model={model}"
        f" fallback_model={fallback_model}"
        f" temperature={temperature}"
        f" thinking_effort={thinking_effort}"
        f" current_session_id={current_session_id}"
        f" context_window={context_window}"
    )


def _format_agent_detail(agent: Mapping[str, Any]) -> str:
    custom_prompt_text = _bool_text(agent.get("custom_system_prompt_enabled"))
    lines = [
        "agent:",
        f"id: {_string_or_default(agent.get('id'), '?')}",
        f"name: {_string_or_default(agent.get('name'), '?')}",
        f"model: {_string_or_default(agent.get('model'), '-')}",
        f"fallback_model: {_string_or_default(agent.get('fallback_model'), '-')}",
        f"workspace: {_string_or_default(agent.get('workspace'), '-')}",
        f"project: {_string_or_default(agent.get('root_project_id'), '-')}",
        f"temperature: {_value_text(agent.get('temperature'))}",
        f"thinking_effort: {_value_text(agent.get('thinking_effort'))}",
        f"memory_prompt_mode: {_string_or_default(agent.get('memory_prompt_mode'), '-')}",
        f"custom_system_prompt_enabled: {custom_prompt_text}",
        f"tool_access: {_json_text(agent.get('tool_access'))}",
        f"allowed_skills: {_format_string_list(agent.get('allowed_skills'))}",
        f"current_session_id: {_string_or_default(agent.get('current_session_id'), '-')}",
        f"context_window: {_value_text(agent.get('context_window'))}",
        f"created_at: {_string_or_default(agent.get('created_at'), '-')}",
        f"updated_at: {_string_or_default(agent.get('updated_at'), '-')}",
    ]
    project_index = next(index for index, line in enumerate(lines) if line.startswith("project:"))
    if "default_workspace" in agent:
        lines.insert(
            project_index,
            f"default_workspace: {_string_or_default(agent.get('default_workspace'), '-')}",
        )
    skills_index = next(
        index for index, line in enumerate(lines) if line.startswith("allowed_skills:")
    )
    policy_lines: list[str] = []
    if "tools" in agent:
        policy_lines.append(
            f"subagent_allowed_agents: {_subagent_allowed_agents(agent.get('tools'))}"
        )
    if "compaction_policy" in agent:
        policy_lines.append(f"compaction_policy: {_json_text(agent.get('compaction_policy'))}")
    if "effective_compaction_policy" in agent:
        policy_lines.append(
            f"effective_compaction_policy: {_json_text(agent.get('effective_compaction_policy'))}"
        )
    if "effective" in agent:
        policy_lines.append(f"effective_sources: {_effective_source_text(agent.get('effective'))}")
    lines[skills_index + 1 : skills_index + 1] = policy_lines
    model = agent.get("model")
    if not isinstance(model, str) or not model:
        agent_id = _string_or_default(agent.get("id"), "<agent-id>")
        lines.extend(
            [
                "warning: no effective Model is configured; this Agent cannot run",
                "next: vbot model list --task chat",
                f"next: vbot agent update {agent_id} --model <model-id>",
            ]
        )
    return "\n".join(lines)


def _format_agent_operation(action: str, agent: Mapping[str, Any], fallback_id: str) -> str:
    agent_id = _string_or_default(agent.get("id"), fallback_id)
    if set(agent) <= {"id"}:
        return f"{action} {agent_id}"
    lines = [f"{action} agent {agent_id}", _format_agent_detail(agent)]
    relocation = agent.get("workspace_relocation")
    if isinstance(relocation, dict):
        lines.extend(
            [
                "workspace_relocation:",
                f"copied_files: {_format_string_list(relocation.get('copied_files'))}",
                f"backed_up_files: {_format_string_list(relocation.get('backed_up_files'))}",
            ]
        )
    return "\n".join(lines)


def _subagent_allowed_agents(value: object) -> str:
    if not isinstance(value, dict):
        return "-"
    subagent = value.get("subagent")
    if not isinstance(subagent, dict):
        return "-"
    return _format_string_list(subagent.get("allowed_agents"))


def _json_text(value: object) -> str:
    if value is None:
        return "-"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _effective_source_text(value: object) -> str:
    if not isinstance(value, dict):
        return "-"
    sources = {
        field: detail.get("source")
        for field, detail in value.items()
        if isinstance(field, str) and isinstance(detail, dict)
    }
    if not sources:
        return "-"
    return _json_text(sources)
