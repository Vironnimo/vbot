"""Canonical Workflow Sessions: admission, delivery receipts, draining and restart recovery."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.agents.temporary import TemporaryAgentConfig
from core.agents.temporary_execution import TemporaryRunInput
from core.extensions import PreparedSessionDelivery, SessionRequestContext
from core.runs import ActiveRunError
from core.sessions import SessionAddress
from core.tools.availability import ToolAccess
from core.utils.logging import get_logger

from . import _limits, _workflows, agent_text
from ._native_agent_text import PRIVATE_TOOLS as NATIVE_PRIVATE_TOOLS
from .store import FINAL_STATES, FactoryError

type Json = dict[str, Any]

_LOGGER = get_logger("extensions.factory")

_PRIVATE = frozenset(agent_text.PRIVATE_TOOLS) | frozenset(NATIVE_PRIVATE_TOOLS)

_STEP_TOOLS = frozenset({"factory_submit_work", "factory_report_blocked"})

if TYPE_CHECKING:
    from ._execution import FactoryExecution


class WorkflowSessions:
    """Canonical Workflow Sessions: admission, delivery, drain and recovery."""

    def __init__(self, execution: FactoryExecution) -> None:
        self.execution = execution
        self.service = execution.service
        self.budgets = execution.budgets
        self._scheduled: set[str] = set()
        self._closing: set[str] = set()
        self._closed: dict[str, str] = {}
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._quiescing = False
        self._reconciled = False

    @property
    def store(self) -> Any:
        return self.service.store

    @property
    def groups(self) -> Any:
        return self.service.host.temporary_agents

    async def _lookup(self, group_id: str) -> tuple[Json, str]:
        card_id, step_id = _workflows.split_group(group_id)
        card = await self.store.get_card(card_id)
        if step_id not in card["runtime"].get("steps", {}):
            raise self.execution._error("stale_run")
        return card, step_id

    def _owns(self, state: Json, owner: Any) -> bool:
        return (
            not self._quiescing
            and owner is not None
            and owner.epoch == state.get("epoch")
            and owner.participant_id == state.get("participant")
        )

    async def before_model_request(self, context: SessionRequestContext) -> None:
        owner = context.execution_owner
        async with self.service.lock:
            card, step_id = await self._lookup(context.binding.group_id)
            state = card["runtime"]["steps"][step_id]
            if not self._owns(state, owner):
                raise self.execution._error("stale_run")
            if (
                native := getattr(self.service, "native", None)
            ) and not await native.workflows.model_permitted(card, getattr(context, "model", None)):
                raise self.execution._error("wrong_state")
            if state["status"] != "running" or not self.execution.running(card):
                raise self.execution._error("wrong_state")
            new_run = context.run_id not in state.get("budget", {}).get("runs", [])
            if await self.budgets._check_budget(card, step_id, check_runs=new_run):
                raise self.execution._error("budget_exhausted")
            if new_run:
                runtime = self.execution._runtime(card)
                _limits.observe_run(runtime, step_id, context.run_id)
                await self.store.change(
                    card["id"], {"runtime": runtime}, actor="factory", event=None
                )

    async def before_tool_call(
        self, context: SessionRequestContext, *, tool_name: str, tool_call_id: str
    ) -> str | None:
        owner = context.execution_owner
        if owner is None:
            return None
        async with self.service.lock:
            card, step_id = await self._lookup(context.binding.group_id)
            runtime = self.execution._runtime(card)
            state = runtime["steps"][step_id]
            if not self._owns(state, owner):
                raise self.execution._error("stale_run")
            if (
                native := getattr(self.service, "native", None)
            ) and not await native.workflows.tool_permitted(card, tool_name):
                from ._native_agent_text import tool_refusal

                return tool_refusal(tool_name)
            if state["status"] != "running" or not self.execution.running(card):
                return (
                    agent_text.limit_refusal(state["limit_hit"])
                    if state.get("limit_hit")
                    else agent_text.error_message("wrong_state")
                )
            if await self.budgets._check_budget(card, step_id, check_runs=False, resources=False):
                stopped = await self.store.get_card(card["id"])
                return agent_text.limit_refusal(stopped["runtime"]["steps"][step_id]["limit_hit"])
            _limits.count_tool_call(runtime, step_id)
            await self.store.change(card["id"], {"runtime": runtime}, actor="factory", event=None)
        return None

    async def _config(self, card: Json, step_id: str) -> TemporaryAgentConfig:
        runtime = card["runtime"]
        step = _workflows.step(runtime["workflow"], step_id)
        tools = _STEP_TOOLS | ({"factory_request_changes"} if step["returns_to"] else set())
        config = TemporaryAgentConfig(
            model="",
            **_workflows.profile(step),
            model_override=step["model"] or None,
            thinking_effort_override=step["effort"] or None,
            cwd=Path(runtime["cwd"]),
            tool_access=ToolAccess(mode="all", denied=tuple(sorted(_PRIVATE - tools))),
            allowed_skills=["*"],
            tools={},
            name=f"Factory {step['name']}",
            instructions=agent_text.rules(runtime["workflow"], step),
        )
        if native := getattr(self.service, "native", None):
            return cast(TemporaryAgentConfig, await native.workflows.config(card, config, tools))
        return config

    def _launch_later(self, card: Json, step_id: str) -> None:
        pending = card["runtime"]["steps"][step_id].get("pending")
        if not pending or self._quiescing or pending["id"] in self._scheduled:
            return
        token = pending["id"]
        self._scheduled.add(token)
        self.service.spawn(self._launch(card["id"], step_id, token))

    async def _wait_run(self, group_id: str, run_id: str | None) -> None:
        if not run_id:
            return
        inspected = (await self.groups.owned_runs(group_id, [run_id])).get(run_id)
        if inspected is not None and inspected.run is not None:
            try:
                await inspected.run.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The durable outcome is consumed by run_finished; waiting here
                # only ensures the prior Session has been released.
                pass

    def _current(self, card: Json, step_id: str, token: str) -> bool:
        state = card["runtime"].get("steps", {}).get(step_id, {})
        return (
            state.get("pending", {}).get("id") == token
            and state.get("status") == "running"
            and not self._quiescing
            and card["state"] not in FINAL_STATES
            and not card["runtime"].get("queued_action")
        )

    async def _launch(self, card_id: str, step_id: str, token: str) -> None:
        group_id = _workflows.group_id(card_id, step_id)
        try:
            async with self.service.lock:
                card = await self.store.get_card(card_id)
                if not self._current(card, step_id, token):
                    return
                if (
                    native := getattr(self.service, "native", None)
                ) and await native.workflows.refresh_pending(card, step_id):
                    return
                state = card["runtime"]["steps"][step_id]
                pending = dict(state["pending"])
                old_run = state.get("run")
            if not pending.get("cancel_previous"):
                await self._wait_run(group_id, old_run)
            async with self._group_locks.setdefault(group_id, asyncio.Lock()):
                # Only a new participant needs a closed group. Same-participant
                # continuations retain their delegated work and native resources.
                if pending["kind"] == "initial" or pending.get("cancel_previous"):
                    await self.groups.close_group(group_id)
                async with self.service.lock:
                    card = await self.store.get_card(card_id)
                    if not self._current(card, step_id, token):
                        return
                    if await self._acknowledge_pending(card, step_id) is not None:
                        return
                    if await self.budgets._check_budget(card, step_id, check_runs=True):
                        return
                    runtime = self.execution._runtime(card)
                    state = runtime["steps"][step_id]
                    participant = pending["participant"]
                    participants = state.setdefault("participants", {})
                    if participant not in participants:
                        binding = await self.groups.create(
                            group_id,
                            participant,
                            await self._config(card, step_id),
                            project_id=card["project_id"] or None,
                        )
                        participants[participant] = {
                            "agent_id": binding.address.agent_id,
                            "session_id": binding.address.session_id,
                            "project_id": binding.address.project_id,
                            "generation_id": binding.generation_id,
                        }
                    handle = await self.groups.open_group(group_id)
                    self._closed.pop(group_id, None)
                    # Consume this Retry before start: a crash must not authorize
                    # another attempt for a newly admitted Run.
                    resume_terminal = state["pending"].pop("retry_terminal", False) is True
                    if resume_terminal:
                        card = await self.execution._change(
                            card, {"runtime": runtime}, event="retry_consumed"
                        )
                        runtime = self.execution._runtime(card)
                        state = runtime["steps"][step_id]
                    admission = await self.groups.start(
                        handle,
                        participant,
                        TemporaryRunInput(pending["kind"], pending["content"], token),
                    )
                    state.update(run=admission.run_id, participant=participant, epoch=handle.epoch)
                    state.setdefault("runs", {})[participant] = admission.run_id
                    _limits.observe_run(runtime, step_id, admission.run_id)
                    step = _workflows.step(runtime["workflow"], step_id)
                    if (
                        resume_terminal
                        and admission.existing
                        and admission.status in {"failed", "cancelled", "interrupted"}
                    ):
                        self.execution._continue_input(
                            state,
                            agent_text.note(
                                "keep_working", {**card, "runtime": runtime}, step=step
                            ),
                        )
                        await self.execution._commit(
                            card,
                            runtime,
                            event="retry_reconciled",
                            detail={"step": step_id, "run_id": admission.run_id},
                        )
                        return
                    state.pop("pending", None)
                    title = pending["kind"] == "initial" and not state.get("titled")
                    if title:
                        state["titled"] = True
                    saved = await self.execution._change(
                        card,
                        {"runtime": runtime},
                        event="run_admitted",
                        detail={
                            "step": step_id,
                            "participant": participant,
                            "run_id": admission.run_id,
                        },
                    )
                    if title:
                        self.service.spawn(
                            self.groups.title_group(group_id, f"{card['title']} · {step['name']}")
                        )
                    if admission.existing and admission.status != "running":
                        if admission.status == "interrupted":
                            await self._recover(saved, step_id)
                        else:
                            await self._completed(
                                saved,
                                step_id,
                                "success" if admission.status == "completed" else "error",
                            )
        except ActiveRunError:
            # An automatic completion can win admission after the prior Run
            # settles. before_request adopts it and delivers this pending note;
            # it is not a failed participant or a request to retry a mutation.
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not isinstance(error, FactoryError):
                _LOGGER.exception(
                    "Factory participant admission failed (card=%s step=%s)", card_id, step_id
                )
            async with self.service.lock:
                try:
                    card = await self.store.get_card(card_id)
                except FactoryError as missing:
                    if missing.code == "card_not_found":
                        return
                    raise
                if self._current(card, step_id, token):
                    runtime = self.execution._runtime(card)
                    state = runtime["steps"][step_id]
                    state["note"] = agent_text.error_message("launch_failed")
                    self.execution._halt(state, "run_failed")
                    await self.execution._commit(
                        card, runtime, event="run_failed", detail={"step": step_id}
                    )
        finally:
            self._scheduled.discard(token)

    def _close_later(self, card: Json, step_id: str, *, cancel: bool = False) -> None:
        """Close a step's group once its current Run ended, or at once to cancel it."""
        if self._quiescing:
            return
        group_id = _workflows.group_id(card["id"], step_id)
        run_id = card["runtime"].get("steps", {}).get(step_id, {}).get("run", "")
        if not cancel and self._closed.get(group_id) == run_id:
            return
        key = f"{group_id}:{'cancel' if cancel else 'close'}:{run_id}"
        if key in self._closing:
            return
        self._closing.add(key)
        self.service.spawn(self._close(card["id"], step_id, key, run_id, cancel=cancel))

    async def _close(
        self, card_id: str, step_id: str, key: str, run_id: str, *, cancel: bool
    ) -> None:
        group_id = _workflows.group_id(card_id, step_id)
        try:
            if not cancel:
                await self._wait_run(group_id, run_id)
            async with self._group_locks.setdefault(group_id, asyncio.Lock()):
                async with self.service.lock:
                    card = await self.store.get_card(card_id)
                    state = card["runtime"].get("steps", {}).get(step_id, {})
                    if (
                        not cancel
                        and card["state"] not in FINAL_STATES
                        and (state.get("status") == "running" or state.get("run", "") != run_id)
                    ):
                        return
                await self.groups.close_group(group_id)
                self._closed[group_id] = run_id
                if self.service.host.publish_change is not None:
                    self.service.host.publish_change("cards", [card_id], card["revision"])
        except Exception as error:
            if isinstance(error, FactoryError) and error.code == "card_not_found":
                return
            self._cleanup_failed(card_id, error)
        finally:
            self._closing.discard(key)

    def _delete_later(self, card: Json) -> None:
        key = f"{card['id']}:delete"
        if self._quiescing or key in self._closing:
            return
        self._closing.add(key)
        self.service.spawn(self._delete(card, key))

    async def _delete(self, card: Json, key: str) -> None:
        try:
            for group_id in self.execution.group_ids(card):
                async with self._group_locks.setdefault(group_id, asyncio.Lock()):
                    await self.groups.close_group(group_id)
                    await self.groups.archive_group(group_id)
                self._group_locks.pop(group_id, None)
            async with self.service.lock:
                current = await self.store.get_card(card["id"])
                await self.store.delete_card(card["id"])
                _LOGGER.info("Factory Card deleted (card=%s)", card["id"])
                host = self.service.host
                if host.publish_change is not None:
                    host.publish_change("cards", [card["id"]], current["revision"] + 1)
        except Exception as error:
            if isinstance(error, FactoryError) and error.code == "card_not_found":
                return
            self._cleanup_failed(card["id"], error)
        finally:
            self._closing.discard(key)

    def _cleanup_failed(self, card_id: str, error: Exception) -> None:
        if isinstance(error, (FactoryError, OSError, ValueError)):
            _LOGGER.warning(
                "Factory group cleanup failed (card=%s error=%s)", card_id, type(error).__name__
            )
        else:
            _LOGGER.exception("Factory group cleanup failed (card=%s)", card_id)
        if not self._quiescing and self.store is not None:
            self.service.spawn(self._note_cleanup_failed(card_id))

    async def _note_cleanup_failed(self, card_id: str) -> None:
        async with self.service.lock:
            try:
                card = await self.store.get_card(card_id)
            except FactoryError:
                return
            runtime = self.execution._runtime(card)
            runtime["note"] = agent_text.error_message("cleanup_failed")
            await self.execution._change(card, {"runtime": runtime}, event="cleanup_failed")

    @staticmethod
    def _prepared_delivery(pending: Json) -> PreparedSessionDelivery:
        entries: tuple[str, ...] = (pending["content"],)
        if pending.get("budget_notice"):
            entries += (pending["budget_notice"]["content"],)
        content = "\n\n".join(entries)
        return PreparedSessionDelivery(
            pending["id"],
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
            entries,
            pending["delivery"]["settings_revision"],
            "factory_continuation",
        )

    async def _acknowledge_pending(self, card: Json, step_id: str) -> Json | None:
        pending = card["runtime"]["steps"][step_id].get("pending", {})
        target = pending.get("delivery")
        if target is None:
            return None
        delivery = self._prepared_delivery(pending)
        receipt = await self.groups.delivery_receipt(
            SessionAddress(**target["address"]), target["generation_id"], delivery.delivery_id
        )
        if receipt is None:
            return None
        if (
            receipt.receipt_id != delivery.delivery_id
            or receipt.content_hash != delivery.content_hash
            or receipt.effect_kind != delivery.effect_kind
            or receipt.carrier_location.get("kind") != "note"
            or type(receipt.carrier_location.get("sequence")) is not int
            or receipt.carrier_location["sequence"] < 0
        ):
            raise FactoryError("receipt_conflict", "Factory continuation receipt conflicts")
        runtime = self.execution._runtime(card)
        state = runtime["steps"][step_id]
        if pending.get("budget_notice"):
            self.budgets._acknowledge_budget_notice(runtime, pending["budget_notice"])
        state.pop("pending")
        return await self.execution._change(
            card, {"runtime": runtime}, event="delivery_acknowledged", detail={"step": step_id}
        )

    async def acknowledge_delivery(
        self, context: SessionRequestContext, delivery: PreparedSessionDelivery
    ) -> None:
        async with self.service.lock:
            card, step_id = await self._lookup(context.binding.group_id)
            if delivery.effect_kind == "factory_budget_notice":
                runtime = self.execution._runtime(card)
                recipient = f"{context.agent_id}/{context.session_id}"
                notice = runtime.get("budget_notices", {}).get(recipient, {})
                pending_notice = notice.get("pending", {})
                if pending_notice.get("id") == delivery.delivery_id:
                    self.budgets._acknowledge_budget_notice(
                        runtime, {**pending_notice, "recipient": recipient}
                    )
                    await self.store.change(
                        card["id"], {"runtime": runtime}, actor="factory", event=None
                    )
                return
            pending = card["runtime"]["steps"][step_id].get("pending", {})
            if pending.get("id") != delivery.delivery_id:
                return
            target = pending.get("delivery", {})
            if (
                target.get("generation_id") != context.binding.generation_id
                or target.get("address")
                != {
                    "project_id": context.binding.address.project_id,
                    "agent_id": context.agent_id,
                    "session_id": context.session_id,
                }
                or pending.get("participant") != context.binding.participant_id
                or self._prepared_delivery(pending) != delivery
            ):
                raise self.execution._error("stale_run")
            if await self._acknowledge_pending(card, step_id) is None:
                raise FactoryError("delivery_unacknowledged", "Factory continuation has no receipt")

    async def before_request(
        self, context: SessionRequestContext
    ) -> PreparedSessionDelivery | None:
        owner = context.execution_owner
        binding = context.binding
        if self._quiescing or owner is None:
            return None
        async with self.service.lock:
            card, step_id = await self._lookup(binding.group_id)
            runtime = self.execution._runtime(card)
            state = runtime["steps"][step_id]
            pending = state.get("pending", {})
            if (
                owner.epoch != state.get("epoch")
                or owner.participant_id != state.get("participant")
                or pending
                and pending.get("participant") != owner.participant_id
            ):
                raise self.execution._error("stale_run")
            if state["status"] != "running" or not self.execution.running(card):
                raise self.execution._error("wrong_state")
            new_run = context.run_id not in state.get("budget", {}).get("runs", [])
            if await self.budgets._check_budget(card, step_id, check_runs=new_run):
                raise self.execution._error("budget_exhausted")
            if (context.agent_id, context.session_id) != (
                binding.address.agent_id,
                binding.address.session_id,
            ):
                if new_run:
                    _limits.observe_run(runtime, step_id, context.run_id)
                    await self.store.change(
                        card["id"], {"runtime": runtime}, actor="factory", event=None
                    )
                return None
            if pending and pending.get("kind") != "continuation":
                raise self.execution._error("stale_run")
            if state.get("run") == context.run_id:
                if pending.get("delivery"):
                    return self._prepared_delivery(pending)
                return await self.budgets._budget_notice(card, step_id, context)
            _limits.observe_run(runtime, step_id, context.run_id)
            state["run"] = context.run_id
            state.setdefault("runs", {})[owner.participant_id] = context.run_id
            if pending and not pending.get("delivery"):
                pending["budget_notice"] = await self.budgets._prepare_budget_notice(
                    card, runtime, step_id, context
                )
                pending["delivery"] = {
                    "address": {
                        "project_id": binding.address.project_id,
                        "agent_id": binding.address.agent_id,
                        "session_id": binding.address.session_id,
                    },
                    "generation_id": binding.generation_id,
                    "settings_revision": str(card["revision"]),
                }
            saved = await self.execution._change(
                card,
                {"runtime": runtime},
                event="run_observed",
                detail={"step": step_id, "run_id": context.run_id},
            )
            if pending:
                return self._prepared_delivery(pending)
            return await self.budgets._budget_notice(saved, step_id, context)

    async def _pause_failed_handoff(
        self, card: Json, step_id: str, context: SessionRequestContext, outcome: str
    ) -> None:
        runtime = self.execution._runtime(card)
        state = runtime["steps"][step_id]
        pending = state.get("pending", {})
        owner, binding = context.execution_owner, context.binding
        participant = state.get("participant")
        address = state.get("participants", {}).get(participant, {})
        group_id = _workflows.group_id(card["id"], step_id)
        if (
            outcome == "success"
            or state["status"] != "running"
            or card["state"] in FINAL_STATES
            or pending.get("kind") != "continuation"
            or pending.get("participant") != participant
            or owner is None
            or (
                owner.extension,
                owner.group_id,
                owner.participant_id,
                owner.generation_id,
                owner.epoch,
            )
            != ("factory", group_id, participant, binding.generation_id, state.get("epoch"))
            or binding.group_id != group_id
            or binding.participant_id != participant
            or (context.agent_id, context.session_id)
            != (binding.address.agent_id, binding.address.session_id)
            or (address.get("project_id"), address.get("agent_id"), address.get("session_id"))
            != (binding.address.project_id, binding.address.agent_id, binding.address.session_id)
            or address.get("generation_id", binding.generation_id) != binding.generation_id
        ):
            return
        records = await self.groups.owned_runs(group_id, [context.run_id, state["run"]])
        inspected, previous = records.get(context.run_id), records.get(state["run"])
        if (
            inspected is None
            or previous is None
            or (previous.record.address, previous.record.generation_id)
            != (binding.address, binding.generation_id)
            or (
                context.run_id != state["run"]
                and inspected.record.start_sequence <= previous.record.start_sequence
            )
            or (inspected.record.owner, inspected.record.address, inspected.record.generation_id)
            != (owner, binding.address, binding.generation_id)
        ):
            return
        card = await self._acknowledge_pending(card, step_id) or card
        runtime = self.execution._runtime(card)
        state = runtime["steps"][step_id]
        state.update(run=context.run_id, note=agent_text.error_message("run_failed"))
        state.setdefault("runs", {})[participant] = context.run_id
        _limits.observe_run(runtime, step_id, context.run_id)
        self.execution._halt(state, "run_failed")
        await self.execution._commit(
            card, runtime, event="run_failed", detail={"step": step_id, "run_id": context.run_id}
        )

    async def run_finished(self, context: SessionRequestContext, *, outcome: str) -> None:
        if self._quiescing or context.execution_owner is None:
            return
        async with self.service.lock:
            if self._quiescing or self.store is None:
                return
            try:
                card, step_id = await self._lookup(context.binding.group_id)
            except FactoryError as error:
                if error.code in {"card_not_found", "stale_run"}:
                    return
                raise
            if card["runtime"].get("queued_action"):
                return
            state = card["runtime"]["steps"][step_id]
            if state.get("pending", {}).get("cancel_previous") and context.run_id == state.get(
                "run"
            ):
                # The step's new input replaces this Run; its launch cancelled it.
                return
            if (
                context.run_id != state.get("run")
                or context.execution_owner.epoch != state.get("epoch")
                or (outcome != "success" and state.get("pending"))
            ):
                await self._pause_failed_handoff(card, step_id, context, outcome)
                return
            card = await self._acknowledge_pending(card, step_id) or card
            state = card["runtime"]["steps"][step_id]
            if state["status"] != "running" or card["state"] in FINAL_STATES:
                self._close_later(card, step_id)
            elif state.get("pending"):
                self._launch_later(card, step_id)
            else:
                await self._completed(card, step_id, outcome)

    async def _completed(self, card: Json, step_id: str, outcome: str) -> None:
        runtime = self.execution._runtime(card)
        state = runtime["steps"][step_id]
        if state["status"] != "running" or card["state"] in FINAL_STATES:
            self._close_later(card, step_id)
            return
        if await self.budgets._check_budget(card, step_id, check_runs=True):
            return
        if outcome != "success":
            state["note"] = agent_text.error_message("run_failed")
            self.execution._halt(state, "run_failed")
            await self.execution._commit(
                card, runtime, event="run_failed", detail={"step": step_id}
            )
            return
        step = _workflows.step(runtime["workflow"], step_id)
        self.execution._continue_input(state, agent_text.note("keep_working", card, step=step))
        await self.execution._commit(card, runtime, event="nudged", detail={"step": step_id})

    async def _recover(self, card: Json, step_id: str) -> None:
        runtime = self.execution._runtime(card)
        state = runtime["steps"][step_id]
        state["restarts"] = int(state.get("restarts", 0)) + 1
        if state["restarts"] >= 2:
            state["note"] = agent_text.error_message("restart")
            self.execution._halt(state, "restart")
            await self.execution._commit(card, runtime, event="restart", detail={"step": step_id})
            return
        step = _workflows.step(runtime["workflow"], step_id)
        if not state.get("participant"):
            state["status"] = "pending"
            await self.execution._advance(card, runtime, event="restarted", actor="factory")
            return
        self.execution._continue_input(state, agent_text.note("restart", card, step=step))
        await self.execution._commit(card, runtime, event="restarted", detail={"step": step_id})

    async def _reconcile_card(self, card: Json) -> None:
        runtime = card["runtime"]
        if runtime.get("deleting"):
            self._delete_later(card)
            return
        if card["state"] in FINAL_STATES:
            if any(state.get("pending") for state in runtime.get("steps", {}).values()):
                # A finished Card no longer runs what was queued for it.
                runtime = self.execution._runtime(card)
                for state in runtime["steps"].values():
                    state.pop("pending", None)
                card = await self.execution._change(
                    card, {"runtime": runtime}, event="pending_dropped"
                )
            for step_id in card["runtime"].get("steps", {}):
                self._close_later(card, step_id, cancel=True)
            return
        if card["state"] in {"proposal", "queued"} or not self.execution.started(card):
            return
        for step_id in list(runtime["steps"]):
            card = await self._acknowledge_pending(card, step_id) or card
        for step_id, state in list(card["runtime"]["steps"].items()):
            card = await self.store.get_card(card["id"])
            state = card["runtime"]["steps"][step_id]
            if state["status"] == "child":
                child = state.get("child", "")
                try:
                    finished = (await self.store.get_card(child))["state"] in FINAL_STATES
                except FactoryError:
                    finished = True
                if finished:
                    self.service.spawn(self.execution.child_finished(child))
                continue
            if state["status"] != "running":
                if state.get("run"):
                    self._close_later(card, step_id)
                continue
            group_id = _workflows.group_id(card["id"], step_id)
            run_id = state.get("run")
            inspected = (
                (await self.groups.owned_runs(group_id, [run_id])).get(run_id) if run_id else None
            )
            if (
                inspected is not None
                and inspected.run is not None
                and inspected.run.status.value == "running"
            ):
                continue
            if state.get("pending"):
                self._launch_later(card, step_id)
            elif run_id:
                if inspected is not None and inspected.record.terminal_status == "completed":
                    await self._completed(card, step_id, "success")
                else:
                    await self._recover(card, step_id)
            else:
                runtime = self.execution._runtime(card)
                runtime["steps"][step_id]["status"] = "pending"
                await self.execution._advance(
                    card, runtime, event="recovered_initial", actor="factory"
                )
        card = await self.store.get_card(card["id"])
        if card["state"] == "working" and not any(
            state["status"] in {"running", "child"} for state in card["runtime"]["steps"].values()
        ):
            await self.execution._advance(
                card, self.execution._runtime(card), event="recovered_initial", actor="factory"
            )

    async def reconcile(self) -> None:
        if self._reconciled:
            return
        self._reconciled = True
        self._quiescing = False
        async with self.service.lock:
            cards = await self.store.list_cards()
            known = {group_id for card in cards for group_id in self.execution.group_ids(card)}
            if native := getattr(self.service, "native", None):
                known.update(await native.group_ids())
            for card in cards:
                try:
                    await self._reconcile_card(card)
                except Exception:
                    _LOGGER.exception("Factory Card recovery failed (card=%s)", card["id"])
        after = ""
        while True:
            page = await self.groups.groups(after=after, limit=100)
            for group_id in page:
                if group_id not in known:
                    await self.groups.close_group(group_id)
                    await self.groups.archive_group(group_id)
            if len(page) < 100:
                break
            after = page[-1]

    async def quiesce(self, reason: str = "extension") -> None:
        self._quiescing = True
        if self.service.host is not None:
            await self.groups.quiesce(reason)
