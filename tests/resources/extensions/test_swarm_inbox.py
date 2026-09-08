"""Focused durable Inbox Tool coverage."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.chat.messages import ChatMessage
from tests.resources.extensions.test_swarm_board import board as board_fixture
from tests.resources.extensions.test_swarm_board import call

board = board_fixture


@pytest.mark.asyncio
async def test_inbox_delivers_oldest_pending_entries_with_a_durable_receipt(board):
    recipient = board.bindings[1].participant_id
    for request_id, text in (("one", "first"), ("two", "second")):
        result, _ = await call(
            board,
            {
                "action": "post",
                "text": text,
                "request_id": request_id,
                "recipients": [recipient],
            },
        )
        assert result["ok"]
    context = replace(board.contexts[1], tool_name="swarm_inbox", tool_call_id="inbox")
    result = await board.tools.get("swarm_inbox").handler(context, {"limit": 1})
    assert [entry["text"] for entry in result["data"]["entries"]] == ["first"]
    assert result["data"]["pending_remaining"] == 1
    assert result["data"]["next_call"] == {
        "tool": "swarm_inbox",
        "arguments": {"limit": 1},
    }
    receipt_id, content_hash, effect = context._delivery_receipts[0]
    binding = board.bindings[1]
    await board.sessions.append_messages_with_receipts_async(
        binding.address,
        generation_id=binding.generation_id,
        owner_name="swarm",
        messages=[
            ChatMessage.tool(
                tool_call_id=context.tool_call_id, name="swarm_inbox", content=json.dumps(result)
            )
        ],
        receipts=[(0, receipt_id, content_hash, effect, "tool")],
    )
    assert await board.store.reconcile_delivery(receipt_id)
    next_result = await board.tools.get("swarm_inbox").handler(context, {})
    assert [entry["text"] for entry in next_result["data"]["entries"]] == ["second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": True}, {"limit": "1"}, {"other": 1}])
async def test_inbox_rejects_invalid_arguments_without_a_receipt(board, arguments):
    context = replace(board.contexts[0], tool_name="swarm_inbox")
    result = await board.tools.get("swarm_inbox").handler(context, arguments)
    assert not result["ok"]
    assert context._delivery_receipts == []


@pytest.mark.asyncio
async def test_inbox_continuation_preserves_default_and_empty_does_not_end_run(board):
    peer = board.bindings[1].participant_id
    for index in range(21):
        await board.store.post(board.swarm["id"], peer, text=str(index), request_id=f"bulk-{index}")
    ended = []
    context = replace(
        board.contexts[0],
        tool_name="swarm_inbox",
        session_tool_grants=("swarm_inbox",),
        request_turn_end_hook=lambda: ended.append(True),
    )
    result = await board.tools.dispatch(context, {}, allowed_tools=["swarm_inbox"])
    assert len(result["data"]["entries"]) == 20
    assert result["data"]["next_call"]["arguments"] == {}
    assert not ended and not context._turn_end_requested
    empty_context = replace(
        board.contexts[1],
        tool_name="swarm_inbox",
        session_tool_grants=("swarm_inbox",),
        request_turn_end_hook=lambda: ended.append(True),
    )
    empty = await board.tools.dispatch(empty_context, {}, allowed_tools=["swarm_inbox"])
    assert empty["data"]["entries"] == []
    assert empty["data"]["pending_remaining"] == 0
    assert "next_call" not in empty["data"]
    assert empty_context._delivery_receipts == []
    assert not ended and not empty_context._turn_end_requested


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["automatic", "inbox"])
async def test_delivery_preserves_message_context_across_batches(board, delivery):
    sid = board.swarm["id"]
    sender, recipient = [binding.participant_id for binding in board.bindings[:2]]
    opening = await board.store.create_discussion(
        sid,
        sender,
        title="Discussion title sentinel",
        text="Opening",
        recipients=[recipient],
        request_id="open-context",
    )
    did = opening["discussion_id"]
    await board.store.join_discussion(sid, recipient, did)
    normal = await board.store.post(
        sid,
        sender,
        discussion_id=did,
        text="Ordinary discussion post",
        request_id="normal",
    )
    await board.store.post(
        sid,
        sender,
        discussion_id=did,
        text="Reply ping",
        reply_to=normal["post_id"],
        recipients=[recipient],
        request_id="reply",
    )
    await board.store.post_human(sid, text="Human post", request_id="human")
    await board.store.apply_delivery_settings(
        sid,
        {**board.swarm["delivery"], "batch_messages": 2},
        expected_revision=1,
        request_id="batches",
        actor="test",
    )
    binding = board.bindings[1]
    context = replace(board.contexts[1], tool_name="swarm_inbox")
    request_context = SimpleNamespace(
        binding=binding,
        execution_owner=context.execution_owner,
        run_id=context.run_id,
    )
    entries = []
    batches = []
    for index in range(3):
        if delivery == "automatic":
            prepared = await board.service._before_request(request_context)
            assert prepared is not None
            text = prepared.entries[0]
            data = json.loads(text[text.index("{") :])
            # Replaying an unacknowledged batch retains both its identity and all metadata.
            assert await board.service._before_request(request_context) == prepared
            message = ChatMessage.note(text)
            receipt = (0, prepared.delivery_id, prepared.content_hash, prepared.effect_kind, "note")
        else:
            context = replace(context, tool_call_id=f"context-inbox-{index}")
            result = await board.service.inbox(context, {"limit": 2})
            assert result["ok"]
            data = result["data"]
            receipt_id, content_hash, effect = context._delivery_receipts[-1]
            message = ChatMessage.tool(
                tool_call_id=context.tool_call_id,
                name="swarm_inbox",
                content=json.dumps(result),
            )
            receipt = (0, receipt_id, content_hash, effect, "tool")
        batches.append([entry["sequence"] for entry in data["entries"]])
        entries.extend(data["entries"])
        await board.sessions.append_messages_with_receipts_async(
            binding.address,
            generation_id=binding.generation_id,
            owner_name="swarm",
            messages=[message],
            receipts=[receipt],
        )
        assert await board.store.reconcile_delivery(receipt[1])
    assert batches == [[1, 2], [3, 4], [5]]
    assert [entry["route_class"] for entry in entries] == [
        "ping",
        "main",
        "discussion",
        "ping",
        "main",
    ]
    assert len({entry["id"] for entry in entries}) == 5
    for entry in entries:
        assert datetime.fromisoformat(entry["created_at"]).utcoffset() == timedelta(0)
        assert entry["discussion_title"] == (
            "Discussion title sentinel" if entry["discussion_id"] == did else "Main"
        )
        assert entry["author"] == (
            {"kind": "user", "id": "user", "name": "User"}
            if entry["text"] == "Human post"
            else {"kind": "participant", "id": sender, "name": "Participant 1"}
        )
    reply = next(entry for entry in entries if entry["text"] == "Reply ping")
    assert reply["reply_to"] == normal["post_id"]
    assert reply["recipients"] == [recipient]
    assert (await board.store.prepare_inbox_delivery(sid, recipient))["entries"] == []


@pytest.mark.asyncio
async def test_delayed_board_message_keeps_original_order_and_context(board):
    sid = board.swarm["id"]
    sender, recipient = [binding.participant_id for binding in board.bindings[:2]]
    await board.store.apply_delivery_settings(
        sid,
        {**board.swarm["delivery"], "main": {"mode": "pull", "wake_idle": False}},
        expected_revision=1,
        request_id="mixed",
        actor="test",
    )
    await board.store.post(sid, sender, text="Older", request_id="older")
    newer = await board.store.post(
        sid,
        sender,
        text="Newer",
        recipients=[recipient],
        request_id="newer",
    )
    binding = board.bindings[1]
    context = SimpleNamespace(
        binding=binding,
        execution_owner=board.contexts[1].execution_owner,
        run_id="delivery-run",
    )
    prepared = await board.service._before_request(context)
    text = prepared.entries[0]
    automatic = json.loads(text[text.index("{") :])["entries"]
    assert [entry["id"] for entry in automatic] == [newer["post_id"]]
    await board.sessions.append_messages_with_receipts_async(
        binding.address,
        generation_id=binding.generation_id,
        owner_name="swarm",
        messages=[ChatMessage.note(text)],
        receipts=[(0, prepared.delivery_id, prepared.content_hash, prepared.effect_kind, "note")],
    )
    assert await board.store.reconcile_delivery(prepared.delivery_id)
    older = (await board.service.inbox(board.contexts[1], {}))["data"]["entries"][0]
    assert older["sequence"] < automatic[0]["sequence"]
    assert datetime.fromisoformat(older["created_at"]) <= datetime.fromisoformat(
        automatic[0]["created_at"]
    )
    assert older["route_class"] == "main" and automatic[0]["route_class"] == "ping"
    assert older["discussion_id"] == automatic[0]["discussion_id"]
    assert older["discussion_title"] == automatic[0]["discussion_title"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["route_class", "discussion_title"])
async def test_board_reads_preserve_context_even_when_text_matches_column_name(board, body):
    sid = board.swarm["id"]
    sender, recipient, outsider = [binding.participant_id for binding in board.bindings]
    created = await board.store.create_discussion(
        sid,
        sender,
        title="Read context",
        text=body,
        recipients=[recipient],
        request_id="read-context",
    )
    for peer in (recipient, outsider):
        exact = (
            await board.store.read_posts(sid, peer, message_id=created["opening_post_id"])
        ).entries[0]
        page = (
            await board.store.read_posts(sid, peer, discussion_id=created["discussion_id"])
        ).entries[0]
        assert exact == page
        assert exact["discussion_title"] == "Read context"
        assert exact["text"] == body
        if peer == recipient:
            assert exact["route_class"] == "ping"
        else:
            assert "route_class" not in exact
