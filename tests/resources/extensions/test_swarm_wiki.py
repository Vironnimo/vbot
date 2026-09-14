import asyncio
from dataclasses import replace

import pytest

from resources.extensions.swarm.store import SwarmStoreError
from tests.resources.extensions.test_swarm_board import board as board


async def invoke(fixture, arguments, peer=0):
    context = replace(
        fixture.contexts[peer], tool_name="swarm_wiki", session_tool_grants=("swarm_wiki",)
    )
    return await fixture.tools.dispatch(context, arguments, allowed_tools=["swarm_wiki"])


@pytest.mark.asyncio
async def test_wiki_collaboration_conflicts_history_delete_restore_and_replay(board):
    create = {
        "action": "create",
        "title": "Research",
        "content": "First finding\nSecond finding",
        "request_id": "create",
    }
    created = await invoke(board, create)
    assert created["ok"], created
    page = created["data"]
    assert (await invoke(board, create))["data"]["page_id"] == page["page_id"]
    update = {
        "action": "update",
        "page_id": page["page_id"],
        "expected_revision": 1,
        "old_text": "Second finding",
        "new_text": "Revised finding",
        "request_id": "update",
    }
    updated, conflict = await asyncio.gather(
        invoke(board, update, 1),
        invoke(board, {**update, "title": "Another title", "request_id": "racing"}, 2),
    )
    assert sum(item["ok"] for item in (updated, conflict)) == 1
    assert (
        next(item for item in (updated, conflict) if not item["ok"])["error"]["code"]
        == "wiki_revision_conflict"
    )
    current = (await invoke(board, {"action": "read", "page_id": page["page_id"]}))["data"]
    assert current["content"] == "First finding\nRevised finding"
    assert current["revision"] == 2
    historical = (
        await invoke(board, {"action": "read", "page_id": page["page_id"], "revision": 1})
    )["data"]
    assert historical["content"] == create["content"]
    deleted = await invoke(
        board,
        {
            "action": "delete",
            "page_id": page["page_id"],
            "expected_revision": 2,
            "request_id": "delete",
        },
    )
    assert deleted["data"]["deleted"]
    assert (await invoke(board, {"action": "list"}))["data"]["entries"] == []
    assert (
        len((await invoke(board, {"action": "list", "include_deleted": True}))["data"]["entries"])
        == 1
    )
    restored = await invoke(
        board,
        {
            "action": "restore",
            "page_id": page["page_id"],
            "expected_revision": 3,
            "revision": 1,
            "request_id": "restore",
        },
    )
    assert restored["data"]["revision"] == 4
    assert not restored["data"]["deleted"]
    assert (await invoke(board, {"action": "read", "page_id": page["page_id"]}))["data"][
        "content"
    ] == create["content"]
    history = (await invoke(board, {"action": "history", "page_id": page["page_id"], "limit": 1}))[
        "data"
    ]
    assert history["entries"][0]["revision"] == 4
    assert (await invoke(board, history["next_call"]["arguments"]))["data"]["entries"][0][
        "revision"
    ] == 3
    assert all(
        peer["pending_count"] == 0
        for peer in (await board.store.get_swarm(board.swarm["id"]))["participants"]
    )
    assert not (await board.store.read_human_posts(board.swarm["id"])).entries


@pytest.mark.asyncio
async def test_wiki_bounded_reads_search_and_frozen_pagination(board):
    content = "a" * 12500 + "\nÜberraschung"
    pages = []
    for index in range(3):
        pages.append(
            (
                await invoke(
                    board,
                    {
                        "action": "create",
                        "title": f"Page {index}",
                        "content": content,
                        "request_id": str(index),
                    },
                )
            )["data"]
        )
    result = (await invoke(board, {"action": "read", "page_id": pages[0]["page_id"]}))["data"]
    assert len(result["content"]) == 12000
    await invoke(
        board,
        {
            "action": "update",
            "page_id": pages[0]["page_id"],
            "expected_revision": 1,
            "content": "changed",
            "request_id": "change",
        },
    )
    tail = (await invoke(board, result["next_call"]["arguments"]))["data"]
    assert result["content"] + tail["content"] == content
    search = (await invoke(board, {"action": "list", "query": "ÜBERRASCHUNG", "limit": 1}))["data"]
    assert len(search["entries"]) == 1
    assert "Überraschung" in search["entries"][0]["excerpt"]
    await invoke(
        board,
        {
            "action": "update",
            "page_id": pages[1]["page_id"],
            "expected_revision": 1,
            "content": "changed",
            "request_id": "change-second",
        },
    )
    continuation = (await invoke(board, search["next_call"]["arguments"]))["data"]
    assert continuation["entries"][0]["page_id"] == pages[1]["page_id"]
    assert not (await invoke(board, {**search["next_call"]["arguments"], "query": "other"}))["ok"]


@pytest.mark.asyncio
async def test_wiki_scope_management_recovery_and_targeted_edits(board):
    created = await invoke(
        board, {"action": "create", "title": "Notes", "content": "same same", "request_id": "new"}
    )
    page_id = created["data"]["page_id"]
    edit = {
        "action": "update",
        "page_id": page_id,
        "expected_revision": 1,
        "old_text": "same",
        "new_text": "",
        "request_id": "edit",
    }
    assert (await invoke(board, edit))["error"]["code"] == "wiki_edit_conflict"
    assert (await invoke(board, {**edit, "content": "lost"}))["error"][
        "code"
    ] == "invalid_arguments"
    assert not (await invoke(board, {"action": "read", "page_id": page_id, "swarm_id": "foreign"}))[
        "ok"
    ]
    assert not (await invoke(board, {"action": "read", "page_id": "foreign"}))["ok"]
    other = await board.store.create_swarm(
        board.swarm["profile_snapshot"]["id"],
        "other",
        {"cwd": str(board.contexts[0].workspace)},
        request_id="other",
        expected_profile_revision=1,
    )
    with pytest.raises(SwarmStoreError, match="wiki_page_not_found"):
        await board.store.wiki(other["swarm_id"], None, {"action": "read", "page_id": page_id})
    repaired = await invoke(
        board, {"request": {"operation": "READ", "page_id": page_id, "limti": "4"}}
    )
    assert repaired["data"]["content"] == "same"
    changed = await board.service.operation(
        "wiki",
        {
            "swarm_id": board.swarm["id"],
            "action": "update",
            "page_id": page_id,
            "title": "Renamed",
            "expected_revision": 1,
            "request_id": "human",
        },
    )
    assert changed["author"]["kind"] == "user"
    assert changed["link"].endswith(f"(#wiki/{page_id})")
    whitespace_edit = await invoke(
        board, {**edit, "expected_revision": 2, "old_text": " ", "request_id": "whitespace"}
    )
    assert whitespace_edit["ok"], whitespace_edit
    assert (await invoke(board, {"action": "read", "page_id": page_id}))["data"][
        "content"
    ] == "samesame"
    await board.store.close()
    await board.store.open()
    assert (
        await board.store.wiki(board.swarm["id"], None, {"action": "read", "page_id": page_id})
    )["title"] == "Renamed"
    await board.service.operation(
        "swarms.stop", {"swarm_id": board.swarm["id"], "request_id": "stop"}
    )
    await board.service.operation("swarms.delete", {"swarm_id": board.swarm["id"]})
    with pytest.raises(SwarmStoreError, match="swarm_not_found"):
        await board.store.wiki(board.swarm["id"], None, {"action": "list"})


@pytest.mark.asyncio
async def test_goal_is_pinned_user_post_without_automatic_delivery(board):
    goal = await board.service.board(
        board.contexts[0], {"action": "read", "message_id": board.swarm["goal_post_id"]}
    )
    assert goal["data"]["entries"][0]["text"] == "fixture-goal"
    assert goal["data"]["entries"][0]["author"]["kind"] == "user"
    assert all(item["pending_count"] == 0 for item in board.swarm["participants"])
    board_read = await board.service.board(board.contexts[0], {"action": "read"})
    assert board_read["data"]["goal_post_id"] == board.swarm["goal_post_id"]
