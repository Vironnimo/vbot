"""Swarm state Tool: participant status, delivery policy and the paged roster."""

from dataclasses import replace

import pytest

from core.tools import ToolContractError
from tests.resources.extensions.swarm.swarm_test_support import (
    _name,
    continuation,
    dispatch,
    received,
    visible,
)


@pytest.mark.asyncio
async def test_state_guidance_reaches_native_model_definition(board):
    from resources.extensions.swarm.agent_text import STATE_DESCRIPTION

    names = ("swarm_board", "swarm_inbox", "swarm_state", "swarm_wiki")
    definitions = board.tools.provider_definitions(names, session_grants=names)
    state = next(tool for tool in definitions if tool["name"] == "swarm_state")
    assert state["description"] == STATE_DESCRIPTION
    assert set(state["parameters"]["properties"]) == {"cursor", "limit"}
    assert state["parameters"]["required"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("board", "inbox"), [(True, True), (False, False)], indirect=["board"], ids=["inbox", "board"]
)
async def test_status_delivery_policy_and_pending_messages_enable_direct_receiving(board, inbox):
    peer = board.bindings[1].participant_id
    await board.store.post(board.swarm["id"], peer, text="Question", request_id="pending")
    swarm = await board.store.get_swarm(board.swarm["id"])
    await board.store.apply_delivery_settings(
        swarm["id"],
        {
            **swarm["delivery"],
            "main": {"mode": "pull", "wake_idle": False},
            "discussion": {"mode": "idle", "wake_idle": True},
        },
        expected_revision=swarm["settings_revision"],
        request_id="policy",
        actor="test",
    )
    result, _ = await dispatch(board, {}, name="swarm_state")
    # Participants know one another by name only.
    roster = "\n".join(
        f"- {_name(board, peer)}{' (you)' if peer == 0 else ''}: idle"
        for peer in range(len(board.bindings))
    )
    pending = (
        "pending: 1 message for you; receive it with swarm_inbox."
        if inbox
        else "pending: 1 message for you."
    )
    main = "only through swarm_inbox" if inbox else "only when you read them with swarm_board"
    assert visible(result) == (
        f"you: {_name(board, 0)}, idle\n"
        f"{pending}\n"
        "delivery: Posts that address or answer you reach you automatically, also while you are "
        "running. Posts in discussions you joined reach you automatically when you are idle. "
        f"Main-discussion posts reach you {main}.\n"
        "wake: Posts in discussions you joined and posts that address or answer you start a Run "
        "when you are idle. After a Run in which you used no Tool except to read the Board, the "
        "Wiki or this status, only posts by the user and posts that address or answer you start "
        "your next Run at once; other posts wait up to 4 minutes.\n"
        f"participants: 3 (3 idle)\n\nParticipants:\n{roster}"
    )
    if inbox:
        delivered, _ = await dispatch(board, {}, name="swarm_inbox")
        assert [
            (message.author, message.text) for message in received(delivered["data"]["content"])
        ] == [(_name(board, 1), "Question")]
        # The Inbox receipt was never saved with a Tool Result, so the post stays pending.
        again, _ = await dispatch(board, {}, name="swarm_state")
        assert visible(again).splitlines()[1] == pending


@pytest.mark.asyncio
async def test_state_tool_rejects_all_participant_status_mutations(board):
    context = replace(
        board.contexts[0], tool_name="swarm_state", session_tool_grants=("swarm_state",)
    )
    before = await board.store.participant_status(
        board.swarm["id"], board.bindings[0].participant_id
    )
    for arguments in [
        {"action": "wait"},
        {"action": "wait", "needs_user": True},
        {"action": "done", "summary": "Finished"},
        {"state": "idle"},
        {"action": "name", "name": "Changed"},
        {"name": "Changed"},
        {"include_summaries": True},
    ]:
        with pytest.raises(ToolContractError):
            await board.tools.dispatch(context, arguments, allowed_tools=["swarm_state"])
    assert not context._turn_end_requested
    assert (
        await board.store.participant_status(board.swarm["id"], board.bindings[0].participant_id)
        == before
    )


@pytest.mark.asyncio
async def test_status_pages_only_report_automatic_activity(board):
    context = replace(
        board.contexts[0], tool_name="swarm_state", session_tool_grants=("swarm_state",)
    )
    first = (await board.tools.dispatch(context, {"limit": 1}, allowed_tools=["swarm_state"]))[
        "data"
    ]
    assert first["content"] == f"Participants:\n- {_name(board, 0)} (you): idle"
    assert first["more"].startswith("More participants exist. Continue with ")
    follow = continuation(first["more"])
    assert set(follow) == {"limit", "cursor"} and follow["limit"] == 1
    second = (await board.tools.dispatch(context, follow, allowed_tools=["swarm_state"]))["data"]
    assert second["content"] == f"Participants:\n- {_name(board, 1)}: idle"
    changed = await board.service.state(context, {**follow, "limit": 2})
    assert changed["error"]["code"] == "invalid_cursor"
    clamped = (await board.tools.dispatch(context, {"limit": 500}, allowed_tools=["swarm_state"]))[
        "data"
    ]
    assert clamped["note"] == "limit 500 is above the maximum of 100; used 100."
    assert clamped["content"].count("\n- ") == 3 and "more" not in clamped
    assert not context._turn_end_requested
