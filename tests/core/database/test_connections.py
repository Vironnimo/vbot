"""Connection ownership, journal policy, retry, bounded readers and metrics."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path

import pytest

from core.database import (
    Database,
    DatabaseUnavailableError,
    open_database,
    write_bootstrap_marker,
)
from core.database import _connections as connections_module
from core.database import _runtime as runtime_module
from core.database._connections import (
    copy_database,
    readonly_sqlite_uri,
    tracked_connection_count,
)
from core.database._runtime import (
    READ_CONNECTION_LIMIT,
    READER_CACHE_KIB,
    WRITER_CACHE_KIB,
    ConnectionRuntime,
)
from core.performance import PerformanceService
from core.performance.performance import reset_for_tests
from tests.core.database.database_test_support import (
    TEST_APPLICATION_ID,
    add_note,
    notes_spec,
)


def _bare_runtime(path: Path) -> ConnectionRuntime:
    return ConnectionRuntime(
        path,
        name="notes",
        synchronous="FULL",
        application_id=TEST_APPLICATION_ID,
        format_generation=1,
    )


def _open(data_dir: Path) -> Database:
    return open_database(notes_spec(data_dir))


def _count(database: Database) -> int:
    return int(database.writer.execute("SELECT COUNT(*) FROM notes").fetchone()[0])


def test_open_failure_unregisters_the_connection(tmp_path: Path, monkeypatch) -> None:
    runtime = _bare_runtime(tmp_path / "notes.db")
    monkeypatch.setattr(
        runtime_module,
        "apply_wal_with_fallback",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected setup failure")),
    )

    with pytest.raises(RuntimeError, match="injected setup failure"):
        runtime.open_writer()

    assert runtime.live_connection_count() == 0
    assert tracked_connection_count(tmp_path / "notes.db") == 0


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (sqlite3.SQLITE_FULL, "database or disk is full"),
        (sqlite3.SQLITE_NOMEM, "out of memory"),
        (sqlite3.SQLITE_INTERRUPT, "interrupted"),
        (sqlite3.SQLITE_IOERR | (13 << 8), "injected extended access error"),
    ],
)
def test_open_operational_error_never_reports_corruption(
    tmp_path: Path, monkeypatch, code: int, message: str
) -> None:
    runtime = _bare_runtime(tmp_path / "notes.db")
    failure = sqlite3.OperationalError(message)
    failure.sqlite_errorcode = code

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(runtime_module, "apply_wal_with_fallback", fail)
    with pytest.raises(DatabaseUnavailableError) as raised:
        runtime.open_writer()
    assert raised.value.__cause__ is failure
    assert tracked_connection_count(tmp_path / "notes.db") == 0


def test_safe_wal_reset_fallback_is_reported_as_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    database = tmp_path / "safe-fallback.db"
    connection = sqlite3.connect(database)
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 40, 1))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.40.1")
    caplog.set_level(logging.INFO, logger="vbot.database")

    try:
        mode = connections_module.apply_wal_with_fallback(
            connection, db_label="safe-fallback-test.db"
        )
    finally:
        connection.close()

    records = [
        record
        for record in caplog.records
        if record.name == "vbot.database" and "safe-fallback-test.db" in record.message
    ]
    assert mode == "delete"
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].message == (
        "safe-fallback-test.db: using safe journal_mode=DELETE because SQLite 3.40.1 "
        "has the WAL-reset issue"
    )


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
def test_wal_reset_vulnerability_matches_the_official_ranges(
    version_info: tuple[int, int, int], expected: bool
) -> None:
    assert connections_module.is_wal_reset_vulnerable(version_info) is expected
    assert connections_module.required_journal_mode(version_info) == (
        "delete" if expected else "wal"
    )


def test_vulnerable_sqlite_uses_rollback_journal_and_keeps_an_existing_wal(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = notes_spec(data_dir).path
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 51, 3))
    _open(data_dir).close()
    with closing(sqlite3.connect(path)) as probe:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 40, 1))
    database = _open(data_dir)
    try:
        # Never live-downgrade an existing WAL database.
        assert database.wal_active() is True
    finally:
        database.close()

    other = data_dir / "other"
    other.mkdir()
    write_bootstrap_marker(other)
    fresh = _open(other)
    try:
        assert fresh.wal_active() is False
    finally:
        fresh.close()
    with closing(sqlite3.connect(notes_spec(other).path)) as probe:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_busy_transaction_retries_as_one_idempotent_unit(data_dir: Path) -> None:
    database = _open(data_dir)
    attempts = 0

    def write(connection: sqlite3.Connection) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        connection.execute("INSERT INTO notes (body) VALUES ('once')")

    try:
        database.write(write, patience_s=1.0)
        assert attempts == 2
        assert _count(database) == 1
    finally:
        database.close()


def test_busy_write_becomes_unavailable_after_its_patience(data_dir: Path) -> None:
    database = _open(data_dir)

    def always_busy(_connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("database is locked")

    try:
        with pytest.raises(DatabaseUnavailableError, match="stayed busy"):
            database.write(always_busy, patience_s=0.1)
        assert database.writer.in_transaction is False
    finally:
        database.close()


def test_failed_write_rolls_back_before_preserving_an_unclassified_error(data_dir: Path) -> None:
    database = _open(data_dir)

    def write(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO notes (body) VALUES ('transient')")
        raise sqlite3.IntegrityError("injected unexpected constraint")

    try:
        with pytest.raises(sqlite3.IntegrityError, match="unexpected constraint"):
            database.write(write)
        assert database.writer.in_transaction is False
        assert _count(database) == 0
    finally:
        database.close()


def test_reader_permits_are_bounded_and_released_on_failure(data_dir: Path) -> None:
    database = _open(data_dir)
    runtime = database._runtime
    try:
        readers = [runtime._checkout_reader() for _ in range(READ_CONNECTION_LIMIT + 2)]
        active = [reader for reader in readers if reader is not None]
        assert len(active) == READ_CONNECTION_LIMIT
        assert runtime.reader_stats()[1] >= 2

        for reader in active:
            runtime._close_reader(reader)

        with pytest.raises(KeyboardInterrupt), database.read():
            raise KeyboardInterrupt
        assert runtime.reader_stats()[0] == 0
    finally:
        database.close()
    assert database.live_connection_count() == 0


def test_writer_and_readers_keep_bounded_page_caches(data_dir: Path) -> None:
    database = _open(data_dir)
    runtime = database._runtime
    reader = runtime._checkout_reader()
    try:
        assert reader is not None
        assert database.writer.execute("PRAGMA cache_size").fetchone()[0] == -WRITER_CACHE_KIB
        assert reader.execute("PRAGMA cache_size").fetchone()[0] == -READER_CACHE_KIB
        # The documented ceiling: every pooled reader plus the writer, full.
        assert WRITER_CACHE_KIB + READ_CONNECTION_LIMIT * READER_CACHE_KIB == 192 * 1024
    finally:
        if reader is not None:
            runtime._close_reader(reader)
        database.close()
    assert database.live_connection_count() == 0


def test_reader_open_failure_releases_permit_and_can_retry(data_dir: Path, monkeypatch) -> None:
    database = _open(data_dir)
    runtime = database._runtime
    real_connect = runtime_module.connect_tracked
    calls = 0

    def connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "connect_tracked", connect)
    try:
        assert runtime._checkout_reader() is None
        assert runtime.reader_stats()[0] == 0
        assert database.live_connection_count() == 1

        runtime._reader_open_failed_at = 0
        reader = runtime._checkout_reader()
        assert reader is not None
        runtime._close_reader(reader)
    finally:
        database.close()
    assert database.live_connection_count() == 0


def test_checkpoint_with_a_reader_keeps_the_runtime_usable(data_dir: Path) -> None:
    database = _open(data_dir)
    runtime = database._runtime
    reader = runtime._checkout_reader()
    try:
        assert reader is not None
        reader.execute("BEGIN")
        reader.execute("SELECT 1").fetchone()
        database.checkpoint()
        reader.execute("ROLLBACK")
        assert reader.execute("SELECT 1").fetchone()[0] == 1
    finally:
        if reader is not None:
            runtime._close_reader(reader)
        database.close()
    assert database.live_connection_count() == 0


def test_readonly_connections_escape_special_path_characters(tmp_path: Path) -> None:
    data_dir = tmp_path / "data#snapshot%source"
    data_dir.mkdir()
    write_bootstrap_marker(data_dir)
    database = _open(data_dir)
    runtime = database._runtime
    reader = None
    try:
        uri = readonly_sqlite_uri(database.path)
        assert "%23" in uri
        assert "%25" in uri
        reader = runtime._checkout_reader()
        assert reader is not None
        assert reader.execute("SELECT 1").fetchone()[0] == 1
        runtime._close_reader(reader)
        reader = None
        backup = data_dir / "backup#%copy.db"
        assert database.backup(backup) is True
        with closing(sqlite3.connect(readonly_sqlite_uri(backup), uri=True)) as copy:
            assert copy.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        if reader is not None:
            runtime._close_reader(reader)
        database.close()


def test_cancelled_backup_leaves_no_partial_database(data_dir: Path) -> None:
    database = _open(data_dir)
    destination = data_dir / "cancelled.db"
    try:
        add_note(database, "hello")

        assert database.backup(destination, cancelled=lambda: True) is False
        assert destination.exists() is False
        assert not list(data_dir.glob(".cancelled.db.*.tmp"))
    finally:
        database.close()


def test_close_releases_every_connection_and_refuses_further_work(data_dir: Path) -> None:
    database = _open(data_dir)
    database.close()

    assert database.is_closed() is True
    assert tracked_connection_count(database.path) == 0
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
    finally:
        database.close()


@pytest.mark.asyncio
async def test_async_work_on_a_closed_database_is_unavailable(data_dir: Path) -> None:
    database = _open(data_dir)
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
        with database.read() as connection:
            bodies = [
                row[0] for row in connection.execute("SELECT body FROM notes ORDER BY note_id")
            ]
        assert bodies == (["held", "next"] if operation == "write" else ["held"])
    finally:
        release.set()
        await asyncio.gather(writer, return_exceptions=True)
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["close", "cancel"])
async def test_async_admission_interrupted_before_dispatch_never_starts_work(
    data_dir: Path, monkeypatch, interruption: str
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
async def test_async_callable_runtime_error_is_preserved_while_open(data_dir: Path) -> None:
    database = _open(data_dir)
    failure = RuntimeError("operation failure")

    def fail() -> None:
        raise failure

    try:
        with pytest.raises(RuntimeError) as raised:
            await database.run_async(fail)
        assert raised.value is failure
    finally:
        database.close()


@pytest.mark.asyncio
async def test_cancelling_async_write_waits_for_its_transaction(data_dir: Path) -> None:
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


def _filled_database(path: Path) -> None:
    # Many small rows give the copy many progress-handler polls.
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE filler(value BLOB NOT NULL)")
        connection.execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 50000) "
            "INSERT INTO filler SELECT randomblob(64) FROM n"
        )
        connection.commit()


def test_copy_database_cancelled_mid_copy_removes_partial_output(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    _filled_database(source_path)
    destination = tmp_path / "copy.db"
    polls = 0
    partial_output_seen = False

    def cancelled() -> bool:
        nonlocal polls, partial_output_seen
        polls += 1
        if polls == 2:
            partial_output_seen = destination.exists()
        return polls >= 2

    with closing(
        sqlite3.connect(readonly_sqlite_uri(source_path), uri=True, isolation_level=None)
    ) as source:
        assert copy_database(source, destination, cancelled=cancelled) is False
        assert partial_output_seen is True
        assert destination.exists() is False
        polls_after_cancel = polls
        # The connection stays usable and no longer consults the cancelled progress handler.
        assert copy_database(source, destination) is True
        assert polls == polls_after_cancel
    with closing(sqlite3.connect(readonly_sqlite_uri(destination), uri=True)) as copy:
        assert copy.execute("SELECT COUNT(*) FROM filler").fetchone()[0] == 50000
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert copy.execute("PRAGMA freelist_count").fetchone()[0] == 0


def test_copy_database_is_one_read_that_concurrent_commits_neither_restart_nor_tear(
    tmp_path: Path,
) -> None:
    # A stepped backup restarts whenever another connection commits between steps;
    # this copy must finish from its first snapshot while the writer keeps committing.
    source_path = tmp_path / "source.db"
    _filled_database(source_path)
    polls = 0
    # One writing connection only, so WAL is safe even on a WAL-reset-vulnerable build.
    with closing(sqlite3.connect(source_path, isolation_level=None)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA busy_timeout=0")

        def commit_during_copy() -> bool:
            nonlocal polls
            polls += 1
            writer.execute("INSERT INTO filler(value) VALUES (x'00')")
            return False

        destination = tmp_path / "copy.db"
        with closing(
            sqlite3.connect(readonly_sqlite_uri(source_path), uri=True, isolation_level=None)
        ) as source:
            assert copy_database(source, destination, cancelled=commit_during_copy) is True
        committed = writer.execute("SELECT COUNT(*) FROM filler").fetchone()[0]

    assert polls >= 2
    assert committed == 50000 + polls
    with closing(sqlite3.connect(readonly_sqlite_uri(destination), uri=True)) as copy:
        assert copy.execute("SELECT COUNT(*) FROM filler").fetchone()[0] == 50000
        assert copy.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_copy_database_refuses_an_existing_destination(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    _filled_database(source_path)
    destination = tmp_path / "copy.db"
    Path(f"{destination}-journal").write_bytes(b"evidence")

    with (
        closing(
            sqlite3.connect(readonly_sqlite_uri(source_path), uri=True, isolation_level=None)
        ) as source,
        pytest.raises(FileExistsError),
    ):
        copy_database(source, destination)

    assert Path(f"{destination}-journal").read_bytes() == b"evidence"
    assert destination.exists() is False


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
        with database.read() as connection:
            connection.execute("SELECT COUNT(*) FROM notes").fetchone()
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
