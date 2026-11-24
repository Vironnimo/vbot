"""Agent-facing texts, Tool schemas, and run prompts for Simplified Factory Jobs."""

from __future__ import annotations

from typing import Any

from ._native_agent_text import _bounded, _data

type Json = dict[str, Any]

JOB_STATUS_TOOL_NAME = "factory_job_status"
LEADER_JOBS_TOOL_NAME = "factory_jobs"

_JOB_STATUS_DESCRIPTION = (
    "Report this job run's status when you need user input (needs_attention), "
    "cannot complete the job (failed), or want to set a dashboard summary (done). "
    "Finishing normally without calling this tool completes the job as done."
)

_JOB_STATUS_PARAMETERS: Json = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["needs_attention", "failed", "done"],
            "description": (
                "Use needs_attention to pause and ask the user, failed when the job "
                "cannot be completed, or done with a short summary."
            ),
        },
        "summary": {
            "type": "string",
            "description": "Short dashboard summary of the outcome. Omit to leave unchanged.",
        },
        "question": {
            "type": "string",
            "description": (
                "Required for needs_attention. Clear question or approval prompt for the user."
            ),
        },
        "reason": {
            "type": "string",
            "description": (
                "Required for failed; optional context for needs_attention. "
                "Explain what blocked or failed."
            ),
        },
    },
    "required": ["status"],
}

_LEADER_JOBS_DESCRIPTION = "Manage and trigger your Factory's recurring or on-demand jobs."

_LEADER_JOBS_PARAMETERS: Json = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "get", "create", "update", "delete", "trigger", "runs"],
            "description": (
                "update changes only the fields you send. "
                "trigger starts an immediate run. runs lists recent job runs."
            ),
        },
        "job_id": {
            "type": "string",
            "description": (
                "Job id returned by list, get, or create. For get, update, delete, or "
                "trigger (if name is omitted); optional filter for runs."
            ),
        },
        "name": {
            "type": "string",
            "description": (
                "Required for create. Exact job name; can identify a job for get or "
                "trigger when job_id is omitted."
            ),
        },
        "prompt": {
            "type": "string",
            "description": "Required for create. Instructions executed when the job runs.",
        },
        "description": {
            "type": "string",
            "description": (
                "When to use this job (shown in your job catalog). Omit to leave empty."
            ),
        },
        "agent_member_id": {
            "type": "string",
            "description": (
                "Factory member id assigned to run this job. Omit to use the Factory Leader."
            ),
        },
        "member_id": {
            "type": "string",
            "description": (
                "For trigger: Factory member id, role, or name to run this execution "
                "instead of the job default. Omit to use the job default."
            ),
        },
        "triggers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": ["schedule", "webhook"]},
                    "title": {"type": "string"},
                    "schedule": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            "description": (
                "Schedule (cron expression) or webhook triggers for this job. Omit for no triggers."
            ),
        },
        "enabled": {
            "type": "boolean",
            "description": (
                "Whether the job and its triggers are active. Omit for true on create."
            ),
        },
        "input_note": {
            "type": "string",
            "description": (
                "For trigger: extra context or parameters passed to this run. Omit if none."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "For runs: maximum number of runs to return (1-100). Omit for 20.",
        },
    },
    "required": ["action"],
}

JOB_PRIVATE_TOOLS: dict[str, tuple[str, Json]] = {
    JOB_STATUS_TOOL_NAME: (_JOB_STATUS_DESCRIPTION, _JOB_STATUS_PARAMETERS),
    LEADER_JOBS_TOOL_NAME: (_LEADER_JOBS_DESCRIPTION, _LEADER_JOBS_PARAMETERS),
}


def _summarize_triggers(triggers: list[Json]) -> str:
    if not triggers:
        return "manual only"
    parts: list[str] = []
    for trigger in triggers:
        trigger_type = str(trigger.get("type") or "trigger")
        if trigger_type == "schedule":
            schedule = str(trigger.get("schedule") or "").strip()
            label = f"schedule ({schedule})" if schedule else "schedule"
        else:
            label = trigger_type
        if not trigger.get("enabled", True):
            label += " [disabled]"
        parts.append(label)
    return ", ".join(parts)


def format_leader_jobs_context(jobs: list[Json]) -> str:
    """Render a concise catalog of the Factory's jobs for the Leader's context."""
    if not jobs:
        return ""
    lines = ["Factory jobs:"]
    for job in jobs:
        status = "" if job.get("enabled", True) else " [disabled]"
        assignee = str(job.get("agent_member_id") or "").strip() or "Leader (default)"
        trigger_summary = _summarize_triggers(list(job.get("triggers") or []))
        line = (
            f"- {job['name']} (id: {job['id']}{status}) | "
            f"assignee: {assignee} | triggers: {trigger_summary}"
        )
        description = str(job.get("description") or "").strip()
        if description:
            single_line = " ".join(description.split())
            if len(single_line) > 200:
                single_line = single_line[:197] + "..."
            line += f" — {single_line}"
        lines.append(line)
    return "\n".join(lines)


def build_job_run_instructions(factory: Json, member: Json, job_run: Json) -> str:
    """Build concise runtime instructions for a Job Run participant."""
    parts = [
        (
            f"You are running a Factory job for Factory {factory.get('name') or factory['id']} "
            f"(run {job_run['id']})."
        ),
        _data("Factory remit", _bounded(factory.get("remit", ""), [4000])),
        _data(
            "Your Factory role and instructions",
            _bounded(
                {key: member.get(key, "") for key in ("id", "name", "role", "instructions")},
                [5000],
            ),
        ),
        (
            "Use factory_job_status with status='needs_attention' and question when you need "
            "user input or approval, or status='failed' and reason if the job cannot be "
            "completed. You can call factory_job_status with status='done' and a short "
            "summary; finishing normally without calling it also completes the job as done."
        ),
    ]
    return "\n\n".join(parts)


def build_job_run_message(job_run: Json) -> str:
    """Build the initial prompt message for a Job Run."""
    parts: list[str] = []
    job_name = str(job_run.get("job_name") or "").strip()
    if job_name:
        parts.append(f"Job: {job_name}")
    prompt = str(job_run.get("prompt") or "").strip()
    if prompt:
        parts.append(prompt)
    input_note = str(job_run.get("input_note") or "").strip()
    if input_note:
        parts.append(_data("Input note", _bounded(input_note, [6000])))
    trigger_event = job_run.get("trigger_event")
    if trigger_event is not None:
        parts.append(_data("Trigger event", _bounded(trigger_event, [8000])))
    return "\n\n".join(parts)
