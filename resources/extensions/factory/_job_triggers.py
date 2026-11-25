"""Job triggers: host Extension triggers that create Factory Job Runs.

Each trigger of a saved Job is one host trigger keyed
``job:<factory id>:<job id>:<trigger id>``; Cron owns its schedule, webhook URL,
secret and catch-up. A fire creates one Job Run per webhook request (up to 20
newest), or one Job Run per scheduled fire, with the event condensed so Agents
can read it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from core.utils.logging import get_logger

from .store import FactoryError

if TYPE_CHECKING:
    from .extension import FactoryExtension

type Json = dict[str, Any]
_LOGGER = get_logger("extensions.factory")
_MAX_RUNS_PER_FIRE = 20
_MAX_COMMITS = 20
_TEXT_LIMIT = 4000


def trigger_key(factory_id: str, job_id: str, trigger_id: str) -> str:
    """Return the host trigger key for a Factory Job trigger."""
    return f"job:{factory_id}:{job_id}:{trigger_id}"


def _short(value: Any, limit: int = _TEXT_LIMIT) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + " …"
    return value


def _pick(value: Any, *fields: str) -> Json:
    if not isinstance(value, dict):
        return {}
    return {field: _short(value[field]) for field in fields if value.get(field) not in (None, "")}


def condense(headers: Json, body: Any) -> Json:
    """Extract the parts of a webhook request an Agent needs, GitHub-aware; raw text is cut."""
    event = next(
        (
            str(value)
            for name, value in headers.items()
            if name.lower().startswith("x-") and name.lower().endswith("-event")
        ),
        "",
    )
    result: Json = {"event": event} if event else {}
    if not isinstance(body, dict):
        if body not in (None, ""):
            text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            result["body"] = _short(text)
        return result
    repository = body.get("repository")
    if isinstance(repository, dict):
        result["repository"] = _pick(
            repository, "full_name", "html_url", "clone_url", "default_branch"
        )
    for field in ("action", "ref", "before", "after", "compare", "base_ref"):
        if isinstance(body.get(field), (str, int)) and body[field] != "":
            result[field] = _short(body[field])
    if isinstance(body.get("pusher"), dict):
        result["pusher"] = _pick(body["pusher"], "name")
    if isinstance(body.get("sender"), dict):
        result["sender"] = _pick(body["sender"], "login")
    commits = body.get("commits")
    if isinstance(commits, list):
        result["commits"] = [
            _pick(commit, "id", "message", "url") for commit in commits[:_MAX_COMMITS]
        ]
        if len(commits) > _MAX_COMMITS:
            result["more_commits"] = len(commits) - _MAX_COMMITS
    for field in ("pull_request", "issue"):
        if isinstance(body.get(field), dict):
            item = body[field]
            result[field] = _pick(item, "number", "title", "html_url", "state", "body") | {
                key: item[key]["ref"]
                for key in ("head", "base")
                if isinstance(item.get(key), dict) and isinstance(item[key].get("ref"), str)
            }
    if isinstance(body.get("comment"), dict):
        result["comment"] = _pick(body["comment"], "body", "html_url")
    if isinstance(body.get("release"), dict):
        result["release"] = _pick(body["release"], "tag_name", "name", "html_url")
    if len(result) <= (1 if event else 0):
        # Not a payload this knows: hand over the JSON itself, cut.
        result["body"] = _short(json.dumps(body, ensure_ascii=False))
    return result


def _parse_key(key: str) -> tuple[str, str, str] | None:
    parts = key.split(":", 3)
    if len(parts) != 4 or parts[0] != "job" or not all(parts[1:]):
        return None
    return parts[1], parts[2], parts[3]


class FactoryJobTriggers:
    """Keeps host triggers in sync with saved Factory Jobs and handles trigger fires."""

    def __init__(self, service: FactoryExtension) -> None:
        self.service = service

    @property
    def host_triggers(self) -> Any:
        host = self.service.host
        return None if host is None else host.triggers

    def check(self, job: Json) -> None:
        """Validate a Job's triggers and refuse triggers when scheduling is unavailable."""
        triggers = self.host_triggers
        job_triggers = job.get("triggers") or []
        for trigger in job_triggers:
            if trigger["type"] != "schedule":
                continue
            if triggers is None:
                raise FactoryError(
                    "triggers_unavailable",
                    "Triggers are unavailable because scheduling is off. "
                    "Remove the trigger or enable scheduling.",
                )
            try:
                triggers.check_schedule(trigger["schedule"])
            except ValueError as error:
                label = trigger.get("title") or trigger["id"]
                raise FactoryError(
                    "invalid_schedule",
                    f"Job {job['name']!r}, trigger {label!r}: {str(error).rstrip('.')}.",
                ) from error
        if triggers is None and job_triggers:
            raise FactoryError(
                "triggers_unavailable",
                "Triggers are unavailable because scheduling is off. "
                "Remove the trigger or enable scheduling.",
            )

    async def sync(self, jobs: list[Json] | None = None) -> None:
        """Reconcile host triggers to match all saved Jobs, deleting obsolete ones."""
        triggers = self.host_triggers
        if triggers is None:
            return
        if jobs is None:
            jobs = await self.service.records().jobs.list_all_jobs()
        wanted: dict[str, Json] = {}
        for job in jobs:
            active_job = job.get("factory_state", "active") == "active" and bool(job["enabled"])
            for trigger in job.get("triggers") or []:
                key = trigger_key(job["factory_id"], job["id"], trigger["id"])
                title = trigger.get("title") or trigger["type"]
                wanted[key] = {
                    "name": f"{job['name']}: {title}"[:200],
                    "schedule": "webhook" if trigger["type"] == "webhook" else trigger["schedule"],
                    "enabled": bool(active_job and trigger["enabled"]),
                }
        existing = {item["key"]: item for item in triggers.list()}
        for key, item in existing.items():
            if key not in wanted:
                await triggers.delete(item["id"])
        for key, values in wanted.items():
            current = existing.get(key)
            try:
                if current is None:
                    await triggers.create(key, **values)
                elif current["status"] in {"completed", "missed"}:
                    # A completed/missed one-shot stays terminal across saves and reloads;
                    # an explicitly changed schedule creates a fresh trigger.
                    if current["schedule"] != values["schedule"]:
                        await triggers.delete(current["id"])
                        await triggers.create(key, **values)
                elif (current["schedule_type"] == "webhook") != (values["schedule"] == "webhook"):
                    # A webhook trigger keeps its URL; a changed type gets a new trigger.
                    await triggers.delete(current["id"])
                    await triggers.create(key, **values)
                elif (
                    current["name"],
                    current["enabled"],
                    "webhook" if current["schedule_type"] == "webhook" else current["schedule"],
                ) != (values["name"], values["enabled"], values["schedule"]):
                    updates = dict(values)
                    if current["status"] == "failed" and values["enabled"]:
                        # The failure stop survives reconciliation. Pause and enable
                        # the trigger explicitly to retry it.
                        updates.pop("enabled")
                    await triggers.update(current["id"], **updates)
            except ValueError, LookupError:
                _LOGGER.warning("Factory job trigger could not be synced (trigger=%s)", key)

    def _host_by_key(self) -> dict[str, Json]:
        triggers = self.host_triggers
        if triggers is None:
            return {}
        return {item["key"]: item for item in triggers.list()}

    def enrich_job(self, job: Json) -> Json:
        """Return a copy of ``job`` with live host trigger status attached to each trigger."""
        by_key = self._host_by_key()
        enriched_triggers: list[Json] = []
        for trigger in job.get("triggers") or []:
            key = trigger_key(job["factory_id"], job["id"], trigger["id"])
            item = by_key.get(key)
            webhook = item.get("webhook") if item is not None else None
            enriched_triggers.append(
                {
                    **trigger,
                    "status": item.get("status") if item is not None else None,
                    "next_fire": item.get("next_fire_at") if item is not None else None,
                    "last_fire": item.get("last_fired_at") if item is not None else None,
                    "last_error": item.get("last_error") if item is not None else None,
                    "webhook_url": webhook.get("url") if isinstance(webhook, dict) else None,
                    "webhook_path": webhook.get("path") if isinstance(webhook, dict) else None,
                }
            )
        return {**job, "triggers": enriched_triggers}

    async def upcoming(self, factory_id: str, limit: int = 10) -> list[Json]:
        """Return upcoming scheduled triggers for ``factory_id`` sorted by ``next_fire``."""
        if self.host_triggers is None or limit <= 0:
            return []
        by_key = self._host_by_key()
        jobs = await self.service.records().jobs.list_all_jobs()
        items: list[Json] = []
        for job in jobs:
            if (
                job["factory_id"] != factory_id
                or job.get("factory_state", "active") != "active"
                or not job["enabled"]
            ):
                continue
            for trigger in job.get("triggers") or []:
                if trigger["type"] != "schedule" or not trigger["enabled"]:
                    continue
                host_item = by_key.get(trigger_key(factory_id, job["id"], trigger["id"]))
                if host_item is None or host_item.get("status") != "active":
                    continue
                next_fire = host_item.get("next_fire_at")
                if not next_fire:
                    continue
                items.append(
                    {
                        "job_id": job["id"],
                        "job_name": job["name"],
                        "trigger_id": trigger["id"],
                        "trigger_title": trigger.get("title") or "",
                        "schedule": trigger["schedule"],
                        "next_fire": next_fire,
                        "agent_member_id": job["agent_member_id"],
                    }
                )
        items.sort(key=lambda entry: (entry["next_fire"], entry["job_id"], entry["trigger_id"]))
        return items[:limit]

    async def secret(
        self, factory_id: str, job_id: str, trigger_id: str, *, rotate: bool = False
    ) -> Json:
        """Read or rotate the webhook signing secret for a Job trigger."""
        triggers = self.host_triggers
        found = self._host_by_key().get(trigger_key(factory_id, job_id, trigger_id))
        if triggers is None or found is None or found.get("webhook") is None:
            raise FactoryError(
                "trigger_not_found",
                "That webhook trigger does not exist. Save the Job with the trigger first.",
            )
        value = (
            await triggers.rotate_webhook_secret(found["id"])
            if rotate
            else await triggers.webhook_secret(found["id"])
        )
        return {"secret": str(value)}

    async def fire(self, fire_event: Json) -> None:
        """Create Job Runs for one trigger fire; raising lets Cron retry its requests."""
        key = str(fire_event.get("key", ""))
        parsed = _parse_key(key)
        if parsed is None:
            return
        factory_id, job_id, trigger_id = parsed
        service = self.service
        async with service.lock:
            records = service.records()
            native = getattr(records, "native", None)
            if native is not None:
                try:
                    factory = await native.get_factory(factory_id)
                except FactoryError:
                    _LOGGER.warning("Factory job trigger fired for missing Factory (key=%s)", key)
                    return
                if factory.get("state") != "active":
                    return
            jobs = records.jobs
            try:
                job = await jobs.get_job(job_id)
            except FactoryError:
                _LOGGER.warning("Factory job trigger fired for a deleted Job (key=%s)", key)
                return
            if job["factory_id"] != factory_id:
                return
            trigger = next(
                (item for item in (job.get("triggers") or []) if item["id"] == trigger_id),
                None,
            )
            if (
                trigger is None
                or job.get("factory_state", "active") != "active"
                or not job["enabled"]
                or not trigger["enabled"]
            ):
                return
            requests = fire_event.get("requests") or []
            base = {
                "factory_id": factory_id,
                "job_id": job_id,
                "trigger_id": trigger_id,
                "type": trigger["type"],
                "fired_at": fire_event.get("fired_at"),
            }
            events: list[tuple[Json, str]] = []
            if trigger["type"] == "webhook":
                for idx, request in enumerate(requests[-_MAX_RUNS_PER_FIRE:]):
                    condensed = condense(request.get("headers") or {}, request.get("body"))
                    receipt = (
                        f"{fire_event['trigger_id']}:request:{request['seq']}"
                        if fire_event.get("trigger_id") and "seq" in request
                        else f"webhook:{key}:{fire_event.get('fired_at') or 'fire'}:{idx}"
                    )
                    events.append(
                        (
                            base
                            | {
                                "received_at": request.get("received_at"),
                                "request": condensed,
                                **({"cut": True} if request.get("cut") else {}),
                            },
                            receipt,
                        )
                    )
                skipped = len(requests) - len(events) + int(fire_event.get("dropped") or 0)
                if skipped and events:
                    events[0][0]["skipped_requests"] = skipped
            else:
                receipt = (
                    f"{fire_event['trigger_id']}:due:{fire_event['due_at']}"
                    if fire_event.get("trigger_id") and fire_event.get("due_at")
                    else (
                        f"schedule:{key}:"
                        f"{fire_event.get('due_at') or fire_event.get('fired_at') or 'fire'}"
                    )
                )
                events.append(
                    (
                        base
                        | {
                            "due_at": fire_event.get("due_at"),
                            **(
                                {"missed": fire_event["earlier_missed"]}
                                if fire_event.get("earlier_missed")
                                else {}
                            ),
                        },
                        receipt,
                    )
                )
            created_runs: list[Json] = []
            for event, receipt in events:
                run = await jobs.create_triggered_run(
                    factory_id,
                    job_id,
                    origin=trigger["type"],
                    trigger_id=trigger_id,
                    receipt=receipt,
                    trigger_event=event,
                )
                if run is not None:
                    created_runs.append(run)
            if created_runs:
                host = service.host
                if host is not None and host.publish_change is not None:
                    for run in created_runs:
                        host.publish_change("job_runs", [run["id"]], run.get("revision", 1))
                service.request_schedule()
                _LOGGER.info(
                    "Factory job trigger fired (factory=%s job=%s trigger=%s runs=%d)",
                    factory_id,
                    job_id,
                    trigger_id,
                    len(created_runs),
                )
