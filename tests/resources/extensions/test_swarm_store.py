from __future__ import annotations

import asyncio
import json

# mypy: disable-error-code=arg-type
import pytest
import pytest_asyncio

from core.sessions import DeliveryReceipt, SessionAddress, TemporarySessionBinding
from resources.extensions.swarm.store import _PARTICIPANT_NAMES, SwarmStore, SwarmStoreError


def _profile(*, slug: str = "research", count: int = 2) -> dict[str, object]:
    return {
        "schema_version": 1,
        "slug": slug,
        "name": "Research",
        "participants": [{"model": "model-a", "count": count}],
        "working_directory": {"kind": "directory", "path": "C:/work"},
        "tool_access": {"mode": "selected", "allowed": []},
        "delivery": {},
    }


@pytest_asyncio.fixture
async def store(tmp_path):
    value = SwarmStore(tmp_path / "swarm.db")
    await value.open()
    yield value
    await value.close()


@pytest.mark.asyncio
async def test_prompt_selection_and_reminders_are_snapshotted(store):
    from resources.extensions.swarm.agent_text import DEFAULT_REMINDERS

    saved = await store.save_profile(
        {
            **_profile(),
            "instructions": "editable-sentinel",
            "prompt_blocks": ["core:tools", "core:working_project"],
            "reminders": dict.fromkeys(DEFAULT_REMINDERS, False),
        },
        expected_revision=None,
    )
    started = await store.create_swarm(
        saved["id"],
        "goal",
        {"cwd": "C:/work"},
        request_id="start",
        expected_profile_revision=saved["revision"],
    )
    await store.save_profile(
        {**saved, "prompt_blocks": [], "instructions": "changed"},
        expected_revision=saved["revision"],
    )
    snapshot = (await store.get_swarm(started["swarm_id"]))["profile_snapshot"]
    assert snapshot["prompt_blocks"] == ["core:tools", "core:working_project"]
    assert snapshot["instructions"] == "editable-sentinel"
    assert not any(snapshot["reminders"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"prompt_blocks": "core:runtime"},
        {"prompt_blocks": ["core:runtime", "core:runtime"]},
        {"prompt_blocks": [2]},
        {"prompt_blocks": ["core:agent_body"]},
        {"reminders": {"resume": False}},
        {"reminders": {"delivery": True, "wake": True, "resume": 1}},
    ],
)
async def test_prompt_controls_reject_invalid_values(store, fields):
    with pytest.raises(SwarmStoreError):
        await store.save_profile({**_profile(), **fields}, expected_revision=None)


async def _swarm(store: SwarmStore, *, count: int = 2) -> dict[str, object]:
    profile = await store.save_profile(_profile(count=count), expected_revision=None)
    return await store.create_swarm(
        profile["id"],
        "Investigate",
        {"cwd": "C:/work"},
        request_id="start-1",
        expected_profile_revision=profile["revision"],
    )


@pytest.mark.asyncio
async def test_current_store_reopens_with_profile_board_and_execution_state(tmp_path):
    path = tmp_path / "swarm.db"
    original = SwarmStore(path)
    await original.open()
    try:
        profile = await original.save_profile(_profile(), expected_revision=None)
        started = await original.create_swarm(
            profile["id"],
            "Keep the current work",
            {"cwd": "C:/work"},
            request_id="create",
            expected_profile_revision=profile["revision"],
        )
        sid = started["swarm_id"]
        pid = (await original.get_swarm(sid))["participants"][0]["id"]
        await original.bind_participant_session(
            TemporarySessionBinding(
                SessionAddress(None, "temporary", "session"),
                "generation",
                "swarm",
                sid,
                pid,
                {},
            )
        )
        await original.set_participant_state(sid, pid, "failed")
        post = await original.post_human(sid, text="Still pending", request_id="post")
        snapshot = await original.get_swarm(sid)
        schema = original._connection.execute(
            "SELECT name,sql FROM sqlite_master ORDER BY name"
        ).fetchall()
    finally:
        await original.close()
    reopened = SwarmStore(path)
    await reopened.open()
    try:
        assert await reopened.get_profile(profile["id"]) == profile
        assert await reopened.get_swarm(sid) == snapshot
        assert (await reopened.read_human_posts(sid)).entries[0]["id"] == post["post_id"]
        assert (await reopened.prepare_inbox_delivery(sid, pid))["entries"][0]["id"] == post[
            "post_id"
        ]
        assert (
            reopened._connection.execute(
                "SELECT name,sql FROM sqlite_master ORDER BY name"
            ).fetchall()
            == schema
        )
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_profiles_validate_revision_slug_and_are_immutable(store: SwarmStore) -> None:
    saved = await store.save_profile(_profile(), expected_revision=None)
    assert saved["revision"] == 1
    saved["name"] = "Mutated"
    assert (await store.get_profile(saved["id"]))["name"] == "Research"
    changed = await store.get_profile(saved["id"])
    changed["name"] = "Changed"
    assert (await store.save_profile(changed, expected_revision=1))["revision"] == 2
    with pytest.raises(SwarmStoreError, match="revision_conflict"):
        await store.save_profile(changed, expected_revision=1)
    with pytest.raises(SwarmStoreError, match="slug_unavailable"):
        await store.save_profile(_profile(), expected_revision=None)


@pytest.mark.asyncio
async def test_automatic_profile_shortcuts_are_unique_and_stable(store: SwarmStore) -> None:
    profile = _profile()
    profile.pop("slug")
    first, second = await asyncio.gather(
        store.save_profile(profile, expected_revision=None),
        store.save_profile(profile, expected_revision=None),
    )
    assert {first["slug"], second["slug"]} == {"research", "research-2"}
    original_slug = first.pop("slug")
    first["name"] = "Renamed"
    updated = await store.save_profile(first, expected_revision=1)
    assert updated["slug"] == original_slug
    assert (await store.get_profile(updated["id"]))["slug"] == updated["slug"]
    profile["name"] = "研究"
    assert (await store.save_profile(profile, expected_revision=None))["slug"] == "swarm"


@pytest.mark.asyncio
async def test_profile_defaults_and_strict_nested_validation(store: SwarmStore) -> None:
    profile = _profile()
    profile.pop("delivery")
    saved = await store.save_profile(profile, expected_revision=None)
    assert saved["delivery"] == {
        "main": {"mode": "all", "wake_idle": True},
        "discussion": {"mode": "all", "wake_idle": True},
        "ping": {"mode": "all", "wake_idle": True},
        "coalesce_ms": 250,
        "batch_messages": 20,
        "batch_chars": 24_000,
    }
    invalid_profiles = [
        {**_profile(slug="bad-schema"), "schema_version": True},
        {**_profile(slug="bad-cwd"), "working_directory": {"kind": "other", "path": "x"}},
        {
            **_profile(slug="bad-formation"),
            "participants": [{"model": "model-a", "count": 1, "extra": True}],
        },
        {**_profile(slug="bad-delivery"), "delivery": {"main": {"mode": "later"}}},
        {
            **_profile(slug="bad-tools"),
            "tool_access": {"mode": "selected", "allowed": [], "extra": True},
        },
        {**_profile(slug="bad-skills"), "allowed_skills": ["ok", 1]},
        {
            **_profile(slug="bad-thinking"),
            "participants": [{"model": "model-a", "count": 1, "thinking_effort": "impossible"}],
        },
        {
            **_profile(slug="bad-temperature"),
            "participants": [{"model": "model-a", "count": 1, "temperature": True}],
        },
        {
            **_profile(slug="bad-fallback"),
            "participants": [{"model": "model-a", "count": 1, "fallback_models": ["a", "a"]}],
        },
    ]
    for invalid in invalid_profiles:
        with pytest.raises(SwarmStoreError, match="invalid_arguments"):
            await store.save_profile(invalid, expected_revision=None)


@pytest.mark.asyncio
async def test_delete_profile_and_start_require_the_current_profile_revision(
    store: SwarmStore,
) -> None:
    saved = await store.save_profile(_profile(), expected_revision=None)
    with pytest.raises(SwarmStoreError, match="revision_conflict"):
        await store.create_swarm(
            saved["id"],
            "Investigate",
            {"cwd": "C:/work"},
            request_id="stale-start",
            expected_profile_revision=2,
        )
    started = await store.create_swarm(
        saved["id"],
        "Investigate",
        {"cwd": "C:/work"},
        request_id="fresh-start",
        expected_profile_revision=1,
    )
    with pytest.raises(SwarmStoreError, match="revision_conflict"):
        await store.delete_profile(saved["id"], expected_revision=2)
    await store.delete_profile(saved["id"], expected_revision=1)
    with pytest.raises(SwarmStoreError, match="profile_not_found"):
        await store.get_profile(saved["id"])
    snapshot = await store.get_swarm(started["swarm_id"])
    assert snapshot["profile_snapshot"] == saved


@pytest.mark.asyncio
async def test_swarm_snapshots_profile_and_has_stable_roster(store: SwarmStore) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    assert swarm["state"] == "preparing"
    names = [item["display_name"] for item in swarm["participants"]]
    assert len(set(names)) == 2
    assert set(names) <= set(_PARTICIPANT_NAMES)
    profile = await store.get_profile(swarm["profile_snapshot"]["id"])
    profile["name"] = "Later"
    await store.save_profile(profile, expected_revision=1)
    assert (await store.get_swarm(started["swarm_id"]))["profile_snapshot"]["name"] == "Research"


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
async def test_prepared_delivery_reconciles_only_matching_canonical_receipts(tmp_path) -> None:
    receipts: dict[str, DeliveryReceipt] = {}

    async def lookup(
        _address: SessionAddress, _generation: str, _owner: str, receipt_id: str
    ) -> DeliveryReceipt | None:
        return receipts.get(receipt_id)

    value = SwarmStore(tmp_path / "delivery.db", lookup_delivery_receipt=lookup)
    await value.open()
    try:
        started = await _swarm(value)
        swarm = await value.get_swarm(started["swarm_id"])
        sender, recipient = [item["id"] for item in swarm["participants"]]
        binding = TemporarySessionBinding(
            SessionAddress(None, "tmp_agent", "ses_delivery"),
            "gen_delivery",
            "swarm",
            started["swarm_id"],
            recipient,
            {},
        )
        await value.bind_participant_session(binding)
        await value.post(started["swarm_id"], sender, text="one", request_id="delivery-one")
        prepared = await value.prepare_inbox_delivery(started["swarm_id"], recipient)
        assert [entry["text"] for entry in prepared["entries"]] == ["one"]
        assert await value.reconcile_delivery(prepared["receipt_id"]) is False
        receipts[prepared["receipt_id"]] = DeliveryReceipt(
            prepared["receipt_id"],
            prepared["content_hash"],
            prepared["effect_kind"],
            {"kind": "tool", "sequence": 4},
        )
        assert await value.reconcile_delivery(prepared["receipt_id"]) is True
        assert await value.reconcile_delivery(prepared["receipt_id"]) is True
        assert (await value.prepare_inbox_delivery(started["swarm_id"], recipient))["entries"] == []
        receipts[prepared["receipt_id"]] = DeliveryReceipt(
            prepared["receipt_id"],
            "different",
            prepared["effect_kind"],
            {"kind": "tool", "sequence": 4},
        )
        with pytest.raises(SwarmStoreError, match="receipt_conflict"):
            await value.reconcile_delivery(prepared["receipt_id"])
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_inbox_batches_complete_posts_without_exceeding_character_budget(tmp_path) -> None:
    async def lookup(*_arguments: object) -> DeliveryReceipt | None:
        return None

    value = SwarmStore(tmp_path / "batch.db", lookup_delivery_receipt=lookup)
    await value.open()
    try:
        started = await _swarm(value)
        swarm = await value.get_swarm(started["swarm_id"])
        sender, recipient = [item["id"] for item in swarm["participants"]]
        await value.bind_participant_session(
            TemporarySessionBinding(
                SessionAddress(None, "tmp_agent", "ses_batch"),
                "gen_batch",
                "swarm",
                started["swarm_id"],
                recipient,
                {},
            )
        )
        for index, size in enumerate((16_000, 8_000, 1), start=1):
            await value.post(
                started["swarm_id"], sender, text="x" * size, request_id=f"batch-{index}"
            )
        prepared = await value.prepare_inbox_delivery(started["swarm_id"], recipient)
        assert [len(entry["text"]) for entry in prepared["entries"]] == [16_000, 8_000]
        assert prepared["pending_remaining"] == 1
        discussions = await value.list_discussions(started["swarm_id"], recipient)
        assert discussions.entries[0]["pending_count"] == 3
        await value.apply_delivery_settings(
            started["swarm_id"],
            {**swarm["delivery"], "batch_chars": 16_000},
            expected_revision=1,
            request_id="smaller-batches",
            actor="user",
        )
        current = await value.prepare_inbox_delivery(started["swarm_id"], recipient)
        assert [len(entry["text"]) for entry in current["entries"]] == [16_000]
        assert current["pending_remaining"] == 2
        assert prepared["entries"][1]["text"] == "x" * 8_000
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_settings_are_revisioned_audited_and_closed_swarms_reject_board_mutations(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    original = await store.get_swarm(started["swarm_id"])
    updated_delivery = {**original["delivery"], "main": {"mode": "pull", "wake_idle": False}}
    saved = await store.apply_delivery_settings(
        started["swarm_id"],
        updated_delivery,
        expected_revision=1,
        request_id="settings-1",
        actor="user",
    )
    assert saved["revision"] == 2
    assert (await store.get_swarm(started["swarm_id"]))["profile_snapshot"]["delivery"] == original[
        "delivery"
    ]
    assert (await store.get_swarm(started["swarm_id"]))["delivery"] == updated_delivery
    replay = await store.apply_delivery_settings(
        started["swarm_id"],
        updated_delivery,
        expected_revision=1,
        request_id="settings-1",
        actor="user",
    )
    assert replay["replayed"] is True
    await store.begin_stop(started["swarm_id"], request_id="stop", actor="user")
    await store.finish_stop(
        started["swarm_id"], request_id="stop-finish", actor="user", drain_report={"drained": True}
    )
    with pytest.raises(SwarmStoreError, match="swarm_closed"):
        await store.post(
            started["swarm_id"],
            original["participants"][0]["id"],
            text="closed",
            request_id="closed-post",
        )


@pytest.mark.asyncio
async def test_overlapping_prepared_batches_acknowledge_each_recipient_once(tmp_path) -> None:
    receipts: dict[str, DeliveryReceipt] = {}

    async def lookup(
        _address: SessionAddress, _generation: str, _owner: str, receipt_id: str
    ) -> DeliveryReceipt | None:
        return receipts.get(receipt_id)

    value = SwarmStore(tmp_path / "overlap.db", lookup_delivery_receipt=lookup)
    await value.open()
    try:
        started = await _swarm(value)
        swarm = await value.get_swarm(started["swarm_id"])
        sender, recipient = [item["id"] for item in swarm["participants"]]
        await value.bind_participant_session(
            TemporarySessionBinding(
                SessionAddress(None, "tmp_agent", "ses_overlap"),
                "gen_overlap",
                "swarm",
                started["swarm_id"],
                recipient,
                {},
            )
        )
        await value.post(started["swarm_id"], sender, text="shared", request_id="overlap-post")
        inbox = await value.prepare_inbox_delivery(started["swarm_id"], recipient)
        page = await value.read_posts(started["swarm_id"], recipient)
        board = await value.prepare_board_read_delivery(
            started["swarm_id"], recipient, [entry["id"] for entry in page.entries]
        )
        for prepared in (board, inbox):
            receipts[prepared["receipt_id"]] = DeliveryReceipt(
                prepared["receipt_id"],
                prepared["content_hash"],
                prepared["effect_kind"],
                {"kind": "tool", "sequence": 9},
            )
        assert await value.reconcile_delivery(board["receipt_id"])
        assert await value.reconcile_delivery(inbox["receipt_id"])
        assert (await value.prepare_inbox_delivery(started["swarm_id"], recipient))["entries"] == []
    finally:
        await value.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["all", "idle", "pull"])
@pytest.mark.parametrize("wake_idle", [True, False])
@pytest.mark.parametrize("state", ["running", "idle"])
async def test_automatic_policy_modes_and_wake_watermark(
    store: SwarmStore, mode: str, wake_idle: bool, state: str
) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    sender, recipient = [item["id"] for item in swarm["participants"]]
    settings = {**swarm["delivery"], "main": {"mode": mode, "wake_idle": wake_idle}}
    await store.apply_delivery_settings(
        started["swarm_id"],
        settings,
        expected_revision=1,
        request_id=f"policy-{mode}-{wake_idle}",
        actor="test",
    )
    await store.set_participant_state(started["swarm_id"], recipient, state)
    await store.post(
        started["swarm_id"], sender, text="policy", request_id=f"post-{mode}-{wake_idle}-{state}"
    )
    result = await store.prepare_automatic_delivery(
        started["swarm_id"],
        recipient,
        expected_epoch=0,
        admission_boundary=1 if state == "idle" else None,
    )
    assert result["wake"] is (wake_idle and state == "idle")
    if (mode == "pull" and not (wake_idle and state == "idle")) or (
        mode == "idle" and state == "running"
    ):
        assert result["entries"] == []
    else:
        assert len(result["entries"]) == 1
    repeat = await store.prepare_automatic_delivery(
        started["swarm_id"],
        recipient,
        expected_epoch=0,
        admission_boundary=1 if state == "idle" else None,
    )
    assert repeat["wake"] is False
    await store.post(
        started["swarm_id"], sender, text="new", request_id=f"new-{mode}-{wake_idle}-{state}"
    )
    newer = await store.prepare_automatic_delivery(
        started["swarm_id"],
        recipient,
        expected_epoch=0,
        admission_boundary=2 if state == "idle" else None,
    )
    assert newer["wake"] is False  # one unadmitted wake coalesces newer pending posts


@pytest.mark.asyncio
async def test_stop_resume_retires_old_epoch_and_settings_toggle_retracts_wake(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    sender, recipient = [item["id"] for item in swarm["participants"]]
    await store.set_participant_state(started["swarm_id"], recipient, "idle")
    await store.post(started["swarm_id"], sender, text="wake", request_id="wake-post")
    assert (
        await store.prepare_automatic_delivery(
            started["swarm_id"], recipient, expected_epoch=0, admission_boundary=1
        )
    )["wake"]
    disabled = {
        **swarm["delivery"],
        "main": {"mode": "all", "wake_idle": False},
        "discussion": {"mode": "all", "wake_idle": False},
        "ping": {"mode": "all", "wake_idle": False},
    }
    await store.apply_delivery_settings(
        started["swarm_id"], disabled, expected_revision=1, request_id="disable", actor="test"
    )
    await store.begin_stop(started["swarm_id"], request_id="stop", actor="test")
    await store.finish_stop(
        started["swarm_id"], request_id="stop-finish", actor="test", drain_report={"drained": True}
    )
    await store.begin_resume(started["swarm_id"], request_id="resume", actor="test")
    with pytest.raises(SwarmStoreError, match="stale_epoch"):
        await store.prepare_automatic_delivery(
            started["swarm_id"], recipient, expected_epoch=0, admission_boundary=2
        )


@pytest.mark.asyncio
async def test_recovery_interrupts_open_swarms_without_admitting_work(tmp_path) -> None:
    path = tmp_path / "recovery.db"
    first = SwarmStore(path)
    await first.open()
    started = await _swarm(first)
    swarm = await first.get_swarm(started["swarm_id"])
    participant = swarm["participants"][0]["id"]
    await first.set_swarm_state(started["swarm_id"], "running")
    await first.set_participant_state(started["swarm_id"], participant, "running")
    await first.close()

    recovered = SwarmStore(path)
    await recovered.open()
    try:
        assert await recovered.recover_interrupted() == [
            {"swarm_id": started["swarm_id"], "state": "interrupted"}
        ]
        snapshot = await recovered.get_swarm(started["swarm_id"])
        assert snapshot["state"] == "interrupted"
        assert [item["state"] for item in snapshot["participants"]] == ["interrupted", "idle"]
        assert await recovered.recover_interrupted() == []
        with pytest.raises(SwarmStoreError, match="swarm_closed"):
            await recovered.prepare_automatic_delivery(
                started["swarm_id"], participant, expected_epoch=0
            )
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_settings_enable_returns_wake_intent_and_events_are_bounded(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    sender, recipient = [item["id"] for item in swarm["participants"]]
    await store.set_participant_state(started["swarm_id"], recipient, "idle")
    disabled = {**swarm["delivery"], "main": {"mode": "all", "wake_idle": False}}
    await store.apply_delivery_settings(
        started["swarm_id"], disabled, expected_revision=1, request_id="disable", actor="user"
    )
    await store.post(started["swarm_id"], sender, text="pending", request_id="pending")
    enabled = {**disabled, "main": {"mode": "all", "wake_idle": True}}
    applied = await store.apply_delivery_settings(
        started["swarm_id"], enabled, expected_revision=2, request_id="enable", actor="user"
    )

    assert applied["wake_intents"] == [
        {"participant_id": recipient, "pending_count": 1, "settings_revision": 3}
    ]
    assert (
        applied["old"] == disabled and applied["new"] == enabled and applied["effective"] == enabled
    )
    events = await store.list_events(started["swarm_id"], limit=1)
    assert events.entries[0]["kind"] == "settings"
    assert events.entries[0]["old"] == disabled
    assert events.entries[0]["new"] == enabled
    assert events.entries[0]["settings_revision"] == 3
    assert events.has_more is True

    second_profile = await store.save_profile(_profile(slug="other"), expected_revision=None)
    await store.create_swarm(
        second_profile["id"],
        "Other",
        {"cwd": "C:/work"},
        request_id="other-start",
        expected_profile_revision=second_profile["revision"],
    )
    page = await store.list_swarms(limit=1)
    assert len(page.entries) == 1 and page.has_more is True and page.cursor is not None
    assert page.entries[0]["title"] == "Other"


@pytest.mark.asyncio
async def test_wake_claim_preserves_idle_boundary_for_running_idle_delivery(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    sender, recipient = [
        item["id"] for item in (await store.get_swarm(started["swarm_id"]))["participants"]
    ]
    await store.set_participant_state(started["swarm_id"], recipient, "idle", idle_boundary=7)
    await store.post(started["swarm_id"], sender, text="idle", request_id="idle-boundary")
    assert (
        await store.prepare_automatic_delivery(
            started["swarm_id"], recipient, expected_epoch=0, admission_boundary=7
        )
    )["wake"]
    assert (await store.claim_wake(started["swarm_id"], recipient, expected_epoch=0))[
        "boundary"
    ] == 7
    assert (
        await store.mark_wake_admitted(
            started["swarm_id"], recipient, expected_epoch=0, run_id="wake-run", boundary=7
        )
    )["admitted"]
    assert (
        await store.mark_wake_admitted(
            started["swarm_id"], recipient, expected_epoch=0, run_id="wake-run", boundary=7
        )
    )["replayed"]
    assert (
        await store.prepare_automatic_delivery(
            started["swarm_id"], recipient, expected_epoch=0, admission_boundary=7
        )
    )["replayed"]


@pytest.mark.asyncio
async def test_status_and_exact_run_finish(store: SwarmStore) -> None:
    started = await _swarm(store)
    participant = (await store.get_swarm(started["swarm_id"]))["participants"][0]["id"]
    status = await store.participant_status(started["swarm_id"], participant, limit=1)
    assert (
        status["roster"][0]["name"]
        == (await store.get_swarm(started["swarm_id"]))["participants"][0]["display_name"]
    )
    assert "epoch" not in status and "usage" not in status
    await store.record_run_started(started["swarm_id"], participant, run_id="new", expected_epoch=0)
    with pytest.raises(SwarmStoreError, match="stale_run"):
        await store.reconcile_run_finished(
            started["swarm_id"], participant, run_id="old", expected_epoch=0, outcome="completed"
        )


@pytest.mark.asyncio
async def test_unadmitted_wake_coalesces_without_advancing_watermark(store: SwarmStore) -> None:
    started = await _swarm(store)
    sender, recipient = [
        item["id"] for item in (await store.get_swarm(started["swarm_id"]))["participants"]
    ]
    await store.set_participant_state(started["swarm_id"], recipient, "idle", idle_boundary=4)
    await store.post(started["swarm_id"], sender, text="A", request_id="wake-a")
    assert (
        await store.prepare_automatic_delivery(
            started["swarm_id"], recipient, expected_epoch=0, admission_boundary=4
        )
    )["wake"]
    connection = store._connection  # noqa: SLF001 - durable watermark assertion
    assert connection is not None
    assert (
        connection.execute(
            "SELECT wake_announced_seq FROM participants WHERE id=?", (recipient,)
        ).fetchone()[0]
        == 0
    )
    await store.post(started["swarm_id"], sender, text="B", request_id="wake-b")
    assert not (
        await store.prepare_automatic_delivery(
            started["swarm_id"], recipient, expected_epoch=0, admission_boundary=4
        )
    )["wake"]
    assert (await store.claim_wake(started["swarm_id"], recipient, expected_epoch=0))["pending"]
    await store.mark_wake_admitted(
        started["swarm_id"], recipient, expected_epoch=0, run_id="wake", boundary=4
    )
    assert (
        connection.execute(
            "SELECT wake_announced_seq FROM participants WHERE id=?", (recipient,)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_reopen_reannounces_unadmitted_prepared_delivery(store: SwarmStore) -> None:
    started = await _swarm(store)
    sender, recipient = [
        item["id"] for item in (await store.get_swarm(started["swarm_id"]))["participants"]
    ]
    await store.set_participant_state(started["swarm_id"], recipient, "idle", idle_boundary=3)
    await store.post(started["swarm_id"], sender, text="A", request_id="prepared-a")
    prepared = await store.prepare_automatic_delivery(
        started["swarm_id"], recipient, expected_epoch=0, admission_boundary=3
    )
    assert prepared["wake"] and [entry["text"] for entry in prepared["entries"]] == ["A"]

    await store.begin_stop(started["swarm_id"], request_id="cancel", actor="test")
    await store.finish_stop(
        started["swarm_id"], request_id="cancel-finish", actor="test", drain_report={}
    )
    await store.begin_resume(started["swarm_id"], request_id="reopen", actor="test")
    await store.set_participant_state(started["swarm_id"], recipient, "idle", idle_boundary=9)

    replay = await store.prepare_automatic_delivery(
        started["swarm_id"], recipient, expected_epoch=1, admission_boundary=9
    )
    assert replay["replayed"] and replay["wake"]
    assert [entry["text"] for entry in replay["entries"]] == ["A"]
    claimed = await store.claim_wake(started["swarm_id"], recipient, expected_epoch=1)
    assert claimed["pending"] and claimed["boundary"] == 9
    admitted = await store.mark_wake_admitted(
        started["swarm_id"], recipient, expected_epoch=1, run_id="reopened", boundary=9
    )
    assert admitted["admitted"]


@pytest.mark.asyncio
async def test_foreign_ping_after_resume_has_exact_code(store: SwarmStore) -> None:
    started = await _swarm(store)
    sender = (await store.get_swarm(started["swarm_id"]))["participants"][0]["id"]
    await store.begin_stop(started["swarm_id"], request_id="stop-r", actor="test")
    await store.finish_stop(
        started["swarm_id"], request_id="finish-r", actor="test", drain_report={}
    )
    await store.begin_resume(started["swarm_id"], request_id="resume-r", actor="test")
    with pytest.raises(SwarmStoreError, match="invalid_recipient"):
        await store.post(
            started["swarm_id"],
            sender,
            text="ping",
            request_id="foreign-ping",
            recipients=("foreign",),
            expected_epoch=1,
        )


@pytest.mark.asyncio
async def test_status_cursor_and_board_epoch_inactive_guards(store: SwarmStore) -> None:
    started = await _swarm(store, count=2)
    swarm = await store.get_swarm(started["swarm_id"])
    first, second = [item["id"] for item in swarm["participants"]]
    page = await store.participant_status(started["swarm_id"], first, limit=1)
    assert page["self"]["id"] == first and page["has_more"] and page["cursor"]
    with pytest.raises(SwarmStoreError, match="invalid_cursor"):
        await store.participant_status(started["swarm_id"], second, cursor=page["cursor"], limit=1)
    await store.set_participant_state(started["swarm_id"], first, "failed")
    await store.create_discussion(
        started["swarm_id"], first, title="Review", text="Question", request_id="inactive"
    )
    await store.post(
        started["swarm_id"], first, text="human", request_id="human", author_kind="user"
    )


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


@pytest.mark.asyncio
async def test_newer_run_blocks_old_finished_callback_and_status_limit_100(
    store: SwarmStore,
) -> None:
    started = await _swarm(store, count=101)
    participant = (await store.get_swarm(started["swarm_id"]))["participants"][0]["id"]
    page = await store.participant_status(started["swarm_id"], participant, limit=100)
    assert len(page["roster"]) == 100 and page["has_more"]
    await store.record_run_started(started["swarm_id"], participant, run_id="old", expected_epoch=0)
    await store.record_run_started(started["swarm_id"], participant, run_id="new", expected_epoch=0)
    with pytest.raises(SwarmStoreError, match="stale_run"):
        await store.reconcile_run_finished(
            started["swarm_id"], participant, run_id="old", expected_epoch=0, outcome="completed"
        )
    assert (
        await store.reconcile_run_finished(
            started["swarm_id"], participant, run_id="new", expected_epoch=0, outcome="completed"
        )
    )["state"] == "idle"


@pytest.mark.asyncio
async def test_participant_lifecycle_aggregates_swarm_state(store: SwarmStore) -> None:
    started = await _swarm(store, count=2)
    swarm_id = started["swarm_id"]
    first, second = [item["id"] for item in (await store.get_swarm(swarm_id))["participants"]]
    await store.set_swarm_state(swarm_id, "running")
    await store.set_participant_state(swarm_id, first, "idle")
    await store.set_participant_state(swarm_id, second, "idle")
    assert (await store.get_swarm(swarm_id))["state"] == "idle"
    await store.set_participant_state(swarm_id, second, "failed")
    assert (await store.get_swarm(swarm_id))["state"] == "needs_attention"


@pytest.mark.asyncio
async def test_open_epoch_resume_excludes_busy_participant(store: SwarmStore) -> None:
    started = await _swarm(store, count=2)
    swarm_id = started["swarm_id"]
    waiting, busy = [item["id"] for item in (await store.get_swarm(swarm_id))["participants"]]
    await store.set_swarm_state(swarm_id, "running")
    await store.set_participant_state(swarm_id, waiting, "idle")
    await store.record_run_started(swarm_id, busy, run_id="busy", expected_epoch=0)
    resumed = await store.begin_resume(swarm_id, request_id="open", actor="test")
    assert resumed["reused_epoch"] and resumed["participant_ids"] == [waiting]
    assert (await store.get_swarm(swarm_id))["participants"][1]["lifecycle_run_id"] == "busy"


@pytest.mark.asyncio
async def test_resume_waits_for_stop_to_finish_before_opening_epoch(store: SwarmStore) -> None:
    started = await _swarm(store)
    swarm_id = started["swarm_id"]
    await store.begin_stop(swarm_id, request_id="stop", actor="test")
    with pytest.raises(SwarmStoreError, match="swarm_closed"):
        await store.begin_resume(swarm_id, request_id="too-early", actor="test")
    assert (await store.get_swarm(swarm_id))["epoch"] == 0
    await store.finish_stop(
        swarm_id, request_id="stop", actor="test", drain_report={"closed": True, "run_ids": []}
    )
    resumed = await store.begin_resume(swarm_id, request_id="resume", actor="test")
    assert resumed["epoch"] == 1


@pytest.mark.asyncio
async def test_board_read_preserves_saved_timestamp(store):
    started = await _swarm(store)
    snapshot = await store.get_swarm(started["swarm_id"])
    await store.set_swarm_state(snapshot["id"], "running")
    result = await store.post(
        snapshot["id"],
        snapshot["participants"][0]["id"],
        text="timestamp-sentinel",
        request_id="post-time",
    )
    page = await store.read_human_posts(
        snapshot["id"], discussion_id=snapshot["main_discussion_id"]
    )
    post = next(item for item in page.entries if item["id"] == result["post_id"])
    from datetime import datetime

    assert datetime.fromisoformat(post["created_at"]).utcoffset().total_seconds() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("completed", "idle"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("interrupted", "interrupted"),
    ],
)
async def test_participant_state_follows_run_outcome(store, outcome, expected):
    started = await _swarm(store, count=1)
    swarm_id = started["swarm_id"]
    participant = (await store.get_swarm(swarm_id))["participants"][0]["id"]
    assert (await store.participant_status(swarm_id, participant))["self"]["state"] == "idle"
    await store.set_swarm_state(swarm_id, "running")
    await store.record_run_started(swarm_id, participant, run_id="run", expected_epoch=0)
    assert (await store.participant_status(swarm_id, participant))["self"]["state"] == "running"
    await store.reconcile_run_finished(
        swarm_id, participant, run_id="run", expected_epoch=0, outcome=outcome
    )
    assert (await store.participant_status(swarm_id, participant))["self"]["state"] == expected
    assert (await store.get_swarm(swarm_id))["state"] == (
        "idle" if outcome == "completed" else "needs_attention"
    )
    await store.post_human(swarm_id, text="Follow-up", request_id="follow-up")
    assert (await store.participant_status(swarm_id, participant))["pending_count"] == 1


@pytest.mark.asyncio
async def test_stop_resume_keeps_every_participant_and_rejects_stale_callbacks(store):
    started = await _swarm(store, count=3)
    swarm_id = started["swarm_id"]
    participants = [p["id"] for p in (await store.get_swarm(swarm_id))["participants"]]
    names = {p["id"]: p["display_name"] for p in (await store.get_swarm(swarm_id))["participants"]}
    await store.set_swarm_state(swarm_id, "running")
    for peer, outcome in zip(participants, ["completed", "failed", "cancelled"], strict=True):
        await store.record_run_started(swarm_id, peer, run_id=peer, expected_epoch=0)
        await store.reconcile_run_finished(
            swarm_id, peer, run_id=peer, expected_epoch=0, outcome=outcome
        )
    stop = await store.begin_stop(swarm_id, request_id="stop", actor="user")
    assert (await store.begin_stop(swarm_id, request_id="stop", actor="user"))["replayed"]
    await store.finish_stop(
        swarm_id, request_id="stop", actor="user", drain_report={"closed": True, "run_ids": []}
    )
    resumed = await store.begin_resume(swarm_id, request_id="resume", actor="user")
    assert resumed["participant_ids"] == participants and resumed["epoch"] == stop["epoch"] + 1
    assert {
        p["id"]: p["display_name"] for p in (await store.get_swarm(swarm_id))["participants"]
    } == names
    assert (await store.begin_resume(swarm_id, request_id="resume", actor="user"))["replayed"]
    with pytest.raises(SwarmStoreError, match="stale_epoch"):
        await store.reconcile_run_finished(
            swarm_id, participants[0], run_id=participants[0], expected_epoch=0, outcome="completed"
        )
    assert {p["state"] for p in (await store.get_swarm(swarm_id))["participants"]} == {"idle"}


@pytest.mark.asyncio
async def test_no_participant_lifecycle_storage_or_summary_contract(store):
    started = await _swarm(store)
    participants = (await store.get_swarm(started["swarm_id"]))["participants"]
    assert all(
        not ({"wait_reason", "summary", "artifacts", "completion_call_id"} & p.keys())
        for p in participants
    )
    assert "done_count" not in (await store.list_swarms()).entries[0]
    assert (
        store._connection.execute(
            "SELECT name FROM sqlite_master WHERE name='lifecycle_intents'"
        ).fetchone()
        is None
    )
    columns = {r["name"] for r in store._connection.execute("PRAGMA table_info(participants)")}
    assert not {"wait_reason", "summary_json", "artifacts_json", "completion_call_id"} & columns


@pytest.mark.asyncio
async def test_completed_admission_outcomes_survive_reopen_and_later_epochs(store):
    started = await _swarm(store, count=1)
    sid = started["swarm_id"]
    snapshot = await store.get_swarm(sid)
    pid = snapshot["participants"][0]["id"]
    await store.set_swarm_state(sid, "running")
    admitted = await store.finish_admission(
        sid, request_id="start-1", kind="start", runs=[{"participant_id": pid, "run_id": "first"}]
    )
    await store.begin_stop(sid, request_id="stop", actor="user")
    stopped = await store.finish_stop(sid, request_id="stop", actor="user", drain_report={})
    await store.begin_resume(sid, request_id="resume", actor="user")
    await store.set_swarm_state(sid, "running")
    resumed = await store.finish_admission(
        sid, request_id="resume", kind="resume", runs=[{"participant_id": pid, "run_id": "second"}]
    )
    await store.begin_stop(sid, request_id="stop-2", actor="user")
    await store.finish_stop(sid, request_id="stop-2", actor="user", drain_report={})
    await store.begin_resume(sid, request_id="resume-2", actor="user")
    await store.set_swarm_state(sid, "running")
    current = await store.get_swarm(sid)
    await store.close()
    await store.open()
    profile = snapshot["profile_snapshot"]
    assert await store.create_swarm(
        profile["id"],
        "Investigate",
        {"cwd": "C:/work"},
        request_id="start-1",
        expected_profile_revision=profile["revision"],
    ) == {**admitted, "replayed": True}
    assert await store.begin_resume(sid, request_id="resume", actor="user") == {
        **resumed,
        "replayed": True,
    }
    assert await store.begin_stop(sid, request_id="stop", actor="user") == {
        **stopped,
        "replayed": True,
    }
    assert await store.get_swarm(sid) == current


@pytest.mark.asyncio
async def test_late_start_and_wake_ack_cannot_resurrect_finished_run(store):
    started = await _swarm(store, count=1)
    sid = started["swarm_id"]
    pid = (await store.get_swarm(sid))["participants"][0]["id"]
    await store.set_swarm_state(sid, "running")
    await store.bind_participant_session(
        TemporarySessionBinding(SessionAddress(None, "tmp", "ses"), "gen", "swarm", sid, pid, {})
    )
    await store.post_human(sid, text="Wake", request_id="wake")
    await store.prepare_wake(sid, pid, expected_epoch=0)
    claim = await store.claim_wake(sid, pid, expected_epoch=0)
    assert claim["pending"]
    await store.record_run_started(sid, pid, run_id="quick", expected_epoch=0)
    await store.reconcile_run_finished(
        sid, pid, run_id="quick", expected_epoch=0, outcome="completed"
    )
    assert (await store.record_run_started(sid, pid, run_id="quick", expected_epoch=0))[
        "state"
    ] == "idle"
    await store.mark_wake_admitted(
        sid, pid, expected_epoch=0, run_id="quick", boundary=claim["boundary"]
    )
    assert (await store.participant_status(sid, pid))["self"]["state"] == "idle"
    assert (
        await store.mark_wake_admitted(
            sid, pid, expected_epoch=0, run_id="quick", boundary=claim["boundary"]
        )
    )["replayed"]


@pytest.mark.asyncio
async def test_stop_preserves_idle_and_failed_outcomes(store):
    started = await _swarm(store, count=3)
    sid = started["swarm_id"]
    peers = (await store.get_swarm(sid))["participants"]
    await store.record_run_started(sid, peers[0]["id"], run_id="active", expected_epoch=0)
    await store.set_participant_state(sid, peers[2]["id"], "failed")
    await store.begin_stop(sid, request_id="stop", actor="user")
    await store.finish_stop(
        sid, request_id="stop", actor="user", drain_report={"closed": True, "run_ids": []}
    )
    assert [p["state"] for p in (await store.get_swarm(sid))["participants"]] == [
        "cancelled",
        "idle",
        "failed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [False, True])
async def test_targeted_resume_preserves_other_participants(store, closed):
    started = await _swarm(store, count=3)
    swarm_id = started["swarm_id"]
    await store.set_swarm_state(swarm_id, "running")
    peers = (await store.get_swarm(swarm_id))["participants"]
    for peer, state in zip(peers, ["failed", "idle", "running"], strict=True):
        await store.set_participant_state(swarm_id, peer["id"], state)
    if closed:
        await store.begin_stop(swarm_id, request_id="stop", actor="user")
        await store.finish_stop(
            swarm_id, request_id="finish-stop", actor="user", drain_report={"drained": True}
        )
    before = (await store.get_swarm(swarm_id))["participants"]
    result = await store.begin_resume(
        swarm_id, request_id="target", actor="user", participant_id=peers[0]["id"]
    )
    assert result["participant_ids"] == [peers[0]["id"]]
    after = (await store.get_swarm(swarm_id))["participants"]
    assert after[1:] == before[1:]
    assert await store.begin_resume(
        swarm_id, request_id="target", actor="user", participant_id=peers[0]["id"]
    ) == {**result, "replayed": True}
    with pytest.raises(SwarmStoreError) as conflict:
        await store.begin_resume(
            swarm_id, request_id="target", actor="user", participant_id=peers[1]["id"]
        )
    assert conflict.value.code == "request_conflict"


@pytest.mark.asyncio
async def test_targeted_resume_rejects_foreign_or_active_participant(store):
    started = await _swarm(store)
    swarm_id = started["swarm_id"]
    await store.set_swarm_state(swarm_id, "running")
    peers = (await store.get_swarm(swarm_id))["participants"]
    await store.set_participant_state(swarm_id, peers[0]["id"], "running")
    for peer_id in ["foreign", peers[0]["id"]]:
        with pytest.raises(SwarmStoreError):
            await store.begin_resume(
                swarm_id, request_id=peer_id, actor="user", participant_id=peer_id
            )


def test_participant_name_pool_is_short_and_unique():
    assert len(_PARTICIPANT_NAMES) == 300
    assert len({name.casefold() for name in _PARTICIPANT_NAMES}) == 300
    assert all(
        name.isascii() and name.isalpha() and 1 <= len(name) <= 8 for name in _PARTICIPANT_NAMES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [300, 601])
async def test_names_are_unique_across_formations_and_persist(store, count, monkeypatch):
    import secrets

    shuffles = []

    def shuffle(_random, names):
        shuffles.append(tuple(names))
        names.reverse()

    monkeypatch.setattr(secrets.SystemRandom, "shuffle", shuffle)
    saved = await store.save_profile(
        {
            **_profile(),
            "participants": [
                {"model": "model-a", "count": 150},
                {"model": "model-b", "count": count - 150},
            ],
        },
        expected_revision=None,
    )

    async def start(request_id):
        return await store.create_swarm(
            saved["id"],
            "goal",
            {},
            request_id=request_id,
            expected_profile_revision=saved["revision"],
        )

    started = await start("names")
    swarm_id = started["swarm_id"]
    original = (await store.get_swarm(swarm_id))["participants"]
    names = [p["display_name"] for p in original]
    assert names[:300] == list(reversed(_PARTICIPANT_NAMES))
    assert len(set(names)) == count
    assert all(len(name) <= 8 for name in names)
    assert [p["ordinal"] for p in original] == list(range(1, count + 1))
    assert (await start("names"))["replayed"]
    assert len(shuffles) == 1
    await store.close()
    await store.open()
    assert (await store.get_swarm(swarm_id))["participants"] == original
    await start("another-swarm")
    assert len(shuffles) == 2
