"""Focused durable Inbox Tool coverage."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.chat.messages import ChatMessage
from resources.extensions.swarm.agent_text import (
    DELIVERY_PREFIX,
    DISCUSSION_ANNOUNCEMENT,
    EMPTY_INBOX,
)
from resources.extensions.swarm.store import SwarmStoreError
from tests.resources.extensions.test_swarm_board import (
    Received,
    _name,
    call,
    continuation,
    deny_inbox,
    dispatch,
    persist_carriers,
    received,
    visible,
)
from tests.resources.extensions.test_swarm_board import board as board_fixture

board = board_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["automatic", "inbox", "board"])
async def test_delivery_invalidates_pending_only_after_canonical_receipt(board, delivery):
    sid = board.swarm["id"]
    sender, recipient = [binding.participant_id for binding in board.bindings[:2]]
    await board.store.post(sid, sender, text="receipt-sentinel", request_id="post")
    changes = []
    board.service.host = replace(
        board.service.host, publish_change=lambda *args: changes.append(args)
    )
    binding = board.bindings[1]
    context = replace(
        board.contexts[1], tool_name="swarm_inbox" if delivery == "inbox" else "swarm_board"
    )
    request = SimpleNamespace(
        binding=binding, execution_owner=context.execution_owner, run_id=context.run_id
    )
    if delivery == "automatic":
        prepared = await board.service._before_request(request)
        assert prepared is not None
        message = ChatMessage.note("\n\n".join(prepared.entries))
        receipt = (0, prepared.delivery_id, prepared.content_hash, prepared.effect_kind, "note")

        async def reconcile():
            await board.service._acknowledge_delivery(request, prepared)
    else:
        result = (
            await board.service.inbox(context, {})
            if delivery == "inbox"
            else await board.service.board(
                context, {"action": "read", "discussion_id": board.swarm["main_discussion_id"]}
            )
        )
        assert result["ok"]
        receipt_id, content_hash, effect = context._delivery_receipts[0]
        receipt = (0, receipt_id, content_hash, effect, "tool")
        message = ChatMessage.tool(
            tool_call_id=context.tool_call_id, name=context.tool_name, content=json.dumps(result)
        )

        async def reconcile():
            await board.service._reconcile_tool_batch(
                request,
                receipts=((context.tool_call_id, receipt_id, content_hash, effect),),
                persisted_call_ids=(context.tool_call_id,),
                turn_end_requested=False,
            )

    assert not changes
    with pytest.raises(SwarmStoreError) as error:
        await reconcile()
    assert error.value.code == "delivery_unacknowledged"
    assert not changes
    assert (await board.store.participant_status(sid, recipient))["pending_count"] == 1
    await persist_carriers(
        board.sessions,
        binding.address,
        generation_id=binding.generation_id,
        owner_name="swarm",
        messages=[message],
        receipts=[receipt],
    )
    assert (await board.store.participant_status(sid, recipient))["pending_count"] == 1
    await reconcile()
    assert (await board.store.participant_status(sid, recipient))["pending_count"] == 0
    assert [change[0:2] for change in changes] == [("swarms", [sid])]
    changes.clear()
    await board.service._reconcile_tool_batch(
        request,
        receipts=(),
        persisted_call_ids=("state",),
        turn_end_requested=False,
    )
    assert not changes


@pytest.mark.asyncio
async def test_inbox_delivers_oldest_pending_entries_with_a_durable_receipt(board):
    recipient = board.bindings[1].participant_id
    for text in ("first", "second"):
        result, _ = await call(
            board,
            {
                "action": "post",
                "text": text,
                "recipients": [recipient],
            },
        )
        assert result["ok"]
    context = replace(board.contexts[1], tool_name="swarm_inbox", tool_call_id="inbox")
    result = await board.tools.get("swarm_inbox").handler(context, {"limit": 1})
    main = f"the main discussion ({board.swarm['main_discussion_id']})"
    [first] = received(result["data"]["content"])
    assert first._replace(post_id="") == Received(main, "", _name(board, 0), "pinged you", "first")
    assert result["data"]["more"] == (
        '1 more pending; call swarm_inbox again with {"limit": 1} to continue.'
    )
    receipt_id, content_hash, effect = context._delivery_receipts[0]
    binding = board.bindings[1]
    await persist_carriers(
        board.sessions,
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
    next_result = await board.tools.get("swarm_inbox").handler(
        context, continuation(result["data"]["more"])
    )
    assert [message.text for message in received(next_result["data"]["content"])] == ["second"]
    assert "more" not in next_result["data"]


@pytest.mark.asyncio
async def test_wake_scan_does_not_replay_messages_read_during_a_run(board):
    sid = board.swarm["id"]
    sender, recipient = [binding.participant_id for binding in board.bindings[:2]]
    await board.store.record_run_started(sid, recipient, run_id="active", expected_epoch=0)
    await board.store.post(sid, sender, text="read-once-sentinel", request_id="post")
    # The background wake scan visits every participant, including busy peers.
    await board.store.prepare_wake(sid, recipient, expected_epoch=0)
    context = replace(board.contexts[1], tool_name="swarm_inbox")
    result = await board.service.inbox(context, {})
    assert [message.text for message in received(result["data"]["content"])] == [
        "read-once-sentinel"
    ]
    receipt_id, content_hash, effect = context._delivery_receipts[0]
    binding = board.bindings[1]
    await persist_carriers(
        board.sessions,
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
    assert (await board.store.participant_status(sid, recipient))["pending_count"] == 0
    automatic = await board.store.prepare_automatic_delivery(sid, recipient, expected_epoch=0)
    assert automatic["entries"] == []
    assert automatic["pending_remaining"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments", [{"limit": 0}, {"limit": True}, {"limit": "unknown"}, {"other": 1}]
)
async def test_inbox_rejects_invalid_arguments_without_a_receipt(board, arguments):
    context = replace(board.contexts[0], tool_name="swarm_inbox")
    result = await board.tools.get("swarm_inbox").handler(context, arguments)
    assert not result["ok"]
    assert context._delivery_receipts == []


@pytest.mark.asyncio
async def test_inbox_lowers_a_large_limit_with_a_note(board):
    peer = board.bindings[1].participant_id
    await board.store.post(board.swarm["id"], peer, text="Only", request_id="only")
    result, context = await dispatch(board, {"limit": 500}, name="swarm_inbox")
    assert result["data"]["note"] == "limit 500 is above the maximum of 100; used 100."
    assert [message.text for message in received(result["data"]["content"])] == ["Only"]
    assert len(context._delivery_receipts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        (
            "swarm_inbox",
            {"action": "wait"},
            "swarm_inbox has no wait action; it only receives your pending Board messages. "
            "Call it without arguments, or with limit.",
        ),
        (
            "swarm_state",
            {"action": "done", "summary": "Finished"},
            "swarm_state has no done action; it only shows the participants, their Run "
            "activity, your pending messages, and how Board messages reach you. Share progress, "
            "results, or requests for help on the Board with swarm_board, and end your reply "
            "normally when you have no further work now.",
        ),
    ],
)
async def test_inbox_and_state_explain_actions_they_do_not_have(board, name, arguments, expected):
    result, context = await dispatch(board, arguments, name=name)
    assert visible(result) == f"Error (invalid_arguments): {expected}"
    assert context._delivery_receipts == [] and not context._turn_end_requested


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
    assert [message.text for message in received(result["data"]["content"])] == [
        str(index) for index in range(20)
    ]
    assert result["data"]["more"] == "1 more pending; call swarm_inbox again to continue."
    assert not ended and not context._turn_end_requested
    empty_context = replace(
        board.contexts[1],
        tool_name="swarm_inbox",
        session_tool_grants=("swarm_inbox",),
        request_turn_end_hook=lambda: ended.append(True),
    )
    empty = await board.tools.dispatch(empty_context, {}, allowed_tools=["swarm_inbox"])
    assert visible(empty) == EMPTY_INBOX
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
    reply = await board.store.post(
        sid,
        sender,
        discussion_id=did,
        text="Reply ping",
        reply_to=normal["post_id"],
        recipients=[recipient],
        request_id="reply",
    )
    human = await board.store.post_human(sid, text="Human post", request_id="human")
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
    batches = []
    remaining = []
    for index in range(3):
        if delivery == "automatic":
            prepared = await board.service._before_request(request_context)
            assert prepared is not None
            [text] = prepared.entries
            # Replaying an unacknowledged batch retains both its identity and its text.
            assert await board.service._before_request(request_context) == prepared
            assert text.startswith(f"{DELIVERY_PREFIX}\n\nIn ")
            remaining.append(text.rpartition("\n\n")[2] if "more pending" in text else None)
            message = ChatMessage.note(text)
            receipt = (0, prepared.delivery_id, prepared.content_hash, prepared.effect_kind, "note")
        else:
            context = replace(context, tool_call_id=f"context-inbox-{index}")
            result = await board.service.inbox(context, {"limit": 2})
            assert result["ok"]
            text = result["data"]["content"]
            remaining.append(result["data"].get("more"))
            receipt_id, content_hash, effect = context._delivery_receipts[-1]
            message = ChatMessage.tool(
                tool_call_id=context.tool_call_id,
                name="swarm_inbox",
                content=json.dumps(result),
            )
            receipt = (0, receipt_id, content_hash, effect, "tool")
        batches.append(received(text))
        await persist_carriers(
            board.sessions,
            binding.address,
            generation_id=binding.generation_id,
            owner_name="swarm",
            messages=[message],
            receipts=[receipt],
        )
        assert await board.store.reconcile_delivery(receipt[1])
    author = _name(board, 0)
    topic = f'discussion "Discussion title sentinel" ({did})'
    main = f"the main discussion ({board.swarm['main_discussion_id']})"
    announcement = DISCUSSION_ANNOUNCEMENT.format(
        author_name=author,
        title="Discussion title sentinel",
        discussion_id=did,
        opening_post_id=opening["opening_post_id"],
    )
    assert batches == [
        [
            Received(topic, opening["opening_post_id"], author, "pinged you", "Opening"),
            Received(main, opening["main_announcement_id"], author, None, announcement),
        ],
        [
            Received(topic, normal["post_id"], author, None, "Ordinary discussion post"),
            Received(
                topic,
                reply["post_id"],
                author,
                f"reply to {normal['post_id']}; pinged you",
                "Reply ping",
            ),
        ],
        [Received(main, human["post_id"], "User", None, "Human post")],
    ]
    assert remaining == (
        [
            "3 more pending; receive them with swarm_inbox.",
            "1 more pending; receive them with swarm_inbox.",
            None,
        ]
        if delivery == "automatic"
        else [
            '3 more pending; call swarm_inbox again with {"limit": 2} to continue.',
            '1 more pending; call swarm_inbox again with {"limit": 2} to continue.',
            None,
        ]
    )
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
    older = await board.store.post(sid, sender, text="Older", request_id="older")
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
    [text] = prepared.entries
    main = f"the main discussion ({board.swarm['main_discussion_id']})"
    author = _name(board, 0)
    assert received(text) == [Received(main, newer["post_id"], author, "pinged you", "Newer")]
    await persist_carriers(
        board.sessions,
        binding.address,
        generation_id=binding.generation_id,
        owner_name="swarm",
        messages=[ChatMessage.note(text)],
        receipts=[(0, prepared.delivery_id, prepared.content_hash, prepared.effect_kind, "note")],
    )
    assert await board.store.reconcile_delivery(prepared.delivery_id)
    inbox = await board.service.inbox(board.contexts[1], {})
    assert received(inbox["data"]["content"]) == [
        Received(main, older["post_id"], author, None, "Older")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("inbox", [True, False])
async def test_delivery_names_swarm_inbox_only_when_it_is_available(board, monkeypatch, inbox):
    sid = board.swarm["id"]
    sender, recipient = [binding.participant_id for binding in board.bindings[:2]]
    await board.store.apply_delivery_settings(
        sid,
        {**board.swarm["delivery"], "batch_messages": 1},
        expected_revision=1,
        request_id="single",
        actor="test",
    )
    for text in ("one", "two"):
        await board.store.post(sid, sender, text=text, request_id=text)
    if not inbox:
        deny_inbox(board, monkeypatch)
    request = SimpleNamespace(
        binding=board.bindings[1],
        execution_owner=board.contexts[1].execution_owner,
        run_id="delivery-run",
    )
    [text] = (await board.service._before_request(request)).entries
    assert [message.text for message in received(text)] == ["one"]
    assert text.endswith(
        "\n\n1 more pending; receive them with swarm_inbox." if inbox else "\n\n1 more pending."
    )
    assert ("swarm_inbox" in text) is inbox


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
