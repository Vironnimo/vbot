"""Pinned Memory management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping
from difflib import get_close_matches
from typing import Any

from cli.formatting import string_or_default as _string_or_default
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def memory_list(instance: ServerInstance, agent_id: str) -> CommandResult:
    """Return formatted pinned memory entries from `memory.list` RPC."""

    payload = _rpc_call(instance, "memory.list", {"agent_id": agent_id})
    if not payload.ok:
        return _memory_failure_result(instance, payload.to_command_result())
    return CommandResult(
        ok=True,
        message=_format_memory_response(payload.data, verb="pinned memory for"),
        instance=instance,
    )


def _memory_failure_result(
    instance: ServerInstance,
    failed: CommandResult,
) -> CommandResult:
    """Attach known agents to unknown-agent failures for one-retry fixes."""

    if "unknown agent" not in failed.message and "not found" not in failed.message.lower():
        return failed
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
    return CommandResult(ok=False, message="\n".join(lines), instance=instance)


def memory_add(
    instance: ServerInstance,
    agent_id: str,
    scope: str,
    content: str,
) -> CommandResult:
    """Add one pinned memory entry via `memory.add` RPC."""

    return _memory_mutation(
        instance,
        "memory.add",
        agent_id=agent_id,
        scope=scope,
        content=content,
        verb="added",
    )


def memory_replace(
    instance: ServerInstance,
    agent_id: str,
    scope: str,
    entry_id: int,
    content: str,
) -> CommandResult:
    """Replace one pinned memory entry's content via `memory.replace` RPC."""

    return _memory_mutation(
        instance,
        "memory.replace",
        agent_id=agent_id,
        scope=scope,
        entry_id=entry_id,
        content=content,
        verb="replaced",
    )


def memory_remove(
    instance: ServerInstance,
    agent_id: str,
    scope: str,
    entry_id: int,
    confirm: bool,
) -> CommandResult:
    """Remove one pinned memory entry after explicit confirmation."""

    if not confirm:
        return CommandResult(
            ok=False,
            message=(
                f"refusing to remove memory entry {entry_id} from {agent_id} "
                f"(scope: {scope}) without confirmation; re-run with --yes"
            ),
            instance=instance,
        )
    return _memory_mutation(
        instance,
        "memory.remove",
        agent_id=agent_id,
        scope=scope,
        entry_id=entry_id,
        verb="removed",
    )


def _memory_mutation(
    instance: ServerInstance,
    method: str,
    *,
    agent_id: str,
    scope: str,
    content: str | None = None,
    entry_id: int | None = None,
    verb: str,
) -> CommandResult:
    params: dict[str, Any] = {"agent_id": agent_id, "scope": scope}
    if entry_id is not None:
        params["entry_id"] = entry_id
    if content is not None:
        params["content"] = content
    payload = _rpc_call(instance, method, params)
    if not payload.ok:
        failed = payload.to_command_result()
        if "unknown agent" in failed.message or "not found" in failed.message.lower():
            failed = _memory_failure_result(instance, failed)
        elif entry_id is not None:
            return _with_available_entry_ids(instance, agent_id, scope, failed)
        return failed
    entry = payload.data.get("entry")
    lines = [f"{verb} memory entry in {agent_id} (scope: {scope})"]
    if isinstance(entry, dict):
        lines.append(_format_entry(entry))
    counts = _scope_counts(payload.data)
    if counts is not None:
        lines.append(f"remaining entries: {counts}")
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def _format_memory_response(data: object, *, verb: str) -> str:
    if not isinstance(data, dict):
        return "memory unavailable: unexpected RPC result"
    agent_id = _string_or_default(data.get("agent_id"), "?")
    scopes = data.get("scopes")
    lines = [f"{verb} {agent_id}:"]
    if not isinstance(scopes, dict):
        lines.append("no entries")
        return "\n".join(lines)
    total = 0
    for scope_name in ("agent", "user"):
        entries = scopes.get(scope_name)
        if not isinstance(entries, list):
            entries = []
        total += len(entries)
        lines.append(f"{scope_name} scope:")
        if not entries:
            lines.append("  (no entries)")
            continue
        for entry in entries:
            lines.append(f"  {_format_entry(entry)}")
    if total == 0:
        lines.append("no entries")
    return "\n".join(lines)


def _format_entry(entry: Mapping[str, Any]) -> str:
    entry_id = entry.get("id")
    content = _string_or_default(entry.get("content"), "")
    return f"#{entry_id}: {content}"


def _with_available_entry_ids(
    instance: ServerInstance,
    agent_id: str,
    scope: str,
    failed: CommandResult,
) -> CommandResult:
    """Show the scope's existing entry ids after a failed entry mutation."""

    listing = _rpc_call(instance, "memory.list", {"agent_id": agent_id})
    scopes = listing.data.get("scopes") if listing.ok else None
    entries = scopes.get(scope) if isinstance(scopes, dict) else None
    ids = (
        [entry.get("id") for entry in entries if isinstance(entry, dict)]
        if isinstance(entries, list)
        else []
    )
    lines = [failed.message]
    if ids:
        lines.append(f"existing {scope}-scope entries: {', '.join(str(i) for i in ids)}")
    else:
        lines.append(f"{agent_id} has no {scope}-scope entries")
    return CommandResult(ok=False, message="\n".join(lines), instance=instance)


def _first_quoted(message: str) -> str:
    start = message.find("'")
    if start == -1:
        return message
    end = message.find("'", start + 1)
    return message[start + 1 : end] if end != -1 else message


def _scope_counts(data: Mapping[str, Any]) -> str | None:
    scopes = data.get("scopes")
    if not isinstance(scopes, dict):
        return None
    parts = []
    for scope_name in ("agent", "user"):
        entries = scopes.get(scope_name)
        count = len(entries) if isinstance(entries, list) else 0
        parts.append(f"{scope_name}={count}")
    return " ".join(parts)
