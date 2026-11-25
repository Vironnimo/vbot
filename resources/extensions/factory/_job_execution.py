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

from . import _native_agent_text
from ._job_agent_text import JOB_PRIVATE_TOOLS
from ._jobs_store import FINAL_JOB_RUN_STATES, FactoryJobsStore
from ._public_tool import PUBLIC_TOOL_NAME
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
            needs_attention = await self.jobs.list_runs(
                factory["id"], state="needs_attention", limit=1000
            )
            failed = await self.jobs.list_runs(factory["id"], state="failed", limit=1000)
            count += len(needs_attention) + sum(
                1 for run in failed if not run.get("acknowledged", False)
            )
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

    async def schedule(self, wip_limit: int | None = None) -> None:
        if self._quiescing:
            return
        limit = wip_limit if wip_limit is not None and wip_limit > 0 else None
        active_runs = await self.jobs.list_active_runs()
        running = sum(1 for run in active_runs if run["state"] == "running")
        if limit is not None and running >= limit:
            return
        queued = [run for run in active_runs if run["state"] == "queued"]
        if not queued:
            return

        factories: dict[str, Json] = {}
        for run in queued:
            if limit is not None and running >= limit:
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
        denied = (
            set(JOB_PRIVATE_TOOLS) | set(_native_agent_text.PRIVATE_TOOLS) | {PUBLIC_TOOL_NAME}
        ) - grants
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
                allowed=(
                    tuple(sorted(((allowed or set()) | grants) - denied))
                    if allowed is not None
                    else ()
                ),
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
        if factory.get("state") == "archived":
            raise FactoryError("factory_archived", "This Factory is archived.")
        if factory.get("state") != "active":
            raise FactoryError("factory_paused", "This Factory is paused.")
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
            allowed_models = factory.get("config", {}).get("allowed_models")
            host = getattr(self.service, "host", None)
            inspect_prompt = getattr(host, "inspect_prompt", None) if host is not None else None
            if allowed_models is not None and inspect_prompt is not None:
                preview = await inspect_prompt(config, factory["project_id"])
                if not self.service.native._model_allowed(preview["model"], allowed_models):
                    raise FactoryError(
                        "model_unavailable",
                        "This Model is outside the Factory configuration boundary.",
                    )
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
        async with self.service.lock:
            run = await self.jobs.get_run_by_group(context.binding.group_id)
            if run["state"] in FINAL_JOB_RUN_STATES:
                raise FactoryError("stale_run", "This Job Run is already finished.")
            factory = await self.native_store.get_factory(run["factory_id"])
            if factory.get("state") == "archived":
                raise FactoryError("factory_archived", "This Factory is archived.")
            if factory.get("state") != "active":
                raise FactoryError("factory_paused", "This Factory is paused.")
            member = await self.native_store.get_member(run["member_id"])
            if member.get("factory_id") != factory["id"] or member.get("state") != "active":
                raise FactoryError("member_unavailable", "Selected member is not active.")
            models = factory.get("config", {}).get("allowed_models")
            physical_model = getattr(context, "model", None)
            if (
                models is not None
                and physical_model is not None
                and not self.service.native._model_allowed(physical_model, models)
            ):
                raise FactoryError(
                    "model_unavailable",
                    "This Model is outside the Factory configuration boundary.",
                )

    async def before_tool_call(
        self, context: Any, *, tool_name: str, tool_call_id: str
    ) -> str | None:
        _ = tool_call_id
        async with self.service.lock:
            run = await self.jobs.get_run_by_group(context.binding.group_id)
            factory = await self.native_store.get_factory(run["factory_id"])
            member = await self.native_store.get_member(run["member_id"])
            from ._native_policy import tool_permitted

            if not await tool_permitted(
                self.service,
                [factory.get("config", {}), member.get("config", {})],
                tool_name,
                {_JOB_STATUS_TOOL_NAME},
            ):
                return _native_agent_text.tool_refusal(tool_name)
        return None

    async def acknowledge_delivery(self, context: Any, delivery: Any) -> None:
        pending = self._pending_deliveries.get(context.binding.group_id)
        if pending is not None and getattr(pending, "delivery_id", None) == getattr(
            delivery, "delivery_id", None
        ):
            self._pending_deliveries.pop(context.binding.group_id, None)

    async def run_finished(self, context: Any, outcome: str) -> None:
        async with self.service.lock:
            if self._quiescing:
                return
            group_id = context.binding.group_id
            if context.run_id != self._active_run_ids.get(group_id):
                return
            try:
                run = await self.jobs.get_run_by_group(group_id)
            except FactoryError as exc:
                if exc.code == "not_found":
                    return
                raise
            self._active_run_ids.pop(group_id, None)
            await self._settle_finished(run, outcome, run_id=context.run_id)

    async def _settle_finished(self, run: Json, outcome: str, *, run_id: str | None = None) -> None:
        if run["state"] in {"needs_attention", "failed", "done", "cancelled"}:
            await self._publish_run(run, notify_attention=False)
            self.service.request_schedule()
            return

        if outcome == "cancelled":
            updated = await self.jobs.transition_run(
                run["id"], "cancelled", expected_revision=run["revision"]
            )
        elif outcome != "success":
            updated = await self.jobs.transition_run(
                run["id"],
                "failed",
                reason=f"Job run ended with outcome: {outcome}.",
                expected_revision=run["revision"],
            )
        else:
            summary = await self._extract_summary(run, run_id=run_id)
            updated = await self.jobs.transition_run(
                run["id"], "done", summary=summary, expected_revision=run["revision"]
            )

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

    async def _all_group_ids(self, groups: Any) -> set[str]:
        found: set[str] = set()
        after = ""
        while True:
            page = await groups.groups(after=after, limit=100)
            if not page:
                return found
            found.update(page)
            if len(page) < 100:
                return found
            after = page[-1]

    async def quiesce(self, reason: str = "extension") -> None:
        self._quiescing = True
        host = getattr(self.service, "host", None)
        groups = getattr(host, "temporary_agents", None) if host is not None else None
        if groups is None:
            return
        group_ids = set(self._active_run_ids)
        try:
            for group_id in await self._all_group_ids(groups):
                if self.owns(group_id):
                    group_ids.add(group_id)
        except Exception:
            pass
        for group_id in group_ids:
            try:
                await groups.close_group(group_id, reason)
            except Exception:
                _LOGGER.warning("Failed to close job group %s during quiesce", group_id)

    async def wait_for_turn_settle(self, run_id: str, *, timeout: float = 3.0) -> None:
        """Wait outside service.lock for a finishing turn to clear _active_run_ids."""
        try:
            run = await self.jobs.get_run(run_id)
        except FactoryError:
            return
        group_id = run["group_id"]
        if group_id not in self._active_run_ids:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while group_id in self._active_run_ids and loop.time() < deadline:
            await asyncio.sleep(0.05)

    async def close_group_safe(self, group_id: str, reason: str = "cancelled") -> None:
        try:
            await self.groups.close_group(group_id, reason=reason)
        except Exception:
            _LOGGER.warning("Failed to close job group %s (%s)", group_id, reason)

    async def reply_run(self, run_id: str, text: str) -> Json:
        if not isinstance(text, str) or not text.strip():
            raise FactoryError("invalid_job", "Reply text must be a non-empty string.")
        run = await self.jobs.get_run(run_id)
        if run["state"] not in {"needs_attention", "failed"}:
            raise FactoryError(
                "wrong_state",
                f"Job run in state {run['state']!r} cannot be resumed.",
            )

        had_session = run.get("session_address") is not None
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

        reply = text.strip()
        request_id = f"reply:{updated['revision']}:{new_id('jrp')}"
        run_input = (
            TemporaryRunInput("continuation", reply, request_id)
            if had_session
            else TemporaryRunInput(
                "initial", f"{_job_message(updated)}\n\nOperator reply:\n{reply}", request_id
            )
        )
        try:
            admission = await self.groups.start(
                handle,
                updated["participant_id"],
                run_input,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed = await self.jobs.transition_run(
                updated["id"],
                "failed",
                reason=_error_reason(exc),
                expected_revision=updated["revision"],
            )
            await self._publish_run(failed, notify_attention=True)
            raise
        self._active_run_ids[updated["group_id"]] = admission.run_id
        _LOGGER.info("Factory job run resumed (run=%s, job=%s)", updated["id"], updated["job_id"])
        return updated

    async def cancel_run(self, run_id: str, *, close_inline: bool = True) -> Json:
        run = await self.jobs.get_run(run_id)
        if run["state"] in FINAL_JOB_RUN_STATES:
            return run
        prev_state = run["state"]
        group_id = run["group_id"]
        self._active_run_ids.pop(group_id, None)
        updated = await self.jobs.transition_run(run["id"], "cancelled")
        await self._publish_run(updated)
        self.service.request_schedule()
        _LOGGER.info("Factory job run cancelled (run=%s, prev=%s)", updated["id"], prev_state)
        if prev_state in {"running", "needs_attention"}:
            if close_inline and not self.service.lock.locked():
                await self.close_group_safe(group_id, reason="cancelled")
            elif close_inline and hasattr(self.service, "spawn"):
                self.service.spawn(self.close_group_safe(group_id, reason="cancelled"))
        return updated

    async def reconcile(self) -> None:
        self._quiescing = False
        active_runs = await self.jobs.list_active_runs()
        running_runs = [run for run in active_runs if run["state"] == "running"]
        if not running_runs:
            return
        try:
            live_groups = await self._all_group_ids(self.groups)
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
        badge = getattr(self.service, "_badge", None)
        if badge is not None:
            await badge()
            return
        host = getattr(self.service, "host", None)
        if host is None or getattr(host, "set_page_badge", None) is None:
            return
        host.set_page_badge("board", await self.attention_count())

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
