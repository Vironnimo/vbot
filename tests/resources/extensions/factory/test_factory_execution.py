"""Factory execution contracts over deterministic canonical-group stand-ins."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents.temporary_execution import TemporaryRunInput
from core.extensions import SessionRequestContext
from core.runs import RunExecutionOwner
from core.sessions import SessionAddress
from resources.extensions.factory import _execution
from resources.extensions.factory._execution import FactoryExecution
from resources.extensions.factory.store import FactoryError

type Json = dict[str, Any]
_HEAD = "a" * 40
_NEW = "b" * 40


class Store:
    def __init__(self) -> None:
        self.cards: dict[str, Json] = {}
        self.histories: dict[str, Json] = {}

    def add(self, card_id: str = "fct_card") -> Json:
        card: Json = {
            "id": card_id,
            "group_id": card_id,
            "project_id": "repo",
            "title": "Card",
            "description": "Objective",
            "state": "queued",
            "waiting_reason": "",
            "round": 0,
            "worker_turns": 0,
            "revision": 1,
            "branch": "topic",
            "base": "main",
            "base_sha": "0" * 40,
            "head_sha": _HEAD,
            "summary": "Submitted implementation",
            "checks": "Checks passed",
            "recommendation": "",
            "integration_reference": "",
            "runtime": {
                "cwd": "C:/repo",
                "setup": {
                    "worker_profile": "builder",
                    "reviewer_profile": "reviewer",
                    "worker_model": "provider/worker",
                    "reviewer_model": "other/reviewer",
                    "review_rounds": 2,
                    "turn_budget": 2,
                    "base_branch": "main",
                },
            },
        }
        self.cards[card_id] = deepcopy(card)
        self.histories[card_id] = {"events": [], "comments": [], "reviews": []}
        return card

    async def get_card(self, card_id: str) -> Json:
        if card_id not in self.cards:
            raise FactoryError("card_not_found", "missing")
        return deepcopy(self.cards[card_id])

    async def list_cards(self, project_id=None, state=None):
        return [
            deepcopy(card)
            for card in self.cards.values()
            if state is None or card["state"] == state
        ]

    async def history(self, card_id: str) -> Json:
        return deepcopy(self.histories[card_id])

    async def change(self, card_id, patch, *, actor, event, review=None, comment=None, **_extra):
        card = self.cards[card_id]
        card.update(deepcopy(patch))
        card["revision"] += 1
        history = self.histories[card_id]
        history["events"].append({"event": event, "actor": actor})
        if review is not None:
            history["reviews"].append(deepcopy(review))
        if comment is not None:
            history["comments"].append({"author": actor, "text": comment})
        return deepcopy(card)

    async def delete_card(self, card_id):
        self.cards.pop(card_id, None)


class Run:
    def __init__(self, run_id: str, owner: RunExecutionOwner, binding) -> None:
        self.id = run_id
        self.execution_owner = owner
        self.binding = binding
        self.status = SimpleNamespace(value="running")
        self.finished = asyncio.Event()
        self.wait_entered = asyncio.Event()

    async def wait(self):
        self.wait_entered.set()
        await self.finished.wait()

    def finish(self, outcome="completed"):
        self.status.value = outcome
        self.finished.set()


class Groups:
    def __init__(self, service) -> None:
        self.service = service
        self.bindings: dict[tuple[str, str], Any] = {}
        self.configs: dict[str, Any] = {}
        self.open: dict[str, Any] = {}
        self.runs: dict[str, Run] = {}
        self.admissions: list[tuple[str, Any]] = []
        self.closed: list[str] = []
        self.archived: list[str] = []
        self.epochs = 0
        self.recovered = False

    async def create(self, group, participant, config, *, project_id):
        assert not self.open.get(group)
        self.configs[participant] = config
        binding = self.bindings.setdefault(
            (group, participant),
            SimpleNamespace(
                group_id=group,
                participant_id=participant,
                generation_id=f"gen-{participant}",
                address=SessionAddress(project_id, f"tmp-{participant}", f"session-{participant}"),
            ),
        )
        return binding

    async def open_group(self, group):
        if not self.open.get(group):
            self.epochs += 1
            self.open[group] = f"epoch-{self.epochs}"
        return SimpleNamespace(group_id=group, epoch=self.open[group])

    async def start(self, handle, participant, input):
        assert self.open[handle.group_id]
        binding = self.bindings[(handle.group_id, participant)]
        owner = RunExecutionOwner(
            "factory", handle.group_id, participant, binding.generation_id, handle.epoch
        )
        run = Run(f"run-{len(self.runs) + 1}", owner, binding)
        self.runs[run.id] = run
        self.admissions.append((participant, input))
        return SimpleNamespace(run_id=run.id, status="running", existing=False)

    async def owned_runs(self, group, ids):
        return {
            run_id: SimpleNamespace(
                run=None if self.recovered else self.runs[run_id],
                record=SimpleNamespace(terminal_status=self.runs[run_id].status.value),
            )
            for run_id in ids
            if run_id in self.runs and self.runs[run_id].execution_owner.group_id == group
        }

    async def close_group(self, group):
        self.open[group] = False
        self.closed.append(group)
        for run in tuple(self.runs.values()):
            if run.execution_owner.group_id == group and run.status.value == "running":
                await self.service.execution.run_finished(self.context(run), outcome="cancelled")
                run.finish("cancelled")

    def context(self, run):
        return SessionRequestContext(
            run.binding,
            run.id,
            run.binding.address.agent_id,
            run.binding.address.session_id,
            run.execution_owner,
        )

    async def groups(self, *, after="", limit=100):
        return sorted(
            {group for group, _ in self.bindings if group > after and group not in self.archived}
        )[:limit]

    async def archive_group(self, group):
        assert not self.open.get(group)
        self.archived.append(group)

    async def title_group(self, *_args):
        return "Card"

    async def quiesce(self, reason):
        for group in tuple(self.open):
            await self.close_group(group)


class Service:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.store = Store()
        self.tasks: list[asyncio.Task[Any]] = []
        self.changes: list[tuple[str, str | None]] = []
        self.host = SimpleNamespace(temporary_agents=Groups(self), publish_change=lambda *_: None)
        self.execution = FactoryExecution(self)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def changed(self, card, previous=None, **_extra):
        self.changes.append((card["state"], previous["state"] if previous else None))

    async def flush(self):
        # Explicitly remove every settled snapshot: gather(done) need not yield.
        while self.tasks:
            current, self.tasks = self.tasks, []
            await asyncio.gather(*current)

    async def card(self):
        return await self.store.get_card("fct_card")

    def tool_context(self, card):
        run = self.host.temporary_agents.runs[card["runtime"]["current_run"]]
        return SimpleNamespace(
            execution_owner=run.execution_owner,
            run_id=run.id,
            request_turn_end_hook=True,
            request_turn_end=lambda: None,
        )

    async def finish(self, outcome="success"):
        card = await self.card()
        run = self.host.temporary_agents.runs[card["runtime"]["current_run"]]
        # Mirrors the real contract: owner callback before terminal publication.
        await self.execution.run_finished(self.host.temporary_agents.context(run), outcome=outcome)
        run.finish({"success": "completed", "error": "failed"}.get(outcome, outcome))
        await self.flush()


class Git:
    head_value: str | None = _HEAD
    ancestor = True

    def __init__(self, _cwd):
        pass

    async def submission(self, branch, base):
        return {
            "branch": branch,
            "base": base,
            "base_sha": "0" * 40,
            "head_sha": self.head_value or _HEAD,
        }

    async def head(self, _branch, **_kwargs):
        return self.head_value

    async def is_ancestor(self, _head, _base):
        return self.ancestor


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(_execution, "GitRepository", Git)
    monkeypatch.setattr(Git, "head_value", _HEAD)
    monkeypatch.setattr(Git, "ancestor", True)
    return Service()


async def started(service):
    async with service.lock:
        await service.execution.start(service.store.add())
    await service.flush()
    return await service.card()


async def submitted(service):
    card = await service.card()
    async with service.lock:
        result = await service.execution.submit_work(
            service.tool_context(card),
            {
                "branch": "topic",
                "base": "main",
                "summary": "Implementation",
                "checks": "Checked",
            },
        )
    assert result["ok"]
    await service.finish()
    return await service.card()


async def approved(service):
    card = await submitted(service)
    async with service.lock:
        await service.execution.submit_review(
            service.tool_context(card),
            {
                "verdict": "approve",
                "recommendation": "Idea and implementation are sound.",
            },
        )
    await service.finish()
    return await service.card()


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_failure", [False, True])
async def test_worker_reviewer_acceptance_integration_and_archival(
    service, monkeypatch, archive_failure
):
    card = await started(service)
    assert card["state"] == "working" and card["worker_turns"] == 1
    groups = service.host.temporary_agents
    assert groups.configs["worker"].model_override == "provider/worker"
    assert "factory_submit_review" in groups.configs["worker"].tool_access.denied
    card = await approved(service)
    assert card["state"] == "waiting" and card["waiting_reason"] == "approved"
    assert groups.configs["reviewer-1"].model_override == "other/reviewer"
    assert {"apply_patch", "edit", "write", "factory_submit_work", "factory_report_blocked"} <= set(
        groups.configs["reviewer-1"].tool_access.denied
    )
    async with service.lock:
        await service.execution.accept(card)
    await service.flush()
    card = await service.card()
    assert card["state"] == "integrating"
    assert groups.admissions[-1][1].kind == "continuation"
    monkeypatch.setattr(Git, "head_value", None)  # Project cleanup removed branch.
    async with service.lock:
        result = await service.execution.report_integration(
            service.tool_context(card), {"outcome": "integrated"}
        )
    assert result["ok"]
    await service.finish()
    card = await service.card()
    assert card["state"] == "done" and card["integration_reference"] == _HEAD
    assert set(card["runtime"]["runs"]) == {"worker", "reviewer-1"}
    archive_group = groups.archive_group
    if archive_failure:

        async def unavailable(_group):
            raise OSError("Archive temporarily unavailable")

        monkeypatch.setattr(groups, "archive_group", unavailable)
    async with service.lock:
        await service.execution.delete(card)
    await service.flush()
    if archive_failure:
        card = await service.card()
        assert card["runtime"]["note"] and not groups.archived
        monkeypatch.setattr(groups, "archive_group", archive_group)
        async with service.lock:
            await service.execution.delete(card)
        await service.flush()
    assert not service.store.cards and "fct_card" in groups.archived


@pytest.mark.asyncio
async def test_review_rejections_use_fresh_reviewers_and_exhaust_rounds(service):
    await started(service)
    stale_context = None
    for round_number in (1, 2):
        card = await submitted(service)
        assert card["round"] == round_number
        current_context = service.tool_context(card)
        if stale_context is not None:
            async with service.lock:
                with pytest.raises(FactoryError) as caught:
                    await service.execution.submit_review(
                        stale_context, {"verdict": "approve", "recommendation": "stale"}
                    )
            assert caught.value.code == "stale_run"
        stale_context = current_context
        async with service.lock:
            await service.execution.submit_review(
                current_context,
                {
                    "verdict": "changes_requested",
                    "findings": [f"Finding {round_number}"],
                },
            )
        await service.finish()
    card = await service.card()
    assert (card["state"], card["waiting_reason"]) == ("waiting", "no_agreement")
    inputs = [
        input.content
        for participant, input in service.host.temporary_agents.admissions
        if participant == "reviewer-2"
    ]
    assert len(inputs) == 1 and "Finding 1" in inputs[0]
    async with service.lock:
        await service.execution.send_back(card, "Reconsider both findings")
    await service.flush()
    assert (await service.card())["runtime"]["auto_round"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancelled"])
async def test_worker_budget_and_failure_do_not_retry_failed_runs(service, outcome):
    await started(service)
    await service.finish(outcome)
    card = await service.card()
    if outcome == "success":
        assert card["state"] == "working" and card["worker_turns"] == 2
        await service.finish()
        card = await service.card()
        assert card["waiting_reason"] == "budget_exhausted"
    else:
        assert card["waiting_reason"] == "run_failed"
        assert len(service.host.temporary_agents.admissions) == 1


@pytest.mark.asyncio
async def test_reviewer_has_one_nudge_then_one_fresh_attempt(service):
    await started(service)
    await submitted(service)
    for _ in range(3):
        await service.finish()
    card = await service.card()
    assert card["waiting_reason"] == "review_failed"
    assert [participant for participant, _ in service.host.temporary_agents.admissions] == [
        "worker",
        "reviewer-1",
        "reviewer-1",
        "reviewer-2",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["moved", "missing", "reference", "needs_changes", "failed"])
async def test_integration_checks_and_changed_heads_return_to_review(service, monkeypatch, case):
    await started(service)
    card = await approved(service)
    async with service.lock:
        await service.execution.accept(card)
    await service.flush()
    card = await service.card()
    if case == "moved":
        monkeypatch.setattr(Git, "head_value", _NEW)
    if case == "missing":
        monkeypatch.setattr(Git, "ancestor", False)
    arguments = {
        "outcome": case if case in {"needs_changes", "failed"} else "integrated",
        "note": "Integration conflict",
    }
    if case == "reference":
        monkeypatch.setattr(Git, "ancestor", False)
        arguments["reference"] = "https://example.test/pull/1"
    async with service.lock:
        await service.execution.report_integration(service.tool_context(card), arguments)
    await service.finish()
    card = await service.card()
    if case == "moved":
        assert card["state"] == "reviewing" and card["head_sha"] == _NEW
        assert (
            card["runtime"]["review_kind"] == "integration_review"
            and card["runtime"]["auto_round"] == 1
        )
    elif case in {"failed", "missing"}:
        assert (card["state"], card["waiting_reason"]) == ("waiting", "integration_failed")
    elif case == "needs_changes":
        assert card["state"] == "working"
        card = await submitted(service)
        assert card["runtime"]["review_kind"] == "integration_review"
    else:
        assert card["state"] == "done"


@pytest.mark.asyncio
async def test_terminal_tool_handoff_waits_until_old_run_is_published(service):
    card = await started(service)
    groups = service.host.temporary_agents
    worker = groups.runs[card["runtime"]["current_run"]]
    async with service.lock:
        await service.execution.submit_work(
            service.tool_context(card),
            {
                "branch": "topic",
                "base": "main",
                "summary": "Ready",
                "checks": "Passed",
            },
        )
    await worker.wait_entered.wait()
    assert "reviewer-1" not in groups.configs
    await service.finish()
    assert "reviewer-1" in groups.configs


@pytest.mark.asyncio
async def test_discard_cancels_review_and_gives_worker_one_cleanup_turn(service):
    await started(service)
    card = await submitted(service)
    async with service.lock:
        await service.execution.discard(card)
    await service.flush()
    card = await service.card()
    assert card["state"] == "discarded" and card["runtime"]["current_participant"] == "worker"
    count = len(service.host.temporary_agents.admissions)
    await service.finish()
    assert len(service.host.temporary_agents.admissions) == count
    assert not service.host.temporary_agents.open[card["group_id"]]


@pytest.mark.asyncio
async def test_restart_is_idempotent_and_second_interruption_waits_for_user(service):
    card = await started(service)
    groups = service.host.temporary_agents
    # An unknown canonical group is archived, not deleted from Session storage.
    groups.bindings[("orphan", "peer")] = SimpleNamespace()
    for interruption in (1, 2):
        run = groups.runs[(await service.card())["runtime"]["current_run"]]
        run.finish("interrupted")
        groups.recovered = True
        service.execution = FactoryExecution(service)
        await service.execution.reconcile()
        await service.execution.reconcile()
        await service.flush()
        card = await service.card()
        assert card["runtime"]["restarts"] == interruption
    assert card["waiting_reason"] == "restart"
    assert groups.archived == ["orphan"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["accept", "send_back", "retry"])
async def test_wip_queued_operator_actions_are_admitted_without_duplicate_comments(service, action):
    await started(service)
    card = await approved(service)
    runtime = deepcopy(card["runtime"])
    runtime["pending_action"] = {"action": action, "comment": "Comment", "comment_recorded": True}
    card = await service.store.change(
        card["id"], {"state": "queued", "runtime": runtime}, actor="user", event="queued"
    )
    async with service.lock:
        if action == "send_back":
            await service.execution.send_back(card, "Comment")
        else:
            await getattr(service.execution, action)(card)
    await service.flush()
    card = await service.card()
    assert "pending_action" not in card["runtime"]
    assert card["state"] in {"working", "integrating", "reviewing"}
    assert not (await service.store.history(card["id"]))["comments"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["foreign", "role", "epoch", "state", "missing_summary"])
async def test_private_tools_refuse_wrong_participants_states_and_incomplete_submissions(
    service, case
):
    card = await started(service)
    context = service.tool_context(card)
    arguments = {"branch": "topic", "base": "main", "summary": "Ready", "checks": "Passed"}
    handler = service.execution.submit_work
    if case in {"foreign", "epoch"}:
        context.execution_owner = replace(
            context.execution_owner,
            **({"extension": "swarm"} if case == "foreign" else {"epoch": "older"}),
        )
    elif case == "role":
        handler = service.execution.submit_review
        arguments = {"verdict": "approve", "recommendation": "Not a Reviewer"}
    elif case == "state":
        card = await service.store.change(
            card["id"], {"state": "waiting"}, actor="factory", event="paused"
        )
    else:
        arguments.pop("summary")
    revision = card["revision"]
    async with service.lock:
        with pytest.raises(FactoryError):
            await handler(context, arguments)
    assert (await service.card())["revision"] == revision
    await service.execution.quiesce()
    await service.flush()


@pytest.mark.asyncio
async def test_blocked_worker_waits_for_user_and_answer_continues_same_session(service):
    card = await started(service)
    worker_address = card["runtime"]["participants"]["worker"]
    async with service.lock:
        await service.execution.report_blocked(
            service.tool_context(card), {"question": "Which behavior?"}
        )
    await service.finish()
    card = await service.card()
    assert (card["state"], card["waiting_reason"]) == ("waiting", "blocked")
    assert card["runtime"]["question"] == "Which behavior?"
    async with service.lock:
        await service.execution.send_back(card, "Use the first behavior")
    await service.flush()
    card = await service.card()
    assert card["runtime"]["participants"]["worker"] == worker_address
    assert service.host.temporary_agents.admissions[-1][1].kind == "continuation"
    await service.execution.quiesce()


@pytest.mark.asyncio
async def test_automatic_owned_continuation_adopts_canonical_run_and_preserves_pending_note(
    service,
):
    card = await started(service)
    groups = service.host.temporary_agents
    previous = groups.runs[card["runtime"]["current_run"]]
    await service.execution.run_finished(groups.context(previous), outcome="success")
    previous.finish()
    card = await service.card()
    pending = card["runtime"]["pending"]
    handle = await groups.open_group(card["group_id"])
    admission = await groups.start(
        handle, "worker", TemporaryRunInput("continuation", "Background result", "automatic")
    )
    automatic = groups.runs[admission.run_id]
    # A delegated child under the same temporary Agent never becomes its Worker.
    await service.execution.before_request(replace(groups.context(automatic), session_id="child"))
    assert (await service.card())["runtime"]["current_run"] == previous.id
    delivery = await service.execution.before_request(groups.context(automatic))
    assert delivery is not None and delivery.delivery_id == pending["id"]
    assert delivery.entries == (pending["content"],)
    await service.flush()
    card = await service.card()
    assert card["runtime"]["current_run"] == automatic.id and "pending" not in card["runtime"]
    async with service.lock:
        await service.execution.report_blocked(
            service.tool_context(card), {"question": "Decision needed"}
        )
    # Waiting is not permission to let a late completion keep the participant or
    # its children acting with ordinary Tools while group cancellation catches up.
    for context in (
        groups.context(automatic),
        replace(groups.context(automatic), session_id="child"),
    ):
        with pytest.raises(FactoryError) as refusal:
            await service.execution.before_request(context)
        assert refusal.value.code == "wrong_state"
    await service.finish()


@pytest.mark.asyncio
async def test_quiesce_drains_without_classifying_shutdown_as_worker_failure(service):
    await started(service)
    revision = (await service.card())["revision"]
    await service.execution.quiesce("reload")
    await service.flush()
    card = await service.card()
    assert card["state"] == "working" and card["revision"] == revision
    assert not service.host.temporary_agents.open[card["group_id"]]
