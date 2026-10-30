"""Live approval identity, suppression, cancellation, and Tool scheduling."""

import asyncio
from typing import Any

import pytest

from core.permissions import (
    ApprovalRequirement,
    BoundaryContext,
    PermissionBoundary,
    normalize_permission_rules,
)
from core.runs import ChatRunManager, Run, RunKind
from core.tools import ToolCall, ToolExecutor, ToolRegistry, tool_success
from tests.core.tools.tools_test_support import make_execution_config


@pytest.mark.asyncio
async def test_approval_rejection_is_run_local_and_cancellation_removes_request() -> None:
    manager = ChatRunManager()
    run = Run(run_id="r1", agent_id="a", session_id="s")
    changes: list[dict[str, Any]] = []
    manager.add_approvals_changed_callback(changes.append)
    request = asyncio.create_task(
        manager.request_approval(run, {"tool_call_id": "c"}, ["read"], ["read"])
    )
    await asyncio.sleep(0)
    snapshot = manager.approvals_snapshot()
    assert len(snapshot["requests"]) == 1
    assert manager.resolve_approval(snapshot["requests"][0]["id"], "reject")
    assert not await request
    assert not await manager.request_approval(run, {}, ["read"], ["read"])
    waiting = asyncio.create_task(manager.request_approval(run, {}, ["other"], ["other"]))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert manager.approvals_snapshot()["requests"] == []
    assert [snapshot["revision"] for snapshot in changes] == [1, 2, 3, 4]
    unattended = Run(run_id="r2", agent_id="a", session_id="s", run_kind=RunKind.REFLECTION)
    assert not await manager.request_approval(unattended, {}, ["other"], ["other"])


@pytest.mark.asyncio
async def test_waiting_approval_releases_execution_slots() -> None:
    manager = ChatRunManager()
    run = Run(run_id="r", agent_id="a", session_id="s")
    started = asyncio.Event()
    sibling_ran = asyncio.Event()
    registry = ToolRegistry()

    async def approval(context, reason, requirements, arguments, cwd):
        started.set()
        return await manager.request_approval(
            run, {"tool_call_id": context.tool_call_id}, ["x"], ["x"]
        )

    async def preflight(context, arguments):
        if arguments["wait"]:
            assert await context.request_approval(
                "review", [ApprovalRequirement.review("x")], arguments=arguments
            )

    def handler(context, arguments):
        if not arguments["wait"]:
            sibling_ran.set()
        return tool_success({})

    registry.register(
        "probe",
        "Probe.",
        {
            "type": "object",
            "properties": {"wait": {"type": "boolean"}},
            "required": ["wait"],
            "additionalProperties": False,
        },
        handler,
        preflight=preflight,
    )
    config = make_execution_config(approval_hook=approval)
    batch = ToolExecutor(registry, per_run_limit=1, global_limit=1).start_batch(config)
    batch.add(ToolCall(id="one", name="probe", arguments={"wait": True}))
    await started.wait()
    batch.add(ToolCall(id="two", name="probe", arguments={"wait": False}))
    await asyncio.wait_for(sibling_ran.wait(), timeout=1)
    snapshot = manager.approvals_snapshot()
    assert manager.resolve_approval(snapshot["requests"][0]["id"], "allow")
    results = await batch.results()
    assert all(item["ok"] for item in results)


@pytest.mark.asyncio
async def test_rejected_subtree_suppresses_descendant_read_and_write(tmp_path) -> None:
    boundary = PermissionBoundary.build(
        normalize_permission_rules({"read": "work_area"}),
        BoundaryContext(workdir=tmp_path / "work", home=tmp_path, data_dir=tmp_path / "data"),
    )
    subtree = ApprovalRequirement.path(
        boundary, tmp_path / "outside", "read", subtree=True, filtered=True
    )
    child = ApprovalRequirement.path(boundary, tmp_path / "outside" / "file", "write")
    manager = ChatRunManager()
    run = Run(run_id="scope", agent_id="a", session_id="s")
    pending = asyncio.create_task(
        manager.request_approval(run, {}, subtree.suppression_keys(), [subtree.suppression_key])
    )
    await asyncio.sleep(0)
    request = manager.approvals_snapshot()["requests"][0]
    assert manager.resolve_approval(request["id"], "reject")
    assert not await pending
    assert not await manager.request_approval(
        run, {}, child.suppression_keys(), [child.suppression_key]
    )
    assert manager.approvals_snapshot()["requests"] == []


@pytest.mark.asyncio
async def test_rejection_settles_other_pending_calls_for_same_access() -> None:
    manager = ChatRunManager()
    run = Run(run_id="r", agent_id="a", session_id="s")
    tasks = [
        asyncio.create_task(manager.request_approval(run, {}, [key], [key]))
        for key in ("same", "same", "other")
    ]
    await asyncio.sleep(0)
    snapshot = manager.approvals_snapshot()
    assert len(snapshot["requests"]) == 3
    assert manager.resolve_approval(snapshot["requests"][0]["id"], "reject")
    assert not await tasks[0]
    assert not await tasks[1]
    assert not tasks[2].done()
    assert len(manager.approvals_snapshot()["requests"]) == 1
    assert manager.approvals_snapshot()["revision"] == 4
    tasks[2].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[2]


@pytest.mark.asyncio
async def test_cancelling_one_waiting_approval_does_not_cancel_sibling() -> None:
    manager = ChatRunManager()
    run = Run(run_id="r", agent_id="a", session_id="s")
    sibling_started, sibling_finish = asyncio.Event(), asyncio.Event()
    registry = ToolRegistry()

    async def approval(context, reason, requirements, arguments, cwd):
        return await manager.request_approval(run, {}, ["x"], ["x"])

    async def handler(context, arguments):
        if arguments["wait"]:
            await context.request_approval(
                "review", [ApprovalRequirement.review("x")], arguments=arguments
            )
        else:
            sibling_started.set()
            await sibling_finish.wait()
        return tool_success({})

    registry.register(
        "probe",
        "Probe.",
        {
            "type": "object",
            "properties": {"wait": {"type": "boolean"}},
            "required": ["wait"],
            "additionalProperties": False,
        },
        handler,
    )
    config = make_execution_config(
        approval_hook=approval,
        cancellation_hook=lambda: run.cancel_requested,
        tool_call_cancel_registrar=run.register_tool_cancel,
        tool_call_cancel_check=run.tool_call_cancelled,
    )
    batch = ToolExecutor(registry).start_batch(config)
    run.begin_tool_call("one")
    run.begin_tool_call("two")
    batch.add(ToolCall(id="one", name="probe", arguments={"wait": True}))
    batch.add(ToolCall(id="two", name="probe", arguments={"wait": False}))
    await sibling_started.wait()
    while not manager.approvals_snapshot()["requests"]:
        await asyncio.sleep(0)
    assert run.cancel_tool_call("one")
    sibling_finish.set()
    results = await batch.results()
    assert results[0]["error"]["code"] == "cancelled_by_user"
    assert results[1]["ok"] and not run.cancel_requested
    assert manager.approvals_snapshot()["requests"] == []


@pytest.mark.asyncio
async def test_rejected_file_blocks_later_recursive_and_wildcard_access(tmp_path) -> None:
    boundary = PermissionBoundary.build(
        normalize_permission_rules({"read": "work_area"}),
        BoundaryContext(workdir=tmp_path / "work", home=tmp_path, data_dir=tmp_path / "data"),
    )
    folder = tmp_path / "private"
    child = ApprovalRequirement.path(boundary, folder / "a.txt", "read")
    recursive = ApprovalRequirement.path(boundary, folder, "read", subtree=True, filtered=True)
    wildcard = ApprovalRequirement.path(boundary, folder, "read", glob="*.txt")
    manager = ChatRunManager()
    run = Run(run_id="r", agent_id="a", session_id="s")
    pending = asyncio.create_task(
        manager.request_approval(run, {}, child.suppression_keys(), [child.suppression_key])
    )
    await asyncio.sleep(0)
    assert manager.resolve_approval(manager.approvals_snapshot()["requests"][0]["id"], "reject")
    assert not await pending
    for wider in (recursive, wildcard):
        assert not await manager.request_approval(
            run, {}, wider.suppression_keys(), [wider.suppression_key]
        )
    assert not manager.approvals_snapshot()["requests"]


@pytest.mark.asyncio
async def test_rejected_host_matches_dns_and_idna_representations() -> None:
    manager = ChatRunManager()
    run = Run(run_id="hosts", agent_id="a", session_id="s")
    first = ApprovalRequirement.host("BÜCHER.example.", "connect")
    equivalent = ApprovalRequirement.host("xn--bcher-kva.example", "connect")
    pending = asyncio.create_task(
        manager.request_approval(run, {}, first.suppression_keys(), [first.suppression_key])
    )
    await asyncio.sleep(0)
    assert manager.resolve_approval(manager.approvals_snapshot()["requests"][0]["id"], "reject")
    assert not await pending
    assert not await manager.request_approval(
        run, {}, equivalent.suppression_keys(), [equivalent.suppression_key]
    )
    assert not manager.approvals_snapshot()["requests"]


@pytest.mark.asyncio
async def test_cancellation_while_approved_call_reacquires_slot_is_local() -> None:
    manager = ChatRunManager()
    run = Run(run_id="r", agent_id="a", session_id="s")
    sibling_started, sibling_finish = asyncio.Event(), asyncio.Event()
    registry = ToolRegistry()
    effects: list[str] = []

    async def approval(context, reason, requirements, arguments, cwd):
        return await manager.request_approval(run, {}, ["x"], ["x"])

    async def handler(context, arguments):
        if arguments["wait"]:
            await context.request_approval(
                "review", [ApprovalRequirement.review("x")], arguments=arguments
            )
            await context.revalidate_permissions()
            effects.append("approved")
        else:
            sibling_started.set()
            await sibling_finish.wait()
            effects.append("sibling")
        return tool_success({})

    registry.register(
        "probe",
        "Probe.",
        {
            "type": "object",
            "properties": {"wait": {"type": "boolean"}},
            "required": ["wait"],
            "additionalProperties": False,
        },
        handler,
    )
    config = make_execution_config(
        approval_hook=approval,
        cancellation_hook=lambda: run.cancel_requested,
        tool_call_cancel_registrar=run.register_tool_cancel,
        tool_call_cancel_check=run.tool_call_cancelled,
    )
    batch = ToolExecutor(registry, per_run_limit=1, global_limit=1).start_batch(config)
    run.begin_tool_call("one")
    run.begin_tool_call("two")
    batch.add(ToolCall(id="one", name="probe", arguments={"wait": True}))
    while not manager.approvals_snapshot()["requests"]:
        await asyncio.sleep(0)
    batch.add(ToolCall(id="two", name="probe", arguments={"wait": False}))
    await sibling_started.wait()
    assert manager.resolve_approval(manager.approvals_snapshot()["requests"][0]["id"], "allow")
    await asyncio.sleep(0)
    assert run.cancel_tool_call("one")
    result = await asyncio.wait_for(batch.result(0), timeout=1)
    assert result["error"]["code"] == "cancelled_by_user"
    assert not sibling_finish.is_set() and not run.cancel_requested
    sibling_finish.set()
    assert (await batch.result(1))["ok"]
    assert effects == ["sibling"]
