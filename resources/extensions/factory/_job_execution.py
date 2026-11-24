"""Simplified Software Factory Job Run execution engine."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from core.agents.temporary import TemporaryAgentConfig
from core.agents.temporary_execution import TemporaryRunInput
from core.runs import RunNotFoundError, is_silent_answer
from core.tools.availability import ToolAccess
from core.utils.ids import new_id
from core.utils.logging import get_logger

from . import _native_agent_text, agent_text
from ._jobs_store import FINAL_JOB_RUN_STATES, FactoryJobsStore
from .store import FactoryError

type Json = dict[str, Any]

_LOGGER = get_logger("extensions.factory")
_JOB_STATUS_TOOL_NAME = "factory_job_status"
_SUMMARY_LIMIT = 4000


def _job_instructions(factory: Json, member: Json, run: Json) -> str:
    try:
        from . import _job_agent_text

        return str(_job_agent_text.build_job_run_instructions(factory, member, run))
    except ImportError:
        role = member.get("role") or member.get("agent_id") or "worker"
        remit = factory.get("remit", "")
        parts = [
            f"You are executing a Factory Job Run as {role}.",
            (
                f"Call `{_JOB_STATUS_TOOL_NAME}` to report `done`, `needs_attention` "
                "(with a clear question), or `failed` (with a clear reason)."
            ),
        ]
        if remit:
            parts.append(f"Factory remit:\n{remit}")
        if member.get("instructions"):
            parts.append(f"Member instructions:\n{member['instructions']}")
        return "\n\n".join(parts)


def _job_message(run: Json) -> str:
    try:
        from . import _job_agent_text

        return str(_job_agent_text.build_job_run_message(run))
    except ImportError:
        parts = [run.get("prompt", "").strip()]
        if run.get("input_note"):
            parts.append(f"Additional input:\n{run['input_note'].strip()}")
        if run.get("trigger_event") is not None:
            parts.append(f"Trigger event:\n{run['trigger_event']}")
        return "\n\n".join(part for part in parts if part)


def _error_reason(exc: Exception) -> str:
    message = str(exc).strip()
    return message or type(exc).__name__


class FactoryJobExecution:
    """Admits, runs, reconciles, and handles operator actions for Factory Job Runs."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self._quiescing = False
        self._active_run_ids: dict[str, str] = {}
        self._pending_deliveries: dict[str, Any] = {}

    @property
    def jobs(self) -> FactoryJobsStore:
        return self.service.records().jobs  # type: ignore[no-any-return]

    @property
    def native_store(self) -> Any:
        return self.service.records().native

    @property
    def groups(self) -> Any:
        return self.service.host.temporary_agents

    @staticmethod
    def owns(group_id: str) -> bool:
        return group_id.startswith("fjob.")

    async def running_count(self) -> int:
        active = await self.jobs.list_active_runs()
        return sum(1 for run in active if run["state"] == "running")

    async def _active_factories(self) -> list[Json]:
        factories: list[Json] = []
        after = ""
        while True:
            page = await self.native_store.list_factories(state="active", after=after, limit=100)
            factories.extend(page)
            if len(page) < 100:
                return factories
            after = page[-1]["id"]

    async def attention_count(self) -> int:
        count = 0
        for factory in await self._active_factories():
            for state in ("needs_attention", "failed"):
                runs = await self.jobs.list_runs(factory["id"], state=state, limit=1000)
                count += len(runs)
        host = getattr(self.service, "host", None)
        pending_approvals = getattr(host, "pending_approvals", None) if host is not None else None
        if pending_approvals is not None and self._active_run_ids:
            for group_id, run_id in list(self._active_run_ids.items()):
                if not run_id:
                    continue
                try:
                    result = await pending_approvals(group_id, [run_id])
                except Exception:
                    continue
                count += len(result.get("requests", ()))
        return count

    async def schedule(self, wip_limit: int) -> None:
        if self._quiescing or wip_limit <= 0:
            return
        active_runs = await self.jobs.list_active_runs()
        running = sum(1 for run in active_runs if run["state"] == "running")
        if running >= wip_limit:
            return
        queued = [run for run in active_runs if run["state"] == "queued"]
        if not queued:
            return

        factories: dict[str, Json] = {}
        for run in queued:
            if running >= wip_limit:
                break
            factory_id = run["factory_id"]
            factory = factories.get(factory_id)
            if factory is None:
                try:
                    factory = await self.native_store.get_factory(factory_id)
                except FactoryError:
                    continue
                factories[factory_id] = factory
            if factory.get("state") != "active":
                continue
            try:
                await self.start_run(run)
                running += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.exception("Factory job admission failed (run=%s)", run["id"])
                failed = await self.jobs.transition_run(
                    run["id"], "failed", reason=_error_reason(exc)
                )
                await self._publish_run(failed, notify_attention=True)

    async def _build_config(self, factory: Json, member: Json, run: Json) -> TemporaryAgentConfig:
        project = await self.service.project(factory["project_id"])
        local = member.get("config", {})
        boundary = factory.get("config", {})
        grants = {_JOB_STATUS_TOOL_NAME}
        denied = (set(agent_text.PRIVATE_TOOLS) | set(_native_agent_text.PRIVATE_TOOLS)) - grants
        for settings in (boundary, local):
            denied.update(settings.get("denied_tools", []))
        allowed: set[str] | None = None
        for settings in (boundary, local):
            if "allowed_tools" in settings:
                selection = set(settings["allowed_tools"])
                allowed = selection if allowed is None else allowed & selection
        ceiling_skills = boundary.get("allowed_skills", ["*"])
        local_skills = local.get("allowed_skills", ["*"])
        skills = (
            local_skills
            if "*" in ceiling_skills
            else ceiling_skills
            if "*" in local_skills
            else [name for name in local_skills if name in ceiling_skills]
        )
        return TemporaryAgentConfig(
            model="",
            cwd=Path(project["cwd"]),
            tool_access=ToolAccess(
                mode="all" if allowed is None else "selected",
                allowed=tuple(sorted((allowed or set()) | grants)),
                denied=tuple(sorted(denied)),
                fixed=allowed is not None,
            ),
            allowed_skills=skills,
            tools={**boundary.get("tools", {}), **local.get("tools", {})},
            name=member.get("name") or member.get("role") or member["agent_id"],
            instructions=_job_instructions(factory, member, run),
            identity_agent=member["agent_id"],
            identity_incarnation=member.get("agent_proof") or None,
            permission_ceiling=boundary.get("permission_ceiling"),
            permission_ceiling_profile=boundary.get("permission_ceiling_profile"),
        )

    async def _ensure_participant(self, run: Json) -> tuple[Any, Any]:
        factory = await self.native_store.get_factory(run["factory_id"])
        if factory.get("state") != "active":
            raise FactoryError("factory_archived", "This Factory is not active.")
        member = await self.native_store.get_member(run["member_id"])
        if member.get("factory_id") != factory["id"] or member.get("state") != "active":
            raise FactoryError("member_unavailable", "Selected member is not active.")

        bindings = await self.groups.list(run["group_id"], limit=10)
        binding = next(
            (item for item in bindings if item.participant_id == run["participant_id"]),
            None,
        )
        if binding is None:
            config = await self._build_config(factory, member, run)
            binding = await self.groups.create(
                run["group_id"],
                run["participant_id"],
                config,
                project_id=factory["project_id"],
            )
        handle = await self.groups.open_group(run["group_id"])
        return binding, handle

    async def start_run(self, run: Json) -> Json:
        binding, handle = await self._ensure_participant(run)
        session_address = {
            "project_id": binding.address.project_id,
            "agent_id": binding.address.agent_id,
            "session_id": binding.address.session_id,
        }
        updated = await self.jobs.transition_run(
            run["id"], "running", session_address=session_address
        )
        await self._publish_run(updated)

        admission = await self.groups.start(
            handle,
            updated["participant_id"],
            TemporaryRunInput("initial", _job_message(updated), updated["id"]),
        )
        self._active_run_ids[updated["group_id"]] = admission.run_id
        if admission.existing and admission.status != "running":
            await self._settle_finished(
                await self.jobs.get_run(updated["id"]),
                "success" if admission.status == "completed" else admission.status,
                run_id=admission.run_id,
            )
            return await self.jobs.get_run(updated["id"])
        return updated

    async def before_request(self, context: Any) -> Any:
        group_id = context.binding.group_id
        self._active_run_ids[group_id] = context.run_id
        return self._pending_deliveries.get(group_id)

    async def before_model_request(self, context: Any) -> None:
        run = await self.jobs.get_run_by_group(context.binding.group_id)
        if run["state"] in FINAL_JOB_RUN_STATES:
            raise FactoryError("stale_run", "This Job Run is already finished.")

    async def before_tool_call(
        self, context: Any, *, tool_name: str, tool_call_id: str
    ) -> str | None:
        _ = context, tool_name, tool_call_id
        return None

    async def acknowledge_delivery(self, context: Any, delivery: Any) -> None:
        pending = self._pending_deliveries.get(context.binding.group_id)
        if pending is not None and getattr(pending, "delivery_id", None) == getattr(
            delivery, "delivery_id", None
        ):
            self._pending_deliveries.pop(context.binding.group_id, None)

    async def run_finished(self, context: Any, outcome: str) -> None:
        if self._quiescing:
            return
        group_id = context.binding.group_id
        try:
            run = await self.jobs.get_run_by_group(group_id)
        except FactoryError as exc:
            if exc.code == "not_found":
                return
            raise
        if self._active_run_ids.get(group_id) == context.run_id:
            self._active_run_ids.pop(group_id, None)
        await self._settle_finished(run, outcome, run_id=context.run_id)

    async def _settle_finished(self, run: Json, outcome: str, *, run_id: str | None = None) -> None:
        if run["state"] in {"needs_attention", "failed", "done", "cancelled"}:
            await self._publish_run(
                run, notify_attention=run["state"] in {"needs_attention", "failed"}
            )
            self.service.request_schedule()
            return

        if outcome == "cancelled":
            updated = await self.jobs.transition_run(run["id"], "cancelled")
        elif outcome != "success":
            updated = await self.jobs.transition_run(
                run["id"], "failed", reason=f"Job run ended with outcome: {outcome}."
            )
        else:
            summary = await self._extract_summary(run, run_id=run_id)
            updated = await self.jobs.transition_run(run["id"], "done", summary=summary)

        await self._publish_run(
            updated, notify_attention=updated["state"] in {"needs_attention", "failed"}
        )
        self.service.request_schedule()

    async def _extract_summary(self, run: Json, *, run_id: str | None = None) -> str:
        try:
            snapshot = await self.groups.inspect(
                run["group_id"], run["participant_id"], {"limit": 12}
            )
        except RunNotFoundError, Exception:
            return ""
        messages = list(snapshot.page.messages)
        if run_id:
            run_messages = [msg for msg in messages if getattr(msg, "run_id", None) == run_id]
            if run_messages:
                messages = run_messages
        answer = next(
            (
                msg.content.strip()
                for msg in reversed(messages)
                if getattr(msg, "role", None) == "assistant"
                and not getattr(msg, "tool_calls", None)
                and isinstance(getattr(msg, "content", None), str)
                and msg.content.strip()
                and not is_silent_answer(msg.content)
            ),
            "",
        )
        return answer[:_SUMMARY_LIMIT]

    async def quiesce(self, reason: str = "extension") -> None:
        self._quiescing = True
        host = getattr(self.service, "host", None)
        groups = getattr(host, "temporary_agents", None) if host is not None else None
        if groups is None:
            return
        group_ids = set(self._active_run_ids)
        try:
            for group_id in await groups.groups():
                if self.owns(group_id):
                    group_ids.add(group_id)
        except Exception:
            pass
        for group_id in group_ids:
            try:
                await groups.close_group(group_id, reason)
            except Exception:
                _LOGGER.warning("Failed to close job group %s during quiesce", group_id)

    async def reply_run(self, run_id: str, text: str) -> Json:
        if not isinstance(text, str) or not text.strip():
            raise FactoryError("invalid_job", "Reply text must be a non-empty string.")
        run = await self.jobs.get_run(run_id)
        if run["state"] == "cancelled":
            raise FactoryError("wrong_state", "Cancelled job runs cannot be resumed.")
        if run["state"] == "running":
            raise FactoryError("wrong_state", "This job run is already running.")

        binding, handle = await self._ensure_participant(run)
        session_address = {
            "project_id": binding.address.project_id,
            "agent_id": binding.address.agent_id,
            "session_id": binding.address.session_id,
        }
        updated = await self.jobs.transition_run(
            run["id"],
            "running",
            question="",
            reason="",
            session_address=session_address,
        )
        await self._publish_run(updated)

        request_id = f"reply:{updated['revision']}:{new_id('jrp')}"
        admission = await self.groups.start(
            handle,
            updated["participant_id"],
            TemporaryRunInput("continuation", text.strip(), request_id),
        )
        self._active_run_ids[updated["group_id"]] = admission.run_id
        return updated

    async def cancel_run(self, run_id: str) -> Json:
        run = await self.jobs.get_run(run_id)
        if run["state"] in FINAL_JOB_RUN_STATES:
            return run
        self._active_run_ids.pop(run["group_id"], None)
        if run["state"] in {"running", "needs_attention"}:
            try:
                await self.groups.close_group(run["group_id"], reason="cancelled")
            except Exception:
                _LOGGER.warning("Failed to close job group %s on cancel", run["group_id"])
        updated = await self.jobs.transition_run(run["id"], "cancelled")
        await self._publish_run(updated)
        self.service.request_schedule()
        return updated

    async def reconcile(self) -> None:
        self._quiescing = False
        active_runs = await self.jobs.list_active_runs()
        running_runs = [run for run in active_runs if run["state"] == "running"]
        if not running_runs:
            return
        try:
            live_groups = set(await self.groups.groups())
        except Exception:
            live_groups = set()
        for run in running_runs:
            group_id = run["group_id"]
            is_live = False
            if group_id in live_groups:
                run_id = self._active_run_ids.get(group_id)
                if run_id:
                    try:
                        inspected = (await self.groups.owned_runs(group_id, [run_id])).get(run_id)
                        is_live = (
                            inspected is not None
                            and inspected.run is not None
                            and inspected.run.status.value == "running"
                        )
                    except Exception:
                        is_live = False
            if not is_live:
                self._active_run_ids.pop(group_id, None)
                failed = await self.jobs.transition_run(
                    run["id"], "failed", reason="Interrupted by server restart."
                )
                await self._publish_run(failed, notify_attention=True)

    async def _refresh_badge(self) -> None:
        host = getattr(self.service, "host", None)
        if host is None or getattr(host, "set_page_badge", None) is None:
            return
        count = await self.attention_count()
        store = getattr(self.service, "store", None)
        if store is not None and hasattr(store, "attention_count"):
            count += await store.attention_count()
        host.set_page_badge("board", count)

    async def _publish_run(self, run: Json, *, notify_attention: bool = False) -> None:
        host = getattr(self.service, "host", None)
        if host is None or getattr(self.service, "_closing", False):
            return
        if getattr(host, "publish_change", None) is not None:
            host.publish_change("job_runs", [run["id"]], run["revision"])
        await self._refresh_badge()
        if notify_attention and getattr(host, "notify", None) is not None:
            label = " ".join((run.get("job_name") or "Job run").split())
            prefix = "Needs attention: " if run["state"] == "needs_attention" else "Job failed: "
            body = run.get("question") or run.get("reason") or "This Job Run needs your attention."
            try:
                await host.notify(
                    f"{prefix}{label}"[:200],
                    str(body).splitlines()[0][:2000],
                    page_id="board",
                    route=f"/job-run/{run['id']}",
                    key=run["id"],
                )
            except Exception:
                _LOGGER.warning(
                    "Factory job attention notification failed (run=%s)", run["id"], exc_info=True
                )
