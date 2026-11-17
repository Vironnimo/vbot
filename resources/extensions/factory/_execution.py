"""Persisted Factory handoffs over the host's canonical owned execution groups.

A Card runs its Workflow snapshot as a graph: every step whose ``after`` steps
are done starts, so independent steps run in parallel. Each step has its own
execution group (``<card>.<step>``), participants and delivery intent; the
Card's state is derived from its steps after every transition. A step keeps
its Session across reruns; a step that can send work back judges with a fresh
participant on every run.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

from core.tools import ToolContext
from core.utils.ids import new_id

from . import _limits, _workflows, agent_text
from ._workflow_budgets import WorkflowBudgets
from ._workflow_sessions import WorkflowSessions
from .store import FINAL_STATES, RUNTIME_VERSION, FactoryError

type Json = dict[str, Any]


_RESULT_LIMIT = 100000


_RETRYABLE = frozenset(
    {"run_failed", "restart", "budget_exhausted", "upgraded", "child_failed", "native_paused"}
)


class FactoryExecution:
    """Workflow graph transitions and durable operator/participant actions."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.budgets = WorkflowBudgets(self)
        self.sessions = WorkflowSessions(self)

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

    async def _change(
        self, card: Json, patch: Json, *, actor: str = "factory", event: str, **extra: Any
    ) -> Json:
        saved = await self.store.change(card["id"], patch, actor=actor, event=event, **extra)
        await self.service.changed(saved, card, actor=actor)
        return cast(Json, saved)

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
                self.sessions._close_later(card, step_id, cancel=True)
            elif (
                state["status"] == "running"
                and state.get("pending")
                and not runtime.get("queued_action")
            ):
                self.sessions._launch_later(card, step_id)
            elif state["status"] != "running" and state.get("run"):
                self.sessions._close_later(card, step_id)

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
            if self.sessions._quiescing or self.store is None:
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

    async def _guard(self, context: ToolContext) -> tuple[Json, str]:
        owner = context.execution_owner
        if owner is None or owner.extension != "factory" or context.request_turn_end_hook is None:
            raise self._error("participant_required")
        card, step_id = await self.sessions._lookup(owner.group_id)
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
        acknowledged = await self.sessions._acknowledge_pending(card, target)
        if acknowledged is not None:
            card = acknowledged
            runtime = self._runtime(card)
        state = runtime["steps"][target]
        step = _workflows.step(runtime["workflow"], target)
        reason = state.get("reason")
        if await self.budgets._check_budget(card, target, check_runs=True):
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
            self.sessions._close_later(saved, step_id, cancel=True)
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
        self.sessions._delete_later(saved)
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
