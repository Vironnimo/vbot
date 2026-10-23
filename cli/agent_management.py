"""Agent management RPC commands for the vBot CLI."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any

from cli._recovery import format_command, target_options
from cli.archive_management import (
    permanent_delete_refusal,
    sessions_text,
    unfinished_permanent_delete,
)
from cli.formatting import bool_text as _bool_text
from cli.formatting import format_string_list as _format_string_list
from cli.formatting import record_fields
from cli.formatting import string_or_default as _string_or_default
from cli.formatting import value_text as _value_text
from cli.permission_notes import permission_notes, with_notes
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance
from core.permissions import (
    PermissionRulesError,
    PresetName,
    apply_preset,
    matching_preset,
    normalize_permission_rules,
)

AGENT_UPDATE_FLAGS = (
    "--name",
    "--model",
    "--clear-model",
    "--fallback-models",
    "--clear-fallback-models",
    "--temperature",
    "--clear-temperature",
    "--top-p",
    "--clear-top-p",
    "--thinking-effort",
    "--clear-thinking-effort",
    "--memory-prompt-mode",
    "--custom-system-prompt",
    "--librarian",
    "--tool-access-mode",
    "--tool-allow",
    "--tool-deny",
    "--allowed-skills",
    "--excluded-skills",
    "--context-commands",
    "--subagent-allow",
    "--permissions",
    "--allow-rule",
    "--deny-rule",
    "--clear-permissions",
    "--compaction-policy",
    "--clear-compaction-policy",
    "--workspace",
    "--default-workspace",
    "--copy-workspace-files",
    "--project",
    "--clear-project",
    "--current-session-id",
)


@dataclass(frozen=True)
class PermissionEdit:
    """What the permission flags of ``agent create`` and ``agent update`` ask for.

    ``rules`` replaces the agent's own rule set and ``preset`` applies a preset
    (``full_access`` stores the explicit full set, which overrides a restrictive
    shared default); ``allow`` and ``deny`` add rules; ``clear`` removes the own
    rule set, so the agent follows the shared default. Unless ``rules``,
    ``clear`` or full access replaces everything, the edit starts from the
    agent's current own rules.
    """

    rules: Mapping[str, Any] | None = None
    preset: PresetName | None = None
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    clear: bool = False

    @property
    def keeps_current(self) -> bool:
        return not self.clear and self.rules is None and self.preset != "full_access"

    def apply(self, current: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """The own rule set after this edit, starting from ``current``."""
        if self.clear:
            return None
        result = dict(self.rules if self.rules is not None else current or {})
        if self.preset is not None:
            result = apply_preset(self.preset, result)
        for part, added in (("allow", self.allow), ("deny", self.deny)):
            if added:
                listed = result.get(part)
                result[part] = [*(listed if isinstance(listed, list) else []), *added]
        return result


def _with_permission_rules(
    instance: ServerInstance, agent_id: str | None, changes: Mapping[str, Any]
) -> dict[str, Any] | CommandResult:
    """``changes`` with a :class:`PermissionEdit` turned into ``permission_rules``.

    An edit that keeps the current rules of an existing agent reads them first.
    """
    edit = changes.get("permission_rules")
    if not isinstance(edit, PermissionEdit):
        return dict(changes)
    current: Mapping[str, Any] | None = None
    if agent_id is not None and edit.keeps_current:
        payload = _rpc_call(instance, "agent.get", {"id": agent_id})
        if not payload.ok:
            return payload.to_command_result()
        own = _own_config(payload.data).get("permission_rules")
        current = own if isinstance(own, dict) else None
    return {**changes, "permission_rules": edit.apply(current)}


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
    return CommandResult(
        ok=True,
        message=_format_agent_detail(payload.data),
        instance=instance,
        attention=_agent_attention(payload.data),
    )


def agent_create(
    instance: ServerInstance,
    agent_id: str,
    name: str,
    changes: Mapping[str, Any],
) -> CommandResult:
    """Create an agent via `agent.create` RPC."""

    resolved = _with_permission_rules(instance, None, changes)
    if isinstance(resolved, CommandResult):
        return resolved
    params = {"id": agent_id, "name": name, **resolved}
    payload = _rpc_call(instance, "agent.create", params)
    if not payload.ok:
        return payload.to_command_result()
    notes = permission_notes(
        instance,
        resolved.get("permission_rules"),
        agent_id=_string_or_default(payload.data.get("id"), agent_id),
    )
    return CommandResult(
        ok=True,
        message=with_notes(_format_agent_operation("created", payload.data, agent_id), notes),
        instance=instance,
        attention=_agent_attention(payload.data),
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
    resolved = _with_permission_rules(instance, agent_id, changes)
    if isinstance(resolved, CommandResult):
        return resolved
    params = {"id": agent_id, **resolved}
    payload = _rpc_call(instance, "agent.update", params)
    if not payload.ok:
        return payload.to_command_result()
    notes = permission_notes(instance, resolved.get("permission_rules"), agent_id=agent_id)
    return CommandResult(
        ok=True,
        message=with_notes(_format_agent_operation("updated", payload.data, agent_id), notes),
        instance=instance,
        attention=_agent_attention(payload.data),
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


def agent_delete(
    instance: ServerInstance, agent_id: str, permanent: bool = False, confirm: bool = False
) -> CommandResult:
    """Archive an Identity Agent via `agent.delete` RPC, or delete it permanently.

    A permanent deletion cannot be undone, so it needs ``confirm`` (``--yes``).
    """

    if permanent and not confirm:
        return CommandResult(
            ok=False,
            message=permanent_delete_refusal(
                instance, f"agent {agent_id}", ("vbot", "agent", "delete", agent_id, "--permanent")
            ),
            instance=instance,
        )
    params: dict[str, object] = {"id": agent_id}
    if permanent:
        params["permanent"] = True
    payload = _rpc_call(instance, "agent.delete", params)
    if not payload.ok:
        return payload.to_command_result()
    data = payload.data
    deleted_id = _string_or_default(data.get("agent_id"), agent_id)
    entry_id = _string_or_default(data.get("archive_entry_id"), "-")
    sessions = sessions_text(data.get("session_count"))
    attention: tuple[str, ...] = ()
    ok = True
    if data.get("purged") is True:
        lines = [f"deleted agent {deleted_id} permanently ({sessions})"]
    elif permanent:
        clause, attention, ok = unfinished_permanent_delete(instance, data, entry_id)
        lines = [f"archived agent {deleted_id} as archive entry {entry_id} ({sessions}), {clause}"]
    else:
        command = format_command(
            ("vbot", "archive", "restore", entry_id, *target_options(instance))
        )
        lines = [
            f"archived agent {deleted_id} as archive entry {entry_id} ({sessions}); "
            f"restore with: {command}"
        ]
    workspace = data.get("external_workspace")
    if isinstance(workspace, str) and workspace:
        lines.append(f"external Workspace left in place: {workspace}")
    return CommandResult(ok=ok, message="\n".join(lines), instance=instance, attention=attention)


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
    fallback_models = _format_string_list(agent.get("fallback_models"))
    temperature = _value_text(agent.get("temperature"))
    top_p = _value_text(agent.get("top_p"))
    thinking_effort = _value_text(agent.get("thinking_effort"))
    current_session_id = _string_or_default(agent.get("current_session_id"), "-")
    context_window = _value_text(agent.get("context_window"))
    return record_fields(
        [
            f"- id={agent_id}",
            f" name={name}",
            f" model={model}",
            f" fallback_models={fallback_models}",
            f" temperature={temperature}",
            f" top_p={top_p}",
            f" thinking_effort={thinking_effort}",
            f" current_session_id={current_session_id}",
            f" context_window={context_window}",
        ],
        separator="",
    )


# The built-in Librarian: what it is and what an update may change.
_LIBRARIAN_LINE = (
    "builtin: librarian (curates the skills of your other agents; only model, "
    "fallback_models, temperature, top_p and thinking_effort can change)"
)


def _format_agent_detail(agent: Mapping[str, Any]) -> str:
    custom_prompt_text = _bool_text(agent.get("custom_system_prompt_enabled"))
    lines = [
        "agent:",
        f"id: {_string_or_default(agent.get('id'), '?')}",
        f"name: {_string_or_default(agent.get('name'), '?')}",
        f"description: {_string_or_default(agent.get('description'), '-')}",
        f"model: {_string_or_default(agent.get('model'), '-')}",
        f"fallback_models: {_format_string_list(agent.get('fallback_models'))}",
        f"workspace: {_string_or_default(agent.get('workspace'), '-')}",
        f"default_project: {_string_or_default(agent.get('root_project_id'), '-')}",
        f"temperature: {_value_text(agent.get('temperature'))}",
        f"top_p: {_value_text(agent.get('top_p'))}",
        f"thinking_effort: {_value_text(agent.get('thinking_effort'))}",
        f"memory_prompt_mode: {_string_or_default(agent.get('memory_prompt_mode'), '-')}",
        f"custom_system_prompt_enabled: {custom_prompt_text}",
        f"librarian_enabled: {_bool_text(agent.get('librarian_enabled'))}",
        f"tool_access: {_json_text(agent.get('tool_access'))}",
        f"tool_loading: {_json_text(agent.get('tool_loading'))}",
        f"tool_loading_own: {_json_text(_own_config(agent).get('tool_loading'))}",
        *_permission_lines(agent),
        f"allowed_skills: {_format_string_list(agent.get('allowed_skills'))}",
        f"current_session_id: {_string_or_default(agent.get('current_session_id'), '-')}",
        f"context_window: {_value_text(agent.get('context_window'))}",
        f"created_at: {_string_or_default(agent.get('created_at'), '-')}",
        f"updated_at: {_string_or_default(agent.get('updated_at'), '-')}",
    ]
    if agent.get("builtin") == "librarian":
        lines.insert(3, _LIBRARIAN_LINE)
    project_index = next(
        index for index, line in enumerate(lines) if line.startswith("default_project:")
    )
    if "default_workspace" in agent:
        lines.insert(
            project_index,
            f"default_workspace: {_string_or_default(agent.get('default_workspace'), '-')}",
        )
    skills_index = next(
        index for index, line in enumerate(lines) if line.startswith("allowed_skills:")
    )
    policy_lines: list[str] = []
    if "excluded_skills" in agent:
        policy_lines.append(f"excluded_skills: {_format_string_list(agent.get('excluded_skills'))}")
    if agent.get("context_commands"):
        policy_lines.append(f"context_commands: {_json_text(agent.get('context_commands'))}")
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


def _permission_lines(agent: Mapping[str, Any]) -> list[str]:
    """The agent's Permission Rules: the preset they match, then the rules and their origin."""
    if "permission_rules" not in agent:
        return []
    effective = agent.get("permission_rules")
    own = _own_config(agent).get("permission_rules")
    default = agent.get("permission_rules_default")
    if effective is None:
        # Own rules that restrict nothing (the full-access preset) override the default.
        summary = "full-access (own rules)" if own is not None else "full-access"
    else:
        try:
            preset: str = matching_preset(normalize_permission_rules(effective))
        except PermissionRulesError:
            preset = "custom"
        if own is not None and default is not None:
            origin = "own rules over the shared default"
        elif own is not None:
            origin = "own rules"
        else:
            origin = "shared default"
        summary = f"{preset.replace('_', '-')} ({origin})"
    return [
        f"permissions: {summary}",
        f"permission_rules: {_json_text(effective)}",
        f"permission_rules_own: {_json_text(own)}",
        f"permission_rules_default: {_json_text(default)}",
    ]


def _own_config(agent: Mapping[str, Any]) -> Mapping[str, Any]:
    config = agent.get("config")
    return config if isinstance(config, dict) else {}


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


def _agent_attention(agent: Mapping[str, Any]) -> tuple[str, ...]:
    if set(agent) <= {"id"} or agent.get("model"):
        return ()
    return ("No effective Model is configured; this Agent cannot run",)
