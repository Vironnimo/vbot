"""Persisted Factory handoffs over the host's canonical owned execution groups.

A Card runs its Workflow snapshot one step at a time in the Project directory, so
participants never share a working tree concurrently. A step keeps its Session
across reruns; a step that can send work back judges with a fresh participant on
every run.
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

from . import _workflows, agent_text
from .store import ACTIVE_STATES, FINAL_STATES, FactoryError

type Json = dict[str, Any]
_LOGGER = get_logger("extensions.factory")
_PRIVATE = frozenset(agent_text.PRIVATE_TOOLS)
_STEP_TOOLS = frozenset({"factory_submit_work", "factory_report_blocked"})
_RESULT_LIMIT = 100000


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
        self._closing_groups: set[str] = set()
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
    def _budget(runtime: Json) -> int:
        return int(runtime.get("settings", {}).get("turn_budget", 30))

    async def _change(
        self, card: Json, patch: Json, *, actor: str = "factory", event: str, **extra: Any
    ) -> Json:
        saved = await self.store.change(card["id"], patch, actor=actor, event=event, **extra)
        await self.service.changed(saved, card, actor=actor)
        return cast(Json, saved)

    # Workflow positions

    @staticmethod
    def _step(runtime: Json, participant: str | None = None) -> Json:
        """The step of a participant, or the Card's current step."""
        workflow = runtime["workflow"]
        step_id = _workflows.step_id(participant) if participant else runtime["step"]
        return _workflows.step(workflow, step_id)

    @staticmethod
    def _participant(runtime: Json, step: Json) -> str:
        """The step's latest participant; a judging step gets a fresh one per run."""
        return _workflows.participant(step, int(runtime.get("sequences", {}).get(step["id"], 1)))

    def _config(self, card: Json, participant: str) -> TemporaryAgentConfig:
        runtime = card["runtime"]
        step = self._step(runtime, participant)
        tools = _STEP_TOOLS | ({"factory_request_changes"} if step["returns_to"] else set())
        return TemporaryAgentConfig(
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

    @staticmethod
    def _latest(outputs: list[Json], workflow: Json, step_ids: set[str]) -> list[Json]:
        """The newest hand-in of each given step, in Workflow order."""
        newest = {
            output["step"]: output
            for output in outputs
            if output["kind"] == "work" and output["step"] in step_ids
        }
        return [newest[step["id"]] for step in workflow["steps"] if step["id"] in newest]

    async def _history(self, card: Json, recording: Json | None) -> Json:
        """The Card's history including the output this transition records with it."""
        history: Json = await self.store.history(card["id"])
        if recording is not None:
            history["outputs"].append(recording)
        return history

    async def _step_input(
        self, card: Json, runtime: Json, step: Json, recording: Json | None, note: Json
    ) -> str:
        workflow = runtime["workflow"]
        history = await self._history(card, recording)
        earlier = self._latest(
            history["outputs"], workflow, _workflows.ancestors(workflow, step["id"])
        )
        findings = [
            item
            for item in history["outputs"]
            if item["kind"] == "changes" and item["step"] == step["id"]
        ]
        return agent_text.step_input(
            {**card, "runtime": runtime},
            step,
            comments=history["comments"],
            earlier=earlier,
            findings=findings,
            note=note,
        )

    async def _queue(
        self,
        card: Json,
        *,
        participant: str,
        content: str,
        initial: bool = False,
        cancel: bool = False,
        patch: Json | None = None,
        event: str,
        actor: str = "factory",
        runtime: Json | None = None,
        pending: Json | None = None,
        **extra: Any,
    ) -> Json:
        runtime = runtime if runtime is not None else self._runtime(card)
        if (
            pending is None
            and participant not in runtime.get("participants", {})
            and not initial
            and runtime.get("workflow", {}).get("steps")
        ):
            # A participant that never started (its launch failed, or its Card was
            # carried over from an older Factory) begins with the full step input.
            initial = True
            step = self._step(runtime, participant)
            content = (
                await self._step_input(
                    card, runtime, step, extra.get("output"), {"kind": "", "text": ""}
                )
                + "\n\n"
                + content
            )
        runtime.pop("pending_action", None)
        runtime.pop("question", None)
        runtime.pop("note", None)
        runtime["pending"] = (
            pending
            if pending is not None
            else {
                "id": new_id("fci"),
                "participant": participant,
                "kind": "initial" if initial else "continuation",
                "content": content,
                "cancel_previous": cancel,
            }
        )
        saved = await self._change(
            card,
            {
                **(patch or {}),
                "state": "working",
                "waiting_reason": "",
                "runtime": runtime,
            },
            actor=actor,
            event=event,
            **extra,
        )
        self._launch_later(saved)
        return saved

    def _launch_later(self, card: Json) -> None:
        pending = card["runtime"].get("pending")
        if not pending or self._quiescing or pending["id"] in self._scheduled:
            return
        token = pending["id"]
        self._scheduled.add(token)
        self.service.spawn(self._launch(card["id"], token))

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

    async def _launch(self, card_id: str, token: str) -> None:
        try:
            async with self.service.lock:
                card = await self.store.get_card(card_id)
                pending = card["runtime"].get("pending", {})
                if (
                    pending.get("id") != token
                    or self._quiescing
                    or card["state"] not in ACTIVE_STATES
                ):
                    return
                pending = dict(pending)
                group_id = card["group_id"]
                old_run = card["runtime"].get("current_run")
            if not pending.get("cancel_previous"):
                await self._wait_run(group_id, old_run)
            async with self._group_locks.setdefault(card_id, asyncio.Lock()):
                # Only a new participant needs a closed group. Same-participant
                # continuations retain their delegated work and native resources.
                if pending["kind"] == "initial" or pending.get("cancel_previous"):
                    await self.groups.close_group(group_id)
                async with self.service.lock:
                    card = await self.store.get_card(card_id)
                    runtime = self._runtime(card)
                    if (
                        runtime.get("pending", {}).get("id") != token
                        or self._quiescing
                        or card["state"] not in ACTIVE_STATES
                    ):
                        return
                    if await self._acknowledge_pending(card) is not None:
                        return
                    participant = pending["participant"]
                    participants = runtime.setdefault("participants", {})
                    if participant not in participants:
                        binding = await self.groups.create(
                            group_id,
                            participant,
                            self._config(card, participant),
                            project_id=card["project_id"],
                        )
                        participants[participant] = {
                            "agent_id": binding.address.agent_id,
                            "session_id": binding.address.session_id,
                            "project_id": binding.address.project_id,
                            "generation_id": binding.generation_id,
                        }
                    handle = await self.groups.open_group(group_id)
                    # Consume this Retry before start: a crash must not authorize
                    # another attempt for a newly admitted Run.
                    resume_terminal = runtime["pending"].pop("retry_terminal", False) is True
                    if resume_terminal:
                        card = await self._change(
                            card, {"runtime": runtime}, event="retry_consumed"
                        )
                    admission = await self.groups.start(
                        handle,
                        participant,
                        TemporaryRunInput(pending["kind"], pending["content"], token),
                    )
                    runtime.update(
                        current_run=admission.run_id,
                        current_participant=participant,
                        epoch=handle.epoch,
                    )
                    runtime.setdefault("runs", {})[participant] = admission.run_id
                    if (
                        resume_terminal
                        and admission.existing
                        and admission.status in {"failed", "cancelled", "interrupted"}
                    ):
                        await self._queue(
                            card,
                            participant=participant,
                            content=agent_text.note(
                                "keep_working", card, step=self._step(runtime, participant)
                            ),
                            runtime=runtime,
                            patch={"turns": 0},
                            event="retry_reconciled",
                            detail={"run_id": admission.run_id},
                        )
                        return
                    runtime.pop("pending", None)
                    title = pending["kind"] == "initial" and not runtime.get("titled")
                    if title:
                        runtime["titled"] = True
                    saved = await self._change(
                        card,
                        {"runtime": runtime, "turns": int(card.get("turns", 0)) + 1},
                        event="run_admitted",
                        detail={"participant": participant, "run_id": admission.run_id},
                    )
                    if title:
                        self.service.spawn(self.groups.title_group(group_id, card["title"]))
                    if admission.existing and admission.status != "running":
                        if admission.status == "interrupted":
                            await self._recover(saved)
                        else:
                            await self._completed(
                                saved, "success" if admission.status == "completed" else "error"
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
                _LOGGER.exception("Factory participant admission failed (card=%s)", card_id)
            async with self.service.lock:
                try:
                    card = await self.store.get_card(card_id)
                except FactoryError as missing:
                    if missing.code == "card_not_found":
                        return
                    raise
                if card["runtime"].get("pending", {}).get("id") == token and not self._quiescing:
                    await self._wait(card, "run_failed", "launch_failed")
        finally:
            self._scheduled.discard(token)

    async def _guard(self, context: ToolContext, *, states: set[str]) -> Json:
        owner = context.execution_owner
        if owner is None or owner.extension != "factory" or context.request_turn_end_hook is None:
            raise self._error("participant_required")
        card = await self.store.get_card(owner.group_id)
        runtime = card["runtime"]
        if (
            runtime.get("current_run") != context.run_id
            or runtime.get("epoch") != owner.epoch
            or runtime.get("current_participant") != owner.participant_id
            or runtime.get("pending")
            or not runtime.get("workflow", {}).get("steps")
        ):
            raise self._error("stale_run")
        if card["state"] not in states:
            raise self._error("wrong_state")
        return cast(Json, card)

    async def start(self, card: Json) -> Json:
        if card["state"] != "queued" or card["runtime"].get("pending_action"):
            raise self._error("wrong_state")
        setup = await self.store.get_setup(card["project_id"])
        try:
            workflow = _workflows.choose(setup, card.get("workflow_id") or None)
        except FactoryError:
            # The chosen Workflow was removed from the setup while the Card waited.
            workflow = _workflows.choose(setup, None)
        runtime = self._runtime(card)
        runtime.pop("setup", None)
        runtime.update(
            workflow=workflow,
            settings={"turn_budget": setup["turn_budget"]},
            done=[],
            approved=[],
            notes={},
            passes={},
            rounds={},
            restarts=0,
        )
        return await self._continue(
            card,
            runtime,
            event="started",
            actor="factory",
            patch={"workflow_id": workflow["id"]},
            detail={"workflow": workflow["name"]},
        )

    async def _continue(
        self,
        card: Json,
        runtime: Json,
        *,
        event: str,
        actor: str,
        patch: Json | None = None,
        **extra: Any,
    ) -> Json:
        """Start the next step that is not done, or wait for the user."""
        step = _workflows.next_step(runtime["workflow"], runtime.get("done", []))
        if step is None:
            return await self._wait(
                card, "finished", runtime=runtime, actor=actor, event=event, patch=patch, **extra
            )
        runtime["step"] = step["id"]
        if step["approval"] and step["id"] not in runtime.get("approved", []):
            return await self._wait(
                card, "checkpoint", runtime=runtime, actor=actor, event=event, patch=patch, **extra
            )
        passes = runtime.setdefault("passes", {})
        passes[step["id"]] = int(passes.get(step["id"], 0)) + 1
        return await self._run_step(
            card, runtime, step, event=event, actor=actor, patch=patch, **extra
        )

    async def _run_step(
        self,
        card: Json,
        runtime: Json,
        step: Json,
        *,
        event: str,
        actor: str = "factory",
        patch: Json | None = None,
        **extra: Any,
    ) -> Json:
        """Queue a run of the step: its Session continues, or a fresh one starts."""
        runtime["step"] = step["id"]
        if step["returns_to"]:
            sequences = runtime.setdefault("sequences", {})
            sequences[step["id"]] = int(sequences.get(step["id"], 0)) + 1
        participant = self._participant(runtime, step)
        note = runtime.setdefault("notes", {}).pop(step["id"], None) or {
            "kind": "refresh",
            "text": "",
        }
        updated = {**card, **(patch or {}), "runtime": runtime}
        # The output handed in by this transition is saved together with the
        # queued input, so the next participant's input must include it already.
        recording = extra.get("output")
        if participant in runtime.get("participants", {}):
            earlier = []
            if note["kind"] == "refresh":
                workflow = runtime["workflow"]
                earlier = self._latest(
                    (await self._history(card, recording))["outputs"],
                    workflow,
                    _workflows.ancestors(workflow, step["id"]),
                )
            content = agent_text.note(
                note["kind"],
                updated,
                note["text"],
                earlier,
                step=step,
                source=note.get("source", ""),
            )
            initial = False
        else:
            content = await self._step_input(updated, runtime, step, recording, note)
            initial = True
        extra["detail"] = {**extra.get("detail", {}), "step": step["id"]}
        return await self._queue(
            card,
            participant=participant,
            initial=initial,
            content=content,
            runtime=runtime,
            patch={**(patch or {}), "turns": 0},
            event=event,
            actor=actor,
            **extra,
        )

    @staticmethod
    def _reset(runtime: Json, target: str, note: Json) -> None:
        """Make the target step and every step after it run again, the target first."""
        workflow = runtime["workflow"]
        later = _workflows.dependents(workflow, target)
        runtime["done"] = [item for item in runtime.get("done", []) if item not in later | {target}]
        # Steps after the target ask for approval again; the target keeps its own.
        runtime["approved"] = [item for item in runtime.get("approved", []) if item not in later]
        runtime.setdefault("notes", {})[target] = note
        for item in later:
            runtime["notes"].pop(item, None)

    def _output(self, runtime: Json, kind: str) -> Json:
        participant = runtime["current_participant"]
        step = self._step(runtime, participant)
        return {
            "step": step["id"],
            "step_name": step["name"],
            "kind": kind,
            "pass": int(runtime.get("passes", {}).get(step["id"], 1)),
            "participant_id": participant,
        }

    async def submit_work(self, context: ToolContext, arguments: Json) -> Json:
        card = await self._guard(context, states={"working"})
        runtime = self._runtime(card)
        step = self._step(runtime, runtime["current_participant"])
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
        output = self._output(runtime, "work") | {"summary": summary, "result": result or ""}
        runtime.update(restarts=0)
        runtime.setdefault("done", []).append(step["id"])
        saved = await self._continue(
            card,
            runtime,
            event="submitted",
            actor="agent",
            patch={"summary": summary},
            output=output,
            detail={"handed_in": step["id"]},
        )
        context.request_turn_end()
        return agent_text.tool_result("submitted", saved)

    async def report_blocked(self, context: ToolContext, arguments: Json) -> Json:
        card = await self._guard(context, states={"working"})
        question = self._text(arguments, "question")
        runtime = self._runtime(card)
        runtime.update(question=question, restarts=0)
        saved = await self._wait(card, "blocked", runtime=runtime, actor="agent")
        context.request_turn_end()
        return agent_text.tool_result("blocked", saved)

    async def request_changes(self, context: ToolContext, arguments: Json) -> Json:
        card = await self._guard(context, states={"working"})
        runtime = self._runtime(card)
        step = self._step(runtime, runtime["current_participant"])
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
        runtime.update(restarts=0)
        target = step["returns_to"]
        output = self._output(runtime, "changes") | {"findings": findings, "target": target}
        detail = {"step": step["id"], "target": target}
        rounds = runtime.setdefault("rounds", {})
        if int(rounds.get(step["id"], 0)) >= int(step["rounds"]):
            saved = await self._wait(
                card,
                "no_agreement",
                runtime=runtime,
                actor="agent",
                event="changes_requested",
                output=output,
                detail=detail,
            )
        else:
            rounds[step["id"]] = int(rounds.get(step["id"], 0)) + 1
            self._reset(
                runtime,
                target,
                {"kind": "rework", "text": "\n".join(findings), "source": step["name"]},
            )
            saved = await self._continue(
                card,
                runtime,
                event="changes_requested",
                actor="agent",
                output=output,
                detail=detail,
            )
        context.request_turn_end()
        return agent_text.tool_result("changes_requested", saved)

    async def _done(self, card: Json, runtime: Json) -> Json:
        runtime.pop("pending_action", None)
        saved = await self._change(
            card,
            {"state": "done", "waiting_reason": "", "runtime": runtime},
            actor="user",
            event="accepted",
        )
        self._close_later(saved)
        return saved

    def _action(self, card: Json, action: str) -> Json:
        runtime = self._runtime(card)
        pending = runtime.get("pending_action", {})
        if card["state"] != "waiting" and not (
            card["state"] == "queued" and pending.get("action") == action
        ):
            raise self._error("wrong_state")
        if runtime.get("deleting"):
            raise self._error("wrong_state")
        return runtime

    async def accept(self, card: Json) -> Json:
        runtime = self._action(card, "accept")
        reason = runtime.get("pending_action", {}).get("waiting_reason") or card["waiting_reason"]
        if reason not in {"checkpoint", "finished", "no_agreement"}:
            raise self._error("wrong_state")
        runtime["restarts"] = 0
        if reason == "checkpoint":
            runtime.setdefault("approved", []).append(runtime["step"])
        elif reason == "no_agreement":
            # The user overrules the step that kept sending work back.
            runtime.setdefault("done", []).append(runtime["step"])
        runtime["rounds"] = {}
        if _workflows.next_step(runtime["workflow"], runtime.get("done", [])) is None:
            return await self._done(card, runtime)
        return await self._continue(card, runtime, event="accepted", actor="user")

    @staticmethod
    def _default_target(runtime: Json) -> str:
        """The step a send-back without a chosen step reworks."""
        workflow = runtime["workflow"]
        done = [step for step in workflow["steps"] if step["id"] in runtime.get("done", [])]
        current = _workflows.step(workflow, runtime["step"]) if runtime.get("step") else None
        if current is None or current["id"] not in runtime.get("done", []):
            current = done[-1] if done else workflow["steps"][0]
        return str(current["returns_to"] or current["id"])

    async def send_back(self, card: Json, comment: str, step: str = "") -> Json:
        runtime = self._action(card, "send_back")
        action = runtime.get("pending_action", {})
        recorded = action.get("comment_recorded", False)
        reason = action.get("waiting_reason") or card["waiting_reason"]
        runtime["restarts"] = 0
        if not runtime.get("workflow", {}).get("steps"):
            return await self._requeue(
                card, runtime, event="sent_back", comment=None if recorded else comment
            )
        participant = runtime.get("current_participant")
        if reason == "blocked" and not step and participant:
            return await self._queue(
                card,
                participant=participant,
                content=agent_text.note("answer", card, comment),
                runtime=runtime,
                patch={"turns": 0},
                event="answered",
                actor="user",
                comment=None if recorded else comment,
            )
        runtime["rounds"] = {}
        target = step or self._default_target(runtime)
        self._reset(runtime, target, {"kind": "send_back", "text": comment})
        return await self._continue(
            card,
            runtime,
            event="sent_back",
            actor="user",
            comment=None if recorded else comment,
            detail={"target": target},
        )

    async def _requeue(
        self, card: Json, runtime: Json, *, event: str, comment: str | None = None
    ) -> Json:
        """Queue a Card that never chose its Workflow, so the scheduler starts it again."""
        runtime.pop("pending_action", None)
        runtime.pop("note", None)
        return await self._change(
            card,
            {"state": "queued", "waiting_reason": "", "turns": 0, "runtime": runtime},
            actor="user",
            event=event,
            comment=comment,
        )

    async def retry(self, card: Json) -> Json:
        runtime = self._action(card, "retry")
        acknowledged = await self._acknowledge_pending(card)
        if acknowledged is not None:
            card = acknowledged
            runtime = self._action(card, "retry")
        runtime["restarts"] = 0
        if not runtime.get("workflow", {}).get("steps"):
            return await self._requeue(card, runtime, event="retried")
        pending = runtime.get("pending")
        if pending:
            pending["retry_terminal"] = True
            return await self._queue(
                card,
                participant=pending["participant"],
                content=pending["content"],
                initial=pending["kind"] == "initial",
                runtime=runtime,
                pending=pending,
                patch={"turns": 0},
                event="retried",
                actor="user",
            )
        step = self._step(runtime)
        if step["returns_to"]:
            # A step that judges earlier work starts over with a fresh participant.
            return await self._run_step(card, runtime, step, event="retried", actor="user")
        return await self._queue(
            card,
            participant=self._participant(runtime, step),
            content=agent_text.note("keep_working", card, step=step),
            runtime=runtime,
            patch={"turns": 0},
            event="retried",
            actor="user",
        )

    async def discard(self, card: Json) -> Json:
        if card["state"] in FINAL_STATES:
            raise self._error("wrong_state")
        runtime = self._runtime(card)
        runtime.pop("pending", None)
        runtime.pop("pending_action", None)
        saved = await self._change(
            card,
            {"state": "discarded", "waiting_reason": "", "runtime": runtime},
            actor="user",
            event="discarded",
        )
        self._close_later(saved, cancel=True)
        return saved

    async def delete(self, card: Json) -> Json:
        if card["state"] not in FINAL_STATES:
            raise self._error("wrong_state")
        runtime = self._runtime(card)
        runtime.update(deleting=True)
        runtime.pop("pending", None)
        saved = await self._change(
            card, {"runtime": runtime}, actor="user", event="deletion_requested"
        )
        self._close_later(saved, delete=True)
        return {"id": card["id"], "pending": True}

    async def _wait(
        self,
        card: Json,
        reason: str,
        message: str | None = None,
        *,
        runtime: Json | None = None,
        actor: str = "factory",
        event: str | None = None,
        patch: Json | None = None,
        **extra: Any,
    ) -> Json:
        runtime = runtime if runtime is not None else self._runtime(card)
        if message is not None:
            runtime["note"] = agent_text.error_message(message)
        runtime.pop("pending_action", None)
        saved = await self._change(
            card,
            {**(patch or {}), "state": "waiting", "waiting_reason": reason, "runtime": runtime},
            actor=actor,
            event=event or reason,
            **extra,
        )
        self._close_later(saved)
        return saved

    def _close_later(self, card: Json, *, delete: bool = False, cancel: bool = False) -> None:
        """Close the Card's group once its current Run ended, or at once to cancel it."""
        if self._quiescing:
            return
        key = card["id"] + (
            ":delete" if delete else f":close:{card['runtime'].get('current_run', '')}"
        )
        if key in self._closing_groups:
            return
        self._closing_groups.add(key)
        self.service.spawn(self._close(card["id"], key, delete=delete, cancel=cancel))

    async def _close(self, card_id: str, key: str, *, delete: bool, cancel: bool = False) -> None:
        try:
            async with self.service.lock:
                card = await self.store.get_card(card_id)
                group_id = card["group_id"]
                current_run = card["runtime"].get("current_run")
            if not delete and not cancel:
                await self._wait_run(group_id, current_run)
            async with self._group_locks.setdefault(card_id, asyncio.Lock()):
                async with self.service.lock:
                    current = await self.store.get_card(card_id)
                    if not delete and (
                        current["runtime"].get("pending")
                        and current["state"] != "waiting"
                        or current["state"] in ACTIVE_STATES
                        or current["runtime"].get("current_run") != current_run
                    ):
                        return
                await self.groups.close_group(group_id)
                if delete:
                    await self.groups.archive_group(group_id)
                    async with self.service.lock:
                        await self.store.delete_card(card_id)
                        _LOGGER.info("Factory Card deleted (card=%s)", card_id)
                        host = self.service.host
                        if host.publish_change is not None:
                            host.publish_change("cards", [card_id], card["revision"] + 1)
                elif self.service.host.publish_change is not None:
                    self.service.host.publish_change("cards", [card_id], current["revision"])
        except Exception as error:
            if isinstance(error, FactoryError) and error.code == "card_not_found":
                return
            if isinstance(error, (FactoryError, OSError, ValueError)):
                _LOGGER.warning(
                    "Factory group cleanup failed (card=%s error=%s)",
                    card_id,
                    type(error).__name__,
                )
            else:
                _LOGGER.exception("Factory group cleanup failed (card=%s)", card_id)
            if not self._quiescing and self.store is not None:
                async with self.service.lock:
                    card = await self.store.get_card(card_id)
                    runtime = self._runtime(card)
                    runtime["note"] = agent_text.error_message("cleanup_failed")
                    await self._change(card, {"runtime": runtime}, event="cleanup_failed")
        finally:
            self._closing_groups.discard(key)

    @staticmethod
    def _prepared_delivery(pending: Json) -> PreparedSessionDelivery:
        content = pending["content"]
        return PreparedSessionDelivery(
            pending["id"],
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
            (content,),
            pending["delivery"]["settings_revision"],
            "factory_continuation",
        )

    async def _acknowledge_pending(self, card: Json) -> Json | None:
        pending = card["runtime"].get("pending", {})
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
        runtime.pop("pending")
        return await self._change(card, {"runtime": runtime}, event="delivery_acknowledged")

    async def acknowledge_delivery(
        self, context: SessionRequestContext, delivery: PreparedSessionDelivery
    ) -> None:
        async with self.service.lock:
            card = await self.store.get_card(context.binding.group_id)
            pending = card["runtime"].get("pending", {})
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
            if await self._acknowledge_pending(card) is None:
                raise FactoryError("delivery_unacknowledged", "Factory continuation has no receipt")

    async def before_request(
        self, context: SessionRequestContext
    ) -> PreparedSessionDelivery | None:
        owner = context.execution_owner
        binding = context.binding
        if self._quiescing or owner is None:
            return None
        async with self.service.lock:
            card = await self.store.get_card(binding.group_id)
            runtime = self._runtime(card)
            pending = runtime.get("pending", {})
            if (
                owner.epoch != runtime.get("epoch")
                or owner.participant_id != runtime.get("current_participant")
                or pending
                and pending.get("participant") != owner.participant_id
            ):
                raise self._error("stale_run")
            if card["state"] not in ACTIVE_STATES:
                raise self._error("wrong_state")
            if (context.agent_id, context.session_id) != (
                binding.address.agent_id,
                binding.address.session_id,
            ):
                return None
            if pending and pending.get("kind") != "continuation":
                raise self._error("stale_run")
            if runtime.get("current_run") == context.run_id:
                return self._prepared_delivery(pending) if pending.get("delivery") else None
            if card["turns"] >= self._budget(runtime):
                await self._wait(card, "budget_exhausted", "budget_exhausted")
                raise self._error("budget_exhausted")
            runtime["current_run"] = context.run_id
            runtime.setdefault("runs", {})[owner.participant_id] = context.run_id
            if pending and not pending.get("delivery"):
                pending["delivery"] = {
                    "address": {
                        "project_id": binding.address.project_id,
                        "agent_id": binding.address.agent_id,
                        "session_id": binding.address.session_id,
                    },
                    "generation_id": binding.generation_id,
                    "settings_revision": str(card["revision"]),
                }
            await self._change(
                card,
                {"runtime": runtime, "turns": card["turns"] + 1},
                event="run_observed",
                detail={"run_id": context.run_id},
            )
            if pending:
                return self._prepared_delivery(pending)
        return None

    async def _pause_failed_handoff(
        self, card: Json, context: SessionRequestContext, outcome: str
    ) -> None:
        runtime = self._runtime(card)
        pending = runtime.get("pending", {})
        owner, binding = context.execution_owner, context.binding
        participant = runtime.get("current_participant")
        address = runtime.get("participants", {}).get(participant, {})
        if (
            outcome == "success"
            or card["state"] not in ACTIVE_STATES
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
            != (
                "factory",
                card["group_id"],
                participant,
                binding.generation_id,
                runtime.get("epoch"),
            )
            or binding.group_id != card["group_id"]
            or binding.participant_id != participant
            or (context.agent_id, context.session_id)
            != (binding.address.agent_id, binding.address.session_id)
            or (address.get("project_id"), address.get("agent_id"), address.get("session_id"))
            != (binding.address.project_id, binding.address.agent_id, binding.address.session_id)
            or address.get("generation_id", binding.generation_id) != binding.generation_id
        ):
            return
        records = await self.groups.owned_runs(
            card["group_id"], [context.run_id, runtime["current_run"]]
        )
        inspected, previous = records.get(context.run_id), records.get(runtime["current_run"])
        if (
            inspected is None
            or previous is None
            or (previous.record.address, previous.record.generation_id)
            != (binding.address, binding.generation_id)
            or (
                context.run_id != runtime["current_run"]
                and inspected.record.start_sequence <= previous.record.start_sequence
            )
            or (inspected.record.owner, inspected.record.address, inspected.record.generation_id)
            != (owner, binding.address, binding.generation_id)
        ):
            return
        card = await self._acknowledge_pending(card) or card
        runtime = self._runtime(card)
        runtime.update(current_run=context.run_id, note=agent_text.error_message("run_failed"))
        runtime.setdefault("runs", {})[participant] = context.run_id
        saved = await self._change(
            card,
            {"state": "waiting", "waiting_reason": "run_failed", "runtime": runtime},
            event="run_failed",
            detail={"run_id": context.run_id},
        )
        self._close_later(saved)

    async def run_finished(self, context: SessionRequestContext, *, outcome: str) -> None:
        if self._quiescing or context.execution_owner is None:
            return
        async with self.service.lock:
            if self._quiescing or self.store is None:
                return
            try:
                card = await self.store.get_card(context.binding.group_id)
            except FactoryError as error:
                if error.code == "card_not_found":
                    return
                raise
            runtime = card["runtime"]
            if (
                context.run_id != runtime.get("current_run")
                or context.execution_owner.epoch != runtime.get("epoch")
                or (outcome != "success" and runtime.get("pending"))
            ):
                await self._pause_failed_handoff(card, context, outcome)
                return
            card = await self._acknowledge_pending(card) or card
            runtime = card["runtime"]
            if card["state"] == "waiting":
                self._close_later(card)
            elif runtime.get("pending"):
                self._launch_later(card)
            else:
                await self._completed(card, outcome)

    async def _completed(self, card: Json, outcome: str) -> None:
        runtime = self._runtime(card)
        if card["state"] not in ACTIVE_STATES:
            self._close_later(card, delete=runtime.get("deleting", False))
            return
        if outcome != "success":
            await self._wait(card, "run_failed", "run_failed")
        elif int(card["turns"]) >= self._budget(runtime):
            await self._wait(card, "budget_exhausted", "budget_exhausted")
        else:
            participant = runtime["current_participant"]
            await self._queue(
                card,
                participant=participant,
                content=agent_text.note(
                    "keep_working", card, step=self._step(runtime, participant)
                ),
                runtime=runtime,
                event="nudged",
            )

    async def _recover(self, card: Json) -> None:
        runtime = self._runtime(card)
        runtime["restarts"] = int(runtime.get("restarts", 0)) + 1
        if runtime["restarts"] >= 2:
            await self._wait(card, "restart", "restart", runtime=runtime)
            return
        if card["state"] not in ACTIVE_STATES:
            return
        participant = runtime.get("current_participant")
        if not participant:
            await self._run_step(card, runtime, self._step(runtime), event="restarted")
            return
        await self._queue(
            card,
            participant=participant,
            content=agent_text.note("restart", card, step=self._step(runtime, participant)),
            runtime=runtime,
            event="restarted",
        )

    async def _upgrade(self, card: Json) -> None:
        """Carry a Card that started under an older Factory over to steps.

        A Card that handed nothing in starts over. Any other waits for the user with
        its earlier steps done. An older Factory integrated a handed-in branch after
        acceptance; such a Card waits for approval of its Workflow's last step,
        which now integrates.
        """
        old = card["runtime"]
        runtime: Json = {"cwd": old.get("cwd", ""), "participants": old.get("participants", {})}
        if not card["round"] and card["state"] != "waiting":
            await self._change(
                card,
                {"state": "queued", "waiting_reason": "", "turns": 0, "runtime": runtime},
                event="workflow_upgrade",
            )
            return
        patch: Json = {"state": "waiting", "waiting_reason": "finished", "turns": 0}
        output: Json | None = None
        workflow: Json | None = None
        budget = old.get("settings", {}).get("turn_budget")
        if isinstance(old.get("workflow"), dict) and old["workflow"].get("stages"):
            workflow = _workflows.from_stages(old["workflow"])
        else:
            try:
                setup = await self.store.get_setup(card["project_id"])
            except FactoryError:
                setup = None
            if setup is not None:
                workflow = _workflows.choose(setup, None)
                budget = setup["turn_budget"]
        if workflow is not None and workflow["steps"]:
            steps = [step["id"] for step in workflow["steps"]]
            done = steps[:-1] if card["branch"] else steps
            runtime.update(
                workflow=workflow,
                settings={"turn_budget": int(budget or 30)},
                done=done,
                approved=list(done),
                notes={},
                passes={},
                rounds={},
                restarts=0,
                step=steps[-1],
            )
            if card["branch"]:
                patch["waiting_reason"] = "checkpoint"
                # The earlier Factory kept the handed-in branch on the Card; the
                # Integrate step reads it as the hand-in of the step that made it.
                makers = [step for step in workflow["steps"][:-1] if not step["returns_to"]]
                maker = makers[-1] if makers else workflow["steps"][-1]
                output = {
                    "step": maker["id"],
                    "step_name": maker["name"],
                    "kind": "work",
                    "pass": 1,
                    "participant_id": "",
                    "summary": card["summary"] or "Handed in under an earlier Factory version.",
                    "result": (
                        f"Branch {card['branch']} based on {card['base']}, "
                        f"head commit {card['head_sha']}."
                    ),
                }
            patch["workflow_id"] = workflow["id"]
        else:
            patch["waiting_reason"] = "run_failed"
        runtime["note"] = agent_text.error_message("workflow_upgrade")
        await self._change(
            card, {**patch, "runtime": runtime}, event="workflow_upgrade", output=output
        )

    async def reconcile(self) -> None:
        if self._reconciled:
            return
        self._reconciled = True
        self._quiescing = False
        async with self.service.lock:
            cards = await self.store.list_cards()
            known = {card["group_id"] for card in cards}
            for card in cards:
                card = await self._acknowledge_pending(card) or card
                runtime = card["runtime"]
                if runtime.get("deleting"):
                    self._close_later(card, delete=True)
                elif card["state"] in FINAL_STATES:
                    if runtime.get("pending"):
                        # A discarded Card no longer runs what an older Factory queued for it.
                        runtime = self._runtime(card)
                        runtime.pop("pending")
                        card = await self._change(
                            card, {"runtime": runtime}, event="pending_dropped"
                        )
                    self._close_later(card, cancel=True)
                elif not runtime.get("workflow", {}).get("steps") and (
                    runtime.get("setup")
                    or runtime.get("workflow")
                    or card["state"] in ACTIVE_STATES
                ):
                    await self._upgrade(card)
                elif card["state"] in {"proposal", "queued"}:
                    continue
                elif card["state"] in ACTIVE_STATES:
                    run_id = runtime.get("current_run")
                    inspected = (
                        (await self.groups.owned_runs(card["group_id"], [run_id])).get(run_id)
                        if run_id
                        else None
                    )
                    if (
                        inspected is not None
                        and inspected.run is not None
                        and inspected.run.status.value == "running"
                    ):
                        continue
                    if runtime.get("pending"):
                        self._launch_later(card)
                    elif run_id:
                        if (
                            inspected is not None
                            and inspected.record.terminal_status == "completed"
                        ):
                            await self._completed(card, "success")
                        else:
                            await self._recover(card)
                    elif runtime.get("step"):
                        await self._run_step(
                            card,
                            self._runtime(card),
                            self._step(runtime),
                            event="recovered_initial",
                        )
                    else:
                        await self._continue(
                            card, self._runtime(card), event="recovered_initial", actor="factory"
                        )
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
