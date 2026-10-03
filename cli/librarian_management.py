"""Librarian commands of the vBot CLI: inspect and start Librarian passes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cli.formatting import string_or_default as _string_or_default
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance
from cli.skill_management import format_skill_revision

_CONSOLIDATION_TEXT = {
    "ran": "merged and fixed overlapping skills",
    "unchanged": "skipped, no skill it may change has changed since the last merge",
    "too_few": "skipped, fewer than 2 skills it may change",
    "disabled": "off",
    "failed": "did not finish",
}
# Why the built-in Librarian agent is unavailable (`librarian_problem`).
_PROBLEM_TEXT = {
    "agent_id_taken": (
        "one of your agents uses its id librarian; rename that agent with: vbot agent rename "
        "librarian <new-id>, then restart vBot to create the Librarian"
    ),
    "invalid_config": (
        "agents/librarian/agent.json cannot be loaded; vbot doctor config names the problem; "
        "fix the file and restart vBot"
    ),
    "missing": "it does not exist yet; restart vBot to create it",
}
# Why an agent gets no scheduled pass (`unscheduled_reason`); status shows one reason.
_UNSCHEDULED_TEXT = {
    "librarian_unavailable": "not scheduled: the Librarian is unavailable: {problem}",
    "agent_disabled": (
        "not scheduled: the Librarian is off for this agent, so no pass runs; turn it on with: "
        "vbot agent update {agent_id} --librarian true"
    ),
    "no_skills": (
        "not scheduled: the agent has no skills of its own, so the Librarian has nothing to curate"
    ),
    "schedule_disabled": (
        "not scheduled: scheduled passes are off (librarian.enabled); a pass can still be started"
    ),
}
# A pass that stopped before it finished; the next scheduled pass still waits a full interval.
_OUTCOME_TEXT = {
    "failed": "stopped early by an error",
    "interrupted": "stopped early because vBot stopped",
}
# Each consolidation runs in a kept Session of the Librarian agent.
_SESSION_HINT = (
    "read a pass's session in the WebUI under Settings > Memory > Skill maintenance, or ask the "
    'Librarian about it with: vbot chat --agent librarian --session <session-id> "<message>"'
)
_MODEL_HINT = (
    "the Librarian is an agent with its own model; change it with: vbot agent update librarian "
    "--model <provider/model> --thinking-effort <level>"
)


def librarian_overview(instance: ServerInstance) -> CommandResult:
    """Show whether the Librarian is available and its recent passes (`librarian.overview`)."""

    payload = _rpc_call(instance, "librarian.overview", {})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True, message="\n".join(_format_overview(payload.data)), instance=instance
    )


def librarian_status(instance: ServerInstance, agent_id: str) -> CommandResult:
    """Show an agent's recent Librarian passes and the next one (`librarian.status`)."""

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


def _format_overview(data: Mapping[str, Any]) -> list[str]:
    settings = _mapping(data.get("settings"))
    lines = ["Librarian (agent librarian)"]
    problem = data.get("problem")
    if isinstance(problem, str):
        lines.append(f"unavailable: {_PROBLEM_TEXT.get(problem, problem)}")
    if settings.get("enabled"):
        lines.append(_schedule_line(settings))
    else:
        lines.append(
            "scheduled passes: off (librarian.enabled); start one with: "
            "vbot librarian run <agent-id>"
        )
    if isinstance(data.get("running"), str):
        lines.append(f"a pass of {data['running']} is running{_running_session(data)}")
    passes = _records(data.get("passes"))
    if not passes:
        lines.append("no pass has run yet")
    else:
        lines.append("recent passes, newest first:")
        lines.extend(f"- {_pass_line(record, agent=True)}" for record in passes)
    lines.append(
        "see an agent's passes and undo their changes with: vbot librarian status <agent-id>"
    )
    if any(isinstance(record.get("session_id"), str) for record in passes):
        lines.append(_SESSION_HINT)
    lines.append(_MODEL_HINT)
    return lines


def _format_status(agent_id: str, data: Mapping[str, Any]) -> list[str]:
    settings = _mapping(data.get("settings"))
    lines = [f"Librarian of {agent_id}"]
    reason = _UNSCHEDULED_TEXT.get(str(data.get("unscheduled_reason")))
    if reason is not None:
        problem = str(data.get("librarian_problem"))
        lines.append(reason.format(agent_id=agent_id, problem=_PROBLEM_TEXT.get(problem, problem)))
    if settings.get("enabled"):
        lines.append(_schedule_line(settings))
    if data.get("running"):
        since = _string_or_default(data.get("running_since"), "?")
        lines.append(f"a pass is running since {since}{_running_session(data)}")
    if isinstance(data.get("next_due_at"), str):
        lines.append(f"next scheduled pass: {data['next_due_at']} (when the agent is idle)")
    last_pass = data.get("last_pass")
    if not isinstance(last_pass, dict):
        lines.append("no pass has run yet")
        if _running_session(data):
            lines.append(_SESSION_HINT)
        return lines
    lines.extend(_format_pass(last_pass))
    earlier = _records(data.get("passes"))[1:]
    if earlier:
        lines.append("earlier passes, newest first:")
        lines.extend(f"- {_pass_line(record, agent=False)}" for record in earlier)
    changes = _records(data.get("changes"))
    if not changes:
        lines.append("the last pass changed no skill")
    else:
        lines.append(f"changes of the last pass: {len(changes)} revisions, newest first")
        for revision in changes:
            lines.extend(format_skill_revision(revision))
        ids = " ".join(str(revision.get("id")) for revision in changes)
        lines.append(f"undo them together with: vbot skill revert {ids} --scope agent:{agent_id}")
    if _running_session(data) or any(
        isinstance(record.get("session_id"), str) for record in (last_pass, *earlier)
    ):
        lines.append(_SESSION_HINT)
    return lines


def _running_session(data: Mapping[str, Any]) -> str:
    session_id = data.get("running_session_id")
    return f"; its session: {session_id}" if isinstance(session_id, str) and session_id else ""


def _schedule_line(settings: Mapping[str, Any]) -> str:
    merging = "on" if settings.get("consolidate") else "off"
    return (
        f"scheduled passes: every {settings.get('interval_days')} days; unpinned skills are "
        f"archived after {settings.get('archive_after_days')} days unused; "
        f"merging overlapping skills: {merging}"
    )


def _format_pass(last_pass: Mapping[str, Any]) -> list[str]:
    outcome = str(last_pass.get("consolidation"))
    lines = [
        f"last pass: {_string_or_default(last_pass.get('finished_at'), '?')} "
        f"({_trigger_text(last_pass)})"
    ]
    stopped = _OUTCOME_TEXT.get(str(last_pass.get("outcome")))
    if stopped is not None:
        lines.append(f"  {stopped}")
    lines += [
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


def _pass_line(record: Mapping[str, Any], *, agent: bool) -> str:
    """One pass in a list: when, whose, how it ended and its Session."""
    outcome = str(record.get("consolidation"))
    parts = [_string_or_default(record.get("finished_at"), "?")]
    if agent:
        agent_id = _string_or_default(record.get("agent_id"), "?")
        name = _string_or_default(record.get("agent_name"), agent_id)
        parts.append(agent_id if name == agent_id else f"{name} ({agent_id})")
    parts.append(_trigger_text(record))
    stopped = _OUTCOME_TEXT.get(str(record.get("outcome")))
    if stopped is not None:
        parts.append(stopped)
    parts.append(f"archived {record.get('archived', 0)}")
    parts.append(f"merge: {_CONSOLIDATION_TEXT.get(outcome, outcome)}")
    if outcome in ("ran", "failed"):
        parts.append(
            f"created {record.get('created', 0)}, changed {record.get('changed', 0)}, "
            f"merged away {record.get('merged', 0)}"
        )
    if isinstance(record.get("session_id"), str):
        parts.append(f"session {record['session_id']}")
    return "; ".join(parts)


def _trigger_text(record: Mapping[str, Any]) -> str:
    return "started by hand" if record.get("trigger") == "manual" else "scheduled"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _records(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
