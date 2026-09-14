import asyncio
from dataclasses import replace
from itertools import count

import pytest

from core.runs import RunAdmissionBlockedError
from tests.resources.extensions.test_swarm_board import board as board

requests = count()


async def invoke(board, args, peer=0):
    context = replace(
        board.contexts[peer], tool_name="swarm_decisions", session_tool_grants=("swarm_decisions",)
    )
    return await board.tools.dispatch(context, args, allowed_tools=["swarm_decisions"])


async def change(board, action, **kwargs):
    result = await invoke(
        board, {"action": action, "request_id": f"{action}-{next(requests)}", **kwargs}
    )
    assert result["ok"], result
    return result["data"]


async def question(board):
    created = await change(
        board, "create", title="2D or 3D?", text="Compare quality and feasibility."
    )
    qid = created["question"]["question_id"]
    first = await change(board, "add_option", question_id=qid, expected_revision=1, title="2D")
    second = await change(board, "add_option", question_id=qid, expected_revision=2, title="3D")
    return qid, first["option"]["option_id"], second["option"]["option_id"]


@pytest.mark.asyncio
async def test_independent_support_moves_and_new_options_preserve_old_positions(board):
    qid, flat, spatial = await question(board)
    position = {
        "action": "position",
        "question_id": qid,
        "expected_revision": 3,
        "position_revision": 0,
        "option_id": flat,
        "request_id": "support",
    }
    results = await asyncio.gather(*(invoke(board, position, peer) for peer in range(3)))
    assert all(result["ok"] for result in results)
    assert (await invoke(board, position))["data"]["replayed"]
    current = (await invoke(board, {"action": "read", "question_id": qid}))["data"]
    assert current["entries"][0]["reviewed_support"] == 3
    assert current["participation"]["not_positioned"] == 0
    assert current["question"]["revision"] == 3
    added = await change(board, "add_option", question_id=qid, expected_revision=3, title="2.5D")
    new_option = added["option"]["option_id"]
    stale = await invoke(board, {**position, "position_revision": 1, "request_id": "stale"})
    assert stale["error"]["code"] == "decision_conflict"
    current = (await invoke(board, {"action": "read", "question_id": qid}))["data"]
    assert current["entries"][0]["support"] == 3
    assert current["entries"][0]["reviewed_support"] == 0
    assert current["my_position"]["needs_review"]
    await change(
        board,
        "position",
        question_id=qid,
        expected_revision=4,
        position_revision=1,
        option_id=new_option,
        note="Better scope while preserving depth.",
    )
    current = (await invoke(board, {"action": "read", "question_id": qid}))["data"]
    assert [option["support"] for option in current["entries"]] == [2, 0, 1]
    assert current["entries"][2]["reviewed_support"] == 1
    await change(
        board,
        "update_option",
        question_id=qid,
        expected_revision=4,
        option_id=new_option,
        text="Use prerendered sprites.",
    )
    current = (await invoke(board, {"action": "read", "question_id": qid}))["data"]
    assert current["my_position"]["option_changed"]
    await change(
        board,
        "update_option",
        question_id=qid,
        expected_revision=5,
        option_id=new_option,
        withdrawn=True,
    )
    assert (
        await invoke(
            board,
            {
                **position,
                "expected_revision": 6,
                "position_revision": 2,
                "option_id": new_option,
                "request_id": "withdrawn",
            },
        )
    )["error"]["code"] == "option_withdrawn"
    await change(board, "withdraw", question_id=qid, position_revision=2)
    assert (await invoke(board, {"action": "read", "question_id": qid}))["data"]["participation"][
        "not_positioned"
    ] == 1
    assert all(
        p["pending_count"] == 0
        for p in (await board.store.get_swarm(board.swarm["id"]))["participants"]
    )


@pytest.mark.asyncio
async def test_decision_history_frozen_pages_scope_archive_and_own_position_guard(board):
    qid, flat, spatial = await question(board)
    for peer in range(3):
        result = await invoke(
            board,
            {
                "action": "position",
                "question_id": qid,
                "expected_revision": 3,
                "position_revision": 0,
                "note": f"Concern {peer}",
                "request_id": "concern",
            },
            peer,
        )
        assert result["ok"]
    read = {"action": "read", "question_id": qid, "section": "positions", "limit": 1}
    page = (await invoke(board, read))["data"]
    await change(
        board,
        "position",
        question_id=qid,
        expected_revision=3,
        position_revision=1,
        option_id=spatial,
    )
    continuation = (await invoke(board, page["next_call"]["arguments"]))["data"]
    last = (await invoke(board, continuation["next_call"]["arguments"]))["data"]
    assert last["entries"][0]["note"] == "Concern 0"
    assert not (await invoke(board, {**page["next_call"]["arguments"], "section": "options"}))["ok"]
    assert (
        await invoke(
            board,
            {
                "action": "withdraw",
                "question_id": qid,
                "position_revision": 1,
                "request_id": "stale-own",
            },
        )
    )["error"]["code"] == "position_conflict"
    assert not (await invoke(board, {"action": "read", "question_id": qid, "swarm_id": "foreign"}))[
        "ok"
    ]
    assert not (await invoke(board, {"action": "read", "question_id": "foreign"}))["ok"]
    await change(board, "update", question_id=qid, expected_revision=3, archived=True)
    assert (await invoke(board, {"action": "list"}))["data"]["entries"] == []
    assert (
        len((await invoke(board, {"action": "list", "include_archived": True}))["data"]["entries"])
        == 1
    )
    await change(board, "reconsider", question_id=qid, expected_revision=4)
    history = (await invoke(board, {"action": "history", "question_id": qid}))["data"]
    assert history["entries"][0]["action"] == "reconsider"
    assert len(history["entries"]) == 9
    await board.store.close()
    await board.store.open()
    assert (await invoke(board, {"action": "read", "question_id": qid}))["data"]["my_position"][
        "option_id"
    ] == spatial
    await board.service.operation(
        "swarms.stop", {"swarm_id": board.swarm["id"], "request_id": "stop"}
    )
    await board.service.operation("swarms.delete", {"swarm_id": board.swarm["id"]})
    with pytest.raises(RunAdmissionBlockedError):
        await invoke(board, {"action": "read", "question_id": qid})


@pytest.mark.asyncio
async def test_linked_board_posts_return_current_question_revision(board):
    qid, _, _ = await question(board)
    post = await board.service.board(
        board.contexts[1],
        {
            "action": "post",
            "text": f"Consider [alternatives](#decision/{qid}/3)",
            "request_id": "link",
        },
    )
    assert post["ok"]
    await change(board, "reconsider", question_id=qid, expected_revision=3)
    read = await board.service.board(board.contexts[0], {"action": "read"})
    link = read["data"]["linked_decisions"][0]
    assert link["changed"] and link["revision"] == 4
    assert link["referenced_revisions"] == [3]
    assert link["next_call"]["arguments"]["question_id"] == qid
