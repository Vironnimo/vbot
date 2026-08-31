"""Declarative schema reconciliation and WAL-reset guard for the Session store."""

from __future__ import annotations

import json
import sqlite3

import pytest

from core.sessions import schema as session_schema
from core.sessions.errors import SessionStoreCorruptError, SessionStoreSchemaMismatchError
from core.sessions.schema import (
    APPLICATION_ID,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    declared_schema,
    is_wal_reset_vulnerable,
    reconcile_schema,
)
from core.sessions.sessions import SessionAddress
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
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
        monkeypatch.setattr(session_schema, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
        applied = reconcile_schema(connection)
        assert applied == [f"schema version {SCHEMA_VERSION - 1} -> {SCHEMA_VERSION + 1}"]
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION + 1
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


def test_store_reports_an_older_schema_separately_from_corruption(tmp_path) -> None:
    database = tmp_path / "sessions.db"
    connection = _create_current_database(database)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    connection.commit()
    connection.close()

    with pytest.raises(SessionStoreSchemaMismatchError, match="offline conversion"):
        SessionStore(database)


def test_store_rejects_a_marker_with_an_older_schema_version(tmp_path) -> None:
    marker = json.loads((tmp_path / "session-store.json").read_text(encoding="utf-8"))
    marker["schema_version"] = SCHEMA_VERSION - 1
    (tmp_path / "session-store.json").write_text(json.dumps(marker), encoding="utf-8")

    with pytest.raises(SessionStoreSchemaMismatchError, match="marker schema"):
        SessionStore(tmp_path / "sessions.db")


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
