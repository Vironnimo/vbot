import asyncio
import re
from dataclasses import replace

import pytest

from core.tools import ToolContractError, tool_failure
from core.utils.ids import new_id
from resources.extensions.swarm.agent_text import REPLAYED
from resources.extensions.swarm.store import SwarmStoreError
from tests.resources.extensions.test_swarm_board import board as board
from tests.resources.extensions.test_swarm_board import continuation, visible


async def invoke(fixture, arguments, peer=0, *, tool_call_id=None):
    """Run one swarm_wiki Tool Call through production dispatch."""

    context = replace(
        fixture.contexts[peer],
        tool_name="swarm_wiki",
        tool_call_id=tool_call_id or new_id("call"),
        session_tool_grants=("swarm_wiki",),
    )
    try:
        return await fixture.tools.dispatch(context, arguments, allowed_tools=["swarm_wiki"])
    except ToolContractError as error:
        return tool_failure("invalid_arguments", str(error))


async def stored(fixture, page_id, **query):
    """Return a page as the Store holds it."""

    return await fixture.store.wiki(
        fixture.swarm["id"], None, {"action": "read", "page_id": page_id, "limit": 20000, **query}
    )


async def pages(fixture):
    return await fixture.store.wiki_pages(fixture.swarm["id"])


async def create(fixture, content, title="Notes"):
    result = await invoke(fixture, {"action": "create", "title": title, "content": content})
    assert result["ok"], result
    return result["data"]["page_id"]


async def peer_rename(fixture, page_id, title="Peer title"):
    """Let another participant make revision 1 stale."""

    renamed = await invoke(
        fixture,
        {"action": "update", "page_id": page_id, "expected_revision": 1, "title": title},
        1,
    )
    assert renamed["ok"], renamed


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
        # Blank lines both texts share around the change need not exist in the page.
        (
            "# Notes\nFirst finding\nOutro",
            "\n\nFirst finding",
            "\n\nVerified",
            "# Notes\nVerified\nOutro",
        ),
        # A kept line may differ slightly; the page keeps its own wording there.
        (
            "Intro\nThe parser handles nested lists correctly.\nStatus: pending\nEnd",
            "The parser handles nested list correctly.\nStatus: pending",
            "The parser handles nested list correctly.\nStatus: done",
            "Intro\nThe parser handles nested lists correctly.\nStatus: done\nEnd",
        ),
    ],
)
async def test_wiki_tolerant_matching_preserves_surrounding_content(
    board, stale, content, old, new, expected
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
        # A similar kept line never excuses a changed line that differs.
        (
            "Intro\nThe parser handles nested lists correctly.\nStatus: blocked\nEnd",
            "The parser handles nested list correctly.\nStatus: pending",
            "The parser handles nested list correctly.\nStatus: done",
        ),
    ],
)
async def test_wiki_old_text_must_match_every_changed_line_precisely(board, content, old, new):
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
    assert visible(await invoke(board, {"action": "list"})) == (
        'pages: The Wiki has only deleted pages. List them with {"action": "list", '
        '"include_deleted": true}'
    )
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
    content = "a" * 12500 + "\nÜberraschung"
    page_ids = [await create(board, content, f"Page {index}") for index in range(3)]
    result = (await invoke(board, {"action": "read", "page_id": page_ids[0]}))["data"]
    assert len(result["content"]) == 12000
    assert result["shown"] == "Characters 0 to 12000 of 12513."
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
        'status: Created.\nnote: create needs a title; used the first heading, "Plan".'
    )
    assert visible(await invoke(board, {"action": "read", "page_id": page_id})) == (
        f"page_id: {page_id}\ntitle: Plan\nrevision: 1 (current), saved by {author}\n"
        f"link: [Plan](#wiki/{page_id})\n\n# Plan\n\nStep one"
    )
    partial = await invoke(board, {"action": "read", "page_id": page_id, "limit": 6})
    assert visible(partial) == (
        f"page_id: {page_id}\ntitle: Plan\nrevision: 1 (current), saved by {author}\n"
        f"link: [Plan](#wiki/{page_id})\nshown: Characters 0 to 6 of 16.\n"
        f'more: Continue with {{"action": "read", "page_id": "{page_id}", "limit": 6, '
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
        f'status: Deleted. Restore it with {{"action": "restore", "page_id": "{page_id}", '
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
async def test_wiki_expected_revision_is_needed_only_to_replace_the_whole_page(board):
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
    replace_all = await invoke(board, {"action": "update", "page_id": page_id, "content": "new"})
    assert visible(replace_all) == (
        "Error (invalid_arguments): Replacing the whole content needs expected_revision: the "
        "revision your content is based on. The current revision is 3; read it first if you "
        "have not, so no newer change is lost. Nothing changed."
    )
    assert await stored(board, page_id) == current
    assert (await invoke(board, {"action": "delete", "page_id": page_id}))["ok"]
    assert (await invoke(board, {"action": "restore", "page_id": page_id, "revision": 3}))["ok"]
    assert (await stored(board, page_id))["revision"] == 5


@pytest.mark.asyncio
async def test_wiki_edit_misses_show_the_closest_passage(board):
    page_id = await create(
        board,
        "# Plan\nThe deployment uses three replicas behind the load balancer today.\n"
        "Row 1: value\nRow 2: value\nsame same\nEnd",
    )
    read_call = f'{{"action": "read", "page_id": "{page_id}"}}'

    async def edit(old, **extra):
        return visible(
            await invoke(
                board,
                {"action": "update", "page_id": page_id, "old_text": old, "new_text": "x", **extra},
            )
        )

    assert await edit("Row 2: valeu") == (
        "Error (wiki_edit_conflict): old_text does not occur in the current page (revision 1). "
        "Nothing changed.\nClosest passages:\nline 4:\nRow 2: value\nline 3:\nRow 1: value\n"
        f"Copy old_text exactly from the page, or read it with {read_call}."
    )
    # A fragment of a longer line points at that line.
    assert await edit("three replicas behind the load balancers") == (
        "Error (wiki_edit_conflict): old_text does not occur in the current page (revision 1). "
        "Nothing changed.\nClosest passage, at line 2:\n"
        "The deployment uses three replicas behind the load balancer today.\n"
        f"Copy old_text exactly from the page, or read it with {read_call}."
    )
    assert await edit("same") == (
        "Error (wiki_edit_conflict): old_text occurs 2 times, all in line 5. Include "
        "neighboring text so it occurs only once. Nothing changed.\nline 5:\nsame same\nEnd"
    )
    assert await edit("value") == (
        "Error (wiki_edit_conflict): old_text occurs 2 times, at lines 3 and 4. Include "
        "neighboring text so it occurs only once. Nothing changed.\nline 3:\nRow 1: value\n"
        "Row 2: value\nline 4:\nRow 2: value\nsame same"
    )
    await peer_rename(board, page_id)
    assert (await edit("Row 9: value", expected_revision=1)).startswith(
        "Error (wiki_revision_conflict): The page changed since revision 1 (now 2), and "
        "old_text no longer occurs in it. Nothing changed.\n"
    )
    assert (await stored(board, page_id))["revision"] == 2


@pytest.mark.asyncio
async def test_wiki_conflicts_show_what_changed_and_the_retry(board):
    page_id = await create(board, "Intro\nStatus: pending\nEnd")
    await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": "pending", "new_text": "done"},
        1,
    )
    stale = {"action": "update", "page_id": page_id, "expected_revision": 1}
    assert visible(await invoke(board, {**stale, "content": "Replaced"})) == (
        "Error (wiki_revision_conflict): The page changed since revision 1; the current "
        "revision is 2. Nothing changed.\nChanges since revision 1:\n@@ -1,3 +1,3 @@\n Intro\n"
        "-Status: pending\n+Status: done\n End\nApply your change to the current page: replace "
        "one passage with old_text and new_text, or send the whole content with "
        "expected_revision 2."
    )
    retry = "Repeat the call with expected_revision 2 if it still applies. Nothing changed."
    assert visible(await invoke(board, {**stale, "title": "Renamed"})) == (
        "Error (wiki_revision_conflict): The page changed since revision 1; the current "
        f"revision is 2. {retry}"
    )
    assert visible(await invoke(board, {**stale, "expected_revision": 7, "title": "Renamed"})) == (
        "Error (wiki_revision_conflict): Revision 7 does not exist yet; the current revision "
        f"is 2. {retry}"
    )
    current = await stored(board, page_id)
    assert (current["revision"], current["title"]) == (2, "Notes")
    assert (await invoke(board, {**stale, "expected_revision": 2, "title": "Renamed"}))["ok"]


@pytest.mark.asyncio
async def test_wiki_page_references_resolve_only_for_reading(board):
    page_id = await create(board, "Body", "Release plan")
    typo = page_id[:-1] + ("x" if page_id[-1] != "x" else "y")
    by_title = await invoke(board, {"action": "read", "page_id": "release plan"})
    assert by_title["data"]["content"] == "Body"
    assert by_title["data"]["note"] == f"page_id release plan is a page title; used {page_id}."
    by_close_id = await invoke(board, {"action": "read", "page_id": f"[link](#wiki/{typo})"})
    assert by_close_id["data"]["note"] == (
        f'page_id {typo} matched no page; used {page_id} ("Release plan").'
    )
    pasted = await invoke(board, {"action": "read", "page_id": f"[Release plan](#wiki/{page_id})"})
    assert pasted["data"]["content"] == "Body" and "note" not in pasted["data"]
    # A change never lands on a guessed page.
    guessed = await invoke(board, {"action": "delete", "page_id": "Release plan"})
    assert visible(guessed) == (
        "Error (wiki_page_not_found): No page Release plan exists in your group's Wiki. The "
        f'closest page is "Release plan" ({page_id}). Repeat the call with page_id "{page_id}" '
        "if you meant it. Nothing changed."
    )
    unknown = await invoke(board, {"action": "read", "page_id": "wpg_unrelated"})
    assert visible(unknown) == (
        "Error (wiki_page_not_found): No page wpg_unrelated exists in your group's Wiki. Find "
        'pages with {"action": "list"}.'
    )
    assert not (await stored(board, page_id))["deleted"]


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
    query = await invoke(board, {"action": "read", "page_id": page_id, "query": "x"})
    assert visible(query) == (
        "Error (invalid_arguments): read shows one page and has no query. To search pages, use "
        '{"action": "list", "query": "x"}; to read the page, repeat the call without query.'
    )


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
