"""Identity-backed native Session launch, request admission and terminal handoff."""

from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents.temporary import TemporaryAgentConfig
from core.agents.temporary_execution import TemporaryRunInput
from core.extensions import PreparedSessionDelivery, SessionRequestContext
from core.runs import ActiveRunError
from core.sessions import SessionAddress
from core.tools.availability import ToolAccess

from . import agent_text
from .store import FactoryError

type Json = dict[str, Any]

_TERMINAL = frozenset({"done", "cancelled"})

if TYPE_CHECKING:
    from ._native_execution import NativeFactoryExecution


class NativeSessions:
    """Canonical native Sessions: launch, delivery, admission and terminal handoff."""

    def __init__(self, native: NativeFactoryExecution) -> None:
        self.native = native
        self.service = native.service
        self._scheduled: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def store(self) -> Any:
        return self.service.records().native

    @property
    def groups(self) -> Any:
        return self.service.host.temporary_agents

    async def _close(
        self,
        execution: Json,
        *,
        archive: bool = False,
        wait: bool = False,
        final_state: str = "closed",
    ) -> None:
        async with self._locks.setdefault(execution["id"], asyncio.Lock()):
            if wait:
                await self.service.execution.sessions._wait_run(
                    execution["group_id"], execution["runtime"].get("run_id")
                )
            await self.groups.close_group(execution["group_id"])
            if archive:
                from ._native_handoff import capture

                await capture(self.native, execution)
                await self.groups.archive_group(execution["group_id"])
            async with self.service.lock:
                if self.service._closing:
                    return
                current = await self.store.get_execution(execution["id"])
                if current["state"] == "draining":
                    await self.store.change_execution(
                        execution["id"], {"state": final_state}, actor="factory"
                    )
                    self.service.request_schedule()

    def _launch_later(self, execution: Json) -> None:
        token = (execution["runtime"].get("pending") or {}).get("id")
        if token and token not in self._scheduled:
            self._scheduled.add(token)
            self.service.spawn(self._launch(execution["id"], token))

    async def _launch(self, execution_id: str, token: str) -> None:
        from ._native_agent_text import PRIVATE_TOOLS, native_instructions

        try:
            async with self._locks.setdefault(execution_id, asyncio.Lock()):
                async with self.service.lock:
                    execution = await self.store.get_execution(execution_id)
                    if (
                        not await self.native._valid(execution)
                        or (execution["runtime"].get("pending") or {}).get("id") != token
                    ):
                        return
                    old_run = execution["runtime"].get("run_id")
                from ._native_handoff import prepare

                if not await prepare(self.native, execution, token):
                    return
                await self.service.execution.sessions._wait_run(execution["group_id"], old_run)
                async with self.service.lock:
                    execution = await self.store.get_execution(execution_id)
                    if (
                        not await self.native._valid(execution)
                        or (execution["runtime"].get("pending") or {}).get("id") != token
                    ):
                        return
                    factory, member, scope = await self.native._scope(execution)
                    runtime = deepcopy(execution["runtime"])
                    pending = runtime["pending"]
                    if await self.native.limits.check(execution, check_runs=True):
                        return
                    if not runtime.get("address"):
                        project = await self.service.project(factory["project_id"])
                        local = member.get("config", {})
                        boundary = factory.get("config", {})
                        grants = set(PRIVATE_TOOLS)
                        if member["id"] != factory["leader_member_id"]:
                            grants -= {"factory_settings", "factory_members"}
                        denied = (set(agent_text.PRIVATE_TOOLS) | set(PRIVATE_TOOLS)) - grants
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
                        allowed_models = boundary.get("allowed_models")
                        if (
                            allowed_models is not None
                            and local.get("model")
                            and not self.native._model_allowed(local["model"], allowed_models)
                        ):
                            await self.native.block(execution, "model_unavailable")
                            return
                        config = TemporaryAgentConfig(
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
                            instructions=native_instructions(
                                factory,
                                member,
                                task=scope if execution["kind"] in {"task", "review"} else None,
                                topic=scope if execution["kind"] == "topic" else None,
                            ),
                            identity_agent=member["agent_id"],
                            identity_incarnation=member.get("agent_proof") or None,
                            model_override=local.get("model") or boundary.get("model") or None,
                            thinking_effort_override=local.get("thinking_effort")
                            or boundary.get("thinking_effort")
                            or None,
                            permission_ceiling=boundary.get("permission_ceiling"),
                        )
                        if allowed_models is not None:
                            preview = await self.service.host.inspect_prompt(
                                config, factory["project_id"]
                            )
                            if not self.native._model_allowed(preview["model"], allowed_models):
                                await self.native.block(execution, "model_unavailable")
                                return
                        binding = await self.groups.create(
                            execution["group_id"],
                            execution["participant_id"],
                            config,
                            project_id=factory["project_id"],
                        )
                        runtime.update(
                            address={
                                "project_id": binding.address.project_id,
                                "agent_id": binding.address.agent_id,
                                "session_id": binding.address.session_id,
                            },
                            generation_id=binding.generation_id,
                        )
                        execution = await self.store.change_execution(
                            execution_id, {"runtime": runtime}, actor="factory"
                        )
                        if execution["kind"] in {"task", "review"}:
                            scope_runtime = deepcopy(scope["runtime"])
                            sessions = scope_runtime.setdefault("sessions", [])
                            if not any(
                                item.get("execution_id") == execution_id for item in sessions
                            ):
                                sessions.append(
                                    {
                                        **runtime["address"],
                                        "generation_id": runtime["generation_id"],
                                        "execution_id": execution_id,
                                        "kind": execution["kind"],
                                    }
                                )
                                await self.store.change_task(
                                    scope["id"],
                                    {"runtime": scope_runtime},
                                    actor="factory",
                                    event="session_bound",
                                )
                    handle = await self.groups.open_group(execution["group_id"])
                    runtime["group_epoch"] = handle.epoch
                    execution = await self.store.change_execution(
                        execution_id, {"runtime": runtime}, actor="factory"
                    )
                    admission = await self.groups.start(
                        handle,
                        execution["participant_id"],
                        TemporaryRunInput(pending["kind"], pending["content"], token),
                    )
                    # The admitted input is durable. Advance its read marker
                    # while the same input identity is still safe to retry.
                    await self.native.discussions.acknowledged(execution, pending)
                    runtime.update(run_id=admission.run_id)
                    runtime["pending"] = None
                    execution = await self.store.change_execution(
                        execution_id,
                        {"state": "running", "input_id": token, "runtime": runtime},
                        actor="factory",
                    )
                    await self.native.limits.observe_run(execution, admission.run_id)
                    if admission.existing and admission.status != "running":
                        await self._finished(
                            await self.store.get_execution(execution_id),
                            "success" if admission.status == "completed" else "error",
                        )
        except ActiveRunError:
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self.service.lock:
                execution = await self.store.get_execution(execution_id)
                if (execution["runtime"].get("pending") or {}).get("id") == token:
                    await self.native.block(execution, "run_failed")
            raise
        finally:
            self._scheduled.discard(token)

    async def _guard(self, context: SessionRequestContext) -> Json:
        execution = await self.native._find(context.binding.group_id)
        owner = context.execution_owner
        runtime = execution["runtime"]
        if (
            owner is None
            or not await self.native._valid(execution)
            or owner.epoch != runtime.get("group_epoch")
            or owner.participant_id != execution["participant_id"]
            or owner.generation_id != runtime.get("generation_id")
        ):
            raise FactoryError("stale_run", "This Factory execution is no longer current.")
        return execution

    async def before_request(
        self, context: SessionRequestContext
    ) -> PreparedSessionDelivery | None:
        async with self.service.lock:
            execution = await self._guard(context)
            if await self.native.limits.check(
                execution, check_runs=context.run_id not in execution["runtime"].get("runs", [])
            ):
                raise FactoryError(
                    "budget_exhausted", "Factory admission paused at its configured limit."
                )
            await self.native.limits.observe_run(execution, context.run_id)
            execution = await self.store.get_execution(execution["id"])
            runtime = deepcopy(execution["runtime"])
            if (context.agent_id, context.session_id) != (
                context.binding.address.agent_id,
                context.binding.address.session_id,
            ):
                return None
            runtime["run_id"] = context.run_id
            pending = runtime.get("pending")
            if pending and pending["kind"] == "continuation" and not pending.get("delivery"):
                pending["delivery"] = {
                    "generation_id": context.binding.generation_id,
                    "address": runtime["address"],
                    "revision": str(execution["revision"]),
                }
            await self.store.change_execution(
                execution["id"], {"state": "running", "runtime": runtime}, actor="factory"
            )
            return self._delivery(pending) if pending and pending.get("delivery") else None

    @staticmethod
    def _delivery(pending: Json) -> PreparedSessionDelivery:
        content = pending["content"]
        return PreparedSessionDelivery(
            pending["id"],
            hashlib.sha256(content.encode()).hexdigest(),
            (content,),
            pending["delivery"]["revision"],
            "factory_native",
        )

    async def acknowledge_delivery(
        self, context: SessionRequestContext, delivery: PreparedSessionDelivery
    ) -> None:
        async with self.service.lock:
            execution = await self._guard(context)
            pending = execution["runtime"].get("pending") or {}
            if not pending or pending.get("id") != delivery.delivery_id:
                return
            if (
                self._delivery(pending) != delivery
                or pending["delivery"]["generation_id"] != context.binding.generation_id
            ):
                raise FactoryError("receipt_conflict", "Factory discussion delivery conflicts.")
            if not await self._acknowledge(execution):
                raise FactoryError(
                    "delivery_unacknowledged", "Factory discussion has no canonical receipt."
                )

    async def _acknowledge(self, execution: Json) -> bool:
        pending = execution["runtime"].get("pending") or {}
        target = pending.get("delivery")
        if not target:
            return False
        receipt = await self.groups.delivery_receipt(
            SessionAddress(**target["address"]), target["generation_id"], pending["id"]
        )
        if receipt is None:
            return False
        delivery = self._delivery(pending)
        if (
            receipt.content_hash != delivery.content_hash
            or receipt.effect_kind != delivery.effect_kind
            or receipt.carrier_location.get("kind") != "note"
        ):
            raise FactoryError("receipt_conflict", "Factory discussion delivery conflicts.")
        # A committed receipt is replayable until the read marker is durable.
        await self.native.discussions.acknowledged(execution, pending)
        runtime = deepcopy(execution["runtime"])
        runtime["pending"] = None
        await self.store.change_execution(execution["id"], {"runtime": runtime}, actor="factory")
        return True

    async def before_model_request(self, context: SessionRequestContext) -> None:
        async with self.service.lock:
            execution = await self._guard(context)
            factory = await self.store.get_factory(execution["factory_id"])
            models = factory.get("config", {}).get("allowed_models")
            physical_model = getattr(context, "model", None)
            if (
                models is not None
                and physical_model is not None
                and not self.native._model_allowed(physical_model, models)
            ):
                await self.native.block(execution, "model_unavailable")
                raise FactoryError(
                    "model_unavailable", "This Model is outside the Factory configuration boundary."
                )
            if await self.native.limits.check(
                execution, check_runs=context.run_id not in execution["runtime"].get("runs", [])
            ):
                raise FactoryError(
                    "budget_exhausted", "Factory admission paused at its configured limit."
                )
            await self.native.limits.observe_run(execution, context.run_id)

    async def before_tool_call(
        self, context: SessionRequestContext, *, tool_name: str, tool_call_id: str
    ) -> str | None:
        async with self.service.lock:
            execution = await self._guard(context)
            from ._native_agent_text import PRIVATE_TOOLS, tool_refusal
            from ._native_policy import tool_permitted

            factory, member, _ = await self.native._scope(execution)
            if not await tool_permitted(
                self.service,
                [factory.get("config", {}), member.get("config", {})],
                tool_name,
                set(PRIVATE_TOOLS),
            ):
                return tool_refusal(tool_name)
            if tool_call_id in execution["runtime"].get("tool_receipts", []):
                return None
            if await self.native.limits.check(execution, resources=False):
                return "Factory admission paused at its configured limit."
            await self.native.limits.count_tool(execution, tool_call_id)
        return None

    async def run_finished(self, context: SessionRequestContext, *, outcome: str) -> None:
        async with self.service.lock:
            if self.native.quiescing:
                return
            execution = await self.native._find(context.binding.group_id)
            if execution["runtime"].get("run_id") != context.run_id or not await self.native._valid(
                execution
            ):
                return
            await self._acknowledge(execution)
            execution = await self.store.get_execution(execution["id"])
            await self._finished(execution, outcome)

    async def _finished(self, execution: Json, outcome: str) -> None:
        if outcome != "success":
            await self.native.block(execution, "run_failed")
            return
        runtime = execution["runtime"]
        if self.groups.has_descendants(
            execution["group_id"],
            execution["participant_id"],
            runtime.get("run_id", ""),
            runtime.get("group_epoch", ""),
        ):
            # A natural root answer can precede owned Sub-Agent/resource work.
            # Its eventual canonical continuation keeps this task admission.
            await self.store.change_execution(
                execution["id"], {"state": "running"}, actor="factory"
            )
            return
        await self.native.discussions.publish_answer(execution)
        execution = await self.store.change_execution(
            execution["id"], {"state": "idle"}, actor="factory"
        )
        if execution["runtime"].get("pending"):
            execution = await self.store.change_execution(
                execution["id"], {"state": "admitting"}, actor="factory"
            )
            self._launch_later(execution)
        elif execution["kind"] in {"task", "review"}:
            task = await self.store.get_task(execution["scope_id"])
            if (
                execution["kind"] == "review"
                and task["state"] == "review"
                and not (task.get("submission") or {}).get("reviewer_approved")
            ):
                task = await self.store.change_task(
                    task["id"],
                    {"state": "blocked", "reason": "review_input"},
                    actor="factory",
                    event="awaiting_review",
                )
                await self.native._publish("tasks", task)
            elif task["state"] == "working":
                task = await self.store.change_task(
                    task["id"],
                    {
                        "state": "ready" if task["runtime"].get("wakes") else "blocked",
                        "reason": "" if task["runtime"].get("wakes") else "input",
                    },
                    actor="factory",
                    event="awaiting_input",
                )
                await self.native._publish("tasks", task)
        if execution["kind"] == "topic":
            topic = await self.store.get_topic(execution["scope_id"])
            if topic.get("direct_member_id"):
                await self.native._publish("topics", topic)
        self.service.request_schedule()
