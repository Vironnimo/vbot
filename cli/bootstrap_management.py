"""Bootstrap management RPC commands for the vBot CLI."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from cli.formatting import string_or_default as _string_or_default
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

RUN_AGENT_ENV = "VBOT_RUN_AGENT_ID"
RUN_SESSION_ENV = "VBOT_RUN_SESSION_ID"
RUN_PROJECT_ENV = "VBOT_RUN_PROJECT_ID"
BOOTSTRAP_UPDATE_FLAGS = (
    "--agent",
    "--name",
    "--prompt",
    "--mode",
    "--session",
    "--clear-session",
)
_PROMPT_PREVIEW_LIMIT = 60


def bootstrap_create(
    instance: ServerInstance,
    fields: Mapping[str, Any],
    *,
    current_session: bool = False,
) -> CommandResult:
    params = dict(fields)
    if current_session:
        current = _current_run_target(instance)
        if isinstance(current, CommandResult):
            return current
        params.update(current)
    payload = _rpc_call(instance, "bootstrap.create", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_operation("created", payload.data),
        instance=instance,
    )


def bootstrap_list(instance: ServerInstance) -> CommandResult:
    payload = _rpc_call(instance, "bootstrap.list", {})
    if not payload.ok:
        return payload.to_command_result()
    jobs = payload.data.get("jobs")
    if not isinstance(jobs, list):
        return CommandResult(ok=False, message="RPC result missing jobs list", instance=instance)
    return CommandResult(ok=True, message=_format_rows(jobs), instance=instance)


def bootstrap_update(
    instance: ServerInstance, job_id: str, changes: Mapping[str, Any]
) -> CommandResult:
    if not changes:
        return CommandResult(
            ok=False,
            message=(
                f"no Bootstrap fields provided; use one of: {', '.join(BOOTSTRAP_UPDATE_FLAGS)}"
            ),
            instance=instance,
        )
    payload = _rpc_call(instance, "bootstrap.update", {"id": job_id, **dict(changes)})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_operation("updated", payload.data, fallback_id=job_id),
        instance=instance,
    )


def bootstrap_delete(instance: ServerInstance, job_id: str) -> CommandResult:
    return _simple_command(instance, "delete", job_id)


def bootstrap_enable(instance: ServerInstance, job_id: str) -> CommandResult:
    return _simple_command(instance, "enable", job_id)


def bootstrap_disable(instance: ServerInstance, job_id: str) -> CommandResult:
    return _simple_command(instance, "disable", job_id)


def _simple_command(instance: ServerInstance, command: str, job_id: str) -> CommandResult:
    payload = _rpc_call(instance, f"bootstrap.{command}", {"id": job_id})
    if not payload.ok:
        return payload.to_command_result()
    action = {"delete": "deleted", "enable": "enabled", "disable": "disabled"}[command]
    if command == "delete":
        message = f"{action} Bootstrap job {job_id}"
    else:
        message = _format_operation(action, payload.data, fallback_id=job_id)
    return CommandResult(ok=True, message=message, instance=instance)


def _current_run_target(instance: ServerInstance) -> dict[str, str] | CommandResult:
    if instance.host not in {"127.0.0.1", "localhost", "::1"}:
        return CommandResult(
            ok=False,
            message="--current-session cannot target a remote vBot server",
            instance=instance,
        )
    agent_id = os.environ.get(RUN_AGENT_ENV, "").strip()
    session_id = os.environ.get(RUN_SESSION_ENV, "").strip()
    project_id = os.environ.get(RUN_PROJECT_ENV, "").strip()
    if not agent_id or not session_id:
        return CommandResult(
            ok=False,
            message=(
                "--current-session is only available inside a vBot Run Bash command; "
                f"missing {RUN_AGENT_ENV} or {RUN_SESSION_ENV}"
            ),
            instance=instance,
        )
    target = f"{agent_id}@{project_id}" if project_id else agent_id
    return {"agent_id": target, "session_id": session_id}


def _format_rows(jobs: Sequence[object]) -> str:
    if not jobs:
        return "no Bootstrap jobs configured"
    return "\n".join(["Bootstrap jobs:", *(_format_row(job) for job in jobs)])


def _format_row(job: object) -> str:
    if not isinstance(job, dict):
        return "- invalid Bootstrap job entry"
    return (
        f"- name={_string_or_default(job.get('name'), _prompt_preview(job.get('prompt')))}"
        f" id={_string_or_default(job.get('id'), '?')}"
        f" agent={_string_or_default(job.get('target'), '?')}"
        f" mode={_string_or_default(job.get('mode'), '?')}"
        f" status={_string_or_default(job.get('status'), '?')}"
        f" session={_string_or_default(job.get('session_id'), 'new')}"
        f" last_outcome={_string_or_default(job.get('last_outcome'), '-')}"
        f" last_error={_string_or_default(job.get('last_error'), '-')}"
        f" prompt={_prompt_preview(job.get('prompt'))}"
    )


def _format_operation(action: str, job: Mapping[str, Any], *, fallback_id: str = "?") -> str:
    job_id = _string_or_default(job.get("id"), fallback_id)
    if set(job) <= {"id", "ok"}:
        return f"{action} Bootstrap job {job_id}"
    name = _string_or_default(job.get("name"), _prompt_preview(job.get("prompt")))
    return f"{action} Bootstrap job {name} ({job_id})\n{_format_row(job)}"


def _prompt_preview(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    collapsed = " ".join(value.split())
    if len(collapsed) <= _PROMPT_PREVIEW_LIMIT:
        return collapsed
    return collapsed[: _PROMPT_PREVIEW_LIMIT - 3] + "..."
