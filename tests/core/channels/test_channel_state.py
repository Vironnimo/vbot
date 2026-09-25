"""Channel state in channels.db: registry, access, routing, bindings, receipts, polling."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

import core.channels.state as state_module
from core.channels import ChannelConfigError, ChannelError, ChannelNotFoundError
from core.channels.adapter import RunButtonBinding
from core.channels.state import ChannelStateStore
from core.database import APPLICATION_IDS, write_bootstrap_marker
from core.runtime.databases import canonical_database_specs
from core.utils.timestamps import utc_now_timestamp

_STATE_TABLES = (
    "channel_admins",
    "channel_participants",
    "channel_conversations",
    "channel_run_buttons",
    "channel_received",
    "channel_polling",
)


def _open(data_dir: Path) -> ChannelStateStore:
    if not (data_dir / "data-store.json").exists():
        write_bootstrap_marker(data_dir)
    return ChannelStateStore.open(data_dir)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ChannelStateStore]:
    state = _open(tmp_path)
    state.reset("tg")
    try:
        yield state
    finally:
        state.close()


def _binding(binding_id: str = "binding", *, thread_id: str | None = None) -> RunButtonBinding:
    return RunButtonBinding(
        id=binding_id,
        platform_target="-100",
        thread_id=thread_id,
        origin_session_id="origin",
        original_button_data=("run:yes", "run:no"),
        created_at=utc_now_timestamp(),
    )


def _row_counts(state: ChannelStateStore, channel_id: str) -> dict[str, int]:
    with state.database.read() as connection:
        return {
            table: int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE channel_id = ?", (channel_id,)
                ).fetchone()[0]
            )
            for table in _STATE_TABLES
        }


async def _fill(state: ChannelStateStore, channel_id: str) -> None:
    await state.snapshot_participant_role(channel_id, "-100", "50", "Alice")
    await state.grant_group_admin(channel_id, "-100", "50")
    await state.record_received(channel_id, "-100:1")
    state.point_conversation(channel_id, f"ch-{channel_id}--100", "group", "ses_next")
    state.save_run_button_binding(channel_id, _binding())
    state.save_update_offset(channel_id, 42)


def test_channels_db_is_a_canonical_database_of_the_data_directory(tmp_path: Path) -> None:
    specs = {spec.name: spec for spec in canonical_database_specs(tmp_path)}

    assert specs["channels"].path == tmp_path / "channels.db"
    assert specs["channels"].application_id == APPLICATION_IDS["channels"] == 0x56424348


@pytest.mark.asyncio
async def test_state_writes_for_an_unregistered_channel_are_refused(
    store: ChannelStateStore,
) -> None:
    refused = [
        lambda: store.snapshot_participant_role("gone", "-100", "50", "Alice"),
        lambda: store.set_self_user_id("gone", "50"),
        lambda: store.grant_group_admin("gone", "-100", "50"),
        lambda: store.revoke_group_admin("gone", "-100", "50"),
        lambda: store.record_received("gone", "-100:1"),
        lambda: store.access_state("gone"),
    ]
    for operation in refused:
        with pytest.raises(ChannelNotFoundError, match="gone"):
            await operation()
    with pytest.raises(ChannelNotFoundError):
        store.point_conversation("gone", "ch-gone-1", "direct", "ses_next")
    with pytest.raises(ChannelNotFoundError):
        store.save_run_button_binding("gone", _binding())
    with pytest.raises(ChannelNotFoundError):
        store.save_update_offset("gone", 7)

    assert _row_counts(store, "gone") == dict.fromkeys(_STATE_TABLES, 0)


@pytest.mark.asyncio
async def test_unregister_deletes_every_row_of_only_that_channel(
    store: ChannelStateStore,
) -> None:
    store.reset("other")
    await _fill(store, "tg")
    await _fill(store, "other")

    store.unregister("tg")

    assert _row_counts(store, "tg") == dict.fromkeys(_STATE_TABLES, 0)
    assert _row_counts(store, "other") == dict.fromkeys(_STATE_TABLES, 1)
    assert store.role_for("tg", "-100", "50") == "member"
    assert store.role_for("other", "-100", "50") == "admin"
    with pytest.raises(ChannelNotFoundError):
        store.save_update_offset("tg", 43)


@pytest.mark.asyncio
async def test_reset_gives_a_reused_channel_id_empty_state(store: ChannelStateStore) -> None:
    await _fill(store, "tg")

    store.reset("tg")

    assert _row_counts(store, "tg") == dict.fromkeys(_STATE_TABLES, 0)
    assert await store.access_state("tg") == {
        "channel_id": "tg",
        "self_user_id": None,
        "groups": [],
    }
    assert store.role_for("tg", "-100", "50") == "member"


@pytest.mark.asyncio
async def test_adopting_configured_channels_keeps_their_state(tmp_path: Path) -> None:
    state = _open(tmp_path)
    try:
        state.reset("tg")
        await _fill(state, "tg")

        state.adopt(["tg", "discord-main"])

        assert _row_counts(state, "tg") == dict.fromkeys(_STATE_TABLES, 1)
        assert state.role_for("tg", "-100", "50") == "admin"
        assert (await state.access_state("discord-main"))["groups"] == []
    finally:
        state.close()


@pytest.mark.asyncio
async def test_group_access_is_durable_and_scoped_per_group(tmp_path: Path) -> None:
    state = _open(tmp_path)
    state.reset("tg")
    assert await state.snapshot_participant_role("tg", "-100", "50", "Alice") == "member"
    assert await state.snapshot_participant_role("tg", "-100", "51", "  Bob  ") == "member"
    assert await state.snapshot_participant_role("tg", "-200", "51", "") == "member"

    await state.set_self_user_id("tg", "50")
    await state.grant_group_admin("tg", "-100", "51")
    await state.revoke_group_admin("tg", "-100", "50")
    assert await state.snapshot_participant_role("tg", "-200", "50", "Alice") == "admin"

    assert state.role_for("tg", "-100", "50") == "admin"
    assert state.role_for("tg", "-100", "51") == "admin"
    assert state.role_for("tg", "-200", "51") == "member"
    state.close()

    reloaded = _open(tmp_path)
    try:
        access = await reloaded.access_state("tg")
        assert access["self_user_id"] == "50"
        groups = {group["access_scope_id"]: group for group in access["groups"]}
        assert groups["-100"]["admin_user_ids"] == ["50", "51"]
        assert groups["-200"]["admin_user_ids"] == ["50"]
        participants = groups["-100"]["participants"]
        assert [(item["user_id"], item["display_name"], item["role"]) for item in participants] == [
            ("50", "Alice", "admin"),
            ("51", "Bob", "admin"),
        ]
        assert [
            (item["user_id"], item["display_name"]) for item in groups["-200"]["participants"]
        ] == [
            ("50", "Alice"),
            ("51", "51"),
        ]
        for item in participants:
            assert datetime.strptime(item["last_seen_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
        assert reloaded.role_for("tg", "-100", "51") == "admin"

        await reloaded.revoke_group_admin("tg", "-100", "51")
        assert reloaded.role_for("tg", "-100", "51") == "member"
    finally:
        reloaded.close()


@pytest.mark.asyncio
async def test_own_identity_must_have_been_seen(store: ChannelStateStore) -> None:
    with pytest.raises(ChannelConfigError, match="has not been seen: 50"):
        await store.set_self_user_id("tg", "50")
    with pytest.raises(ChannelConfigError, match="user_id must be a non-empty string"):
        await store.grant_group_admin("tg", "-100", "  ")


@pytest.mark.asyncio
async def test_role_checks_read_committed_access_without_storage(
    store: ChannelStateStore,
) -> None:
    await store.snapshot_participant_role("tg", "-100", "50", "Alice")
    await store.grant_group_admin("tg", "-100", "51")
    await store.set_self_user_id("tg", "50")

    store.close()

    assert store.role_for("tg", "-100", "51") == "admin"
    assert store.role_for("tg", "-200", "51") == "member"
    assert store.role_for("tg", "-200", "50") == "admin"


@pytest.mark.asyncio
async def test_group_access_migration_merges_groups_and_removes_old_scope(
    store: ChannelStateStore,
) -> None:
    await store.snapshot_participant_role("tg", "-100", "50", "Alice")
    await store.snapshot_participant_role("tg", "-200", "51", "Bob")
    await store.snapshot_participant_role("tg", "-200", "50", "Old Alice")
    await store.snapshot_participant_role("tg", "-100", "50", "Alice")
    await store.grant_group_admin("tg", "-100", "50")
    await store.grant_group_admin("tg", "-200", "51")

    store.migrate_group_access("tg", "-100", "-200")

    access = await store.access_state("tg")
    assert [group["access_scope_id"] for group in access["groups"]] == ["-200"]
    group = access["groups"][0]
    assert group["admin_user_ids"] == ["50", "51"]
    assert [
        (participant["user_id"], participant["display_name"])
        for participant in group["participants"]
    ] == [("50", "Alice"), ("51", "Bob")]
    assert store.role_for("tg", "-200", "50") == "admin"
    assert store.role_for("tg", "-100", "50") == "member"

    store.migrate_group_access("tg", "-100", "-200")
    assert await store.access_state("tg") == access


def test_conversation_pointers_restore_only_their_own_navigation(
    store: ChannelStateStore,
) -> None:
    anchor = "ch-tg--100"
    assert store.active_session_id("tg", anchor) is None

    assert store.point_conversation("tg", anchor, "group", "ses_tap") is None
    store.restore_conversation_pointer("tg", anchor, None, expected_session_id="ses_tap")
    assert store.active_session_id("tg", anchor) is None

    assert store.point_conversation("tg", anchor, "group", "ses_new") is None
    assert store.point_conversation("tg", anchor, "group", "ses_tap") == "ses_new"
    assert store.point_conversation("tg", anchor, "group", "ses_later") == "ses_tap"
    store.restore_conversation_pointer("tg", anchor, "ses_new", expected_session_id="ses_tap")
    assert store.active_session_id("tg", anchor) == "ses_later"

    with pytest.raises(Exception, match="conversation kind"):
        store.point_conversation("tg", anchor, "channel", "ses_x")


def test_run_button_claims_are_atomic_and_consumed_rows_block_replays(
    tmp_path: Path,
) -> None:
    state = _open(tmp_path)
    state.reset("tg")
    state.save_run_button_binding("tg", _binding())
    barrier = threading.Barrier(8)
    statuses: list[str] = []
    lock = threading.Lock()

    def claim() -> None:
        barrier.wait()
        result = state.claim_run_button_binding(
            "tg", "binding", platform_target="-100", thread_id=None
        )
        with lock:
            statuses.append(result.status)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    state.close()

    assert sorted(statuses) == ["claimed", *["consumed"] * 7]
    reopened = _open(tmp_path)
    try:
        replay = reopened.claim_run_button_binding(
            "tg", "binding", platform_target="-100", thread_id=None
        )
        assert replay.status == "consumed"
        assert replay.binding is not None and replay.binding.consumed
        assert replay.binding.original_button_data == ("run:yes", "run:no")
    finally:
        reopened.close()


def test_run_button_claims_check_target_and_thread_and_can_be_restored(
    store: ChannelStateStore,
) -> None:
    store.save_run_button_binding("tg", _binding(thread_id="7"))

    def claim(target: str, thread_id: str | None) -> str:
        return store.claim_run_button_binding(
            "tg", "binding", platform_target=target, thread_id=thread_id
        ).status

    assert claim("-200", "7") == "target_mismatch"
    assert claim("-100", None) == "target_mismatch"
    assert (
        store.claim_run_button_binding(
            "other", "binding", platform_target="-100", thread_id="7"
        ).status
        == "missing"
    )
    assert claim("-100", "7") == "claimed"
    assert claim("-100", "7") == "consumed"

    store.restore_run_button_binding("tg", "binding")
    assert claim("-100", "7") == "claimed"

    store.discard_run_button_binding("tg", "binding")
    assert claim("-100", "7") == "missing"


@pytest.mark.asyncio
async def test_receipts_keep_a_fifo_window_per_channel(
    store: ChannelStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert state_module.RECEIVED_MESSAGE_WINDOW == 4096
    monkeypatch.setattr(state_module, "RECEIVED_MESSAGE_WINDOW", 3)
    store.reset("other")
    await store.record_received("other", "keep")

    for index in range(5):
        await store.record_received("tg", f"-100:{index}")
    await store.record_received("tg", "-100:4")

    assert [await store.has_received("tg", f"-100:{index}") for index in range(5)] == [
        False,
        False,
        True,
        True,
        True,
    ]
    assert await store.has_received("other", "keep")
    assert not await store.has_received("other", "-100:4")


def test_polling_watermark_is_monotonic_while_fresh(store: ChannelStateStore) -> None:
    assert store.load_update_offset("tg") == 0

    store.save_update_offset("tg", 100)
    store.save_update_offset("tg", 90)
    store.save_update_offset("tg", 100)
    assert store.load_update_offset("tg") == 100
    with pytest.raises(ChannelError, match="non-negative integer"):
        store.save_update_offset("tg", -1)


def test_async_access_runs_off_the_calling_thread(store: ChannelStateStore) -> None:
    caller = threading.get_ident()
    seen: list[int] = []
    original = store.database.write

    def spy(operation, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(threading.get_ident())
        return original(operation, **kwargs)

    store.database.write = spy  # type: ignore[method-assign]
    try:
        role = asyncio.run(store.snapshot_participant_role("tg", "-100", "50", "Alice"))
    finally:
        del store.database.write

    assert role == "member"
    assert seen and caller not in seen
