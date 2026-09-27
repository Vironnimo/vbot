"""The Database runtime: connections, journal policy, write retry, readers, backups and metrics."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.chat.errors import ChatSessionError
from core.database import (
    Database,
    DatabaseCorruptError,
    DatabaseError,
    DatabaseFormatError,
    DatabaseUnavailableError,
    IncidentConflictError,
    has_live_connection,
    is_wal_reset_vulnerable,
    open_database,
    required_journal_mode,
    write_bootstrap_marker,
)
from core.database._connections import copy_database
from core.performance import PerformanceService
from core.performance.performance import reset_for_tests
from core.sessions.errors import SessionStoreCorruptError
from tests.core.database.database_test_support import (
    add_note,
    add_notes,
    note_bodies,
    note_count,
    notes_spec,
    pin_journal_mode,
    projection_spec,
    raw_execute,
)


def _open(data_dir: Path) -> Database:
    return open_database(notes_spec(data_dir))


def _open_in_wal(data_dir: Path, **changes: Any) -> Database:
    """Open the notes database in WAL mode, which the kernel keeps on a file that uses it."""
    open_database(notes_spec(data_dir)).close()
    raw_execute(notes_spec(data_dir).path, "PRAGMA journal_mode = WAL")
    database = open_database(replace(notes_spec(data_dir), **changes))
    assert database.wal_active()
    return database


@pytest.fixture
def wal_database(data_dir: Path) -> Iterator[Database]:
    database = _open_in_wal(data_dir)
    yield database
    database.close()


def _sqlite_error(kind: type[sqlite3.Error], message: str, code: int | None = None) -> Exception:
    error = kind(message)
    if code is not None:
        error.sqlite_errorcode = code  # type: ignore[attr-defined]
    return error


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("injected setup failure"),
        _sqlite_error(sqlite3.OperationalError, "database or disk is full", sqlite3.SQLITE_FULL),
    ],
    ids=["unclassified", "disk-full"],
)
def test_an_open_failure_closes_every_connection_and_is_never_corruption(
    tmp_path: Path, failure: Exception
) -> None:
    path = tmp_path / "index.db"

    def fail(_connection: sqlite3.Connection) -> None:
        raise failure

    expected = RuntimeError if isinstance(failure, RuntimeError) else DatabaseUnavailableError
    with pytest.raises(expected) as raised:
        open_database(projection_spec(path, connection_setup=fail))

    if expected is DatabaseUnavailableError:
        assert raised.value.__cause__ is failure
    else:
        assert raised.value is failure
    assert not has_live_connection(path)


def test_the_wal_reset_vulnerable_sqlite_ranges() -> None:
    # Affected 3.7.0 through 3.51.2; fixed in 3.51.3 with backports to 3.50.7 and 3.44.6.
    ranges = {
        (3, 6, 99): False,
        (3, 7, 0): True,
        (3, 44, 5): True,
        (3, 44, 6): False,
        (3, 50, 7): False,
        (3, 51, 2): True,
        (3, 51, 3): False,
    }
    for version, vulnerable in ranges.items():
        assert is_wal_reset_vulnerable(version) is vulnerable, version
        assert required_journal_mode(version) == ("delete" if vulnerable else "wal"), version


def test_vulnerable_sqlite_uses_a_rollback_journal_and_keeps_an_existing_wal(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A name of its own: the kernel reports its journal choice once per database.
    spec = notes_spec(data_dir, name="journal_policy")
    pin_journal_mode(monkeypatch, "wal")
    open_database(spec).close()
    with closing(sqlite3.connect(spec.path)) as probe:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 40, 1))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.40.1")
    caplog.set_level(logging.INFO, logger="vbot.database")
    database = open_database(spec)
    try:
        # Never live-downgrade an existing WAL database.
        assert database.wal_active() is True
    finally:
        database.close()

    other = data_dir / "other"
    other.mkdir()
    write_bootstrap_marker(other)
    fresh_spec = notes_spec(other, name="journal_policy")
    fresh = open_database(fresh_spec)
    try:
        assert fresh.wal_active() is False
    finally:
        fresh.close()
    with closing(sqlite3.connect(fresh_spec.path)) as probe:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    label = f"journal_policy ({fresh_spec.path.name})"
    reports = [
        record
        for record in caplog.records
        if record.levelno == logging.INFO and label in record.getMessage()
    ]
    assert [record.getMessage() for record in reports] == [
        f"{label}: using safe journal_mode=DELETE because SQLite 3.40.1 has the WAL-reset issue"
    ]


def test_a_busy_write_retries_as_one_idempotent_unit_until_its_patience(data_dir: Path) -> None:
    database = _open(data_dir)
    attempts = 0

    def busy_once(connection: sqlite3.Connection) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        connection.execute("INSERT INTO notes (body) VALUES ('once')")

    def always_busy(_connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("database is locked")

    try:
        database.write(busy_once, patience_s=1.0)
        assert attempts == 2
        assert note_bodies(database) == ["once"]

        with pytest.raises(DatabaseUnavailableError, match="stayed busy"):
            database.write(always_busy, patience_s=0.0)
        assert database.writer.in_transaction is False
    finally:
        database.close()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            _sqlite_error(sqlite3.DatabaseError, "database disk image is malformed"),
            DatabaseCorruptError,
        ),
        (
            _sqlite_error(sqlite3.DatabaseError, "opaque", sqlite3.SQLITE_NOTADB),
            DatabaseCorruptError,
        ),
        (
            _sqlite_error(sqlite3.OperationalError, "attempt to write a readonly database"),
            DatabaseUnavailableError,
        ),
        (
            _sqlite_error(sqlite3.OperationalError, "opaque", sqlite3.SQLITE_IOERR | (3 << 8)),
            DatabaseUnavailableError,
        ),
        (sqlite3.IntegrityError("UNIQUE constraint failed: notes.note_id"), None),
        (sqlite3.ProgrammingError("Cannot operate on a closed cursor"), None),
        (sqlite3.OperationalError("no such table: missing"), None),
        (ValueError("not a SQLite failure"), None),
    ],
    ids=[
        "corrupt-message",
        "corrupt-code",
        "unavailable-message",
        "unavailable-extended-code",
        "constraint",
        "api-misuse",
        "statement",
        "not-sqlite",
    ],
)
def test_a_failed_write_rolls_back_and_escapes_as_its_kind_or_unchanged(
    data_dir: Path, failure: Exception, expected: type[DatabaseError] | None
) -> None:
    database = _open(data_dir)

    def fail_after_insert(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO notes (body) VALUES ('rolled back')")
        raise failure

    try:
        with pytest.raises(Exception) as raised:
            database.write(fail_after_insert)
        if expected is None:
            # Owners translate the failures they expect; the rest stays visible.
            assert raised.value is failure
        else:
            assert type(raised.value) is expected
            assert str(raised.value) == "notes (notes.db) write failed"
            assert raised.value.__cause__ is failure
        assert database.writer.in_transaction is False
        assert note_bodies(database) == []
    finally:
        database.close()


def test_kernel_errors_are_never_domain_errors() -> None:
    for kind in (
        DatabaseError,
        DatabaseUnavailableError,
        DatabaseCorruptError,
        DatabaseFormatError,
        IncidentConflictError,
    ):
        assert not issubclass(kind, ChatSessionError)
    # Owners may add domain context, but the kind stays a storage failure.
    assert issubclass(SessionStoreCorruptError, DatabaseCorruptError)
    assert not issubclass(SessionStoreCorruptError, ChatSessionError)


def test_readers_are_bounded_permits_with_bounded_page_caches(wal_database: Database) -> None:
    database = wal_database
    assert database.writer.execute("PRAGMA cache_size").fetchone()[0] == -64 * 1024
    with ExitStack() as reads:
        readers = [reads.enter_context(database.read()) for _ in range(8)]
        assert len({id(reader) for reader in readers}) == 8
        assert all(reader is not database.writer for reader in readers)
        assert readers[0].execute("PRAGMA cache_size").fetchone()[0] == -16 * 1024
        # Past its eight permits a read is served by the serialized writer.
        assert reads.enter_context(database.read()) is database.writer
        assert database.reader_stats() == (8, 1)

    # A read that fails closes its reader and releases the permit.
    with pytest.raises(KeyboardInterrupt), database.read():
        raise KeyboardInterrupt
    assert database.reader_stats()[0] == 7
    with ExitStack() as reads:
        readers = [reads.enter_context(database.read()) for _ in range(8)]
        assert all(reader is not database.writer for reader in readers)
    database.close()
    assert database.live_connection_count() == 0


def test_a_failed_reader_open_falls_back_to_the_writer_and_retries_later(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = 0

    def fail_first_reader(_connection: sqlite3.Connection) -> None:
        nonlocal prepared
        prepared += 1
        if prepared == 2:
            raise sqlite3.OperationalError("database is locked")

    database = _open_in_wal(data_dir, connection_setup=fail_first_reader)
    try:
        for _ in range(2):
            with database.read() as connection:
                assert connection is database.writer
        # The failed open released its permit and backs off before the next attempt.
        assert prepared == 2
        assert database.reader_stats()[0] == 0
        assert database.live_connection_count() == 1

        monkeypatch.setattr("core.database._runtime.READ_OPEN_RETRY_SECONDS", 0.0)
        real_connect = sqlite3.connect

        def refuse_readers(target: str, *args: Any, **kwargs: Any) -> Any:
            if "mode=ro" in str(target):
                raise sqlite3.OperationalError("unable to open database file")
            return real_connect(target, *args, **kwargs)

        with database.read() as pooled:
            assert pooled is not database.writer
            assert prepared == 3
            # A reader that cannot even connect falls back the same way.
            with monkeypatch.context() as patched:
                patched.setattr(sqlite3, "connect", refuse_readers)
                with database.read() as connection:
                    assert connection is database.writer
        assert database.reader_stats()[0] == 1
    finally:
        database.close()
    assert database.live_connection_count() == 0


def test_a_checkpoint_with_an_open_reader_keeps_the_runtime_usable(
    wal_database: Database,
) -> None:
    database = wal_database
    add_note(database, "before")
    with database.read() as reader:
        assert reader is not database.writer
        assert reader.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1
        database.checkpoint()
    add_note(database, "after")

    assert note_bodies(database) == ["before", "after"]


def test_readers_and_backups_escape_special_path_characters(tmp_path: Path) -> None:
    data_dir = tmp_path / "data#snapshot%source"
    data_dir.mkdir()
    write_bootstrap_marker(data_dir)
    database = _open_in_wal(data_dir)
    backup = data_dir / "backup#%copy.db"
    try:
        with database.read() as reader:
            assert reader is not database.writer
            assert reader.execute("SELECT 1").fetchone()[0] == 1
        assert database.backup(backup) is True
    finally:
        database.close()
    with closing(sqlite3.connect(backup)) as copy:
        assert copy.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_a_cancelled_backup_leaves_nothing_and_a_later_one_is_a_standalone_copy(
    data_dir: Path,
) -> None:
    database = _open(data_dir)
    destination = data_dir / "copy.db"
    polls = 0
    partial_output_seen = False

    def cancel_mid_copy() -> bool:
        nonlocal polls, partial_output_seen
        polls += 1
        if polls == 2:
            partial_output_seen = bool(list(data_dir.glob(".copy.db.*")))
        return polls >= 2

    try:
        add_notes(database, 5000)  # Enough rows for several progress polls.
        assert database.backup(destination, cancelled=cancel_mid_copy) is False
        assert partial_output_seen is True
        assert destination.exists() is False
        assert list(data_dir.glob(".copy.db.*")) == []

        assert database.backup(destination) is True
        with pytest.raises(DatabaseUnavailableError, match="already exists"):
            database.backup(destination)
    finally:
        database.close()
    with closing(sqlite3.connect(destination)) as copy:
        assert copy.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 5000
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert copy.execute("PRAGMA freelist_count").fetchone()[0] == 0


def test_a_database_copy_never_writes_over_an_existing_file_or_its_sidecars(
    data_dir: Path,
) -> None:
    # Every kernel caller copies to a fresh name, so this guard of the copy
    # helper is unreachable through Database.backup or create_data_snapshot.
    source_path = notes_spec(data_dir).path
    _open(data_dir).close()
    destination = data_dir / "copy.db"
    Path(f"{destination}-journal").write_bytes(b"evidence")

    with (
        closing(sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)) as source,
        pytest.raises(FileExistsError),
    ):
        copy_database(source, destination)

    assert Path(f"{destination}-journal").read_bytes() == b"evidence"
    assert destination.exists() is False


def test_without_wal_every_read_is_a_transaction_on_the_writer(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin_journal_mode(monkeypatch, "delete")
    database = _open(data_dir)
    try:
        assert database.wal_active() is False
        with database.read() as reader:
            assert reader is database.writer
            assert reader.in_transaction is True
        # The caller's own statement errors reach it unchanged, after the rollback.
        with pytest.raises(sqlite3.OperationalError, match="no such column"), database.read() as r:
            r.execute("SELECT missing FROM notes")
        assert database.writer.in_transaction is False
        database.checkpoint()  # Nothing to checkpoint without WAL.
        assert database.reader_stats() == (0, 0)
        assert database.live_connection_count() == 1
    finally:
        database.close()
    database.checkpoint()  # A closed database has nothing to checkpoint either.


def test_close_releases_every_connection_and_refuses_further_work(data_dir: Path) -> None:
    database = _open(data_dir)
    database.close()

    assert database.is_closed() is True
    assert not has_live_connection(database.path)
    with pytest.raises(DatabaseUnavailableError):
        add_note(database, "late")


@pytest.mark.asyncio
async def test_async_work_runs_on_the_database_worker_pool(data_dir: Path) -> None:
    database = _open(data_dir)
    try:
        await database.write_async(
            lambda connection: connection.execute("INSERT INTO notes (body) VALUES ('async')")
        )
        bodies = await database.read_async(
            lambda connection: [row[0] for row in connection.execute("SELECT body FROM notes")]
        )
        assert bodies == ["async"]
        thread = await database.run_async(lambda: threading.current_thread().name)
        assert thread.startswith(f"vbot-db-{database.name}")
        failure = RuntimeError("operation failure")

        def fail() -> None:
            raise failure

        with pytest.raises(RuntimeError) as raised:
            await database.run_async(fail)
        assert raised.value is failure
    finally:
        database.close()

    with pytest.raises(DatabaseUnavailableError):
        await database.read_async(lambda connection: connection.execute("SELECT 1").fetchone())


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["run", "read", "write"])
async def test_async_admission_keeps_the_loop_running_during_a_write(
    data_dir: Path, operation: str
) -> None:
    database = _open(data_dir)
    started: Future[None] = Future()
    release = threading.Event()

    def hold_write(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO notes (body) VALUES ('held')")
        started.set_result(None)
        assert release.wait(timeout=10), "the Event Loop could not release the transaction"

    writer = asyncio.create_task(database.write_async(hold_write))
    try:
        await asyncio.wait_for(asyncio.wrap_future(started), timeout=10)
        # No clock threshold: the callback can run only once admission yields.
        # A regression blocks the loop until the writer's bounded wait fails.
        asyncio.get_running_loop().call_soon(release.set)
        if operation == "run":
            assert await database.run_async(lambda: "ready") == "ready"
        elif operation == "read":
            assert (
                await database.read_async(
                    lambda connection: connection.execute("SELECT body FROM notes").fetchone()[0]
                )
                == "held"
            )
        else:
            await database.write_async(
                lambda connection: connection.execute("INSERT INTO notes (body) VALUES ('next')")
            )
        await writer
        assert note_bodies(database) == (["held", "next"] if operation == "write" else ["held"])
    finally:
        release.set()
        await asyncio.gather(writer, return_exceptions=True)
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["close", "cancel"])
async def test_async_admission_interrupted_before_dispatch_never_starts_work(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    monkeypatch.setattr("core.database.database.IO_WORKERS", 1)
    database = _open(data_dir)
    started: Future[None] = Future()
    release = threading.Event()
    late_work_ran = threading.Event()

    def hold_worker() -> None:
        started.set_result(None)
        assert release.wait(timeout=10)

    first = asyncio.create_task(database.run_async(hold_worker))
    queued: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(asyncio.wrap_future(started), timeout=10)
        queued = asyncio.create_task(database.run_async(late_work_ran.set))
        await asyncio.sleep(0)  # The second call reaches the occupied pool's admission wait.
        assert not queued.done()

        if interruption == "close":
            database.close()
        else:
            queued.cancel()
        release.set()
        await first
        expected_error = (
            DatabaseUnavailableError if interruption == "close" else asyncio.CancelledError
        )
        with pytest.raises(expected_error):
            await queued
        assert not late_work_ran.is_set()
    finally:
        release.set()
        await asyncio.gather(
            first, *([queued] if queued is not None else []), return_exceptions=True
        )
        database.close()


@pytest.mark.asyncio
async def test_cancelling_an_async_write_waits_for_its_transaction(data_dir: Path) -> None:
    database = _open(data_dir)
    started: Future[None] = Future()
    release = threading.Event()

    def hold_write(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO notes (body) VALUES ('committed')")
        started.set_result(None)
        assert release.wait(timeout=10)

    writer = asyncio.create_task(database.write_async(hold_write))
    try:
        await asyncio.wait_for(asyncio.wrap_future(started), timeout=10)
        writer.cancel()
        await asyncio.sleep(0)
        writer.cancel()
        await asyncio.sleep(0)
        assert not writer.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await writer
        assert (
            await database.read_async(
                lambda connection: connection.execute("SELECT body FROM notes").fetchone()[0]
            )
            == "committed"
        )
    finally:
        release.set()
        await asyncio.gather(writer, return_exceptions=True)
        database.close()


@pytest.mark.asyncio
async def test_transactions_are_measured_per_database_on_the_sqlite_track(
    data_dir: Path,
) -> None:
    reset_for_tests()
    performance = PerformanceService(data_dir / "performance")
    database = _open(data_dir)
    attempts = 0

    def write(connection: sqlite3.Connection) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        connection.execute("INSERT INTO notes (body) VALUES ('measured')")

    try:
        performance.start_recording()
        database.write(write, patience_s=1.0)
        assert note_count(database) == 1
        result = await performance.stop_recording()
        metrics = (await performance.snapshot())["metrics"]
    finally:
        database.close()
        await performance.aclose()
        reset_for_tests()

    # The busy attempt is measured as well; it spent real time inside the transaction.
    assert metrics["sqlite.notes.write"]["count"] == 2
    assert metrics["sqlite.notes.write_wait"]["count"] == 2
    assert metrics["sqlite.notes.read"]["count"] == 1
    events = json.loads(Path(result["trace_path"]).read_text("utf-8"))["traceEvents"]
    tracks = {e["pid"]: e["args"]["name"] for e in events if e["name"] == "process_name"}
    spans = [(tracks[e["pid"]], e["name"]) for e in events if e["ph"] == "X"]
    assert spans.count(("sqlite", "notes write")) == 2
    assert ("sqlite", "notes read") in spans
