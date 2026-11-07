"""Factory execution contracts over deterministic canonical-group stand-ins."""

from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents.temporary_execution import TemporaryRunInput
from core.extensions import (
    ExtensionAPI,
    PreparedSessionDelivery,
    SessionRequestContext,
)
from core.extensions.extensions import ExtensionDeclarations
from core.runs import ActiveRunError, RunExecutionOwner
from core.sessions import DeliveryReceipt, SessionAddress
from resources.extensions.factory import _execution
from resources.extensions.factory._execution import FactoryExecution
from resources.extensions.factory._registration import register
from resources.extensions.factory.store import FactoryError

type Json = dict[str, Any]
_HEAD = "a" * 40
_NEW = "b" * 40
_OUTPUT_DEFAULTS: Json = dict.fromkeys(
    ("summary", "result", "checks", "branch", "base", "base_sha", "head_sha", "target"),
    "",
) | {"findings": []}


def step(
    step_id: str,
    agent: str = "builder",
    model: str = "",
    *,
    after: list[str],
    source: str = "agent",
    task: str = "Do the step.",
    returns_to: str = "",
    rounds: int = 1,
    approval: bool = False,
) -> Json:
    return {
        "id": step_id,
        "name": step_id.title(),
        "source": source,
        "agent": agent,
        "model": model,
        "effort": "",
        "task": task,
        "after": after,
        "approval": approval,
        "returns_to": returns_to,
        "rounds": rounds,
    }


def implement_steps(
    rounds: int = 1, *, after: list[str] | None = None, **fields: Any
) -> list[Json]:
    """Implement then Review; the reviewer is a Team member, the implementer an Agent."""
    return [
        step("implement", "builder", "provider/worker", after=after or [], **fields),
        step(
            "review",
            "reviewer",
            "other/reviewer",
            after=["implement"],
            source="team",
            returns_to="implement",
            rounds=rounds,
        ),
    ]


def implement_workflow(rounds: int = 1) -> Json:
    return {
        "id": "implement",
        "name": "Implement",
        "description": "",
        "steps": implement_steps(rounds),
    }


class Store:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.cards: dict[str, Json] = {}
        self.histories: dict[str, Json] = {}
        self.setup: Json = {
            "project_id": "repo",
            "workflows": [implement_workflow()],
            "default_workflow": "implement",
            "turn_budget": 2,
            "base_branch": "main",
            "integration_check": "merged",
        }

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
            "turns": 0,
            "workflow_id": "",
            "revision": 1,
            "branch": "topic",
            "base": "main",
            "base_sha": "0" * 40,
            "head_sha": _HEAD,
            "summary": "Submitted implementation",
            "checks": "Checks passed",
            "recommendation": "",
            "integration_reference": "",
            "runtime": {"cwd": str(self.directory)},
        }
        self.cards[card_id] = deepcopy(card)
        self.histories[card_id] = {"events": [], "comments": [], "outputs": []}
        return card

    async def get_setup(self, project_id: str) -> Json:
        return deepcopy(self.setup)

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

    async def change(self, card_id, patch, *, actor, event, output=None, comment=None, **_extra):
        card = self.cards[card_id]
        card.update(deepcopy(patch))
        card["revision"] += 1
        history = self.histories[card_id]
        history["events"].append({"event": event, "actor": actor})
        if output is not None:
            history["outputs"].append(
                {**_OUTPUT_DEFAULTS, "id": len(history["outputs"]) + 1, **deepcopy(output)}
            )
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
        self.start_sequence = 0

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
        self.inputs: dict[tuple[SessionAddress, str], Run] = {}
        self.receipts: dict[tuple[SessionAddress, str, str], DeliveryReceipt] = {}
        self.carriers: list[tuple[str, str]] = []

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
        input_id = (
            f"initial:{binding.generation_id}"
            if input.kind == "initial"
            else f"continuation:{binding.generation_id}:{input.request_id}"
        )
        receipt_key = (binding.address, binding.generation_id, input_id)
        digest = hashlib.sha256(input.content.encode()).hexdigest()
        receipt = self.receipts.get(receipt_key)
        if receipt is not None and receipt.content_hash != digest:
            raise FactoryError("receipt_conflict", "Input identity has different content")
        previous = self.inputs.get((binding.address, input_id))
        if previous is not None:
            return SimpleNamespace(run_id=previous.id, status=previous.status.value, existing=True)
        if any(
            run.binding.address == binding.address and run.status.value == "running"
            for run in self.runs.values()
        ):
            raise ActiveRunError("Session is busy")
        if receipt is None:
            self.receipts[receipt_key] = DeliveryReceipt(
                input_id,
                digest,
                input.kind,
                {
                    "kind": "user" if input.kind == "initial" else "note",
                    "sequence": len(self.carriers),
                },
            )
            self.carriers.append((input_id, input.content))
        owner = RunExecutionOwner(
            "factory", handle.group_id, participant, binding.generation_id, handle.epoch
        )
        run = Run(f"run-{len(self.runs) + 1}", owner, binding)
        run.start_sequence = len(self.runs) + 1
        self.runs[run.id] = run
        self.inputs[(binding.address, input_id)] = run
        self.admissions.append((participant, input))
        return SimpleNamespace(run_id=run.id, status="running", existing=False)

    async def owned_runs(self, group, ids):
        return {
            run_id: SimpleNamespace(
                run=None if self.recovered else self.runs[run_id],
                record=SimpleNamespace(
                    terminal_status=self.runs[run_id].status.value,
                    owner=self.runs[run_id].execution_owner,
                    address=self.runs[run_id].binding.address,
                    generation_id=self.runs[run_id].binding.generation_id,
                    start_sequence=self.runs[run_id].start_sequence,
                ),
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

    async def delivery_receipt(self, address, generation, receipt_id):
        return self.receipts.get((address, generation, receipt_id))

    async def append_delivery(
        self, context: SessionRequestContext, delivery: PreparedSessionDelivery, *, fail=False
    ):
        if fail:
            raise OSError("Canonical append failed before its receipt committed")
        binding = context.binding
        key = (binding.address, binding.generation_id, delivery.delivery_id)
        if key not in self.receipts:
            self.receipts[key] = DeliveryReceipt(
                delivery.delivery_id,
                delivery.content_hash,
                delivery.effect_kind,
                {"kind": "note", "sequence": len(self.carriers)},
            )
            self.carriers.append((delivery.delivery_id, "\n\n".join(delivery.entries)))

    async def quiesce(self, reason):
        for group in tuple(self.open):
            await self.close_group(group)


class Service:
    def __init__(self, directory: Path) -> None:
        self.lock = asyncio.Lock()
        self.store = Store(directory)
        self.tasks: list[asyncio.Task[Any]] = []
        self.changes: list[tuple[str, str | None]] = []
        self.publications: list[tuple[str, list[str], int, bool]] = []
        groups = Groups(self)
        self.host = SimpleNamespace(
            temporary_agents=groups,
            publish_change=lambda resource, ids, revision: self.publications.append(
                (resource, ids, revision, groups.open[ids[0]])
            ),
        )
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
    target_value = "c" * 40
    merge_value = "e" * 40
    ancestor = True
    merged = False
    extra_paths: tuple[str, ...] = ()
    merge_paths: tuple[str, ...] = ()
    unknown_base = False

    def __init__(self, _cwd):
        pass

    async def submission(self, branch, base, *, head_sha=None, base_sha=None):
        if self.merged and base_sha is None:
            raise FactoryError("no_commits", "Head is already on the mutable base")
        return {
            "branch": branch,
            "base": base,
            "base_sha": base_sha or "0" * 40,
            "head_sha": head_sha or self.head_value or _HEAD,
        }

    async def head(self, _branch, **_kwargs):
        return self.head_value

    async def is_ancestor(self, _head, _base):
        return self.ancestor

    async def base(self, _base):
        if self.extra_paths or self.merge_paths:
            return self.merge_value
        return self.head_value if self.merged else self.target_value

    async def integration(
        self,
        _branch,
        _base,
        *,
        reviewed_base,
        reviewed_head,
        expected_branch_head=None,
        accepted_base=None,
        base_commit=None,
        verify_merged=True,
        reported_reference=None,
    ):
        moved = self.head_value is not None and self.head_value != (
            expected_branch_head or reviewed_head
        )
        merge_changes = (
            verify_merged
            and self.ancestor
            and bool(self.extra_paths or self.merge_paths)
            and reviewed_head != self.merge_value
        )
        review = moved or merge_changes
        landed = self.merged if moved else self.ancestor and verify_merged
        head = self.merge_value if merge_changes else self.head_value if moved else reviewed_head
        baseline = base_commit or accepted_base or self.target_value if review else reviewed_base
        if self.unknown_base and landed and moved and base_commit is None:
            baseline = ""
        return {
            "branch_head_sha": self.head_value,
            "target_sha": await self.base(_base) if verify_merged or moved else "",
            "landed": landed,
            "review_required": review,
            "base_sha": baseline,
            "head_sha": head,
            "landing_sha": head if landed else "",
            "extra_paths": list(self.extra_paths) if merge_changes else [],
            "merge_paths": list(self.merge_paths) if merge_changes else [],
            "base_source": "accepted_base" if review else "",
        }


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setattr(_execution, "GitRepository", Git)
    monkeypatch.setattr(Git, "head_value", _HEAD)
    monkeypatch.setattr(Git, "ancestor", True)
    monkeypatch.setattr(Git, "merged", False)
    monkeypatch.setattr(Git, "extra_paths", ())
    monkeypatch.setattr(Git, "merge_paths", ())
    monkeypatch.setattr(Git, "unknown_base", False)
    return Service(tmp_path)


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


async def reviewed(service):
    card = await submitted(service)
    async with service.lock:
        await service.execution.submit_work(
            service.tool_context(card), {"summary": "Idea and implementation are sound."}
        )
    await service.finish()
    return await service.card()


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_failure", [False, True])
async def test_worker_reviewer_acceptance_integration_and_archival(
    service, monkeypatch, archive_failure
):
    card = await started(service)
    assert card["state"] == "working" and card["turns"] == 1
    groups = service.host.temporary_agents
    assert groups.configs["implement"].model_override == "provider/worker"
    assert groups.configs["implement"].identity_profile == "builder"
    assert "factory_request_changes" in groups.configs["implement"].tool_access.denied
    card = await reviewed(service)
    assert card["state"] == "waiting" and card["waiting_reason"] == "finished"
    assert service.publications[-1] == ("cards", [card["id"]], card["revision"], False)
    assert groups.configs["review-r1"].model_override == "other/reviewer"
    assert groups.configs["review-r1"].repository_profile == "reviewer"
    assert not {"edit", "factory_request_changes", "factory_submit_work"} & set(
        groups.configs["review-r1"].tool_access.denied
    )
    async with service.lock:
        await service.execution.accept(card)
    await service.flush()
    card = await service.card()
    assert card["state"] == "integrating"
    assert groups.admissions[-1][1].kind == "continuation"
    assert f'"commit": "{Git.target_value}"' in groups.admissions[-1][1].content
    monkeypatch.setattr(Git, "head_value", None)  # Project cleanup removed branch.
    async with service.lock:
        result = await service.execution.report_integration(
            service.tool_context(card), {"outcome": "integrated"}
        )
    assert result["ok"]
    await service.finish()
    card = await service.card()
    assert card["state"] == "done" and card["integration_reference"] == _HEAD
    assert service.publications[-1] == ("cards", [card["id"]], card["revision"], False)
    assert set(card["runtime"]["runs"]) == {"implement", "review-r1"}
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
async def test_change_requests_use_fresh_participants_and_exhaust_rounds(service):
    await started(service)
    stale_context = None
    for round_number in (1, 2):
        card = await submitted(service)
        assert card["round"] == round_number
        current_context = service.tool_context(card)
        if stale_context is not None:
            async with service.lock:
                with pytest.raises(FactoryError) as caught:
                    await service.execution.request_changes(stale_context, {"findings": ["stale"]})
            assert caught.value.code == "stale_run"
        stale_context = current_context
        async with service.lock:
            await service.execution.request_changes(
                current_context, {"findings": [f"Finding {round_number}"]}
            )
        await service.finish()
    card = await service.card()
    assert (card["state"], card["waiting_reason"]) == ("waiting", "no_agreement")
    inputs = [
        input.content
        for participant, input in service.host.temporary_agents.admissions
        if participant == "review-r2"
    ]
    assert len(inputs) == 1 and "Finding 1" in inputs[0]
    async with service.lock:
        await service.execution.send_back(card, "Reconsider both findings")
    await service.flush()
    card = await service.card()
    assert card["runtime"]["rounds"] == {} and card["runtime"]["current_participant"] == (
        "implement"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancelled"])
async def test_worker_budget_and_failure_do_not_retry_failed_runs(service, outcome):
    await started(service)
    await service.finish(outcome)
    card = await service.card()
    if outcome == "success":
        assert card["state"] == "working" and card["turns"] == 2
        await service.finish()
        card = await service.card()
        assert card["waiting_reason"] == "budget_exhausted"
    else:
        assert card["waiting_reason"] == "run_failed"
        assert len(service.host.temporary_agents.admissions) == 1


@pytest.mark.asyncio
async def test_judging_step_is_nudged_within_budget_and_retries_fresh(service):
    await started(service)
    await submitted(service)
    for _ in range(2):
        await service.finish()
    card = await service.card()
    assert card["waiting_reason"] == "budget_exhausted"
    async with service.lock:
        await service.execution.retry(card)
    await service.flush()
    admissions = service.host.temporary_agents.admissions
    assert [participant for participant, _ in admissions] == [
        "implement",
        "review-r1",
        "review-r1",
        "review-r2",
    ]
    assert "factory_request_changes" in admissions[2][1].content
    assert admissions[3][1].kind == "initial"
    await service.execution.quiesce()
    await service.flush()


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
    assert "review-r1" not in groups.configs
    await service.finish()
    assert "review-r1" in groups.configs


@pytest.mark.asyncio
async def test_discard_cancels_review_and_gives_worker_one_cleanup_turn(service):
    await started(service)
    card = await submitted(service)
    async with service.lock:
        await service.execution.discard(card)
    await service.flush()
    card = await service.card()
    assert card["state"] == "discarded" and card["runtime"]["current_participant"] == "implement"
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
    card = await reviewed(service)
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
    assert card["state"] in {"working", "integrating"}
    assert not (await service.store.history(card["id"]))["comments"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["foreign", "role", "epoch", "state", "missing_summary", "missing_checks"]
)
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
        # Only a step that can send work back requests changes.
        handler = service.execution.request_changes
        arguments = {"findings": ["Not a judging step"]}
    elif case == "state":
        card = await service.store.change(
            card["id"], {"state": "waiting"}, actor="factory", event="paused"
        )
    elif case == "missing_checks":
        arguments.pop("checks")
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
    worker_address = card["runtime"]["participants"]["implement"]
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
    assert card["runtime"]["participants"]["implement"] == worker_address
    assert service.host.temporary_agents.admissions[-1][1].kind == "continuation"
    await service.execution.quiesce()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["preflight", "carrier", "admission"])
async def test_failed_admission_retries_original_guidance_without_replaying_canonical_input(
    service, monkeypatch, boundary
):
    card = await started(service)
    groups = service.host.temporary_agents
    async with service.lock:
        await service.execution.report_blocked(
            service.tool_context(card), {"question": "Which behavior?"}
        )
    await service.finish()
    start = groups.start

    async def unavailable(handle, participant, input):
        if boundary == "carrier":
            binding = groups.bindings[(handle.group_id, participant)]
            input_id = f"continuation:{binding.generation_id}:{input.request_id}"
            groups.receipts[(binding.address, binding.generation_id, input_id)] = DeliveryReceipt(
                input_id,
                hashlib.sha256(input.content.encode()).hexdigest(),
                input.kind,
                {"kind": "note", "sequence": len(groups.carriers)},
            )
            groups.carriers.append((input_id, input.content))
        elif boundary == "admission":
            admitted = await start(handle, participant, input)
            groups.runs[admitted.run_id].finish("failed")
        raise FactoryError("launch_failed", "Admission failed at a controlled boundary")

    monkeypatch.setattr(groups, "start", unavailable)
    async with service.lock:
        queued = await service.execution.send_back(await service.card(), "unique-sendback-guidance")
    pending = queued["runtime"]["pending"]
    await service.flush()
    paused = await service.card()
    assert (paused["state"], paused["waiting_reason"]) == ("waiting", "run_failed")
    assert paused["runtime"]["pending"] == pending
    assert len(groups.admissions) == (2 if boundary == "admission" else 1)
    assert not groups.open[card["group_id"]]
    monkeypatch.setattr(groups, "start", start)
    async with service.lock:
        retried = await service.execution.retry(paused)
    assert retried["runtime"]["pending"]["id"] == pending["id"]
    await service.flush()
    card = await service.card()
    admissions = 3 if boundary == "admission" else 2
    assert len(groups.admissions) == admissions
    assert "pending" not in card["runtime"]
    assert groups.admissions[1][1].request_id == pending["id"]
    assert groups.admissions[1][1].content == pending["content"]
    assert sum("unique-sendback-guidance" in content for _, content in groups.carriers) == 1
    if boundary == "admission":
        assert groups.admissions[-1][1].request_id != pending["id"]
        assert "unique-sendback-guidance" not in groups.admissions[-1][1].content
    assert card["state"] == "working" and card["turns"] == 1
    await service.finish("error")
    assert (await service.card())["waiting_reason"] == "run_failed"
    assert len(groups.admissions) == admissions
    await service.execution.quiesce()
    await service.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_admission", [False, True])
async def test_retry_authorization_does_not_survive_crash_at_canonical_admission(
    service, monkeypatch, prior_admission
):
    card = await started(service)
    groups = service.host.temporary_agents
    async with service.lock:
        await service.execution.report_blocked(
            service.tool_context(card), {"question": "Which behavior?"}
        )
    await service.finish()
    start = groups.start

    async def unavailable(handle, participant, input):
        if prior_admission:
            admitted = await start(handle, participant, input)
            groups.runs[admitted.run_id].finish("failed")
        raise FactoryError("launch_failed", "Admission unavailable")

    monkeypatch.setattr(groups, "start", unavailable)
    async with service.lock:
        queued = await service.execution.send_back(await service.card(), "unique-sendback-guidance")
    pending = queued["runtime"]["pending"]
    await service.flush()

    async def crash(handle, participant, input):
        admitted = await start(handle, participant, input)
        groups.runs[admitted.run_id].finish("failed")
        # Simulate loss after the canonical check but before Factory saved it.
        raise asyncio.CancelledError

    monkeypatch.setattr(groups, "start", crash)
    async with service.lock:
        await service.execution.retry(await service.card())
    with pytest.raises(asyncio.CancelledError):
        await service.flush()
    assert (await service.card())["runtime"]["pending"]["id"] == pending["id"]
    assert len(groups.admissions) == 2
    monkeypatch.setattr(groups, "start", start)
    groups.recovered = True
    service.execution = FactoryExecution(service)
    await service.execution.reconcile()
    await service.flush()
    card = await service.card()
    assert (card["state"], card["waiting_reason"]) == ("waiting", "run_failed")
    assert len(groups.admissions) == 2
    assert sum("unique-sendback-guidance" in content for _, content in groups.carriers) == 1
    async with service.lock:
        await service.execution.retry(card)
    await service.flush()
    assert (await service.card())["state"] == "working"
    assert len(groups.admissions) == 3
    assert "unique-sendback-guidance" not in groups.admissions[-1][1].content
    await service.execution.quiesce()
    await service.flush()


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
        handle, "implement", TemporaryRunInput("continuation", "Background result", "automatic")
    )
    automatic = groups.runs[admission.run_id]
    # A delegated child under the same temporary Agent never becomes its Worker.
    await service.execution.before_request(replace(groups.context(automatic), session_id="child"))
    assert (await service.card())["runtime"]["current_run"] == previous.id
    delivery = await service.execution.before_request(groups.context(automatic))
    assert delivery is not None and delivery.delivery_id == pending["id"]
    assert delivery.entries == (pending["content"],)
    adopted = await service.card()
    assert adopted["runtime"]["pending"]["id"] == pending["id"]
    assert await service.execution.before_request(groups.context(automatic)) == delivery
    assert (await service.card())["turns"] == adopted["turns"]
    with pytest.raises(FactoryError) as unacknowledged:
        await service.execution.acknowledge_delivery(groups.context(automatic), delivery)
    assert unacknowledged.value.code == "delivery_unacknowledged"
    await groups.append_delivery(groups.context(automatic), delivery)
    await groups.append_delivery(groups.context(automatic), delivery)
    await service.execution.acknowledge_delivery(groups.context(automatic), delivery)
    assert await service.execution.before_request(groups.context(automatic)) is None
    revision = (await service.card())["revision"]
    await service.execution.acknowledge_delivery(groups.context(automatic), delivery)
    assert (await service.card())["revision"] == revision
    assert sum(receipt_id == delivery.delivery_id for receipt_id, _ in groups.carriers) == 1
    declarations = ExtensionDeclarations()
    register(ExtensionAPI("factory", declarations, config={}, logger=None))
    assert declarations.session_runtime is not None
    assert (
        getattr(declarations.session_runtime.acknowledge_delivery, "__func__", None)
        is FactoryExecution.acknowledge_delivery
    )
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


async def automatic_handoff(service):
    card = await started(service)
    groups = service.host.temporary_agents
    previous = groups.runs[card["runtime"]["current_run"]]
    async with service.lock:
        await service.execution.report_blocked(
            service.tool_context(card), {"question": "Which behavior?"}
        )
    previous.finish()
    async with service.lock:
        await service.execution.send_back(await service.card(), "unique-sendback-guidance")
    pending = (await service.card())["runtime"]["pending"]
    handle = await groups.open_group(card["group_id"])
    admission = await groups.start(
        handle, "implement", TemporaryRunInput("continuation", "Background result", "automatic")
    )
    automatic = groups.runs[admission.run_id]
    # The ordinary launch loses to the active automatic Run before its hook.
    await service.flush()
    return groups.context(automatic), pending


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["missing", "committed", "conflicting"])
@pytest.mark.parametrize("recovery", ["restart", "retry"])
async def test_pending_handoff_survives_append_failure_and_reconciles_receipts_before_recovery(
    service, receipt, recovery
):
    context, pending = await automatic_handoff(service)
    groups = service.host.temporary_agents
    delivery = await service.execution.before_request(context)
    assert delivery is not None and "unique-sendback-guidance" in delivery.entries[0]
    if recovery == "retry":
        await service.execution.run_finished(context, outcome="error")
        groups.runs[context.run_id].finish("failed")
        await service.flush()
        assert (await service.card())["runtime"]["pending"]["id"] == pending["id"]
    if receipt == "missing":
        with pytest.raises(OSError):
            await groups.append_delivery(context, delivery, fail=True)
        if recovery == "restart":
            assert await service.execution.before_request(context) == delivery
    else:
        await groups.append_delivery(context, delivery)
        if receipt == "conflicting":
            key = (context.binding.address, context.binding.generation_id, delivery.delivery_id)
            groups.receipts[key] = replace(groups.receipts[key], content_hash="wrong")

    async def recover():
        if recovery == "restart":
            groups.runs[context.run_id].finish("interrupted")
            groups.recovered = True
            service.execution = FactoryExecution(service)
            await service.execution.reconcile()
        else:
            async with service.lock:
                await service.execution.retry(await service.card())

    if receipt == "conflicting":
        with pytest.raises(FactoryError) as conflict:
            await recover()
        assert conflict.value.code == "receipt_conflict"
        assert (await service.card())["runtime"]["pending"]["id"] == pending["id"]
        assert len(groups.admissions) == 2
    else:
        await recover()
        await service.flush()
        latest = groups.admissions[-1][1]
        assert ("unique-sendback-guidance" in latest.content) is (receipt == "missing")
        assert (latest.request_id == pending["id"]) is (receipt == "missing")
        assert sum("unique-sendback-guidance" in content for _, content in groups.carriers) == 1
        assert "pending" not in (await service.card())["runtime"]
    await service.execution.quiesce()
    await service.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", ["before_admission", "admitted_before_save", "admitted_after_save"]
)
async def test_restart_delivers_never_admitted_cleanup_once_and_only_closes_admitted_cleanup(
    service, monkeypatch, boundary
):
    card = await started(service)
    groups = service.host.temporary_agents
    groups.runs[card["runtime"]["current_run"]].finish("interrupted")
    monkeypatch.setattr(service.execution, "_launch_later", lambda _card: None)
    async with service.lock:
        await service.execution.discard(card)
    pending = (await service.card())["runtime"]["pending"]
    if boundary == "admitted_before_save":
        handle = await groups.open_group(card["group_id"])
        admitted = await groups.start(
            handle,
            "implement",
            TemporaryRunInput(pending["kind"], pending["content"], pending["id"]),
        )
        groups.runs[admitted.run_id].finish("interrupted")
    elif boundary == "admitted_after_save":
        await service.execution._launch(card["id"], pending["id"])
        groups.runs[(await service.card())["runtime"]["current_run"]].finish("interrupted")
    groups.recovered = True
    service.execution = FactoryExecution(service)
    await service.execution.reconcile()
    await service.flush()
    assert len(groups.admissions) == 2
    assert "pending" not in (await service.card())["runtime"]
    if boundary == "before_admission":
        await service.finish()
    assert not groups.open[card["group_id"]]
    service.execution = FactoryExecution(service)
    await service.execution.reconcile()
    await service.flush()
    assert len(groups.admissions) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["error", "sibling", "participant", "epoch", "generation", "unowned", "older"]
)
@pytest.mark.parametrize("boundary", ["before_request", "before_receipt", "committed"])
async def test_root_failure_pauses_exact_handoff_without_replaying_it(
    service, monkeypatch, case, boundary
):
    context, pending = await automatic_handoff(service)
    groups = service.host.temporary_agents
    if boundary != "before_request":
        delivery = await service.execution.before_request(context)
        assert delivery is not None
        if boundary == "before_receipt":
            with pytest.raises(OSError):
                await groups.append_delivery(context, delivery, fail=True)
        else:
            await groups.append_delivery(context, delivery)
    before = await service.card()
    original = context
    if case == "sibling":
        context = replace(context, session_id="child-session")
    elif case in {"participant", "epoch", "generation"}:
        field = {"participant": "participant_id", "epoch": "epoch", "generation": "generation_id"}[
            case
        ]
        context = replace(
            context, execution_owner=replace(context.execution_owner, **{field: "stale"})
        )
    elif case == "unowned":

        async def missing(_group, _runs):
            return {}

        monkeypatch.setattr(groups, "owned_runs", missing)
    elif case == "older":
        older = Run("older-root", context.execution_owner, context.binding)
        older.finish("failed")
        groups.runs[older.id] = older
        context = groups.context(older)
    await service.execution.run_finished(context, outcome="error")
    groups.runs[original.run_id].finish("failed")
    await service.flush()
    card = await service.card()
    assert len(groups.admissions) == 2
    if case == "error" and boundary == "committed":
        assert "pending" not in card["runtime"]
    else:
        assert card["runtime"]["pending"]["id"] == pending["id"]
    if case == "error":
        assert (card["state"], card["waiting_reason"]) == ("waiting", "run_failed")
        assert card["runtime"]["current_run"] == original.run_id
        assert not groups.open[card["group_id"]]
        await service.execution.run_finished(context, outcome="error")
        await service.flush()
        assert len(groups.admissions) == 2
        async with service.lock:
            await service.execution.retry(await service.card())
        await service.flush()
        assert len(groups.admissions) == 3
        assert ("unique-sendback-guidance" in groups.admissions[-1][1].content) is (
            boundary != "committed"
        )
        assert (groups.admissions[-1][1].request_id == pending["id"]) is (boundary != "committed")
        assert sum("unique-sendback-guidance" in content for _, content in groups.carriers) == 1
    else:
        assert card == before
    await service.execution.quiesce()
    await service.flush()


@pytest.mark.asyncio
async def test_quiesce_drains_without_classifying_shutdown_as_worker_failure(service):
    await started(service)
    revision = (await service.card())["revision"]
    await service.execution.quiesce("reload")
    await service.flush()
    card = await service.card()
    assert card["state"] == "working" and card["revision"] == revision
    assert not service.host.temporary_agents.open[card["group_id"]]


async def hand_in(service, result: str):
    card = await service.card()
    async with service.lock:
        outcome = await service.execution.submit_work(
            service.tool_context(card), {"summary": f"Wrote {result}", "result": result}
        )
    assert outcome["ok"]
    await service.finish()
    return await service.card()


async def send_back_work(service, finding: str):
    card = await service.card()
    async with service.lock:
        outcome = await service.execution.request_changes(
            service.tool_context(card), {"findings": [finding]}
        )
    assert outcome["ok"]
    await service.finish()
    return await service.card()


@pytest.mark.asyncio
async def test_parallel_steps_feed_later_steps_and_rework_reruns_what_follows(service):
    service.store.setup["workflows"] = [
        {
            "id": "drafts",
            "name": "Drafts",
            "description": "",
            "steps": [
                step("draft1", "builder", "provider/worker", after=[], task="Draft a plan."),
                step("draft2", "planner", after=[]),
                step("merge", after=["draft1", "draft2"]),
                step(
                    "review",
                    "reviewer",
                    after=["merge"],
                    source="team",
                    returns_to="draft2",
                    rounds=2,
                ),
            ],
        }
    ]
    groups = service.host.temporary_agents
    card = await started(service)
    assert card["workflow_id"] == "drafts"
    config = groups.configs["draft1"]
    assert "Draft a plan." in config.instructions and config.identity_profile == "builder"
    card = await hand_in(service, "Plan A")
    participant, draft = groups.admissions[-1]
    # Steps without a common predecessor work independently of each other.
    assert participant == "draft2" and "Plan A" not in draft.content
    await hand_in(service, "Plan B")
    participant, merge = groups.admissions[-1]
    assert participant == "merge" and "Plan A" in merge.content and "Plan B" in merge.content
    await hand_in(service, "Merged plan")
    assert groups.admissions[-1][0] == "review-r1"
    card = await send_back_work(service, "Plan B misses a rollback")
    participant, rework = groups.admissions[-1]
    assert (card["state"], participant) == ("working", "draft2")
    assert rework.kind == "continuation" and "misses a rollback" in rework.content
    await hand_in(service, "Plan B2")
    participant, refresh = groups.admissions[-1]
    assert participant == "merge" and refresh.kind == "continuation"
    assert "Plan B2" in refresh.content
    await hand_in(service, "Merged plan 2")
    participant, review = groups.admissions[-1]
    assert participant == "review-r2" and review.kind == "initial"
    assert "Plan B2" in review.content and "misses a rollback" in review.content
    card = await hand_in(service, "The drafts now cover rollback.")
    assert (card["state"], card["waiting_reason"]) == ("waiting", "finished")
    async with service.lock:
        await service.execution.accept(card)
    await service.flush()
    card = await service.card()
    assert card["state"] == "done" and card["integration_reference"] == ""
    outputs = (await service.store.history(card["id"]))["outputs"]
    assert [(item["step"], item["kind"], item["pass"]) for item in outputs] == [
        ("draft1", "work", 1),
        ("draft2", "work", 1),
        ("merge", "work", 1),
        ("review", "changes", 1),
        ("draft2", "work", 2),
        ("merge", "work", 2),
        ("review", "work", 2),
    ]
    assert outputs[3]["target"] == "draft2"


@pytest.mark.asyncio
async def test_approval_waits_before_its_step_and_send_back_targets_the_chosen_step(service):
    service.store.setup["workflows"] = [
        {
            "id": "planimpl",
            "name": "Plan, then implement",
            "description": "",
            "steps": [
                step("plan", after=[]),
                step(
                    "planreview",
                    "reviewer",
                    after=["plan"],
                    source="team",
                    returns_to="plan",
                    rounds=2,
                ),
                *implement_steps(after=["planreview"], approval=True),
            ],
        }
    ]
    groups = service.host.temporary_agents
    await started(service)
    await hand_in(service, "First plan")
    card = await hand_in(service, "Sound plan.")
    assert (card["state"], card["waiting_reason"]) == ("waiting", "checkpoint")
    assert card["runtime"]["step"] == "implement"
    async with service.lock:
        await service.execution.send_back(card, "Add a migration step", "plan")
    await service.flush()
    assert groups.admissions[-1][0] == "plan"
    assert "Add a migration step" in groups.admissions[-1][1].content
    await hand_in(service, "Second plan")
    card = await hand_in(service, "Sound plan with migration.")
    assert card["waiting_reason"] == "checkpoint"
    async with service.lock:
        await service.execution.accept(card)
    await service.flush()
    participant, implement = groups.admissions[-1]
    assert participant == "implement" and implement.kind == "initial"
    assert "Second plan" in implement.content and "First plan" not in implement.content
    card = await submitted(service)
    assert card["runtime"]["current_participant"] == "review-r1"


def stage_workflow() -> Json:
    """A Workflow snapshot as the stage-based Factory stored it."""

    def lane(agent: str, source: str = "agent") -> Json:
        return {"source": source, "agent": agent, "model": "", "effort": ""}

    return {
        "id": "implement",
        "name": "Implement",
        "description": "",
        "stages": [
            {
                "id": "implement",
                "name": "Implement",
                "kind": "work",
                "output": "changes",
                "approval": False,
                "instructions": "",
                "lanes": [lane("builder")],
            },
            {
                "id": "review",
                "name": "Review",
                "kind": "review",
                "target": "implement",
                "rounds": 2,
                "approval": False,
                "instructions": "",
                "lanes": [lane("reviewer", "team")],
            },
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["fresh", "submitted", "stages", "unstarted"])
async def test_cards_from_earlier_factory_versions_restart_or_wait_with_their_worktree_kept(
    service, case
):
    """Role- and stage-based Cards and Cards whose start failed never lose their worktree."""
    groups = service.host.temporary_agents
    card = service.store.add()
    runtime: Json = {"cwd": card["runtime"]["cwd"]}
    patch: Json = {"state": "waiting", "waiting_reason": "run_failed"}
    if case != "unstarted":
        old = ["implement-1", "review-1-r1"] if case == "stages" else ["worker"]
        participants = {}
        for participant in old:
            binding = await groups.create(card["group_id"], participant, None, project_id="repo")
            participants[participant] = {"generation_id": binding.generation_id}
        runtime["participants"] = participants
        if case == "stages":
            runtime["workflow"] = stage_workflow()
        else:
            runtime["setup"] = {"review_rounds": 2}
        patch = (
            {"state": "working"}
            if case == "fresh"
            else {"state": "waiting", "waiting_reason": "finished", "round": 1}
        )
    card = await service.store.change(
        card["id"], {**patch, "runtime": runtime}, actor="factory", event="legacy"
    )
    service.execution = FactoryExecution(service)
    await service.execution.reconcile()
    card = await service.card()
    if case == "fresh":
        assert card["state"] == "queued"
        assert card["runtime"] == {
            "cwd": runtime["cwd"],
            "participants": runtime["participants"],
            "legacy_cleanup": ["worker"],
        }
        return
    if case == "unstarted":
        assert card["state"] == "waiting"
        async with service.lock:
            await service.execution.retry(card)
        assert (await service.card())["state"] == "queued"
        return
    assert (card["state"], card["waiting_reason"]) == ("waiting", "finished")
    steps = card["runtime"]["workflow"]["steps"]
    assert [item["id"] for item in steps] == ["implement", "review"]
    assert card["runtime"]["note"]
    if case == "stages":
        async with service.lock:
            await service.execution.send_back(card, "Rework the error handling")
        await service.flush()
        participant, rework = groups.admissions[-1]
        assert participant == "implement" and rework.kind == "initial"
        assert "Rework the error handling" in rework.content
        return
    async with service.lock:
        await service.execution.accept(card)
    await service.flush()
    participant, integrate = groups.admissions[-1]
    assert (await service.card())["state"] == "integrating"
    assert participant == "implement" and integrate.kind == "initial"
    assert f"The user accepted commit {_HEAD}" in integrate.content
    async with service.lock:
        await service.execution.discard(await service.card())
    await service.flush()
    assert groups.admissions[-1][0] == "worker"
    assert (await service.card())["state"] == "discarded"
