"""Cron job management RPC commands for the vBot CLI."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from cli.formatting import record_fields
from cli.formatting import string_or_default as _string_or_default
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

CRON_UPDATE_FLAGS = (
    "--agent",
    "--name",
    "--prompt",
    "--cron",
    "--every",
    "--at",
    "--event",
    "--webhook",
    "--event-time",
    "--repeat",
    "--session",
    "--clear-session",
    "--working-project",
    "--no-project",
    "--model",
    "--clear-model",
    "--thinking-effort",
    "--clear-thinking-effort",
    "--status",
    "--deliver",
    "--watch",
    "--clear-watch",
    "--watch-runs",
)
_PROMPT_PREVIEW_LIMIT = 60
# Who can send requests to a webhook job's URL, by the reach the server reports.
_WEBHOOK_REACH = {
    "internet": "reachable from the internet",
    "network": "reachable from this computer and the home network",
    "computer": "reachable from this computer only",
}


def cron_create(instance: ServerInstance, fields: Mapping[str, Any]) -> CommandResult:
    """Create a cron job via `cron.create` RPC."""

    payload = _rpc_call(instance, "cron.create", dict(fields))
    if not payload.ok:
        return payload.to_command_result()
    message = _format_job_operation("created", payload.data)
    if fields.get("schedule_type") == "webhook":
        message += "\n" + _new_webhook_secret(instance, payload.data)
    return CommandResult(ok=True, message=message, instance=instance)


def cron_list(instance: ServerInstance) -> CommandResult:
    """Return formatted cron job list output from `cron.list` RPC."""

    payload = _rpc_call(instance, "cron.list", {})
    if not payload.ok:
        return payload.to_command_result()
    jobs = payload.data.get("jobs")
    if not isinstance(jobs, list):
        return CommandResult(ok=False, message="RPC result missing jobs list", instance=instance)
    return CommandResult(ok=True, message=_format_job_rows(jobs), instance=instance)


def cron_show(instance: ServerInstance, job_id: str) -> CommandResult:
    """Read the complete saved job, including its prompt and execution state."""
    payload = _rpc_call(instance, "cron.list", {})
    if not payload.ok:
        return payload.to_command_result()
    jobs = payload.data.get("jobs")
    if not isinstance(jobs, list):
        return CommandResult(ok=False, message="RPC result missing jobs list", instance=instance)
    job = next((item for item in jobs if isinstance(item, dict) and item.get("id") == job_id), None)
    if job is None:
        return CommandResult(
            ok=False,
            message=f"cron job not found: {job_id}; use vbot cron list with the same target",
            instance=instance,
        )
    return CommandResult(
        ok=True, message=json.dumps(job, ensure_ascii=False, indent=2), instance=instance
    )


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
    changes = dict(changes)
    watch = changes.get("watch")
    if isinstance(watch, dict) and ("always" not in watch or len(watch) == 1):
        # The RPC replaces the whole watch: keep what this call does not name.
        stored, refusal = _stored_watch(instance, job_id)
        if refusal is not None:
            return refusal
        if len(watch) == 1 and "always" in watch:
            if stored is None:
                return CommandResult(
                    ok=False,
                    message=f"--watch-runs needs a watch, and cron job {job_id} has none; add "
                    "--watch <command-or-url>. Nothing was changed.",
                    instance=instance,
                )
            changes["watch"] = {**stored, "always": watch["always"]}
        elif stored is not None and stored.get("always") is True:
            changes["watch"] = {**watch, "always": True}
    payload = _rpc_call(instance, "cron.update", {"id": job_id, **changes})
    if not payload.ok:
        return payload.to_command_result()
    message = _format_job_operation("updated", payload.data, fallback_id=job_id)
    if changes.get("schedule_type") == "webhook":
        message += "\n" + _new_webhook_secret(instance, {"id": job_id, **payload.data})
    return CommandResult(ok=True, message=message, instance=instance)


def _stored_watch(
    instance: ServerInstance, job_id: str
) -> tuple[dict[str, Any] | None, CommandResult | None]:
    """The job's stored watch, or the result that refuses the update."""
    shown = cron_show(instance, job_id)
    if not shown.ok:
        return None, shown
    watch = json.loads(shown.message).get("watch")
    return (watch if isinstance(watch, dict) else None), None


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
    return CommandResult(
        ok=True,
        message=_format_job_operation("enabled", payload.data, fallback_id=job_id),
        instance=instance,
    )


def cron_disable(instance: ServerInstance, job_id: str) -> CommandResult:
    """Disable a cron job via `cron.disable` RPC."""

    payload = _rpc_call(instance, "cron.disable", {"id": job_id})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_job_operation("disabled", payload.data, fallback_id=job_id),
        instance=instance,
    )


def cron_secret(instance: ServerInstance, job_id: str, *, rotate: bool) -> CommandResult:
    """Show or replace a webhook job's secret via `cron.webhook_secret` or
    `cron.rotate_webhook_secret` RPC."""

    method = "cron.rotate_webhook_secret" if rotate else "cron.webhook_secret"
    payload = _rpc_call(instance, method, {"id": job_id})
    if not payload.ok:
        return payload.to_command_result()
    if rotate:
        first = (
            f"replaced the secret of cron job {job_id}; requests that send the old secret "
            "are refused from now on"
        )
    else:
        first = f"secret of cron job {job_id}"
    return CommandResult(
        ok=True,
        message=f"{first}\n{_format_webhook_secret(job_id, payload.data)}",
        instance=instance,
    )


def _new_webhook_secret(instance: ServerInstance, job: Mapping[str, Any]) -> str:
    """The secret lines of a job that just became a webhook job."""
    job_id = _string_or_default(job.get("id"), "?")
    payload = _rpc_call(instance, "cron.webhook_secret", {"id": job_id})
    if not payload.ok:
        return (
            f"Note: the job's secret could not be read ({payload.message.rstrip('. ')}); "
            f"vbot cron secret {job_id} shows it."
        )
    return _format_webhook_secret(job_id, payload.data)


def _format_webhook_secret(job_id: str, data: Mapping[str, Any]) -> str:
    secret = _string_or_default(data.get("secret"), "?")
    webhook = data.get("webhook")
    webhook = webhook if isinstance(webhook, dict) else {}
    url = webhook.get("url")
    lines = []
    if isinstance(url, str) and url:
        reach = _WEBHOOK_REACH.get(str(webhook.get("reach")))
        lines.append(f"url: {url}" + (f" ({reach})" if reach else ""))
    else:
        path = _string_or_default(webhook.get("path"), f"/api/hooks/{job_id}")
        lines.append(f"url: {path} on vBot's server address")
    lines.append(f"secret: {secret}")
    lines.append(
        f'Send each request as POST to url with the header "Authorization: Bearer {secret}", '
        "or enter secret as the secret of a GitHub webhook (X-Hub-Signature-256)."
    )
    if webhook.get("reach") in {"network", "computer"}:
        lines.append(
            "Note: services on the internet, such as GitHub, cannot reach this URL until the "
            "user opens vBot to the internet (vbot application access show)."
        )
    return "\n".join(lines)


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
    name = _string_or_default(job.get("name"), _prompt_preview(job.get("prompt")))
    # Prefer the server-provided address form so a project target shows as
    # ``builder@projekt`` and a bare identity target stays ``builder``; fall back
    # to the raw agent id for older payloads without ``target``.
    agent_id = _string_or_default(job.get("target"), _string_or_default(job.get("agent_id"), "?"))
    status = _string_or_default(job.get("status"), "?")
    schedule = _format_schedule(job)
    raw_remaining_runs = job.get("remaining_runs")
    if isinstance(raw_remaining_runs, int):
        remaining_runs = str(raw_remaining_runs)
    else:
        remaining_runs = "1" if job.get("schedule_type") == "once" else "unlimited"
    next_fire_at = _string_or_default(job.get("next_fire_at"), "-")
    last_outcome = _string_or_default(job.get("last_outcome"), "-")
    prompt = _prompt_preview(job.get("prompt"))
    return record_fields(
        [
            f"- name={name}",
            f" id={job_id}",
            f" agent={agent_id}",
            f" status={status}",
            f" schedule={schedule}",
            f" remaining_runs={remaining_runs}",
            f" next_fire_at={next_fire_at}",
            f" session={job.get('session_id') or 'new'}",
            *_format_execution(job),
            f" delivery={_format_delivery(job.get('delivery'))}",
            f" watch={_format_watch(job.get('watch'))}",
            f" last_outcome={last_outcome}",
            f" last_error={job.get('last_error') or '-'}",
            *_format_alert(job.get("unread_alert")),
            f" prompt={prompt}",
        ],
        separator="",
    )


def _format_execution(job: Mapping[str, Any]) -> list[str]:
    """The saved choices, including an explicit Workspace and Model default."""
    fields: list[str] = []
    if "working_project_id" in job:
        fields.append(f" working_project={job['working_project_id'] or 'Workspace'}")
    overrides = job.get("run_overrides")
    if isinstance(overrides, dict):
        if overrides.get("model"):
            fields.append(f" model={overrides['model']}")
        if overrides.get("thinking_effort_default"):
            fields.append(" thinking_effort=default")
        elif overrides.get("thinking_effort"):
            fields.append(f" thinking_effort={overrides['thinking_effort']}")
    return fields


def _format_job_operation(action: str, job: Mapping[str, Any], *, fallback_id: str = "?") -> str:
    job_id = _string_or_default(job.get("id"), fallback_id)
    if set(job) <= {"id", "ok"}:
        return f"{action} cron job {job_id}"
    name = _string_or_default(job.get("name"), _prompt_preview(job.get("prompt")))
    return f"{action} cron job {name} ({job_id})\n{_format_job_row(job)}"


def _format_schedule(job: Mapping[str, Any]) -> str:
    schedule_type = job.get("schedule_type")
    if schedule_type == "cron":
        return f"cron[{_string_or_default(job.get('cron_expression'), '?')}]"
    if schedule_type == "interval":
        return f"interval[{_string_or_default(job.get('schedule'), '?')}]"
    if schedule_type == "once":
        return f"once[{_string_or_default(job.get('run_at'), '?')}]"
    if schedule_type == "event":
        event_id = _string_or_default(job.get("event_id"), "?")
        return f"event[{event_id} {_string_or_default(job.get('schedule'), '?')}]"
    if schedule_type == "webhook":
        webhook = job.get("webhook")
        address = (webhook.get("url") or webhook.get("path")) if isinstance(webhook, dict) else None
        return f"webhook[{_string_or_default(address, '?')}]"
    return "?"


def _format_delivery(delivery: object) -> str:
    if not isinstance(delivery, dict):
        return "none"
    kind = _string_or_default(delivery.get("kind"), "none")
    if kind != "chat":
        return kind
    target = " ".join(
        str(delivery[name])
        for name in ("channel_id", "platform_target", "thread_id")
        if delivery.get(name)
    )
    return f"chat[{target}]"


def _format_alert(alert: object) -> list[str]:
    """The unread alert of a job, only while it has one."""
    if not isinstance(alert, dict) or not isinstance(alert.get("message"), str):
        return []
    return [f" alert={alert['message']}"]


def _format_watch(watch: object) -> str:
    if not isinstance(watch, dict):
        return "-"
    runs = ",always" if watch.get("always") is True else ""
    if isinstance(watch.get("url"), str):
        return f"url[{watch['url']}]{runs}"
    return f"command[{_prompt_preview(watch.get('command'))}]{runs}"


def _prompt_preview(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    collapsed = " ".join(value.split())
    if len(collapsed) <= _PROMPT_PREVIEW_LIMIT:
        return collapsed
    return collapsed[: _PROMPT_PREVIEW_LIMIT - 3] + "..."
