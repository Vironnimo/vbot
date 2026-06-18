"""Cron job management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

CRON_UPDATE_FLAGS = (
    "--agent",
    "--prompt",
    "--cron",
    "--at",
    "--timezone",
    "--session",
    "--status",
)
_PROMPT_PREVIEW_LIMIT = 60


def cron_create(instance: ServerInstance, fields: Mapping[str, Any]) -> CommandResult:
    """Create a cron job via `cron.create` RPC."""

    payload = _rpc_call(instance, "cron.create", dict(fields))
    if not payload.ok:
        return payload.to_command_result()
    created_id = _string_or_default(payload.data.get("id"), "?")
    return CommandResult(ok=True, message=f"created cron job {created_id}", instance=instance)


def cron_list(instance: ServerInstance) -> CommandResult:
    """Return formatted cron job list output from `cron.list` RPC."""

    payload = _rpc_call(instance, "cron.list", {})
    if not payload.ok:
        return payload.to_command_result()
    jobs = payload.data.get("jobs")
    if not isinstance(jobs, list):
        return CommandResult(ok=False, message="RPC result missing jobs list", instance=instance)
    return CommandResult(ok=True, message=_format_job_rows(jobs), instance=instance)


def cron_update(
    instance: ServerInstance,
    job_id: str,
    changes: Mapping[str, Any],
) -> CommandResult:
    """Update a cron job via `cron.update` RPC."""

    if not changes:
        return CommandResult(
            ok=False,
            message=f"no cron fields provided; use one of: {', '.join(CRON_UPDATE_FLAGS)}",
            instance=instance,
        )
    payload = _rpc_call(instance, "cron.update", {"id": job_id, **dict(changes)})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(ok=True, message=f"updated cron job {job_id}", instance=instance)


def cron_delete(instance: ServerInstance, job_id: str) -> CommandResult:
    """Delete a cron job via `cron.delete` RPC."""

    payload = _rpc_call(instance, "cron.delete", {"id": job_id})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(ok=True, message=f"deleted cron job {job_id}", instance=instance)


def cron_enable(instance: ServerInstance, job_id: str) -> CommandResult:
    """Enable a cron job via `cron.enable` RPC."""

    payload = _rpc_call(instance, "cron.enable", {"id": job_id})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(ok=True, message=f"enabled cron job {job_id}", instance=instance)


def cron_disable(instance: ServerInstance, job_id: str) -> CommandResult:
    """Disable a cron job via `cron.disable` RPC."""

    payload = _rpc_call(instance, "cron.disable", {"id": job_id})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(ok=True, message=f"disabled cron job {job_id}", instance=instance)


def _format_job_rows(jobs: Sequence[object]) -> str:
    if not jobs:
        return "no cron jobs configured"

    lines = ["cron jobs:"]
    for job in jobs:
        lines.append(_format_job_row(job))
    return "\n".join(lines)


def _format_job_row(job: object) -> str:
    if not isinstance(job, dict):
        return "- invalid cron job entry"

    job_id = _string_or_default(job.get("id"), "?")
    # Prefer the server-provided address form so a project target shows as
    # ``builder@projekt`` and a bare identity target stays ``builder``; fall back
    # to the raw agent id for older payloads without ``target``.
    agent_id = _string_or_default(job.get("target"), _string_or_default(job.get("agent_id"), "?"))
    status = _string_or_default(job.get("status"), "?")
    schedule = _format_schedule(job)
    next_fire_at = _string_or_default(job.get("next_fire_at"), "-")
    prompt = _prompt_preview(job.get("prompt"))
    return (
        f"- id={job_id}"
        f" agent={agent_id}"
        f" status={status}"
        f" schedule={schedule}"
        f" next_fire_at={next_fire_at}"
        f" prompt={prompt}"
    )


def _format_schedule(job: Mapping[str, Any]) -> str:
    schedule_type = job.get("schedule_type")
    if schedule_type == "cron":
        return f"cron[{_string_or_default(job.get('cron_expression'), '?')}]"
    if schedule_type == "once":
        return f"once[{_string_or_default(job.get('run_at'), '?')}]"
    return "?"


def _prompt_preview(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    collapsed = " ".join(value.split())
    if len(collapsed) <= _PROMPT_PREVIEW_LIMIT:
        return collapsed
    return collapsed[: _PROMPT_PREVIEW_LIMIT - 3] + "..."


def _string_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default
