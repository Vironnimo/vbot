from __future__ import annotations

import asyncio

# mypy: disable-error-code=arg-type
import pytest
import pytest_asyncio

from core.runs.runs import RunExecutionOwner
from core.sessions import DeliveryReceipt, OwnedRunRecord, SessionAddress, TemporarySessionBinding
from resources.extensions.swarm.store import SwarmStore, SwarmStoreError


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
        {"reminders": {"delivery": True, "wake": True, "resume": 1, "completion": True}},
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


async def _reserve_done(store: SwarmStore, swarm_id: str, participant_id: str, run_id: str) -> None:
    await store.bind_execution_epoch(swarm_id, expected_epoch=0, execution_epoch="host-epoch")
    await store.request_done(
        swarm_id,
        participant_id,
        run_id=run_id,
        expected_epoch=0,
        call_id="done-call",
        summary="done",
    )
    result = await store.reconcile_tool_batch(
        swarm_id, participant_id, run_id=run_id, expected_epoch=0, persisted_call_ids=("done-call",)
    )
    assert result["state"] == "finishing"


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
async def test_swarm_snapshots_profile_and_has_deterministic_roster(store: SwarmStore) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    assert swarm["state"] == "preparing"
    assert [item["display_name"] for item in swarm["participants"]] == [
        "Participant 1",
        "Participant 2",
    ]
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
    with pytest.raises(SwarmStoreError, match="invalid_cursor"):
        await store.read_posts(started["swarm_id"], first, cursor=first_page.cursor + "x")
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
    if mode == "pull":
        assert result["entries"] == [] and result["pull_reminder"] is True
    elif mode == "idle" and state == "running":
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
async def test_lifecycle_done_requires_matching_completed_owned_run(tmp_path) -> None:
    record: OwnedRunRecord | None = None

    async def lookup(_swarm_id: str, _run_id: str) -> OwnedRunRecord | None:
        return record

    store = SwarmStore(tmp_path / "life.db", lookup_terminal_proof=lookup)
    await store.open()
    try:
        started = await _swarm(store)
        swarm = await store.get_swarm(started["swarm_id"])
        participant = swarm["participants"][0]["id"]
        address = SessionAddress(None, "tmp", "ses")
        binding = TemporarySessionBinding(
            address, "gen", "swarm", started["swarm_id"], participant, {}
        )
        await store.bind_participant_session(binding)
        await _reserve_done(store, started["swarm_id"], participant, "run")
        owner = RunExecutionOwner("swarm", started["swarm_id"], participant, "gen", "host-epoch")
        record = OwnedRunRecord(1, address, "gen", "run", owner, 2, "completed", 2)
        assert (
            await store.finalize_participant(
                started["swarm_id"], participant, run_id="run", expected_epoch=0
            )
        )["state"] == "done"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_post_committed_after_finish_request_blocks_finalization(tmp_path) -> None:
    record: OwnedRunRecord | None = None
    store: SwarmStore

    async def lookup(_swarm_id: str, _run_id: str) -> OwnedRunRecord | None:
        # This callback is the deterministic boundary between the persisted
        # finish intent and Store finalization.
        await store.post(started["swarm_id"], sender, text="late", request_id="late-post")
        return record

    store = SwarmStore(tmp_path / "finish-race.db", lookup_terminal_proof=lookup)
    await store.open()
    try:
        started = await _swarm(store)
        swarm = await store.get_swarm(started["swarm_id"])
        sender, participant = [item["id"] for item in swarm["participants"]]
        address = SessionAddress(None, "tmp", "ses_finish_race")
        await store.bind_participant_session(
            TemporarySessionBinding(address, "gen", "swarm", started["swarm_id"], participant, {})
        )
        await _reserve_done(store, started["swarm_id"], participant, "run")
        record = OwnedRunRecord(
            1,
            address,
            "gen",
            "run",
            RunExecutionOwner("swarm", started["swarm_id"], participant, "gen", "host-epoch"),
            1,
            "completed",
            2,
        )

        result = await store.finalize_participant(
            started["swarm_id"], participant, run_id="run", expected_epoch=0
        )

        assert result == {"participant_id": participant, "state": "done"}
        assert (await store.get_swarm(started["swarm_id"]))["participants"][1]["state"] == "done"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_post_after_committed_done_is_retained_with_inactive_recipient(tmp_path) -> None:
    record: OwnedRunRecord | None = None

    async def lookup(_swarm_id: str, _run_id: str) -> OwnedRunRecord | None:
        return record

    store = SwarmStore(tmp_path / "done-post.db", lookup_terminal_proof=lookup)
    await store.open()
    try:
        started = await _swarm(store)
        swarm = await store.get_swarm(started["swarm_id"])
        sender, participant = [item["id"] for item in swarm["participants"]]
        address = SessionAddress(None, "tmp", "ses_done_post")
        await store.bind_participant_session(
            TemporarySessionBinding(address, "gen", "swarm", started["swarm_id"], participant, {})
        )
        await _reserve_done(store, started["swarm_id"], participant, "run")
        record = OwnedRunRecord(
            1,
            address,
            "gen",
            "run",
            RunExecutionOwner("swarm", started["swarm_id"], participant, "gen", "host-epoch"),
            1,
            "completed",
            2,
        )
        assert (
            await store.finalize_participant(
                started["swarm_id"], participant, run_id="run", expected_epoch=0
            )
        )["state"] == "done"

        posted = await store.post(
            started["swarm_id"], sender, text="after", request_id="after-done"
        )

        assert posted["inactive_recipients"] == [participant]
        assert (await store.read_posts(started["swarm_id"], sender)).entries[-1]["text"] == "after"
        assert (await store.prepare_inbox_delivery(started["swarm_id"], participant))[
            "entries"
        ] == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_done_reservation_requires_persisted_call_and_rejects_post_race(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    sender, participant = [item["id"] for item in swarm["participants"]]
    await store.bind_execution_epoch(
        started["swarm_id"], expected_epoch=0, execution_epoch="opaque"
    )
    requested = await store.request_done(
        started["swarm_id"],
        participant,
        run_id="run",
        expected_epoch=0,
        call_id="call",
        summary="done",
    )
    assert requested["status"] == "finish_requested"
    assert (await store.get_swarm(started["swarm_id"]))["participants"][1]["state"] == "prepared"
    await store.post(started["swarm_id"], sender, text="late", request_id="late")
    result = await store.reconcile_tool_batch(
        started["swarm_id"],
        participant,
        run_id="run",
        expected_epoch=0,
        persisted_call_ids=("call",),
    )
    assert result["continuation_required"] is True
    assert result["end_run"] is False


@pytest.mark.asyncio
async def test_lifecycle_rejects_wrong_or_failed_terminal_proof(tmp_path) -> None:
    async def lookup(_swarm_id: str, _run_id: str) -> OwnedRunRecord | None:
        return OwnedRunRecord(
            1,
            SessionAddress(None, "wrong", "ses"),
            "bad",
            "run",
            RunExecutionOwner("swarm", "bad", "bad", "bad", "0"),
            1,
            "failed",
            1,
        )

    store = SwarmStore(tmp_path / "bad.db", lookup_terminal_proof=lookup)
    await store.open()
    try:
        started = await _swarm(store)
        swarm = await store.get_swarm(started["swarm_id"])
        participant = swarm["participants"][0]["id"]
        await store.bind_participant_session(
            TemporarySessionBinding(
                SessionAddress(None, "tmp", "ses"),
                "gen",
                "swarm",
                started["swarm_id"],
                participant,
                {},
            )
        )
        await _reserve_done(store, started["swarm_id"], participant, "run")
        with pytest.raises(SwarmStoreError, match="terminal_proof_missing"):
            await store.finalize_participant(
                started["swarm_id"], participant, run_id="run", expected_epoch=0
            )
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
async def test_lifecycle_rejects_matching_noncompleted_terminal_proof(
    tmp_path, terminal_status: str
) -> None:
    record: OwnedRunRecord | None = None

    async def lookup(_swarm_id: str, _run_id: str) -> OwnedRunRecord | None:
        return record

    store = SwarmStore(tmp_path / f"{terminal_status}.db", lookup_terminal_proof=lookup)
    await store.open()
    try:
        started = await _swarm(store)
        participant = (await store.get_swarm(started["swarm_id"]))["participants"][0]["id"]
        address = SessionAddress(None, "tmp", f"ses_{terminal_status}")
        await store.bind_participant_session(
            TemporarySessionBinding(address, "gen", "swarm", started["swarm_id"], participant, {})
        )
        await _reserve_done(store, started["swarm_id"], participant, "run")
        record = OwnedRunRecord(
            1,
            address,
            "gen",
            "run",
            RunExecutionOwner("swarm", started["swarm_id"], participant, "gen", "host-epoch"),
            1,
            terminal_status,
            2,
        )

        assert (
            await store.finalize_participant(
                started["swarm_id"], participant, run_id="run", expected_epoch=0
            )
        )["state"] == terminal_status
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_wait_and_needs_user_remain_distinct(store: SwarmStore) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    participant = swarm["participants"][0]["id"]
    assert (
        await store.request_wait(
            started["swarm_id"], participant, run_id="wait", expected_epoch=0, call_id="wait"
        )
    )["state"] == "waiting"
    assert (
        await store.request_wait(
            started["swarm_id"],
            participant,
            run_id="block",
            expected_epoch=0,
            call_id="block",
            needs_user=True,
        )
    )["state"] == "blocked"


@pytest.mark.asyncio
async def test_wait_can_wake_but_needs_user_blocks_automatic_delivery(store: SwarmStore) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    sender, participant = [item["id"] for item in swarm["participants"]]
    await store.request_wait(
        started["swarm_id"], participant, run_id="wait", expected_epoch=0, call_id="wait"
    )
    await store.reconcile_tool_batch(
        started["swarm_id"],
        participant,
        run_id="wait",
        expected_epoch=0,
        persisted_call_ids=("wait",),
    )
    await store.post(started["swarm_id"], sender, text="wake", request_id="wait-wake")
    waiting = await store.prepare_automatic_delivery(
        started["swarm_id"], participant, expected_epoch=0, admission_boundary=1
    )
    assert waiting["wake"] is True and len(waiting["entries"]) == 1
    await store.request_wait(
        started["swarm_id"],
        participant,
        run_id="block",
        expected_epoch=0,
        call_id="block",
        needs_user=True,
    )
    await store.reconcile_tool_batch(
        started["swarm_id"],
        participant,
        run_id="block",
        expected_epoch=0,
        persisted_call_ids=("block",),
    )
    await store.post(started["swarm_id"], sender, text="hold", request_id="blocked-hold")
    blocked = await store.prepare_automatic_delivery(
        started["swarm_id"], participant, expected_epoch=0, admission_boundary=2
    )
    assert blocked == {"entries": [], "wake": False, "pending_remaining": 0}


@pytest.mark.asyncio
async def test_done_is_rejected_while_owned_descendant_is_active(tmp_path) -> None:
    async def descendants(*_args: object) -> bool:
        return True

    store = SwarmStore(tmp_path / "desc.db", has_owned_descendants=descendants)
    await store.open()
    try:
        started = await _swarm(store)
        participant = (await store.get_swarm(started["swarm_id"]))["participants"][0]["id"]
        assert (
            await store.request_done(
                started["swarm_id"],
                participant,
                run_id="run",
                expected_epoch=0,
                call_id="done",
                summary="done",
            )
        )["owned_work_active"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_descendant_started_after_finish_intent_blocks_finalization(tmp_path) -> None:
    descendants_active = False
    record: OwnedRunRecord | None = None

    async def descendants(*_args: object) -> bool:
        return descendants_active

    async def lookup(_swarm_id: str, _run_id: str) -> OwnedRunRecord | None:
        return record

    store = SwarmStore(
        tmp_path / "late-descendant.db",
        lookup_terminal_proof=lookup,
        has_owned_descendants=descendants,
    )
    await store.open()
    try:
        started = await _swarm(store)
        participant = (await store.get_swarm(started["swarm_id"]))["participants"][0]["id"]
        address = SessionAddress(None, "tmp", "ses_late_descendant")
        await store.bind_participant_session(
            TemporarySessionBinding(address, "gen", "swarm", started["swarm_id"], participant, {})
        )
        await _reserve_done(store, started["swarm_id"], participant, "run")
        descendants_active = True
        record = OwnedRunRecord(
            1,
            address,
            "gen",
            "run",
            RunExecutionOwner("swarm", started["swarm_id"], participant, "gen", "host-epoch"),
            1,
            "completed",
            2,
        )

        result = await store.finalize_participant(
            started["swarm_id"], participant, run_id="run", expected_epoch=0
        )
        assert result["state"] == "finishing" and result["owned_work_active"] is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stale_epoch_finalize_is_rejected(tmp_path) -> None:
    async def lookup(_swarm_id: str, _run: str) -> OwnedRunRecord | None:
        return None

    store = SwarmStore(tmp_path / "stale-final.db", lookup_terminal_proof=lookup)
    await store.open()
    try:
        started = await _swarm(store)
        participant = (await store.get_swarm(started["swarm_id"]))["participants"][0]["id"]
        await store.request_done(
            started["swarm_id"],
            participant,
            run_id="run",
            expected_epoch=0,
            call_id="done",
            summary="done",
        )
        await store.begin_stop(started["swarm_id"], request_id="stop", actor="test")
        await store.finish_stop(
            started["swarm_id"],
            request_id="stop-finish",
            actor="test",
            drain_report={"drained": True},
        )
        await store.begin_resume(started["swarm_id"], request_id="resume", actor="test")
        with pytest.raises(SwarmStoreError, match="stale_epoch"):
            await store.finalize_participant(
                started["swarm_id"], participant, run_id="run", expected_epoch=0
            )
    finally:
        await store.close()


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
        assert all(item["state"] == "interrupted" for item in snapshot["participants"])
        assert await recovered.recover_interrupted() == []
        with pytest.raises(SwarmStoreError, match="swarm_closed"):
            await recovered.prepare_automatic_delivery(
                started["swarm_id"], participant, expected_epoch=0
            )
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_stop_resume_intents_are_idempotent_and_skip_done_participants(tmp_path) -> None:
    record: OwnedRunRecord | None = None

    async def lookup(_swarm_id: str, _run_id: str) -> OwnedRunRecord | None:
        return record

    store = SwarmStore(tmp_path / "resume.db", lookup_terminal_proof=lookup)
    await store.open()
    try:
        started = await _swarm(store)
        swarm = await store.get_swarm(started["swarm_id"])
        done_participant, unfinished = [item["id"] for item in swarm["participants"]]
        address = SessionAddress(None, "tmp", "ses_resume")
        await store.bind_participant_session(
            TemporarySessionBinding(
                address, "gen", "swarm", started["swarm_id"], done_participant, {}
            )
        )
        await _reserve_done(store, started["swarm_id"], done_participant, "run")
        record = OwnedRunRecord(
            1,
            address,
            "gen",
            "run",
            RunExecutionOwner("swarm", started["swarm_id"], done_participant, "gen", "host-epoch"),
            1,
            "completed",
            2,
        )
        assert (
            await store.finalize_participant(
                started["swarm_id"], done_participant, run_id="run", expected_epoch=0
            )
        )["state"] == "done"
        stop = await store.begin_stop(started["swarm_id"], request_id="stop", actor="user")
        assert (await store.begin_stop(started["swarm_id"], request_id="stop", actor="user"))[
            "replayed"
        ]
        assert stop["state"] == "stopping"
        await store.finish_stop(
            started["swarm_id"], request_id="finish", actor="user", drain_report={"drained": True}
        )
        resumed = await store.begin_resume(started["swarm_id"], request_id="resume", actor="user")

        assert resumed["participant_ids"] == [unfinished]
        assert resumed["epoch"] == 1
        assert (await store.begin_resume(started["swarm_id"], request_id="resume", actor="user"))[
            "replayed"
        ]
        states = {
            item["id"]: item["state"]
            for item in (await store.get_swarm(started["swarm_id"]))["participants"]
        }
        assert states == {done_participant: "done", unfinished: "prepared"}
    finally:
        await store.close()


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
async def test_status_rename_and_exact_run_finish(store: SwarmStore) -> None:
    started = await _swarm(store)
    participant = (await store.get_swarm(started["swarm_id"]))["participants"][0]["id"]
    assert (await store.rename_participant(started["swarm_id"], participant, " Reviewer "))[
        "name"
    ] == "Reviewer"
    status = await store.participant_status(started["swarm_id"], participant, limit=1)
    assert status["self"]["name"] == "Reviewer"
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
async def test_failed_or_cancelled_terminal_overrides_persisted_wait(store: SwarmStore) -> None:
    started = await _swarm(store)
    participants = [
        item["id"] for item in (await store.get_swarm(started["swarm_id"]))["participants"]
    ]
    for participant, outcome in zip(participants, ("failed", "cancelled"), strict=True):
        run_id = f"wait-{outcome}"
        await store.request_wait(
            started["swarm_id"], participant, run_id=run_id, expected_epoch=0, call_id=run_id
        )
        batch = await store.reconcile_tool_batch(
            started["swarm_id"],
            participant,
            run_id=run_id,
            expected_epoch=0,
            persisted_call_ids=(run_id,),
        )
        assert batch["state"] == "waiting"
        finished = await store.reconcile_run_finished(
            started["swarm_id"], participant, run_id=run_id, expected_epoch=0, outcome=outcome
        )
        assert finished["state"] == outcome


@pytest.mark.asyncio
async def test_finish_group_requires_clean_drain_and_every_participant_done(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    swarm_id = started["swarm_id"]
    with pytest.raises(SwarmStoreError, match="owned_work_active"):
        await store.finish_group(
            swarm_id, expected_epoch=0, drain_report={"closed": True, "run_ids": []}
        )

    connection = store._connection  # noqa: SLF001 - exact completion transaction fixture
    assert connection is not None
    connection.execute("UPDATE participants SET state='done' WHERE swarm_id=?", (swarm_id,))
    connection.execute(
        "UPDATE participants SET wake_pending=1 WHERE id=("
        "SELECT id FROM participants WHERE swarm_id=? LIMIT 1)",
        (swarm_id,),
    )
    with pytest.raises(SwarmStoreError, match="owned_work_active"):
        await store.finish_group(
            swarm_id, expected_epoch=0, drain_report={"closed": True, "run_ids": []}
        )
    connection.execute("UPDATE participants SET wake_pending=0 WHERE swarm_id=?", (swarm_id,))
    with pytest.raises(SwarmStoreError, match="owned_work_active"):
        await store.finish_group(
            swarm_id, expected_epoch=0, drain_report={"closed": True, "run_ids": ["active-run"]}
        )

    completed = await store.finish_group(
        swarm_id, expected_epoch=0, drain_report={"closed": True, "run_ids": []}
    )
    assert completed["state"] == "completed"
    assert (await store.get_swarm(swarm_id))["state"] == "completed"
    assert (
        await store.finish_group(
            swarm_id, expected_epoch=0, drain_report={"closed": True, "run_ids": []}
        )
    )["replayed"]


@pytest.mark.asyncio
async def test_stale_rename_and_foreign_ping_have_exact_codes(store: SwarmStore) -> None:
    started = await _swarm(store)
    sender = (await store.get_swarm(started["swarm_id"]))["participants"][0]["id"]
    await store.begin_stop(started["swarm_id"], request_id="stop-r", actor="test")
    await store.finish_stop(
        started["swarm_id"], request_id="finish-r", actor="test", drain_report={}
    )
    await store.begin_resume(started["swarm_id"], request_id="resume-r", actor="test")
    with pytest.raises(SwarmStoreError, match="stale_epoch"):
        await store.rename_participant(started["swarm_id"], sender, "Later", expected_epoch=0)
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
async def test_status_keeps_whole_16k_summaries_and_pages_at_delivery_budget(
    store: SwarmStore,
) -> None:
    started = await _swarm(store)
    participants = (await store.get_swarm(started["swarm_id"]))["participants"]
    swarm = await store.get_swarm(started["swarm_id"])
    await store.apply_delivery_settings(
        started["swarm_id"],
        {**swarm["delivery"], "batch_chars": 16_000},
        expected_revision=1,
        request_id="budget",
        actor="test",
    )
    connection = store._connection  # noqa: SLF001 - exact bounded read-model fixture
    assert connection is not None
    connection.execute(
        "UPDATE participants SET summary_json=? WHERE id=?",
        ('{"summary":"' + "x" * 16_000 + '"}', participants[0]["id"]),
    )
    connection.execute(
        "UPDATE participants SET summary_json=? WHERE id=?",
        ('{"summary":"next"}', participants[1]["id"]),
    )
    first = await store.participant_status(started["swarm_id"], participants[0]["id"], limit=20)
    assert [len(item["summary"] or "") for item in first["roster"]] == [16_000]
    assert first["has_more"] and first["cursor"]
    second = await store.participant_status(
        started["swarm_id"], participants[0]["id"], cursor=first["cursor"], limit=20
    )
    assert [item["summary"] for item in second["roster"]] == ["next"]
    assert first["self"]["summary"] is None and first["self"]["summary_available"]


@pytest.mark.asyncio
async def test_rename_rejects_reserved_collision_inactive_and_foreign(store: SwarmStore) -> None:
    started = await _swarm(store)
    first, second = [
        item["id"] for item in (await store.get_swarm(started["swarm_id"]))["participants"]
    ]
    assert (await store.rename_participant(started["swarm_id"], first, "Same"))["name"] == "Same"
    assert (await store.rename_participant(started["swarm_id"], first, " Same "))["name"] == "Same"
    for name in ("same", "System", "User"):
        with pytest.raises(SwarmStoreError, match="name_unavailable"):
            await store.rename_participant(started["swarm_id"], second, name)
    with pytest.raises(SwarmStoreError, match="participant_not_found"):
        await store.rename_participant(started["swarm_id"], "foreign", "Other")
    await store.set_participant_state(started["swarm_id"], first, "cancelled")
    with pytest.raises(SwarmStoreError, match="swarm_closed|participant_inactive"):
        await store.rename_participant(started["swarm_id"], first, "Later")


@pytest.mark.asyncio
async def test_status_cursor_and_board_epoch_inactive_guards(store: SwarmStore) -> None:
    started = await _swarm(store, count=2)
    swarm = await store.get_swarm(started["swarm_id"])
    first, second = [item["id"] for item in swarm["participants"]]
    page = await store.participant_status(started["swarm_id"], first, limit=1)
    assert page["self"]["id"] == first and page["has_more"] and page["cursor"]
    with pytest.raises(SwarmStoreError, match="invalid_cursor"):
        await store.participant_status(started["swarm_id"], second, cursor=page["cursor"], limit=1)
    await store.set_participant_state(started["swarm_id"], first, "blocked")
    with pytest.raises(SwarmStoreError, match="participant_inactive"):
        await store.create_discussion(
            started["swarm_id"], first, title="No", text="No", request_id="inactive"
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
    await store.rename_participant(started["swarm_id"], first, "Renamed")
    assert (await store.read_posts(started["swarm_id"], second)).entries[0]["author"][
        "name"
    ] == "Participant 1"
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
    await store.set_participant_state(swarm_id, first, "waiting")
    await store.set_participant_state(swarm_id, second, "idle")
    assert (await store.get_swarm(swarm_id))["state"] == "waiting"
    await store.set_participant_state(swarm_id, second, "blocked")
    assert (await store.get_swarm(swarm_id))["state"] == "needs_attention"


@pytest.mark.asyncio
async def test_open_epoch_resume_excludes_busy_participant(store: SwarmStore) -> None:
    started = await _swarm(store, count=2)
    swarm_id = started["swarm_id"]
    waiting, busy = [item["id"] for item in (await store.get_swarm(swarm_id))["participants"]]
    await store.set_swarm_state(swarm_id, "running")
    await store.set_participant_state(swarm_id, waiting, "waiting")
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
