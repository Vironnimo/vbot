"""Swarm state Tool: participant status, delivery policy and the paged roster."""

from dataclasses import replace

import pytest

from core.tools import ToolContractError
from resources.extensions.swarm.agent_text import (
    STATE_DELIVERY,
    STATE_PENDING,
    STATE_ROUTES,
    STATE_WAIT,
    STATE_WAKE_ADDRESSED,
)
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
    # When every route delivers while running, the pending line says the message needs no
    # call, whether or not the Session offers swarm_inbox, and replaces the delivery field.
    automatic, _ = await dispatch(board, {}, name="swarm_state")
    assert set(automatic["data"]) == {"pending", "wake", "content"}
    assert automatic["data"]["pending"] == STATE_PENDING["one_automatic"]
    # With nothing to receive, the reader is polling: ending the reply is how to wait.
    author, _ = await dispatch(board, {}, peer=1, name="swarm_state")
    assert author["data"]["pending"] == f"{STATE_PENDING['none_automatic']} {STATE_WAIT}"
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
    # Participants know one another by name only; the roster marks the reader and is
    # complete, so neither the reader's state nor state totals repeat it.
    roster = "\n".join(
        f"- {_name(board, peer)}{' (you)' if peer == 0 else ''}: idle"
        for peer in range(len(board.bindings))
    )
    pending = "pending: " + STATE_PENDING["one" if inbox else "one_no_inbox"]
    main = STATE_DELIVERY["on_request" if inbox else "on_request_no_inbox"].format(
        routes="Main-discussion posts"
    )
    lines = visible(result).split("\n")
    assert lines[0] == pending
    assert lines[1].startswith("delivery: ") and lines[1].endswith(main)
    # Wake pacing delays the routes that do not address the reader, up to the longest Quiet
    # period.
    assert lines[2] == "wake: " + STATE_WAKE_ADDRESSED.format(
        routes=STATE_ROUTES["discussion"], minutes=4
    )
    assert "\n".join(lines[3:]) == f"\nParticipants:\n{roster}"
    if inbox:
        delivered, _ = await dispatch(board, {}, name="swarm_inbox")
        assert [
            (message.author, message.text) for message in received(delivered["data"]["content"])
        ] == [(_name(board, 1), "Question")]
        # The Inbox receipt was never saved with a Tool Result, so the post stays pending.
        again, _ = await dispatch(board, {}, name="swarm_state")
        assert visible(again).splitlines()[0] == pending
    # Where no post starts a Run, ending the reply waits for nothing.
    swarm = await board.store.get_swarm(board.swarm["id"])
    await board.store.apply_delivery_settings(
        swarm["id"],
        {
            **swarm["delivery"],
            **{route: {"mode": "all", "wake_idle": False} for route in STATE_ROUTES},
        },
        expected_revision=swarm["settings_revision"],
        request_id="no-wake",
        actor="test",
    )
    sleeping, _ = await dispatch(board, {}, peer=1, name="swarm_state")
    assert sleeping["data"]["pending"] == STATE_PENDING["none_automatic"]


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
    # Beside a partial roster, totals cover the participants it does not list.
    assert first["participants"] == "3 (3 idle)"
    assert first["more"].startswith("More participants exist. Continue with ")
    follow = continuation(first["more"])
    assert set(follow) == {"limit", "cursor"} and follow["limit"] == 1
    second = (await board.tools.dispatch(context, follow, allowed_tools=["swarm_state"]))["data"]
    assert second["content"] == f"Participants:\n- {_name(board, 1)}: idle"
    # Another page size continues from the same place.
    changed = (
        await board.tools.dispatch(context, {**follow, "limit": 2}, allowed_tools=["swarm_state"])
    )["data"]
    assert changed["content"] == (
        f"Participants:\n- {_name(board, 1)}: idle\n- {_name(board, 2)}: idle"
    )
    assert "more" not in changed
    whole = (await board.tools.dispatch(context, {}, allowed_tools=["swarm_state"]))["data"]
    assert whole["content"].count("\n- ") == 3 and "more" not in whole
    assert "participants" not in whole
    clamped = (await board.tools.dispatch(context, {"limit": 500}, allowed_tools=["swarm_state"]))[
        "data"
    ]
    assert clamped["note"] == "limit 500 is above the maximum of 100; used 100."
    assert clamped["content"].count("\n- ") == 3 and "more" not in clamped
    assert not context._turn_end_requested
