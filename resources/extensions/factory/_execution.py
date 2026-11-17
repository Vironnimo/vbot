"""Persisted Factory handoffs over the host's canonical owned execution groups.

A Card runs its Workflow snapshot as a graph: every step whose ``after`` steps
are done starts, so independent steps run in parallel. Each step has its own
execution group (``<card>.<step>``), participants and delivery intent; the
Card's state is derived from its steps after every transition. A step keeps
its Session across reruns; a step that can send work back judges with a fresh
participant on every run.
"""

from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from core.agents.temporary import TemporaryAgentConfig
from core.agents.temporary_execution import TemporaryRunInput
from core.extensions import PreparedSessionDelivery, SessionRequestContext
from core.runs import ActiveRunError
from core.sessions import SessionAddress
from core.tools import ToolContext
from core.tools.availability import ToolAccess
from core.utils.ids import new_id
from core.utils.logging import get_logger

from . import _limits, _workflows, agent_text
from ._native_agent_text import PRIVATE_TOOLS as NATIVE_PRIVATE_TOOLS
from .store import FINAL_STATES, RUNTIME_VERSION, FactoryError

type Json = dict[str, Any]
_LOGGER = get_logger("extensions.factory")
_PRIVATE = frozenset(agent_text.PRIVATE_TOOLS) | frozenset(NATIVE_PRIVATE_TOOLS)
_STEP_TOOLS = frozenset({"factory_submit_work", "factory_report_blocked"})
_RESULT_LIMIT = 100000
_RETRYABLE = frozenset(
    {"run_failed", "restart", "budget_exhausted", "upgraded", "child_failed", "native_paused"}
)


def _sum_reports(reports: list[Json]) -> Json:
    """Add usage reports of several execution groups."""
    total: Json = {}
    for report in reports:
        for key, value in report.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            total[key] = total.get(key, 0) + value
    return total


class FactoryExecution:
    """Transitions run under the service lock; draining never holds that lock.

    A durable pending input is a handoff intent, not another Run queue. Its stable
    request id is admitted/deduplicated by TemporaryExecutionGroups. A background
    task waits for the old Run's *terminal publication* before closing a group or
    admitting more work: run_finished itself precedes that publication.
    """

    def __init__(self, service: Any) -> None:
        self.service = service
        self._scheduled: set[str] = set()
        self._closing: set[str] = set()
        # Groups closed after their Run, by the Run; a launch reopens its group.
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

    def _error(self, code: str, **values: Any) -> FactoryError:
        return FactoryError(code, agent_text.error_message(code, **values))

    @staticmethod
    def _runtime(card: Json) -> Json:
        return deepcopy(card.get("runtime", {}))

    @staticmethod
    def _text(arguments: Json, field: str) -> str:
        value = arguments.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 20000:
            raise FactoryError(
                "missing_text", agent_text.error_message("missing_text", field=field)
            )
        return value

    @staticmethod
    def started(card: Json) -> bool:
        return bool(card.get("runtime", {}).get("steps"))

    @staticmethod
    def group_ids(card: Json) -> list[str]:
        """Every execution group of a Card: its steps' and an earlier Factory's Card group."""
        workflow = card.get("runtime", {}).get("workflow") or {}
        return [card["group_id"]] + [
            _workflows.group_id(card["id"], step["id"]) for step in workflow.get("steps", [])
        ]

    @staticmethod
    def running(card: Json) -> bool:
        """Whether the Card occupies a WIP slot: one of its Agent steps runs."""
        return (
            card["state"] not in FINAL_STATES
            and not card.get("runtime", {}).get("queued_action")
            and any(
                state.get("status") == "running"
                for state in card.get("runtime", {}).get("steps", {}).values()
            )
        )

    async def _lookup(self, group_id: str) -> tuple[Json, str]:
        card_id, step_id = _workflows.split_group(group_id)
        card = await self.store.get_card(card_id)
        if step_id not in card["runtime"].get("steps", {}):
            raise self._error("stale_run")
        return card, step_id

    # Budgets

    async def budget_status(
        self, card: Json, step_id: str, *, resources: bool = True, report_all: bool = False
    ) -> Json:
        runtime = card["runtime"]
        card_limits, step_limits = _limits.configured(runtime, step_id)
        result: Json = {}
        for scope, limits in (("card", card_limits), ("step", step_limits)):
            report = None
            if resources and (report_all or limits.get("tokens") or limits.get("cost_usd")):
                host = self.service.host
                if scope == "step":
                    report = await host.execution_usage(
                        _workflows.group_id(card["id"], step_id),
                        runtime["steps"][step_id].get("budget", {}).get("runs", []),
                    )
                else:
                    report = _sum_reports(
                        [
                            await host.execution_usage(group_id, None)
                            for group_id in self.group_ids(card)
                        ]
                    )
            result[scope] = {
                "limits": limits,
                "usage": _limits.consumption(runtime, scope, step_id, report),
            }
        return result

    async def _check_budget(
        self, card: Json, step_id: str, *, check_runs: bool, resources: bool = True
    ) -> bool:
        if (
            native := getattr(self.service, "native", None)
        ) and not await native.workflows.permitted(card):
            await native.workflows.halt(card)
            return True
        if (native := getattr(self.service, "native", None)) and await native.limits.workflow_check(
            card, check_runs=check_runs, resources=resources
        ):
            runtime = self._runtime(card)
            for state in runtime.get("steps", {}).values():
                if state.get("status") == "running":
                    self._halt(state, "budget_exhausted")
            await self._commit(card, runtime, event="budget_exhausted")
            return True
        status = await self.budget_status(card, step_id, resources=resources)
        for scope, item in status.items():
            limits = (
                item["limits"] if resources else {"tool_calls": item["limits"].get("tool_calls", 0)}
            )
            hit = _limits.reached(limits, item["usage"], scope, check_runs=check_runs)
            if hit is not None:
                runtime = self._runtime(card)
                state = runtime["steps"][step_id]
                state.update(limit_hit=hit, note=_limits.message(hit))
                self._halt(state, "budget_exhausted")
                await self._commit(card, runtime, event="budget_exhausted", detail=hit)
                return True
        return False

    async def _prepare_budget_notice(
        self, card: Json, runtime: Json, step_id: str, context: SessionRequestContext
    ) -> Json | None:
        recipient = f"{context.agent_id}/{context.session_id}"
        notice = runtime.setdefault("budget_notices", {}).setdefault(recipient, {"seen": []})
        warnings = _limits.warnings(
            runtime,
            step_id,
            await self.budget_status({**card, "runtime": runtime}, step_id),
            notice["seen"],
        )
        if not warnings:
            return None
        return {
            "id": new_id("fnotice"),
            "recipient": recipient,
            "content": agent_text.budget_warning(warnings),
            "keys": [warning["key"] for warning in warnings],
            "revision": str(card["revision"]),
        }

    @staticmethod
    def _acknowledge_budget_notice(runtime: Json, pending: Json) -> None:
        notice = runtime.setdefault("budget_notices", {}).setdefault(
            pending["recipient"], {"seen": []}
        )
        notice["seen"] = list(dict.fromkeys([*notice["seen"], *pending["keys"]]))
        notice.pop("pending", None)

    async def _budget_notice(
        self, card: Json, step_id: str, context: SessionRequestContext
    ) -> PreparedSessionDelivery | None:
        runtime = self._runtime(card)
        recipient = f"{context.agent_id}/{context.session_id}"
        notice = runtime.setdefault("budget_notices", {}).setdefault(recipient, {"seen": []})
        pending = notice.get("pending")
        if pending is None:
            pending = await self._prepare_budget_notice(card, runtime, step_id, context)
            if pending is None:
                return None
            notice["pending"] = pending
            await self.store.change(card["id"], {"runtime": runtime}, actor="factory", event=None)
        content = pending["content"]
        return PreparedSessionDelivery(
            pending["id"],
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
            (content,),
            pending["revision"],
            "factory_budget_notice",
        )

    # Session runtime hooks

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
                raise self._error("stale_run")
            if (
                native := getattr(self.service, "native", None)
            ) and not await native.workflows.model_permitted(card, getattr(context, "model", None)):
                raise self._error("wrong_state")
            if state["status"] != "running" or not self.running(card):
                raise self._error("wrong_state")
            new_run = context.run_id not in state.get("budget", {}).get("runs", [])
            if await self._check_budget(card, step_id, check_runs=new_run):
                raise self._error("budget_exhausted")
            if new_run:
                runtime = self._runtime(card)
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
            runtime = self._runtime(card)
            state = runtime["steps"][step_id]
            if not self._owns(state, owner):
                raise self._error("stale_run")
            if (
                native := getattr(self.service, "native", None)
            ) and not await native.workflows.tool_permitted(card, tool_name):
                from ._native_agent_text import tool_refusal

                return tool_refusal(tool_name)
            if state["status"] != "running" or not self.running(card):
                return (
                    agent_text.limit_refusal(state["limit_hit"])
                    if state.get("limit_hit")
                    else agent_text.error_message("wrong_state")
                )
            if await self._check_budget(card, step_id, check_runs=False, resources=False):
                stopped = await self.store.get_card(card["id"])
                return agent_text.limit_refusal(stopped["runtime"]["steps"][step_id]["limit_hit"])
            _limits.count_tool_call(runtime, step_id)
            await self.store.change(card["id"], {"runtime": runtime}, actor="factory", event=None)
        return None

    async def _change(
        self, card: Json, patch: Json, *, actor: str = "factory", event: str, **extra: Any
    ) -> Json:
        saved = await self.store.change(card["id"], patch, actor=actor, event=event, **extra)
        await self.service.changed(saved, card, actor=actor)
        return cast(Json, saved)

    # Card state

    @staticmethod
    def derive(runtime: Json) -> tuple[str, str]:
        """The Card's (state, waiting_reason) from its steps."""
        blocker = runtime.get("blocker")
        if blocker:
            return "waiting", str(blocker["reason"])
        if runtime.get("queued_action"):
            return "queued", ""
        steps = runtime["steps"]
        order = [step["id"] for step in runtime["workflow"]["steps"]]
        if any(steps[step_id]["status"] == "approval" for step_id in order):
            return "waiting", "checkpoint"
        for step_id in order:
            state = steps[step_id]
            if state["status"] == "waiting":
                return "waiting", str(state.get("reason") or "blocked")
        if all(steps[step_id]["status"] == "done" for step_id in order):
            if runtime["workflow"].get("final_approval") and not runtime.get("accepted"):
                return "waiting", "finished"
            return "done", ""
        return "working", ""

    @staticmethod
    def _halt(state: Json, reason: str) -> None:
        """A step stops for the user; its pending input stays for a Retry."""
        state.update(status="waiting", reason=reason)

    async def _commit(
        self,
        card: Json,
        runtime: Json,
        *,
        event: str,
        actor: str = "factory",
        patch: Json | None = None,
        cancel: set[str] | frozenset[str] = frozenset(),
        **extra: Any,
    ) -> Json:
        """Save a transition with the Card state derived from its steps, then act on it.

        ``cancel`` names steps whose current Run must stop; a step that already
        runs again cancels it when its new input starts.
        """
        stopped = set()
        for step_id in cancel:
            step_state = runtime["steps"][step_id]
            if step_state["status"] == "running" and step_state.get("pending"):
                step_state["pending"]["cancel_previous"] = True
            else:
                stopped.add(step_id)
        # A continuation after a halt or a child Card needs a new WIP slot.
        # Persist the prepared inputs, so admission resumes the same snapshot
        # without replaying the user's action or resetting its counters.
        if not self.running(card) and any(
            item["status"] == "running" for item in runtime.get("steps", {}).values()
        ):
            limit, _ = self.service.limits()
            occupied = sum(
                self.running(item)
                for item in await self.store.list_cards()
                if item["id"] != card["id"]
            )
            if native := getattr(self.service, "native", None):
                occupied += await native.running()
            if occupied >= limit or (
                native is not None and not await native.workflows.admissible(card, limit)
            ):
                runtime["queued_action"] = {
                    "accepted": "accept",
                    "answered": "answer",
                    "sent_back": "send_back",
                    "retried": "retry",
                }.get(event, "resume")
        state, reason = self.derive(runtime)
        saved = await self._change(
            card,
            {**(patch or {}), "state": state, "waiting_reason": reason, "runtime": runtime},
            actor=actor,
            event=event,
            **extra,
        )
        self._follow_up(saved, stopped)
        return saved

    def _follow_up(self, card: Json, cancel: set[str] | frozenset[str] = frozenset()) -> None:
        """Launch pending inputs and release the groups of steps that stopped running."""
        runtime = card["runtime"]
        if runtime.get("deleting"):
            return
        for step_id, state in runtime.get("steps", {}).items():
            if step_id in cancel:
                self._close_later(card, step_id, cancel=True)
            elif (
                state["status"] == "running"
                and state.get("pending")
                and not runtime.get("queued_action")
            ):
                self._launch_later(card, step_id)
            elif state["status"] != "running" and state.get("run"):
                self._close_later(card, step_id)

    # Workflow positions

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

    @staticmethod
    def _participant(state: Json, step: Json) -> str:
        """The step's latest participant; a judging step gets a fresh one per run."""
        participant = _workflows.participant(step, int(state.get("sequence", 1)))
        return (
            f"{participant}-p{state['native_policy_generation']}"
            if state.get("native_policy_generation")
            else participant
        )

    @staticmethod
    def latest(outputs: list[Json], workflow: Json, step_ids: set[str]) -> list[Json]:
        """The newest hand-in of each given step, in Workflow order."""
        newest = {
            output["step"]: output
            for output in outputs
            if output["kind"] == "work" and output["step"] in step_ids
        }
        return [newest[step["id"]] for step in workflow["steps"] if step["id"] in newest]

    async def _history(self, card: Json, recordings: list[Json]) -> Json:
        """The Card's history including the outputs this transition records with it."""
        history: Json = await self.store.history(card["id"])
        history["outputs"].extend(recordings)
        history["trigger"] = await self.store.trigger_event(card["id"])
        return history

    def _prepare_run(self, card: Json, runtime: Json, step: Json, history: Json) -> None:
        """Make a step run: its Session continues, or a fresh one starts."""
        state = runtime["steps"][step["id"]]
        workflow = runtime["workflow"]
        _limits.new_execution(state)
        state["passes"] = int(state.get("passes", 0)) + 1
        if step["returns_to"]:
            state["sequence"] = int(state.get("sequence", 0)) + 1
        participant = self._participant(state, step)
        note = state.pop("input", None) or {"kind": "refresh", "text": ""}
        updated = {**card, "runtime": runtime}
        earlier = self.latest(
            history["outputs"], workflow, _workflows.ancestors(workflow, step["id"])
        )
        if participant in state.get("participants", {}):
            content = agent_text.note(
                note["kind"],
                updated,
                note["text"],
                earlier if note["kind"] == "refresh" else [],
                step=step,
                source=note.get("source", ""),
            )
            kind = "continuation"
        else:
            content = agent_text.step_input(
                updated,
                step,
                comments=history["comments"],
                earlier=earlier,
                findings=[
                    item
                    for item in history["outputs"]
                    if item["kind"] == "changes" and item["step"] == step["id"]
                ],
                note=note,
                trigger=history.get("trigger"),
            )
            kind = "initial"
        for key in ("reason", "question", "note"):
            state.pop(key, None)
        state.update(
            status="running",
            pending={
                "id": new_id("fci"),
                "participant": participant,
                "kind": kind,
                "content": content,
                "cancel_previous": False,
            },
        )

    def _continue_input(self, state: Json, content: str, *, cancel: bool = False) -> None:
        """Queue a continuation for the step's current participant."""
        for key in ("reason", "question", "note"):
            state.pop(key, None)
        state.update(
            status="running",
            pending={
                "id": new_id("fci"),
                "participant": state["participant"],
                "kind": "continuation",
                "content": content,
                "cancel_previous": cancel,
            },
        )

    async def _advance(
        self,
        card: Json,
        runtime: Json,
        *,
        event: str,
        actor: str,
        patch: Json | None = None,
        cancel: set[str] | frozenset[str] = frozenset(),
        outputs: list[Json] | None = None,
        **extra: Any,
    ) -> Json:
        """Start every step whose predecessors are done, then save the transition."""
        workflow = runtime["workflow"]
        updated = {**card, **(patch or {}), "runtime": runtime}
        recordings = list(outputs or [])
        history: Json | None = None
        progressed = True
        while progressed:
            progressed = False
            for step in workflow["steps"]:
                state = runtime["steps"][step["id"]]
                if state["status"] != "pending" or any(
                    runtime["steps"][item]["status"] != "done" for item in step["after"]
                ):
                    continue
                if step["approval"] and not state.get("approved"):
                    state["status"] = "approval"
                    continue
                if step["kind"] == "workflow":
                    if history is None:
                        history = await self._history(card, recordings)
                    output = await self._start_child(updated, runtime, step, history)
                    if output is not None:
                        recordings.append(output)
                        history["outputs"].append(output)
                        progressed = True
                    continue
                if history is None:
                    history = await self._history(card, recordings)
                self._prepare_run(updated, runtime, step, history)
        if recordings:
            extra["output"] = recordings
        return await self._commit(
            card, runtime, event=event, actor=actor, patch=patch, cancel=cancel, **extra
        )

    async def _start_child(
        self, card: Json, runtime: Json, step: Json, history: Json
    ) -> Json | None:
        """Start a workflow step's child Card; its output when the step needs no wait."""
        state = runtime["steps"][step["id"]]
        workflow = runtime["workflow"]
        earlier = self.latest(
            history["outputs"], workflow, _workflows.ancestors(workflow, step["id"])
        )
        try:
            child = await self.service.create_child({**card, "runtime": runtime}, step, earlier)
        except FactoryError as error:
            state["note"] = str(error)
            self._halt(state, "child_failed")
            return None
        state["passes"] = int(state.get("passes", 0)) + 1
        state.pop("note", None)
        state.pop("reason", None)
        state["child"] = child["id"]
        if step["wait"]:
            state["status"] = "child"
            return None
        state["status"] = "done"
        return {
            "step": step["id"],
            "step_name": step["name"],
            "kind": "work",
            "pass": state["passes"],
            "participant_id": "",
            "summary": f"Started Card {child['id']} with Workflow {child['workflow_name']!r}.",
            "result": "",
        }

    async def child_finished(self, child_id: str) -> None:
        """A child Card ended: its parent's workflow step is done, or waits for the user."""
        async with self.service.lock:
            if self._quiescing or self.store is None:
                return
            try:
                child = await self.store.get_card(child_id)
                card = await self.store.get_card(child["parent_id"])
            except FactoryError as error:
                if error.code == "card_not_found":
                    return
                raise
            if card["state"] in FINAL_STATES or child["state"] not in FINAL_STATES:
                return
            runtime = self._runtime(card)
            step_id = next(
                (
                    key
                    for key, state in runtime.get("steps", {}).items()
                    if state.get("child") == child_id and state["status"] == "child"
                ),
                None,
            )
            if step_id is None:
                return
            step = _workflows.step(runtime["workflow"], step_id)
            state = runtime["steps"][step_id]
            if child["state"] == "discarded":
                state["note"] = agent_text.error_message("child_discarded", card=child_id)
                self._halt(state, "child_failed")
                await self._commit(card, runtime, event="child_discarded")
                return
            history = await self.store.history(child_id)
            results = self.latest(
                history["outputs"],
                child["runtime"].get("workflow", {"steps": []}),
                {item["id"] for item in _workflows.sinks(child["runtime"]["workflow"])},
            )
            state["status"] = "done"
            output = {
                "step": step_id,
                "step_name": step["name"],
                "kind": "work",
                "pass": int(state.get("passes", 1)),
                "participant_id": "",
                "summary": child["summary"] or f"Card {child_id} finished.",
                "result": "\n\n".join(
                    f"{item['step_name']}: {item['result'] or item['summary']}" for item in results
                ),
            }
            await self._advance(
                card, runtime, event="child_finished", actor="factory", outputs=[output]
            )

    # Launching inputs

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
                    if await self._check_budget(card, step_id, check_runs=True):
                        return
                    runtime = self._runtime(card)
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
                        card = await self._change(
                            card, {"runtime": runtime}, event="retry_consumed"
                        )
                        runtime = self._runtime(card)
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
                        self._continue_input(
                            state,
                            agent_text.note(
                                "keep_working", {**card, "runtime": runtime}, step=step
                            ),
                        )
                        await self._commit(
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
                    saved = await self._change(
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
                    runtime = self._runtime(card)
                    state = runtime["steps"][step_id]
                    state["note"] = agent_text.error_message("launch_failed")
                    self._halt(state, "run_failed")
                    await self._commit(card, runtime, event="run_failed", detail={"step": step_id})
        finally:
            self._scheduled.discard(token)

    # Participant Tools

    async def _guard(self, context: ToolContext) -> tuple[Json, str]:
        owner = context.execution_owner
        if owner is None or owner.extension != "factory" or context.request_turn_end_hook is None:
            raise self._error("participant_required")
        card, step_id = await self._lookup(owner.group_id)
        state = card["runtime"]["steps"][step_id]
        if (
            state.get("run") != context.run_id
            or state.get("epoch") != owner.epoch
            or state.get("participant") != owner.participant_id
            or state.get("pending")
        ):
            raise self._error("stale_run")
        if state["status"] != "running" or not self.running(card):
            raise self._error("wrong_state")
        return cast(Json, card), step_id

    @staticmethod
    def _output(runtime: Json, step_id: str, kind: str) -> Json:
        state = runtime["steps"][step_id]
        step = _workflows.step(runtime["workflow"], step_id)
        return {
            "step": step_id,
            "step_name": step["name"],
            "kind": kind,
            "pass": int(state.get("passes", 1)),
            "participant_id": state.get("participant", ""),
        }

    async def submit_work(self, context: ToolContext, arguments: Json) -> Json:
        card, step_id = await self._guard(context)
        runtime = self._runtime(card)
        summary = self._text(arguments, "summary")
        result = arguments.get("result")
        if result is not None and not isinstance(result, str):
            raise self._error("missing_text", field="result")
        if isinstance(result, str) and len(result) > _RESULT_LIMIT:
            raise FactoryError(
                "text_too_long",
                f"Nothing was changed. result exceeds {_RESULT_LIMIT} characters. "
                "Shorten it and submit again.",
            )
        output = self._output(runtime, step_id, "work") | {
            "summary": summary,
            "result": result or "",
        }
        state = runtime["steps"][step_id]
        state.update(status="done", restarts=0)
        saved = await self._advance(
            card,
            runtime,
            event="submitted",
            actor="agent",
            patch={"summary": summary},
            outputs=[output],
            detail={"step": step_id},
        )
        context.request_turn_end()
        return agent_text.tool_result("submitted", saved)

    async def report_blocked(self, context: ToolContext, arguments: Json) -> Json:
        card, step_id = await self._guard(context)
        question = self._text(arguments, "question")
        runtime = self._runtime(card)
        state = runtime["steps"][step_id]
        state.update(question=question, restarts=0)
        self._halt(state, "blocked")
        saved = await self._commit(
            card, runtime, event="blocked", actor="agent", detail={"step": step_id}
        )
        context.request_turn_end()
        return agent_text.tool_result("blocked", saved)

    async def request_changes(self, context: ToolContext, arguments: Json) -> Json:
        card, step_id = await self._guard(context)
        runtime = self._runtime(card)
        step = _workflows.step(runtime["workflow"], step_id)
        findings = arguments.get("findings")
        if isinstance(findings, str) and findings.strip():
            findings = [findings]
        if (
            not isinstance(findings, list)
            or not findings
            or len(findings) > 100
            or any(
                not isinstance(value, str) or not value.strip() or len(value) > 20000
                for value in findings
            )
        ):
            raise self._error("invalid_findings")
        if not step["returns_to"]:
            raise self._error("participant_required")
        state = runtime["steps"][step_id]
        state["restarts"] = 0
        target = step["returns_to"]
        output = self._output(runtime, step_id, "changes") | {
            "findings": findings,
            "target": target,
        }
        detail = {"step": step_id, "target": target}
        if int(state.get("rounds", 0)) >= int(step["rounds"]):
            self._halt(state, "no_agreement")
            saved = await self._commit(
                card,
                runtime,
                event="changes_requested",
                actor="agent",
                output=[output],
                detail=detail,
            )
        else:
            state["rounds"] = int(state.get("rounds", 0)) + 1
            cancel = await self._reset(
                runtime,
                target,
                {"kind": "rework", "text": "\n".join(findings), "source": step["name"]},
                keep={step_id},
            )
            saved = await self._advance(
                card,
                runtime,
                event="changes_requested",
                actor="agent",
                outputs=[output],
                cancel=cancel,
                detail=detail,
            )
        context.request_turn_end()
        return agent_text.tool_result("changes_requested", saved)

    async def _reset(
        self, runtime: Json, target: str, note: Json, *, keep: set[str] | None = None
    ) -> set[str]:
        """Make the target step and every step after it run again, the target first.

        Returns the running steps whose current Run must be cancelled; ``keep``
        names steps whose Run is ending on its own. Child Cards of reset steps are
        discarded.
        """
        workflow = runtime["workflow"]
        later = _workflows.dependents(workflow, target)
        cancel: set[str] = set()
        for step_id in later | {target}:
            state = runtime["steps"][step_id]
            if state["status"] == "child" and state.get("child"):
                await self.service.discard_child(state["child"])
            if state["status"] == "running" and step_id not in (keep or set()):
                cancel.add(step_id)
            state["status"] = "pending"
            for key in ("pending", "reason", "question", "note", "input", "child"):
                state.pop(key, None)
            if step_id in later:
                # Steps after the target ask for approval again; the target keeps its own.
                state.pop("approved", None)
        runtime["steps"][target]["input"] = note
        return cancel

    # Operator actions

    def _waiting(self, card: Json) -> Json:
        if card["state"] != "waiting" or card["runtime"].get("deleting"):
            raise self._error("wrong_state")
        return self._runtime(card)

    @staticmethod
    def _stopped_step(runtime: Json, step_id: str, reasons: set[str] | frozenset[str]) -> str:
        """The named step, or the first step that stopped for one of ``reasons``."""
        steps = runtime.get("steps", {})
        if step_id:
            state = steps.get(step_id)
            reason = (
                "checkpoint"
                if state and state["status"] == "approval"
                else (state or {}).get("reason")
            )
            if state is None or state["status"] not in {"waiting", "approval"}:
                raise FactoryError(
                    "invalid_step", "That step does not wait for you. Refresh the Card."
                )
            if reason not in reasons:
                raise FactoryError(
                    "invalid_step", "That step waits for another action. Refresh the Card."
                )
            return step_id
        for step in runtime.get("workflow", {}).get("steps", []):
            state = steps[step["id"]]
            reason = "checkpoint" if state["status"] == "approval" else state.get("reason")
            if state["status"] in {"waiting", "approval"} and reason in reasons:
                return str(step["id"])
        return ""

    async def accept(self, card: Json, step_id: str = "") -> Json:
        runtime = self._waiting(card)
        target = self._stopped_step(runtime, step_id, {"checkpoint", "no_agreement"})
        if not target:
            if card["waiting_reason"] != "finished":
                raise self._error("wrong_state")
            runtime["accepted"] = True
            return await self._commit(card, runtime, event="accepted", actor="user")
        state = runtime["steps"][target]
        for item in runtime["steps"].values():
            item.pop("rounds", None)
        if state["status"] == "approval":
            state.update(status="pending", approved=True)
        else:
            # The user overrules the step that kept sending work back.
            state["status"] = "done"
            state.pop("reason", None)
        return await self._advance(
            card, runtime, event="accepted", actor="user", detail={"step": target}
        )

    @staticmethod
    def _default_target(runtime: Json) -> str:
        """The step a send-back without a chosen step reworks."""
        workflow = runtime["workflow"]
        steps = runtime["steps"]
        waiting = [step for step in workflow["steps"] if steps[step["id"]]["status"] == "waiting"]
        current = waiting[0] if waiting else _workflows.sinks(workflow)[-1]
        return str(current.get("returns_to") or current["id"])

    async def answer(self, card: Json, text: str, step_id: str = "") -> Json:
        runtime = self._waiting(card)
        target = self._stopped_step(runtime, step_id, {"blocked"})
        if not target:
            raise FactoryError(
                "not_blocked",
                "The Card has no open question. Use Send back with your comment instead.",
            )
        state = runtime["steps"][target]
        state["restarts"] = 0
        self._continue_input(state, agent_text.note("answer", card, text))
        return await self._commit(
            card, runtime, event="answered", actor="user", comment=text, detail={"step": target}
        )

    async def send_back(self, card: Json, comment: str, step_id: str = "") -> Json:
        runtime = self._waiting(card)
        if runtime.get("blocker"):
            raise self._error("wrong_state")
        target = step_id or self._default_target(runtime)
        for item in runtime["steps"].values():
            item.pop("rounds", None)
            item["restarts"] = 0
        runtime.pop("accepted", None)
        cancel = await self._reset(runtime, target, {"kind": "send_back", "text": comment})
        return await self._advance(
            card,
            runtime,
            event="sent_back",
            actor="user",
            comment=comment,
            cancel=cancel,
            detail={"target": target},
        )

    async def retry(self, card: Json, step_id: str = "") -> Json:
        runtime = self._waiting(card)
        if runtime.get("blocker"):
            # The Card could not start: the scheduler starts it again.
            runtime.pop("blocker")
            return await self._change(
                card,
                {"state": "queued", "waiting_reason": "", "runtime": runtime},
                actor="user",
                event="retried",
            )
        target = self._stopped_step(runtime, step_id, _RETRYABLE)
        if not target:
            raise FactoryError(
                "not_retryable",
                "No step of this Card failed or was interrupted. Accept it or send it back.",
            )
        acknowledged = await self._acknowledge_pending(card, target)
        if acknowledged is not None:
            card = acknowledged
            runtime = self._runtime(card)
        state = runtime["steps"][target]
        step = _workflows.step(runtime["workflow"], target)
        reason = state.get("reason")
        if await self._check_budget(card, target, check_runs=True):
            return cast(Json, await self.store.get_card(card["id"]))
        state["restarts"] = 0
        pending = state.get("pending")
        if reason in {"upgraded", "child_failed"} or (not pending and not state.get("participant")):
            state["status"] = "pending"
            for key in ("reason", "note", "child"):
                state.pop(key, None)
            return await self._advance(
                card, runtime, event="retried", actor="user", detail={"step": target}
            )
        if pending:
            pending["retry_terminal"] = True
            for key in ("reason", "question", "note"):
                state.pop(key, None)
            state["status"] = "running"
        elif step["returns_to"]:
            # A step that judges earlier work starts over with a fresh participant.
            history = await self._history(card, [])
            passes = state.get("passes", 1)
            budget = deepcopy(state.get("budget", {}))
            self._prepare_run(card, runtime, step, history)
            state["passes"] = passes
            state["budget"] = budget
        else:
            self._continue_input(state, agent_text.note("keep_working", card, step=step))
        return await self._commit(
            card, runtime, event="retried", actor="user", detail={"step": target}
        )

    async def discard(self, card: Json) -> Json:
        if card["state"] in FINAL_STATES:
            raise self._error("wrong_state")
        runtime = self._runtime(card)
        runtime.pop("queued_action", None)
        for state in runtime.get("steps", {}).values():
            state.pop("pending", None)
            if state["status"] == "child" and state.get("child"):
                await self.service.discard_child(state["child"])
        saved = await self._change(
            card,
            {"state": "discarded", "waiting_reason": "", "runtime": runtime},
            actor="user",
            event="discarded",
        )
        for step_id in runtime.get("steps", {}):
            self._close_later(saved, step_id, cancel=True)
        return saved

    async def delete(self, card: Json) -> Json:
        if card["state"] not in FINAL_STATES:
            raise self._error("wrong_state")
        await self.store.check_card_deletion(card["id"])
        runtime = self._runtime(card)
        runtime["deleting"] = True
        for state in runtime.get("steps", {}).values():
            state.pop("pending", None)
        saved = await self._change(
            card, {"runtime": runtime}, actor="user", event="deletion_requested"
        )
        self._delete_later(saved)
        return {"id": card["id"], "pending": True}

    async def start(self, card: Json) -> Json:
        """Start a queued Card: resolve its Workflow and Project, then its first steps."""
        if card["state"] != "queued":
            raise self._error("wrong_state")
        runtime = self._runtime(card)
        if runtime.pop("queued_action", None):
            return await self._commit(card, runtime, event="started")
        workflow = await self.service.workflow_snapshot(card["workflow_id"])
        if workflow is None:
            runtime["blocker"] = {
                "reason": "workflow_missing",
                "note": agent_text.error_message("workflow_missing", workflow=card["workflow_id"]),
            }
            return await self._commit(card, {**runtime, "steps": {}}, event="admission_failed")
        if (
            native := getattr(self.service, "native", None)
        ) and not await native.workflows.definition_permitted(card, workflow):
            return cast(Json, await self.store.get_card(card["id"]))
        project, problem = await self.service.resolve_card_project(card, workflow)
        if problem:
            runtime["blocker"] = {"reason": "project_unresolved", "note": problem}
            runtime.setdefault("steps", {})
            return await self._commit(card, runtime, event="admission_failed")
        runtime.pop("blocker", None)
        runtime.update(
            version=RUNTIME_VERSION,
            workflow=workflow,
            cwd=project["cwd"],
            steps={step["id"]: {"status": "pending"} for step in workflow["steps"]},
        )
        return await self._advance(
            card,
            runtime,
            event="started",
            actor="factory",
            patch={"project_id": project["id"]},
            detail={"workflow": workflow["name"]},
        )

    # Closing groups

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
            for group_id in self.group_ids(card):
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
            runtime = self._runtime(card)
            runtime["note"] = agent_text.error_message("cleanup_failed")
            await self._change(card, {"runtime": runtime}, event="cleanup_failed")

    # Deliveries

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
        runtime = self._runtime(card)
        state = runtime["steps"][step_id]
        if pending.get("budget_notice"):
            self._acknowledge_budget_notice(runtime, pending["budget_notice"])
        state.pop("pending")
        return await self._change(
            card, {"runtime": runtime}, event="delivery_acknowledged", detail={"step": step_id}
        )

    async def acknowledge_delivery(
        self, context: SessionRequestContext, delivery: PreparedSessionDelivery
    ) -> None:
        async with self.service.lock:
            card, step_id = await self._lookup(context.binding.group_id)
            if delivery.effect_kind == "factory_budget_notice":
                runtime = self._runtime(card)
                recipient = f"{context.agent_id}/{context.session_id}"
                notice = runtime.get("budget_notices", {}).get(recipient, {})
                pending_notice = notice.get("pending", {})
                if pending_notice.get("id") == delivery.delivery_id:
                    self._acknowledge_budget_notice(
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
                raise self._error("stale_run")
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
            runtime = self._runtime(card)
            state = runtime["steps"][step_id]
            pending = state.get("pending", {})
            if (
                owner.epoch != state.get("epoch")
                or owner.participant_id != state.get("participant")
                or pending
                and pending.get("participant") != owner.participant_id
            ):
                raise self._error("stale_run")
            if state["status"] != "running" or not self.running(card):
                raise self._error("wrong_state")
            new_run = context.run_id not in state.get("budget", {}).get("runs", [])
            if await self._check_budget(card, step_id, check_runs=new_run):
                raise self._error("budget_exhausted")
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
                raise self._error("stale_run")
            if state.get("run") == context.run_id:
                if pending.get("delivery"):
                    return self._prepared_delivery(pending)
                return await self._budget_notice(card, step_id, context)
            _limits.observe_run(runtime, step_id, context.run_id)
            state["run"] = context.run_id
            state.setdefault("runs", {})[owner.participant_id] = context.run_id
            if pending and not pending.get("delivery"):
                pending["budget_notice"] = await self._prepare_budget_notice(
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
            saved = await self._change(
                card,
                {"runtime": runtime},
                event="run_observed",
                detail={"step": step_id, "run_id": context.run_id},
            )
            if pending:
                return self._prepared_delivery(pending)
            return await self._budget_notice(saved, step_id, context)

    async def _pause_failed_handoff(
        self, card: Json, step_id: str, context: SessionRequestContext, outcome: str
    ) -> None:
        runtime = self._runtime(card)
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
        runtime = self._runtime(card)
        state = runtime["steps"][step_id]
        state.update(run=context.run_id, note=agent_text.error_message("run_failed"))
        state.setdefault("runs", {})[participant] = context.run_id
        _limits.observe_run(runtime, step_id, context.run_id)
        self._halt(state, "run_failed")
        await self._commit(
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
        runtime = self._runtime(card)
        state = runtime["steps"][step_id]
        if state["status"] != "running" or card["state"] in FINAL_STATES:
            self._close_later(card, step_id)
            return
        if await self._check_budget(card, step_id, check_runs=True):
            return
        if outcome != "success":
            state["note"] = agent_text.error_message("run_failed")
            self._halt(state, "run_failed")
            await self._commit(card, runtime, event="run_failed", detail={"step": step_id})
            return
        step = _workflows.step(runtime["workflow"], step_id)
        self._continue_input(state, agent_text.note("keep_working", card, step=step))
        await self._commit(card, runtime, event="nudged", detail={"step": step_id})

    async def _recover(self, card: Json, step_id: str) -> None:
        runtime = self._runtime(card)
        state = runtime["steps"][step_id]
        state["restarts"] = int(state.get("restarts", 0)) + 1
        if state["restarts"] >= 2:
            state["note"] = agent_text.error_message("restart")
            self._halt(state, "restart")
            await self._commit(card, runtime, event="restart", detail={"step": step_id})
            return
        step = _workflows.step(runtime["workflow"], step_id)
        if not state.get("participant"):
            state["status"] = "pending"
            await self._advance(card, runtime, event="restarted", actor="factory")
            return
        self._continue_input(state, agent_text.note("restart", card, step=step))
        await self._commit(card, runtime, event="restarted", detail={"step": step_id})

    # Startup

    async def _reconcile_card(self, card: Json) -> None:
        runtime = card["runtime"]
        if runtime.get("deleting"):
            self._delete_later(card)
            return
        if card["state"] in FINAL_STATES:
            if any(state.get("pending") for state in runtime.get("steps", {}).values()):
                # A finished Card no longer runs what was queued for it.
                runtime = self._runtime(card)
                for state in runtime["steps"].values():
                    state.pop("pending", None)
                card = await self._change(card, {"runtime": runtime}, event="pending_dropped")
            for step_id in card["runtime"].get("steps", {}):
                self._close_later(card, step_id, cancel=True)
            return
        if card["state"] in {"proposal", "queued"} or not self.started(card):
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
                    self.service.spawn(self.child_finished(child))
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
                runtime = self._runtime(card)
                runtime["steps"][step_id]["status"] = "pending"
                await self._advance(card, runtime, event="recovered_initial", actor="factory")
        card = await self.store.get_card(card["id"])
        if card["state"] == "working" and not any(
            state["status"] in {"running", "child"} for state in card["runtime"]["steps"].values()
        ):
            await self._advance(
                card, self._runtime(card), event="recovered_initial", actor="factory"
            )

    async def reconcile(self) -> None:
        if self._reconciled:
            return
        self._reconciled = True
        self._quiescing = False
        async with self.service.lock:
            cards = await self.store.list_cards()
            known = {group_id for card in cards for group_id in self.group_ids(card)}
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
