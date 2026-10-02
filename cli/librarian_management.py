"""Librarian commands of the vBot CLI: inspect and start Librarian passes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cli.formatting import string_or_default as _string_or_default
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance
from cli.skill_management import format_skill_revision

_CONSOLIDATION_TEXT = {
    "ran": "merged and fixed overlapping skills with the agent's model",
    "unchanged": "skipped, no skill it may change has changed since the last merge",
    "too_few": "skipped, fewer than 2 skills it may change",
    "disabled": "off",
    "failed": "failed",
}


def librarian_status(instance: ServerInstance, agent_id: str) -> CommandResult:
    """Show an agent's last Librarian pass, its changes and the next one (`librarian.status`)."""

    payload = _rpc_call(instance, "librarian.status", {"agent_id": agent_id})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True, message="\n".join(_format_status(agent_id, payload.data)), instance=instance
    )


def librarian_run(instance: ServerInstance, agent_id: str) -> CommandResult:
    """Start a Librarian pass of an agent now, regardless of the interval (`librarian.run`)."""

    payload = _rpc_call(instance, "librarian.run", {"agent_id": agent_id})
    if not payload.ok:
        return payload.to_command_result()
    lines = [
        f"Librarian pass of {agent_id} started",
        f"see its result with: vbot librarian status {agent_id}",
    ]
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def _format_status(agent_id: str, data: Mapping[str, Any]) -> list[str]:
    settings = data.get("settings")
    settings = settings if isinstance(settings, dict) else {}
    lines = [f"Librarian of {agent_id}"]
    if settings.get("enabled"):
        lines.append(
            f"scheduled passes: every {settings.get('interval_days')} days; skills made in "
            f"the background are archived after {settings.get('archive_after_days')} days "
            f"unused; merging overlapping skills: {'on' if settings.get('consolidate') else 'off'}"
        )
    else:
        lines.append("scheduled passes: off (librarian.enabled); a pass can still be started")
    if data.get("available") is False:
        lines.append("not available: the agent cannot call skill and skill_manage, so no pass runs")
    if data.get("running"):
        lines.append(
            f"a pass is running since {_string_or_default(data.get('running_since'), '?')}"
        )
    last_pass = data.get("last_pass")
    if not isinstance(last_pass, dict):
        lines.append("no pass has run yet")
        return lines
    lines.extend(_format_pass(last_pass))
    if isinstance(data.get("next_due_at"), str):
        lines.append(f"next scheduled pass: {data['next_due_at']} (when the agent is idle)")
    changes = data.get("changes")
    changes = (
        [item for item in changes if isinstance(item, dict)] if isinstance(changes, list) else []
    )
    if not changes:
        lines.append("the last pass changed no skill")
        return lines
    lines.append(f"changes of the last pass: {len(changes)} revisions, newest first")
    for revision in changes:
        lines.extend(format_skill_revision(revision))
    ids = " ".join(str(revision.get("id")) for revision in changes)
    lines.append(f"undo them together with: vbot skill revert {ids} --scope agent:{agent_id}")
    return lines


def _format_pass(last_pass: Mapping[str, Any]) -> list[str]:
    trigger = "started by hand" if last_pass.get("trigger") == "manual" else "scheduled"
    outcome = str(last_pass.get("consolidation"))
    lines = [
        f"last pass: {_string_or_default(last_pass.get('finished_at'), '?')} ({trigger})",
        f"  archived {last_pass.get('archived', 0)} unused skills",
        f"  merge: {_CONSOLIDATION_TEXT.get(outcome, outcome)} "
        f"({last_pass.get('candidates', 0)} skills it may change)",
    ]
    if outcome in ("ran", "failed"):
        lines.append(
            f"  created {last_pass.get('created', 0)}, changed {last_pass.get('changed', 0)}, "
            f"merged away {last_pass.get('merged', 0)}"
        )
    if isinstance(last_pass.get("session_id"), str):
        lines.append(f"  session {last_pass['session_id']}, run {last_pass.get('run_id')}")
    if isinstance(last_pass.get("error"), str):
        lines.append(f"  error: {last_pass['error']}")
    return lines
