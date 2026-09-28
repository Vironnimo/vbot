"""Swarm delivery: the Inbox Tool and automatic delivery through the Session hooks."""

import json
from dataclasses import replace

import pytest

from core.chat.messages import ChatMessage
from resources.extensions.swarm.agent_text import (
    DELIVERY_PREFIX,
    DISCUSSION_ANNOUNCEMENT,
    EMPTY_INBOX,
)
from resources.extensions.swarm.store import SwarmStoreError
from tests.resources.extensions.swarm.swarm_test_support import (
    Received,
    _name,
    call,
    continuation,
    delivery_request,
    dispatch,
    persist_carriers,
    post_ref,
    received,
    visible,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["automatic", "inbox", "board"])
async def test_delivery_invalidates_pending_only_after_canonical_receipt(board, delivery):
    sid = board.swarm["id"]
    sender, recipient = [binding.participant_id for binding in board.bindings[:2]]
    await board.store.post(sid, sender, text="receipt-sentinel", request_id="post")
    # The recipient's Run was admitted before its first request.
    await board.store.record_run_started(
        sid, recipient, run_id=board.contexts[1].run_id, expected_epoch=0
    )
    changes = []
    board.service.host = replace(
        board.service.host, publish_change=lambda *args: changes.append(args)
    )
    binding = board.bindings[1]
    context = replace(
        board.contexts[1], tool_name="swarm_inbox" if delivery == "inbox" else "swarm_board"
    )
    request = delivery_request(board, 1)
    if delivery == "automatic":
        prepared = await board.runtime.before_request(request)
        assert prepared is not None
        message = ChatMessage.note("\n\n".join(prepared.entries))
        receipt = (0, prepared.delivery_id, prepared.content_hash, prepared.effect_kind, "note")

        async def reconcile():
            await board.runtime.acknowledge_delivery(request, prepared)
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
            await board.runtime.reconcile_tool_batch(
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
    assert [change[0:2] for change in changes] == [("participants", [sid])]
    changes.clear()
    await board.runtime.reconcile_tool_batch(
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
    main = "the main discussion (d1)"
    [first] = received(result["data"]["content"])
    assert first._replace(post_id="") == Received(main, "", _name(board, 0), "to you", "first")
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
async def test_inbox_rejects_invalid_arguments_without_a_receipt(board):
    for arguments in [{"limit": 0}, {"limit": True}, {"limit": "unknown"}, {"other": 1}]:
        context = replace(board.contexts[0], tool_name="swarm_inbox")
        result = await board.tools.get("swarm_inbox").handler(context, arguments)
        assert not result["ok"], arguments
        assert context._delivery_receipts == [], arguments


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
    request_context = delivery_request(board, 1)
    batches = []
    remaining = []
    for index in range(3):
        if delivery == "automatic":
            prepared = await board.runtime.before_request(request_context)
            assert prepared is not None
            [text] = prepared.entries
            # Replaying an unacknowledged batch retains both its identity and its text.
            assert await board.runtime.before_request(request_context) == prepared
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
    topic = 'discussion "Discussion title sentinel" (d2)'
    main = "the main discussion (d1)"
    opening_ref = await post_ref(board, opening["opening_post_id"])
    announcement = DISCUSSION_ANNOUNCEMENT.format(
        author_name=author,
        title="Discussion title sentinel",
        discussion_id="d2",
        opening_post_id=opening_ref,
    )
    assert batches == [
        [
            Received(topic, opening_ref, author, "to you", "Opening"),
            Received(
                main,
                await post_ref(board, opening["main_announcement_id"]),
                author,
                None,
                announcement,
            ),
        ],
        [
            Received(topic, f"#{normal['sequence']}", author, None, "Ordinary discussion post"),
            Received(
                topic,
                f"#{reply['sequence']}",
                author,
                f"reply to #{normal['sequence']}; to you",
                "Reply ping",
            ),
        ],
        [Received(main, f"#{human['sequence']}", "User", None, "Human post")],
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
    prepared = await board.runtime.before_request(delivery_request(board, 1))
    [text] = prepared.entries
    main = "the main discussion (d1)"
    author = _name(board, 0)
    assert received(text) == [Received(main, f"#{newer['sequence']}", author, "to you", "Newer")]
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
        Received(main, f"#{older['sequence']}", author, None, "Older")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("board", "inbox"), [(True, True), (False, False)], indirect=["board"], ids=["inbox", "board"]
)
async def test_delivery_names_swarm_inbox_only_when_it_is_available(board, inbox):
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
    [text] = (await board.runtime.before_request(delivery_request(board, 1))).entries
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


@pytest.mark.asyncio
async def test_long_main_posts_reach_unaddressed_readers_as_their_opening(board):
    from resources.extensions.swarm.agent_text import OPENING_CHARS, POST_SHORTENED

    sid = board.swarm["id"]
    sender, recipient = [binding.participant_id for binding in board.bindings[:2]]
    reader = _name(board, 1)
    long_text = " ".join(f"word{index:04d}" for index in range(120))
    assert len(long_text) > 1000
    unaddressed, _ = await dispatch(board, {"text": long_text})
    assert unaddressed["data"]["delivery"] == (
        "Queued for 2 participants. 2 participants receive only its opening lines and the call "
        "to read the rest."
    )
    addressed, _ = await dispatch(board, {"text": f"@{reader}: {long_text}"})
    assert addressed["data"]["delivery"] == (
        f"Queued for 2 participants. It reaches {reader} in full because it addresses or answers "
        "them. 1 participant receives only its opening lines and the call to read the rest."
    )
    human = await board.store.post_human(sid, text=long_text, request_id="human")
    opened = await board.store.create_discussion(
        sid, sender, title="Parser", text="Opening", request_id="parser"
    )
    await board.store.join_discussion(sid, recipient, opened["discussion_id"])
    discussion = await board.store.post(
        sid, sender, discussion_id=opened["discussion_id"], text=long_text, request_id="long"
    )
    [text] = (await board.runtime.before_request(delivery_request(board, 1))).entries
    delivered = {message.post_id: message.text for message in received(text)}

    post_id = unaddressed["data"]["post_id"]
    start = delivered[post_id].split(" ...\n")[0]
    # The opening ends at a word boundary.
    assert len(start) <= OPENING_CHARS and long_text.startswith(f"{start} ")
    call = json.dumps({"action": "read", "message_id": post_id})
    assert delivered[post_id] == f"{start} ...\n" + POST_SHORTENED.format(
        count=len(long_text) - len(start), call=call
    )
    assert delivered[addressed["data"]["post_id"]] == f"@{reader}: {long_text}"
    assert delivered[f"#{human['sequence']}"] == long_text
    assert delivered[f"#{discussion['sequence']}"] == long_text
    # The call named in the delivery returns the whole post.
    read, _ = await dispatch(board, json.loads(call), peer=1)
    assert long_text in read["data"]["content"]
