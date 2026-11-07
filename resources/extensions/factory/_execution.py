"""Persisted Factory handoffs over the host's canonical owned execution groups."""

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
from core.tools import ToolContext
from core.tools.availability import ToolAccess
from core.utils.ids import new_id
from core.utils.logging import get_logger

from . import agent_text
from ._git import GitRepository
from .store import ACTIVE_STATES, FINAL_STATES, FactoryError

type Json = dict[str, Any]
_LOGGER = get_logger("extensions.factory")
_PRIVATE = frozenset(agent_text.PRIVATE_TOOLS)
_WORKER_TOOLS = frozenset(
    {"factory_submit_work", "factory_report_blocked", "factory_report_integration"}
)


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

    async def _change(
        self, card: Json, patch: Json, *, actor: str = "factory", event: str, **extra: Any
    ) -> Json:
        saved = await self.store.change(card["id"], patch, actor=actor, event=event, **extra)
        await self.service.changed(saved, card, actor=actor)
        return cast(Json, saved)

    def _config(self, card: Json, participant: str) -> TemporaryAgentConfig:
        runtime = card["runtime"]
        setup = runtime["setup"]
        reviewer = participant != "worker"
        role = "reviewer" if reviewer else "worker"
        tools = {"factory_submit_review"} if reviewer else _WORKER_TOOLS
        denied = set(_PRIVATE) - set(tools)
        if reviewer:
            denied.update({"apply_patch", "edit", "write"})
        return TemporaryAgentConfig(
            model="",
            repository_profile=setup[f"{role}_profile"],
            model_override=setup.get(f"{role}_model") or None,
            thinking_effort_override=setup.get(f"{role}_effort") or None,
            cwd=Path(runtime["cwd"]),
            tool_access=ToolAccess(mode="all", denied=tuple(sorted(denied))),
            allowed_skills=["*"],
            tools={},
            name=f"Factory {role.title()}",
            instructions=agent_text.REVIEWER_RULES if reviewer else agent_text.WORKER_RULES,
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
        **extra: Any,
    ) -> Json:
        runtime = runtime if runtime is not None else self._runtime(card)
        if (
            participant == "worker"
            and "worker" not in runtime.get("participants", {})
            and not initial
        ):
            initial = True
            hydrated = {**card, "comments": (await self.store.history(card["id"]))["comments"]}
            content = agent_text.worker_input(hydrated) + "\n\n" + content
        runtime.pop("pending_action", None)
        runtime.pop("question", None)
        runtime.pop("note", None)
        runtime["phase"] = phase
        runtime["pending"] = {
            "id": new_id("fci"),
            "participant": participant,
            "kind": "initial" if initial else "continuation",
            "content": content,
            "cancel_previous": cancel,
        }
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
                if pending.get("id") != token or self._quiescing:
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
                    if runtime.get("pending", {}).get("id") != token or self._quiescing:
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
                        }
                    handle = await self.groups.open_group(group_id)
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
                    runtime.pop("pending", None)
                    runtime.setdefault("runs", {})[participant] = admission.run_id
                    patch: Json = {"runtime": runtime}
                    if participant == "worker" and runtime["phase"] != "cleanup":
                        patch["worker_turns"] = int(card.get("worker_turns", 0)) + 1
                    saved = await self._change(
                        card,
                        patch,
                        event="run_admitted",
                        detail={"participant": participant, "run_id": admission.run_id},
                    )
                    if pending["kind"] == "initial" and participant == "worker":
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

    async def _guard(
        self, context: ToolContext, *, reviewer: bool = False, states: set[str]
    ) -> Json:
        owner = context.execution_owner
        if owner is None or owner.extension != "factory" or context.request_turn_end_hook is None:
            raise self._error("participant_required")
        card = await self.store.get_card(owner.group_id)
        runtime = card["runtime"]
        if (
            runtime.get("current_run") != context.run_id
            or runtime.get("epoch") != owner.epoch
            or runtime.get("current_participant") != owner.participant_id
            or (owner.participant_id != "worker") != reviewer
            or runtime.get("pending")
        ):
            raise self._error("stale_run")
        if card["state"] not in states:
            raise self._error("wrong_state")
        return cast(Json, card)

    async def start(self, card: Json) -> Json:
        if card["state"] != "queued" or card["runtime"].get("pending_action"):
            raise self._error("wrong_state")
        hydrated = {**card, "comments": (await self.store.history(card["id"]))["comments"]}
        runtime = self._runtime(card)
        runtime.update(auto_round=0, restarts=0)
        return await self._queue(
            card,
            phase="working",
            participant="worker",
            initial=True,
            content=agent_text.worker_input(hydrated),
            runtime=runtime,
            patch={"worker_turns": 0},
            event="started",
        )

    async def _review(
        self,
        card: Json,
        *,
        runtime: Json | None = None,
        patch: Json | None = None,
        event: str = "review_started",
        actor: str = "factory",
    ) -> Json:
        runtime = runtime if runtime is not None else self._runtime(card)
        runtime["reviewer_sequence"] = int(runtime.get("reviewer_sequence", 0)) + 1
        participant = f"reviewer-{runtime['reviewer_sequence']}"
        runtime["reviewer_nudges"] = 0
        history = await self.store.history(card["id"])
        updated = {**card, **(patch or {}), "runtime": runtime}
        return await self._queue(
            card,
            phase="reviewing",
            participant=participant,
            initial=True,
            runtime=runtime,
            content=agent_text.reviewer_input(updated, history["reviews"]),
            patch=patch,
            event=event,
            actor=actor,
        )

    async def submit_work(self, context: ToolContext, arguments: Json) -> Json:
        card = await self._guard(context, states={"working", "integrating"})
        branch = self._text(arguments, "branch")
        base = self._text(arguments, "base")
        summary = self._text(arguments, "summary")
        checks = self._text(arguments, "checks")
        facts = await GitRepository(card["runtime"]["cwd"]).submission(branch, base)
        runtime = self._runtime(card)
        kind = runtime.pop(
            "next_review_kind", "integration_review" if card["state"] == "integrating" else "review"
        )
        runtime.update(review_kind=kind, reviewer_retries=0, restarts=0)
        if kind == "review":
            runtime["auto_round"] = int(runtime.get("auto_round", 0)) + 1
        saved = await self._review(
            card,
            runtime=runtime,
            patch={
                **facts,
                "round": card["round"] + 1,
                "summary": summary,
                "checks": checks,
                "recommendation": "",
            },
            actor="worker",
            event="submitted",
        )
        context.request_turn_end()
        return agent_text.tool_result("submitted", saved)

    async def report_blocked(self, context: ToolContext, arguments: Json) -> Json:
        card = await self._guard(context, states={"working", "integrating"})
        question = self._text(arguments, "question")
        runtime = self._runtime(card)
        runtime.update(question=question, restarts=0)
        saved = await self._wait(card, "blocked", runtime=runtime, actor="worker")
        context.request_turn_end()
        return agent_text.tool_result("blocked", saved)

    async def submit_review(self, context: ToolContext, arguments: Json) -> Json:
        card = await self._guard(context, reviewer=True, states={"reviewing"})
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
        review = {
            "round": card["round"],
            "kind": runtime.get("review_kind", "review"),
            "participant_id": runtime["current_participant"],
            "verdict": verdict,
            "recommendation": recommendation,
            "findings": findings,
            "reviewed_head_sha": card["head_sha"],
        }
        if verdict == "approve" or int(runtime.get("auto_round", 1)) >= int(
            runtime["setup"].get("review_rounds", 2)
        ):
            saved = await self._change(
                card,
                {
                    "state": "waiting",
                    "waiting_reason": "approved" if verdict == "approve" else "no_agreement",
                    "recommendation": recommendation,
                    "runtime": runtime,
                },
                actor="reviewer",
                event="reviewed",
                review=review,
            )
            self._close_later(saved)
        else:
            runtime["reviewer_retries"] = 0
            # Record the review and the handoff in one domain transaction.
            runtime["phase"] = "working"
            runtime["pending"] = {
                "id": new_id("fci"),
                "participant": "worker",
                "kind": "continuation",
                "content": agent_text.note("rework", card, "\n".join(findings)),
                "cancel_previous": False,
            }
            saved = await self._change(
                card,
                {
                    "state": "working",
                    "waiting_reason": "",
                    "worker_turns": 0,
                    "recommendation": recommendation,
                    "runtime": runtime,
                },
                actor="reviewer",
                event="changes_requested",
                review=review,
            )
            self._launch_later(saved)
        context.request_turn_end()
        return agent_text.tool_result("reviewed", saved)

    async def report_integration(self, context: ToolContext, arguments: Json) -> Json:
        card = await self._guard(context, states={"integrating"})
        outcome = arguments.get("outcome")
        if outcome not in {"integrated", "needs_changes", "failed"}:
            raise self._error("invalid_outcome")
        runtime = self._runtime(card)
        runtime["restarts"] = 0
        if outcome == "needs_changes":
            runtime["next_review_kind"] = "integration_review"
            saved = await self._queue(
                card,
                phase="working",
                participant="worker",
                runtime=runtime,
                content=agent_text.note("integration_changes", card, arguments.get("note", "")),
                patch={"worker_turns": 0},
                event="integration_changes",
                actor="worker",
            )
            result = "integration_changes"
        elif outcome == "failed":
            runtime["note"] = self._text(arguments, "note")
            saved = await self._wait(card, "integration_failed", runtime=runtime, actor="worker")
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
            repository = GitRepository(runtime["cwd"])
            head = await repository.head(card["branch"], missing_ok=True)
            if head is not None and head != card["head_sha"]:
                facts = await repository.submission(card["branch"], card["base"])
                runtime.update(review_kind="integration_review", reviewer_retries=0)
                saved = await self._review(
                    card,
                    runtime=runtime,
                    patch={**facts, "round": card["round"] + 1},
                    event="integration_review",
                    actor="worker",
                )
                result = "integration_changes"
            elif url or runtime["setup"].get("integration_check", "merged") == "reference_only":
                saved = await self._done(card, reference or card["head_sha"], runtime)
                result = "integrated"
            elif await repository.is_ancestor(card["head_sha"], card["base"]):
                # Project cleanup can legitimately delete the submitted branch.
                # The exact reviewed commit on base is sufficient in that case.
                saved = await self._done(card, reference or card["head_sha"], runtime)
                result = "integrated"
            else:
                runtime["note"] = agent_text.error_message(
                    "integration_not_found" if head else "integration_branch_missing"
                )
                saved = await self._wait(
                    card, "integration_failed", runtime=runtime, actor="worker"
                )
                result = "integration_failed"
        context.request_turn_end()
        return agent_text.tool_result(result, saved)

    async def _done(self, card: Json, reference: str, runtime: Json) -> Json:
        saved = await self._change(
            card,
            {
                "state": "done",
                "waiting_reason": "",
                "integration_reference": reference,
                "runtime": runtime,
            },
            actor="worker",
            event="integrated",
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
        if card["waiting_reason"] not in {"approved", "no_agreement"}:
            raise self._error("wrong_state")
        runtime["restarts"] = 0
        return await self._queue(
            card,
            phase="integrating",
            participant="worker",
            content=agent_text.note("integrate", card),
            runtime=runtime,
            patch={"worker_turns": 0},
            event="accepted",
            actor="user",
        )

    async def send_back(self, card: Json, comment: str) -> Json:
        runtime = self._action(card, "send_back")
        recorded = runtime.get("pending_action", {}).get("comment_recorded", False)
        runtime.update(auto_round=0, restarts=0)
        runtime.pop("next_review_kind", None)
        return await self._queue(
            card,
            phase="working",
            participant="worker",
            content=agent_text.note("send_back", card, comment),
            runtime=runtime,
            patch={"worker_turns": 0, "recommendation": ""},
            event="sent_back",
            actor="user",
            comment=None if recorded else comment,
        )

    async def retry(self, card: Json) -> Json:
        runtime = self._action(card, "retry")
        runtime["restarts"] = 0
        phase = runtime.get("phase", "working")
        if phase == "reviewing":
            runtime["reviewer_retries"] = 0
            return await self._review(card, runtime=runtime, event="retried", actor="user")
        return await self._queue(
            card,
            phase=phase if phase in {"working", "integrating"} else "working",
            participant="worker",
            content=agent_text.note(
                "keep_integrating" if phase == "integrating" else "keep_working", card
            ),
            runtime=runtime,
            initial="worker" not in runtime.get("participants", {}),
            patch={"worker_turns": 0},
            event="retried",
            actor="user",
        )

    async def discard(self, card: Json) -> Json:
        if card["state"] in FINAL_STATES:
            raise self._error("wrong_state")
        runtime = self._runtime(card)
        if "worker" in runtime.get("participants", {}):
            return await self._queue(
                card,
                phase="cleanup",
                participant="worker",
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
    ) -> Json:
        runtime = runtime if runtime is not None else self._runtime(card)
        runtime.pop("pending", None)
        if message is not None:
            runtime["note"] = agent_text.error_message(message)
        saved = await self._change(
            card,
            {"state": "waiting", "waiting_reason": reason, "runtime": runtime},
            actor=actor,
            event=reason,
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
            ) or runtime.get("current_run") == context.run_id:
                return None
            if pending and pending.get("kind") != "continuation":
                raise self._error("stale_run")
            if (
                owner.participant_id == "worker"
                and not cleanup
                and card["worker_turns"] >= int(runtime["setup"].get("turn_budget", 30))
            ):
                await self._wait(card, "budget_exhausted", "budget_exhausted")
                raise self._error("budget_exhausted")
            runtime["current_run"] = context.run_id
            runtime.setdefault("runs", {})[owner.participant_id] = context.run_id
            runtime.pop("pending", None)
            patch: Json = {"runtime": runtime}
            if owner.participant_id == "worker":
                patch["worker_turns"] = card["worker_turns"] + 1
            await self._change(card, patch, event="run_observed", detail={"run_id": context.run_id})
            if pending:
                content = pending["content"]
                return PreparedSessionDelivery(
                    pending["id"],
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    (content,),
                    str(card["revision"]),
                    "factory_continuation",
                )
        return None

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
            if context.run_id != runtime.get(
                "current_run"
            ) or context.execution_owner.epoch != runtime.get("epoch"):
                return
            if runtime.get("pending"):
                self._launch_later(card)
            else:
                await self._completed(card, outcome)

    async def _completed(self, card: Json, outcome: str) -> None:
        if card["state"] not in ACTIVE_STATES:
            self._close_later(card, delete=card["runtime"].get("deleting", False))
            return
        runtime = self._runtime(card)
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
                await self._review(card, runtime=runtime, event="review_retried")
            else:
                await self._wait(card, "review_failed", "review_failed")
        elif outcome != "success":
            await self._wait(card, "run_failed", "run_failed")
        elif int(card["worker_turns"]) >= int(runtime["setup"].get("turn_budget", 30)):
            await self._wait(card, "budget_exhausted", "budget_exhausted")
        else:
            phase = card["state"]
            await self._queue(
                card,
                phase=phase,
                participant="worker",
                content=agent_text.note(
                    "keep_integrating" if phase == "integrating" else "keep_working", card
                ),
                runtime=runtime,
                event="worker_nudged",
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
        participant = runtime.get("current_participant", "worker")
        await self._queue(
            card,
            phase=phase,
            participant=participant,
            content=agent_text.note("restart", card),
            runtime=runtime,
            event="restarted",
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
                runtime = card["runtime"]
                if runtime.get("deleting"):
                    self._close_later(card, delete=True)
                elif card["state"] in FINAL_STATES:
                    self._close_later(card)
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
                    elif card["state"] == "working":
                        hydrated = {
                            **card,
                            "comments": (await self.store.history(card["id"]))["comments"],
                        }
                        await self._queue(
                            card,
                            phase="working",
                            participant="worker",
                            initial=True,
                            content=agent_text.worker_input(hydrated),
                            event="recovered_initial",
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
