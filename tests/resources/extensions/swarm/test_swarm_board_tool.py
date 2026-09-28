"""Swarm Board Tool: posts, discussions, addressing, repaired calls and readable results."""

import json
from dataclasses import replace

import pytest

from core.chat import ChatMessage
from tests.resources.extensions.swarm.swarm_test_support import (
    _name,
    board_posts,
    call,
    continuation,
    dispatch,
    persist_carriers,
    visible,
)


@pytest.mark.asyncio
async def test_registered_board_public_posts_pages_and_durable_read_receipt(board):
    result, _ = await call(board, {"action": "list"})
    # Participants see discussions and posts by their numbers; the request is post #0.
    assert '- d1 "Main": main discussion, joined' in result["data"]["content"]
    assert continuation(result["data"]["user_request"]) == {"action": "read", "message_id": "#0"}
    peer = board.bindings[1].participant_id
    posted, posted_context = await call(
        board,
        {"action": "post", "text": "full text", "recipients": [peer, peer]},
    )
    assert posted["ok"]
    assert posted["data"]["delivery"] == (
        f"Queued for 2 participants. It reaches {_name(board, 1)} in full because it addresses "
        "or answers them."
    )
    replay, _ = await call(
        board,
        {"action": "post", "text": "full text", "recipients": [peer, peer]},
        tool_call_id=posted_context.tool_call_id,
    )
    assert replay["data"]["post_id"] == posted["data"]["post_id"]
    assert "nothing was duplicated" in replay["data"]["replayed"]
    read, context = await call(board, {"action": "read"}, peer=1)
    author = board.swarm["participants"][0]["display_name"]
    assert read["data"]["content"] == (
        f"[{posted['data']['post_id']}] {author} (to you):\nfull text"
    )
    assert "(1 shown)" in read["data"]["page"]
    assert len(context._delivery_receipts) == 1
    receipt_id, content_hash, effect = context._delivery_receipts[0]
    assert not await board.store.reconcile_delivery(receipt_id)
    binding = board.bindings[1]
    await persist_carriers(
        board.sessions,
        binding.address,
        generation_id=binding.generation_id,
        owner_name="swarm",
        messages=[
            ChatMessage.tool(
                tool_call_id=context.tool_call_id, name="swarm_board", content=json.dumps(read)
            )
        ],
        receipts=[(0, receipt_id, content_hash, effect, "tool")],
    )
    assert await board.store.reconcile_delivery(receipt_id)
    assert (await board.store.prepare_inbox_delivery(board.swarm["id"], peer))["entries"] == []
    assert board.tools.get("swarm_board").catalog_visible is False
    assert board.tools.get("swarm_board").session_scoped


@pytest.mark.asyncio
async def test_board_discussion_join_leave_reply_and_exact_pagination(board):
    created, _ = await call(board, {"action": "create", "title": "Topic", "text": "opening"})
    discussion = created["data"]["discussion_id"]
    opening = created["data"]["opening_post_id"]
    assert (discussion, opening) == ("d2", "#1")
    first, _ = await call(board, {"action": "list", "limit": 1})
    next_page, _ = await call(board, continuation(first["data"]["more"]))
    assert f'- {discussion} "Topic": joined, members: 1' in next_page["data"]["content"]
    joined, _ = await call(board, {"action": "join", "discussion_id": discussion}, peer=1)
    assert joined["data"]["status"] == "Joined. New posts in this discussion now reach you."
    assert f"[{opening}]" in joined["data"]["content"]
    again, _ = await call(board, {"action": "join", "discussion_id": discussion}, peer=1)
    assert again["data"]["status"] == "You had already joined; nothing changed."
    response, _ = await call(
        board,
        {
            "action": "post",
            "discussion_id": discussion,
            "text": "answer",
            "reply_to": opening,
        },
        peer=1,
    )
    assert response["ok"]
    newest, _ = await call(board, {"action": "read", "discussion_id": discussion, "limit": 1})
    # A reply addresses the author of the post it answers.
    assert newest["data"]["content"].endswith(f"(reply to {opening}; to you):\nanswer")
    older_call = continuation(newest["data"]["older"])
    assert older_call == {
        "action": "read",
        "discussion_id": discussion,
        "before": response["data"]["post_id"],
        "limit": 1,
    }
    older, _ = await call(board, older_call)
    assert older["data"]["content"].endswith(f"[{opening}] {_name(board, 0)}:\nopening")
    assert "older" not in older["data"]
    one, _ = await call(board, {"action": "read", "message_id": opening})
    assert one["data"]["content"] == (
        f'[{opening}] {_name(board, 0)} (in "Topic" {discussion}):\nopening'
    )
    left, _ = await call(board, {"action": "leave", "discussion_id": discussion}, peer=1)
    assert left["data"]["status"].startswith("Left.")
    assert not next(
        row
        for row in (
            await board.store.list_discussions(board.swarm["id"], board.bindings[1].participant_id)
        ).entries
        if row["sequence"] == 2
    )["joined"]
    again, _ = await call(board, {"action": "leave", "discussion_id": discussion}, peer=1)
    assert again["data"]["status"] == "You were not a member; nothing changed."


@pytest.mark.asyncio
async def test_invalid_board_calls_have_no_effect(board):
    for arguments in [
        {},
        {"action": "other"},
        {"action": "list", "limit": True},
        {"action": "list", "limit": 0},
        {"action": "list", "swarm_id": "foreign"},
        {"action": "read", "message_id": "foreign", "limit": 20},
        {"action": "read", "text": "wrong"},
        {"action": "post"},
        {"action": "post", "text": ""},
        {"action": "post", "text": "x" * 16001},
        {"action": "create", "text": "x"},
        {"action": "join"},
        {"action": "leave"},
    ]:
        result, context = await call(board, arguments)
        assert not result["ok"], arguments
        assert context._delivery_receipts == [], arguments
    page = await board.store.read_posts(board.swarm["id"], board.bindings[0].participant_id)
    assert page.entries == ()


@pytest.mark.asyncio
async def test_board_rejects_forged_context_and_preserves_separate_identical_posts(board):
    foreign = replace(board.contexts[0], session_id=board.contexts[1].session_id)
    result = await board.service.board(foreign, {"action": "list"})
    assert not result["ok"]
    assert (await call(board, {"action": "post", "text": "first"}))[0]["ok"]
    repeated, _ = await call(board, {"action": "post", "text": "first"})
    assert repeated["ok"]
    page = await board.store.read_posts(board.swarm["id"], board.bindings[0].participant_id)
    assert len(page.entries) == 2
    assert {entry["text"] for entry in page.entries} == {"first"}


@pytest.mark.asyncio
async def test_create_pings_opening_atomically_without_joining_recipients(board):
    peer = board.bindings[1].participant_id
    arguments = {
        "action": "create",
        "title": "Review",
        "text": "Please review this draft",
        "recipients": [peer, peer],
    }
    created, created_context = await call(board, arguments)
    assert created["ok"]
    data = created["data"]
    inbox = await board.store.prepare_inbox_delivery(board.swarm["id"], peer)
    assert [f"#{entry['sequence']}" for entry in inbox["entries"]][:1] == [data["opening_post_id"]]
    announcement = inbox["entries"][1]
    assert announcement["discussion_id"] == board.swarm["main_discussion_id"]
    assert announcement["text"] == (
        f'{_name(board, 0)} opened the discussion "Review" ({data["discussion_id"]}) with post '
        f"{data['opening_post_id']}. Read or join it with swarm_board and this discussion_id."
    )
    discussions = await board.store.list_discussions(board.swarm["id"], peer)
    assert not next(
        row for row in discussions.entries if f"d{row['sequence']}" == data["discussion_id"]
    )["joined"]
    replay, _ = await call(board, arguments, tool_call_id=created_context.tool_call_id)
    assert replay["data"]["discussion_id"] == data["discussion_id"]
    assert replay["data"]["replayed"]
    conflict, _ = await call(
        board, {**arguments, "recipients": []}, tool_call_id=created_context.tool_call_id
    )
    assert conflict["error"]["code"] == "request_conflict"
    invalid, _ = await call(board, {**arguments, "recipients": [peer, "foreign"]})
    assert invalid["error"]["code"] == "invalid_recipient"
    assert (
        await board.store.list_discussions(board.swarm["id"], peer)
    ).entries == discussions.entries
    assert (await board.store.prepare_inbox_delivery(board.swarm["id"], peer))["entries"] == inbox[
        "entries"
    ]


@pytest.mark.asyncio
async def test_reply_uses_owned_message_discussion_and_rejects_contradiction(board):
    created, _ = await call(board, {"action": "create", "title": "Topic", "text": "Opening"})
    topic = created["data"]
    arguments = {
        "action": "post",
        "text": "Answer",
        "reply_to": topic["opening_post_id"],
    }
    reply, reply_context = await call(board, arguments, peer=1)
    assert reply["data"]["discussion"] == f'discussion "Topic" ({topic["discussion_id"]})'
    replay, _ = await call(
        board,
        {**arguments, "discussion_id": topic["discussion_id"]},
        peer=1,
        tool_call_id=reply_context.tool_call_id,
    )
    assert replay["data"]["replayed"]
    mismatch, _ = await call(
        board,
        {**arguments, "discussion_id": board.swarm["main_discussion_id"]},
        peer=1,
    )
    assert mismatch["error"]["code"] == "reply_discussion_mismatch"
    assert mismatch["error"]["message"] == (
        f'reply_to {topic["opening_post_id"]} belongs to discussion "Topic" '
        f"({topic['discussion_id']}), but discussion_id names the main discussion "
        f'(d1). Omit discussion_id to reply in discussion "Topic" ({topic["discussion_id"]}), '
        "or omit reply_to to post a new message in the main discussion (d1). Nothing was saved."
    )
    missing, _ = await call(board, {**arguments, "reply_to": "foreign"}, peer=1)
    assert missing["error"]["code"] == "message_not_found"
    assert missing["error"]["message"] == (
        'reply_to "foreign" is not a post ID in your group. Find the post ID with '
        '{"action": "read"}, or omit reply_to for a new message. Nothing was saved.'
    )
    assert (
        len(
            (
                await board.store.read_posts(
                    board.swarm["id"],
                    board.bindings[0].participant_id,
                    discussion_id=topic["discussion_id"],
                )
            ).entries
        )
        == 2
    )


@pytest.mark.asyncio
async def test_board_validation_names_the_missing_or_misplaced_field_before_any_effect(board):
    for arguments, message in [
        ({"action": "post"}, "post needs text, the message body."),
        (
            {"action": "post", "message_id": "#1"},
            'post needs text, the message body. To read #1, use {"action": "read", '
            '"message_id": "#1"}; to answer it, repeat the call with text.',
        ),
        (
            {"action": "list", "text": "inapplicable"},
            "list does not use text, so the call may mean another action. Repeat it without "
            "text, or use an action that takes text: post or create.",
        ),
        ({"action": "join"}, 'join needs discussion_id. Use {"action": "list"}'),
        ({"action": "create", "text": "opening"}, "create needs title"),
    ]:
        result, context = await dispatch(board, arguments)
        assert result["error"]["code"] == "invalid_arguments"
        assert message in result["error"]["message"]
        assert context._delivery_receipts == []
    # Dispatch rejects a name that is not a parameter before the handler.
    rejected, _ = await dispatch(board, {"action": "list", "unavailable_feature": 1})
    assert '"unavailable_feature" is not a parameter' in rejected["error"]["message"]
    assert not await board_posts(board)


@pytest.mark.asyncio
async def test_create_retains_opening_and_announcement_for_inactive_peers(board):
    peer = board.bindings[1].participant_id
    await board.store.set_participant_state(board.swarm["id"], peer, "cancelled")
    created, _ = await call(
        board,
        {
            "action": "create",
            "title": "Review",
            "text": "Opening",
            "recipients": [peer],
        },
    )
    assert created["ok"]
    assert len((await board.store.prepare_inbox_delivery(board.swarm["id"], peer))["entries"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,arguments",
    [
        ("swarm_board", {"request": {"operation": "LIST", "limti": "1"}}),
        ("swarm_inbox", {"receive": {"limit": "1"}}),
        ("swarm_state", {"operation": "STATUS", "limit": "1.0"}),
    ],
)
async def test_scoped_tools_repair_arguments_and_accept_matching_identity(board, name, arguments):
    context = replace(board.contexts[0], tool_name=name, session_tool_grants=(name,))
    result = await board.tools.dispatch(
        context,
        {
            **arguments,
            "swarm_id": board.swarm["id"],
            "participant_id": board.bindings[0].participant_id,
        },
        allowed_tools=[name],
    )
    assert result["ok"], result
    rejected = await board.tools.dispatch(
        context, {**arguments, "swarm_id": "foreign"}, allowed_tools=[name]
    )
    assert not rejected["ok"]


def _ids(board):
    return [participant["id"] for participant in board.swarm["participants"]]


@pytest.mark.asyncio
async def test_board_runs_clear_intent_after_repairing_the_call_shape(board):
    posted, _ = await dispatch(board, {"text": "inferred post", "request_id": "agent-chosen"})
    assert posted["ok"], posted
    first = posted["data"]["post_id"]
    reply, _ = await dispatch(
        board,
        {"arguments": {"action": "respond", "body": "answer", "in_reply_to": first}},
        peer=1,
    )
    assert reply["ok"], reply
    created, _ = await dispatch(board, {"subject": "Plan", "message": "opening"})
    assert created["data"]["status"] == "You joined it, and the main discussion announces it."
    read, _ = await dispatch(board, {"action": "history", "post_id": first}, peer=2)
    assert read["data"]["content"] == (
        f"[{first}] {_name(board, 0)} (in the main discussion d1):\ninferred post"
    )
    posts = await board_posts(board)
    assert [(post["text"], post["reply_to"]) for post in posts[:2]] == [
        ("inferred post", None),
        ("answer", posts[0]["id"]),
    ]
    topic = await board_posts(board, discussion_id=created["data"]["discussion_id"])
    assert [post["text"] for post in topic] == ["opening"]
    # The same repairs never pick one of two differing instructions.
    for arguments, message in [
        ({"text": "one", "body": "two"}, "text"),
        ({"action": "post", "arguments": {"action": "read"}}, "action"),
        ({"action": "status"}, "Use swarm_state to see participants"),
        ({"action": "check_inbox"}, 'Use {"action": "read"} to read the newest posts'),
    ]:
        result, _ = await dispatch(board, arguments)
        assert result["error"]["code"] == "invalid_arguments", result
        assert message in result["error"]["message"]
    assert len(await board_posts(board)) == len(posts)


def _reaches(*names):
    listed = " and ".join(names)
    return f"It reaches {listed} in full because it addresses or answers them."


@pytest.mark.asyncio
async def test_board_addresses_participants_its_text_names_after_at_or_answers(board):
    ids = _ids(board)
    one, two = _name(board, 1), _name(board, 2)
    named, _ = await dispatch(board, {"text": f"@{one}, can you check the parser?"})
    assert named["data"]["delivery"] == f"Queued for 2 participants. {_reaches(one)}"
    handle, _ = await dispatch(board, {"text": f"thanks @{two.lower()} and @{ids[1]}"})
    assert handle["data"]["delivery"] == f"Queued for 2 participants. {_reaches(one, two)}"
    # A name or ID without "@" credits or mentions a participant; it addresses no one.
    mentioned, _ = await dispatch(
        board, {"text": f"{one} fixed it, {two} and {ids[1]} confirmed; see x@{one} or @ {two}"}
    )
    assert mentioned["data"]["delivery"] == "Queued for 2 participants."
    answer, _ = await dispatch(
        board, {"text": "done", "reply_to": named["data"]["post_id"]}, peer=2
    )
    assert answer["data"]["delivery"] == (f"Queued for 2 participants. {_reaches(_name(board, 0))}")
    saved = {post["text"]: post["recipients"] for post in await board_posts(board)}
    assert saved[f"@{one}, can you check the parser?"] == [ids[1]]
    assert saved["done"] == [ids[0]]
    routes = {post["text"]: post.get("route_class") for post in await board_posts(board, peer=1)}
    assert routes[f"@{one}, can you check the parser?"] == "ping"
    assert routes["done"] == "main"
    read, _ = await dispatch(board, {"action": "read"}, peer=1)
    assert f"{_name(board, 0)} (to you and {two}):\nthanks" in read["data"]["content"]


@pytest.mark.asyncio
async def test_board_accepts_explicit_recipients_and_never_guesses_one(board):
    # Agents address with @Name; explicit recipients and reply_to stay accepted unadvertised.
    names = ["swarm_board"]
    definition = board.tools.provider_definitions(names, session_grants=names)[0]
    assert not {"recipients", "reply_to"} & set(definition["parameters"]["properties"])
    ids = _ids(board)
    named, _ = await dispatch(board, {"text": "to one", "recipients": [f"@{_name(board, 1)}"]})
    assert named["data"]["delivery"] == f"Queued for 2 participants. {_reaches(_name(board, 1))}"
    everyone, _ = await dispatch(board, {"text": "to all", "to": "all"})
    assert everyone["data"]["delivery"] == (
        f"Queued for 2 participants. {_reaches(_name(board, 1), _name(board, 2))}"
    )
    user, _ = await dispatch(board, {"text": "to user", "recipients": ["the user"]})
    assert user["data"]["note"] == (
        "The user is not a participant and sees every Board post, so the user needs no "
        "recipient entry."
    )
    assert user["data"]["delivery"] == "Queued for 2 participants."
    listed, _ = await dispatch(
        board,
        {"text": "listed", "recipients": [f"{_name(board, 1)}; prt_{_name(board, 2).lower()}"]},
    )
    assert listed["data"]["delivery"] == everyone["data"]["delivery"]
    saved = {post["text"]: sorted(post["recipients"]) for post in await board_posts(board)}
    assert saved == {
        "to one": [ids[1]],
        "to all": sorted(ids[1:]),
        "to user": [],
        "listed": sorted(ids[1:]),
    }
    read, _ = await dispatch(board, {"action": "read"}, peer=1)
    assert f"{_name(board, 0)} (to you and {_name(board, 2)}):\nto all" in read["data"]["content"]

    near = ids[1][:-1] + ("x" if ids[1][-1] != "x" else "y")
    corrected, _ = await dispatch(board, {"text": "typo", "recipients": [near, _name(board, 2)]})
    assert corrected["error"]["code"] == "invalid_recipient"
    assert f'Did you mean {_name(board, 1)} for "{near}"?' in (corrected["error"]["message"])
    assert corrected["error"]["message"].endswith(
        f'Repeat the call with recipients ["{_name(board, 1)}", "{_name(board, 2)}"]; "all" '
        "addresses every other participant. Nothing was saved."
    )
    unknown, _ = await dispatch(board, {"text": "typo", "recipients": ["Nobody"]})
    assert unknown["error"]["code"] == "invalid_recipient"
    assert (
        f"Participants: {_name(board, 0)} (you), {_name(board, 1)}" in (unknown["error"]["message"])
    )
    assert (
        "Repeat the call with recipients chosen from these participants"
        in (unknown["error"]["message"])
    )
    assert "typo" not in {post["text"] for post in await board_posts(board)}


@pytest.mark.asyncio
async def test_board_corrects_read_references_but_never_write_targets(board):
    created, _ = await dispatch(board, {"title": "Topic", "text": "opening"})
    topic = created["data"]["discussion_id"]
    posted, _ = await dispatch(board, {"text": "first main post"})
    first = posted["data"]["post_id"]
    assert (topic, first) == ("d2", "#3")
    stored = next(post for post in await board_posts(board) if post["sequence"] == 3)["id"]

    # A number is an exact reference, with or without "#", for reads and writes alike.
    by_number, _ = await dispatch(board, {"action": "read", "message_id": "3"})
    assert by_number["data"]["content"].endswith(":\nfirst main post")
    assert "note" not in by_number["data"]
    answer, _ = await dispatch(board, {"text": "answer", "reply_to": first}, peer=1)
    assert answer["ok"] and _reaches(_name(board, 0)) in answer["data"]["delivery"]
    # Every post is older than a number past the newest post.
    ahead, _ = await dispatch(board, {"action": "read", "before": "#9"})
    assert ahead["data"]["note"] == (
        'before "#9" names no post yet; the newest post is #4, so this shows the newest posts.'
    )
    assert ahead["data"]["page"].startswith("Newest posts of the main discussion (d1)")
    assert ahead["data"]["content"].endswith(":\nanswer")
    unposted, _ = await dispatch(board, {"action": "read", "message_id": "#9"})
    assert unposted["error"]["message"] == (
        'message_id "#9" names no post yet; the newest post is #4.'
    )

    near_post = stored[:-1] + ("x" if stored[-1] != "x" else "y")
    close_message, _ = await dispatch(board, {"action": "read", "message_id": near_post})
    assert close_message["data"]["note"] == (
        f'message_id "{near_post}" does not exist; this uses post {first}, its only close match.'
    )
    reply, _ = await dispatch(board, {"text": "misreplied", "reply_to": near_post})
    assert reply["error"]["code"] == "message_not_found"
    assert reply["error"]["message"].startswith(f'reply_to "{near_post}" is not a post ID')
    assert reply["error"]["message"].endswith(
        f'Repeat the call with reply_to "{first}". Nothing was saved.'
    )

    topic_id = next(
        row["id"]
        for row in (
            await board.store.list_discussions(board.swarm["id"], board.bindings[0].participant_id)
        ).entries
        if row["sequence"] == 2
    )
    near = topic_id[:-1] + ("x" if topic_id[-1] != "x" else "y")
    close_read, _ = await dispatch(board, {"action": "read", "discussion_id": near})
    assert close_read["data"]["content"].endswith(":\nopening")
    assert close_read["data"]["note"] == (
        f'discussion_id "{near}" does not exist; this shows discussion "Topic" (d2), its only '
        "close match."
    )
    close_post, _ = await dispatch(board, {"text": "misaddressed", "discussion_id": near})
    assert close_post["error"]["code"] == "discussion_not_found"
    assert 'Repeat the call with discussion_id "d2".' in close_post["error"]["message"]

    older, _ = await dispatch(board, {"action": "read", "cursor": first})
    assert older["data"]["note"] == "cursor #3 is a post ID, so this shows the posts before it."
    assert older["data"]["page"].startswith("Posts before #3 in the main discussion (d1)")
    conflict, _ = await dispatch(board, {"action": "read", "discussion_id": topic, "before": first})
    assert conflict["error"]["message"].startswith(
        'before #3 belongs to the main discussion (d1), but discussion_id names discussion "Topic" '
        "(d2)."
    )
    texts = [post["text"] for post in await board_posts(board)]
    assert "misreplied" not in texts
    assert "misaddressed" not in texts


@pytest.mark.asyncio
async def test_board_extra_targets_run_only_when_they_change_nothing(board):
    main = board.swarm["main_discussion_id"]
    first, _ = await dispatch(board, {"title": "First", "text": "opening"})
    existing = first["data"]["discussion_id"]
    inside, _ = await dispatch(
        board, {"action": "create", "discussion_id": existing, "title": "Second", "text": "x"}
    )
    assert inside["error"]["message"].startswith(
        "create opens a new discussion, but discussion_id names the existing discussion "
        f"{existing}."
    )
    in_main, _ = await dispatch(
        board, {"action": "create", "discussion_id": main, "title": "Second", "text": "x"}
    )
    assert in_main["ok"] and "note" not in in_main["data"]
    unknown, _ = await dispatch(
        board, {"action": "create", "discussion_id": "new-topic", "title": "Third", "text": "x"}
    )
    assert unknown["data"]["note"] == (
        'discussion_id "new-topic" names no discussion and was ignored; the new discussion has '
        "its own ID."
    )
    listed = await board.store.list_discussions(board.swarm["id"], board.bindings[0].participant_id)
    assert sorted(row["title"] for row in listed.entries) == ["First", "Main", "Second", "Third"]

    target = first["data"]["opening_post_id"]
    # A post that names a post with message_id answers it, as with reply_to.
    only_message, _ = await dispatch(
        board, {"action": "post", "message_id": target, "text": "answer"}
    )
    assert only_message["ok"], only_message
    differing, _ = await dispatch(
        board, {"action": "post", "message_id": main, "reply_to": target, "text": "other"}
    )
    assert differing["error"]["message"] == (
        "message_id and reply_to name different posts. Repeat the call with only reply_to, set "
        "to the post you answer. Nothing was saved."
    )
    same, _ = await dispatch(
        board, {"action": "post", "message_id": target, "reply_to": target, "text": "again"}
    )
    assert same["ok"], same
    replies = [
        post for post in await board_posts(board, discussion_id=existing) if post["reply_to"]
    ]
    assert sorted((post["text"], f"#{post['reply_sequence']}") for post in replies) == [
        ("again", target),
        ("answer", target),
    ]


@pytest.mark.asyncio
async def test_board_clamps_page_size_and_drops_paging_fields_it_cannot_use(board):
    for index in range(3):
        await dispatch(board, {"text": f"post {index}"})
    clamped, _ = await dispatch(board, {"action": "read", "limit": 500})
    assert clamped["data"]["note"] == "limit 500 is above the maximum of 100; used 100."
    assert "(3 shown)" in clamped["data"]["page"]
    ignored, _ = await dispatch(board, {"action": "post", "text": "paged", "limit": 5})
    assert ignored["data"]["note"] == "limit is not used by post and was ignored."
    assert "paged" in {post["text"] for post in await board_posts(board)}
    rejected, _ = await dispatch(board, {"action": "list", "limit": 0})
    assert rejected["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_board_results_read_as_plain_text(board):
    posted, _ = await dispatch(board, {"text": "line one\nline two", "recipients": ["all"]})
    assert visible(posted) == (
        "post_id: #1\ndiscussion: the main discussion (d1)\n"
        f"delivery: Queued for 2 participants. {_reaches(_name(board, 1), _name(board, 2))}"
    )
    await dispatch(board, {"text": "second"}, peer=1)
    read, _ = await dispatch(board, {"action": "read", "limit": 1}, peer=2)
    assert visible(read) == (
        "page: Newest posts of the main discussion (d1), oldest first (1 shown).\n"
        "older: Older posts exist. Continue with "
        '{"action": "read", "discussion_id": "d1", "before": "#2", "limit": 1}\n\n'
        f"[#2] {_name(board, 1)}:\nsecond"
    )


@pytest.mark.asyncio
async def test_goal_is_pinned_user_post_without_automatic_delivery(board):
    goal = await board.service.board(board.contexts[0], {"action": "read", "message_id": "#0"})
    assert goal["data"]["content"] == "[#0] User (in the main discussion d1):\nfixture-goal"
    assert all(item["pending_count"] == 0 for item in board.swarm["participants"])
    board_read = await board.service.board(board.contexts[0], {"action": "read"})
    assert board_read["data"]["page"] == "No posts in the main discussion (d1) yet."
    assert board_read["data"]["user_request"] == (
        'Post #0 holds the user\'s request. Read it with {"action": "read", "message_id": "#0"}'
    )
    listed = await board.service.board(board.contexts[0], {"action": "list"})
    assert listed["data"]["user_request"].startswith("Post #0 holds the user's request.")
