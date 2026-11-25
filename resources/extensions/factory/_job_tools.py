"""Private session Tools for Simplified Factory Jobs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.tools import ToolContext, tool_failure, tool_success
from core.tools.arguments import optional_bool, optional_int, optional_string
from core.tools.contracts import ToolContractError
from core.utils.logging import get_logger

from ._job_agent_text import JOB_STATUS_TOOL_NAME, LEADER_JOBS_TOOL_NAME
from ._jobs_store import FINAL_JOB_RUN_STATES, _validate_triggers
from .store import FactoryError

if TYPE_CHECKING:
    from .extension import FactoryExtension

type Json = dict[str, Any]
_LOGGER = get_logger("extensions.factory")
_JOB_STATUS_VALUES = frozenset({"needs_attention", "failed", "done"})


def _binding_group_id(context: ToolContext) -> str:
    binding = getattr(context, "binding", None)
    if binding is not None:
        group_id = getattr(binding, "group_id", None)
        if isinstance(group_id, str) and group_id:
            return group_id
    owner = getattr(context, "execution_owner", None)
    if owner is not None:
        extension = getattr(owner, "extension", "factory")
        if extension not in ("", "factory"):
            return ""
        group_id = getattr(owner, "group_id", None)
        if isinstance(group_id, str) and group_id:
            return group_id
    return ""


class FactoryJobTools:
    """Handlers for factory_job_status and factory_jobs private session Tools."""

    def __init__(self, service: FactoryExtension) -> None:
        self.service = service

    async def handle(self, name: str, context: ToolContext, arguments: Json) -> Json:
        if name == JOB_STATUS_TOOL_NAME:
            return await self._handle_job_status(context, arguments)
        if name == LEADER_JOBS_TOOL_NAME:
            return await self._handle_leader_jobs(context, arguments)
        return tool_failure("unknown_tool", f"Unknown Factory job tool: {name}.")

    async def _handle_job_status(self, context: ToolContext, arguments: Json) -> Json:
        group_id = _binding_group_id(context)
        if not group_id.startswith("fjob."):
            return tool_failure(
                "job_run_required",
                "Use factory_job_status only inside an active Factory job run session.",
            )

        status = str(arguments.get("status") or "").strip()
        if status not in _JOB_STATUS_VALUES:
            return tool_failure(
                "invalid_arguments",
                "status must be one of: needs_attention, failed, done.",
            )

        try:
            summary = optional_string(arguments.get("summary"), field_name="summary")
            question = optional_string(arguments.get("question"), field_name="question")
            reason = optional_string(arguments.get("reason"), field_name="reason")
        except ToolContractError as error:
            return tool_failure("invalid_arguments", str(error))

        if status == "needs_attention" and not question:
            return tool_failure(
                "invalid_arguments",
                "question is required when status is needs_attention.",
            )
        if status == "failed" and not reason:
            return tool_failure(
                "invalid_arguments",
                "reason is required when status is failed.",
            )

        try:
            async with self.service.lock:
                jobs_store = self.service.records().jobs
                run = await jobs_store.get_run_by_group(group_id)
                if run["state"] in FINAL_JOB_RUN_STATES:
                    return tool_failure(
                        "job_run_finished",
                        f"Job run {run['id']} is already {run['state']}.",
                    )
                updated = await jobs_store.transition_run(
                    run["id"],
                    status,
                    summary=summary,
                    question=question,
                    reason=reason,
                    expected_revision=run["revision"],
                )
        except FactoryError as error:
            return tool_failure(error.code, str(error))

        context.request_turn_end()
        await self._notify_run_change(
            updated, status=status, question=question or "", reason=reason or ""
        )
        return tool_success(
            {
                "text": f"Job run {updated['id']} marked {status}.",
                "run": updated,
            }
        )

    async def _notify_run_change(
        self, run: Json, *, status: str, question: str, reason: str = ""
    ) -> None:
        host = self.service.host
        if host is None:
            return
        if host.publish_change is not None:
            host.publish_change("job_runs", [run["id"]], run["revision"])
        job_execution = getattr(self.service, "job_execution", None)
        if job_execution is not None and hasattr(job_execution, "_refresh_badge"):
            await job_execution._refresh_badge()
        if status in {"needs_attention", "failed"} and host.notify is not None:
            label = str(run.get("job_name") or run["id"]).strip()
            prefix = "Job needs attention: " if status == "needs_attention" else "Job failed: "
            fallback = (
                "This job run needs your attention."
                if status == "needs_attention"
                else "This job run failed."
            )
            detail = question if status == "needs_attention" else reason
            title = f"{prefix}{label}"[:200]
            body = (detail or fallback).splitlines()[0][:2000]
            try:
                await host.notify(
                    title,
                    body,
                    page_id="board",
                    route=f"/job-run/{run['id']}",
                    key=run["id"],
                )
            except Exception:
                _LOGGER.warning(
                    "Factory job attention notification failed (run=%s)",
                    run["id"],
                    exc_info=True,
                )

    async def _authenticate_leader(self, context: ToolContext) -> tuple[Json, Json]:
        native = getattr(self.service, "native", None)
        if native is not None and hasattr(native, "actor"):
            try:
                actor = await native.actor(context)
            except FactoryError as error:
                if error.code == "participant_required":
                    raise FactoryError(
                        "leader_required",
                        "Only the active Factory Leader session can use factory_jobs.",
                    ) from error
                raise
            factory = actor["factory"]
            member = actor["member"]
            if factory.get("leader_member_id") != member["id"]:
                raise FactoryError(
                    "leader_required",
                    "Only the active Factory Leader can manage Factory jobs.",
                )
            return factory, member

        group_id = _binding_group_id(context)
        if not group_id.startswith("native."):
            raise FactoryError(
                "leader_required",
                "Only the active Factory Leader session can use factory_jobs.",
            )
        execution_id = group_id.removeprefix("native.")
        native_store = self.service.records().native
        try:
            execution = await native_store.get_execution(execution_id)
        except FactoryError as error:
            raise FactoryError(
                "leader_required",
                "Only an active Factory Leader execution can use factory_jobs.",
            ) from error
        if execution.get("group_id") != group_id or execution.get("state") != "running":
            raise FactoryError(
                "leader_required",
                "This Factory execution is not currently active.",
            )
        factory = await native_store.get_factory(execution["factory_id"])
        member = await native_store.get_member(execution["member_id"])
        if factory.get("state") != "active" or member.get("state") != "active":
            raise FactoryError(
                "leader_required",
                "This Factory or Leader member is not active.",
            )
        if factory.get("leader_member_id") != member["id"]:
            raise FactoryError(
                "leader_required",
                "Only the active Factory Leader can manage Factory jobs.",
            )
        return factory, member

    async def _handle_leader_jobs(self, context: ToolContext, arguments: Json) -> Json:
        action = str(arguments.get("action") or "").strip().lower()
        valid_actions = {"list", "get", "create", "update", "delete", "trigger", "runs"}
        if action not in valid_actions:
            return tool_failure(
                "invalid_arguments",
                "Unknown action. Use one of: list, get, create, update, delete, trigger, runs.",
            )

        try:
            async with self.service.lock:
                factory, _leader = await self._authenticate_leader(context)
                return await self._dispatch_leader_action(factory, action, arguments)
        except ToolContractError as error:
            return tool_failure("invalid_arguments", f"Nothing was changed. {error}")
        except FactoryError as error:
            return tool_failure(error.code, f"Nothing was changed. {error}")

    async def _dispatch_leader_action(self, factory: Json, action: str, arguments: Json) -> Json:
        jobs_store = self.service.records().jobs
        job_triggers = getattr(self.service, "job_triggers", None)
        factory_id = factory["id"]

        if action == "list":
            jobs = await jobs_store.list_jobs(factory_id)
            enriched_jobs = [
                job_triggers.enrich_job(job) if job_triggers is not None else job for job in jobs
            ]
            return tool_success({"text": f"{len(enriched_jobs)} job(s).", "jobs": enriched_jobs})

        if action == "get":
            job = await self._resolve_job(factory_id, arguments)
            enriched_job = job_triggers.enrich_job(job) if job_triggers is not None else job
            return tool_success({"text": f"Job {job['id']}: {job['name']}.", "job": enriched_job})

        if action == "create":
            name = optional_string(arguments.get("name"), field_name="name") or ""
            prompt = optional_string(arguments.get("prompt"), field_name="prompt") or ""
            description = (
                optional_string(arguments.get("description"), field_name="description") or ""
            )
            enabled = optional_bool(arguments.get("enabled"), field_name="enabled", default=True)
            agent_member_id = (
                optional_string(arguments.get("agent_member_id"), field_name="agent_member_id")
                or ""
            )
            triggers = arguments.get("triggers")
            clean_triggers = _validate_triggers(triggers) if triggers is not None else None
            if job_triggers is not None and clean_triggers:
                job_triggers.check({"name": name.strip(), "triggers": clean_triggers})
            created = await jobs_store.create_job(
                factory_id,
                name=name,
                prompt=prompt,
                description=description,
                enabled=enabled,
                agent_member_id=agent_member_id,
                triggers=clean_triggers,
            )
            if job_triggers is not None:
                await job_triggers.sync()
                enriched_job = job_triggers.enrich_job(created)
            else:
                enriched_job = created
            self._publish("jobs", created["id"], created["revision"])
            return tool_success(
                {"text": f"Created job {created['id']}: {created['name']}.", "job": enriched_job}
            )

        if action == "update":
            previous = await self._resolve_job(factory_id, arguments, allow_name=False)
            next_name: str | None = (
                optional_string(arguments.get("name"), field_name="name")
                if "name" in arguments
                else None
            )
            next_prompt: str | None = (
                optional_string(arguments.get("prompt"), field_name="prompt")
                if "prompt" in arguments
                else None
            )
            next_description: str | None = (
                optional_string(arguments.get("description"), field_name="description")
                if "description" in arguments
                else None
            )
            next_enabled: bool | None = (
                optional_bool(arguments.get("enabled"), field_name="enabled", default=True)
                if "enabled" in arguments and arguments.get("enabled") is not None
                else None
            )
            next_member_id: str | None = (
                optional_string(arguments.get("agent_member_id"), field_name="agent_member_id")
                if "agent_member_id" in arguments
                else None
            )
            next_triggers = arguments.get("triggers") if "triggers" in arguments else None
            clean_triggers = (
                _validate_triggers(next_triggers)
                if "triggers" in arguments and next_triggers is not None
                else None
            )
            if job_triggers is not None and clean_triggers is not None:
                job_triggers.check(
                    {
                        "name": next_name.strip() if next_name is not None else previous["name"],
                        "triggers": clean_triggers,
                    }
                )
            updated = await jobs_store.update_job(
                previous["id"],
                name=next_name,
                prompt=next_prompt,
                description=next_description,
                enabled=next_enabled,
                agent_member_id=next_member_id,
                triggers=clean_triggers,
                expected_revision=previous["revision"],
            )
            if job_triggers is not None:
                await job_triggers.sync()
                enriched_job = job_triggers.enrich_job(updated)
            else:
                enriched_job = updated
            self._publish("jobs", updated["id"], updated["revision"])
            return tool_success(
                {"text": f"Updated job {updated['id']}: {updated['name']}.", "job": enriched_job}
            )

        if action == "delete":
            job = await self._resolve_job(factory_id, arguments)
            await jobs_store.delete_job(job["id"])
            if job_triggers is not None:
                await job_triggers.sync()
            self._publish("jobs", job["id"], job["revision"] + 1)
            return tool_success(
                {
                    "text": f"Deleted job {job['id']} ({job['name']}).",
                    "deleted_job_id": job["id"],
                }
            )

        if action == "trigger":
            job = await self._resolve_job(factory_id, arguments)
            raw_member = optional_string(arguments.get("member_id"), field_name="member_id")
            member_override = (
                await self._resolve_member(factory_id, raw_member) if raw_member else None
            )
            input_note = optional_string(arguments.get("input_note"), field_name="input_note") or ""
            run = await jobs_store.create_run(
                factory_id,
                job["id"],
                origin="leader",
                member_id=member_override,
                input_note=input_note,
            )
            self._publish("job_runs", run["id"], run["revision"])
            self.service.request_schedule()
            return tool_success(
                {"text": f"Queued job run {run['id']} for {job['name']}.", "run": run}
            )

        # action == "runs"
        job_id = optional_string(arguments.get("job_id"), field_name="job_id")
        if not job_id:
            name_filter = optional_string(arguments.get("name"), field_name="name")
            if name_filter:
                job_id = (await self._resolve_job(factory_id, {"name": name_filter}))["id"]
        state_filter = optional_string(arguments.get("state"), field_name="state")
        limit = optional_int(
            arguments.get("limit"), field_name="limit", default=20, minimum=1, maximum=100
        )
        runs = await jobs_store.list_runs(
            factory_id,
            state=state_filter or None,
            job_id=job_id or None,
            limit=limit,
        )
        compact_runs = [
            {
                key: run.get(key)
                for key in (
                    "id",
                    "job_id",
                    "job_name",
                    "member_id",
                    "state",
                    "origin",
                    "summary",
                    "question",
                    "reason",
                    "created_at",
                    "finished_at",
                )
            }
            for run in runs
        ]
        return tool_success({"text": f"{len(compact_runs)} job run(s).", "runs": compact_runs})

    async def _resolve_job(
        self, factory_id: str, arguments: Json, *, allow_name: bool = True
    ) -> Json:
        jobs_store = self.service.records().jobs
        job_id = optional_string(arguments.get("job_id"), field_name="job_id")
        if job_id:
            job = await jobs_store.get_job(job_id)
            if job["factory_id"] != factory_id:
                raise FactoryError("not_found", f"Job {job_id!r} does not belong to this Factory.")
            return job
        if allow_name:
            name = optional_string(arguments.get("name"), field_name="name")
            if name:
                matches = [
                    item for item in await jobs_store.list_jobs(factory_id) if item["name"] == name
                ]
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    ids = ", ".join(item["id"] for item in matches)
                    raise FactoryError(
                        "ambiguous_job",
                        f"Multiple jobs share the name {name!r} ({ids}). Pass job_id instead.",
                    )
                raise FactoryError("not_found", f"No job named {name!r} exists in this Factory.")
        raise FactoryError(
            "invalid_arguments",
            "Supply job_id" + (" or exact name." if allow_name else "."),
        )

    async def _resolve_member(self, factory_id: str, target: str) -> str:
        members = await self.service.records().native.list_members(factory_id, state="active")
        for member in members:
            if member["id"] == target:
                return str(member["id"])
        needle = target.casefold()
        matches = [
            member
            for member in members
            if str(member.get("role") or "").strip().casefold() == needle
            or str(member.get("name") or "").strip().casefold() == needle
        ]
        if len(matches) == 1:
            return str(matches[0]["id"])
        if len(matches) > 1:
            ids = ", ".join(f"{m['id']} ({m.get('role') or m.get('name')})" for m in matches)
            raise FactoryError(
                "ambiguous_member",
                f"Multiple active members match {target!r}: {ids}. Pass member_id instead.",
            )
        raise FactoryError(
            "member_unavailable",
            f"No active Factory member matches {target!r}.",
        )

    def _publish(self, resource: str, record_id: str, revision: int) -> None:
        host = self.service.host
        if host is not None and host.publish_change is not None:
            host.publish_change(resource, [record_id], revision)
