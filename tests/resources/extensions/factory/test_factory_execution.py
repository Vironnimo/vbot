"""Factory execution contracts over deterministic canonical-group stand-ins."""

from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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
from resources.extensions.factory._execution import FactoryExecution
from resources.extensions.factory._management import FactoryManagement
from resources.extensions.factory._registration import register
from resources.extensions.factory.extension import FactoryExtension
from resources.extensions.factory.store import FINAL_STATES, FactoryError, _upgraded_runtime

type Json = dict[str, Any]
CARD = "fct_card"
_OUTPUT_DEFAULTS: Json = dict.fromkeys(("summary", "result", "target"), "") | {"findings": []}


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
        "kind": "agent",
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


def child_step(step_id: str, workflow: str, *, after: list[str], wait: bool = True) -> Json:
    return {
        "id": step_id,
        "name": step_id.title(),
        "kind": "workflow",
        "after": after,
        "approval": False,
        "workflow": workflow,
        "wait": wait,
        "task": "Plan the change.",
    }


def workflow(workflow_id: str, steps: list[Json], **fields: Any) -> Json:
    return {
        "id": workflow_id,
        "name": workflow_id.title(),
        "description": "",
        "enabled": True,
        "project_id": "",
        "final_approval": False,
        "steps": steps,
        "triggers": [],
        **fields,
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


class Store:
    def __init__(self) -> None:
        self.cards: dict[str, Json] = {}
        self.histories: dict[str, Json] = {}
        self.triggers: dict[str, Json] = {}
        self.workflows: dict[str, Json] = {"implement": workflow("implement", implement_steps())}

    def add(self, card_id: str = CARD, workflow_id: str = "implement", **fields: Any) -> Json:
        card: Json = {
            "id": card_id,
            "group_id": card_id,
            "project_id": "repo",
            "title": "Card",
            "description": "Objective",
            "state": "queued",
            "waiting_reason": "",
            "workflow_id": workflow_id,
            "parent_id": "",
            "revision": 1,
            "summary": "",
            "runtime": {},
            **fields,
        }
        self.cards[card_id] = deepcopy(card)
        self.histories[card_id] = {"events": [], "comments": [], "outputs": []}
        return card

    async def get_workflow(self, workflow_id: str) -> Json:
        if workflow_id not in self.workflows:
            raise FactoryError("workflow_not_found", "missing")
        return deepcopy(self.workflows[workflow_id])

    async def trigger_event(self, card_id: str) -> Json | None:
        return deepcopy(self.triggers.get(card_id))

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
        for item in output or []:
            history["outputs"].append(
                {**_OUTPUT_DEFAULTS, "id": len(history["outputs"]) + 1, **deepcopy(item)}
            )
        if comment is not None:
            history["comments"].append({"author": actor, "text": comment})
        return deepcopy(card)

    async def delete_card(self, card_id):
        self.cards.pop(card_id, None)

    async def check_card_deletion(self, card_id):
        pass


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
                generation_id=f"gen-{group}-{participant}",
                address=SessionAddress(
                    project_id, f"tmp-{group}-{participant}", f"session-{group}-{participant}"
                ),
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
                await self.service.execution.sessions.run_finished(
                    self.context(run), outcome="cancelled"
                )
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
    """The FactoryExtension seams execution uses, over in-memory records."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.lock = asyncio.Lock()
        self.store = Store()
        self.tasks: list[asyncio.Task[Any]] = []
        self.publications: list[tuple[str, list[str], int]] = []
        self.children = 0
        self.wip_limit = 100
        groups = Groups(self)

        async def usage(group_id, run_ids):
            return {}

        self.host = SimpleNamespace(
            temporary_agents=groups,
            execution_usage=usage,
            publish_change=lambda resource, ids, revision: self.publications.append(
                (resource, ids, revision)
            ),
        )
        self.execution = FactoryExecution(self)

    def records(self) -> Store:
        return self.store

    def limits(self) -> tuple[int, int]:
        return self.wip_limit, 0

    @property
    def groups(self) -> Groups:
        return cast(Groups, self.host.temporary_agents)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def changed(self, card, previous=None, **_extra):
        moved = previous is None or previous["state"] != card["state"]
        if moved and card["state"] in FINAL_STATES and card.get("parent_id"):
            self.spawn(self.execution.child_finished(card["id"]))

    async def workflow_snapshot(self, workflow_id: str) -> Json | None:
        found = self.store.workflows.get(workflow_id)
        return deepcopy(found) if found is not None else None

    async def resolve_card_project(self, card: Json, workflow: Json) -> tuple[Json, str]:
        project_id = workflow["project_id"] or card["project_id"]
        if project_id == "missing":
            return {}, "Project 'missing' does not exist."
        return {"id": project_id, "cwd": str(self.directory)}, ""

    async def create_child(self, card: Json, step: Json, earlier: list[Json]) -> Json:
        if step["workflow"] not in self.store.workflows:
            raise FactoryError("workflow_not_found", f"Workflow {step['workflow']!r} is gone.")
        self.children += 1
        child = self.store.add(
            f"fct_child{self.children}",
            step["workflow"],
            parent_id=card["id"],
            project_id=card["project_id"],
            title=f"{card['title']} · {step['name']}",
            description="\n\n".join(item["result"] for item in earlier),
        )
        return {**child, "workflow_name": self.store.workflows[step["workflow"]]["name"]}

    async def discard_child(self, child_id: str) -> None:
        child = await self.store.get_card(child_id)
        if child["state"] not in FINAL_STATES:
            await self.execution.discard(child)

    async def flush(self):
        # Explicitly remove every settled snapshot: gather(done) need not yield.
        while self.tasks:
            current, self.tasks = self.tasks, []
            await asyncio.gather(*current)

    async def card(self, card_id: str = CARD) -> Json:
        return cast(Json, await self.store.get_card(card_id))

    def state(self, card: Json, step_id: str) -> Json:
        return cast(Json, card["runtime"]["steps"][step_id])

    async def run(self, step_id: str, card_id: str = CARD) -> Run:
        card = await self.card(card_id)
        return self.groups.runs[self.state(card, step_id)["run"]]

    async def context(self, step_id: str, card_id: str = CARD) -> SessionRequestContext:
        return cast(SessionRequestContext, self.groups.context(await self.run(step_id, card_id)))

    async def tool_context(self, step_id: str, card_id: str = CARD):
        run = await self.run(step_id, card_id)
        return SimpleNamespace(
            execution_owner=run.execution_owner,
            run_id=run.id,
            request_turn_end_hook=True,
            request_turn_end=lambda: None,
        )

    async def finish(self, step_id: str, outcome="success", card_id: str = CARD):
        run = await self.run(step_id, card_id)
        # Mirrors the real contract: owner callback before terminal publication.
        await self.execution.sessions.run_finished(self.groups.context(run), outcome=outcome)
        run.finish({"success": "completed", "error": "failed"}.get(outcome, outcome))
        await self.flush()


@pytest.fixture
def service(tmp_path):
    return Service(tmp_path)


@pytest.mark.asyncio
async def test_step_hand_ins_and_rework_commit_through_the_real_store(
    service, factory_store, monkeypatch
):
    definition = workflow("implement", implement_steps())
    await factory_store.save_workflow(definition, expected_revision=0)
    card = await factory_store.create_card(
        "repo", "Objective", "Scope", origin="operator", workflow_id="implement"
    )
    monkeypatch.setattr(service, "store", factory_store)
    monkeypatch.setattr(service, "workflow_snapshot", factory_store.get_workflow)
    async with service.lock:
        await service.execution.start(card)
    await service.flush()
    card = await hand_in(service, "implement", "First result", card_id=card["id"])
    assert service.state(card, "review")["status"] == "running"
    async with service.lock:
        await service.execution.request_changes(
            await service.tool_context("review", card["id"]), {"findings": ["Fix the edge case"]}
        )
    await service.finish("review", card_id=card["id"])
    await hand_in(service, "implement", "Corrected result", card_id=card["id"])
    card = await hand_in(service, "review", "Accepted", card_id=card["id"])
    assert card["state"] == "done"
    history = await factory_store.history(card["id"])
    assert [item["kind"] for item in history["outputs"]] == ["work", "changes", "work", "work"]


async def started(service: Service, card_id: str = CARD, workflow_id: str = "implement") -> Json:
    if card_id not in service.store.cards:
        service.store.add(card_id, workflow_id)
    async with service.lock:
        await service.execution.start(await service.card(card_id))
    await service.flush()
    return await service.card(card_id)


async def hand_in(service: Service, step_id: str, result: str = "", card_id: str = CARD) -> Json:
    context = await service.tool_context(step_id, card_id)
    async with service.lock:
        outcome = await service.execution.submit_work(
            context, {"summary": f"Wrote {result or step_id}", "result": result}
        )
    assert outcome["ok"]
    await service.finish(step_id, card_id=card_id)
    return await service.card(card_id)


async def send_back_work(service: Service, step_id: str, finding: str) -> Json:
    context = await service.tool_context(step_id)
    async with service.lock:
        outcome = await service.execution.request_changes(context, {"findings": [finding]})
    assert outcome["ok"]
    await service.finish(step_id)
    return await service.card()


async def reviewed(service: Service) -> Json:
    await hand_in(service, "implement", "Implementation on branch topic")
    return await hand_in(service, "review", "Idea and implementation are sound.")


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_failure", [False, True])
async def test_steps_run_until_accepted_and_deletion_archives_every_group(
    service, monkeypatch, archive_failure
):
    service.store.workflows["implement"]["final_approval"] = True
    card = await started(service)
    assert (card["state"], card["project_id"]) == ("working", "repo")
    assert service.state(card, "implement")["status"] == "running"
    assert service.state(card, "review")["status"] == "pending"
    groups = service.groups
    assert groups.configs["implement"].model_override == "provider/worker"
    assert groups.configs["implement"].identity_profile == "builder"
    assert "factory_request_changes" in groups.configs["implement"].tool_access.denied
    # Saving the Workflow changes later Cards, never this Card's running snapshot.
    service.store.workflows["implement"]["steps"][1]["model"] = "updated/reviewer"
    service.store.workflows["implement"]["steps"].append(step("newstep", after=["review"]))
    card = await reviewed(service)
    assert (card["state"], card["waiting_reason"]) == ("waiting", "finished")
    assert groups.configs["review-r1"].model_override == "other/reviewer"
    assert groups.configs["review-r1"].repository_profile == "reviewer"
    assert not {"edit", "factory_request_changes", "factory_submit_work"} & set(
        groups.configs["review-r1"].tool_access.denied
    )
    assert {group for group, _ in groups.bindings} == {f"{CARD}.implement", f"{CARD}.review"}
    admissions = len(groups.admissions)
    async with service.lock:
        await service.execution.accept(card)
    await service.flush()
    card = await service.card()
    assert card["state"] == "done" and len(groups.admissions) == admissions
    assert service.publications[-1][:2] == ("cards", [CARD])
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
    assert not service.store.cards
    # The Card group of an earlier Factory is archived with the step groups.
    assert groups.archived == [CARD, f"{CARD}.implement", f"{CARD}.review"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["workflow", "project"])
async def test_card_without_workflow_or_project_waits_and_retry_queues_it(service, missing):
    if missing == "workflow":
        service.store.add(workflow_id="plan")
    else:
        service.store.workflows["implement"]["project_id"] = "missing"
        service.store.add()
    async with service.lock:
        paused = await service.execution.start(await service.card())
    await service.flush()
    reason = "workflow_missing" if missing == "workflow" else "project_unresolved"
    assert (paused["state"], paused["waiting_reason"]) == ("waiting", reason)
    assert paused["runtime"]["blocker"]["note"]
    assert not service.groups.runs
    async with service.lock:
        with pytest.raises(FactoryError):
            await service.execution.send_back(paused, "Try again")
    if missing == "workflow":
        service.store.workflows["plan"] = workflow("plan", [step("plan", after=[])])
    else:
        service.store.workflows["implement"]["project_id"] = ""
    async with service.lock:
        queued = await service.execution.retry(paused)
        assert queued["state"] == "queued" and "blocker" not in queued["runtime"]
        await service.execution.start(queued)
    await service.flush()
    running = await service.card()
    first = "plan" if missing == "workflow" else "implement"
    assert service.state(running, first)["status"] == "running"
    assert running["runtime"]["workflow"]["id"] == running["workflow_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", ["send_back", "accept_final", "accept_next"])
async def test_change_requests_use_fresh_participants_and_exhaust_rounds(service, resolution):
    if resolution == "accept_next":
        service.store.workflows["implement"]["steps"].append(step("publish", after=["review"]))
    await started(service)
    stale_context = None
    for round_number in (1, 2):
        card = await hand_in(service, "implement", "Implementation")
        assert service.state(card, "review")["participant"] == f"review-r{round_number}"
        current_context = await service.tool_context("review")
        if stale_context is not None:
            async with service.lock:
                with pytest.raises(FactoryError) as caught:
                    await service.execution.request_changes(stale_context, {"findings": ["stale"]})
            assert caught.value.code == "stale_run"
        stale_context = current_context
        card = await send_back_work(service, "review", f"Finding {round_number}")
    assert (card["state"], card["waiting_reason"]) == ("waiting", "no_agreement")
    inputs = [
        input.content
        for participant, input in service.groups.admissions
        if participant == "review-r2"
    ]
    assert len(inputs) == 1 and "Finding 1" in inputs[0]
    admissions = len(service.groups.admissions)
    async with service.lock:
        if resolution == "send_back":
            await service.execution.send_back(card, "Reconsider both findings")
        else:
            await service.execution.accept(card)
    await service.flush()
    card = await service.card()
    assert not any("rounds" in item for item in card["runtime"]["steps"].values())
    if resolution == "accept_final":
        assert card["state"] == "done"
        assert len(service.groups.admissions) == admissions
    else:
        assert card["state"] == "working"
        running = "implement" if resolution == "send_back" else "publish"
        assert service.groups.admissions[-1][0] == running


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancelled"])
async def test_runs_without_hand_in_are_nudged_within_budget_and_failures_wait(service, outcome):
    service.store.workflows["implement"]["steps"][0]["limits"] = {"runs": 2}
    await started(service)
    await service.finish("implement", outcome)
    card = await service.card()
    if outcome == "success":
        assert card["state"] == "working" and len(service.groups.admissions) == 2
        await service.finish("implement")
        card = await service.card()
        assert card["waiting_reason"] == "budget_exhausted"
        assert service.state(card, "implement")["limit_hit"]["metric"] == "runs"
    else:
        assert card["waiting_reason"] == "run_failed"
        assert len(service.groups.admissions) == 1


@pytest.mark.asyncio
async def test_unconfigured_steps_have_no_run_limit(service):
    await started(service)
    for _ in range(31):
        await service.finish("implement")
    card = await service.card()
    assert card["state"] == "working"
    assert len(card["runtime"]["budget"]["runs"]) == 32
    await service.execution.sessions.quiesce()
    await service.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 10])
async def test_budget_warning_has_remaining_capacity_and_is_delivered_once(service, limit):
    service.store.workflows["implement"]["steps"][0]["limits"] = {"tool_calls": limit}
    await started(service)
    context = await service.context("implement")
    if limit == 10:
        assert await service.execution.sessions.before_request(context) is None
        for i in range(8):
            assert (
                await service.execution.sessions.before_tool_call(
                    context, tool_name="read", tool_call_id=str(i)
                )
                is None
            )
    warning = await service.execution.sessions.before_request(context)
    assert warning is not None and warning.effect_kind == "factory_budget_notice"
    assert warning == await service.execution.sessions.before_request(context)
    # The note holds a snapshot with useful remaining capacity, not a request to change settings.
    assert str(limit) in warning.entries[0]
    assert "factory_report_blocked" in warning.entries[0]
    assert "Board" not in warning.entries[0]
    await service.groups.append_delivery(context, warning)
    await service.execution.sessions.acknowledge_delivery(context, warning)
    assert await service.execution.sessions.before_request(context) is None
    assert (await service.card())["state"] == "working"
    service.execution = FactoryExecution(service)
    assert await service.execution.sessions.before_request(context) is None
    await service.execution.sessions.quiesce()
    await service.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["card", "step"])
async def test_tool_budget_reserves_parallel_calls_and_survives_retry_and_rework(service, scope):
    definition = service.store.workflows["implement"]
    (definition if scope == "card" else definition["steps"][0])["limits"] = {"tool_calls": 2}
    await started(service)
    context = await service.context("implement")
    results = await asyncio.gather(
        *[
            service.execution.sessions.before_tool_call(
                context, tool_name="read", tool_call_id=f"call-{i}"
            )
            for i in range(4)
        ]
    )
    assert sum(result is None for result in results) == 2
    card = await service.card()
    assert card["waiting_reason"] == "budget_exhausted"
    assert service.state(card, "implement")["limit_hit"] == {
        "scope": scope,
        "metric": "tool_calls",
        "used": 2,
        "limit": 2,
        "reason": "limit_reached",
    }
    await service.finish("implement")
    # A new controller, like Extension Reload, reads the same durable counters.
    service.execution = FactoryExecution(service)
    async with service.lock:
        await service.execution.retry(await service.card())
    await service.flush()
    card = await service.card()
    assert card["state"] == "waiting"
    assert card["runtime"]["budget"]["tool_calls"] == 2
    async with service.lock:
        if scope == "card":
            card["runtime"]["card_limits"] = {"tool_calls": 3}
        else:
            service.state(card, "implement")["limits"] = {"tool_calls": 3}
        await service.execution.retry(card)
    await service.flush()
    card = await service.card()
    assert card["state"] == "working"
    assert service.state(card, "implement")["budget"]["tool_calls"] == 2
    # A human send-back is a new step execution; the whole Card keeps its usage.
    async with service.lock:
        await service.execution.report_blocked(
            await service.tool_context("implement"), {"question": "Review?"}
        )
    await service.finish("implement")
    async with service.lock:
        await service.execution.send_back(await service.card(), "Rework", step_id="implement")
    await service.flush()
    card = await service.card()
    state = service.state(card, "implement")
    assert card["runtime"]["budget"]["tool_calls"] == 2
    assert state["budget"]["tool_calls"] == 0 and len(state["budget"]["runs"]) == 1
    assert len(card["runtime"]["budget"]["runs"]) == 3
    if scope == "step":
        assert state["limits"] == {"tool_calls": 3}
    await service.execution.sessions.quiesce()
    await service.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["card", "step"])
@pytest.mark.parametrize("boundary", ["before_request", "before_model_request"])
@pytest.mark.parametrize(
    "metric, totals, reason",
    [
        ("tokens", {"calls": 1, "input_tokens": 8, "output_tokens": 3}, "limit_reached"),
        ("cost_usd", {"calls": 1, "cost_usd": 11}, "limit_reached"),
        ("cost_usd", {"calls": 1, "cost_usd": 0.1, "unpriced_calls": 1}, "usage_unavailable"),
        ("tokens", {"calls": 1, "unreported_calls": 1}, "usage_unavailable"),
    ],
)
async def test_resource_limits_stop_inside_a_run_and_unknown_usage_is_not_free(
    service, scope, boundary, metric, totals, reason
):
    current: Json = {}
    queries = []

    async def usage(group_id, run_ids):
        queries.append((group_id, run_ids))
        return current

    service.host.execution_usage = usage
    definition = service.store.workflows["implement"]
    # Independent limits are alternatives: generous Tool and Run limits do not mask this one.
    (definition if scope == "card" else definition["steps"][0])["limits"] = {
        metric: 10,
        "tool_calls": 100,
        "runs": 100,
    }
    await started(service)
    current.update(totals)
    context = await service.context("implement")
    queries.clear()
    with pytest.raises(FactoryError) as stopped:
        await getattr(service.execution.sessions, boundary)(context)
    assert stopped.value.code == "budget_exhausted"
    card = await service.card()
    hit = service.state(card, "implement")["limit_hit"]
    assert (hit["metric"], hit["reason"]) == (metric, reason)
    if scope == "step":
        assert queries[0] == (f"{CARD}.implement", [context.run_id])
    else:
        # The Card's usage adds every execution group of the Card.
        assert queries == [(CARD, None), (f"{CARD}.implement", None), (f"{CARD}.review", None)]
    assert len(service.groups.admissions) == 1
    await service.finish("implement")


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["card", "step"])
@pytest.mark.parametrize("limit", [1, 2])
async def test_owned_child_requests_share_run_admission_without_becoming_the_participant(
    service, scope, limit
):
    definition = service.store.workflows["implement"]
    (definition if scope == "card" else definition["steps"][0])["limits"] = {"runs": limit}
    await started(service)
    root = await service.context("implement")
    child = replace(root, run_id="child-run", agent_id="child", session_id="child-session")
    if limit == 2:
        await service.execution.sessions.before_model_request(child)
        await service.execution.sessions.before_model_request(child)
        await service.execution.sessions.before_tool_call(
            child, tool_name="read", tool_call_id="child-call"
        )
        child = replace(child, run_id="next-child-run")
    with pytest.raises(FactoryError) as refusal:
        await service.execution.sessions.before_model_request(child)
    assert refusal.value.code == "budget_exhausted"
    card = await service.card()
    state = service.state(card, "implement")
    assert state["run"] == root.run_id
    assert len(card["runtime"]["budget"]["runs"]) == limit
    assert len(state["budget"]["runs"]) == limit
    assert card["runtime"]["budget"].get("tool_calls", 0) == limit - 1
    await service.finish("implement")


@pytest.mark.asyncio
async def test_judging_step_is_nudged_within_budget_and_retries_fresh(service):
    service.store.workflows["implement"]["steps"][1]["limits"] = {"runs": 2}
    await started(service)
    await hand_in(service, "implement")
    for _ in range(2):
        await service.finish("review")
    card = await service.card()
    assert card["waiting_reason"] == "budget_exhausted"
    async with service.lock:
        await service.execution.retry(card)
    await service.flush()
    card = await service.card()
    assert card["waiting_reason"] == "budget_exhausted"
    assert len(service.state(card, "review")["budget"]["runs"]) == 2
    async with service.lock:
        service.state(card, "review")["limits"] = {"runs": 3}
        await service.execution.retry(card)
    await service.flush()
    admissions = service.groups.admissions
    assert [participant for participant, _ in admissions] == [
        "implement",
        "review-r1",
        "review-r1",
        "review-r2",
    ]
    assert "factory_request_changes" in admissions[2][1].content
    assert admissions[3][1].kind == "initial"
    assert len(service.state(await service.card(), "review")["budget"]["runs"]) == 3
    await service.finish("review")
    assert (await service.card())["waiting_reason"] == "budget_exhausted"
    await service.execution.sessions.quiesce()
    await service.flush()


@pytest.mark.asyncio
async def test_terminal_tool_handoff_waits_until_old_run_is_published(service):
    await started(service)
    worker = await service.run("implement")
    async with service.lock:
        await service.execution.submit_work(
            await service.tool_context("implement"), {"summary": "Ready"}
        )
    # The next step starts at once; only the finished step's group waits for its Run.
    await asyncio.sleep(0)
    await worker.wait_entered.wait()
    # A launch closes its group once before the first participant joins.
    assert service.groups.closed.count(f"{CARD}.implement") == 1
    await service.finish("implement")
    assert service.groups.closed.count(f"{CARD}.implement") == 2
    assert "review-r1" in service.groups.configs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "cancelled"), [("working", False), ("working", True), ("waiting", False)]
)
async def test_discard_cancels_the_running_steps_without_another_turn(
    service, monkeypatch, state, cancelled
):
    await started(service)
    card = await hand_in(service, "implement")
    count = len(service.groups.admissions)
    if state == "waiting":
        run = await service.run("review")
        async with service.lock:
            await service.execution.report_blocked(
                await service.tool_context("review"), {"question": "A decision is needed."}
            )
        # The owner callback can precede terminal publication while another
        # parallel Tool is still running. Discard must cancel that work too.
        await run.wait_entered.wait()
    management = FactoryManagement(service)
    if cancelled:
        committed = asyncio.Event()
        release = asyncio.Event()
        changed = service.changed

        async def held_change(*args, **kwargs):
            await changed(*args, **kwargs)
            committed.set()
            await release.wait()

        monkeypatch.setattr(service, "changed", held_change)
        discarding = asyncio.create_task(management.operation("card.discard", {"id": card["id"]}))
        try:
            await committed.wait()
            discarding.cancel()
            await asyncio.sleep(0)
            assert not discarding.done()
        finally:
            release.set()
            await asyncio.gather(discarding, return_exceptions=True)
        assert discarding.cancelled()
    else:
        await management.operation("card.discard", {"id": card["id"]})
    await service.flush()
    card = await service.card()
    assert card["state"] == "discarded"
    assert not service.execution.running(card)
    assert not any("pending" in item for item in card["runtime"]["steps"].values())
    assert not service.groups.open[f"{CARD}.review"]
    assert len(service.groups.admissions) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["accept", "answer", "retry", "send_back"])
async def test_resuming_a_card_waits_for_wip_and_keeps_its_snapshot(service, action):
    service.wip_limit = 1
    service.store.workflows["one"] = workflow(
        "one", [step("do", after=[], approval=action == "accept")]
    )
    card = await started(service, workflow_id="one")
    if action != "accept":
        if action == "retry":
            await service.finish("do", "error")
        else:
            async with service.lock:
                await service.execution.report_blocked(
                    await service.tool_context("do"), {"question": "Which option?"}
                )
            await service.finish("do")
        card = await service.card()
    service.store.workflows["busy"] = workflow("busy", [step("other", after=[])])
    await started(service, "fct_busy", "busy")
    async with service.lock:
        if action in {"answer", "send_back"}:
            queued = await getattr(service.execution, action)(card, "Use option A")
        else:
            queued = await getattr(service.execution, action)(card)
    await service.flush()
    assert queued["state"] == "queued"
    assert queued["runtime"]["queued_action"] == action
    assert not service.execution.running(queued)
    pending = deepcopy(queued["runtime"]["steps"]["do"]["pending"])
    admissions = len(service.groups.admissions)
    if action in {"answer", "retry"}:
        context = await service.context("do")
        for boundary in ("before_request", "before_model_request"):
            with pytest.raises(FactoryError) as refused:
                await getattr(service.execution.sessions, boundary)(context)
            assert refused.value.code == "wrong_state"
        assert await service.execution.sessions.before_tool_call(
            context, tool_name="read", tool_call_id="late-call"
        )
        await service.execution.sessions.run_finished(context, outcome="success")
        assert (await service.card())["runtime"]["steps"]["do"]["pending"] == pending
    await service.execution.sessions.reconcile()
    await service.flush()
    assert len(service.groups.admissions) == admissions
    service.store.workflows["one"]["steps"][0]["task"] = "Unrelated later edit"
    async with service.lock:
        await service.execution.discard(await service.card("fct_busy"))
    await service.flush()
    async with service.lock:
        await service.execution.start(await service.card())
    await service.flush()
    resumed = await service.card()
    assert resumed["state"] == "working"
    assert "queued_action" not in resumed["runtime"]
    assert len(service.groups.admissions) == admissions + 1
    assert service.groups.admissions[-1][1].content == pending["content"]
    assert resumed["runtime"]["workflow"]["steps"][0]["task"] == "Do the step."
    await service.execution.sessions.quiesce()
    await service.flush()


@pytest.mark.asyncio
async def test_restart_is_idempotent_and_second_interruption_waits_for_user(service):
    await started(service)
    groups = service.groups
    # An unknown canonical group is archived, not deleted from Session storage;
    # the Card group of an earlier Factory stays known.
    groups.bindings[("orphan", "peer")] = SimpleNamespace()
    groups.bindings[(CARD, "worker")] = SimpleNamespace()
    for interruption in (1, 2):
        run = await service.run("implement")
        run.finish("interrupted")
        groups.recovered = True
        service.execution = FactoryExecution(service)
        await service.execution.sessions.reconcile()
        await service.execution.sessions.reconcile()
        await service.flush()
        card = await service.card()
        assert service.state(card, "implement")["restarts"] == interruption
    assert card["waiting_reason"] == "restart"
    assert groups.archived == ["orphan"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["foreign", "role", "epoch", "state", "missing_summary"])
async def test_private_tools_refuse_wrong_participants_states_and_incomplete_submissions(
    service, case
):
    card = await started(service)
    context = await service.tool_context("implement")
    arguments = {"summary": "Ready"}
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
        runtime = deepcopy(card["runtime"])
        runtime["steps"]["implement"].update(status="waiting", reason="blocked")
        card = await service.store.change(
            card["id"],
            {"state": "waiting", "runtime": runtime},
            actor="factory",
            event="paused",
        )
    else:
        arguments.pop("summary")
    revision = card["revision"]
    async with service.lock:
        with pytest.raises(FactoryError):
            await handler(context, arguments)
    assert (await service.card())["revision"] == revision
    await service.execution.sessions.quiesce()
    await service.flush()


@pytest.mark.asyncio
async def test_blocked_step_waits_for_user_and_answer_continues_same_session(service):
    card = await started(service)
    address = service.state(card, "implement")["participants"]["implement"]
    async with service.lock:
        await service.execution.report_blocked(
            await service.tool_context("implement"), {"question": "Which behavior?"}
        )
    await service.finish("implement")
    card = await service.card()
    assert (card["state"], card["waiting_reason"]) == ("waiting", "blocked")
    assert service.state(card, "implement")["question"] == "Which behavior?"
    async with service.lock:
        await service.execution.answer(card, "Use the first behavior")
    await service.flush()
    card = await service.card()
    assert service.state(card, "implement")["participants"]["implement"] == address
    participant, answer = service.groups.admissions[-1]
    assert participant == "implement" and answer.kind == "continuation"
    assert "Use the first behavior" in answer.content
    assert (await service.store.history(CARD))["comments"][-1]["text"] == "Use the first behavior"
    await service.execution.sessions.quiesce()


async def blocked(service: Service) -> Json:
    await started(service)
    async with service.lock:
        await service.execution.report_blocked(
            await service.tool_context("implement"), {"question": "Which behavior?"}
        )
    await service.finish("implement")
    return await service.card()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["preflight", "carrier", "admission"])
async def test_failed_admission_retries_original_guidance_without_replaying_canonical_input(
    service, monkeypatch, boundary
):
    await blocked(service)
    groups = service.groups
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
        queued = await service.execution.answer(await service.card(), "unique-answer-guidance")
    pending = service.state(queued, "implement")["pending"]
    await service.flush()
    paused = await service.card()
    assert (paused["state"], paused["waiting_reason"]) == ("waiting", "run_failed")
    assert service.state(paused, "implement")["pending"] == pending
    assert len(groups.admissions) == (2 if boundary == "admission" else 1)
    assert not groups.open[f"{CARD}.implement"]
    monkeypatch.setattr(groups, "start", start)
    async with service.lock:
        retried = await service.execution.retry(paused)
    assert service.state(retried, "implement")["pending"]["id"] == pending["id"]
    await service.flush()
    card = await service.card()
    admissions = 3 if boundary == "admission" else 2
    assert len(groups.admissions) == admissions
    assert "pending" not in service.state(card, "implement")
    assert groups.admissions[1][1].request_id == pending["id"]
    assert groups.admissions[1][1].content == pending["content"]
    assert sum("unique-answer-guidance" in content for _, content in groups.carriers) == 1
    if boundary == "admission":
        assert groups.admissions[-1][1].request_id != pending["id"]
        assert "unique-answer-guidance" not in groups.admissions[-1][1].content
    assert card["state"] == "working"
    await service.finish("implement", "error")
    assert (await service.card())["waiting_reason"] == "run_failed"
    assert len(groups.admissions) == admissions
    await service.execution.sessions.quiesce()
    await service.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_admission", [False, True])
async def test_retry_authorization_does_not_survive_crash_at_canonical_admission(
    service, monkeypatch, prior_admission
):
    await blocked(service)
    groups = service.groups
    start = groups.start

    async def unavailable(handle, participant, input):
        if prior_admission:
            admitted = await start(handle, participant, input)
            groups.runs[admitted.run_id].finish("failed")
        raise FactoryError("launch_failed", "Admission unavailable")

    monkeypatch.setattr(groups, "start", unavailable)
    async with service.lock:
        queued = await service.execution.answer(await service.card(), "unique-answer-guidance")
    pending = service.state(queued, "implement")["pending"]
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
    assert service.state(await service.card(), "implement")["pending"]["id"] == pending["id"]
    assert len(groups.admissions) == 2
    monkeypatch.setattr(groups, "start", start)
    groups.recovered = True
    service.execution = FactoryExecution(service)
    await service.execution.sessions.reconcile()
    await service.flush()
    card = await service.card()
    assert (card["state"], card["waiting_reason"]) == ("waiting", "run_failed")
    assert len(groups.admissions) == 2
    assert sum("unique-answer-guidance" in content for _, content in groups.carriers) == 1
    async with service.lock:
        await service.execution.retry(card)
    await service.flush()
    assert (await service.card())["state"] == "working"
    assert len(groups.admissions) == 3
    assert "unique-answer-guidance" not in groups.admissions[-1][1].content
    await service.execution.sessions.quiesce()
    await service.flush()


@pytest.mark.asyncio
async def test_automatic_owned_continuation_adopts_canonical_run_and_preserves_pending_note(
    service,
):
    await started(service)
    groups = service.groups
    previous = await service.run("implement")
    await service.execution.sessions.run_finished(groups.context(previous), outcome="success")
    previous.finish()
    card = await service.card()
    pending = service.state(card, "implement")["pending"]
    handle = await groups.open_group(f"{CARD}.implement")
    admission = await groups.start(
        handle, "implement", TemporaryRunInput("continuation", "Background result", "automatic")
    )
    automatic = groups.runs[admission.run_id]
    # A delegated child under the same temporary Agent never becomes the participant.
    await service.execution.sessions.before_request(
        replace(groups.context(automatic), session_id="child")
    )
    assert service.state(await service.card(), "implement")["run"] == previous.id
    delivery = await service.execution.sessions.before_request(groups.context(automatic))
    assert delivery is not None and delivery.delivery_id == pending["id"]
    assert delivery.entries == (pending["content"],)
    adopted = await service.card()
    assert service.state(adopted, "implement")["pending"]["id"] == pending["id"]
    assert await service.execution.sessions.before_request(groups.context(automatic)) == delivery
    with pytest.raises(FactoryError) as unacknowledged:
        await service.execution.sessions.acknowledge_delivery(groups.context(automatic), delivery)
    assert unacknowledged.value.code == "delivery_unacknowledged"
    await groups.append_delivery(groups.context(automatic), delivery)
    await groups.append_delivery(groups.context(automatic), delivery)
    await service.execution.sessions.acknowledge_delivery(groups.context(automatic), delivery)
    assert await service.execution.sessions.before_request(groups.context(automatic)) is None
    revision = (await service.card())["revision"]
    await service.execution.sessions.acknowledge_delivery(groups.context(automatic), delivery)
    assert (await service.card())["revision"] == revision
    assert sum(receipt_id == delivery.delivery_id for receipt_id, _ in groups.carriers) == 1
    declarations = ExtensionDeclarations()
    register(ExtensionAPI("factory", declarations, config={}, logger=None))
    assert declarations.session_runtime is not None
    assert (
        getattr(declarations.session_runtime.acknowledge_delivery, "__func__", None)
        is FactoryExtension.acknowledge_delivery
    )
    assert (
        getattr(declarations.session_runtime.before_model_request, "__func__", None)
        is FactoryExtension.before_model_request
    )
    registered = declarations.session_runtime.before_request.__self__
    registered.execution = service.execution
    assert await declarations.session_runtime.before_request(groups.context(automatic)) is None
    await declarations.session_runtime.acknowledge_delivery(groups.context(automatic), delivery)
    await declarations.session_runtime.before_model_request(groups.context(automatic))
    await service.flush()
    state = service.state(await service.card(), "implement")
    assert state["run"] == automatic.id and "pending" not in state
    async with service.lock:
        await service.execution.report_blocked(
            await service.tool_context("implement"), {"question": "Decision needed"}
        )
    # Waiting is not permission to let a late completion keep the participant or
    # its children acting with ordinary Tools while group cancellation catches up.
    for context in (
        groups.context(automatic),
        replace(groups.context(automatic), session_id="child"),
    ):
        with pytest.raises(FactoryError) as refusal:
            await service.execution.sessions.before_request(context)
        assert refusal.value.code == "wrong_state"
    await service.finish("implement")


async def automatic_handoff(service: Service):
    await started(service)
    groups = service.groups
    previous = await service.run("implement")
    async with service.lock:
        await service.execution.report_blocked(
            await service.tool_context("implement"), {"question": "Which behavior?"}
        )
    previous.finish()
    async with service.lock:
        await service.execution.answer(await service.card(), "unique-answer-guidance")
    pending = service.state(await service.card(), "implement")["pending"]
    handle = await groups.open_group(f"{CARD}.implement")
    admission = await groups.start(
        handle, "implement", TemporaryRunInput("continuation", "Background result", "automatic")
    )
    automatic = groups.runs[admission.run_id]
    # The ordinary launch loses to the active automatic Run before its hook.
    await service.flush()
    return groups.context(automatic), pending


@pytest.mark.asyncio
async def test_final_run_warning_arrives_with_its_pending_continuation(service):
    context, pending = await automatic_handoff(service)
    card = await service.card()
    service.state(card, "implement")["limits"] = {"runs": 2}
    await service.store.change(
        card["id"], {"runtime": card["runtime"]}, actor="user", event="limits_updated"
    )
    delivery = await service.execution.sessions.before_request(context)
    assert delivery.effect_kind == "factory_continuation"
    assert delivery.entries[0] == pending["content"]
    assert len(delivery.entries) == 2
    assert "factory_report_blocked" in delivery.entries[1]
    assert await service.execution.sessions.before_request(context) == delivery
    await service.groups.append_delivery(context, delivery)
    # Startup reconciliation acknowledges the warning with the same canonical receipt.
    service.execution = FactoryExecution(service)
    await service.execution.sessions.reconcile()
    assert await service.execution.sessions.before_request(context) is None
    card = await service.card()
    assert len(service.state(card, "implement")["budget"]["runs"]) == 2
    assert all(notice["seen"] for notice in card["runtime"]["budget_notices"].values())
    await service.finish("implement")
    assert (await service.card())["waiting_reason"] == "budget_exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["missing", "committed", "conflicting"])
@pytest.mark.parametrize("recovery", ["restart", "retry"])
async def test_pending_handoff_survives_append_failure_and_reconciles_receipts_before_recovery(
    service, receipt, recovery
):
    context, pending = await automatic_handoff(service)
    groups = service.groups
    delivery = await service.execution.sessions.before_request(context)
    assert delivery is not None and "unique-answer-guidance" in delivery.entries[0]
    if recovery == "retry":
        await service.execution.sessions.run_finished(context, outcome="error")
        groups.runs[context.run_id].finish("failed")
        await service.flush()
        state = service.state(await service.card(), "implement")
        assert state["pending"]["id"] == pending["id"]
    if receipt == "missing":
        with pytest.raises(OSError):
            await groups.append_delivery(context, delivery, fail=True)
        if recovery == "restart":
            assert await service.execution.sessions.before_request(context) == delivery
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
            await service.execution.sessions.reconcile()
        else:
            async with service.lock:
                await service.execution.retry(await service.card())

    if receipt == "conflicting":
        if recovery == "restart":
            # Startup recovery logs the conflict and leaves the Card as it was.
            await recover()
        else:
            with pytest.raises(FactoryError) as conflict:
                await recover()
            assert conflict.value.code == "receipt_conflict"
        state = service.state(await service.card(), "implement")
        assert state["pending"]["id"] == pending["id"]
        assert len(groups.admissions) == 2
    else:
        await recover()
        await service.flush()
        latest = groups.admissions[-1][1]
        assert ("unique-answer-guidance" in latest.content) is (receipt == "missing")
        assert (latest.request_id == pending["id"]) is (receipt == "missing")
        assert sum("unique-answer-guidance" in content for _, content in groups.carriers) == 1
        assert "pending" not in service.state(await service.card(), "implement")
    await service.execution.sessions.quiesce()
    await service.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["error", "sibling", "participant", "epoch", "generation", "unowned", "older"]
)
@pytest.mark.parametrize("boundary", ["before_request", "before_receipt", "committed"])
async def test_root_failure_pauses_exact_handoff_without_replaying_it(
    service, monkeypatch, case, boundary
):
    context, pending = await automatic_handoff(service)
    groups = service.groups
    if boundary != "before_request":
        delivery = await service.execution.sessions.before_request(context)
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
    await service.execution.sessions.run_finished(context, outcome="error")
    groups.runs[original.run_id].finish("failed")
    await service.flush()
    card = await service.card()
    state = service.state(card, "implement")
    assert len(groups.admissions) == 2
    if case == "error" and boundary == "committed":
        assert "pending" not in state
    else:
        assert state["pending"]["id"] == pending["id"]
    if case == "error":
        assert (card["state"], card["waiting_reason"]) == ("waiting", "run_failed")
        assert state["run"] == original.run_id
        assert card["runtime"]["budget"]["runs"] == ["run-1", original.run_id]
        assert not groups.open[f"{CARD}.implement"]
        await service.execution.sessions.run_finished(context, outcome="error")
        await service.flush()
        assert len(groups.admissions) == 2
        async with service.lock:
            await service.execution.retry(await service.card())
        await service.flush()
        assert len(groups.admissions) == 3
        assert len((await service.card())["runtime"]["budget"]["runs"]) == 3
        assert ("unique-answer-guidance" in groups.admissions[-1][1].content) is (
            boundary != "committed"
        )
        assert (groups.admissions[-1][1].request_id == pending["id"]) is (boundary != "committed")
        assert sum("unique-answer-guidance" in content for _, content in groups.carriers) == 1
    else:
        assert card == before
    await service.execution.sessions.quiesce()
    await service.flush()


@pytest.mark.asyncio
async def test_quiesce_drains_without_classifying_shutdown_as_step_failure(service):
    await started(service)
    revision = (await service.card())["revision"]
    await service.execution.sessions.quiesce("reload")
    await service.flush()
    card = await service.card()
    assert card["state"] == "working" and card["revision"] == revision
    assert not service.groups.open[f"{CARD}.implement"]


@pytest.mark.asyncio
async def test_independent_steps_run_in_parallel_and_rework_reruns_what_follows(service):
    service.store.workflows["drafts"] = workflow(
        "drafts",
        [
            step("draft1", "builder", "provider/worker", after=[], task="Draft a plan."),
            step("draft2", "planner", after=[]),
            step("merge", after=["draft1", "draft2"]),
            step("review", "reviewer", after=["merge"], returns_to="draft2", rounds=2),
        ],
        final_approval=True,
    )
    groups = service.groups
    card = await started(service, workflow_id="drafts")
    # Steps without a common predecessor run at the same time in their own groups.
    assert {participant for participant, _ in groups.admissions} == {"draft1", "draft2"}
    assert FactoryExecution.running(card)
    config = groups.configs["draft1"]
    assert "Draft a plan." in config.instructions and config.identity_profile == "builder"
    draft2 = next(item for participant, item in groups.admissions if participant == "draft2")
    card = await hand_in(service, "draft1", "Plan A")
    assert service.state(card, "merge")["status"] == "pending"
    assert "Plan A" not in draft2.content
    await hand_in(service, "draft2", "Plan B")
    participant, merge = groups.admissions[-1]
    assert participant == "merge" and "Plan A" in merge.content and "Plan B" in merge.content
    await hand_in(service, "merge", "Merged plan")
    assert groups.admissions[-1][0] == "review-r1"
    card = await send_back_work(service, "review", "Plan B misses a rollback")
    participant, rework = groups.admissions[-1]
    assert (card["state"], participant) == ("working", "draft2")
    assert rework.kind == "continuation" and "misses a rollback" in rework.content
    assert service.state(card, "draft1")["status"] == "done"
    await hand_in(service, "draft2", "Plan B2")
    participant, refresh = groups.admissions[-1]
    assert participant == "merge" and refresh.kind == "continuation"
    assert "Plan B2" in refresh.content
    await hand_in(service, "merge", "Merged plan 2")
    participant, review = groups.admissions[-1]
    assert participant == "review-r2" and review.kind == "initial"
    assert "Plan B2" in review.content and "misses a rollback" in review.content
    card = await hand_in(service, "review", "The drafts now cover rollback.")
    assert (card["state"], card["waiting_reason"]) == ("waiting", "finished")
    async with service.lock:
        await service.execution.accept(card)
    await service.flush()
    card = await service.card()
    assert card["state"] == "done"
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
async def test_send_back_to_a_running_step_restarts_it_with_the_comment(service):
    service.store.workflows["pair"] = workflow(
        "pair", [step("left", after=[]), step("right", after=[])]
    )
    await started(service, workflow_id="pair")
    async with service.lock:
        await service.execution.report_blocked(
            await service.tool_context("right"), {"question": "Which variant?"}
        )
    await service.finish("right")
    left = await service.run("left")
    async with service.lock:
        await service.execution.send_back(await service.card(), "Use variant B", step_id="left")
    await service.flush()
    card = await service.card()
    # The running step's old Run is cancelled; its Session continues with the comment.
    assert left.status.value == "cancelled"
    participant, rework = service.groups.admissions[-1]
    assert participant == "left" and rework.kind == "continuation"
    assert "Use variant B" in rework.content
    assert service.state(card, "left")["status"] == "running"
    assert service.state(card, "right")["reason"] == "blocked"
    await service.execution.sessions.quiesce()
    await service.flush()


@pytest.mark.asyncio
async def test_approval_waits_before_its_step_and_send_back_targets_the_chosen_step(service):
    service.store.workflows["planimpl"] = workflow(
        "planimpl",
        [
            step("plan", after=[]),
            step("planreview", "reviewer", after=["plan"], returns_to="plan", rounds=2),
            *implement_steps(after=["planreview"], approval=True),
        ],
    )
    groups = service.groups
    mixed = {
        "workflow": {"steps": [{"id": "waiting"}, {"id": "approval"}]},
        "steps": {
            "waiting": {"status": "waiting", "reason": "blocked"},
            "approval": {"status": "approval"},
        },
    }
    assert service.execution.derive(mixed) == ("waiting", "checkpoint")
    await started(service, workflow_id="planimpl")
    await hand_in(service, "plan", "First plan")
    card = await hand_in(service, "planreview", "Sound plan.")
    assert (card["state"], card["waiting_reason"]) == ("waiting", "checkpoint")
    assert service.state(card, "implement")["status"] == "approval"
    async with service.lock:
        await service.execution.send_back(card, "Add a migration step", "plan")
    await service.flush()
    assert groups.admissions[-1][0] == "plan"
    assert "Add a migration step" in groups.admissions[-1][1].content
    await hand_in(service, "plan", "Second plan")
    card = await hand_in(service, "planreview", "Sound plan with migration.")
    assert card["waiting_reason"] == "checkpoint"
    # The user's limits for a step that has not run yet stay when it starts.
    service.state(card, "implement")["limits"] = {"tool_calls": 7}
    async with service.lock:
        await service.execution.accept(card)
    await service.flush()
    card = await service.card()
    assert service.state(card, "implement")["limits"] == {"tool_calls": 7}
    participant, implement = groups.admissions[-1]
    assert participant == "implement" and implement.kind == "initial"
    assert "Second plan" in implement.content and "First plan" not in implement.content
    card = await hand_in(service, "implement")
    assert service.state(card, "review")["participant"] == "review-r1"
    card = await send_back_work(service, "review", "test-owned-changes")
    assert service.state(card, "implement")["status"] == "running"
    assert service.state(card, "implement").get("approved") is True


@pytest.mark.asyncio
async def test_workflow_step_waits_for_its_child_card_and_rework_discards_it(service):
    service.store.workflows["plan"] = workflow("plan", [step("plan", after=[])])
    service.store.workflows["parent"] = workflow(
        "parent",
        [
            step("prepare", after=[]),
            child_step("sub", "plan", after=["prepare"]),
            step("check", "reviewer", after=["prepare"], returns_to="prepare", rounds=2),
        ],
    )
    await started(service, workflow_id="parent")
    card = await hand_in(service, "prepare", "Context v1")
    # The workflow step and the judging step run side by side.
    sub = service.state(card, "sub")
    assert (sub["status"], sub["passes"], sub["child"]) == ("child", 1, "fct_child1")
    assert service.state(card, "check")["status"] == "running"
    child = await service.card("fct_child1")
    assert (child["parent_id"], child["state"], child["workflow_id"]) == (CARD, "queued", "plan")
    assert child["description"] == "Context v1"
    await send_back_work(service, "check", "Context misses the API")
    assert (await service.card("fct_child1"))["state"] == "discarded"
    card = await hand_in(service, "prepare", "Context v2")
    assert service.state(card, "sub")["child"] == "fct_child2"
    card = await hand_in(service, "check", "Context is complete.")
    assert card["state"] == "working"
    await started(service, "fct_child2", "plan")
    await hand_in(service, "plan", "Plan text", card_id="fct_child2")
    await service.flush()
    card = await service.card()
    assert card["state"] == "done"
    outputs = (await service.store.history(CARD))["outputs"]
    sub = [item for item in outputs if item["step"] == "sub"]
    assert len(sub) == 1 and sub[0]["result"] == "Plan: Plan text"
    assert sub[0]["summary"] == "Wrote Plan text"


@pytest.mark.asyncio
async def test_initial_steps_receive_the_resolved_project(service):
    service.store.workflows["plan"] = workflow("plan", [step("plan", after=[])])
    service.store.workflows["parent"] = workflow(
        "parent",
        [child_step("child", "plan", after=[]), step("parallel", after=[])],
        project_id="resolved",
    )
    service.store.add(CARD, "parent", project_id="")
    card = await started(service, workflow_id="parent")
    child = await service.card(service.state(card, "child")["child"])
    assert card["project_id"] == child["project_id"] == "resolved"
    assert '"project_id": "resolved"' in service.groups.admissions[-1][1].content
    await service.execution.sessions.quiesce()
    await service.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["nowait", "discarded", "missing"])
async def test_workflow_step_without_wait_continues_and_failed_children_wait_for_retry(
    service, case
):
    service.store.workflows["plan"] = workflow("plan", [step("plan", after=[])])
    service.store.workflows["parent"] = workflow(
        "parent",
        [
            child_step("sub", "plan", after=[], wait=case != "nowait"),
            step("after", after=["sub"]),
        ],
    )
    if case == "missing":
        service.store.workflows["parent"]["steps"][0]["workflow"] = "gone"
    card = await started(service, workflow_id="parent")
    if case == "nowait":
        assert service.state(card, "sub")["status"] == "done"
        assert service.state(card, "after")["status"] == "running"
        assert (await service.card("fct_child1"))["state"] == "queued"
        outputs = (await service.store.history(CARD))["outputs"]
        assert outputs[0]["summary"] == "Started Card fct_child1 with Workflow 'Plan'."
        return
    if case == "discarded":
        async with service.lock:
            await service.execution.discard(await service.card("fct_child1"))
        await service.flush()
    card = await service.card()
    state = service.state(card, "sub")
    assert (card["state"], card["waiting_reason"]) == ("waiting", "child_failed")
    assert state["note"]
    if case == "missing":
        service.store.workflows["parent"]["steps"][0]["workflow"] = "plan"
        async with service.lock:
            card["runtime"]["workflow"]["steps"][0]["workflow"] = "plan"
            await service.execution.retry(card)
    else:
        async with service.lock:
            await service.execution.retry(card)
    await service.flush()
    card = await service.card()
    child, passes = ("fct_child1", 1) if case == "missing" else ("fct_child2", 2)
    state = service.state(card, "sub")
    assert (state["status"], state["passes"], state["child"]) == ("child", passes, child)


@pytest.mark.asyncio
async def test_step_interrupted_by_an_upgrade_waits_and_retry_runs_it_with_kept_results(service):
    definition = deepcopy(service.store.workflows["implement"])
    card = service.store.add(state="waiting", waiting_reason="upgraded")
    runtime = _upgraded_runtime(
        {"done": ["implement"], "step": "review", "cwd": str(service.directory)}, definition
    )
    service.store.cards[CARD]["runtime"] = runtime
    service.store.histories[CARD]["outputs"].append(
        {
            **_OUTPUT_DEFAULTS,
            "id": 1,
            "step": "implement",
            "step_name": "Implement",
            "kind": "work",
            "pass": 1,
            "participant_id": "worker",
            "summary": "Implemented on branch topic",
        }
    )
    service.execution = FactoryExecution(service)
    await service.execution.sessions.reconcile()
    card = await service.card()
    assert (card["state"], service.state(card, "review")["reason"]) == ("waiting", "upgraded")
    assert not service.groups.admissions
    async with service.lock:
        await service.execution.retry(card)
    await service.flush()
    participant, review = service.groups.admissions[-1]
    assert participant == "review-r1" and review.kind == "initial"
    assert "Implemented on branch topic" in review.content
