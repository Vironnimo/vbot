"""Tests for the Generation 1 conversion of the Swarm Extension database."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from core.database import APPLICATION_IDS, open_offline_database
from core.extensions.databases import Database, extension_database_spec
from core.sessions import SessionAddress, TemporarySessionBinding
from resources.extensions.swarm.store import SCHEMA_SQL, SwarmStore
from scripts.converters.persistence_generation_1._context import (
    ConversionContext,
    ConversionError,
)
from scripts.converters.persistence_generation_1.swarm import AREA, convert
from tests.resources.extensions.swarm_store_helpers import _swarm, open_swarm_database
from tests.scripts.converters.persistence_generation_1.legacy_schema_support import (
    LEGACY_SWARM_DDL,
    create_legacy_database,
)

_CANONICAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_KERNEL_TABLES = {"kernel_meta", "kernel_migrations"}


def _context(tmp_path: Path) -> ConversionContext:
    source = tmp_path / "data"
    source.mkdir(exist_ok=True)
    return ConversionContext(source=source, staging=tmp_path / "staging")


def _source_path(context: ConversionContext) -> Path:
    return context.source / "extension-data" / "swarm" / "swarm.db"


def _staged(context: ConversionContext) -> Database:
    return open_offline_database(
        extension_database_spec(context.staging, "swarm", "swarm", SCHEMA_SQL)
    )


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _all_rows(database: Database) -> dict[str, list[tuple[Any, ...]]]:
    with database.read() as connection:
        return {
            table: sorted(
                (tuple(row) for row in connection.execute(f"SELECT * FROM {table}")), key=repr
            )
            for table in _tables(connection)
            if table not in _KERNEL_TABLES
        }


def _legacy_timestamp(value: Any) -> Any:
    """Today's Swarm stored ``datetime.isoformat()`` values with ``+00:00``."""
    if isinstance(value, str) and _CANONICAL.match(value):
        return datetime.fromisoformat(value).isoformat()
    return value


async def _origin_swarm(directory: Path) -> tuple[Database, str, dict[str, Any]]:
    """A Swarm with Board, delivery and Wiki rows, written by the current store."""
    database = open_swarm_database(directory)
    store = SwarmStore(database)
    await store.open()
    try:
        started = await _swarm(store, count=3)
        sid = str(started["swarm_id"])
        peers = [item["id"] for item in (await store.get_swarm(sid))["participants"]]
        await store.bind_participant_session(
            TemporarySessionBinding(
                SessionAddress(None, "tmp_agent", "ses_peer"), "gen_1", "swarm", sid, peers[1], {}
            )
        )
        await store.post(sid, peers[0], text="First finding", request_id="post-1")
        await store.prepare_inbox_delivery(sid, peers[1])
        await store.wiki(
            sid,
            peers[0],
            {"action": "create", "title": "Notes", "content": "Shared", "request_id": "wiki-1"},
        )
        await store.set_swarm_state(sid, "running")
        await store.recover_interrupted()
        snapshot = await store.get_swarm(sid)
    finally:
        await store.close()
    return database, sid, snapshot


def _write_legacy_copy(origin: Database, path: Path) -> None:
    """Store ``origin``'s rows in a database with today's schema and timestamps."""
    create_legacy_database(path, LEGACY_SWARM_DDL)
    with origin.read() as source, closing(sqlite3.connect(path)) as target, target:
        for table in _tables(target):
            if table.startswith("decision_"):
                continue
            columns = _columns(target, table)
            for row in source.execute(f"SELECT {','.join(columns)} FROM {table}"):
                target.execute(
                    f"INSERT INTO {table}({','.join(columns)}) "
                    f"VALUES({','.join('?' * len(columns))})",
                    [_legacy_timestamp(value) for value in row],
                )


def _add_decision_history(path: Path, swarm_id: str) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("INSERT INTO decision_questions VALUES('dq_1',?,'{}')", (swarm_id,))
        connection.execute("INSERT INTO decision_positions VALUES('dq_1','peer','{}')")
        connection.execute(
            "INSERT INTO decision_events VALUES(1,?,'dq_1','asked','peer','Peer',"
            "'2026-09-01T10:00:00+00:00','{}')",
            (swarm_id,),
        )
        connection.execute(
            "INSERT INTO requests VALUES(?,'ask-1','hash','{}')", (f"decisions:{swarm_id}:ask",)
        )


@pytest.mark.asyncio
async def test_round_trip_keeps_every_row_and_the_store_reads_it(tmp_path: Path) -> None:
    context = _context(tmp_path)
    origin, sid, snapshot = await _origin_swarm(tmp_path / "origin")
    try:
        _write_legacy_copy(origin, _source_path(context))
        expected = _all_rows(origin)
    finally:
        origin.close()
    exercised = ("posts", "recipients", "delivery_batches", "delivery_batch_entries")
    assert all(expected[table] for table in (*exercised, "swarm_events", "wiki_revisions"))
    _add_decision_history(_source_path(context), sid)
    original = _source_path(context).read_bytes()

    convert(context)

    staged = _staged(context)
    try:
        assert _all_rows(staged) == expected
        with staged.read() as connection:
            assert not [name for name in _tables(connection) if name.startswith("decision_")]
            assert (
                connection.execute("PRAGMA application_id").fetchone()[0]
                == (APPLICATION_IDS["extensions"])
            )
        store = SwarmStore(staged)
        await store.open()
        try:
            assert await store.get_swarm(sid) == snapshot
            listed = await store.wiki(sid, None, {"action": "list"})
            assert [entry["title"] for entry in listed["entries"]] == ["Notes"]
        finally:
            await store.close()
    finally:
        staged.close()
    counts = context.report.counts[AREA]
    assert counts["posts"] == len(expected["posts"])
    assert counts["requests"] == len(expected["requests"])
    assert counts["decision_questions_dropped"] == 1
    assert counts["decision_events_dropped"] == 1
    assert counts["decision_positions_dropped"] == 1
    assert counts["decision_requests_dropped"] == 1
    assert {item.item for item in context.report.skipped} == {
        "decision_events",
        "decision_positions",
        "decision_questions",
        "requests",
    }
    assert _source_path(context).read_bytes() == original


def _legacy_source(context: ConversionContext, *statements: str) -> Path:
    path = _source_path(context)
    create_legacy_database(path, LEGACY_SWARM_DDL)
    with closing(sqlite3.connect(path)) as connection, connection:
        for statement in statements:
            connection.execute(statement)
    return path


_SWARM_ROW = (
    "INSERT INTO swarms VALUES('swr_1','Goal','{}','{}','idle','2026-09-01T10:00:00+00:00')"
)


def test_rows_the_new_database_refuses_are_dropped_and_reported(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _legacy_source(
        context,
        _SWARM_ROW,
        "INSERT INTO swarm_settings VALUES('swr_missing',1,'{}')",
        "INSERT INTO swarm_events VALUES(5,'swr_1','created','user',NULL,'{}',NULL,'soon')",
    )

    convert(context)

    staged = _staged(context)
    try:
        with staged.read() as connection:
            assert connection.execute("SELECT COUNT(*) FROM swarm_settings").fetchone()[0] == 0
            events = connection.execute("SELECT id,created_at FROM swarm_events").fetchall()
        assert [tuple(row) for row in events] == [(5, "soon")]
    finally:
        staged.close()
    counts = context.report.counts[AREA]
    assert (counts["swarms"], counts["swarm_settings"], counts["swarm_events"]) == (1, 0, 1)
    reasons = {item.item: item.reason for item in context.report.skipped}
    assert reasons["swarm_settings swr_missing"].startswith("row dropped: FOREIGN KEY")
    assert reasons["swarm_events 5"].startswith("created_at 'soon' kept")


@pytest.mark.asyncio
async def test_retired_tool_names_in_profiles_and_snapshots_are_replaced(tmp_path: Path) -> None:
    context = _context(tmp_path)
    retired = {"mode": "selected", "allowed": ["write", "edit", "glob", "grep", "read"]}
    profile = json.dumps({"id": "prf_1", "name": "Review", "tool_access": retired})
    current = '{"tool_access": {"mode": "selected", "allowed": ["read"]}, "id": "prf_2"}'
    snapshot = json.dumps({"id": "prf_1", "tool_access": {"mode": "all", "denied": ["write"]}})
    at = "2026-09-01T10:00:00+00:00"
    _legacy_source(
        context,
        f"INSERT INTO profiles VALUES('prf_1','review','Review',1,'{profile}','{at}','{at}')",
        f"INSERT INTO profiles VALUES('prf_2','read','Read',1,'{current}','{at}','{at}')",
        f"INSERT INTO swarms VALUES('swr_1','Goal','{snapshot}','{{}}','idle','{at}')",
    )

    convert(context)

    staged = _staged(context)
    try:
        store = SwarmStore(staged)
        await store.open()
        try:
            assert (await store.get_profile("prf_1"))["tool_access"] == {
                "mode": "selected",
                "allowed": ["apply_patch", "search_files", "read"],
            }
        finally:
            await store.close()
        with staged.read() as connection:
            payloads = dict(connection.execute("SELECT id, payload FROM profiles").fetchall())
            [stored] = connection.execute("SELECT profile_snapshot FROM swarms").fetchone()
    finally:
        staged.close()
    assert payloads["prf_2"] == current
    assert json.loads(stored)["tool_access"] == {"mode": "all", "denied": ["apply_patch"]}
    assert stored == json.dumps(json.loads(stored), sort_keys=True, separators=(",", ":"))
    assert context.report.counts[AREA]["retired_tool_names_converted"] == 2
    assert [(item.item, item.reason) for item in context.report.skipped] == [
        (
            "profiles prf_1",
            "payload.tool_access: write and edit replaced by apply_patch; "
            "glob and grep replaced by search_files",
        ),
        (
            "swarms swr_1",
            "profile_snapshot.tool_access: write replaced by a denial of apply_patch: "
            "apply_patch needs write, so the access of edit is not carried over; "
            "enable apply_patch explicitly if wanted",
        ),
    ]


def test_board_request_outcomes_lose_the_retired_recipient_list(tmp_path: Path) -> None:
    context = _context(tmp_path)
    routes = {"discussion": 0, "main": 1, "ping": 0}
    post = {"discussion_id": "dsc_1", "post_id": "pst_1", "routes": routes, "sequence": 1}
    created = {
        "discussion_id": "dsc_2",
        "joined": True,
        "main_announcement_id": "pst_3",
        "opening_post_id": "pst_2",
    }
    current = json.dumps({**post, "post_id": "pst_4", "sequence": 2})
    # Only Board outcomes carried the retired list; another scope keeps a field it names.
    foreign = json.dumps({"inactive_recipients": ["kept"], "page_id": "wpg_1"})
    retired = [
        ("post:swr_1:prt_1", "p-1", {**post, "inactive_recipients": ["prt_2"]}),
        ("create:swr_1:prt_1", "c-1", {**created, "inactive_recipients": []}),
    ]
    _legacy_source(
        context,
        _SWARM_ROW,
        *(
            f"INSERT INTO requests VALUES('{scope}','{request_id}','hash','{json.dumps(value)}')"
            for scope, request_id, value in retired
        ),
        f"INSERT INTO requests VALUES('post:swr_1:prt_1','p-2','hash','{current}')",
        f"INSERT INTO requests VALUES('wiki:swr_1:prt_1','w-1','hash','{foreign}')",
    )

    convert(context)

    staged = _staged(context)
    try:
        with staged.read() as connection:
            outcomes = dict(
                connection.execute("SELECT request_id, outcome FROM requests").fetchall()
            )
    finally:
        staged.close()
    assert outcomes == {
        "p-1": json.dumps(post, sort_keys=True, separators=(",", ":")),
        "c-1": json.dumps(created, sort_keys=True, separators=(",", ":")),
        "p-2": current,
        "w-1": foreign,
    }
    assert context.report.counts[AREA]["request_inactive_recipients_dropped"] == 2
    reason = (
        "retired outcome.inactive_recipients dropped: Swarm activity is derived now, "
        "and a replay of the request returns only the current outcome fields"
    )
    assert [(item.item, item.reason) for item in context.report.skipped] == [
        ("requests create:swr_1:prt_1 c-1", reason),
        ("requests post:swr_1:prt_1 p-1", reason),
    ]


def test_missing_and_unknown_tables_and_columns_are_reported(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _legacy_source(
        context,
        _SWARM_ROW,
        "DROP TABLE wiki_revisions",
        "CREATE TABLE scratch(value TEXT)",
        "ALTER TABLE swarms ADD COLUMN color TEXT",
    )

    convert(context)

    reasons = {item.item: item.reason for item in context.report.skipped}
    assert reasons["wiki_revisions"] == "table missing in the source; none copied"
    assert reasons["scratch"] == "unknown source table dropped"
    assert reasons["swarms.color"] == "unknown source column dropped"
    assert context.report.counts[AREA]["swarms"] == 1


def test_a_table_without_its_columns_is_refused(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _legacy_source(context, "ALTER TABLE participants DROP COLUMN lifecycle_run_id")

    with pytest.raises(ConversionError, match="participants lacks the columns lifecycle_run_id"):
        convert(context)


def test_a_missing_source_stages_nothing(tmp_path: Path) -> None:
    context = _context(tmp_path)

    convert(context)

    assert context.report.counts[AREA] == {"source_missing": 1}
    assert not (context.staging / "extension-data").exists()


def test_an_already_converted_source_is_left_alone(tmp_path: Path) -> None:
    context = _context(tmp_path)
    open_offline_database(
        extension_database_spec(context.source, "swarm", "swarm", SCHEMA_SQL)
    ).close()

    convert(context)

    assert context.report.counts[AREA] == {"already_current": 1}
    assert not (context.staging / "extension-data").exists()


def test_a_foreign_database_is_refused(tmp_path: Path) -> None:
    context = _context(tmp_path)
    path = _legacy_source(context)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version = 3")

    with pytest.raises(ConversionError, match="not a pre-Generation-1 Swarm database"):
        convert(context)
