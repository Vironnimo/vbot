"""Focused durable Inbox Tool coverage."""

import json
from dataclasses import replace

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
