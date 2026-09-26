"""Canonical and disposable profiles, declarations and application identities."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from core.database import (
    APPLICATION_IDS,
    CANONICAL,
    DISPOSABLE,
    DatabaseCorruptError,
    DatabaseSchemaMismatchError,
    DatabaseSpec,
    DatabaseUnavailableError,
    DisposableDatabase,
    open_database,
    projection_failure,
    read_marker,
)
from core.database.spec import canonical_relative_path
from core.sessions._store_schema import session_database_spec
from tests.core.database.database_test_support import (
    NOTES_SCHEMA_SQL,
    TEST_APPLICATION_ID,
    add_note,
    note_bodies,
    notes_spec,
    projection_spec,
    raw_execute,
)


def _synchronous(database) -> int:
    return int(database.writer.execute("PRAGMA synchronous").fetchone()[0])


def test_canonical_databases_sync_fully_and_disposable_ones_normally(
    data_dir: Path, tmp_path: Path
) -> None:
    canonical = open_database(notes_spec(data_dir))
    disposable = open_database(projection_spec(tmp_path / "index" / "notes-index.db"))
    try:
        assert _synchronous(canonical) == 2
        assert _synchronous(disposable) == 1
        assert int(canonical.writer.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    finally:
        canonical.close()
        disposable.close()


def test_disposable_databases_need_no_marker_and_are_never_registered(
    data_dir: Path, tmp_path: Path
) -> None:
    unmanaged = open_database(projection_spec(tmp_path / "anywhere" / "index.db"))
    unmanaged.close()
    inside = open_database(projection_spec(data_dir / "recall" / "index.db"))
    inside.close()

    marker = read_marker(data_dir)
    assert marker is not None
    assert marker.databases == {}
    assert inside.data_dir is None


def _identity(path: Path) -> dict[str, str]:
    with closing(sqlite3.connect(path)) as connection:
        return dict(connection.execute("SELECT key, value FROM kernel_meta").fetchall())


def test_a_projection_version_change_discards_and_rebuilds(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    database = open_database(projection_spec(path))
    add_note(database, "derived")
    database.close()
    Path(f"{path}-journal").write_bytes(b"")

    rebuilt = open_database(projection_spec(path, projection_version=2))
    try:
        assert note_bodies(rebuilt) == []
    finally:
        rebuilt.close()
    assert _identity(path)["projection_version"] == "2"
    assert rebuilt.database_id != database.database_id


@pytest.mark.parametrize("damage", ["foreign", "newer", "garbage"])
def test_a_foreign_newer_or_damaged_projection_is_discarded(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "index.db"
    database = open_database(projection_spec(path))
    add_note(database, "derived")
    database.close()
    if damage == "foreign":
        raw_execute(path, "PRAGMA application_id = 1")
    elif damage == "newer":
        raw_execute(
            path,
            "PRAGMA user_version = 2",
            "UPDATE kernel_meta SET value = '2' WHERE key = 'format_generation'",
        )
    else:
        path.write_bytes(b"X" * 8192)

    rebuilt = open_database(projection_spec(path))
    try:
        assert note_bodies(rebuilt) == []
    finally:
        rebuilt.close()


def test_a_busy_projection_is_unavailable_and_never_discarded(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    database = open_database(projection_spec(path))
    add_note(database, "derived")
    database.close()
    raw_execute(path, "PRAGMA journal_mode = DELETE")

    with closing(sqlite3.connect(path, isolation_level=None)) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("INSERT INTO notes (body) VALUES ('uncommitted')")
        with pytest.raises(DatabaseUnavailableError):
            open_database(projection_spec(path, projection_version=2))
        holder.execute("ROLLBACK")

    kept = open_database(projection_spec(path))
    try:
        assert note_bodies(kept) == ["derived"]
    finally:
        kept.close()


def test_a_projection_open_in_this_process_is_never_deleted(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    database = open_database(projection_spec(path))
    try:
        add_note(database, "in use")
        with pytest.raises(DatabaseUnavailableError, match="still open"):
            open_database(projection_spec(path, projection_version=2))
        assert note_bodies(database) == ["in use"]
    finally:
        database.close()


def test_connection_setup_prepares_the_writer_and_every_pooled_reader(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    prepared: list[sqlite3.Connection] = []

    def setup(connection: sqlite3.Connection) -> None:
        connection.create_function("projection_marker", 0, lambda: "ready")
        prepared.append(connection)

    open_database(projection_spec(path, connection_setup=setup)).close()
    # Pooled readers exist only in WAL mode, which the kernel keeps on a file
    # that already uses it.
    raw_execute(path, "PRAGMA journal_mode = WAL")
    prepared.clear()
    database = open_database(projection_spec(path, connection_setup=setup))
    try:
        assert database.writer.execute("SELECT projection_marker()").fetchone()[0] == "ready"
        assert database.wal_active()
        with database.read() as connection:
            assert connection is not database.writer
            assert connection.execute("SELECT projection_marker()").fetchone()[0] == "ready"
        assert len(prepared) == 2
    finally:
        database.close()


def test_a_disposable_database_is_discarded_at_runtime_and_rebuilt_empty(
    tmp_path: Path,
) -> None:
    projection = DisposableDatabase(projection_spec(tmp_path / "index.db"))
    first = projection.get()
    add_note(first, "derived")
    assert projection.get() is first

    projection.discard()

    assert first.is_closed()
    rebuilt = projection.get()
    try:
        assert rebuilt is not first
        assert note_bodies(rebuilt) == []
        assert rebuilt.database_id != first.database_id
    finally:
        projection.close()
    with pytest.raises(DatabaseUnavailableError, match="closed"):
        projection.get()


def _worker_thread() -> str:
    return threading.current_thread().name


@pytest.mark.asyncio
async def test_a_disposable_database_serves_every_handle_from_one_worker_pool(
    tmp_path: Path,
) -> None:
    projection = DisposableDatabase(projection_spec(tmp_path / "index.db"))
    try:
        first = await projection.get_async()
        threads = {await projection.run_async(_worker_thread)}
        threads.add(await first.run_async(_worker_thread))

        await projection.discard_async()
        rebuilt = await projection.get_async()
        threads.add(await rebuilt.read_async(lambda _connection: _worker_thread()))

        assert rebuilt is not first
        assert all(name.startswith("vbot-db-notes_index") for name in threads)
        # A discarded handle refuses work although the shared pool lives on.
        with pytest.raises(DatabaseUnavailableError):
            await first.run_async(_worker_thread)
    finally:
        projection.close()


@pytest.mark.asyncio
async def test_work_on_a_closed_disposable_database_is_unavailable(tmp_path: Path) -> None:
    projection = DisposableDatabase(projection_spec(tmp_path / "index.db"))
    handle = await projection.get_async()
    projection.close()

    assert projection.is_closed() is True
    assert handle.is_closed() is True
    for work in (
        lambda: projection.run_async(_worker_thread),
        projection.get_async,
        projection.discard_async,
        lambda: handle.read_async(lambda connection: connection.execute("SELECT 1").fetchone()),
    ):
        with pytest.raises(DatabaseUnavailableError):
            await work()


@pytest.mark.asyncio
async def test_cached_disposable_handle_does_not_block_the_loop_during_a_write(
    tmp_path: Path,
) -> None:
    projection = DisposableDatabase(projection_spec(tmp_path / "index.db"))
    database = await projection.get_async()
    started: Future[None] = Future()
    release = threading.Event()

    def hold_write(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO notes (body) VALUES ('derived')")
        started.set_result(None)
        assert release.wait(timeout=10), "the Event Loop could not release the transaction"

    writer = asyncio.create_task(database.write_async(hold_write))
    try:
        await asyncio.wait_for(asyncio.wrap_future(started), timeout=10)
        asyncio.get_running_loop().call_soon(release.set)
        assert await projection.get_async() is database
        await writer
        assert note_bodies(database) == ["derived"]
    finally:
        release.set()
        await asyncio.gather(writer, return_exceptions=True)
        projection.close()


def test_a_disposable_database_open_in_another_handle_is_never_discarded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "index.db"
    projection = DisposableDatabase(projection_spec(path))
    other = open_database(projection_spec(path))
    try:
        add_note(other, "in use")
        with pytest.raises(DatabaseUnavailableError, match="still open"):
            projection.discard()
        assert note_bodies(other) == ["in use"]
    finally:
        other.close()
        projection.close()


def test_projection_failures_separate_contention_unavailability_and_damage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "index.db"
    database = open_database(projection_spec(path))
    try:
        with closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as holder:
            holder.execute("BEGIN EXCLUSIVE")
            with pytest.raises(DatabaseUnavailableError) as busy:
                add_note_with_patience(database, patience_s=0.1)
            holder.execute("ROLLBACK")
        with pytest.raises(sqlite3.OperationalError) as missing:
            database.write(lambda connection: connection.execute("SELECT * FROM missing"))
    finally:
        database.close()

    assert projection_failure(busy.value) == "busy"
    assert projection_failure(sqlite3.OperationalError("database is locked")) == "busy"
    assert projection_failure(DatabaseUnavailableError("index is closed")) == "unavailable"
    assert projection_failure(sqlite3.OperationalError("database or disk is full")) == (
        "unavailable"
    )
    assert projection_failure(missing.value) == "rebuild"
    assert projection_failure(DatabaseCorruptError("damaged")) == "rebuild"
    assert projection_failure(DatabaseSchemaMismatchError("index", "table notes", "differs")) == (
        "rebuild"
    )
    assert projection_failure(sqlite3.IntegrityError("UNIQUE constraint failed")) == "rebuild"
    assert projection_failure(sqlite3.ProgrammingError("closed database")) is None
    assert projection_failure(ValueError("owner bug")) is None


def add_note_with_patience(database, *, patience_s: float) -> None:
    database.write(
        lambda connection: connection.execute("INSERT INTO notes (body) VALUES ('late')"),
        patience_s=patience_s,
    )


def test_spec_validation_rejects_inconsistent_declarations(tmp_path: Path) -> None:
    base: dict[str, Any] = {
        "name": "notes",
        "path": tmp_path / "notes.db",
        "application_id": TEST_APPLICATION_ID,
        "format_generation": 1,
        "schema_sql": NOTES_SCHEMA_SQL,
    }
    with pytest.raises(ValueError, match="projection_version"):
        DatabaseSpec(profile=DISPOSABLE, **base)
    with pytest.raises(ValueError, match="projection_version"):
        DatabaseSpec(profile=CANONICAL, projection_version=1, **base)
    with pytest.raises(ValueError, match="application_id"):
        DatabaseSpec(profile=CANONICAL, **{**base, "application_id": 0})
    with pytest.raises(ValueError, match="format_generation"):
        DatabaseSpec(profile=CANONICAL, **{**base, "format_generation": 0})
    with pytest.raises(ValueError, match="invalid database name"):
        DatabaseSpec(profile=CANONICAL, **{**base, "name": "Notes"})
    with pytest.raises(ValueError, match="unique"):
        from core.database import Migration

        DatabaseSpec(profile=CANONICAL, migrations=(Migration("a"), Migration("a")), **base)


@pytest.mark.parametrize(
    ("name", "relative"),
    [
        ("sessions", "sessions.db"),
        ("provider_usage", "provider-usage.db"),
        ("ext.swarm.board", "extension-data/swarm/board.db"),
    ],
)
def test_canonical_names_map_to_fixed_paths(name: str, relative: str) -> None:
    assert canonical_relative_path(name).as_posix() == relative


def test_application_ids_are_distinct_and_the_session_database_uses_its_own() -> None:
    assert len(set(APPLICATION_IDS.values())) == len(APPLICATION_IDS)
    assert TEST_APPLICATION_ID not in APPLICATION_IDS.values()
    assert APPLICATION_IDS["sessions"] == 0x56425353
    spec = session_database_spec(Path("data") / "sessions.db")
    assert spec.application_id == APPLICATION_IDS["sessions"]
    assert spec.profile == CANONICAL
    assert spec.format_generation == 1
