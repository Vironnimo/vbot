"""Recover native authority and handoff at persisted/callback failure seams."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from core.extensions.operations import ExtensionOperationError
from core.sessions import SessionAddress
from tests.core.runtime.runtime_test_support import extension_record, tool_context
from tests.resources.extensions.factory.test_factory_execution import step, workflow
from tests.resources.extensions.factory.test_factory_native_budget_admission import (
    drain_factory_work,
    establish,
)
from tests.resources.extensions.factory.test_factory_native_integration import (
    MODEL,
    NativeFactoryRuntime,
)
from tests.resources.extensions.factory.test_factory_native_integration import (
    native_factory_runtime as native_factory_runtime,
)

type Json = dict[str, Any]
pytestmark = pytest.mark.asyncio


async def restart(harness: NativeFactoryRuntime) -> None:
    await harness.runtime.aclose()
    harness.runtime.start()
    await harness.runtime.fire_extension_startup()
    record = extension_record(harness.runtime, "factory")
    assert record.declarations.operations is not None
    harness.operations = record.declarations.operations
    harness.service = next(
        tool for tool in record.declarations.tools if tool.name == "factory_card"
    ).handler.__self__


async def member(harness: NativeFactoryRuntime, factory: Json, agent_id: str) -> Json:
    harness.runtime.agents.create(agent_id, model=MODEL, librarian_enabled=False)
    return cast(
        Json,
        (await harness.operation("member.add", factory_id=factory["id"], agent_id=agent_id))[
            "member"
        ],
    )


async def task(harness: NativeFactoryRuntime, factory: Json, assignee: str, **fields: Any) -> Json:
    return cast(
        Json,
        (
            await harness.operation(
                "task.create",
                factory_id=factory["id"],
                title="Recovery task",
                objective="test-owned-recovery-objective",
                assignee_id=assignee,
                **fields,
            )
        )["task"],
    )


def complete(harness: NativeFactoryRuntime, record: Json, request: Json) -> None:
    harness.model.tool(
        "factory_tasks",
        {
            "action": "complete",
            "id": record["id"],
            "outcome": "test-owned-recovered-outcome",
            "evidence": "test-owned-recovered-evidence",
        },
        request=request,
    )


@pytest.mark.parametrize("scope", ["factory", "member"])
async def test_restart_repairs_a_committed_scope_revision_before_its_callback(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path, scope: str
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path)
    worker = await member(harness, factory, "worker")
    work = await task(harness, factory, worker["id"])
    await harness.request()
    native = harness.service.records().native
    before = await native.get_task(work["id"])
    if scope == "factory":
        await native.update_factory(
            factory["id"], {"name": "Recovered name"}, expected_revision=factory["revision"]
        )
    else:
        await native.update_member(
            worker["id"], {"role": "Recovered role"}, expected_revision=worker["revision"]
        )
    # The record transaction committed; changed() never ran before process loss.
    await restart(harness)
    request = await harness.request()
    recovered = await harness.service.records().native.get_task(work["id"])
    assert recovered["epoch"] > before["epoch"]
    assert recovered["state"] == "working"
    assert ("Recovered name" if scope == "factory" else "Recovered role") in str(
        request["messages"]
    )
    run = harness.runtime.chat_run_manager.active_runs()[0]
    complete(harness, work, request)
    await harness.finish(run)
    await harness.settle()
    assert len(harness.model.requests) == 2


@pytest.mark.parametrize("scope", ["factory", "member"])
async def test_restart_archives_historical_groups_after_a_committed_retirement(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path, scope: str
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path)
    worker = await member(harness, factory, "worker")
    work = await task(harness, factory, worker["id"])
    request = await harness.request()
    run = harness.runtime.chat_run_manager.active_runs()[0]
    complete(harness, work, request)
    await harness.finish(run)
    await harness.settle()
    group_id = run.execution_owner.group_id if run.execution_owner is not None else ""
    assert group_id in await harness.runtime.chat_sessions.temporary_groups_async(
        owner_name="factory"
    )
    native = harness.service.records().native
    if scope == "factory":
        await native.update_factory(factory["id"], {"state": "archived"})
    else:
        await native.update_member(worker["id"], {"state": "retired"})
    await restart(harness)
    await harness.settle()
    assert group_id not in await harness.runtime.chat_sessions.temporary_groups_async(
        owner_name="factory"
    )
    detail = await harness.operation("task.get", id=work["id"])
    assert detail["task"]["state"] == "done"
    assert len(detail["results"]) == 1
    assert len(harness.model.requests) == 1


@pytest.mark.parametrize("paused", [False, True])
async def test_restart_recovers_a_child_result_once_without_reviving_a_paused_parent(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path, paused: bool
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path)
    parent = await task(harness, factory, factory["leader_member_id"])
    initial = await harness.request()
    parent_run = harness.runtime.chat_run_manager.active_runs()[0]
    initial["answer"].set_result(
        {
            "content": "test-owned-await-child",
            "tool_calls": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
    )
    await harness.finish(parent_run)
    worker = await member(harness, factory, "worker")
    child = await task(harness, factory, worker["id"], parent_id=parent["id"])
    await harness.request()
    native = harness.service.records().native
    if paused:
        current = (await harness.operation("task.get", id=parent["id"]))["task"]
        await harness.operation(
            "task.pause", id=parent["id"], expected_revision=current["revision"]
        )
    await native.submit_task(
        child["id"],
        "test-owned-durable-child-result",
        "test-owned-child-evidence",
        actor=f"member:{worker['id']}",
        expected_epoch=child["epoch"],
    )
    # The durable result exists but _task_done() has not prepared its parent wake.
    await restart(harness)
    if paused:
        await harness.settle()
        current = (await harness.operation("task.get", id=parent["id"]))["task"]
        assert current["state"] == "paused"
        assert len(harness.model.requests) == 2
        await harness.operation(
            "task.resume", id=parent["id"], expected_revision=current["revision"]
        )
    request = await harness.request()
    assert "test-owned-durable-child-result" in str(request["messages"])
    run = harness.runtime.chat_run_manager.active_runs()[0]
    request["answer"].set_result(
        {
            "content": "test-owned-child-seen",
            "tool_calls": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
    )
    await harness.finish(run)
    await harness.settle()
    count = len(harness.model.requests)
    await harness.service.native.reconcile()
    await harness.settle()
    assert len(harness.model.requests) == count
    assert (await harness.operation("task.get", id=parent["id"]))["task"]["state"] == "blocked"


@pytest.mark.parametrize("retire_first", [False, True])
async def test_handoff_waits_for_owned_descendants_and_preserves_bounded_canonical_progress(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path, retire_first: bool
) -> None:
    harness = native_factory_runtime
    runtime = harness.runtime
    factory = await establish(harness, tmp_path)
    original = await member(harness, factory, "original-worker")
    successor = await member(harness, factory, "successor-worker")
    work = await task(harness, factory, original["id"])
    initial = await harness.request()
    root = runtime.chat_run_manager.active_runs()[0]
    old_address = SessionAddress(None, root.agent_id, root.session_id)
    initial["answer"].set_result(
        {
            "content": "test-owned-partial-progress " + "x" * 5000 + " test-owned-omitted-tail",
            "tool_calls": [
                {
                    "id": "spawn-before-handoff",
                    "name": "subagent",
                    "arguments": {
                        "action": "run",
                        "description": "Inspect partial implementation",
                        "content": "test-owned-delegated-progress",
                    },
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )
    requests = [await harness.request(), await harness.request()]
    assert {request["kwargs"]["test_context"]["agent_id"] for request in requests} == {
        root.agent_id
    }
    assert len({request["kwargs"]["test_context"]["session_id"] for request in requests}) == 2
    descendant = next(run for run in runtime.chat_run_manager.active_runs() if run.id != root.id)
    assert descendant.execution_owner == root.execution_owner
    released = asyncio.Event()
    root_stopping = asyncio.Event()
    descendant_stopping = asyncio.Event()

    async def cleanup(entered: asyncio.Event) -> None:
        entered.set()
        await released.wait()

    root.add_cancel_callback(lambda: cleanup(root_stopping))
    descendant.add_cancel_callback(lambda: cleanup(descendant_stopping))
    old_context = replace(
        tool_context("factory_tasks", tmp_path, agent_id=root.agent_id),
        session_id=root.session_id,
        run_id=root.id,
        execution_owner=root.execution_owner,
        session_tool_grants=("factory_tasks",),
    )
    if retire_first:
        await harness.operation(
            "member.retire", id=original["id"], expected_revision=original["revision"]
        )
    else:
        current = (await harness.operation("task.get", id=work["id"]))["task"]
        await harness.operation(
            "task.assign",
            id=work["id"],
            expected_revision=current["revision"],
            assignee_id=successor["id"],
        )
    async with asyncio.timeout(3):
        await asyncio.gather(root_stopping.wait(), descendant_stopping.wait())
    assert len(harness.model.requests) == 3
    assert harness.model.pending.empty()
    assert not any(
        run.agent_id == "successor-worker" for run in runtime.chat_run_manager.active_runs()
    )
    released.set()
    await drain_factory_work(harness)
    if retire_first:
        assert root.execution_owner is not None
        assert (
            root.execution_owner.group_id
            not in await runtime.chat_sessions.temporary_groups_async(owner_name="factory")
        )
        await restart(harness)
        current = (await harness.operation("task.get", id=work["id"]))["task"]
        await harness.operation(
            "task.assign",
            id=work["id"],
            expected_revision=current["revision"],
            assignee_id=successor["id"],
        )
    request = await harness.request()
    assert root.status.value != "running"
    assert descendant.status.value != "running"
    assert request["kwargs"]["test_context"]["agent_id"] == "successor-worker"
    context = str(request["messages"])
    assert "test-owned-partial-progress" in context
    assert "test-owned-omitted-tail" not in context
    assert original["id"] in context
    assert root.session_id in context
    refused = await runtime.tools.dispatch(
        old_context,
        {
            "action": "complete",
            "id": work["id"],
            "outcome": "stale-outcome",
            "evidence": "stale-evidence",
        },
    )
    assert refused["error"]["code"] == "stale_run"
    new_run = next(
        run for run in runtime.chat_run_manager.active_runs() if run.agent_id == "successor-worker"
    )
    complete(harness, work, request)
    await harness.finish(new_run)
    await harness.settle()
    if not retire_first:
        old_history = runtime.chat_sessions.get(old_address).load()
        assert (
            sum(
                call.name == "subagent"
                for message in old_history
                for call in message.tool_calls or []
            )
            == 1
        )
    new_history = runtime.chat_sessions.get(
        SessionAddress(None, new_run.agent_id, new_run.session_id)
    ).load()
    assert [call.name for message in new_history for call in message.tool_calls or []] == [
        "factory_tasks"
    ]
    assert len(harness.model.requests) == 4


async def test_restart_recovers_a_linked_workflow_result_before_its_native_wake(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path)
    definition = (
        await harness.operation(
            "workflow.save",
            workflow=workflow("recover-result", [step("do", "leader", MODEL, after=[])]),
        )
    )["workflow"]
    work = await task(harness, factory, factory["leader_member_id"], workflow_id=definition["id"])
    await harness.request()
    current = (await harness.operation("task.get", id=work["id"]))["task"]
    assert (current["state"], current["reason"]) == ("blocked", "workflow")
    records = harness.service.records()
    card = await records.get_card(current["workflow_card_id"])
    runtime = card["runtime"]
    runtime["steps"]["do"]["status"] = "done"
    await records.change(
        card["id"],
        {"state": "done", "summary": "test-owned-durable-workflow-result", "runtime": runtime},
        actor="factory",
        event="test_terminal_before_wake",
    )
    await restart(harness)
    request = await harness.request()
    assert request["kwargs"]["test_context"]["agent_id"] == "leader"
    assert "test-owned-durable-workflow-result" in str(request["messages"])
    run = harness.runtime.chat_run_manager.active_runs()[0]
    request["answer"].set_result(
        {
            "content": "test-owned-workflow-seen",
            "tool_calls": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
    )
    await harness.finish(run)
    await harness.settle()
    count = len(harness.model.requests)
    await harness.service.native.reconcile()
    await harness.settle()
    assert len(harness.model.requests) == count == 2
    assert (await harness.operation("task.get", id=work["id"]))["task"]["state"] == "blocked"


@pytest.mark.parametrize("completed_review", [False, True])
async def test_review_without_decision_can_resume_after_restart(
    native_factory_runtime: NativeFactoryRuntime,
    tmp_path: Path,
    completed_review: bool,
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path)
    reviewer = await member(harness, factory, "reviewer")
    work = await task(
        harness,
        factory,
        factory["leader_member_id"],
        reviewer_id=reviewer["id"],
        user_approval=True,
    )
    request = await harness.request()
    complete(harness, work, request)
    review = await harness.request()
    review["answer"].set_result({"content": "test-owned-review-progress", "tool_calls": None})
    await harness.settle()
    current = (await harness.operation("task.get", id=work["id"]))["task"]
    assert (current["state"], current["reason"]) == ("blocked", "review_input")
    submission = current["submission"]
    if completed_review:
        # Simulate the old persisted projection, repaired without relying on a callback.
        await harness.service.records().native.change_task(
            work["id"], {"state": "review", "reason": "review"}, actor="factory"
        )
    await restart(harness)
    await harness.settle()
    current = (await harness.operation("task.get", id=work["id"]))["task"]
    assert current["state"] == "blocked"
    await harness.operation("task.resume", id=current["id"], expected_revision=current["revision"])
    request = await harness.request()
    assert request["kwargs"]["test_context"]["agent_id"] == "reviewer"
    harness.model.tool(
        "factory_tasks", {"action": "review", "id": work["id"], "approve": True}, request=request
    )
    await harness.settle()
    current = (await harness.operation("task.get", id=work["id"]))["task"]
    assert current["state"] == "review"
    assert current["submission"]["reviewer_approved"]
    assert not current["submission"]["user_approved"]
    assert current["submission"]["agreement_revision"] == submission["agreement_revision"]
    done = await harness.operation(
        "task.review", id=current["id"], expected_revision=current["revision"], approve=True
    )
    assert done["task"]["state"] == "done"


async def test_deleted_linked_card_does_not_poison_mutations_or_restart(
    native_factory_runtime: NativeFactoryRuntime,
    tmp_path: Path,
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path)
    native = harness.service.records().native
    work = await task(harness, factory, "")
    card = await harness.service.records().create_card(
        "product", "Linked", "Scope", origin="operator"
    )
    await harness.service.records().change(
        card["id"], {"state": "discarded"}, actor="user", event="discarded"
    )
    work = await native.change_task(
        work["id"], {"state": "blocked", "reason": "workflow", "workflow_card_id": card["id"]}
    )
    with pytest.raises(ExtensionOperationError) as refused:
        await harness.operation("card.delete", id=card["id"])
    assert refused.value.code == "card_in_use"
    assert not (await harness.service.records().get_card(card["id"]))["runtime"].get("deleting")
    # Reproduce a database left by the previous version, bypassing the new guard.
    await harness.service.records().database.write_async(
        lambda connection: (
            connection.execute("DELETE FROM cards WHERE id = ?", (card["id"],)) and None
        )
    )
    await harness.operation(
        "task.update", id=work["id"], expected_revision=work["revision"], title="Edited"
    )
    current = await native.get_task(work["id"])
    assert (current["state"], current["reason"]) == ("blocked", "workflow_missing")
    assert current["workflow_card_id"] == ""
    # Leave another stale reference for startup rather than the mutation path.
    await native.change_task(
        work["id"], {"state": "blocked", "reason": "workflow", "workflow_card_id": card["id"]}
    )
    await restart(harness)
    current = (await harness.operation("task.get", id=work["id"]))["task"]
    assert current["reason"] == "workflow_missing"
    later = await task(harness, factory, factory["leader_member_id"])
    request = await harness.request()
    complete(harness, later, request)
    await harness.settle()
    assert (await harness.operation("task.get", id=later["id"]))["task"]["state"] == "done"
