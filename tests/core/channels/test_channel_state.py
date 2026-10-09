"""Channel state in channels.db: registry, access, routing, bindings, receipts, owed
replies, polling."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import core.channels._state_schema as schema_module
import core.channels.state as state_module
from core.channels import ChannelConfigError, ChannelError, ChannelNotFoundError
from core.channels.adapter import PendingReply, ReplyPlanFacts, RouteFacts, RunButtonBinding
from core.channels.state import ChannelStateStore
from core.database import APPLICATION_IDS, DatabaseUnavailableError, write_bootstrap_marker
from core.runtime.databases import canonical_database_specs
from core.utils.timestamps import format_canonical_timestamp, utc_now_timestamp

_STATE_TABLES = (
    "channel_admins",
    "channel_participants",
    "channel_conversations",
    "channel_run_buttons",
    "channel_received",
    "channel_pending_replies",
    "channel_polling",
)

_BOT = 7001
_OTHER_BOT = 7002


def _open(data_dir: Path) -> ChannelStateStore:
    if not (data_dir / "data-store.json").exists():
        write_bootstrap_marker(data_dir)
    return ChannelStateStore.open(data_dir)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ChannelStateStore]:
    state = _open(tmp_path)
    state.reset("tg", "telegram")
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


def _owed(
    reply_id: str,
    owner: str,
    *,
    channel_id: str = "tg",
    created_at: str | None = None,
    run_id: str | None = None,
) -> PendingReply:
    return PendingReply(
        id=reply_id,
        reply_plan=ReplyPlanFacts(
            channel_id=channel_id,
            platform_target="-100",
            reply_to_message_id="9",
            thread_id="7",
        ),
        owner=owner,
        created_at=created_at or utc_now_timestamp(),
        route=None if run_id is None else RouteFacts(agent_id="assistant", session_id="ses"),
        run_id=run_id,
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
    state.save_update_offset(channel_id, _BOT, 42)
    state.owe_reply(channel_id, _owed("owed", "engine", channel_id=channel_id))


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
        store.save_update_offset("gone", _BOT, 7)
    with pytest.raises(ChannelNotFoundError):
        store.owe_reply("gone", _owed("owed", "engine", channel_id="gone"))

    assert _row_counts(store, "gone") == dict.fromkeys(_STATE_TABLES, 0)


@pytest.mark.asyncio
async def test_unregister_deletes_every_row_of_only_that_channel(
    store: ChannelStateStore,
) -> None:
    store.reset("other", "telegram")
    await _fill(store, "tg")
    await _fill(store, "other")

    store.unregister("tg")

    assert _row_counts(store, "tg") == dict.fromkeys(_STATE_TABLES, 0)
    assert _row_counts(store, "other") == dict.fromkeys(_STATE_TABLES, 1)
    assert store.role_for("tg", "-100", "50") == "member"
    assert store.role_for("other", "-100", "50") == "admin"
    with pytest.raises(ChannelNotFoundError):
        store.save_update_offset("tg", _BOT, 43)


@pytest.mark.asyncio
async def test_reset_gives_a_reused_channel_id_empty_state(store: ChannelStateStore) -> None:
    await _fill(store, "tg")

    store.reset("tg", "telegram")

    assert _row_counts(store, "tg") == dict.fromkeys(_STATE_TABLES, 0)
    assert await store.access_state("tg") == {
        "channel_id": "tg",
        "self_user_id": None,
        "groups": [],
    }
    assert store.role_for("tg", "-100", "50") == "member"


def _platforms(state: ChannelStateStore) -> dict[str, str | None]:
    with state.database.read() as connection:
        return dict(connection.execute("SELECT channel_id, platform FROM channels").fetchall())


@pytest.mark.asyncio
async def test_a_platform_change_resets_state_but_keeps_the_main_conversation(
    store: ChannelStateStore,
) -> None:
    store.reset("other", "telegram")
    for channel_id in ("tg", "other"):
        await _fill(store, channel_id)
        await store.set_self_user_id(channel_id, "50")
        store.point_conversation(channel_id, f"ch-{channel_id}-main", "direct", "ses_main")

    assert store.bind_platform("tg", "telegram") is False
    assert _row_counts(store, "tg")["channel_conversations"] == 2

    assert store.bind_platform("tg", "discord") is True

    counts = _row_counts(store, "tg")
    assert counts == {**dict.fromkeys(_STATE_TABLES, 0), "channel_conversations": 1}
    assert store.active_session_id("tg", "ch-tg-main") == "ses_main"
    assert store.active_session_id("tg", "ch-tg--100") is None
    assert await store.access_state("tg") == {
        "channel_id": "tg",
        "self_user_id": None,
        "groups": [],
    }
    assert store.role_for("tg", "-100", "50") == "member"
    assert store.load_update_offset("tg", _BOT) == 0
    assert _platforms(store) == {"tg": "discord", "other": "telegram"}
    # Another Channel keeps its state and roles.
    assert _row_counts(store, "other") == {
        **dict.fromkeys(_STATE_TABLES, 1),
        "channel_conversations": 2,
    }
    assert store.role_for("other", "-100", "50") == "admin"
    with pytest.raises(ChannelNotFoundError):
        store.bind_platform("gone", "discord")


@pytest.mark.asyncio
async def test_adopt_records_platforms_and_resets_state_recorded_for_another(
    tmp_path: Path,
) -> None:
    state = _open(tmp_path)
    try:
        state.reset("tg", "telegram")
        state.adopt({"legacy": None})
        for channel_id in ("tg", "legacy"):
            await _fill(state, channel_id)
            state.point_conversation(channel_id, f"ch-{channel_id}-main", "direct", "ses_main")

        # Configured Channels on their recorded platform keep their state; a new
        # one is registered with empty state.
        assert state.adopt({"tg": "telegram", "legacy": None, "discord-main": "discord"}) == []
        assert _row_counts(state, "tg") == {
            **dict.fromkeys(_STATE_TABLES, 1),
            "channel_conversations": 2,
        }
        assert (await state.access_state("discord-main"))["groups"] == []

        # An unreadable config registers its Channel and keeps what was recorded.
        assert state.adopt({"tg": None, "legacy": None, "new": None}) == []
        assert _platforms(state) == {
            "tg": "telegram",
            "legacy": None,
            "discord-main": "discord",
            "new": None,
        }

        # A first recorded platform keeps the state; another platform resets it.
        assert state.adopt({"tg": "slack", "legacy": "slack", "new": "discord"}) == ["tg"]

        assert _platforms(state) == {
            "tg": "slack",
            "legacy": "slack",
            "discord-main": "discord",
            "new": "discord",
        }
        assert _row_counts(state, "tg") == {
            **dict.fromkeys(_STATE_TABLES, 0),
            "channel_conversations": 1,
        }
        assert state.active_session_id("tg", "ch-tg-main") == "ses_main"
        assert state.role_for("tg", "-100", "50") == "member"
        assert _row_counts(state, "legacy") == {
            **dict.fromkeys(_STATE_TABLES, 1),
            "channel_conversations": 2,
        }
        assert state.role_for("legacy", "-100", "50") == "admin"
    finally:
        state.close()


@pytest.mark.asyncio
async def test_group_access_is_durable_and_scoped_per_group(tmp_path: Path) -> None:
    state = _open(tmp_path)
    state.reset("tg", "telegram")
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
async def test_async_state_work_runs_on_the_channel_pool_until_closed(tmp_path: Path) -> None:
    state = _open(tmp_path)
    state.reset("tg", "telegram")
    caller = threading.get_ident()
    writers: list[int] = []
    original_write = state.database.write

    def spy(operation, **kwargs):  # type: ignore[no-untyped-def]
        writers.append(threading.get_ident())
        return original_write(operation, **kwargs)

    def point() -> str:
        state.point_conversation("tg", "anchor", "direct", "active")
        return threading.current_thread().name

    state.database.write = spy  # type: ignore[method-assign]
    try:
        assert await state.snapshot_participant_role("tg", "-100", "50", "Alice") == "member"
    finally:
        del state.database.write
    assert writers and caller not in writers
    assert (await state.run_async(point)).startswith("vbot-db-channels")
    assert state.active_session_id("tg", "anchor") == "active"
    state.close()

    with pytest.raises(DatabaseUnavailableError):
        await state.run_async(point)


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
    state.reset("tg", "telegram")
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

    assert store.restore_run_button_binding("tg", "binding") is True
    assert store.restore_run_button_binding("tg", "binding") is True
    assert claim("-100", "7") == "claimed"

    store.discard_run_button_binding("tg", "binding")
    assert claim("-100", "7") == "missing"
    assert store.restore_run_button_binding("tg", "binding") is False


def test_owed_replies_are_sent_at_most_once_and_handed_on_by_owner(
    store: ChannelStateStore,
) -> None:
    old = format_canonical_timestamp(
        datetime.now(UTC) - timedelta(seconds=state_module.PENDING_REPLY_RETENTION_SECONDS + 60)
    )
    earlier = format_canonical_timestamp(datetime.now(UTC) - timedelta(minutes=5))
    store.owe_reply("tg", _owed("mine", "current"))
    store.owe_reply("tg", _owed("expired", "ended", created_at=old))
    store.owe_reply("tg", _owed("in-flight", "ended"))
    store.owe_reply("tg", _owed("answer", "ended", created_at=earlier))
    keyboard = {
        "buttons": [[{"label": "Retry", "data": "run:done", "future_button_field": 1}]],
        "future_keyboard_field": {"keep": True},
    }
    store.owe_reply("tg", replace(_owed("notice", "ended"), retry_keyboard=keyboard))
    # A Run's answer takes over the record of the work it answers.
    store.owe_reply("tg", _owed("answer", "ended", run_id="run-1"))

    # A claim succeeds once; a released claim can be taken again.
    assert store.claim_reply("tg", "in-flight") is True
    assert store.claim_reply("tg", "in-flight") is False
    assert store.claim_reply("tg", "notice") is True
    store.release_reply("tg", "notice")

    replies, dropped = store.take_pending_replies("tg", "current")

    # The current engine's own replies stay with it; a reply left mid-send or owed
    # too long is dropped, never sent.
    assert dropped == 2
    assert [(reply.id, reply.run_id) for reply in replies] == [
        ("answer", "run-1"),
        ("notice", None),
    ]
    answer = replies[0]
    assert answer.retry_keyboard is None
    assert replies[1].retry_keyboard == keyboard
    store.owe_reply("tg", replies[1])
    assert store.take_pending_replies("tg", "current")[0][1].retry_keyboard == keyboard
    assert answer.created_at == earlier
    assert answer.route == RouteFacts(agent_id="assistant", session_id="ses")
    assert answer.reply_plan == ReplyPlanFacts(
        channel_id="tg", platform_target="-100", reply_to_message_id="9", thread_id="7"
    )

    store.settle_reply("tg", "answer")
    assert store.claim_reply("tg", "answer") is False
    assert [reply.id for reply in store.take_pending_replies("tg", "next")[0]] == [
        "mine",
        "notice",
    ]


def test_owed_replies_open_additively_from_a_database_without_retry_keyboards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = _owed("legacy", "ended")
    with monkeypatch.context() as legacy:
        legacy.setattr(
            schema_module,
            "SCHEMA_SQL",
            schema_module.SCHEMA_SQL.replace("  retry_keyboard_json TEXT,\n", ""),
        )
        state = _open(tmp_path)
        try:
            state.reset("tg", "telegram")
            state.database.write(
                lambda connection: connection.execute(
                    "INSERT INTO channel_pending_replies "
                    "(channel_id, reply_id, platform_target, thread_id, reply_to_message_id, "
                    "owner, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("tg", pending.id, "-100", "7", "9", pending.owner, pending.created_at),
                )
            )
        finally:
            state.close()

    state = _open(tmp_path)
    try:
        assert state.take_pending_replies("tg", "next") == ([pending], 0)
        keyboard = {"buttons": [[{"label": "Retry", "data": "run:done"}]]}
        state.owe_reply("tg", replace(pending, retry_keyboard=keyboard))
        assert state.take_pending_replies("tg", "next")[0][0].retry_keyboard == keyboard
    finally:
        state.close()


@pytest.mark.asyncio
async def test_receipts_keep_a_fifo_window_per_channel(
    store: ChannelStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert state_module.RECEIVED_MESSAGE_WINDOW == 4096
    monkeypatch.setattr(state_module, "RECEIVED_MESSAGE_WINDOW", 3)
    store.reset("other", "telegram")
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
    assert store.load_update_offset("tg", _BOT) == 0

    store.save_update_offset("tg", _BOT, 100)
    store.save_update_offset("tg", _BOT, 90)
    store.save_update_offset("tg", _BOT, 100)
    assert store.load_update_offset("tg", _BOT) == 100
    with pytest.raises(ChannelError, match="update id must be a non-negative integer"):
        store.save_update_offset("tg", _BOT, -1)
    with pytest.raises(ChannelError, match="bot id must be a positive integer"):
        store.save_update_offset("tg", 0, 101)
    with pytest.raises(ChannelError, match="bot id must be a positive integer"):
        store.load_update_offset("tg", True)  # type: ignore[arg-type]


def test_polling_watermark_applies_only_to_the_bot_it_names(store: ChannelStateStore) -> None:
    store.save_update_offset("tg", _BOT, 100)

    # Another bot's update ids form their own sequence: lower ids replace the mark.
    assert store.load_update_offset("tg", _OTHER_BOT) == 0
    store.save_update_offset("tg", _OTHER_BOT, 5)
    assert store.load_update_offset("tg", _OTHER_BOT) == 5
    assert store.load_update_offset("tg", _BOT) == 0

    # A watermark that names no bot applies to none.
    store.database.write(
        lambda connection: connection.execute(
            "UPDATE channel_polling SET bot_id = NULL WHERE channel_id = 'tg'"
        )
    )
    assert store.load_update_offset("tg", _OTHER_BOT) == 0
    store.save_update_offset("tg", _OTHER_BOT, 3)
    assert store.load_update_offset("tg", _OTHER_BOT) == 3


@pytest.mark.parametrize(("age_hours", "fresh"), [(47, True), (48, False)])
def test_polling_watermark_expires_before_telegram_randomizes_ids(
    store: ChannelStateStore, age_hours: int, fresh: bool
) -> None:
    # Telegram may restart update ids after a week without updates; the stored
    # watermark stops applying once its 48-hour lifetime has passed.
    store.save_update_offset("tg", _BOT, 100)
    written_at = format_canonical_timestamp(datetime.now(UTC) - timedelta(hours=age_hours))
    store.database.write(
        lambda connection: connection.execute(
            "UPDATE channel_polling SET updated_at = ? WHERE channel_id = 'tg'", (written_at,)
        )
    )

    assert store.load_update_offset("tg", _BOT) == (100 if fresh else 0)
    store.save_update_offset("tg", _BOT, 5)
    assert store.load_update_offset("tg", _BOT) == (100 if fresh else 5)


def test_polling_watermark_survives_reopening_the_store(tmp_path: Path) -> None:
    state = _open(tmp_path)
    state.reset("tg", "telegram")
    state.save_update_offset("tg", _BOT, 42)
    state.close()

    reopened = _open(tmp_path)
    try:
        assert reopened.load_update_offset("tg", _BOT) == 42
    finally:
        reopened.close()
