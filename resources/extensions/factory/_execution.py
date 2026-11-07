"""Persisted Factory handoffs over the host's canonical owned execution groups.

A Card runs its Workflow snapshot one participant at a time: the lanes of a stage
run one after another in the Project directory, so they never share a working
tree concurrently. A work lane keeps its Session across rework; every review lane
is a fresh participant.
"""

from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

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
from ._git import GitRepository
from ._workflows import is_review
from .store import ACTIVE_STATES, FINAL_STATES, FactoryError

type Json = dict[str, Any]
_LOGGER = get_logger("extensions.factory")
_PRIVATE = frozenset(agent_text.PRIVATE_TOOLS)
_WORK_TOOLS = frozenset({"factory_submit_work", "factory_report_blocked"})
_INTEGRATOR_TOOLS = _WORK_TOOLS | {"factory_report_integration"}
_REVIEW_TOOLS = frozenset({"factory_submit_review"})
_EDIT_TOOLS = frozenset({"apply_patch", "edit", "write"})
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
    def _locate(workflow: Json, participant: str) -> tuple[int, int]:
        """The stage index and lane of a participant id."""
        stage_id, lane, *_ = participant.split("-")
        return _workflows.stage_index(workflow, stage_id), int(lane) - 1

    def _integrator(self, runtime: Json) -> str | None:
        workflow = runtime["workflow"]
        index = _workflows.integrator(workflow)
        if index is None:
            return None
        return _workflows.work_participant(workflow["stages"][index], 0)

    def _config(self, card: Json, participant: str) -> TemporaryAgentConfig:
        runtime = card["runtime"]
        workflow = runtime["workflow"]
        index, lane = self._locate(workflow, participant)
        stage = workflow["stages"][index]
        if stage["kind"] == "review":
            tools = _REVIEW_TOOLS
        elif index == _workflows.integrator(workflow):
            tools = _INTEGRATOR_TOOLS
        else:
            tools = _WORK_TOOLS
        denied = set(_PRIVATE) - set(tools)
        if stage["kind"] == "review" or stage["output"] == "document":
            denied.update(_EDIT_TOOLS)
        value = stage["lanes"][lane]
        name = stage["name"] + (f" {lane + 1}" if len(stage["lanes"]) > 1 else "")
        return TemporaryAgentConfig(
            model="",
            **_workflows.profile(value),
            model_override=value["model"] or None,
            thinking_effort_override=value["effort"] or None,
            cwd=Path(runtime["cwd"]),
            tool_access=ToolAccess(mode="all", denied=tuple(sorted(denied))),
            allowed_skills=["*"],
            tools={},
            name=f"Factory {name}",
            instructions=agent_text.rules(stage),
        )

    @staticmethod
    def _latest(outputs: list[Json], workflow: Json, stage_ids: set[str]) -> list[Json]:
        """The newest work result and review verdict of each lane of the given stages."""
        newest: dict[tuple[str, str, int], Json] = {}
        for output in outputs:
            if output["stage_id"] in stage_ids:
                kind = "work" if output["kind"] == "work" else "review"
                newest[(output["stage_id"], kind, output["lane"])] = output
        order = {stage["id"]: index for index, stage in enumerate(workflow["stages"])}
        lanes = {stage["id"]: len(stage["lanes"]) for stage in workflow["stages"]}
        return [
            {**output, "lanes": lanes.get(output["stage_id"], 1)}
            for _key, output in sorted(
                newest.items(), key=lambda item: (order[item[0][0]], item[0][1], item[0][2])
            )
        ]

    async def _earlier(self, card: Json, runtime: Json, index: int) -> list[Json]:
        workflow = runtime["workflow"]
        history = await self.store.history(card["id"])
        return self._latest(
            history["outputs"], workflow, {stage["id"] for stage in workflow["stages"][:index]}
        )

    async def _work_input(self, card: Json, runtime: Json, participant: str) -> str:
        workflow = runtime["workflow"]
        index, lane = self._locate(workflow, participant)
        history = await self.store.history(card["id"])
        earlier = self._latest(
            history["outputs"], workflow, {stage["id"] for stage in workflow["stages"][:index]}
        )
        return agent_text.work_input(
            {**card, "runtime": runtime}, workflow, index, lane, history["comments"], earlier
        )

    async def _queue(
        self,
        card: Json,
        *,
        phase: str,
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
            and not is_review(participant)
            and participant not in runtime.get("participants", {})
            and not initial
        ):
            initial = True
            content = await self._work_input(card, runtime, participant) + "\n\n" + content
        runtime.pop("pending_action", None)
        runtime.pop("question", None)
        runtime.pop("note", None)
        runtime["phase"] = phase
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
                "state": phase if phase != "cleanup" else "discarded",
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
                    or (
                        card["state"] not in ACTIVE_STATES
                        and not (
                            card["state"] == "discarded"
                            and card["runtime"].get("phase") == "cleanup"
                        )
                    )
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
                        or (
                            card["state"] not in ACTIVE_STATES
                            and not (
                                card["state"] == "discarded" and runtime.get("phase") == "cleanup"
                            )
                        )
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
                        and not is_review(participant)
                        and runtime["phase"] in {"working", "integrating"}
                        and admission.existing
                        and admission.status in {"failed", "cancelled", "interrupted"}
                    ):
                        await self._queue(
                            card,
                            phase=runtime["phase"],
                            participant=participant,
                            content=agent_text.note(
                                "keep_integrating"
                                if runtime["phase"] == "integrating"
                                else "keep_working",
                                card,
                            ),
                            runtime=runtime,
                            patch={"turns": 0},
                            event="retry_reconciled",
                            detail={"run_id": admission.run_id},
                        )
                        return
                    runtime.pop("pending", None)
                    title = (
                        pending["kind"] == "initial"
                        and not is_review(participant)
                        and not runtime.get("titled")
                    )
                    if title:
                        runtime["titled"] = True
                    patch: Json = {"runtime": runtime}
                    if not is_review(participant) and runtime["phase"] != "cleanup":
                        patch["turns"] = int(card.get("turns", 0)) + 1
                    saved = await self._change(
                        card,
                        patch,
                        event="run_admitted",
                        detail={"participant": participant, "run_id": admission.run_id},
                    )
                    if title:
                        self.service.spawn(self.groups.title_group(group_id, card["title"]))
                    if admission.existing and admission.status != "running":
                        if runtime["phase"] == "cleanup":
                            self._close_later(saved)
                        elif admission.status == "interrupted":
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
                    if card["state"] == "discarded":
                        runtime = self._runtime(card)
                        runtime.pop("pending", None)
                        runtime["note"] = agent_text.error_message("cleanup_failed")
                        await self._change(card, {"runtime": runtime}, event="cleanup_failed")
                        self._close_later(card)
                    else:
                        await self._wait(
                            card,
                            "review_failed" if card["state"] == "reviewing" else "run_failed",
                            "launch_failed",
                        )
        finally:
            self._scheduled.discard(token)

    async def _guard(self, context: ToolContext, *, review: bool = False, states: set[str]) -> Json:
        owner = context.execution_owner
        if owner is None or owner.extension != "factory" or context.request_turn_end_hook is None:
            raise self._error("participant_required")
        card = await self.store.get_card(owner.group_id)
        runtime = card["runtime"]
        if (
            runtime.get("current_run") != context.run_id
            or runtime.get("epoch") != owner.epoch
            or runtime.get("current_participant") != owner.participant_id
            or is_review(owner.participant_id) != review
            or runtime.get("pending")
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
            settings={
                key: setup[key] for key in ("turn_budget", "base_branch", "integration_check")
            },
            passes={},
            rounds={},
            restarts=0,
        )
        return await self._begin_stage(
            card,
            runtime,
            0,
            event="started",
            patch={"workflow_id": workflow["id"]},
            detail={"workflow": workflow["name"]},
        )

    async def _begin_stage(
        self,
        card: Json,
        runtime: Json,
        index: int,
        *,
        note: str = "refresh",
        text: str = "",
        event: str,
        actor: str = "factory",
        patch: Json | None = None,
        **extra: Any,
    ) -> Json:
        stage = runtime["workflow"]["stages"][index]
        passes = runtime.setdefault("passes", {})
        passes[stage["id"]] = int(passes.get(stage["id"], 0)) + 1
        runtime.update(
            stage=index,
            lane=0,
            verdicts=[],
            review_kind="review",
            lane_note={"kind": note, "text": text},
            reviewer_retries=0,
        )
        return await self._run_lane(card, runtime, event=event, actor=actor, patch=patch, **extra)

    async def _run_lane(
        self,
        card: Json,
        runtime: Json,
        *,
        event: str,
        actor: str = "factory",
        patch: Json | None = None,
        **extra: Any,
    ) -> Json:
        """Queue the current lane of the current stage."""
        workflow = runtime["workflow"]
        index, lane = int(runtime["stage"]), int(runtime["lane"])
        stage = workflow["stages"][index]
        updated = {**card, **(patch or {}), "runtime": runtime}
        if stage["kind"] == "review":
            runtime["reviewer_sequence"] = int(runtime.get("reviewer_sequence", 0)) + 1
            participant = _workflows.review_participant(stage, lane, runtime["reviewer_sequence"])
            runtime["reviewer_nudges"] = 0
            history = await self.store.history(card["id"])
            target = {stage["target"]}
            content = agent_text.review_input(
                updated,
                workflow,
                index,
                under_review=[
                    item
                    for item in self._latest(history["outputs"], workflow, target)
                    if item["kind"] == "work"
                ],
                previous=[item for item in history["outputs"] if item["stage_id"] == stage["id"]],
                integration=runtime.get("review_kind") == "integration_review",
            )
            return await self._queue(
                card,
                phase="reviewing",
                participant=participant,
                initial=True,
                content=content,
                runtime=runtime,
                patch=patch,
                event=event,
                actor=actor,
                **extra,
            )
        participant = _workflows.work_participant(stage, lane)
        note = runtime.get("lane_note") or {"kind": "refresh", "text": ""}
        if participant in runtime.get("participants", {}):
            earlier = await self._earlier(card, runtime, index) if note["kind"] == "refresh" else []
            content = agent_text.note(note["kind"], updated, note["text"], earlier)
            initial = False
        else:
            content = await self._work_input(updated, runtime, participant)
            initial = True
        return await self._queue(
            card,
            phase="working",
            participant=participant,
            initial=initial,
            content=content,
            runtime=runtime,
            patch={**(patch or {}), "turns": 0},
            event=event,
            actor=actor,
            **extra,
        )

    async def _after_lane(
        self, card: Json, runtime: Json, *, event: str, actor: str, patch: Json, **extra: Any
    ) -> Json:
        stage = runtime["workflow"]["stages"][runtime["stage"]]
        if int(runtime["lane"]) + 1 < len(stage["lanes"]):
            runtime["lane"] = int(runtime["lane"]) + 1
            return await self._run_lane(
                card, runtime, event=event, actor=actor, patch=patch, **extra
            )
        if stage["approval"]:
            return await self._wait(
                card, "checkpoint", runtime=runtime, actor=actor, event=event, patch=patch, **extra
            )
        return await self._advance(card, runtime, event=event, actor=actor, patch=patch, **extra)

    async def _advance(
        self, card: Json, runtime: Json, *, event: str, actor: str, patch: Json | None, **extra: Any
    ) -> Json:
        index = int(runtime.get("stage", 0)) + 1
        if index < len(runtime["workflow"]["stages"]):
            return await self._begin_stage(
                card, runtime, index, event=event, actor=actor, patch=patch, **extra
            )
        return await self._wait(
            card, "finished", runtime=runtime, actor=actor, event=event, patch=patch, **extra
        )

    async def _integration_review(
        self, card: Json, runtime: Json, *, event: str, actor: str, patch: Json, **extra: Any
    ) -> Json:
        index = _workflows.integration_review(runtime["workflow"])
        runtime.update(review_kind="integration_review", reviewer_retries=0)
        if index is None:
            runtime["note"] = agent_text.error_message("integration_unreviewed")
            runtime["stage"] = _workflows.integrator(runtime["workflow"])
            return await self._wait(
                card, "finished", runtime=runtime, actor=actor, event=event, patch=patch, **extra
            )
        runtime.update(stage=index, lane=0, verdicts=[])
        return await self._run_lane(card, runtime, event=event, actor=actor, patch=patch, **extra)

    def _output(self, card: Json, runtime: Json, kind: str) -> Json:
        workflow = runtime["workflow"]
        participant = runtime["current_participant"]
        index, lane = self._locate(workflow, participant)
        stage = workflow["stages"][index]
        return {
            "stage_id": stage["id"],
            "stage_name": stage["name"],
            "kind": kind,
            "lane": lane,
            "pass": int(runtime.get("passes", {}).get(stage["id"], 1)),
            "participant_id": participant,
        }

    async def submit_work(self, context: ToolContext, arguments: Json) -> Json:
        card = await self._guard(context, states={"working", "integrating"})
        runtime = self._runtime(card)
        workflow = runtime["workflow"]
        index, _lane = self._locate(workflow, runtime["current_participant"])
        stage = workflow["stages"][index]
        summary = self._text(arguments, "summary")
        output = self._output(card, runtime, "work") | {"summary": summary}
        patch: Json = {"summary": summary, "recommendation": ""}
        if stage["output"] == "changes":
            if not all(
                isinstance(arguments.get(field), str) and arguments[field].strip()
                for field in ("branch", "base", "checks")
            ):
                raise self._error("missing_changes")
            branch = self._text(arguments, "branch")
            base = self._text(arguments, "base")
            checks = self._text(arguments, "checks")
            facts = await GitRepository(runtime["cwd"]).submission(branch, base)
            output.update(facts, checks=checks)
            patch.update(facts, checks=checks, round=card["round"] + 1)
        else:
            result = arguments.get("result")
            if not isinstance(result, str) or not result.strip():
                raise self._error("missing_result")
            if len(result) > _RESULT_LIMIT:
                raise FactoryError(
                    "text_too_long",
                    f"Nothing was changed. result exceeds {_RESULT_LIMIT} characters. "
                    "Shorten the document and submit it again.",
                )
            noted = arguments.get("checks")
            output.update(result=result, checks=noted if isinstance(noted, str) else "")
            patch["checks"] = output["checks"]
        runtime.update(restarts=0)
        runtime.pop("review_branch_head_sha", None)
        integration = (
            card["state"] == "integrating"
            or runtime.pop("next_review_kind", None) == "integration_review"
        )
        if integration:
            saved = await self._integration_review(
                card, runtime, event="submitted", actor="agent", patch=patch, output=output
            )
        else:
            saved = await self._after_lane(
                card, runtime, event="submitted", actor="agent", patch=patch, output=output
            )
        context.request_turn_end()
        return agent_text.tool_result("submitted", saved)

    async def report_blocked(self, context: ToolContext, arguments: Json) -> Json:
        card = await self._guard(context, states={"working", "integrating"})
        question = self._text(arguments, "question")
        runtime = self._runtime(card)
        runtime.update(question=question, restarts=0)
        saved = await self._wait(card, "blocked", runtime=runtime, actor="agent")
        context.request_turn_end()
        return agent_text.tool_result("blocked", saved)

    async def submit_review(self, context: ToolContext, arguments: Json) -> Json:
        card = await self._guard(context, review=True, states={"reviewing"})
        verdict = arguments.get("verdict")
        recommendation = arguments.get("recommendation", "")
        findings = arguments.get("findings", [])
        if (
            verdict not in {"approve", "changes_requested"}
            or not isinstance(recommendation, str)
            or len(recommendation) > 20000
            or not isinstance(findings, list)
            or len(findings) > 100
            or any(
                not isinstance(value, str) or not value.strip() or len(value) > 20000
                for value in findings
            )
            or verdict == "approve"
            and not recommendation.strip()
            or verdict == "changes_requested"
            and not findings
        ):
            raise self._error("invalid_verdict")
        runtime = self._runtime(card)
        runtime.update(restarts=0)
        workflow = runtime["workflow"]
        stage = workflow["stages"][runtime["stage"]]
        kind = runtime.get("review_kind", "review")
        integration = kind == "integration_review"
        landing = runtime.get("integration_landing")
        after_landing = (
            integration
            and isinstance(landing, dict)
            and landing.get("head_sha") == card["head_sha"]
        )
        output = self._output(card, runtime, kind) | {
            "verdict": verdict,
            "recommendation": recommendation,
            "findings": findings,
            "head_sha": card["head_sha"],
        }
        verdicts = runtime.setdefault("verdicts", [])
        verdicts.append(
            {"verdict": verdict, "recommendation": recommendation, "findings": findings}
        )
        recommendations = "\n\n".join(
            item["recommendation"].strip() for item in verdicts if item["recommendation"].strip()
        )
        patch: Json = {"recommendation": recommendation}
        detail = {"after_landing": after_landing}
        if after_landing and verdict == "changes_requested":
            assert isinstance(landing, dict)
            landing["status"] = "changes_requested"
            saved = await self._wait(
                card,
                "no_agreement",
                runtime=runtime,
                actor="agent",
                event="reviewed",
                patch=patch,
                output=output,
                detail=detail,
            )
        elif int(runtime["lane"]) + 1 < len(stage["lanes"]):
            runtime["lane"] = int(runtime["lane"]) + 1
            saved = await self._run_lane(
                card,
                runtime,
                event="reviewed",
                actor="agent",
                patch=patch,
                output=output,
                detail=detail,
            )
        else:
            patch["recommendation"] = recommendations
            changes = [
                finding
                for item in verdicts
                if item["verdict"] == "changes_requested"
                for finding in item["findings"]
            ]
            if not changes:
                if after_landing:
                    assert isinstance(landing, dict)
                    landing["status"] = "approved"
                if integration:
                    saved = await self._wait(
                        card,
                        "finished",
                        runtime=runtime,
                        actor="agent",
                        event="reviewed",
                        patch=patch,
                        output=output,
                        detail=detail,
                    )
                else:
                    saved = await self._after_lane(
                        card, runtime, event="reviewed", actor="agent", patch=patch, output=output
                    )
            else:
                rounds = runtime.setdefault("rounds", {})
                key = stage["id"] + (":integration" if integration else "")
                rounds[key] = int(rounds.get(key, 0)) + 1
                findings_text = "\n".join(changes)
                if rounds[key] >= int(stage["rounds"]):
                    saved = await self._wait(
                        card,
                        "no_agreement",
                        runtime=runtime,
                        actor="agent",
                        event="reviewed",
                        patch=patch,
                        output=output,
                        detail=detail,
                    )
                elif integration:
                    integrator = self._integrator(runtime)
                    assert integrator is not None
                    runtime.update(
                        next_review_kind="integration_review",
                        stage=_workflows.integrator(workflow),
                        lane=0,
                        reviewer_retries=0,
                    )
                    saved = await self._queue(
                        card,
                        phase="working",
                        participant=integrator,
                        content=agent_text.note("rework", card, findings_text),
                        runtime=runtime,
                        patch={**patch, "turns": 0},
                        event="changes_requested",
                        actor="agent",
                        output=output,
                    )
                else:
                    saved = await self._begin_stage(
                        card,
                        runtime,
                        _workflows.stage_index(workflow, stage["target"]),
                        note="rework",
                        text=findings_text,
                        event="changes_requested",
                        actor="agent",
                        patch=patch,
                        output=output,
                    )
        context.request_turn_end()
        return agent_text.tool_result("reviewed", saved)

    async def report_integration(self, context: ToolContext, arguments: Json) -> Json:
        card = await self._guard(context, states={"integrating"})
        outcome = arguments.get("outcome")
        if outcome not in {"integrated", "needs_changes", "failed"}:
            raise self._error("invalid_outcome")
        runtime = self._runtime(card)
        runtime["restarts"] = 0
        base_commit = self._text(arguments, "base_commit") if "base_commit" in arguments else None
        repository = GitRepository(runtime["cwd"])
        if outcome == "needs_changes":
            inspection = await repository.integration(
                card["branch"],
                card["base"],
                reviewed_base=card["base_sha"],
                reviewed_head=card["head_sha"],
                expected_branch_head=runtime.get("review_branch_head_sha"),
                accepted_base=runtime.get("integration_base_sha"),
                base_commit=base_commit,
            )
            if inspection["review_required"] and inspection["landed"]:
                return await self._review_integration(card, runtime, inspection, context)
            integrator = self._integrator(runtime)
            assert integrator is not None
            runtime.update(
                next_review_kind="integration_review",
                stage=_workflows.integrator(runtime["workflow"]),
                lane=0,
            )
            saved = await self._queue(
                card,
                phase="working",
                participant=integrator,
                runtime=runtime,
                content=agent_text.note("integration_changes", card, arguments.get("note", "")),
                patch={"turns": 0},
                event="integration_changes",
                actor="agent",
            )
            result = "integration_changes"
        elif outcome == "failed":
            runtime["note"] = self._text(arguments, "note")
            saved = await self._wait(card, "integration_failed", runtime=runtime, actor="agent")
            result = "integration_failed"
        else:
            reference = arguments.get("reference", "")
            if not isinstance(reference, str) or len(reference) > 2000:
                raise self._error("invalid_reference")
            try:
                parsed = urlsplit(reference)
            except ValueError as error:
                raise self._error("invalid_reference") from error
            url = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and not parsed.username
                and not parsed.password
            )
            if parsed.scheme and not url:
                raise self._error("invalid_reference")
            reference_only = url or self._check(runtime) == "reference_only"
            inspection = await repository.integration(
                card["branch"],
                card["base"],
                reviewed_base=card["base_sha"],
                reviewed_head=card["head_sha"],
                expected_branch_head=runtime.get("review_branch_head_sha"),
                accepted_base=runtime.get("integration_base_sha"),
                base_commit=base_commit,
                verify_merged=not reference_only,
                reported_reference=reference,
            )
            if inspection["review_required"]:
                return await self._review_integration(card, runtime, inspection, context)
            if reference_only or inspection["landed"]:
                # Project cleanup can legitimately delete the submitted branch.
                # Inspection still verifies its exact candidate and landing scope.
                saved = await self._done(card, reference or card["head_sha"], runtime)
                result = "integrated"
            else:
                runtime["note"] = agent_text.error_message(
                    "integration_not_found"
                    if inspection["branch_head_sha"]
                    else "integration_branch_missing"
                )
                saved = await self._wait(card, "integration_failed", runtime=runtime, actor="agent")
                result = "integration_failed"
        context.request_turn_end()
        return agent_text.tool_result(result, saved)

    @staticmethod
    def _check(runtime: Json) -> str:
        return str(runtime.get("settings", {}).get("integration_check", "merged"))

    async def _review_integration(
        self, card: Json, runtime: Json, inspection: Json, context: ToolContext
    ) -> Json:
        if inspection["landed"]:
            runtime["integration_landing"] = {
                key: inspection[key]
                for key in (
                    "head_sha",
                    "base_sha",
                    "landing_sha",
                    "branch_head_sha",
                    "extra_paths",
                    "merge_paths",
                )
            } | {"status": "pending"}
        detail = {
            key: inspection[key]
            for key in ("landed", "landing_sha", "target_sha", "extra_paths", "merge_paths")
        }
        if not inspection["base_sha"]:
            runtime.update(review_kind="integration_review")
            await self._change(
                card,
                {"runtime": runtime},
                actor="agent",
                event="integration_base_required",
                detail=detail,
            )
            return agent_text.integration_base_required()
        runtime["review_branch_head_sha"] = inspection["branch_head_sha"]
        saved = await self._integration_review(
            card,
            runtime,
            patch={
                "base_sha": inspection["base_sha"],
                "head_sha": inspection["head_sha"],
                "round": card["round"] + 1,
                "recommendation": "",
            },
            event="integration_review",
            actor="agent",
            detail=detail,
        )
        context.request_turn_end()
        if saved["state"] == "waiting":
            return agent_text.tool_result("integration_waiting", saved)
        return agent_text.tool_result(
            "post_integration_review" if inspection["landed"] else "integration_review", saved
        )

    async def _done(
        self,
        card: Json,
        reference: str,
        runtime: Json,
        *,
        actor: str = "agent",
        event: str = "integrated",
    ) -> Json:
        runtime.pop("pending_action", None)
        saved = await self._change(
            card,
            {
                "state": "done",
                "waiting_reason": "",
                "integration_reference": reference,
                "runtime": runtime,
            },
            actor=actor,
            event=event,
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
        workflow = runtime["workflow"]
        runtime["restarts"] = 0
        integration = runtime.get("review_kind") == "integration_review"
        if (
            reason != "finished"
            and not integration
            and int(runtime.get("stage", 0)) < len(workflow["stages"]) - 1
        ):
            # A checkpoint or an exhausted review: the user lets the Workflow go on.
            runtime["rounds"] = {}
            return await self._advance(card, runtime, event="accepted", actor="user", patch=None)
        integrator = self._integrator(runtime)
        if integrator is None:
            return await self._done(card, "", runtime, actor="user", event="accepted")
        if self._check(runtime) == "reference_only":
            runtime.pop("integration_base_sha", None)
        else:
            runtime["integration_base_sha"] = await GitRepository(runtime["cwd"]).base(card["base"])
        runtime.update(stage=_workflows.integrator(workflow), lane=0)
        return await self._queue(
            card,
            phase="integrating",
            participant=integrator,
            content=agent_text.note("integrate", {**card, "runtime": runtime}),
            runtime=runtime,
            patch={"turns": 0},
            event="accepted",
            actor="user",
        )

    async def send_back(self, card: Json, comment: str, stage: str = "") -> Json:
        runtime = self._action(card, "send_back")
        action = runtime.get("pending_action", {})
        recorded = action.get("comment_recorded", False)
        reason = action.get("waiting_reason") or card["waiting_reason"]
        runtime.update(rounds={}, restarts=0)
        runtime.pop("next_review_kind", None)
        workflow = runtime["workflow"]
        participant = runtime.get("current_participant")
        if reason == "blocked" and not stage and participant and not is_review(participant):
            phase = runtime.get("phase", "working")
            return await self._queue(
                card,
                phase=phase if phase in {"working", "integrating"} else "working",
                participant=participant,
                content=agent_text.note("answer", card, comment),
                runtime=runtime,
                patch={"turns": 0},
                event="answered",
                actor="user",
                comment=None if recorded else comment,
            )
        index = (
            _workflows.stage_index(workflow, stage)
            if stage
            else _workflows.send_back_stage(workflow, int(runtime.get("stage", 0)))
        )
        return await self._begin_stage(
            card,
            runtime,
            index,
            note="send_back",
            text=comment,
            event="sent_back",
            actor="user",
            patch={"recommendation": ""},
            comment=None if recorded else comment,
        )

    async def retry(self, card: Json) -> Json:
        runtime = self._action(card, "retry")
        acknowledged = await self._acknowledge_pending(card)
        if acknowledged is not None:
            card = acknowledged
            runtime = self._action(card, "retry")
        runtime["restarts"] = 0
        phase = runtime.get("phase", "working")
        pending = runtime.get("pending")
        if pending:
            pending["retry_terminal"] = True
            return await self._queue(
                card,
                phase=phase,
                participant=pending["participant"],
                content=pending["content"],
                initial=pending["kind"] == "initial",
                runtime=runtime,
                pending=pending,
                patch={"turns": 0},
                event="retried",
                actor="user",
            )
        workflow = runtime["workflow"]
        stage = workflow["stages"][int(runtime.get("stage", 0))]
        if phase == "reviewing" or stage["kind"] == "review" and phase != "integrating":
            runtime["reviewer_retries"] = 0
            return await self._run_lane(card, runtime, event="retried", actor="user")
        participant = (
            self._integrator(runtime)
            if phase == "integrating"
            else _workflows.work_participant(stage, int(runtime.get("lane", 0)))
        )
        assert participant is not None
        return await self._queue(
            card,
            phase=phase if phase in {"working", "integrating"} else "working",
            participant=participant,
            content=agent_text.note(
                "keep_integrating" if phase == "integrating" else "keep_working", card
            ),
            runtime=runtime,
            patch={"turns": 0},
            event="retried",
            actor="user",
        )

    @staticmethod
    def _cleanup_participants(runtime: Json) -> list[str]:
        """Existing participants of stages that change the repository, in stage order."""
        workflow = runtime.get("workflow")
        if not workflow:
            return []
        existing = runtime.get("participants", {})
        return [
            participant
            for stage in workflow["stages"]
            if stage["kind"] == "work" and stage["output"] == "changes"
            for lane in range(len(stage["lanes"]))
            if (participant := _workflows.work_participant(stage, lane)) in existing
        ]

    async def discard(self, card: Json) -> Json:
        if card["state"] in FINAL_STATES:
            raise self._error("wrong_state")
        runtime = self._runtime(card)
        cleanup = self._cleanup_participants(runtime)
        if cleanup:
            runtime["cleanup"] = cleanup[1:]
            return await self._queue(
                card,
                phase="cleanup",
                participant=cleanup[0],
                content=agent_text.note("discard", card),
                cancel=True,
                runtime=runtime,
                event="discarded",
                actor="user",
            )
        runtime.pop("pending", None)
        saved = await self._change(
            card,
            {"state": "discarded", "waiting_reason": "", "runtime": runtime},
            actor="user",
            event="discarded",
        )
        self._close_later(saved)
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

    def _close_later(self, card: Json, *, delete: bool = False) -> None:
        if self._quiescing:
            return
        key = card["id"] + (
            ":delete" if delete else f":close:{card['runtime'].get('current_run', '')}"
        )
        if key in self._closing_groups:
            return
        self._closing_groups.add(key)
        self.service.spawn(self._close(card["id"], key, delete=delete))

    async def _close(self, card_id: str, key: str, *, delete: bool) -> None:
        try:
            async with self.service.lock:
                card = await self.store.get_card(card_id)
                group_id = card["group_id"]
                current_run = card["runtime"].get("current_run")
            if not delete:
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
            cleanup = card["state"] == "discarded" and runtime.get("phase") == "cleanup"
            if card["state"] not in ACTIVE_STATES and not cleanup:
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
            if (
                not is_review(owner.participant_id)
                and not cleanup
                and card["turns"] >= self._budget(runtime)
            ):
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
            patch: Json = {"runtime": runtime}
            if not is_review(owner.participant_id) and not cleanup:
                patch["turns"] = card["turns"] + 1
            await self._change(card, patch, event="run_observed", detail={"run_id": context.run_id})
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
                or (
                    outcome != "success"
                    and not is_review(runtime.get("current_participant"))
                    and runtime.get("pending")
                )
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
        if (
            card["state"] == "discarded"
            and runtime.get("phase") == "cleanup"
            and runtime.get("cleanup")
            and not runtime.get("deleting")
        ):
            participant = runtime["cleanup"].pop(0)
            await self._queue(
                card,
                phase="cleanup",
                participant=participant,
                content=agent_text.note("discard", card),
                runtime=runtime,
                event="cleanup_continued",
            )
            return
        if card["state"] not in ACTIVE_STATES:
            self._close_later(card, delete=card["runtime"].get("deleting", False))
            return
        if card["state"] == "reviewing":
            if (
                outcome == "success"
                and not runtime.get("reviewer_nudges")
                and not runtime.get("reviewer_retries")
            ):
                runtime["reviewer_nudges"] = 1
                await self._queue(
                    card,
                    phase="reviewing",
                    participant=runtime["current_participant"],
                    content=agent_text.note("review_nudge", card),
                    runtime=runtime,
                    event="review_nudged",
                )
            elif outcome != "cancelled" and not runtime.get("reviewer_retries"):
                runtime["reviewer_retries"] = 1
                await self._run_lane(card, runtime, event="review_retried")
            else:
                await self._wait(card, "review_failed", "review_failed")
        elif outcome != "success":
            await self._wait(card, "run_failed", "run_failed")
        elif int(card["turns"]) >= self._budget(runtime):
            await self._wait(card, "budget_exhausted", "budget_exhausted")
        else:
            phase = card["state"]
            await self._queue(
                card,
                phase=phase,
                participant=runtime["current_participant"],
                content=agent_text.note(
                    "keep_integrating" if phase == "integrating" else "keep_working", card
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
        phase = runtime.get("phase", card["state"])
        if phase not in ACTIVE_STATES:
            return
        participant = runtime.get("current_participant")
        if not participant:
            await self._run_lane(card, runtime, event="restarted")
            return
        await self._queue(
            card,
            phase=phase,
            participant=participant,
            content=agent_text.note("restart", card),
            runtime=runtime,
            event="restarted",
        )

    async def _upgrade(self, card: Json) -> None:
        """Requeue a Card that started under the role-based Factory without a Workflow."""
        await self._change(
            card,
            {
                "state": "queued",
                "waiting_reason": "",
                "turns": 0,
                "runtime": {"cwd": card["runtime"].get("cwd", "")},
            },
            event="workflow_upgrade",
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
                    if runtime.get("phase") == "cleanup" and runtime.get("pending"):
                        self._launch_later(card)
                    elif runtime.get("phase") == "cleanup" and runtime.get("cleanup"):
                        await self._completed(card, "success")
                    else:
                        self._close_later(card)
                elif card["state"] in {"proposal", "queued"}:
                    continue
                elif not runtime.get("workflow"):
                    await self._upgrade(card)
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
                    else:
                        await self._run_lane(card, self._runtime(card), event="recovered_initial")
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
