"""Project communication reads and waits through production Tool dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from core.activity import SessionActivityState
from core.chat import ChatMessage
from core.chat.messages import MessageSender
from core.permissions import BoundaryContext, PermissionBoundary, normalize_permission_rules
from core.runs import ASSISTANT_OUTPUT_EVENT, RunKind
from core.sessions import SessionWriteLeaseScope
from core.subagents.links import link_session
from tests.core.chat.session_peers_support import PeerHarness
from tests.core.chat.session_peers_support import peer_harness as peer_harness

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("action", ["read", "wait"])
async def test_peers_can_observe_each_other_while_both_hold_caller_write_leases(
    peer_harness: PeerHarness, action: str
):
    h = peer_harness
    peer = h.create("reviewer", "review")
    peer.append(ChatMessage.user("Review the change"))
    barrier = asyncio.Barrier(2)

    async def observe(caller, target):
        with SessionWriteLeaseScope():
            async with h.sessions.write_lock(caller.address):
                await barrier.wait()
                arguments: dict[str, str | int] = {"action": action, "id": h.reference(target)}
                if action == "wait":
                    arguments["timeout"] = 0
                return await h.call(arguments, context=h.context(caller))

    results = await asyncio.wait_for(
        asyncio.gather(observe(h.caller, peer), observe(peer, h.caller)), timeout=2
    )
    assert all(result["ok"] for result in results), results


async def test_peers_are_project_scoped_generation_bound_and_working_first(
    peer_harness: PeerHarness,
):
    h = peer_harness
    active = h.create("reviewer", "same")
    active.append(ChatMessage.user("Old but active", timestamp=datetime(2026, 1, 1, tzinfo=UTC)))
    h.activity.states[active.address] = SessionActivityState("working")
    other_owner = h.create("builder", "same")
    other_owner.append(ChatMessage.user("Current builder work"))
    h.create("outsider", "outside", project="elsewhere").append(ChatMessage.user("OUTSIDE"))
    h.create("reviewer", "ephemeral", metadata={"ephemeral": True})
    h.create("reviewer", "system", run_kind=RunKind.SYSTEM)
    h.create("live-voice", "live")

    first = await h.call({"action": "list", "limit": 1})
    assert first["ok"], first
    [row] = first["data"]["items"]
    assert row["id"] == h.reference(active)
    assert row["activity"] == "working"
    next_page = await h.call({"action": "list", "cursor": first["data"]["next_cursor"]})
    assert [item["id"] for item in next_page["data"]["items"]] == [h.reference(other_owner)]
    assert h.reference(active) != h.reference(other_owner)

    old_ref = h.reference(other_owner)
    h.sessions.delete(other_owner.address)
    h.create("builder", "same").append(ChatMessage.user("REPLACEMENT"))
    old = await h.call({"action": "read", "id": old_ref})
    assert old["error"]["code"] == "session_unavailable"
    missing_project = await h.call(
        {"action": "list"}, context=replace(h.context(), working_project_id=None)
    )
    assert missing_project["error"]["code"] == "project_required"


async def test_peer_read_paginates_and_keeps_the_human_task(peer_harness: PeerHarness):
    h = peer_harness
    peer = h.create("reviewer", "review")
    peer.append_many(
        [
            ChatMessage.user("Human task"),
            ChatMessage.assistant(model="test", content="Progress", reasoning="PRIVATE REASONING"),
            ChatMessage.user(
                "Peer request",
                sender=MessageSender(h.reference(h.caller), "coder", "member", kind="session"),
            ),
            ChatMessage.assistant(model="test", content="Result " + "x" * 2500),
        ]
    )
    read = await h.call({"action": "read", "id": h.reference(peer), "limit": 2})
    assert read["ok"], read
    data = read["data"]
    assert data["task"]["text"] == "Human task"
    assert data["messages"][0]["source_session"] == h.reference(h.caller)
    assert data["messages"][1]["truncated"] is True
    assert len(data["messages"][1]["text"]) == 2000
    older = await h.call(
        {"action": "read", "id": h.reference(peer), "cursor": data["next_cursor"], "limit": 2}
    )
    assert [message["text"] for message in older["data"]["messages"]] == ["Human task", "Progress"]
    assert "next_cursor" not in older["data"]


async def test_peer_read_permissions_match_recall_agent_and_inherited_tree(
    peer_harness: PeerHarness,
):
    h = peer_harness
    own = h.create("coder", "own")
    sibling = h.create("coder", "sibling")
    stranger = h.create("reviewer", "other")
    for session in (own, sibling, stranger):
        session.append(ChatMessage.user(session.id))
    boundary_context = BoundaryContext(
        workdir=h.root / "work", home=h.root / "home", data_dir=h.root
    )
    restricted = PermissionBoundary.build(
        normalize_permission_rules({"read": "work_area"}), boundary_context
    )
    context = replace(h.context(), permissions=restricted)
    listing = await h.call({"action": "list"}, context=context)
    assert {item["id"] for item in listing["data"]["items"]} == {
        h.reference(own),
        h.reference(sibling),
    }
    forbidden = await h.call({"action": "read", "id": h.reference(stranger)}, context=context)
    assert forbidden["error"]["code"] == "permission_denied"
    link_session(
        h.sessions,
        sibling.address,
        subagent_id="sub_sibling",
        parent=h.caller.address,
        run_id="run",
        tool_call_id=None,
        tool_call_index=None,
    )
    ceiling = restricted.ceiling(source="parent")
    assert ceiling is not None
    inherited = replace(
        context, permissions=PermissionBoundary.build(None, boundary_context, ceilings=(ceiling,))
    )
    listing = await h.call({"action": "list"}, context=inherited)
    assert [item["id"] for item in listing["data"]["items"]] == [h.reference(sibling)]
    assert (await h.call({"action": "read", "id": h.reference(own)}, context=inherited))["error"][
        "code"
    ] == "permission_denied"


async def test_peer_wait_ignores_old_results_and_own_sent_input_without_skipping_pages(
    peer_harness: PeerHarness,
):
    h = peer_harness
    peer = h.create("reviewer", "review")
    peer.append(ChatMessage.assistant(model="test", content="Old answer"))
    baseline = (await h.call({"action": "read", "id": h.reference(peer)}))["data"]["cursor"]
    peer.append(
        ChatMessage.user(
            "Our outgoing message",
            sender=MessageSender(h.reference(h.caller), "coder", "member", kind="session"),
        )
    )
    unchanged = await h.call(
        {"action": "wait", "id": h.reference(peer), "cursor": baseline, "timeout": 0}
    )
    assert unchanged["data"]["status"] == "unchanged"
    assert "messages" not in unchanged["data"]
    peer.append_many(
        [ChatMessage.assistant(model="test", content=f"Answer {index}") for index in range(25)]
    )
    first = await h.call(
        {"action": "wait", "id": h.reference(peer), "cursor": baseline, "timeout": 0}
    )
    assert first["data"]["status"] == "changed"
    assert len(first["data"]["messages"]) == 20
    second = await h.call(
        {"action": "wait", "id": h.reference(peer), "cursor": first["data"]["cursor"], "timeout": 0}
    )
    assert [message["text"] for message in second["data"]["messages"]] == [
        f"Answer {index}" for index in range(20, 25)
    ]
    final = await h.call(
        {
            "action": "wait",
            "id": h.reference(peer),
            "cursor": second["data"]["cursor"],
            "timeout": 0,
        }
    )
    assert final["data"]["status"] == "unchanged"
    assert h.activity.callbacks == []


async def test_peer_wait_wakes_for_persisted_output_and_cleans_up_subscriptions(
    peer_harness: PeerHarness,
):
    h = peer_harness
    peer = h.create("reviewer", "review")
    release = asyncio.Event()
    ready = asyncio.Event()

    async def execute(run):
        await run.wait_admitted()
        ready.set()
        await release.wait()
        message = ChatMessage.assistant(model="test", content="New result")
        await h.sessions.get(peer.address).for_run(run.id).append_async(message)
        run.emit(ASSISTANT_OUTPUT_EVENT, {"message": message.to_dict()})

    run = await h.runs.start(peer.address, execute)
    await ready.wait()
    baseline = (await h.call({"action": "read", "id": h.reference(peer)}))["data"]["cursor"]
    wait = asyncio.create_task(
        h.call({"action": "wait", "id": h.reference(peer), "cursor": baseline, "timeout": 60})
    )
    await h.activity.subscribed.wait()
    release.set()
    result = await asyncio.wait_for(wait, timeout=2)
    await run.wait()
    assert result["data"]["status"] == "changed", result
    assert result["data"]["messages"][0]["text"] == "New result"
    assert run.subscriber_count == 0
    assert h.activity.callbacks == []


async def test_peer_wait_releases_for_new_caller_input_only_after_admission(
    peer_harness: PeerHarness,
):
    h = peer_harness
    peer = h.create("reviewer", "review")
    hold = asyncio.Event()
    caller_run = await h.runs.start(h.caller.address, lambda _run: hold.wait())
    await caller_run.wait_admitted()
    wait = asyncio.create_task(h.call({"action": "wait", "id": h.reference(peer)}))
    await h.activity.subscribed.wait()
    persisted = asyncio.Event()
    observations = []
    remove = h.runs.add_input_queued_callback(
        lambda _address: observations.append(persisted.is_set())
    )

    async def refused():
        raise ValueError("admission failed")

    with pytest.raises(ValueError):
        await h.runs.enqueue(h.caller.address, lambda _run: hold.wait(), persist_input=refused)
    assert observations == []

    async def persist():
        persisted.set()

    await h.runs.enqueue(h.caller.address, lambda _run: hold.wait(), persist_input=persist)
    result = await asyncio.wait_for(wait, timeout=2)
    assert persisted.is_set()
    assert observations == [True]
    assert result["data"]["status"] == "input_received", result
    hold.set()
    await caller_run.wait()
    remove()
    assert h.activity.callbacks == []


async def test_peer_wait_is_cancelled_without_leaving_observers(peer_harness: PeerHarness):
    h = peer_harness
    peer = h.create("reviewer", "review")
    callbacks: list[Callable[[], None]] = []
    context = h.context(cancel_registration_hook=callbacks.append)
    wait = asyncio.create_task(h.call({"action": "wait", "id": h.reference(peer)}, context=context))
    await h.activity.subscribed.wait()
    callbacks[0]()
    with pytest.raises(asyncio.CancelledError):
        await wait
    assert h.activity.callbacks == []
    assert h.runs._input_queued_callbacks == []
