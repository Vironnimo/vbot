"""Budget and capacity admission through the real Factory and owned Run host."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from core.extensions import SessionRequestContext
from core.extensions.operations import ExtensionOperationError
from core.permissions import (
    BoundaryContext,
    PermissionBoundary,
    PermissionCeiling,
    normalize_permission_rules,
)
from core.runs import Run
from core.sessions import SessionAddress
from tests.core.runtime.runtime_test_support import tool_context
from tests.resources.extensions.factory.test_factory_execution import child_step, step, workflow
from tests.resources.extensions.factory.test_factory_native_integration import (
    MODEL,
    NativeFactoryRuntime,
)
from tests.resources.extensions.factory.test_factory_native_integration import (
    native_factory_runtime as native_factory_runtime,
)

type Json = dict[str, Any]
pytestmark = pytest.mark.asyncio


async def establish(harness: NativeFactoryRuntime, directory: Path, **settings: Any) -> Json:
    repository = directory / "repository"
    repository.mkdir()
    harness.runtime.projects.create("product", "Product", repository, sources=[])
    harness.runtime.agents.create("leader", model=MODEL, librarian_enabled=False)
    return cast(
        Json,
        (
            await harness.operation(
                "factory.create", project_id="product", leader_agent_id="leader", **settings
            )
        )["factory"],
    )


async def drain_factory_work(harness: NativeFactoryRuntime) -> None:
    """Drain scheduling work while an explicitly controlled Model may be waiting."""
    while tasks := [task for task in harness.service._tasks if not task.done()]:
        await asyncio.gather(*tasks)


def context(harness: NativeFactoryRuntime, run: Run) -> SessionRequestContext:
    binding = harness.runtime.chat_sessions.temporary_binding(
        SessionAddress(run.project_id, run.agent_id, run.session_id)
    )
    assert binding is not None
    return SessionRequestContext(binding, run.id, run.agent_id, run.session_id, run.execution_owner)


async def test_native_tool_reservations_share_one_ceiling_and_deduplicate_receipts(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path
) -> None:
    harness = native_factory_runtime
    factory = await establish(
        harness, tmp_path, limits={"tool_calls": 2}, config={"denied_tools": ["bash"]}
    )
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Bounded calls",
            objective="Do work",
            assignee_id=factory["leader_member_id"],
        )
    )["task"]
    await harness.request()
    run = harness.runtime.chat_run_manager.active_runs()[0]
    root = context(harness, run)
    # The owner callback receives descendants with their own Session and Run,
    # while retaining the root's canonical owned binding.
    descendant = replace(root, run_id="descendant-run", session_id="descendant-session")
    assert (
        await harness.service.before_tool_call(
            descendant, tool_name="bash", tool_call_id="forbidden-descendant-call"
        )
        is not None
    )
    assert (
        await harness.service.before_tool_call(
            root, tool_name="read_file", tool_call_id="root-call"
        )
        is None
    )
    assert (
        await harness.service.before_tool_call(
            root, tool_name="read_file", tool_call_id="root-call"
        )
        is None
    )
    admitted = await asyncio.gather(
        harness.service.before_tool_call(
            descendant, tool_name="read_file", tool_call_id="child-call"
        ),
        harness.service.before_tool_call(root, tool_name="read_file", tool_call_id="excess-call"),
    )
    assert sum(result is None for result in admitted) == 1
    await harness.settle()
    detail = await harness.operation("task.get", id=task["id"])
    assert detail["task"]["state"] == "blocked"
    assert detail["task"]["reason"] == "budget_exhausted"
    assert detail["task"]["consumption"]["tool_calls"] == 2
    assert len(harness.model.requests) == 1


@pytest.mark.parametrize("scope", ["member", "factory"])
async def test_policy_edit_preserves_stopped_native_work_until_explicit_resume(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path, scope: str
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path)
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Stopped policy edit",
            objective="Continue later",
            assignee_id=factory["leader_member_id"],
        )
    )["task"]
    request = await harness.request()
    if scope == "member":
        request["answer"].set_result(
            {"content": "Retained progress", "usage": {"input_tokens": 10, "output_tokens": 5}}
        )
        await harness.settle()
    else:
        task = (await harness.operation("task.get", id=task["id"]))["task"]
        await harness.operation("task.pause", id=task["id"], expected_revision=task["revision"])
        await harness.settle()
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    before = task["consumption"]["runs"]
    record = await getattr(harness.service.native.store, f"get_{scope}")(
        factory["leader_member_id"] if scope == "member" else factory["id"]
    )
    await harness.operation(
        f"{scope}.update",
        id=record["id"],
        expected_revision=record["revision"],
        config={"denied_tools": ["bash"]},
        **(
            {"instructions": "current-policy-sentinel"}
            if scope == "member"
            else {"remit": "current-policy-sentinel"}
        ),
    )
    await drain_factory_work(harness)
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    assert task["state"] == ("blocked" if scope == "member" else "paused")
    assert len(harness.model.requests) == 1
    await harness.operation("task.resume", id=task["id"], expected_revision=task["revision"])
    resumed = await harness.request()
    assert "current-policy-sentinel" in str(resumed["messages"])
    assert "bash" not in {tool["name"] for tool in resumed["kwargs"].get("tools", [])}
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    assert task["consumption"]["runs"] == before + 1
    await harness.operation("task.pause", id=task["id"], expected_revision=task["revision"])
    await harness.settle()


@pytest.mark.parametrize("gate", ["approval", "blocked"])
async def test_associated_workflow_policy_edit_preserves_passive_gate_and_refreshes_binding(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path, gate: str
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path)
    saved = (
        await harness.operation(
            "workflow.save",
            workflow=workflow(
                "passive", [step("do", "leader", MODEL, after=[], approval=gate == "approval")]
            ),
        )
    )["workflow"]
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Passive Workflow",
            objective="Keep its gate",
            assignee_id=factory["leader_member_id"],
            workflow_id=saved["id"],
        )
    )["task"]
    old_session = None
    if gate == "blocked":
        request = await harness.request()
        old_session = harness.runtime.chat_run_manager.active_runs()[0].session_id
        harness.model.tool("factory_report_blocked", {"question": "Which choice?"}, request=request)
    await harness.settle()
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    card_id = task["workflow_card_id"]
    member = await harness.service.native.store.get_member(factory["leader_member_id"])
    count = len(harness.model.requests)
    await harness.operation(
        "member.update",
        id=member["id"],
        expected_revision=member["revision"],
        instructions="passive-policy-sentinel",
        config={"denied_tools": ["bash"], "allowed_skills": []},
    )
    ceiling = PermissionBoundary.build(
        normalize_permission_rules({"write": "scratch"}),
        BoundaryContext(workdir=tmp_path, home=tmp_path, data_dir=tmp_path / "data"),
    ).to_ceiling_dict(source="extension")
    factory = await harness.service.native.store.get_factory(factory["id"])
    await harness.operation(
        "factory.update",
        id=factory["id"],
        expected_revision=factory["revision"],
        config={"permission_ceiling": ceiling},
    )
    await drain_factory_work(harness)
    assert len(harness.model.requests) == count
    card = (await harness.operation("card.get", id=card_id))["card"]
    assert card["runtime"]["steps"]["do"]["status"] == (
        "approval" if gate == "approval" else "waiting"
    )
    if gate == "approval":
        await harness.operation(
            "card.accept", id=card_id, expected_revision=card["revision"], step="do"
        )
    else:
        await harness.operation("card.answer", id=card_id, step="do", text="Use the current policy")
    request = await harness.request()
    run = harness.runtime.chat_run_manager.active_runs()[0]
    assert run.session_id != old_session
    binding = harness.runtime.chat_sessions.temporary_binding(
        SessionAddress(run.project_id, run.agent_id, run.session_id)
    )
    assert binding is not None
    assert binding.config["allowed_skills"] == []
    resolved = harness.runtime.agent_resolver.resolve_temporary_agent(
        binding.address, generation_id=binding.generation_id
    )
    assert resolved.permission_ceiling == PermissionCeiling.from_ceiling_dict(ceiling)
    assert "passive-policy-sentinel" in str(request["messages"])
    assert "bash" not in {tool["name"] for tool in request["kwargs"].get("tools", [])}
    descendant = replace(
        context(harness, run), run_id="owned-descendant", session_id="owned-descendant-session"
    )
    assert (
        await harness.service.before_tool_call(descendant, tool_name="bash", tool_call_id="denied")
        is not None
    )
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    await harness.operation("task.pause", id=task["id"], expected_revision=task["revision"])
    await harness.settle()


@pytest.mark.parametrize("reference", ["missing", "foreign"])
async def test_invalid_persisted_workflow_does_not_starve_unrelated_native_work(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path, reference: str
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path)
    if reference == "foreign":
        repository = tmp_path / "foreign"
        repository.mkdir()
        harness.runtime.projects.create("foreign", "Foreign", repository, sources=[])
        await harness.operation(
            "workflow.save",
            workflow=workflow(
                "foreign", [step("do", "leader", MODEL, after=[])], project_id="foreign"
            ),
        )
    async with harness.service.lock:
        stale = await harness.service.native.store.create_task(
            factory["id"],
            "Stale selection",
            "Retained persisted reference",
            assignee_id=factory["leader_member_id"],
            workflow_id=reference,
            priority=100,
        )
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Independent work",
            objective="Still run",
            assignee_id=factory["leader_member_id"],
        )
    )["task"]
    await harness.request()
    stale = (await harness.operation("task.get", id=stale["id"]))["task"]
    assert (stale["state"], stale["reason"]) == ("blocked", "workflow_unavailable")
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    assert task["state"] == "working"
    await harness.operation("task.pause", id=task["id"], expected_revision=task["revision"])
    await harness.settle()


async def test_workflow_association_recovers_persisted_card_before_link_commit(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path)
    saved = (
        await harness.operation(
            "workflow.save", workflow=workflow("recover", [step("do", "leader", MODEL, after=[])])
        )
    )["workflow"]
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Recover association",
            objective="Reuse durable Card",
            workflow_id=saved["id"],
        )
    )["task"]
    async with harness.service.lock:
        task = await harness.service.native.store.change_task(
            task["id"], {"assignee_id": factory["leader_member_id"], "state": "ready"}, actor="test"
        )

        async def interrupted_link(*args: Any, **kwargs: Any) -> Json:
            raise OSError("test-owned interruption before association commit")

        with monkeypatch.context() as patch:
            patch.setattr(harness.service.native.store, "change_task", interrupted_link)
            with pytest.raises(OSError):
                await harness.service.native.workflows.start(task, factory)
        cards = await harness.service.records().list_cards()
        assert len(cards) == 1
        card = cards[0]
        assert not (await harness.service.native.store.get_task(task["id"]))["workflow_card_id"]
        harness.service.request_schedule()
    await harness.request()
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    assert task["workflow_card_id"] == card["id"]
    assert len(await harness.service.records().list_cards()) == 1
    assert len(harness.model.requests) == 1
    await harness.operation("task.pause", id=task["id"], expected_revision=task["revision"])
    await harness.settle()


async def test_native_run_allocation_survives_pause_and_reassignment(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path)
    harness.runtime.agents.create("successor", model=MODEL, librarian_enabled=False)
    successor = (
        await harness.operation("member.add", factory_id=factory["id"], agent_id="successor")
    )["member"]
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="One Run",
            objective="Retain progress",
            assignee_id=factory["leader_member_id"],
            limits={"runs": 1},
        )
    )["task"]
    await harness.request()
    current = (await harness.operation("task.get", id=task["id"]))["task"]
    paused = (
        await harness.operation("task.pause", id=task["id"], expected_revision=current["revision"])
    )["task"]
    await harness.settle()
    await harness.operation(
        "task.assign",
        id=task["id"],
        expected_revision=paused["revision"],
        assignee_id=successor["id"],
    )
    await harness.settle()
    detail = await harness.operation("task.get", id=task["id"])
    assert detail["task"]["assignee_id"] == successor["id"]
    assert detail["task"]["reason"] == "budget_exhausted"
    assert detail["task"]["consumption"]["runs"] == 1
    assert len(harness.model.requests) == 1


async def test_settled_tokens_stop_the_next_physical_model_request(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path)
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Token allocation",
            objective="Inspect the task then continue",
            assignee_id=factory["leader_member_id"],
            limits={"tokens": 10},
        )
    )["task"]
    request = await harness.request()
    run = harness.runtime.chat_run_manager.active_runs()[0]
    harness.model.tool("factory_tasks", {"action": "get", "id": task["id"]}, request=request)
    # The first physical request is allowed to overshoot the settled allocation.
    # Its recorded Usage must prohibit the next physical request.
    async with asyncio.timeout(3):
        await asyncio.gather(run.wait(), return_exceptions=True)
        await harness.settle()
    detail = await harness.operation("task.get", id=task["id"])
    assert detail["task"]["reason"] == "budget_exhausted"
    assert detail["task"]["consumption"]["tokens"] >= 15
    assert len(harness.model.requests) == 1


async def test_linked_workflow_checks_the_parent_allocation_before_creating_a_run(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path)
    parent = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Parent",
            objective="Own the allocation",
            limits={"runs": 1},
        )
    )["task"]
    child = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="First child",
            objective="Use one Run",
            parent_id=parent["id"],
            assignee_id=factory["leader_member_id"],
        )
    )["task"]
    await harness.request()
    current = (await harness.operation("task.get", id=child["id"]))["task"]
    await harness.operation("task.pause", id=child["id"], expected_revision=current["revision"])
    await harness.settle()
    definition = workflow("bounded", [step("do", "leader", MODEL, after=[])])
    saved = (await harness.operation("workflow.save", workflow=definition))["workflow"]
    linked = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Workflow child",
            objective="Use Workflow",
            parent_id=parent["id"],
            assignee_id=factory["leader_member_id"],
            workflow_id=saved["id"],
        )
    )["task"]
    await harness.settle()
    detail = await harness.operation("task.get", id=linked["id"])
    assert detail["task"]["reason"] == "budget_exhausted"
    assert len(harness.model.requests) == 1
    assert harness.runtime.chat_run_manager.active_runs() == []
    parent_detail = await harness.operation("task.get", id=parent["id"])
    assert parent_detail["task"]["consumption"]["runs"] == 1


async def test_factory_capacity_holds_other_members_until_the_running_task_pauses(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path, capacity=1)
    harness.runtime.agents.create("peer", model=MODEL, librarian_enabled=False)
    peer = (await harness.operation("member.add", factory_id=factory["id"], agent_id="peer"))[
        "member"
    ]
    first = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="First",
            objective="Occupy capacity",
            assignee_id=factory["leader_member_id"],
        )
    )["task"]
    await harness.request()
    second = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Second",
            objective="Wait for capacity",
            assignee_id=peer["id"],
        )
    )["task"]
    await drain_factory_work(harness)
    assert (await harness.operation("task.get", id=second["id"]))["task"]["state"] == "ready"
    assert len(harness.model.requests) == 1
    first = (await harness.operation("task.get", id=first["id"]))["task"]
    await harness.operation("task.pause", id=first["id"], expected_revision=first["revision"])
    await harness.request()
    assert len(harness.model.requests) == 2
    second = (await harness.operation("task.get", id=second["id"]))["task"]
    assert second["state"] == "working"
    await harness.operation("task.pause", id=second["id"], expected_revision=second["revision"])
    await harness.settle()


async def test_linked_workflow_lifecycle_preserves_capacity_and_fences_old_ownership(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path, capacity=1)
    harness.runtime.agents.create("successor", model=MODEL, librarian_enabled=False)
    peer = (await harness.operation("member.add", factory_id=factory["id"], agent_id="successor"))[
        "member"
    ]
    definition = workflow("selected", [step("do", "leader", MODEL, after=[])])
    saved = (await harness.operation("workflow.save", workflow=definition))["workflow"]
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Selected Workflow",
            objective="Run then verify the selected Workflow",
            workflow_id=saved["id"],
            assignee_id=factory["leader_member_id"],
        )
    )["task"]
    await harness.request()
    original_run = harness.runtime.chat_run_manager.active_runs()[0]
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    original_card = task["workflow_card_id"]
    assert original_card
    waiting = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Waiting peer",
            objective="Wait for shared Factory capacity",
            assignee_id=peer["id"],
        )
    )["task"]
    await drain_factory_work(harness)
    assert (await harness.operation("task.get", id=waiting["id"]))["task"]["state"] == "ready"
    assert len(harness.model.requests) == 1
    await harness.operation("task.pause", id=task["id"], expected_revision=task["revision"])
    await harness.request()  # released Workflow capacity admits the waiting peer
    waiting = (await harness.operation("task.get", id=waiting["id"]))["task"]
    await harness.operation("task.pause", id=waiting["id"], expected_revision=waiting["revision"])
    await harness.settle()
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    assert task["state"] == "paused"
    assert task["workflow_card_id"] == original_card
    assert len(harness.model.requests) == 2
    await harness.operation("task.resume", id=task["id"], expected_revision=task["revision"])
    await harness.request()
    resumed = harness.runtime.chat_run_manager.active_runs()[0]
    assert resumed.session_id == original_run.session_id
    assert resumed.id != original_run.id
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    assert task["workflow_card_id"] == original_card
    old_context = replace(
        tool_context("factory_submit_work", tmp_path, agent_id=resumed.agent_id),
        session_id=resumed.session_id,
        run_id=resumed.id,
        execution_owner=resumed.execution_owner,
        session_tool_grants=("factory_submit_work",),
        request_turn_end_hook=lambda _reason: None,
    )
    await harness.operation(
        "task.assign", id=task["id"], expected_revision=task["revision"], assignee_id=peer["id"]
    )
    new_request = await harness.request()
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    assert task["workflow_card_id"] != original_card
    refused = await harness.runtime.tools.dispatch(
        old_context, {"summary": "obsolete-workflow-result", "result": "obsolete-evidence"}
    )
    assert refused["error"]["code"] == "wrong_state"
    harness.model.tool(
        "factory_submit_work",
        {"summary": "accepted-workflow-result", "result": "workflow-evidence"},
        request=new_request,
    )
    owner_request = await harness.request()
    assert "accepted-workflow-result" in str(owner_request["messages"])
    assert "obsolete-evidence" not in str(owner_request["messages"])
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    assert task["state"] == "working"
    assert task["assignee_id"] == peer["id"]
    owner_run = next(
        run for run in harness.runtime.chat_run_manager.active_runs() if run.agent_id == "successor"
    )
    harness.model.tool(
        "factory_tasks",
        {
            "action": "complete",
            "id": task["id"],
            "outcome": "Verified integration",
            "evidence": "Checked workflow-evidence",
        },
        request=owner_request,
    )
    await harness.finish(owner_run)
    await harness.settle()
    assert (await harness.operation("task.get", id=task["id"]))["task"]["state"] == "done"


async def test_physical_model_admission_enforces_the_factory_ceiling(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path, config={"allowed_models": [MODEL]})
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Model ceiling",
            objective="Stay in scope",
            assignee_id=factory["leader_member_id"],
        )
    )["task"]
    await harness.request()
    run = harness.runtime.chat_run_manager.active_runs()[0]
    admitted = context(harness, run)
    await harness.service.before_model_request(replace(admitted, model=f"{MODEL}::api-key"))
    with pytest.raises(ExtensionOperationError) as refused:
        await harness.service.before_model_request(replace(admitted, model="other/disallowed"))
    assert refused.value.code == "model_unavailable"
    await harness.settle()
    assert (await harness.operation("task.get", id=task["id"]))["task"][
        "reason"
    ] == "model_unavailable"
    assert len(harness.model.requests) == 1


async def test_nowait_workflow_descendant_retains_authority_until_native_task_completes(
    native_factory_runtime: NativeFactoryRuntime, tmp_path: Path
) -> None:
    harness = native_factory_runtime
    factory = await establish(harness, tmp_path, capacity=3)
    member = await harness.service.native.store.get_member(factory["leader_member_id"])
    await harness.operation(
        "member.update", id=member["id"], expected_revision=member["revision"], capacity=3
    )
    await harness.operation(
        "workflow.save", workflow=workflow("child", [step("do", "leader", MODEL, after=[])])
    )
    root = (
        await harness.operation(
            "workflow.save",
            workflow=workflow("root", [child_step("delegate", "child", after=[], wait=False)]),
        )
    )["workflow"]
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Nonwaiting Workflow child",
            objective="Keep delegated work under task authority",
            workflow_id=root["id"],
            assignee_id=member["id"],
        )
    )["task"]
    requests = [await harness.request(), await harness.request()]
    owner_request = next(
        request
        for request in requests
        if "factory_tasks" in {tool["name"] for tool in request["kwargs"].get("tools", [])}
    )
    runs = harness.runtime.chat_run_manager.active_runs()
    owner_run = next(run for run in runs if run.agent_id == "leader")
    child_run = next(run for run in runs if run.id != owner_run.id)
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    card = (await harness.operation("card.get", id=task["workflow_card_id"]))["card"]
    assert card["state"] == "done"
    assert task["state"] == "working"
    child_context = context(harness, child_run)
    await harness.service.before_model_request(replace(child_context, model=MODEL))
    assert (
        await harness.service.before_tool_call(
            child_context, tool_name="read_file", tool_call_id="admitted-after-root-completion"
        )
        is None
    )
    graph_ids = {item["id"] for item in await harness.service.records().list_cards()}
    results = task["runtime"]["workflow_results"]
    await harness.operation("task.pause", id=task["id"], expected_revision=task["revision"])
    await harness.settle()
    assert child_run.status.value == "cancelled"
    assert owner_run.status.value == "cancelled"
    paused = (await harness.operation("task.get", id=task["id"]))["task"]
    assert paused["state"] == "paused"
    assert harness.runtime.chat_run_manager.active_runs() == []
    await harness.operation("task.resume", id=task["id"], expected_revision=paused["revision"])
    resumed_requests = [await harness.request(), await harness.request()]
    owner_request = next(
        request
        for request in resumed_requests
        if "factory_tasks" in {tool["name"] for tool in request["kwargs"].get("tools", [])}
    )
    resumed_runs = harness.runtime.chat_run_manager.active_runs()
    owner_run = next(run for run in resumed_runs if run.agent_id == "leader")
    resumed_child = next(run for run in resumed_runs if run.id != owner_run.id)
    assert resumed_child.id != child_run.id
    assert resumed_child.session_id == child_run.session_id
    assert {item["id"] for item in await harness.service.records().list_cards()} == graph_ids
    task = (await harness.operation("task.get", id=task["id"]))["task"]
    assert task["workflow_card_id"] == card["id"]
    assert task["runtime"]["workflow_results"] == results
    resumed_context = context(harness, resumed_child)
    await harness.service.before_model_request(replace(resumed_context, model=MODEL))
    assert (
        await harness.service.before_tool_call(
            resumed_context, tool_name="read_file", tool_call_id="admitted-after-explicit-resume"
        )
        is None
    )
    harness.model.tool(
        "factory_tasks",
        {
            "action": "complete",
            "id": task["id"],
            "outcome": "Finished",
            "evidence": "Verified the delegated work",
        },
        request=owner_request,
    )
    await harness.finish(owner_run)
    await harness.settle()
    cards = await harness.service.records().list_cards()
    descendant = next(item for item in cards if item["parent_id"] == card["id"])
    assert descendant["state"] == "discarded"
    assert child_run.status.value == "cancelled"
    assert resumed_child.status.value == "cancelled"
    assert (await harness.operation("task.get", id=task["id"]))["task"]["state"] == "done"


@pytest.mark.parametrize("source,denied", [("bash", False), ("subagent", False), ("bash", True)])
async def test_native_follower_tool_admission_matches_offered_tools_for_descendants(
    native_factory_runtime: NativeFactoryRuntime,
    tmp_path: Path,
    source: str,
    denied: bool,
) -> None:
    harness = native_factory_runtime
    factory = await establish(
        harness,
        tmp_path,
        config={
            "allowed_tools": [source],
            "denied_tools": ["background_tasks"] if denied else [],
        },
    )
    await harness.operation(
        "task.create",
        factory_id=factory["id"],
        title="Follower",
        objective="test-owned-policy",
        assignee_id=factory["leader_member_id"],
    )
    request = await harness.request()
    offered = {item["name"] for item in request["kwargs"]["tools"]}
    assert ("background_tasks" in offered) is not denied
    run = harness.runtime.chat_run_manager.active_runs()[0]
    root = context(harness, run)
    for index, current in enumerate(
        [root, replace(root, run_id="child-run", session_id="child-session")]
    ):
        result = await harness.service.before_tool_call(
            current, tool_name="background_tasks", tool_call_id=f"follower-{index}"
        )
        assert (result is not None) is denied
    request["answer"].set_result({"content": "test-owned-policy-end", "tool_calls": None})
    await harness.settle()
