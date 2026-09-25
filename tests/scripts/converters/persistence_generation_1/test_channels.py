"""Tests for the Generation 1 conversion of the Channel state into channels.db."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

import scripts.converters.persistence_generation_1.channels as channels_area
from core.channels import channel_database_spec
from core.channels.state import ChannelStateStore
from core.database import open_offline_database
from core.utils.timestamps import format_canonical_timestamp
from scripts.converters.persistence_generation_1._context import ConversionContext
from scripts.converters.persistence_generation_1.channels import AREA, convert

# The pre-Generation-1 ``sessions`` table, frozen to the columns the area reads.
_OLD_SESSIONS_DDL = """
CREATE TABLE sessions (
  session_key INTEGER PRIMARY KEY,
  project_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'live' CHECK (status IN ('live', 'archived')),
  source_channel_id TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
"""

_FILE_TIME = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)


def _context(tmp_path: Path) -> ConversionContext:
    source = tmp_path / "data"
    source.mkdir(exist_ok=True)
    return ConversionContext(source=source, staging=tmp_path / "staging")


def _write(source: Path, relative: str, value: Any, *, file_time: datetime = _FILE_TIME) -> None:
    path = source / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    os.utime(path, (file_time.timestamp(), file_time.timestamp()))


def _channel(source: Path, channel_id: str, agent_id: str = "assistant") -> None:
    _write(
        source,
        f"channels/{channel_id}/channel.json",
        {"id": channel_id, "platform": "telegram", "agent_id": agent_id},
    )


def _old_sessions(source: Path, rows: list[tuple[str, str, str, str, str | None, Any]]) -> Path:
    path = source / "sessions.db"
    with closing(sqlite3.connect(path)) as database:
        database.executescript(_OLD_SESSIONS_DDL)
        database.executemany(
            "INSERT INTO sessions "
            "(project_id, agent_id, session_id, status, source_channel_id, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (project_id, agent_id, session_id, status, channel_id, json.dumps(metadata))
                for project_id, agent_id, session_id, status, channel_id, metadata in rows
            ],
        )
        database.commit()
    return path


@contextmanager
def _staged_store(context: ConversionContext) -> Iterator[ChannelStateStore]:
    path = context.staging / "channels.db"
    store = ChannelStateStore(open_offline_database(channel_database_spec(path)))
    try:
        yield store
    finally:
        store.close()


def _skipped(context: ConversionContext) -> dict[str, str]:
    return {item.item: item.reason for item in context.report.skipped if item.area == AREA}


@pytest.mark.asyncio
async def test_converts_every_state_file_of_a_channel_and_retires_it(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _channel(context.source, "tg-assistant")
    _write(
        context.source,
        "channels/tg-assistant/access.json",
        {
            "version": 1,
            "self_user_id": "50",
            "groups": {
                "-100": {
                    "admin_user_ids": ["51"],
                    "participants": {
                        "50": {
                            "display_name": "Alice",
                            "last_seen_at": "2026-06-18T12:00:00.250000+02:00",
                        },
                        "51": {"display_name": "Bob", "last_seen_at": "2026-06-18T11:00:00+00:00"},
                    },
                },
            },
        },
    )
    binding = {
        "platform_target": "-100",
        "thread_id": "7",
        "origin_session_id": "origin",
        "original_button_data": ["run:yes", "run:no"],
        "created_at": "2026-06-18T09:00:00+00:00",
    }
    _write(
        context.source,
        "channels/tg-assistant/run-button-bindings.json",
        {
            "version": 1,
            "bindings": {
                "pending": {**binding, "consumed": False},
                "used": {**binding, "consumed": True},
            },
        },
    )
    _write(
        context.source,
        "channels/tg-assistant/polling.json",
        {"version": 1, "last_update_id": 42},
    )
    _write(context.source, "channels/tg-assistant/received.json", ["-100:1", "-100:2"])

    convert(context)

    with _staged_store(context) as store:
        access = await store.access_state("tg-assistant")
        assert access["self_user_id"] == "50"
        [group] = access["groups"]
        assert group["admin_user_ids"] == ["50", "51"]
        assert group["participants"] == [
            {
                "user_id": "50",
                "display_name": "Alice",
                "last_seen_at": "2026-06-18T10:00:00.250000Z",
                "role": "admin",
            },
            {
                "user_id": "51",
                "display_name": "Bob",
                "last_seen_at": "2026-06-18T11:00:00.000000Z",
                "role": "admin",
            },
        ]

        pending = store.claim_run_button_binding(
            "tg-assistant", "pending", platform_target="-100", thread_id="7"
        )
        assert pending.status == "claimed"
        assert pending.binding is not None
        assert pending.binding.original_button_data == ("run:yes", "run:no")
        used = store.claim_run_button_binding(
            "tg-assistant", "used", platform_target="-100", thread_id="7"
        )
        assert used.status == "consumed"

        assert await store.has_received("tg-assistant", "-100:1")
        with store.database.read() as connection:
            used_row = connection.execute(
                "SELECT created_at, consumed_at FROM channel_run_buttons WHERE binding_id = 'used'"
            ).fetchone()
            registry = connection.execute("SELECT channel_id, platform FROM channels").fetchall()
            polling_rows = connection.execute("SELECT COUNT(*) FROM channel_polling").fetchone()
            receipts = [
                tuple(row)
                for row in connection.execute(
                    "SELECT message_ref, received_at FROM channel_received ORDER BY received_at"
                )
            ]
    # A consumed binding takes the time the file was last written.
    assert tuple(used_row) == (
        "2026-06-18T09:00:00.000000Z",
        format_canonical_timestamp(_FILE_TIME),
    )
    # The registry records the platform whose ids the state holds.
    assert [tuple(row) for row in registry] == [("tg-assistant", "telegram")]
    # The watermark named no bot, so no bot could use it.
    assert polling_rows[0] == 0
    # Receipts keep their order and end at the time the file was last written.
    assert [message_ref for message_ref, _received_at in receipts] == ["-100:1", "-100:2"]
    assert receipts[-1][1] == format_canonical_timestamp(_FILE_TIME)

    assert context.retired == [
        PurePosixPath(f"channels/tg-assistant/{name}")
        for name in ("access.json", "run-button-bindings.json", "polling.json", "received.json")
    ]
    assert context.report.counts[AREA] == {
        "channels": 1,
        "admins": 1,
        "participants": 2,
        "conversations": 0,
        "run_buttons": 2,
        "received": 2,
    }
    assert _skipped(context) == {
        "channels/tg-assistant/polling.json": (
            "polling watermark dropped: it does not name its Telegram bot"
        ),
    }


def test_every_configured_channel_is_registered_without_state(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _channel(context.source, "dc-assistant")

    convert(context)

    with _staged_store(context) as store:
        # A registered Channel accepts state writes.
        store.save_update_offset("dc-assistant", 7001, 1)
        assert store.load_update_offset("dc-assistant", 7001) == 1
    assert context.retired == []
    assert context.report.counts[AREA]["channels"] == 1


def test_the_registry_records_only_a_readable_platform(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _channel(context.source, "tg-assistant")
    _write(
        context.source,
        "channels/odd/channel.json",
        {"id": "odd", "platform": "icq", "agent_id": "assistant"},
    )
    _write(context.source, "channels/broken/channel.json", "{not json")

    convert(context)

    with _staged_store(context) as store, store.database.read() as connection:
        platforms = dict(connection.execute("SELECT channel_id, platform FROM channels"))
    # Without a platform, the first readable config names it and keeps the state.
    assert platforms == {"broken": None, "odd": None, "tg-assistant": "telegram"}


def test_routing_pointers_move_from_anchor_metadata_of_the_current_agent(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _channel(context.source, "tg")
    _channel(context.source, "tg-assistant")
    sessions_path = _old_sessions(
        context.source,
        [
            # /new in a direct conversation; the anchor names its kind.
            (
                "",
                "assistant",
                "ch-tg-assistant-12345",
                "live",
                "tg-assistant",
                {"active_session_id": "ses_new", "conversation_kind": "direct"},
            ),
            # A chat migration created a bare anchor; the kind comes from its target.
            (
                "",
                "assistant",
                "ch-tg-assistant--100500",
                "live",
                None,
                {"active_session_id": "ch-tg-assistant--500"},
            ),
            ("", "assistant", "ch-tg-assistant--500", "live", "tg-assistant", _group_kind()),
            # No kind anywhere: recorded as direct and reported.
            ("", "assistant", "ch-tg-777", "live", None, {"active_session_id": "ses_x"}),
            # Anchors routing never read are dropped or ignored.
            (
                "",
                "reviewer",
                "ch-tg-assistant-12345",
                "live",
                "tg-assistant",
                {"active_session_id": "ses_old"},
            ),
            ("", "assistant", "ch-gone-1", "live", None, {"active_session_id": "ses_y"}),
            ("", "assistant", "ch-tg-1", "archived", None, {"active_session_id": "ses_z"}),
            ("vbot", "assistant", "ch-tg-2", "live", None, {"active_session_id": "ses_p"}),
        ],
    )
    source_bytes = sessions_path.read_bytes()

    convert(context)

    with _staged_store(context) as store:
        assert store.active_session_id("tg-assistant", "ch-tg-assistant-12345") == "ses_new"
        assert (
            store.active_session_id("tg-assistant", "ch-tg-assistant--100500")
            == "ch-tg-assistant--500"
        )
        assert store.active_session_id("tg", "ch-tg-777") == "ses_x"
        with store.database.read() as connection:
            kinds = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT conversation_id, conversation_kind FROM channel_conversations"
                )
            }
    assert kinds == {
        "ch-tg-assistant-12345": "direct",
        "ch-tg-assistant--100500": "group",
        "ch-tg-777": "direct",
    }
    assert _skipped(context) == {
        "sessions.db#assistant/ch-tg-777": "unknown conversation kind recorded as direct",
        "sessions.db#reviewer/ch-tg-assistant-12345": (
            "routing pointer of an Agent tg-assistant no longer uses dropped"
        ),
        "sessions.db#assistant/ch-gone-1": "routing pointer of no configured Channel dropped",
    }
    # The source database is read, never written.
    assert sessions_path.read_bytes() == source_bytes
    assert context.report.counts[AREA]["conversations"] == 3


def _group_kind() -> dict[str, Any]:
    return {"conversation_kind": "group", "last_reply_target": {"channel_id": "tg-assistant"}}


@pytest.mark.asyncio
async def test_invalid_state_is_dropped_and_reported_without_failing(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _channel(context.source, "tg-assistant")
    _write(context.source, "channels/tg-assistant/access.json", "{not json")
    _write(
        context.source,
        "channels/tg-assistant/run-button-bindings.json",
        {
            "version": 1,
            "bindings": {
                "broken": {"platform_target": "-100", "consumed": False},
                "undated": {
                    "platform_target": "-100",
                    "thread_id": None,
                    "origin_session_id": "origin",
                    "original_button_data": ["run:done"],
                    "created_at": "yesterday",
                    "consumed": False,
                },
            },
        },
    )
    _write(context.source, "channels/tg-assistant/polling.json", {"version": 2})
    _write(context.source, "channels/tg-assistant/received.json", ["-100:1", 7, None])
    _write(context.source, "channels/orphan/polling.json", {"version": 1, "last_update_id": 3})

    convert(context)

    with _staged_store(context) as store:
        assert (await store.access_state("tg-assistant"))["groups"] == []
        claim = store.claim_run_button_binding(
            "tg-assistant", "undated", platform_target="-100", thread_id=None
        )
        assert claim.status == "claimed"
        assert claim.binding is not None
        assert claim.binding.created_at == format_canonical_timestamp(_FILE_TIME)
        assert await store.has_received("tg-assistant", "-100:1")
    skipped = _skipped(context)
    assert skipped.keys() == {
        "channels/tg-assistant/access.json",
        "channels/tg-assistant/run-button-bindings.json#broken",
        "channels/tg-assistant/run-button-bindings.json#undated",
        "channels/tg-assistant/polling.json",
        "channels/tg-assistant/received.json",
        "channels/orphan",
    }
    assert skipped["channels/orphan"] == "state without channel.json dropped"
    assert PurePosixPath("channels/orphan/polling.json") in context.retired
    assert PurePosixPath("channels/tg-assistant/access.json") in context.retired


@pytest.mark.asyncio
async def test_receipts_keep_the_newest_window_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(channels_area, "RECEIVED_MESSAGE_WINDOW", 2)
    context = _context(tmp_path)
    _channel(context.source, "sl-assistant")
    _write(context.source, "channels/sl-assistant/received.json", ["a", "b", "a", "c"])

    convert(context)

    with _staged_store(context) as store, store.database.read() as connection:
        receipts = connection.execute(
            "SELECT message_ref FROM channel_received ORDER BY received_at"
        ).fetchall()
    # A repeated receipt keeps its newest position; the oldest fall out of the window.
    assert [row[0] for row in receipts] == ["a", "c"]


def test_a_repeated_run_replaces_the_staged_database(tmp_path: Path) -> None:
    first = _context(tmp_path)
    _channel(first.source, "tg-assistant")
    _write(first.source, "channels/tg-assistant/received.json", ["-100:1"])
    convert(first)

    second = ConversionContext(source=first.source, staging=first.staging)
    convert(second)

    with _staged_store(second) as store, store.database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM channel_received").fetchone()[0] == 1
    assert second.report.counts[AREA]["received"] == 1
