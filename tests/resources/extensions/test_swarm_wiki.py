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
async def test_wiki_parallel_disjoint_edits_rebase_and_replay_after_restart(board):
    created = await invoke(
        board,
        {
            "action": "create",
            "title": "Notes",
            "content": "First finding\nSecond finding",
            "request_id": "create",
        },
    )
    page_id = created["data"]["page_id"]
    common = {"action": "update", "page_id": page_id, "expected_revision": 1}
    edits = [
        {
            **common,
            "old_text": "First finding",
            "new_text": "First verified",
            "request_id": "first",
        },
        {
            **common,
            "old_text": "Second finding",
            "new_text": "Second verified",
            "request_id": "second",
        },
    ]
    results = await asyncio.gather(*(invoke(board, edit, peer) for peer, edit in enumerate(edits)))
    assert all(result["ok"] for result in results), results
    assert sorted(result["data"]["revision"] for result in results) == [2, 3]
    await board.store.close()
    await board.store.open()
    for peer, edit in enumerate(edits):
        replay = await invoke(board, edit, peer)
        assert replay["data"] == {**results[peer]["data"], "replayed": True}
    current = (await invoke(board, {"action": "read", "page_id": page_id}))["data"]
    assert current["content"] == "First verified\nSecond verified"
    assert current["revision"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize(
    ("content", "old", "new", "expected"),
    [
        (
            "Before\r\nFirst\r\nSecond\r\nAfter",
            "First\nSecond",
            "Changed\nSecond",
            "Before\r\nChanged\r\nSecond\r\nAfter",
        ),
        ("Before\n“Ready” — wait…\nAfter", '"Ready" -- wait...', "Done", "Before\nDone\nAfter"),
        (
            "Before\n    First\n    Second\nAfter",
            "First\nSecond",
            "Changed\nSecond",
            "Before\n    Changed\n    Second\nAfter",
        ),
        (
            "Before\nFirst   finding\nAfter",
            "First finding",
            "Verified finding",
            "Before\nVerified finding\nAfter",
        ),
        ('"Ready" and “Ready”', '"Ready"', "Done", "Done and “Ready”"),
        ("Before\nDelete this\nAfter", "Delete this\n", "", "Before\nAfter"),
    ],
)
async def test_wiki_tolerant_matching_preserves_surrounding_content(
    board, stale, content, old, new, expected
):
    created = await invoke(
        board,
        {
            "action": "create",
            "title": "Notes",
            "content": content,
            "request_id": "create",
        },
    )
    page_id = created["data"]["page_id"]
    if stale:
        renamed = await board.service.operation(
            "wiki",
            {
                "swarm_id": board.swarm["id"],
                "action": "update",
                "page_id": page_id,
                "expected_revision": 1,
                "title": "Peer title",
                "request_id": "rename",
            },
        )
        assert renamed["revision"] == 2
    result = await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "expected_revision": 1,
            "old_text": old,
            "new_text": new,
            "request_id": "edit",
        },
    )
    assert result["ok"], result
    current = (await invoke(board, {"action": "read", "page_id": page_id}))["data"]
    assert current["content"] == expected
    assert current["title"] == ("Peer title" if stale else "Notes")
    assert current["revision"] == (3 if stale else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"content": "replacement"},
        {"title": "replacement"},
        {"old_text": "First finding", "new_text": "Verified", "title": "replacement"},
        {"action": "delete"},
        {"action": "restore", "revision": 1},
        {"old_text": "First finding", "new_text": "Verified", "expected_revision": 99},
        {"old_text": "Missing", "new_text": "Verified"},
    ],
)
async def test_wiki_stale_or_future_unsafe_changes_remain_atomic(board, change):
    created = await invoke(
        board,
        {
            "action": "create",
            "title": "Notes",
            "content": "First finding",
            "request_id": "create",
        },
    )
    page_id = created["data"]["page_id"]
    await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "expected_revision": 1,
            "title": "Peer title",
            "request_id": "rename",
        },
        1,
    )
    read = {"action": "read", "page_id": page_id}
    before = await invoke(board, read)
    result = await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "expected_revision": 1,
            "request_id": "unsafe",
            **change,
        },
    )
    assert result["error"]["code"] == "wiki_revision_conflict"
    assert await invoke(board, read) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize(
    ("content", "old"),
    [
        ("same same", "same"),
        ("“Ready” and “Ready”", '"Ready"'),
        ("  First\n  Second\n    First\n    Second", "First\nSecond"),
    ],
)
async def test_wiki_ambiguous_matches_never_fall_through(board, stale, content, old):
    created = await invoke(
        board,
        {
            "action": "create",
            "title": "Notes",
            "content": content,
            "request_id": "create",
        },
    )
    page_id = created["data"]["page_id"]
    if stale:
        await invoke(
            board,
            {
                "action": "update",
                "page_id": page_id,
                "expected_revision": 1,
                "title": "Peer title",
                "request_id": "rename",
            },
            1,
        )
    read = {"action": "read", "page_id": page_id}
    before = await invoke(board, read)
    result = await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "expected_revision": 1,
            "old_text": old,
            "new_text": "Changed",
            "request_id": "edit",
        },
    )
    assert not result["ok"]
    assert await invoke(board, read) == before


@pytest.mark.asyncio
async def test_wiki_similar_passage_never_stands_in_for_old_text(board):
    created = await invoke(
        board,
        {
            "action": "create",
            "title": "Notes",
            "content": "Start\nThe result is pending.\nEnd",
            "request_id": "create",
        },
    )
    page_id = created["data"]["page_id"]
    peer_content = "Start\nThe result is verified.\nEnd"
    await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "expected_revision": 1,
            "content": peer_content,
            "request_id": "peer",
        },
        1,
    )
    edit = {
        "action": "update",
        "page_id": page_id,
        "expected_revision": 1,
        "old_text": "Start\nThe result is pending.\nEnd",
        "new_text": "Start\nThe result is complete.\nEnd",
        "request_id": "edit",
    }
    read = {"action": "read", "page_id": page_id}
    assert (await invoke(board, edit))["error"]["code"] == "wiki_revision_conflict"
    assert (await invoke(board, read))["data"]["content"] == peer_content
    # The current revision does not authorize replacing a merely similar passage either.
    current = await invoke(board, {**edit, "expected_revision": 2, "request_id": "current"})
    assert current["error"]["code"] == "wiki_edit_conflict"
    assert (await invoke(board, read))["data"]["content"] == peer_content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "old", "new"),
    [
        # A similar line must not stand in for a different value.
        ("Deployment\nRETRIES = 5\nTIMEOUT = 30\n", "RETRIES = 3", "RETRIES = 4"),
        # Exact boundaries must not let a different middle line be deleted.
        (
            "Steps:\nvalidate(data)\nsave_to_database(data)\ndone\n",
            "Steps:\nvalidate(data)\nlog(data)\ndone",
            "Steps:\nvalidate(data)\ndone",
        ),
    ],
)
async def test_wiki_old_text_must_match_every_line_precisely(board, content, old, new):
    created = await invoke(
        board,
        {"action": "create", "title": "Notes", "content": content, "request_id": "create"},
    )
    page_id = created["data"]["page_id"]
    read = {"action": "read", "page_id": page_id}
    before = await invoke(board, read)

    result = await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "expected_revision": 1,
            "old_text": old,
            "new_text": new,
            "request_id": "edit",
        },
    )

    assert result["error"]["code"] == "wiki_edit_conflict"
    assert await invoke(board, read) == before


@pytest.mark.asyncio
async def test_wiki_stale_edit_cannot_revive_deleted_page_or_exceed_size_limit(board):
    created = await invoke(
        board,
        {
            "action": "create",
            "title": "Notes",
            "content": "First\n" + "x" * 199990,
            "request_id": "create",
        },
    )
    page_id = created["data"]["page_id"]
    edit = {
        "action": "update",
        "page_id": page_id,
        "expected_revision": 1,
        "old_text": "First",
        "new_text": "replacement" * 10,
        "request_id": "edit",
    }
    await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "expected_revision": 1,
            "title": "Peer title",
            "request_id": "rename",
        },
        1,
    )
    assert (await invoke(board, edit))["error"]["code"] == "invalid_arguments"
    assert (await invoke(board, {"action": "read", "page_id": page_id}))["data"]["revision"] == 2
    deleted = await invoke(
        board,
        {
            "action": "delete",
            "page_id": page_id,
            "expected_revision": 2,
            "request_id": "delete",
        },
        1,
    )
    assert deleted["ok"]
    assert (await invoke(board, {**edit, "new_text": "Done"}))["error"]["code"] == "wiki_deleted"
    current = (await invoke(board, {"action": "read", "page_id": page_id}))["data"]
    assert current["deleted"]
    assert current["revision"] == 3


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
    goal_id, main = board.swarm["goal_post_id"], board.swarm["main_discussion_id"]
    assert goal["data"]["content"] == (
        f"[{goal_id}] User (in the main discussion {main}):\nfixture-goal"
    )
    assert all(item["pending_count"] == 0 for item in board.swarm["participants"])
    board_read = await board.service.board(board.contexts[0], {"action": "read"})
    assert board_read["data"]["page"] == f"No posts in the main discussion ({main}) yet."
    assert board_read["data"]["user_request"] == (
        f"Post {goal_id} holds the user's request. Read it with "
        f'{{"action": "read", "message_id": "{goal_id}"}}'
    )
    listed = await board.service.board(board.contexts[0], {"action": "list"})
    assert listed["data"]["user_request"].startswith(f"Post {goal_id} holds the user's request.")


@pytest.mark.asyncio
async def test_twelve_peers_can_edit_independent_passages_from_the_same_revision(tmp_path):
    from resources.extensions.swarm.store import SwarmStore
    from tests.resources.extensions.swarm_store_helpers import _swarm, open_swarm_database

    database = open_swarm_database(tmp_path)
    store = SwarmStore(database)
    await store.open()
    try:
        started = await _swarm(store, count=12)
        sid = started["swarm_id"]
        peers = (await store.get_swarm(sid))["participants"]
        created = await store.wiki(
            sid,
            peers[0]["id"],
            {
                "action": "create",
                "title": "Shared findings",
                "content": "\n".join(f"Finding {i:02}: pending" for i in range(12)),
                "request_id": "create-findings",
            },
        )
        edits = await asyncio.gather(
            *(
                store.wiki(
                    sid,
                    peer["id"],
                    {
                        "action": "update",
                        "page_id": created["page_id"],
                        "expected_revision": 1,
                        "old_text": f"Finding {i:02}: pending",
                        "new_text": f"Finding {i:02}: verified",
                        "request_id": f"finding-{i}",
                    },
                )
                for i, peer in enumerate(peers)
            )
        )
        assert sorted(edit["revision"] for edit in edits) == list(range(2, 14))
        current = await store.wiki(sid, None, {"action": "read", "page_id": created["page_id"]})
        assert current["content"] == "\n".join(f"Finding {i:02}: verified" for i in range(12))
        assert current["revision"] == 13
    finally:
        await store.close()
        database.close()
