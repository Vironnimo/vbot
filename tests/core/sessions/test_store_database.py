"""The Session database on the shared database kernel."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
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
    list_data_snapshots,
    read_marker,
    read_snapshot_health,
)
from core.database import _connections as connections_module
from core.database._connections import readonly_sqlite_uri
from core.database.snapshots import SNAPSHOT_MANIFEST_NAME
from core.sessions import ChatSessionManager, SessionAddress, _store_schema
from core.sessions._store_schema import session_database_spec
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
        store.record_terminal_run(
            address,
            run_id="retained-run",
            status="interrupted",
            timestamp="2026-05-01T12:00:00.000000Z",
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


@pytest.fixture(params=["delete", "wal"])
def pinned_journal_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Open databases in one journal mode regardless of the SQLite build.

    Forcing WAL on a WAL-reset-vulnerable build is safe here: the Runtime's single
    writer is the only connection that writes or checkpoints.
    """
    mode = str(request.param)
    monkeypatch.setattr(connections_module, "is_wal_reset_vulnerable", lambda _version: False)
    monkeypatch.setattr(connections_module, "required_journal_mode", lambda _version: mode)
    return mode


def test_online_snapshot_completes_while_runs_keep_committing(
    tmp_path: Path, pinned_journal_mode: str
) -> None:
    # Generous budgets below the 30 s pytest timeout keep a hang readable on a loaded box.
    deadline = time.monotonic() + 18.0

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    manager = ChatSessionManager(tmp_path)
    address = _address("busy")
    commits = 0
    failures: list[BaseException] = []
    progressed = threading.Condition()
    stop = threading.Event()

    def fill(connection: sqlite3.Connection) -> None:
        # About 24 MB, so commits overlap the copy and its verification.
        connection.execute("CREATE TABLE snapshot_filler(value BLOB NOT NULL) STRICT")
        connection.execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 6000) "
            "INSERT INTO snapshot_filler SELECT randomblob(4000) FROM n"
        )

    def write() -> None:
        nonlocal commits
        index = 0
        while not stop.wait(0.1):
            index += 1
            try:
                manager.get(address).append(ChatMessage.user(f"message {index}"))
                # Completion activity has the shortest busy budget (0.5 s).
                manager.record_terminal_run(
                    address, f"run-{index}", "completed", "2026-01-01T00:00:00+00:00"
                )
            except BaseException as exc:
                with progressed:
                    failures.append(exc)
                    progressed.notify_all()
                return
            with progressed:
                commits += 1
                progressed.notify_all()

    def wait_for_more_commits(count: int) -> tuple[int, int]:
        with progressed:
            before = commits
            progressed.wait_for(
                lambda: commits >= before + count or bool(failures), timeout=remaining()
            )
            return before, commits

    outcome: list[Path | BaseException | None] = []

    def snapshot() -> None:
        try:
            outcome.append(
                create_data_snapshot(tmp_path, reason="test", databases=(manager.database,))
            )
        except BaseException as exc:
            outcome.append(exc)

    writer = threading.Thread(target=write, daemon=True)
    snapshotter = threading.Thread(target=snapshot, daemon=True)
    try:
        manager.create("coder", session_id=address.session_id)
        manager._store._execute_write(fill)
        with closing(
            sqlite3.connect(readonly_sqlite_uri(tmp_path / "sessions.db"), uri=True)
        ) as probe:
            assert probe.execute("PRAGMA journal_mode").fetchone()[0] == pinned_journal_mode
        writer.start()
        _, started = wait_for_more_commits(1)
        snapshotter.start()
        snapshotter.join(remaining())
        snapshot_finished = not snapshotter.is_alive()
        after_snapshot, finished = wait_for_more_commits(2)
    finally:
        stop.set()
        writer.join(4.0)
        snapshotter.join(4.0)
        manager.close()

    assert snapshot_finished, "the online snapshot did not finish while Runs kept committing"
    assert failures == []
    assert finished >= after_snapshot + 2, "writes stopped committing after the snapshot"
    published = outcome[0]
    assert isinstance(published, Path), (published, read_snapshot_health(tmp_path))
    assert list_data_snapshots(tmp_path) == [published]
    # A standalone rollback-journal file: verification leaves no WAL sidecars behind.
    assert {path.name for path in published.iterdir()} == {"sessions.db", SNAPSHOT_MANIFEST_NAME}
    manifest = json.loads((published / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert started <= manifest["members"]["sessions"]["facts"]["entry_count"] <= commits


def test_list_history_versions_returns_live_sessions_in_one_call(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        live = manager.create("coder", session_id="live-one")
        live.append(ChatMessage.user("hello"))
        manager.create("coder", session_id="live-two")
        gone = manager.create("coder", session_id="gone")
        gone.delete()

        versions = manager.list_history_versions(
            [_address("live-one"), _address("live-two"), _address("gone")]
        )

        assert set(versions) == {_address("live-one"), _address("live-two")}
        generation_id, revision = versions[_address("live-one")]
        assert isinstance(generation_id, str) and generation_id
        assert revision >= 1
    finally:
        manager.close()


def test_list_history_versions_spans_scopes(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        manager.create("coder", session_id="global-one")
        manager.create("coder", session_id="project-one", project_id="alpha")

        versions = manager.list_history_versions(
            [_address("global-one"), _address("project-one", "alpha")]
        )

        assert set(versions) == {_address("global-one"), _address("project-one", "alpha")}
    finally:
        manager.close()


def test_corrupt_session_rows_are_a_database_corruption() -> None:
    assert issubclass(SessionStoreCorruptError, DatabaseCorruptError)
    assert not issubclass(SessionStoreCorruptError, ChatSessionError)


def test_runtime_session_boundary_has_no_legacy_jsonl_dependency() -> None:
    repository = Path(__file__).resolve().parents[3]
    production_roots = (
        repository / "core" / "sessions",
        repository / "core" / "recall",
        repository / "core" / "runtime",
        repository / "core" / "database",
        repository / "server",
        repository / "cli",
    )
    legacy_reference = re.compile(
        r"(?:session|sessions).{0,100}jsonl|jsonl.{0,100}(?:session|sessions)", re.I
    )

    for root in production_roots:
        for source_path in root.rglob("*.py"):
            source = source_path.read_text(encoding="utf-8")
            assert "jsonl_to_sqlite" not in source_path.name
            assert legacy_reference.search(source) is None, source_path
