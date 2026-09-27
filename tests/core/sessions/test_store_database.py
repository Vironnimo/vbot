"""The Session database on the shared database kernel."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.chat.errors import ChatSessionError
from core.chat.messages import ToolCall
from core.database import (
    APPLICATION_IDS,
    CANONICAL,
    DatabaseCorruptError,
    DatabaseError,
    DatabaseFormatError,
    DatabaseUnavailableError,
    create_data_snapshot,
    data_store_status,
    read_marker,
)
from core.database.snapshots import SNAPSHOT_MANIFEST_NAME
from core.sessions import ChatSessionManager, SessionAddress, _store_schema
from core.sessions._store_schema import session_database_spec
from core.sessions._types import SessionRunAdmission, SessionRunCompletion
from core.sessions.errors import SessionStoreCorruptError
from core.sessions.schema import (
    APPLICATION_ID,
    FORMAT_GENERATION,
    FTS_STALE_KEY,
    FTS_STORAGE_VERSION,
    SCHEMA_SQL,
)
from core.sessions.store import SessionStore


def _address(session_id: str, project_id: str | None = None) -> SessionAddress:
    return SessionAddress(project_id=project_id, agent_id="coder", session_id=session_id)


def test_the_session_database_is_a_canonical_generation_one_database(tmp_path: Path) -> None:
    spec = session_database_spec(tmp_path / "sessions.db")

    assert spec.name == "sessions"
    assert spec.profile == CANONICAL
    assert spec.application_id == APPLICATION_ID == APPLICATION_IDS["sessions"]
    assert spec.format_generation == FORMAT_GENERATION == 1
    assert FTS_STORAGE_VERSION == 1
    assert spec.snapshot_facts is not None
    assert set(spec.snapshot_facts.queries) == {
        "session_count",
        "entry_count",
        "latest_history_revision",
        "latest_state_revision",
    }


def test_opening_registers_the_database_and_keeps_its_identity_in_kernel_meta(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.close()

    marker = read_marker(tmp_path)
    assert marker is not None
    entry = marker.databases["sessions"]
    with closing(sqlite3.connect(tmp_path / "sessions.db")) as connection:
        identity = dict(connection.execute("SELECT key, value FROM kernel_meta").fetchall())
        store_meta = {str(row[0]) for row in connection.execute("SELECT key FROM store_meta")}
    assert identity["database_id"] == entry.database_id
    assert identity["database_name"] == "sessions"
    assert "database_id" not in store_meta
    assert "projection_version" not in store_meta


def test_session_writes_keep_owner_errors_and_classify_storage_failures(tmp_path: Path) -> None:
    # Corrupt Session rows are a database corruption, never an owner error.
    assert issubclass(SessionStoreCorruptError, DatabaseCorruptError)
    assert not issubclass(SessionStoreCorruptError, ChatSessionError)
    store = SessionStore(tmp_path / "sessions.db")
    address = _address("duplicate")
    try:
        store.create(address)
        with pytest.raises(ChatSessionError) as duplicate:
            store.create(address)
        assert not isinstance(duplicate.value, DatabaseError)

        for message, expected in (
            ("database disk image is malformed", DatabaseCorruptError),
            ("attempt to write a readonly database", DatabaseUnavailableError),
        ):
            key = f"rollback-{expected.__name__}"

            def fail(
                connection: sqlite3.Connection, key: str = key, message: str = message
            ) -> None:
                connection.execute("INSERT INTO store_meta(key, value) VALUES (?, 'value')", (key,))
                raise sqlite3.DatabaseError(message)

            with pytest.raises(expected):
                store._execute_write(fail)
            assert store._writer.in_transaction is False
            assert (
                store._writer.execute(
                    "SELECT value FROM store_meta WHERE key = ?", (key,)
                ).fetchone()
                is None
            )
    finally:
        store.close()


def test_the_store_opens_after_an_additive_schema_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.ensure_live(_address("session"))
    store.close()
    monkeypatch.setattr(
        _store_schema,
        "SCHEMA_SQL",
        SCHEMA_SQL
        + "\nALTER TABLE sessions ADD COLUMN reconcile_probe INTEGER NOT NULL DEFAULT 0;",
    )

    store = SessionStore(tmp_path / "sessions.db")
    try:
        probe = store._read(
            lambda connection: connection.execute(
                "SELECT reconcile_probe FROM sessions WHERE session_id = 'session'"
            ).fetchone()
        )
        assert tuple(probe) == (0,)
    finally:
        store.close()


def test_the_store_refuses_a_database_of_a_newer_generation(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    SessionStore(database).close()
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(f"PRAGMA user_version = {FORMAT_GENERATION + 1}")
        connection.commit()
    original = database.read_bytes()

    with pytest.raises(DatabaseFormatError, match="newer than this vBot supports"):
        SessionStore(database)
    assert database.read_bytes() == original


def test_a_populated_database_keeps_every_row_when_tables_are_added(tmp_path: Path) -> None:
    """Opening a copied populated database adds missing tables and preserves every row."""
    database = tmp_path / "sessions.db"
    store = SessionStore(database)
    address = SessionAddress("project", "agent", "retained")
    try:
        store.ensure_live(address)
        store.replace_metadata(
            address, {"title": "Retained title", "custom": {"unicode": "Gruesse"}}
        )
        store.admit_run(
            address,
            SessionRunAdmission(
                run_id="retained-run", run_kind="user", started_at="2026-05-01T11:59:00Z"
            ),
        )
        store.finish_run(
            address,
            SessionRunCompletion(
                run_id="retained-run",
                status="interrupted",
                timing={
                    "started_at": "2026-05-01T11:59:00Z",
                    "completed_at": "2026-05-01T12:00:00Z",
                    "duration_ms": 60_000,
                },
                iteration_count=1,
            ),
        )
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
        archived = SessionAddress(None, "agent", "archived")
        store.ensure_live(archived)
        store.append_messages(archived, [ChatMessage.user("Retained archived task")])
        store.archive(archived)
    finally:
        store.close()

    dropped = ("session_delivery_receipts", "run_execution_owners", "temporary_session_bindings")
    with closing(sqlite3.connect(database)) as baseline:
        for table in dropped:
            baseline.execute(f"DROP TABLE {table}")
        baseline.commit()
        tables = [
            str(row[0])
            for row in baseline.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        columns = {
            table: [str(row[1]) for row in baseline.execute(f'PRAGMA table_info("{table}")')]
            for table in tables
        }
        previous_rows = {
            table: sorted(
                baseline.execute(f'SELECT {", ".join(columns[table])} FROM "{table}"').fetchall(),
                key=repr,
            )
            for table in tables
            if columns[table]
        }
    copy_directory = tmp_path / "copied"
    copy_directory.mkdir()
    copied_database = copy_directory / "sessions.db"
    with (
        closing(sqlite3.connect(database)) as source,
        closing(sqlite3.connect(copied_database)) as destination,
    ):
        source.backup(destination)
    shutil.copy2(tmp_path / "data-store.json", copy_directory / "data-store.json")

    SessionStore(copied_database).close()

    with closing(sqlite3.connect(copied_database)) as verification:
        assert verification.execute("PRAGMA user_version").fetchone() == (FORMAT_GENERATION,)
        assert verification.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,)
        for table, rows in previous_rows.items():
            selected = verification.execute(
                f'SELECT {", ".join(columns[table])} FROM "{table}"'
            ).fetchall()
            assert sorted(selected, key=repr) == rows, table
        for table in dropped:
            assert verification.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_a_damaged_database_without_a_snapshot_raises_and_is_preserved(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    SessionStore(database).close()
    database.write_bytes(b"X" * 8192)

    with pytest.raises(DatabaseCorruptError):
        SessionStore(database)
    assert database.read_bytes() == b"X" * 8192
    assert not (tmp_path / "quarantine").exists()


def test_a_damaged_database_is_restored_from_the_data_snapshot(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        manager.create("coder", session_id="kept").append(ChatMessage.user("kept history"))
        snapshot = create_data_snapshot(tmp_path, reason="test", databases=(manager.database,))
    finally:
        manager.close()
    assert snapshot is not None
    manifest = json.loads((snapshot / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["members"]["sessions"]["facts"]["session_count"] == 1
    assert manifest["members"]["sessions"]["facts"]["entry_count"] == 1
    (tmp_path / "sessions.db").write_bytes(b"X" * 8192)

    manager = ChatSessionManager(tmp_path)
    try:
        messages = manager.get(_address("kept")).load()
    finally:
        manager.close()
    assert [message.content for message in messages] == ["kept history"]
    (quarantine,) = (tmp_path / "quarantine" / "sessions").iterdir()
    assert re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{8}", quarantine.name)


def test_search_index_health_is_reported_as_owner_details(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        manager.create("coder", session_id="indexed").append(ChatMessage.user("searchable"))
        healthy = manager.database.health()
    finally:
        manager.close()
    assert healthy.state == "healthy"
    assert healthy.details["fts"]["state"] == "healthy"

    with closing(sqlite3.connect(tmp_path / "sessions.db")) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO store_meta(key, value) VALUES (?, 'degraded')",
            (FTS_STALE_KEY,),
        )
        connection.commit()
    status = data_store_status(tmp_path, specs=(session_database_spec(tmp_path / "sessions.db"),))

    member = status["databases"]["sessions"]
    assert member["state"] == "degraded"
    assert member["reason"].startswith("Session search: ")
    assert member["details"]["fts"]["state"] == "degraded"
