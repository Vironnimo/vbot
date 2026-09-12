"""Tests for sessions temporary."""

from __future__ import annotations

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.sessions import (
    ChatSessionManager,
)
from tests.core.sessions.sessions_test_support import (
    _address,
)
from tests.core.sessions.sessions_test_support import (
    manager as manager,
)


@pytest.mark.asyncio
async def test_temporary_binding_and_receipt_are_generation_scoped_and_idempotent(tmp_path) -> None:
    sessions = ChatSessionManager(tmp_path)
    address = _address("temporary", "participant")
    binding = sessions.create_bound_temporary_session(
        address,
        owner_name="swarm",
        group_id="group",
        participant_id="participant",
        config={"model": "test/model"},
    )
    assert (
        sessions.create_bound_temporary_session(
            address,
            owner_name="swarm",
            group_id="group",
            participant_id="participant",
            config={"model": "test/model"},
        )
        == binding
    )

    carrier = ChatMessage.note("delivery")
    sessions.append_messages_with_receipts(
        address,
        generation_id=binding.generation_id,
        owner_name="swarm",
        messages=[carrier],
        receipts=[(0, "delivery-1", "hash", "note", "note")],
        deduplicate_carrier=True,
    )
    sessions.append_messages_with_receipts(
        address,
        generation_id=binding.generation_id,
        owner_name="swarm",
        messages=[ChatMessage.note("delivery")],
        receipts=[(0, "delivery-1", "hash", "note", "note")],
        deduplicate_carrier=True,
    )
    assert [message.content for message in sessions.get(address).load()] == ["delivery"]
    receipt = await sessions.lookup_delivery_receipt(
        address, binding.generation_id, "swarm", "delivery-1"
    )
    assert receipt is not None
    assert receipt.carrier_location == {"kind": "note", "sequence": 0}

    tools_and_note = [
        ChatMessage.tool(tool_call_id="one", name="first", content="tool-one"),
        ChatMessage.tool(tool_call_id="two", name="second", content="tool-two"),
        ChatMessage.note("after-tools"),
    ]
    sessions.append_messages_with_receipts(
        address,
        generation_id=binding.generation_id,
        owner_name="swarm",
        messages=tools_and_note,
        receipts=[
            (0, "tool-1", "hash-1", "delivery", "tool"),
            (1, "tool-2", "hash-2", "delivery", "tool"),
        ],
    )
    assert [message.content for message in sessions.get(address).load()] == [
        "delivery",
        "tool-one",
        "tool-two",
        "after-tools",
    ]
    tool_two_receipt = await sessions.lookup_delivery_receipt(
        address, binding.generation_id, "swarm", "tool-2"
    )
    assert tool_two_receipt is not None
    assert tool_two_receipt.carrier_location == {"kind": "tool", "sequence": 2}

    replayed_batch = [
        ChatMessage.tool(tool_call_id="one-retry", name="first", content="tool-one-retry"),
        ChatMessage.tool(tool_call_id="three", name="third", content="tool-three"),
        ChatMessage.note("after-retry"),
    ]
    sessions.append_messages_with_receipts(
        address,
        generation_id=binding.generation_id,
        owner_name="swarm",
        messages=replayed_batch,
        receipts=[
            (0, "tool-1", "hash-1", "delivery", "tool"),
            (1, "tool-3", "hash-3", "delivery", "tool"),
        ],
    )
    assert [message.content for message in sessions.get(address).load()][-3:] == [
        "tool-one-retry",
        "tool-three",
        "after-retry",
    ]
    tool_three_receipt = await sessions.lookup_delivery_receipt(
        address, binding.generation_id, "swarm", "tool-3"
    )
    assert tool_three_receipt is not None
    assert tool_three_receipt.carrier_location == {"kind": "tool", "sequence": 5}

    with pytest.raises(ChatSessionError):
        sessions.append_messages_with_receipts(
            address,
            generation_id=binding.generation_id,
            owner_name="swarm",
            messages=[ChatMessage.note("must roll back"), ChatMessage.note("conflict")],
            receipts=[
                (0, "delivery-2", "hash-2", "note", "note"),
                (1, "delivery-1", "different", "note", "note"),
            ],
        )
    assert [message.content for message in sessions.get(address).load()] == [
        "delivery",
        "tool-one",
        "tool-two",
        "after-tools",
        "tool-one-retry",
        "tool-three",
        "after-retry",
    ]
    assert (
        await sessions.lookup_delivery_receipt(
            address, binding.generation_id, "swarm", "delivery-2"
        )
        is None
    )
    with pytest.raises(ChatSessionError, match="delivery receipt is invalid"):
        sessions.append_messages_with_receipts(
            address,
            generation_id=binding.generation_id,
            owner_name="swarm",
            messages=[ChatMessage.tool(tool_call_id="bad", name="bad", content="bad")],
            receipts=[(0, "bad", "hash", "delivery", "note")],
        )

    with pytest.raises(ChatSessionError, match="managed by an Extension"):
        await sessions.archive(address)
    assert (
        sessions.create_bound_temporary_session(
            address,
            owner_name="swarm",
            group_id="group",
            participant_id="participant",
            config={"model": "test/model"},
        )
        == binding
    )
    sessions.close()


def test_temporary_session_stays_out_of_normal_lists_after_metadata_rewrite(tmp_path) -> None:
    sessions = ChatSessionManager(tmp_path)
    address = _address("temporary", "participant")
    sessions.create_bound_temporary_session(
        address,
        owner_name="swarm",
        group_id="group",
        participant_id="participant",
        config={},
    )
    sessions.set_metadata(address, {"title": "ordinary metadata"})

    assert sessions.list_summaries_page([(None, "temporary")], limit=20).sessions == ()
    sessions.close()


@pytest.mark.asyncio
async def test_owner_managed_session_rejects_lifecycle_mutations_at_storage_boundary(
    tmp_path,
) -> None:
    sessions = ChatSessionManager(tmp_path)
    address = _address("temporary", "participant")
    binding = sessions.create_bound_temporary_session(
        address,
        owner_name="swarm",
        group_id="group",
        participant_id="participant",
        config={},
    )
    try:
        with pytest.raises(ChatSessionError, match="managed by an Extension"):
            await sessions.move(address, _address("ordinary", "moved"))
        with pytest.raises(ChatSessionError, match="managed by an Extension"):
            await sessions.archive(address)
        with pytest.raises(ChatSessionError, match="managed by an Extension"):
            sessions.delete(address)
        with pytest.raises(ChatSessionError, match="managed by an Extension"):
            sessions.get(address).delete()
        with pytest.raises(ChatSessionError, match="managed by an Extension"):
            sessions.archive_identity_agent_sessions("temporary")
        with pytest.raises(ChatSessionError, match="managed by an Extension"):
            sessions.retarget_identity_agent_sessions("temporary", "ordinary")
        assert sessions.temporary_binding(address) == binding
    finally:
        sessions.close()
