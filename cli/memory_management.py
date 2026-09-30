"""Pinned Memory management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping
from difflib import get_close_matches
from typing import Any

from cli.formatting import string_or_default as _string_or_default
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

_AGENT_NOT_FOUND = "agent_not_found"


def memory_list(instance: ServerInstance, agent_id: str) -> CommandResult:
    """Return formatted pinned memory entries from `memory.list` RPC."""

    payload = _rpc_call(instance, "memory.list", {"agent_id": agent_id})
    if not payload.ok:
        return _memory_failure_result(instance, agent_id, payload.to_command_result())
    return CommandResult(
        ok=True,
        message=_format_memory_response(payload.data, verb="pinned memory for"),
        instance=instance,
    )


def _is_unknown_agent(failed: CommandResult) -> bool:
    return failed.failure is not None and failed.failure.code == _AGENT_NOT_FOUND


def _memory_failure_result(
    instance: ServerInstance,
    agent_id: str,
    failed: CommandResult,
) -> CommandResult:
    """Attach known agents to unknown-agent failures for one-retry fixes."""

    if not _is_unknown_agent(failed):
        return failed
    listing = _rpc_call(instance, "agent.list", {})
    agents = listing.data.get("agents") if listing.ok else None
    names: list[str] = []
    if isinstance(agents, list):
        for agent in agents:
            if isinstance(agent, dict) and isinstance(agent.get("id"), str):
                names.append(agent["id"])
    close = get_close_matches(agent_id, names, n=1)
    lines = [failed.message]
    if close:
        lines.append(f"did you mean: {close[0]}")
    if names:
        lines.append(f"available agents: {', '.join(names)}")
    return CommandResult(
        ok=False, message="\n".join(lines), instance=instance, failure=failed.failure
    )


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
        if _is_unknown_agent(failed):
            return _memory_failure_result(instance, agent_id, failed)
        if entry_id is not None:
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
    if not listing.ok:
        return CommandResult(
            ok=False,
            message=f"{failed.message}\nentry lookup failed: {listing.message}",
            instance=instance,
            failure=failed.failure,
        )
    scopes = listing.data.get("scopes")
    entries = scopes.get(scope) if isinstance(scopes, dict) else None
    ids = (
        [entry.get("id") for entry in entries if isinstance(entry, dict)]
        if isinstance(entries, list)
        else []
    )
    lines = [failed.message]
    if not isinstance(entries, list):
        lines.append("entry lookup returned no valid scope; existing entries are unknown")
    elif ids:
        lines.append(f"existing {scope}-scope entries: {', '.join(str(i) for i in ids)}")
    else:
        lines.append(f"{agent_id} has no {scope}-scope entries")
    return CommandResult(
        ok=False, message="\n".join(lines), instance=instance, failure=failed.failure
    )


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


def memory_history(
    instance: ServerInstance, agent_id: str, scope: str | None, limit: int
) -> CommandResult:
    """Return the newest Memory revisions from `memory.history` RPC."""

    params: dict[str, Any] = {"agent_id": agent_id, "limit": limit}
    if scope is not None:
        params["scope"] = scope
    payload = _rpc_call(instance, "memory.history", params)
    if not payload.ok:
        return _memory_failure_result(instance, agent_id, payload.to_command_result())
    revisions = _records(payload.data.get("revisions"))
    total = payload.data.get("total")
    scope_text = f" ({scope} scope)" if scope is not None else ""
    if not revisions:
        message = f"no Memory changes recorded for {agent_id}{scope_text}"
        return CommandResult(ok=True, message=message, instance=instance)
    lines = [
        f"Memory history of {agent_id}{scope_text}: {len(revisions)} of {total} revisions, "
        "newest first"
    ]
    for revision in revisions:
        lines.extend(_format_revision(revision))
    if isinstance(total, int) and total > len(revisions):
        lines.append(f"older revisions: re-run with --limit {total}")
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def memory_show(instance: ServerInstance, agent_id: str, revision: int) -> CommandResult:
    """Return the Memory entries as they were after one revision (`memory.show` RPC)."""

    payload = _rpc_call(instance, "memory.show", {"agent_id": agent_id, "revision": revision})
    if not payload.ok:
        return _memory_failure_result(instance, agent_id, payload.to_command_result())
    scopes = payload.data.get("scopes")
    lines = [f"Memory of {agent_id} after revision {revision}:"]
    for scope_name in ("agent", "user"):
        entries = scopes.get(scope_name) if isinstance(scopes, dict) else None
        lines.append(f"{scope_name} scope:")
        if not isinstance(entries, list) or not entries:
            lines.append("  (no entries)")
            continue
        lines.extend(f"  - {_string_or_default(entry, '')}" for entry in entries)
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def memory_diff(
    instance: ServerInstance, agent_id: str, from_revision: int, to_revision: int | None
) -> CommandResult:
    """Return how Memory changed between two revisions (`memory.diff` RPC)."""

    params: dict[str, Any] = {"agent_id": agent_id, "from": from_revision}
    if to_revision is not None:
        params["to"] = to_revision
    payload = _rpc_call(instance, "memory.diff", params)
    if not payload.ok:
        return _memory_failure_result(instance, agent_id, payload.to_command_result())
    target = f"revision {to_revision}" if to_revision is not None else "now"
    changes = payload.data.get("changes")
    lines = [f"Memory of {agent_id} from revision {from_revision} to {target}:"]
    changed = False
    for scope_name in ("agent", "user"):
        scope_changes = changes.get(scope_name) if isinstance(changes, dict) else None
        if not isinstance(scope_changes, list) or not scope_changes:
            continue
        changed = True
        lines.append(f"{scope_name} scope:")
        lines.extend(_format_change(change) for change in scope_changes)
    if not changed:
        lines.append("no differences")
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def memory_revert(instance: ServerInstance, agent_id: str, revisions: list[int]) -> CommandResult:
    """Take back the changes of one or more revisions (`memory.revert` RPC)."""

    payload = _rpc_call(instance, "memory.revert", {"agent_id": agent_id, "revisions": revisions})
    if not payload.ok:
        return _memory_failure_result(instance, agent_id, payload.to_command_result())
    named = ", ".join(str(revision) for revision in revisions)
    noun = "revisions" if len(revisions) > 1 else "revision"
    recorded = _records(payload.data.get("revisions"))
    if not recorded:
        message = f"nothing to revert in {agent_id}: the changes of {noun} {named} are already gone"
        return CommandResult(ok=True, message=message, instance=instance)
    lines = [f"reverted {noun} {named} in {agent_id}"]
    for revision in recorded:
        lines.extend(_format_revision(revision))
    counts = _scope_counts(payload.data)
    if counts is not None:
        lines.append(f"entries now: {counts}")
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


_REVISION_KIND_TEXT = {
    "baseline": "history starts with the existing entries",
    "edit": "changed",
    "external": "file edited outside Memory",
}


def _records(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _format_revision(revision: Mapping[str, Any]) -> list[str]:
    kind = revision.get("kind")
    if kind == "revert":
        reverted = revision.get("reverts")
        targets = ", ".join(str(item) for item in reverted) if isinstance(reverted, list) else "?"
        what = f"revert of revision {targets}"
    else:
        what = _REVISION_KIND_TEXT.get(str(kind), str(kind))
    header = (
        f"revision {revision.get('id')}  {_string_or_default(revision.get('at'), '?')}  "
        f"{_string_or_default(revision.get('scope'), '?')} scope  {what}"
    )
    actor = revision.get("actor")
    if kind in ("edit", "revert") and isinstance(actor, str):
        header += f" by {actor}"
    origin = [
        f"{name} {revision[key]}"
        for name, key in (("session", "session_id"), ("run", "run_id"))
        if isinstance(revision.get(key), str)
    ]
    if origin:
        header += f" ({', '.join(origin)})"
    changes = revision.get("changes")
    lines = [header]
    if isinstance(changes, list):
        lines.extend(_format_change(change) for change in changes)
    return lines


def _format_change(change: object) -> str:
    if not isinstance(change, dict):
        return "  ? unreadable change"
    text = _string_or_default(change.get("text"), "")
    op = change.get("op")
    if op == "added":
        return f"  + {text}"
    if op == "removed":
        return f"  - {text}"
    previous = _string_or_default(change.get("previous"), "")
    return f"  ~ {previous}\n    -> {text}"
