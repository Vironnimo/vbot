"""Declarative schema reconciliation and WAL-reset guard for the Session store."""

from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from core.chat.messages import ChatMessage, ToolCall
from core.sessions import schema as session_schema
from core.sessions._types import SessionAddress
from core.sessions.errors import SessionStoreCorruptError, SessionStoreSchemaMismatchError
from core.sessions.schema import (
    APPLICATION_ID,
    FTS_STORAGE_VERSION,
    SCHEMA_CONVERSION_FLOOR,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    declared_schema,
    is_wal_reset_vulnerable,
    reconcile_schema,
)
from core.sessions.store import SessionStore


def _create_current_database(path) -> sqlite3.Connection:
    """A database at the current schema generation, as an older vBot wrote it."""
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_SQL)
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    marker = json.loads((path.parent / "session-store.json").read_text(encoding="utf-8"))
    connection.execute(
        "INSERT INTO store_meta (key, value) VALUES ('database_id', ?)",
        (marker["database_id"],),
    )
    connection.commit()
    return connection


def _insert_one_session(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, created_at) "
        "VALUES ('generation-1', '', 'agent', 'session', '2026-08-30T00:00:00Z')"
    )
    connection.commit()


def _journal_mode(database) -> str:
    probe = sqlite3.connect(database)
    try:
        return str(probe.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        probe.close()


def test_unreleased_session_store_uses_the_first_schema_generation() -> None:
    assert SCHEMA_VERSION == 1
    assert SCHEMA_CONVERSION_FLOOR == 1
    assert FTS_STORAGE_VERSION == 1


@pytest.mark.parametrize(
    ("version_info", "expected"),
    [
        ((3, 6, 99), False),
        ((3, 7, 0), True),
        ((3, 40, 1), True),
        ((3, 44, 5), True),
        ((3, 44, 6), False),
        ((3, 50, 4), True),
        ((3, 50, 7), False),
        ((3, 51, 2), True),
        ((3, 51, 3), False),
        ((3, 52, 0), False),
    ],
)
def test_is_wal_reset_vulnerable_matches_the_official_ranges(version_info, expected) -> None:
    assert is_wal_reset_vulnerable(version_info) is expected


def test_declared_schema_marks_generated_and_key_columns_not_addable() -> None:
    declared = declared_schema(SCHEMA_SQL)
    sessions = declared.table_columns["sessions"]
    assert sessions["session_key"].addable is False
    assert sessions["latest_completion_status"].addable is False
    assert sessions["message_count"].addable is True
    assert sessions["message_count"].type_expression == (
        "INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0)"
    )
    assert "sessions_one_live_address" in declared.unique_index_text


def test_reconcile_is_a_noop_on_the_current_schema(tmp_path) -> None:
    connection = _create_current_database(tmp_path / "sessions.db")
    try:
        assert reconcile_schema(connection) == []
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
    finally:
        connection.close()


def test_reconcile_heals_a_missing_column_and_table_and_keeps_rows(tmp_path) -> None:
    connection = _create_current_database(tmp_path / "sessions.db")
    try:
        _insert_one_session(connection)
        future_schema = SCHEMA_SQL + (
            "\nALTER TABLE sessions ADD COLUMN reconcile_probe INTEGER NOT NULL DEFAULT 0;"
            "\nCREATE TABLE reconcile_probe_table (id INTEGER PRIMARY KEY) STRICT;"
        )
        applied = reconcile_schema(connection, schema_sql=future_schema)
        assert "added column sessions.reconcile_probe" in applied
        assert "created table reconcile_probe_table" in applied
        row = connection.execute(
            "SELECT reconcile_probe, message_count FROM sessions WHERE session_id = 'session'"
        ).fetchone()
        assert row == (0, 0)
        assert connection.execute("SELECT 1 FROM reconcile_probe_table").fetchone() is None
    finally:
        connection.close()


def test_additive_temporary_tables_preserve_a_copied_current_baseline(tmp_path) -> None:
    """Opening a copied populated baseline adds tables, preserving every prior row."""
    database = tmp_path / "sessions.db"
    store = SessionStore(database)
    address = SessionAddress("project", "agent", "retained")
    try:
        store.ensure_live(address)
        store.replace_metadata(address, {"title": "Retained title", "custom": {"unicode": "Grüße"}})
        store.replace_activity(address, {"run_id": "retained-run", "state": "interrupted"})
        store.append_messages(
            address,
            [
                ChatMessage.user("Retained task"),
                ChatMessage.assistant(
                    model="provider/model",
                    content="Retained response",
                    reasoning="Retained reasoning",
                    usage={"input_tokens": 17, "output_tokens": 5, "cache_read_tokens": 3},
                    tool_calls=[
                        ToolCall(id="retained-call", name="read", arguments={"path": "notes.md"})
                    ],
                ),
                ChatMessage.tool(
                    tool_call_id="retained-call",
                    name="read",
                    content='{"ok":true,"data":{"text":"retained result"}}',
                ),
            ],
        )
        store.append_continuation(
            address,
            [
                {
                    "version": 1,
                    "type": "run_started",
                    "checkpoint_id": "retained-checkpoint",
                    "run_id": "retained-run",
                    "origin_run_id": "retained-run",
                    "timestamp": "2026-09-07T12:00:00+00:00",
                    "request": "Retained continuation",
                }
            ],
        )
        archived = SessionAddress(None, "agent", "archived")
        store.ensure_live(archived)
        store.append_messages(archived, [ChatMessage.user("Retained archived task")])
        store.archive(archived)
    finally:
        store.close()

    # Remove only this feature's empty additive relations to obtain the prior
    # baseline layout. Take an actual database copy before opening the new code.
    with sqlite3.connect(database) as baseline:
        baseline.execute("DROP TABLE session_delivery_receipts")
        baseline.execute("DROP TABLE run_execution_owners")
        baseline.execute("DROP TABLE temporary_session_bindings")
        tables = [
            row[0]
            for row in baseline.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        previous_rows = {
            table: sorted(baseline.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
            for table in tables
        }
    copy_directory = tmp_path / "copied-baseline"
    copy_directory.mkdir()
    copied_database = copy_directory / "sessions.db"
    with sqlite3.connect(database) as source, sqlite3.connect(copied_database) as destination:
        source.backup(destination)
    shutil.copy2(tmp_path / "session-store.json", copy_directory / "session-store.json")
    copied = SessionStore(copied_database)
    copied.close()

    with sqlite3.connect(copied_database) as verification:
        assert verification.execute("PRAGMA user_version").fetchone() == (1,)
        assert verification.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,)
        for table, rows in previous_rows.items():
            assert (
                sorted(verification.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
                == rows
            ), table
        assert verification.execute(
            "SELECT COUNT(*) FROM temporary_session_bindings"
        ).fetchone() == (0,)
        assert verification.execute(
            "SELECT COUNT(*) FROM session_delivery_receipts"
        ).fetchone() == (0,)
        assert verification.execute("SELECT COUNT(*) FROM run_execution_owners").fetchone() == (0,)


def test_store_backfills_normalized_session_metadata_columns(tmp_path) -> None:
    projection_columns = {
        "title",
        "auto_title",
        "source_channel_id",
        "platform",
        "platform_conv_id",
        "is_subagent_session",
        "subagent_parent_json",
        "fork_source_json",
        "run_kinds_json",
        "compaction_policy_json",
        "list_visibility_mask",
        "active_sort",
    }
    old_schema = "\n".join(
        line
        for line in SCHEMA_SQL.splitlines()
        if not any(line.startswith(f"  {column} ") for column in projection_columns)
    )
    old_schema = old_schema.replace(
        "CREATE INDEX sessions_live_scope_order\n"
        "  ON sessions (project_id, agent_id, active_sort DESC, session_id)\n"
        "  WHERE status = 'live';\n",
        "CREATE INDEX sessions_live_scope_order\n"
        "  ON sessions (project_id, agent_id, last_message_at DESC, session_id)\n"
        "  WHERE status = 'live';\n",
    )
    old_schema = old_schema.replace(
        "CREATE INDEX sessions_live_global_order\n"
        "  ON sessions (active_sort DESC, project_id, agent_id, session_id)\n"
        "  WHERE status = 'live';\n\n",
        "",
    )
    old_schema = old_schema.replace(
        "CREATE INDEX sessions_live_scope_visibility\n"
        "  ON sessions (project_id, agent_id, list_visibility_mask)\n"
        "  WHERE status = 'live';\n",
        "",
    )
    old_schema = old_schema.replace(
        "CREATE INDEX sessions_live_fork_source\n"
        "  ON sessions (project_id, agent_id, json_extract(fork_source_json, '$.session_id'))\n"
        "  WHERE status = 'live';\n",
        "",
    )
    database = tmp_path / "sessions.db"
    connection = sqlite3.connect(database)
    connection.executescript(old_schema)
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    marker = json.loads((tmp_path / "session-store.json").read_text(encoding="utf-8"))
    connection.execute(
        "INSERT INTO store_meta (key, value) VALUES ('database_id', ?)",
        (marker["database_id"],),
    )
    connection.execute(
        "INSERT INTO sessions "
        "(generation_id, project_id, agent_id, session_id, created_at, metadata_json) "
        "VALUES (?, '', 'agent', 'session', '2026-08-30T00:00:00Z', ?)",
        (
            "generation-1",
            json.dumps(
                {
                    "title": "Existing title",
                    "run_kinds": ["user"],
                    "pinned_skill_catalog": "large context",
                }
            ),
        ),
    )
    connection.commit()
    connection.close()

    store = SessionStore(database)
    try:
        metadata = store.metadata(SessionAddress(None, "agent", "session"))
    finally:
        store.close()

    assert metadata == {
        "title": "Existing title",
        "run_kinds": ["user"],
        "pinned_skill_catalog": "large context",
    }
    with sqlite3.connect(database) as verification:
        title, run_kinds, visibility_mask, active_sort, residual = verification.execute(
            "SELECT title, run_kinds_json, list_visibility_mask, active_sort, metadata_json "
            "FROM sessions"
        ).fetchone()
    assert title == "Existing title"
    assert json.loads(run_kinds) == ["user"]
    assert visibility_mask > 0
    assert active_sort > 0
    assert json.loads(residual) == {"pinned_skill_catalog": "large context"}
    with sqlite3.connect(database) as verification:
        scope_order_columns = [
            row[2] for row in verification.execute("PRAGMA index_info(sessions_live_scope_order)")
        ]
    assert scope_order_columns == ["project_id", "agent_id", "active_sort", "session_id"]


def test_projection_upgrade_preserves_already_normalized_metadata(tmp_path) -> None:
    database = tmp_path / "sessions.db"
    connection = _create_current_database(database)
    connection.execute(
        "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, created_at, "
        "title, run_kinds_json, metadata_json) VALUES (?, '', 'agent', 'session', ?, ?, ?, ?)",
        (
            "generation-1",
            "2026-08-30T00:00:00Z",
            "Existing title",
            '["user"]',
            '{"pinned_skill_catalog":"large context"}',
        ),
    )
    connection.execute(
        "INSERT INTO store_meta (key, value) VALUES (?, ?)",
        ("session_metadata_projection_version", "1"),
    )
    connection.commit()
    connection.close()

    store = SessionStore(database)
    try:
        metadata = store.metadata(SessionAddress(None, "agent", "session"))
    finally:
        store.close()

    assert metadata == {
        "title": "Existing title",
        "run_kinds": ["user"],
        "pinned_skill_catalog": "large context",
    }
    with sqlite3.connect(database) as verification:
        title, run_kinds, visibility_mask, residual = verification.execute(
            "SELECT title, run_kinds_json, list_visibility_mask, metadata_json FROM sessions"
        ).fetchone()
    assert title == "Existing title"
    assert json.loads(run_kinds) == ["user"]
    assert visibility_mask > 0
    assert json.loads(residual) == {"pinned_skill_catalog": "large context"}


def test_reconcile_preserves_the_complete_additive_column_contract(tmp_path) -> None:
    connection = _create_current_database(tmp_path / "sessions.db")
    try:
        _insert_one_session(connection)
        future_schema = SCHEMA_SQL + (
            "\nALTER TABLE sessions ADD COLUMN reconcile_checked INTEGER NOT NULL "
            "DEFAULT 0 CHECK (reconcile_checked >= 0);"
        )

        assert "added column sessions.reconcile_checked" in reconcile_schema(
            connection, schema_sql=future_schema
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE sessions SET reconcile_checked = -1 WHERE session_id = 'session'"
            )
    finally:
        connection.close()


def test_reconcile_refuses_a_required_column_without_a_default(tmp_path) -> None:
    connection = _create_current_database(tmp_path / "sessions.db")
    try:
        future_schema = SCHEMA_SQL + (
            "\nALTER TABLE sessions ADD COLUMN reconcile_required TEXT NOT NULL;"
        )
        with pytest.raises(SessionStoreCorruptError):
            reconcile_schema(connection, schema_sql=future_schema)
    finally:
        connection.close()


def test_reconcile_bumps_a_lagging_user_version(tmp_path, monkeypatch) -> None:
    connection = _create_current_database(tmp_path / "sessions.db")
    try:
        connection.execute("PRAGMA user_version = 0")
        monkeypatch.setattr(session_schema, "SCHEMA_VERSION", 2)
        applied = reconcile_schema(connection)
        assert applied == ["schema version 0 -> 2"]
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 2
    finally:
        connection.close()


def test_reconcile_refuses_a_missing_generated_column(tmp_path) -> None:
    connection = _create_current_database(tmp_path / "sessions.db")
    try:
        future_schema = SCHEMA_SQL + (
            "\nALTER TABLE sessions ADD COLUMN reconcile_generated TEXT "
            "GENERATED ALWAYS AS (json_extract(metadata_json, '$.probe')) VIRTUAL;"
        )
        with pytest.raises(SessionStoreCorruptError):
            reconcile_schema(connection, schema_sql=future_schema)
    finally:
        connection.close()


def test_reconcile_refuses_a_primary_key_change(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "sessions.db")
    try:
        connection.executescript("CREATE TABLE reconcile_shape (id INTEGER, name TEXT) STRICT;")
        declared_sql = "CREATE TABLE reconcile_shape (id INTEGER PRIMARY KEY, name TEXT) STRICT;"
        with pytest.raises(SessionStoreCorruptError):
            reconcile_schema(connection, schema_sql=declared_sql)
    finally:
        connection.close()


def test_reconcile_refuses_a_column_type_change(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "sessions.db")
    try:
        connection.executescript(
            "CREATE TABLE reconcile_shape (id INTEGER PRIMARY KEY, name TEXT) STRICT;"
        )
        declared_sql = "CREATE TABLE reconcile_shape (id INTEGER PRIMARY KEY, name BLOB) STRICT;"
        with pytest.raises(SessionStoreCorruptError):
            reconcile_schema(connection, schema_sql=declared_sql)
    finally:
        connection.close()


def test_reconcile_refuses_a_changed_existing_column_contract(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "sessions.db")
    try:
        connection.executescript(
            "CREATE TABLE reconcile_shape (id INTEGER PRIMARY KEY, name TEXT DEFAULT 'old') STRICT;"
        )
        declared_sql = (
            "CREATE TABLE reconcile_shape (id INTEGER PRIMARY KEY, name TEXT DEFAULT 'new') STRICT;"
        )
        with pytest.raises(SessionStoreCorruptError):
            reconcile_schema(connection, schema_sql=declared_sql)
    finally:
        connection.close()


def test_reconcile_refuses_a_changed_table_constraint(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "sessions.db")
    try:
        connection.executescript(
            "CREATE TABLE reconcile_shape (id INTEGER PRIMARY KEY, CHECK (id > 0)) STRICT;"
        )
        declared_sql = (
            "CREATE TABLE reconcile_shape (id INTEGER PRIMARY KEY, CHECK (id >= 0)) STRICT;"
        )
        with pytest.raises(SessionStoreCorruptError):
            reconcile_schema(connection, schema_sql=declared_sql)
    finally:
        connection.close()


def test_reconcile_recreates_a_changed_non_unique_index(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "sessions.db")
    try:
        connection.executescript(
            "CREATE TABLE reconcile_shape (id INTEGER PRIMARY KEY, name TEXT) STRICT;"
            "CREATE INDEX reconcile_lookup ON reconcile_shape (id);"
        )
        declared_sql = (
            "CREATE TABLE reconcile_shape (id INTEGER PRIMARY KEY, name TEXT) STRICT;"
            "CREATE INDEX reconcile_lookup ON reconcile_shape (name);"
        )

        applied = reconcile_schema(connection, schema_sql=declared_sql)

        assert applied == [
            "dropped stale index reconcile_lookup",
            "recreated index reconcile_lookup",
            f"schema version 0 -> {SCHEMA_VERSION}",
        ]
        index_columns = connection.execute("PRAGMA index_info(reconcile_lookup)").fetchall()
        assert [row[2] for row in index_columns] == ["name"]
    finally:
        connection.close()


def test_reconcile_refuses_a_changed_unique_index(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "sessions.db")
    try:
        connection.executescript(
            "CREATE TABLE reconcile_shape (id INTEGER PRIMARY KEY, name TEXT) STRICT;"
            "CREATE UNIQUE INDEX reconcile_unique ON reconcile_shape (id);"
        )
        declared_sql = (
            "CREATE TABLE reconcile_shape (id INTEGER PRIMARY KEY, name TEXT) STRICT;"
            "CREATE UNIQUE INDEX reconcile_unique ON reconcile_shape (name);"
        )

        with pytest.raises(SessionStoreCorruptError):
            reconcile_schema(connection, schema_sql=declared_sql)
    finally:
        connection.close()


def test_store_opens_a_v1_database_after_an_additive_schema_change(tmp_path, monkeypatch) -> None:
    database = tmp_path / "sessions.db"
    connection = _create_current_database(database)
    _insert_one_session(connection)
    connection.close()
    monkeypatch.setattr(
        session_schema,
        "SCHEMA_SQL",
        SCHEMA_SQL
        + "\nALTER TABLE sessions ADD COLUMN reconcile_probe INTEGER NOT NULL DEFAULT 0;",
    )
    store = SessionStore(database)
    try:
        state = store.state(SessionAddress(project_id=None, agent_id="agent", session_id="session"))
        assert state["reconcile_probe"] == 0
    finally:
        store.close()


def test_store_refuses_a_database_from_a_newer_vbot(tmp_path) -> None:
    database = tmp_path / "sessions.db"
    connection = _create_current_database(database)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()
    with pytest.raises(SessionStoreSchemaMismatchError):
        SessionStore(database)


def test_store_uses_rollback_journal_on_vulnerable_sqlite(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 40, 1))
    store = SessionStore(tmp_path / "sessions.db")
    try:
        assert (
            store.exists(SessionAddress(project_id=None, agent_id="agent", session_id="session"))
            is False
        )
    finally:
        store.close()
    assert _journal_mode(tmp_path / "sessions.db") == "delete"


def test_store_moves_an_existing_wal_database_off_vulnerable_wal(tmp_path, monkeypatch) -> None:
    database = tmp_path / "sessions.db"
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 51, 3))
    store = SessionStore(database)
    store.close()
    assert _journal_mode(database) == "wal"
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 40, 1))
    store = SessionStore(database)
    store.close()
    # Hermes WAL-reset safety: never live-downgrade an existing WAL DB.
    assert _journal_mode(database) == "wal"


def test_reopen_repairs_zero_active_sort_for_metadata_only_ensure_live_session(tmp_path) -> None:
    database = tmp_path / "sessions.db"
    address = SessionAddress(None, "agent", "metadata-only")
    store = SessionStore(database)
    store.ensure_live(address)
    store.close()

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE sessions SET active_sort = 0 WHERE session_id = ?",
            (address.session_id,),
        )
        connection.execute(
            "UPDATE store_meta SET value = '2' WHERE key = 'session_metadata_projection_version'"
        )
        connection.commit()

    repaired = SessionStore(database)
    try:
        state = repaired.state(address)
        expected = repaired._writer.execute(
            "SELECT julianday(created_at) FROM sessions WHERE session_id = ?",
            (address.session_id,),
        ).fetchone()[0]
        assert state["message_count"] == 0
        assert state["active_sort"] == pytest.approx(expected)
    finally:
        repaired.close()

    reopened = SessionStore(database)
    try:
        state = reopened.state(address)
        assert state["message_count"] == 0
        assert state["active_sort"] == pytest.approx(expected)
        assert (
            reopened._writer.execute(
                "SELECT value FROM store_meta WHERE key = 'session_metadata_projection_version'"
            ).fetchone()[0]
            == "3"
        )
    finally:
        reopened.close()
