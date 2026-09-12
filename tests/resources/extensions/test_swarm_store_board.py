"""Swarm store: board behavior."""

from __future__ import annotations

# mypy: disable-error-code=arg-type
import asyncio
import json

import pytest

from core.sessions import SessionAddress, TemporarySessionBinding
from resources.extensions.swarm.store import SwarmStore, SwarmStoreError
from tests.resources.extensions.swarm_store_helpers import (
    _profile,
    _swarm,
)
from tests.resources.extensions.swarm_store_helpers import (
    store as store,
)


@pytest.mark.asyncio
async def test_post_audience_snapshot_pings_and_idempotence(store: SwarmStore) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    first, second = [item["id"] for item in swarm["participants"]]
    post = await store.post(
        started["swarm_id"], first, text="hello", recipients=[second, second], request_id="post-1"
    )
    assert post["routes"]["ping"] == 1
    replay = await store.post(
        started["swarm_id"], first, text="hello", recipients=[second], request_id="post-1"
    )
    assert replay["post_id"] == post["post_id"] and replay["replayed"] is True
    with pytest.raises(SwarmStoreError, match="request_conflict"):
        await store.post(started["swarm_id"], first, text="changed", request_id="post-1")


@pytest.mark.asyncio
async def test_discussion_create_is_atomic_and_main_membership_is_required(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    first = swarm["participants"][0]["id"]
    created = await store.create_discussion(
        started["swarm_id"], first, title="Detail", text="opening", request_id="create-1"
    )
    assert created["opening_post_id"] and created["main_announcement_id"]
    assert (await store.join_discussion(started["swarm_id"], first, created["discussion_id"]))[
        "joined"
    ]
    with pytest.raises(SwarmStoreError, match="main_membership_required"):
        await store.leave_discussion(started["swarm_id"], first, swarm["main_discussion_id"])


@pytest.mark.asyncio
async def test_discussion_announcements_have_actionable_text_and_verified_human_targets(store):
    started = await _swarm(store)
    sid = started["swarm_id"]
    swarm = await store.get_swarm(sid)
    author, peer = swarm["participants"]
    arguments = {
        "title": 'Detail "sentinel"',
        "text": "opening-sentinel",
        "request_id": "create-detail",
    }
    created = await store.create_discussion(sid, author["id"], **arguments)
    announcement = (
        await store.read_posts(sid, peer["id"], message_id=created["main_announcement_id"])
    ).entries[0]
    with pytest.raises(json.JSONDecodeError):
        json.loads(announcement["text"])
    for value in (
        author["display_name"],
        arguments["title"],
        created["discussion_id"],
        created["opening_post_id"],
    ):
        assert value in announcement["text"]
    assert "discussion_announcement" not in announcement
    await store.bind_participant_session(
        TemporarySessionBinding(
            SessionAddress(None, "tmp", "ses"), "gen", "swarm", sid, peer["id"], {}
        )
    )
    inbox = await store.prepare_inbox_delivery(sid, peer["id"])
    delivered = next(
        row for row in inbox["entries"] if row["id"] == created["main_announcement_id"]
    )
    assert delivered["text"] == announcement["text"]
    expected = {
        "discussion_id": created["discussion_id"],
        "title": arguments["title"],
        "opening_post_id": created["opening_post_id"],
    }
    # Copying the same content must not turn an ordinary post into an announcement.
    for index, text in enumerate((announcement["text"], json.dumps(expected))):
        posted = await store.post(sid, author["id"], text=text, request_id=f"copy-{index}")
        ordinary = (await store.read_human_posts(sid, message_id=posted["post_id"])).entries[0]
        assert ordinary["text"] == text
        assert "discussion_announcement" not in ordinary
    for result in (
        await store.read_human_posts(sid),
        await store.read_human_posts(sid, message_id=created["main_announcement_id"]),
    ):
        human = next(row for row in result.entries if row["id"] == created["main_announcement_id"])
        assert human["discussion_announcement"] == expected
        assert human["text"] == announcement["text"]
    replay = await store.create_discussion(sid, author["id"], **arguments)
    assert replay["main_announcement_id"] == created["main_announcement_id"]
    assert replay["replayed"]
    assert (
        sum("discussion_announcement" in row for row in (await store.read_human_posts(sid)).entries)
        == 1
    )


@pytest.mark.asyncio
async def test_join_returns_bounded_recent_context_without_replaying_history(
    store: SwarmStore,
) -> None:
    started = await _swarm(store, count=3)
    swarm = await store.get_swarm(started["swarm_id"])
    sender, member, joining = [item["id"] for item in swarm["participants"]]
    created = await store.create_discussion(
        started["swarm_id"], sender, title="Detail", text="opening", request_id="detail-create"
    )
    await store.join_discussion(started["swarm_id"], member, created["discussion_id"])
    for index in range(24):
        await store.post(
            started["swarm_id"],
            sender,
            discussion_id=created["discussion_id"],
            text=f"detail-{index}",
            request_id=f"detail-{index}",
        )

    joined = await store.join_discussion(started["swarm_id"], joining, created["discussion_id"])

    assert joined["joined"] is True
    assert [entry["text"] for entry in joined["recent"]["entries"]] == [
        f"detail-{index}" for index in range(4, 24)
    ]
    assert joined["recent"]["has_more"] is True
    assert joined["recent"]["cursor"] is not None


@pytest.mark.asyncio
async def test_post_pages_keep_complete_messages_with_count_and_character_bounds(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    sender = swarm["participants"][0]["id"]
    for index, size in enumerate((16_000, 16_000, 16_000), start=1):
        await store.post(
            started["swarm_id"], sender, text=str(index) * size, request_id=f"sized-{index}"
        )

    newest = await store.read_posts(started["swarm_id"], sender, limit=20)
    older = await store.read_posts(started["swarm_id"], sender, cursor=newest.cursor)

    assert [len(entry["text"]) for entry in newest.entries] == [16_000]
    assert newest.has_more is True
    assert [len(entry["text"]) for entry in older.entries] == [16_000]
    assert older.has_more is True


@pytest.mark.asyncio
async def test_reply_and_participant_scope_are_checked(store: SwarmStore) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    first = swarm["participants"][0]["id"]
    created = await store.create_discussion(
        started["swarm_id"], first, title="Detail", text="opening", request_id="create-1"
    )
    with pytest.raises(SwarmStoreError, match="reply_discussion_mismatch"):
        await store.post(
            started["swarm_id"],
            first,
            text="reply",
            discussion_id=swarm["main_discussion_id"],
            reply_to=created["opening_post_id"],
            request_id="post-1",
        )
    with pytest.raises(SwarmStoreError, match="participant_not_found"):
        await store.post(started["swarm_id"], "foreign", text="x", request_id="post-2")


@pytest.mark.asyncio
async def test_scoped_cursors_are_frozen_and_reject_tampering(store: SwarmStore) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    first, second = [item["id"] for item in swarm["participants"]]
    for number in range(3):
        await store.post(started["swarm_id"], first, text=str(number), request_id=f"post-{number}")
    first_page = await store.read_posts(started["swarm_id"], first, limit=2)
    assert first_page.cursor is not None
    await store.post(started["swarm_id"], first, text="later", request_id="post-later")
    older = await store.read_posts(started["swarm_id"], first, cursor=first_page.cursor)
    assert [item["text"] for item in first_page.entries] == ["1", "2"]
    assert [item["text"] for item in older.entries] == ["0"]
    encoded_data, encoded_mac = first_page.cursor.split(".")
    for tampered in (
        first_page.cursor + "x",
        first_page.cursor + "=",
        f"{encoded_data}!.{encoded_mac}",
        f"{encoded_data}.{encoded_mac[:-1]}B=",
    ):
        with pytest.raises(SwarmStoreError, match="invalid_cursor"):
            await store.read_posts(started["swarm_id"], first, cursor=tampered)
    with pytest.raises(SwarmStoreError, match="invalid_cursor"):
        await store.read_posts(started["swarm_id"], second, cursor=first_page.cursor)
    with pytest.raises(SwarmStoreError, match="invalid_cursor"):
        await store.list_discussions(started["swarm_id"], first, cursor=first_page.cursor)
    created = await store.create_discussion(
        started["swarm_id"], first, title="Other", text="opening", request_id="other-create"
    )
    with pytest.raises(SwarmStoreError, match="invalid_cursor"):
        await store.read_posts(
            started["swarm_id"],
            first,
            discussion_id=created["discussion_id"],
            cursor=first_page.cursor,
        )


@pytest.mark.asyncio
async def test_post_audience_is_a_deduplicated_snapshot_with_pending_counts(
    store: SwarmStore,
) -> None:
    started = await _swarm(store, count=3)
    swarm = await store.get_swarm(started["swarm_id"])
    sender, main_member, nonmember = [item["id"] for item in swarm["participants"]]
    detail = await store.create_discussion(
        started["swarm_id"], sender, title="Detail", text="opening", request_id="audience-create"
    )
    await store.join_discussion(started["swarm_id"], main_member, detail["discussion_id"])
    posted = await store.post(
        started["swarm_id"],
        sender,
        discussion_id=detail["discussion_id"],
        text="union",
        recipients=[main_member, nonmember, nonmember, sender],
        request_id="audience-post",
    )
    replay = await store.post(
        started["swarm_id"],
        sender,
        discussion_id=detail["discussion_id"],
        text="union",
        recipients=[sender, nonmember, main_member],
        request_id="audience-post",
    )

    assert posted["routes"] == {"ping": 2, "discussion": 0, "main": 0}
    assert replay["post_id"] == posted["post_id"] and replay["replayed"] is True
    counts = {
        item["id"]: item["pending_count"]
        for item in (await store.get_swarm(started["swarm_id"]))["participants"]
    }
    assert counts == {sender: 0, main_member: 2, nonmember: 2}


@pytest.mark.asyncio
async def test_unknown_profile_fields_and_inapplicable_message_read_fields_fail(
    store: SwarmStore,
) -> None:
    bad = _profile()
    bad["unknown"] = True
    with pytest.raises(SwarmStoreError, match="invalid_arguments"):
        await store.save_profile(bad, expected_revision=None)
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    with pytest.raises(SwarmStoreError, match="invalid_arguments"):
        await store.read_posts(
            started["swarm_id"], swarm["participants"][0]["id"], message_id="missing", limit=1
        )


@pytest.mark.asyncio
async def test_concurrent_posts_and_reads_serialize_one_connection(store: SwarmStore) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    participant = swarm["participants"][0]["id"]
    await asyncio.gather(
        *(
            store.post(started["swarm_id"], participant, text=str(index), request_id=f"c-{index}")
            for index in range(12)
        ),
        *(store.read_posts(started["swarm_id"], participant) for _ in range(12)),
    )
    page = await store.read_posts(started["swarm_id"], participant, limit=100)
    assert len(page.entries) == 12


@pytest.mark.asyncio
async def test_human_board_reads_and_posts_need_no_participant_or_receipt(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    swarm_id = started["swarm_id"]
    participants = (await store.get_swarm(swarm_id))["participants"]

    discussions = await store.list_human_discussions(swarm_id)
    assert discussions.entries == (
        {"id": started["main_discussion_id"], "title": "Main", "member_count": 2},
    )
    posted = await store.post_human(
        swarm_id,
        text="Please compare findings",
        request_id="user-post",
        recipients=[participants[0]["id"]],
    )
    assert posted["routes"] == {"ping": 1, "discussion": 0, "main": 1}
    replay = await store.post_human(
        swarm_id,
        text="Please compare findings",
        request_id="user-post",
        recipients=[participants[0]["id"]],
    )
    assert replay["post_id"] == posted["post_id"] and replay["replayed"]
    entries = await store.read_human_posts(swarm_id)
    assert entries.entries[0]["author"] == {"kind": "user", "id": "user", "name": "User"}
    assert entries.entries[0]["text"] == "Please compare findings"
    assert (
        await store.read_human_posts(swarm_id, message_id=posted["post_id"])
    ).entries == entries.entries


@pytest.mark.asyncio
async def test_board_epoch_stale_after_resume_and_post_author_is_immutable(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    first, second = [
        item["id"] for item in (await store.get_swarm(started["swarm_id"]))["participants"]
    ]
    await store.post(
        started["swarm_id"], first, text="before", request_id="before", expected_epoch=0
    )
    assert (await store.read_posts(started["swarm_id"], second)).entries[0]["author"]["name"] == (
        await store.get_swarm(started["swarm_id"])
    )["participants"][0]["display_name"]
    await store.begin_stop(started["swarm_id"], request_id="stop", actor="test")
    await store.finish_stop(started["swarm_id"], request_id="finish", actor="test", drain_report={})
    await store.begin_resume(started["swarm_id"], request_id="resume", actor="test")
    with pytest.raises(SwarmStoreError, match="stale_epoch"):
        await store.post(
            started["swarm_id"], first, text="stale", request_id="stale", expected_epoch=0
        )
