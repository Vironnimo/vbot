"""Operator management operations and schemas for Simplified Factory Jobs and Dashboard."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.tools.arguments import optional_int
from core.tools.contracts import ToolContractError
from core.utils.logging import get_logger
from core.utils.workers import settle_before_cancelling

from ._jobs_store import _validate_name, _validate_triggers
from ._values import text
from .store import FactoryError

if TYPE_CHECKING:
    from ._job_execution import FactoryJobExecution
    from ._job_triggers import FactoryJobTriggers
    from .extension import FactoryExtension

type Json = dict[str, Any]
_LOGGER = get_logger("extensions.factory")

_S = {"type": "string"}
_B = {"type": "boolean"}
_INT = {"type": "integer", "minimum": 1}
_TRIGGER = {
    "type": "object",
    "properties": {
        "id": _S,
        "type": {"type": "string", "enum": ["schedule", "webhook"]},
        "title": _S,
        "schedule": _S,
        "enabled": _B,
    },
}


def _schema(properties: Json, required: tuple[str, ...] = ()) -> Json:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


JOB_SECRET_OPERATIONS: frozenset[str] = frozenset({"job.secret"})

JOB_OPERATION_SCHEMAS: dict[str, Json] = {
    "job.list": _schema(
        {"factory_id": _S, "include_triggers_status": _B},
        ("factory_id",),
    ),
    "job.get": _schema({"job_id": _S}, ("job_id",)),
    "job.create": _schema(
        {
            "factory_id": _S,
            "name": _S,
            "prompt": _S,
            "description": _S,
            "agent_member_id": _S,
            "triggers": {"type": "array", "items": _TRIGGER},
            "enabled": _B,
        },
        ("factory_id", "name", "prompt"),
    ),
    "job.update": _schema(
        {
            "job_id": _S,
            "name": _S,
            "prompt": _S,
            "description": _S,
            "agent_member_id": _S,
            "triggers": {"type": "array", "items": _TRIGGER},
            "enabled": _B,
            "expected_revision": _INT,
        },
        ("job_id",),
    ),
    "job.delete": _schema({"job_id": _S}, ("job_id",)),
    "job.trigger": _schema(
        {"job_id": _S, "member_id": _S, "input_note": _S},
        ("job_id",),
    ),
    "job.secret": _schema(
        {"factory_id": _S, "job_id": _S, "trigger_id": _S, "rotate": _B},
        ("factory_id", "job_id", "trigger_id"),
    ),
    "job_run.list": _schema(
        {
            "factory_id": _S,
            "job_id": _S,
            "state": _S,
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        },
        ("factory_id",),
    ),
    "job_run.get": _schema({"run_id": _S}, ("run_id",)),
    "job_run.reply": _schema({"run_id": _S, "text": _S}, ("run_id", "text")),
    "job_run.cancel": _schema({"run_id": _S}, ("run_id",)),
    "dashboard.get": _schema(
        {
            "factory_id": _S,
            "recent_limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "upcoming_limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        ("factory_id",),
    ),
}

JOB_OPERATION_DESCRIPTIONS: dict[str, str] = {
    "job.list": "List Jobs for a Factory, optionally enriched with live host trigger status.",
    "job.get": "Read one Job and its live host trigger status.",
    "job.create": "Create a Factory Job, validate and sync its triggers.",
    "job.update": "Update a Factory Job, validate and sync its triggers.",
    "job.delete": "Delete a Factory Job and remove its host triggers.",
    "job.trigger": "Manually queue a new Job Run for a Job.",
    "job.secret": "Read or rotate the webhook signing secret for a Job trigger.",
    "job_run.list": "List Job Runs for a Factory, optionally filtered by job_id or state.",
    "job_run.get": "Read one Job Run.",
    "job_run.reply": "Send an operator reply to resume a waiting or failed Job Run.",
    "job_run.cancel": "Cancel an active or queued Job Run.",
    "dashboard.get": (
        "Read the Factory dashboard: team, jobs, attention/running/recent runs, and upcoming fires."
    ),
}


def _require_id(arguments: Json, field: str) -> str:
    return text(arguments, field).strip()


def _optional_str(arguments: Json, field: str) -> str | None:
    if field not in arguments or arguments[field] is None:
        return None
    value = arguments[field]
    if not isinstance(value, str):
        raise FactoryError("invalid_value", f"{field} must be a string.")
    return value


def _optional_bool(arguments: Json, field: str, *, default: bool | None = None) -> bool | None:
    if field not in arguments or arguments[field] is None:
        return default
    value = arguments[field]
    if not isinstance(value, bool):
        raise FactoryError("invalid_value", f"{field} must be a boolean.")
    return value


def _optional_int(
    arguments: Json,
    field: str,
    *,
    default: int | None = None,
    minimum: int = 1,
    maximum: int | None = None,
) -> int | None:
    if field not in arguments or arguments[field] is None:
        return default
    try:
        return optional_int(arguments[field], field_name=field, minimum=minimum, maximum=maximum)
    except ToolContractError as error:
        raise FactoryError("invalid_value", str(error)) from error


_READ_OPERATIONS = frozenset(
    {"job.list", "job.get", "job.secret", "job_run.list", "job_run.get", "dashboard.get"}
)


class FactoryJobManagement:
    """Operator operations for Simplified Factory Jobs, Job Runs, and the Dashboard."""

    def __init__(self, service: FactoryExtension) -> None:
        self.service = service

    @property
    def _triggers(self) -> FactoryJobTriggers:
        return self.service.job_triggers  # type: ignore[attr-defined, no-any-return]

    @property
    def _execution(self) -> FactoryJobExecution:
        return self.service.job_execution  # type: ignore[attr-defined, no-any-return]

    async def operation(self, name: str, arguments: Json) -> Json | None:
        if name not in JOB_OPERATION_SCHEMAS:
            return None
        if name == "job.secret" and bool(arguments.get("rotate")):
            async with self.service.lock:
                return await settle_before_cancelling(self._read_operation(name, arguments))
        if name in _READ_OPERATIONS:
            return await self._read_operation(name, arguments)
        if name == "job_run.reply":
            run_id = _require_id(arguments, "run_id")
            await self._execution.wait_for_turn_settle(run_id)
        if name == "job_run.cancel":
            run_id = _require_id(arguments, "run_id")
            group_to_close: str | None = None
            async with self.service.lock:
                existing = await self.service.records().jobs.get_run(run_id)
                if existing["state"] in {"running", "needs_attention"}:
                    group_to_close = existing["group_id"]
                run = await settle_before_cancelling(
                    self._execution.cancel_run(run_id, close_inline=False)
                )
            if group_to_close is not None:
                await self._execution.close_group_safe(group_to_close, reason="cancelled")
            return {"run": run}
        async with self.service.lock:
            return await settle_before_cancelling(self._mutate_operation(name, arguments))

    def _publish_change(self, resource: str, ids: list[str], revision: int) -> None:
        host = getattr(self.service, "host", None)
        if (
            host is not None
            and getattr(host, "publish_change", None) is not None
            and not getattr(self.service, "_closing", False)
        ):
            host.publish_change(resource, ids, revision)

    async def _list_all_members(self, factory_id: str) -> list[Json]:
        native = self.service.records().native
        members: list[Json] = []
        after = ""
        while True:
            page = await native.list_members(factory_id, after=after, limit=100)
            members.extend(page)
            if len(page) < 100:
                return members
            after = page[-1]["id"]

    async def _read_operation(self, name: str, arguments: Json) -> Json:
        store = self.service.records()
        triggers = self._triggers

        if name == "job.list":
            factory_id = _require_id(arguments, "factory_id")
            await store.native.get_factory(factory_id)
            include_status = _optional_bool(arguments, "include_triggers_status", default=True)
            assert include_status is not None
            jobs = await store.jobs.list_jobs(factory_id)
            if include_status:
                jobs = [triggers.enrich_job(job) for job in jobs]
            return {"jobs": jobs}

        if name == "job.get":
            job = await store.jobs.get_job(_require_id(arguments, "job_id"))
            return {"job": triggers.enrich_job(job)}

        if name == "job.secret":
            factory_id = _require_id(arguments, "factory_id")
            job_id = _require_id(arguments, "job_id")
            trigger_id = _require_id(arguments, "trigger_id")
            rotate = _optional_bool(arguments, "rotate", default=False)
            assert rotate is not None
            return await triggers.secret(factory_id, job_id, trigger_id, rotate=rotate)

        if name == "job_run.list":
            factory_id = _require_id(arguments, "factory_id")
            await store.native.get_factory(factory_id)
            raw_job_id = _optional_str(arguments, "job_id")
            filter_job_id = (
                raw_job_id.strip() if raw_job_id is not None and raw_job_id.strip() else None
            )
            raw_state = _optional_str(arguments, "state")
            state = raw_state.strip() if raw_state is not None and raw_state.strip() else None
            limit = _optional_int(arguments, "limit", default=50, minimum=1, maximum=1000)
            assert limit is not None
            runs = await store.jobs.list_runs(
                factory_id, state=state, job_id=filter_job_id, limit=limit
            )
            return {"runs": runs}

        if name == "job_run.get":
            run = await store.jobs.get_run(_require_id(arguments, "run_id"))
            return {"run": run}

        # dashboard.get
        factory_id = _require_id(arguments, "factory_id")
        recent_limit = _optional_int(arguments, "recent_limit", default=20, minimum=1, maximum=1000)
        upcoming_limit = _optional_int(
            arguments, "upcoming_limit", default=10, minimum=1, maximum=100
        )
        assert recent_limit is not None and upcoming_limit is not None

        factory = await store.native.get_factory(factory_id)
        members = await self._list_all_members(factory_id)
        native_management = getattr(self.service, "native_management", None)
        if native_management is not None and hasattr(native_management, "identities"):
            members = await native_management.identities.member_profiles(members)
            leader = next(
                (member for member in members if member["id"] == factory.get("leader_member_id")),
                None,
            )
            factory = {
                **factory,
                "leader_available": bool(
                    leader is not None
                    and leader.get("state") == "active"
                    and leader.get("profile_available")
                ),
            }
        jobs = [triggers.enrich_job(job) for job in await store.jobs.list_jobs(factory_id)]
        needs_attention_runs = await store.jobs.list_runs(
            factory_id, state="needs_attention", limit=1000
        )
        failed_runs = await store.jobs.list_runs(factory_id, state="failed", limit=1000)
        needs_attention = sorted(
            [*needs_attention_runs, *failed_runs],
            key=lambda run: (run["created_at"], run["id"]),
            reverse=True,
        )
        running_runs = await store.jobs.list_runs(factory_id, state="running", limit=1000)
        queued_runs = await store.jobs.list_runs(factory_id, state="queued", limit=1000)
        running = sorted(
            [*running_runs, *queued_runs],
            key=lambda run: (run["created_at"], run["id"]),
            reverse=True,
        )
        upcoming = await triggers.upcoming(factory_id, limit=upcoming_limit)
        recent_runs = await store.jobs.list_runs(factory_id, limit=recent_limit)
        return {
            "factory": factory,
            "members": members,
            "jobs": jobs,
            "needs_attention": needs_attention,
            "running": running,
            "upcoming": upcoming,
            "recent_runs": recent_runs,
        }

    async def _mutate_operation(self, name: str, arguments: Json) -> Json:
        store = self.service.records()
        triggers = self._triggers

        if name == "job.create":
            factory_id = _require_id(arguments, "factory_id")
            raw_name = _optional_str(arguments, "name")
            if raw_name is None:
                raise FactoryError("missing_field", "name needs a non-empty text value.")
            clean_name = _validate_name(raw_name)
            raw_prompt = _optional_str(arguments, "prompt")
            if raw_prompt is None:
                raise FactoryError("missing_field", "prompt needs a non-empty text value.")
            description = _optional_str(arguments, "description")
            agent_member_id = _optional_str(arguments, "agent_member_id")
            enabled = _optional_bool(arguments, "enabled", default=True)
            assert enabled is not None
            clean_triggers = (
                _validate_triggers(arguments["triggers"]) if "triggers" in arguments else None
            )
            if clean_triggers:
                triggers.check({"name": clean_name, "triggers": clean_triggers})

            job = await store.jobs.create_job(
                factory_id,
                name=clean_name,
                prompt=raw_prompt,
                description="" if description is None else description,
                enabled=enabled,
                agent_member_id="" if agent_member_id is None else agent_member_id,
                triggers=clean_triggers,
            )
            await triggers.sync()
            self._publish_change("jobs", [job["id"]], job["revision"])
            return {"job": triggers.enrich_job(job)}

        if name == "job.update":
            job_id = _require_id(arguments, "job_id")
            existing = await store.jobs.get_job(job_id)
            raw_name = _optional_str(arguments, "name")
            next_name = _validate_name(raw_name) if raw_name is not None else None
            next_prompt = _optional_str(arguments, "prompt")
            next_description = _optional_str(arguments, "description")
            agent_member_id = _optional_str(arguments, "agent_member_id")
            enabled = _optional_bool(arguments, "enabled")
            expected_revision = _optional_int(arguments, "expected_revision", minimum=1)
            clean_triggers = (
                _validate_triggers(arguments["triggers"])
                if "triggers" in arguments and arguments["triggers"] is not None
                else None
            )
            if clean_triggers is not None:
                triggers.check(
                    {
                        "name": next_name if next_name is not None else existing["name"],
                        "triggers": clean_triggers,
                    }
                )

            job = await store.jobs.update_job(
                job_id,
                name=next_name,
                prompt=next_prompt,
                description=next_description,
                enabled=enabled,
                agent_member_id=agent_member_id,
                triggers=clean_triggers,
                expected_revision=expected_revision,
            )
            await triggers.sync()
            self._publish_change("jobs", [job["id"]], job["revision"])
            return {"job": triggers.enrich_job(job)}

        if name == "job.delete":
            job_id = _require_id(arguments, "job_id")
            await store.jobs.delete_job(job_id)
            await triggers.sync()
            self._publish_change("jobs", [job_id], 0)
            return {"deleted": True, "job_id": job_id}

        if name == "job.trigger":
            job_id = _require_id(arguments, "job_id")
            job = await store.jobs.get_job(job_id)
            raw_member_id = _optional_str(arguments, "member_id")
            member_id = (
                raw_member_id.strip()
                if raw_member_id is not None and raw_member_id.strip()
                else None
            )
            input_note = _optional_str(arguments, "input_note") or ""
            run = await store.jobs.create_run(
                job["factory_id"],
                job_id,
                origin="manual",
                member_id=member_id,
                input_note=input_note,
            )
            self._publish_change("job_runs", [run["id"]], run["revision"])
            self.service.request_schedule()
            _LOGGER.info(
                "Factory job triggered manually (factory=%s, job=%s, run=%s)",
                job["factory_id"],
                job_id,
                run["id"],
            )
            return {"run": run}

        if name == "job_run.reply":
            run_id = _require_id(arguments, "run_id")
            reply_text = text(arguments, "text")
            run = await self._execution.reply_run(run_id, reply_text)
            return {"run": run}

        # job_run.cancel
        run_id = _require_id(arguments, "run_id")
        run = await self._execution.cancel_run(run_id)
        return {"run": run}
