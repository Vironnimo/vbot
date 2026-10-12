"""Swarm Wiki Tool: tolerant passage edits, conflicts, history and readable results."""

import asyncio
import re

import pytest

from resources.extensions.swarm.agent_text import REPLAYED
from resources.extensions.swarm.store import SwarmStoreError
from resources.extensions.swarm.wiki_text import (
    WIKI_CHANGES_NONE,
    WIKI_CONFLICT_CONTENT,
    WIKI_CONFLICT_RETRY,
    WIKI_DIFFERENCE,
    WIKI_DIFFERENCE_SPACING,
    WIKI_DIFFERENCES_MORE,
    WIKI_LIST_MATCHES,
    WIKI_LIST_NO_MATCH,
    WIKI_LIST_NO_WORD_MATCH,
    WIKI_LIST_WORD_MATCHES,
    WIKI_OFFSET_FROM_END,
    WIKI_OWN_REVISION,
    WIKI_SINCE_IGNORES,
)
from tests.resources.extensions.swarm.swarm_test_support import (
    _swarm,
    continuation,
    create,
    invoke,
    pages,
    peer_rename,
    stored,
    visible,
)


@pytest.mark.asyncio
async def test_wiki_parallel_disjoint_edits_rebase_and_replay_after_restart(board):
    page_id = await create(board, "First finding\nSecond finding")
    common = {"action": "update", "page_id": page_id, "expected_revision": 1}
    edits = [
        {**common, "old_text": "First finding", "new_text": "First verified"},
        {**common, "old_text": "Second finding", "new_text": "Second verified"},
    ]
    results = await asyncio.gather(
        *(invoke(board, edit, peer, tool_call_id=f"edit-{peer}") for peer, edit in enumerate(edits))
    )
    assert all(result["ok"] for result in results), results
    assert sorted(result["data"]["revision"] for result in results) == [2, 3]
    await board.store.close()
    await board.store.open()
    for peer, edit in enumerate(edits):
        # The same Tool Call replays its result; nothing is applied twice.
        replay = await invoke(board, edit, peer, tool_call_id=f"edit-{peer}")
        assert replay["data"] == {**results[peer]["data"], "note": REPLAYED}
    current = await stored(board, page_id)
    assert current["content"] == "First verified\nSecond verified"
    assert current["revision"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "old", "new", "expected", "stale"),
    [
        (
            "Before\r\nFirst\r\nSecond\r\nAfter",
            "First\nSecond",
            "Changed\nSecond",
            "Before\r\nChanged\r\nSecond\r\nAfter",
            False,
        ),
        (
            "Before\n“Ready” — wait…\nAfter",
            '"Ready" -- wait...',
            "Done",
            "Before\nDone\nAfter",
            True,
        ),
        (
            "Before\n    First\n    Second\nAfter",
            "First\nSecond",
            "Changed\nSecond",
            "Before\n    Changed\n    Second\nAfter",
            False,
        ),
        (
            "Before\nFirst   finding\nAfter",
            "First finding",
            "Verified finding",
            "Before\nVerified finding\nAfter",
            False,
        ),
        ('"Ready" and “Ready”', '"Ready"', "Done", "Done and “Ready”", False),
        ("Before\nDelete this\nAfter", "Delete this\n", "", "Before\nAfter", False),
        # Blank lines both texts share around the change need not exist in the page.
        (
            "# Notes\nFirst finding\nOutro",
            "\n\nFirst finding",
            "\n\nVerified",
            "# Notes\nVerified\nOutro",
            True,
        ),
        # A kept line may differ slightly; the page keeps its own wording there.
        (
            "Intro\nThe parser handles nested lists correctly.\nStatus: pending\nEnd",
            "The parser handles nested list correctly.\nStatus: pending",
            "The parser handles nested list correctly.\nStatus: done",
            "Intro\nThe parser handles nested lists correctly.\nStatus: done\nEnd",
            False,
        ),
    ],
)
async def test_wiki_tolerant_matching_preserves_surrounding_content(
    board, content, old, new, expected, stale
):
    page_id = await create(board, content)
    if stale:
        await peer_rename(board, page_id)
    result = await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "expected_revision": 1,
            "old_text": old,
            "new_text": new,
        },
    )
    assert result["ok"], result
    current = await stored(board, page_id)
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
    page_id = await create(board, "First finding")
    await peer_rename(board, page_id)
    before = await stored(board, page_id)
    result = await invoke(
        board, {"action": "update", "page_id": page_id, "expected_revision": 1, **change}
    )
    assert result["error"]["code"] == "wiki_revision_conflict"
    assert "Nothing changed." in result["error"]["message"]
    assert await stored(board, page_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "old", "stale"),
    [
        ("same same", "same", False),
        ("“Ready” and “Ready”", '"Ready"', True),
        ("  First\n  Second\n    First\n    Second", "First\nSecond", False),
    ],
)
async def test_wiki_ambiguous_matches_never_fall_through(board, content, old, stale):
    page_id = await create(board, content)
    if stale:
        await peer_rename(board, page_id)
    before = await stored(board, page_id)
    result = await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "expected_revision": 1,
            "old_text": old,
            "new_text": "Changed",
        },
    )
    assert not result["ok"]
    assert "occurs 2 times" in result["error"]["message"]
    assert await stored(board, page_id) == before


@pytest.mark.asyncio
async def test_wiki_similar_passage_never_stands_in_for_old_text(board):
    page_id = await create(board, "Start\nThe result is pending.\nEnd")
    peer_content = "Start\nThe result is verified.\nEnd"
    await invoke(
        board,
        {"action": "update", "page_id": page_id, "expected_revision": 1, "content": peer_content},
        1,
    )
    edit = {
        "action": "update",
        "page_id": page_id,
        "expected_revision": 1,
        "old_text": "Start\nThe result is pending.\nEnd",
        "new_text": "Start\nThe result is complete.\nEnd",
    }
    assert (await invoke(board, edit))["error"]["code"] == "wiki_revision_conflict"
    assert (await stored(board, page_id))["content"] == peer_content
    # The current revision does not authorize replacing a merely similar passage either.
    current = await invoke(board, {**edit, "expected_revision": 2})
    assert current["error"]["code"] == "wiki_edit_conflict"
    assert (await stored(board, page_id))["content"] == peer_content


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
        # A misspelled kept line never excuses a changed line holding another word.
        (
            "Intro\nThe parser handles nested lists correctly.\nStatus: blocked\nEnd",
            "The parser handles nested list correctly.\nStatus: pending",
            "The parser handles nested list correctly.\nStatus: done",
        ),
    ],
)
async def test_wiki_old_text_must_not_rest_on_other_text(board, content, old, new):
    page_id = await create(board, content)
    before = await stored(board, page_id)

    result = await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "expected_revision": 1,
            "old_text": old,
            "new_text": new,
        },
    )

    assert result["error"]["code"] == "wiki_edit_conflict"
    assert await stored(board, page_id) == before


@pytest.mark.asyncio
async def test_wiki_old_text_copied_with_a_misspelling_is_applied_and_named(board):
    page_id = await create(board, "Intro\nThe parser handles nested lists correctly.\nEnd")

    result = await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "expected_revision": 1,
            "old_text": "Intro\nThe parser handles nested lists corectly.",
            "new_text": "Intro\nThe parser handles nested lists correctly, even when empty.",
        },
    )

    assert result["ok"], result
    assert (await stored(board, page_id))["content"] == (
        "Intro\nThe parser handles nested lists correctly, even when empty.\nEnd"
    )
    assert (
        "Line 2 did not match your old text exactly and was edited anyway; it read: "
        "The parser handles nested lists correctly." in visible(result)
    )


@pytest.mark.asyncio
async def test_wiki_stale_edit_cannot_revive_deleted_page_or_exceed_size_limit(board):
    page_id = await create(board, "First\n" + "x" * 199990)
    edit = {
        "action": "update",
        "page_id": page_id,
        "expected_revision": 1,
        "old_text": "First",
        "new_text": "replacement" * 10,
    }
    await peer_rename(board, page_id)
    assert (await invoke(board, edit))["error"]["code"] == "invalid_arguments"
    assert (await stored(board, page_id))["revision"] == 2
    deleted = await invoke(
        board, {"action": "delete", "page_id": page_id, "expected_revision": 2}, 1
    )
    assert deleted["ok"]
    assert (await invoke(board, {**edit, "new_text": "Done"}))["error"]["code"] == "wiki_deleted"
    current = await stored(board, page_id)
    assert current["deleted"]
    assert current["revision"] == 3


@pytest.mark.asyncio
async def test_wiki_collaboration_conflicts_history_delete_restore(board):
    content = "First finding\nSecond finding"
    page_id = await create(board, content, "Research")
    update = {
        "action": "update",
        "page_id": page_id,
        "expected_revision": 1,
        "old_text": "Second finding",
        "new_text": "Revised finding",
    }
    updated, conflict = await asyncio.gather(
        invoke(board, update, 1),
        invoke(board, {**update, "title": "Another title"}, 2),
    )
    assert sum(item["ok"] for item in (updated, conflict)) == 1
    assert (
        next(item for item in (updated, conflict) if not item["ok"])["error"]["code"]
        == "wiki_revision_conflict"
    )
    current = await stored(board, page_id)
    assert current["content"] == "First finding\nRevised finding"
    assert current["revision"] == 2
    assert (await stored(board, page_id, revision=1))["content"] == content
    deleted = await invoke(board, {"action": "delete", "page_id": page_id, "expected_revision": 2})
    assert deleted["ok"], deleted
    assert (await stored(board, page_id))["deleted"]
    only_deleted = (await invoke(board, {"action": "list"}))["data"]
    assert continuation(only_deleted["pages"]) == {"action": "list", "include_deleted": True}
    listed = visible(await invoke(board, {"action": "list", "include_deleted": True}))
    # Either racing change may have won, so the title varies.
    assert f"({page_id}), revision 3 by " in listed
    assert ", deleted\n" in listed
    restored = await invoke(
        board, {"action": "restore", "page_id": page_id, "expected_revision": 3, "revision": 1}
    )
    assert restored["data"]["status"] == "Restored revision 1."
    current = await stored(board, page_id)
    assert (current["revision"], current["deleted"], current["content"]) == (4, False, content)
    history = await invoke(board, {"action": "history", "page_id": page_id, "limit": 1})
    assert history["data"]["content"].startswith("- revision 4, ")
    older = await invoke(board, continuation(history["data"]["more"]))
    assert older["data"]["content"].startswith("- revision 3, ")
    assert older["data"]["content"].endswith(", deleted the page")
    assert all(
        peer["pending_count"] == 0
        for peer in (await board.store.get_swarm(board.swarm["id"]))["participants"]
    )
    assert not (await board.store.read_human_posts(board.swarm["id"])).entries


@pytest.mark.asyncio
async def test_wiki_bounded_reads_search_and_frozen_pagination(board):
    content = "a" * 20500 + "\nÜberraschung"
    page_ids = [await create(board, content, f"Page {index}") for index in range(3)]
    # A read without limit shows as much as one read can.
    result = (await invoke(board, {"action": "read", "page_id": page_ids[0]}))["data"]
    assert len(result["content"]) == 20000
    assert result["shown"] == "Characters 0 to 20000 of 20513."
    await invoke(
        board,
        {"action": "update", "page_id": page_ids[0], "expected_revision": 1, "content": "changed"},
    )
    # The continuation keeps reading the revision it started with.
    tail = (await invoke(board, continuation(result["more"])))["data"]
    assert result["content"] + tail["content"] == content
    assert tail["revision"].startswith("1 (the current revision is 2), saved by ")
    search = await invoke(board, {"action": "list", "query": "ÜBERRASCHUNG", "limit": 1})
    assert search["data"]["pages"] == (
        'Pages containing "ÜBERRASCHUNG", most recently changed first (1 shown).'
    )
    assert "Überraschung" in search["data"]["content"]
    await invoke(
        board,
        {"action": "update", "page_id": page_ids[1], "expected_revision": 1, "content": "changed"},
    )
    arguments = continuation(search["data"]["more"])
    following = await invoke(board, arguments)
    assert following["data"]["content"].startswith(f'- "Page 1" ({page_ids[1]}), revision 1 by ')
    changed = await invoke(board, {**arguments, "query": "other"})
    assert changed["error"]["code"] == "invalid_cursor"
    # A query no page contains as written finds the pages holding all of its
    # words, in title or content; a phrase that occurs keeps its exact search.
    found = await create(board, "The board contract names each interface.", "Page 3")
    words = (await invoke(board, {"action": "list", "query": "interface, page"}))["data"]
    assert words["pages"] == WIKI_LIST_WORD_MATCHES.format(query="interface, page", count=1)
    assert words["content"].startswith(f'- "Page 3" ({found}), revision 1 by ')
    phrase = (await invoke(board, {"action": "list", "query": "contract names"}))["data"]
    assert phrase["pages"] == WIKI_LIST_MATCHES.format(query="contract names", count=1)
    for query, header in [
        ("interface missing", WIKI_LIST_NO_WORD_MATCH),
        ("interfaces", WIKI_LIST_NO_MATCH),
    ]:
        missed = (await invoke(board, {"action": "list", "query": query}))["data"]
        assert missed["pages"] == header.format(query=query)


@pytest.mark.asyncio
async def test_wiki_scope_management_recovery_and_targeted_edits(board):
    page_id = await create(board, "same same")
    edit = {
        "action": "update",
        "page_id": page_id,
        "expected_revision": 1,
        "old_text": "same",
        "new_text": "",
    }
    assert (await invoke(board, edit))["error"]["code"] == "wiki_edit_conflict"
    for passage in (edit, {key: value for key, value in edit.items() if key != "old_text"}):
        assert (await invoke(board, {**passage, "content": "lost"}))["error"][
            "code"
        ] == "invalid_arguments"
    assert (await stored(board, page_id))["content"] == "same same"
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
    whitespace_edit = await invoke(board, {**edit, "expected_revision": 2, "old_text": " "})
    assert whitespace_edit["ok"], whitespace_edit
    assert (await stored(board, page_id))["content"] == "samesame"
    await board.store.close()
    await board.store.open()
    assert (await stored(board, page_id))["title"] == "Renamed"
    await board.service.operation(
        "swarms.stop", {"swarm_id": board.swarm["id"], "request_id": "stop"}
    )
    await board.service.operation("swarms.delete", {"swarm_id": board.swarm["id"]})
    with pytest.raises(SwarmStoreError, match="swarm_not_found"):
        await board.store.wiki(board.swarm["id"], None, {"action": "list"})


@pytest.mark.asyncio
async def test_wiki_results_read_as_text(board):
    created = await invoke(board, {"action": "create", "content": "# Plan\n\nStep one"})
    page_id = created["data"]["page_id"]
    author = board.swarm["participants"][0]["display_name"]
    assert visible(created) == (
        f"page_id: {page_id}\ntitle: Plan\nrevision: 1\nlink: [Plan](#wiki/{page_id})\n"
        "status: Created.\n"
        "shown: Saved page content at revision 1, from line 1: characters 0 to 16 of 16.\n"
        'note: create needs a title; used the first heading, "Plan".\n\n# Plan\n\nStep one'
    )
    assert visible(await invoke(board, {"action": "read", "page_id": page_id})) == (
        f"page_id: {page_id}\ntitle: Plan\nrevision: 1 (current), saved by {author}\n"
        f"link: [Plan](#wiki/{page_id})\n\n# Plan\n\nStep one"
    )
    partial = await invoke(board, {"action": "read", "page_id": page_id, "limit": 6})
    assert visible(partial) == (
        f"page_id: {page_id}\ntitle: Plan\nrevision: 1 (current), saved by {author}\n"
        f"link: [Plan](#wiki/{page_id})\nshown: Characters 0 to 6 of 16.\n"
        f'more: Continue with swarm_wiki {{"action": "read", "page_id": "{page_id}", '
        '"limit": 6, '
        f'"revision": 1, "offset": 6}}\n\n# Plan'
    )
    assert visible(await invoke(board, {"action": "list"})) == (
        "pages: Pages, most recently changed first (1 shown).\n\n"
        f'- "Plan" ({page_id}), revision 1 by {author}\n  # Plan Step one'
    )
    edited = await invoke(
        board, {"action": "update", "page_id": page_id, "old_text": "Step one", "new_text": "Done"}
    )
    assert edited["data"]["status"] == "Replaced the passage at line 3."
    history = visible(await invoke(board, {"action": "history", "page_id": page_id}))
    assert re.fullmatch(
        rf'history: Revisions of "Plan" \({page_id}\), newest first \(2 shown\)\.\n\n'
        rf"- revision 2, \d{{4}}-\d\d-\d\d \d\d:\d\d UTC, by {author}\n"
        rf"- revision 1, \d{{4}}-\d\d-\d\d \d\d:\d\d UTC, by {author}",
        history,
    ), history
    deleted = await invoke(board, {"action": "delete", "page_id": page_id})
    assert visible(deleted) == (
        f"page_id: {page_id}\ntitle: Plan\nrevision: 3\n"
        "status: Deleted. Restore it with swarm_wiki "
        f'{{"action": "restore", "page_id": "{page_id}", '
        '"revision": 3}'
    )
    # The restore call in the result is copyable as it stands.
    restored = await invoke(board, continuation(deleted["data"]["status"]))
    assert restored["data"]["status"] == "Restored revision 3."
    current = await stored(board, page_id)
    assert (current["revision"], current["deleted"], current["content"]) == (
        4,
        False,
        "# Plan\n\nDone",
    )


@pytest.mark.asyncio
async def test_wiki_request_ids_come_from_the_tool_call(board):
    first = await invoke(
        board, {"action": "create", "title": "A", "content": "one", "request_id": "same"}
    )
    second = await invoke(
        board, {"action": "create", "title": "B", "content": "two", "request_id": "same"}
    )
    # An Agent's request_id is not used, so reusing one never conflicts.
    assert first["ok"] and second["ok"], (first, second)
    assert {page["title"] for page in await pages(board)} == {"A", "B"}
    edit = {
        "action": "update",
        "page_id": first["data"]["page_id"],
        "old_text": "one",
        "new_text": "uno",
    }
    applied = await invoke(board, edit, tool_call_id="edit")
    replayed = await invoke(board, edit, tool_call_id="edit")
    assert replayed["data"] == {**applied["data"], "note": REPLAYED}
    assert (await stored(board, first["data"]["page_id"]))["revision"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "arguments", "status"),
    [
        (
            "Intro\nStatus: final\nNext: review\nEnd",
            {"old_text": "Status: done\nNext: review", "new_text": "Status: final\nNext: review"},
            "Nothing changed: the page already holds this change.",
        ),
        (
            "Intro\nTimeout is 30 seconds for retries\nEnd",
            {
                "old_text": "Timeout is 20 seconds for retries",
                "new_text": "Timeout is 30 seconds for retries",
            },
            "Nothing changed: the page already holds this change.",
        ),
        (
            "First finding",
            {"title": "Notes", "expected_revision": 1},
            "Nothing changed: the page already holds this change.",
        ),
        (
            "First finding",
            {"action": "restore", "revision": 1},
            "Nothing changed: the page already holds this change.",
        ),
    ],
)
async def test_wiki_changes_the_page_already_holds_change_nothing(
    board, content, arguments, status
):
    page_id = await create(board, content)
    before = await stored(board, page_id)
    result = await invoke(board, {"action": "update", "page_id": page_id, **arguments})
    assert result["data"]["status"] == status
    assert await stored(board, page_id) == before


@pytest.mark.asyncio
async def test_wiki_repeated_create_and_delete_change_nothing(board):
    create_call = {"action": "create", "title": "Notes", "content": "First finding"}
    created = await invoke(board, create_call)
    repeated = await invoke(board, create_call)
    assert repeated["data"]["page_id"] == created["data"]["page_id"]
    assert repeated["data"]["status"] == "Nothing changed: an identical page already exists."
    assert len(await pages(board)) == 1
    page_id = created["data"]["page_id"]
    await invoke(board, {"action": "delete", "page_id": page_id})
    again = await invoke(board, {"action": "delete", "page_id": page_id})
    assert again["data"]["status"] == "Nothing changed: the page is already deleted."
    assert (await stored(board, page_id))["revision"] == 2


@pytest.mark.asyncio
async def test_wiki_expected_revision_is_needed_only_to_replace_a_peers_revision(board):
    page_id = await create(board, "First finding")
    for change in (
        {"old_text": "First", "new_text": "Main"},
        {"title": "Renamed"},
    ):
        result = await invoke(board, {"action": "update", "page_id": page_id, **change})
        assert result["ok"], result
    current = await stored(board, page_id)
    assert (current["revision"], current["title"], current["content"]) == (
        3,
        "Renamed",
        "Main finding",
    )
    # Revision 3 is the caller's own, so replacing it loses no peer's change.
    rewrite = {"action": "update", "page_id": page_id, "content": "Own rewrite"}
    own = await invoke(board, rewrite, tool_call_id="rewrite")
    assert own["data"]["note"] == WIKI_OWN_REVISION.format(revision=3)
    assert (await stored(board, page_id))["content"] == "Own rewrite"
    # The same Tool Call replays its result although the caller now saved the current revision.
    replay = await invoke(board, rewrite, tool_call_id="rewrite")
    assert replay["data"] == {**own["data"], "note": REPLAYED}
    peer_edit = {"action": "update", "page_id": page_id, "old_text": "Own", "new_text": "Peer"}
    assert (await invoke(board, peer_edit, 1))["ok"]
    current = await stored(board, page_id)
    replace_all = await invoke(board, {"action": "update", "page_id": page_id, "content": "new"})
    assert visible(replace_all) == (
        "Error (invalid_arguments): Replacing the whole content needs expected_revision: the "
        "revision your content is based on. The current revision is 5; read it first if you "
        "have not, so no newer change is lost. Nothing changed."
    )
    assert await stored(board, page_id) == current
    assert (await invoke(board, {"action": "delete", "page_id": page_id}))["ok"]
    assert (await invoke(board, {"action": "restore", "page_id": page_id, "revision": 5}))["ok"]
    assert (await stored(board, page_id))["revision"] == 7


@pytest.mark.asyncio
async def test_wiki_edit_misses_show_the_closest_passage(board):
    page_id = await create(
        board,
        "# Plan\nThe deployment uses three replicas behind the load balancer today.\n"
        "Row 1: value\nRow 2: value\nsame same\nEnd",
    )

    async def edit(old, page=page_id, **extra):
        return await invoke(
            board,
            {"action": "update", "page_id": page, "old_text": old, "new_text": "x", **extra},
        )

    async def miss(old, page=page_id, **extra):
        """Return the error's lines and what the read call it suggests shows."""
        message = (await edit(old, page, **extra))["error"]["message"]
        lines = message.splitlines()
        assert lines[0].endswith("Nothing changed."), message
        # The suggested call names the Tool, so it is not sent to the file read Tool.
        assert " swarm_wiki {" in lines[-1]
        shown = await invoke(board, continuation(lines[-1]))
        return lines, shown["data"]["content"]

    # Each line where old_text differs is named, with both versions.
    lines, passage = await miss("Row 2: valeu")
    assert passage == "Row 2: value"
    difference = WIKI_DIFFERENCE.format(line=4, page="'Row 2: value'", copy="'Row 2: valeu'")
    assert difference in lines
    # Closest passages that overlap show each page line once.
    reviews = await create(
        board,
        "# Reviews\nThe team reviews the rollout plan.\nThe team reviews the rollback plan.\n"
        "The team reviews the release notes.",
        "Reviews",
    )
    old = "The team reviewed the rollout plan.\nThe team reviewed the rollback plan."
    lines, passage = await miss(old, reviews)
    assert passage == "The team reviews the rollout plan.\nThe team reviews the rollback plan."
    assert lines.count("The team reviews the rollout plan.") == 1
    assert _differing_lines(lines) == ["2", "3"]
    # A fragment of a longer line points at that line.
    lines, passage = await miss("three replicas behind the proxy")
    assert passage == "The deployment uses three replicas behind the load balancer today."
    # old_text may start inside its first line and end inside its last; the line named
    # is the one that really differs, here a continuation line without its indentation.
    listed = await create(
        board,
        "# Fields\n- item (`date` free text,\n  runtime-parsed); `era` canonical band\n"
        "  stored exactly as rendered\n- other item",
        "Fields",
    )
    for old in (
        "`era` canonical band\nstored exactly as rendered",
        "runtime-parsed); `era` canonical band\nstored exactly",
    ):
        lines, passage = await miss(old, listed)
        assert _differing_lines(lines) == ["4"], lines
        assert (
            WIKI_DIFFERENCE_SPACING.format(
                line=4,
                page="'  stored exactly as rendered'",
                copy=repr(old.splitlines()[1]),
                side="start",
            )
            in lines
        )
        assert "  stored exactly as rendered" in passage
    # A first line that differs in its text is still named.
    lines, _ = await miss("`era` canonical bend\n  stored exactly as rendered", listed)
    assert _differing_lines(lines) == ["3"]
    assert visible(await edit("same")) == (
        "Error (wiki_edit_conflict): old_text occurs 2 times, all in line 5. Include "
        "neighboring text so it occurs only once. Nothing changed.\nline 5:\nsame same\nEnd"
    )
    assert visible(await edit("value")) == (
        "Error (wiki_edit_conflict): old_text occurs 2 times, at lines 3 and 4. Include "
        "neighboring text so it occurs only once. Nothing changed.\nline 3:\nRow 1: value\n"
        "Row 2: value\nline 4:\nRow 2: value\nsame same"
    )
    # A retyped passage names every differing line at once, up to a bound, and tells a
    # line the copy dropped or added from a changed one.
    items = [f"Item {number}: the original text of item {number}" for number in range(1, 13)]
    retyped = await create(board, "\n".join(items), "Retyped")
    copy = [
        item.replace("original", "first") if number in {3, 5, 7, 8, 9, 10} else item
        for number, item in enumerate(items, 1)
    ][1:10]
    copy.remove(items[3])
    copy.insert(3, "An inserted line")
    lines, passage = await miss("\n".join(copy), retyped)
    assert passage == "\n".join(items[1:10])
    assert _differing_lines(lines) == ["3", "4", "5", "old_text 4", "7"]
    dropped = next(line for line in lines if line.startswith("line 4:"))
    assert items[3] in dropped and "Item 5" not in dropped
    assert WIKI_DIFFERENCES_MORE.format(count=3) in lines
    # A passage edit from an older revision shows what changed since and who changed it.
    await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "old_text": "Row 1: value",
            "new_text": "Row 1: ok",
        },
        1,
    )
    lines, passage = await miss("Row 1: value", expected_revision=1)
    assert lines[0].startswith("The page changed since revision 1 (now 2)")
    assert board.swarm["participants"][1]["display_name"] in lines[1]
    assert lines[2:6] == [
        "@@ -2,3 +2,3 @@",
        " The deployment uses three replicas behind the load balancer today.",
        "-Row 1: value",
        "+Row 1: ok",
    ]
    current = await stored(board, page_id)
    assert passage in current["content"]
    assert current["revision"] == 2


def _differing_lines(lines):
    """Return the page lines, or "old_text N" lines, an edit miss names as differing."""

    named = []
    for line in lines:
        if match := re.match(r"line (\d+)(?:,[^:]*)?: the page has", line):
            named.append(match[1])
        elif match := re.match(r"old_text line (\d+) has", line):
            named.append(f"old_text {match[1]}")
    return named


@pytest.mark.asyncio
async def test_wiki_conflicts_show_what_changed_and_the_retry(board):
    page_id = await create(board, "Intro\nStatus: pending\nEnd")
    await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": "pending", "new_text": "done"},
        1,
    )
    peer = board.swarm["participants"][1]["display_name"]
    stale = {"action": "update", "page_id": page_id, "expected_revision": 1}
    for change, next_step in (
        ({"content": "Replaced"}, WIKI_CONFLICT_CONTENT),
        ({"title": "Renamed"}, WIKI_CONFLICT_RETRY),
    ):
        message = (await invoke(board, {**stale, **change}))["error"]["message"]
        first, header, *diff, last = message.splitlines()
        assert first.endswith("Nothing changed.")
        assert peer in header
        assert diff == ["@@ -1,3 +1,3 @@", " Intro", "-Status: pending", "+Status: done", " End"]
        assert last == next_step.format(current=2)
    future = await invoke(board, {**stale, "expected_revision": 7, "title": "Renamed"})
    assert visible(future) == (
        "Error (wiki_revision_conflict): Revision 7 does not exist yet; the current revision "
        "is 2. Repeat the call with expected_revision 2 if it still applies. Nothing changed."
    )
    current = await stored(board, page_id)
    assert (current["revision"], current["title"]) == (2, "Notes")
    assert (await invoke(board, {**stale, "expected_revision": 2, "title": "Renamed"}))["ok"]


@pytest.mark.asyncio
async def test_wiki_read_since_a_revision_shows_what_changed_and_who_changed_it(board):
    page_id = await create(board, "Intro\nStatus: pending\nEnd")
    first, second = (board.swarm["participants"][peer]["display_name"] for peer in (1, 2))
    edit = {"action": "update", "page_id": page_id, "old_text": "pending", "new_text": "done"}
    assert (await invoke(board, edit, 1))["ok"]
    rename = {"action": "update", "page_id": page_id, "title": "Status"}
    assert (await invoke(board, rename, 2))["ok"]
    read = {"action": "read", "page_id": page_id}
    changes = (await invoke(board, {**read, "since_revision": 1}))["data"]
    assert first in changes["changes"] and second in changes["changes"]
    assert changes["content"] == "@@ -1,3 +1,3 @@\n Intro\n-Status: pending\n+Status: done\n End"
    assert changes["revision"].startswith("3 (current)")
    # A rename changes no content; the result names who made it, without a diff.
    renamed = (await invoke(board, {**read, "since_revision": 2, "offset": 5}))["data"]
    assert second in renamed["changes"] and first not in renamed["changes"]
    assert "content" not in renamed
    assert renamed["note"] == WIKI_SINCE_IGNORES.format(field="offset")
    current = (await invoke(board, {**read, "since_revision": 3}))["data"]
    assert (current["changes"], "content" in current) == (WIKI_CHANGES_NONE.format(since=3), False)
    # With revision, the changes end at that revision.
    older = (await invoke(board, {**read, "since_revision": 1, "revision": 2}))["data"]
    assert second not in older["changes"]
    assert older["content"] == changes["content"]
    for revisions in ({"since_revision": 4}, {"since_revision": 3, "revision": 2}):
        refused = await invoke(board, {**read, **revisions})
        assert refused["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_wiki_page_references_resolve_only_for_reading(board):
    # Pages are numbered in creation order; a number names its page exactly.
    assert (await create(board, "Body", "Release plan"), await create(board, "x", "B")) == (
        "w1",
        "w2",
    )
    stored_id = next(page["page_id"] for page in await pages(board) if page["number"] == 1)
    for reference in ("w1", "1", "W1", stored_id, "[Release plan](#wiki/w1)", f"#wiki/{stored_id}"):
        exact = await invoke(board, {"action": "read", "page_id": reference})
        assert (exact["data"]["page_id"], exact["data"]["content"]) == ("w1", "Body")
        assert "note" not in exact["data"]
    assert exact["data"]["link"] == "[Release plan](#wiki/w1)"
    by_title = await invoke(board, {"action": "read", "page_id": "release plan"})
    assert by_title["data"]["content"] == "Body"
    assert by_title["data"]["note"] == "page_id release plan is a page title; used w1."
    typo = stored_id[:-1] + ("x" if stored_id[-1] != "x" else "y")
    by_close_id = await invoke(board, {"action": "read", "page_id": typo})
    assert by_close_id["data"]["note"] == (
        f'page_id {typo} matched no page; used w1 ("Release plan").'
    )
    # A change never lands on a guessed page.
    guessed = await invoke(board, {"action": "delete", "page_id": "Release plan"})
    assert visible(guessed) == (
        "Error (wiki_page_not_found): No page Release plan exists in your group's Wiki. The "
        'closest page is "Release plan" (w1). Repeat the call with page_id "w1" if you meant '
        "it. Nothing changed."
    )
    # Without page_id, an update runs on the one live page that holds its exact old_text.
    changed = {"action": "update", "page_id": "w2", "old_text": "x", "new_text": "no"}
    assert (await invoke(board, changed))["ok"]
    unnamed = await invoke(board, {"action": "update", "old_text": "Body", "new_text": "Body text"})
    assert (unnamed["data"]["page_id"], unnamed["data"]["revision"]) == ("w1", 2)
    assert unnamed["data"]["note"] == (
        'page_id was omitted; old_text occurs only in w1 ("Release plan"), so that page was '
        "updated."
    )
    assert (await stored(board, "w1"))["content"] == "Body text"
    # Several pages or none: the update names them, newest change first, and changes nothing.
    for old_text, named in (
        (
            "o",
            'old_text occurs in w1 ("Release plan") and w2 ("B"); repeat the call with the '
            "page_id of the page you mean.",
        ),
        ("Missing", 'Find pages with swarm_wiki {"action": "list"}.'),
    ):
        unnamed = await invoke(board, {"action": "update", "old_text": old_text, "new_text": "y"})
        assert visible(unnamed) == (
            f"Error (invalid_arguments): update needs page_id. {named} Nothing changed."
        )
    assert [(await stored(board, page))["content"] for page in ("w1", "w2")] == ["Body text", "no"]
    for unknown in ("w9", "wpg_unrelated"):
        missing = await invoke(board, {"action": "read", "page_id": unknown})
        assert visible(missing) == (
            f"Error (wiki_page_not_found): No page {unknown} exists in your group's Wiki. Find "
            'pages with swarm_wiki {"action": "list"}.'
        )
    assert not (await stored(board, "w1"))["deleted"]
    # A deleted page keeps its number, and a new page gets the next one.
    assert (await invoke(board, {"action": "delete", "page_id": "w2"}))["ok"]
    # Only live pages count: with w2 deleted, "o" occurs only in w1.
    only_live = await invoke(board, {"action": "update", "old_text": "o", "new_text": "O"})
    assert (only_live["data"]["page_id"], (await stored(board, "w1"))["content"]) == (
        "w1",
        "BOdy text",
    )
    assert await create(board, "y", "C") == "w3"
    assert (await stored(board, "w2"))["deleted"]


@pytest.mark.asyncio
async def test_wiki_create_decides_about_a_page_id_and_a_missing_title(board):
    page_id = await create(board, "Body")
    existing = await invoke(
        board, {"action": "create", "page_id": page_id, "title": "Other", "content": "x"}
    )
    assert visible(existing) == (
        f"Error (invalid_arguments): create makes a new page, but page_id names the existing page "
        f'"Notes" ({page_id}). To change that page, use update; to add a separate page, omit '
        "page_id. Nothing changed."
    )
    fresh = await invoke(
        board, {"action": "create", "page_id": "wpg_mine", "title": "Other", "content": "x"}
    )
    assert fresh["data"]["note"] == "create makes a new page, so page_id wpg_mine was ignored."
    untitled = await invoke(board, {"action": "create", "content": "no heading"})
    assert visible(untitled) == (
        "Error (invalid_arguments): create needs a title. Nothing changed."
    )
    wrong_field = await invoke(
        board, {"action": "create", "title": "T", "content": "x", "old_text": "a"}
    )
    assert visible(wrong_field) == (
        "Error (invalid_arguments): old_text is used by update, not by create. Repeat the call "
        "without it."
    )
    assert sorted(page["title"] for page in await pages(board)) == ["Notes", "Other"]


@pytest.mark.asyncio
async def test_wiki_reading_calls_run_with_a_note(board):
    page_id = await create(board, "Findings about parsers")
    listed = await invoke(board, {"action": "read"})
    assert listed["data"]["pages"] == "Pages, most recently changed first (1 shown)."
    assert listed["data"]["note"] == "read needs page_id, so this lists the pages instead."
    searched = await invoke(board, {"action": "search_files", "query": "PARSERS"})
    assert searched["data"]["pages"].startswith('Pages containing "PARSERS"')
    clamped = await invoke(board, {"action": "read", "page_id": page_id, "limit": 50000})
    assert clamped["data"]["note"] == "limit 50000 is above the maximum of 20000; used 20000."
    ignored = await invoke(board, {"action": "read", "page_id": page_id, "expected_revision": 1})
    assert ignored["data"]["note"] == "expected_revision is not used by read and was ignored."
    past = await invoke(board, {"action": "read", "page_id": page_id, "offset": 500})
    assert past["data"]["shown"] == (
        "Nothing to show after character 500; the page has 22 characters."
    )
    assert "more" not in past["data"] and "content" not in past["data"]
    # A query found nowhere on the page shows no content, and the search over all pages.
    missing = await invoke(board, {"action": "read", "page_id": page_id, "query": "x"})
    assert continuation(missing["data"]["found"]) == {"action": "list", "query": "x"}
    assert "content" not in missing["data"]
    # A negative offset counts back from the end of the page.
    tail = await invoke(board, {"action": "read", "page_id": page_id, "offset": -7})
    assert tail["data"]["content"] == "parsers"
    assert tail["data"]["note"] == WIKI_OFFSET_FROM_END.format(value=-7)
    # path names the page as page_id does, and a page_id makes the call a read.
    for call in ({"path": page_id}, {"page_id": page_id, "query": "parsers"}):
        assert (await invoke(board, call))["data"]["title"] == "Notes"


@pytest.mark.asyncio
async def test_wiki_read_with_query_names_the_lines_that_contain_it(board):
    lines = ["# Modules", *(f"- module {number}: notes about it" for number in range(1, 200))]
    lines[120] = "- errors.py holds MiniError"
    lines[150] = "- see ERRORS.PY for the codes"
    content = "\n".join(lines)
    page_id = await create(board, content)
    offset = len("\n".join(lines[:150])) + 1

    # The query is found in the whole revision, beyond the window the read shows.
    result = await invoke(
        board, {"action": "read", "page_id": page_id, "query": "errors.py", "limit": 100}
    )

    assert result["ok"], result
    assert result["data"]["found"] == (
        'Lines containing "errors.py", ignoring case:\n'
        f"line 121, character {content.index(lines[120])}: - errors.py holds MiniError\n"
        f"line 151, character {offset}: - see ERRORS.PY for the codes"
    )
    assert list(result["data"]).index("found") < list(result["data"]).index("content")
    assert result["data"]["content"] == content[:100]
    # Without offset and limit, the read shows the text around the first line found,
    # from two lines before it, instead of the whole page.
    around = await invoke(board, {"action": "read", "page_id": page_id, "query": "errors.py"})
    start = content.index(lines[118])
    assert around["data"]["content"] == content[start : start + 2000]
    assert around["data"]["found"] == result["data"]["found"]
    assert continuation(around["data"]["more"]) == {
        "action": "read",
        "page_id": page_id,
        "revision": 1,
        "offset": start + 2000,
    }
    listed = await invoke(board, {"action": "list", "query": "errors.py"})
    assert listed["data"]["pages"].startswith('Pages containing "errors.py"')


@pytest.mark.asyncio
async def test_twelve_peers_can_edit_independent_passages_from_the_same_revision(store):
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
