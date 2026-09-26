"""The connection owner behind one open database: writer, readers, retry, metrics."""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import random
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

from core.database._connections import (
    BUSY_TIMEOUT_MS,
    apply_wal_with_fallback,
    classified_error,
    classify_unavailable,
    connect_tracked,
    copy_database,
    readonly_sqlite_uri,
    remove_database_files,
    tracked_connection_count,
)
from core.database.errors import (
    DatabaseCorruptError,
    DatabaseError,
    DatabaseUnavailableError,
)
from core.performance import measure, record_span

_LOGGER = logging.getLogger("vbot.database")

WRITE_PATIENCE_S = 20.0
READ_CONNECTION_LIMIT = 8
# Page cache per connection, in KiB. The writer also serves every read in
# rollback-journal mode; pooled readers exist only in WAL mode. Ceiling per
# database: 64 MiB + READ_CONNECTION_LIMIT x 16 MiB = 192 MiB, reached only
# when every connection has read that many distinct pages.
WRITER_CACHE_KIB = 64 * 1024
READER_CACHE_KIB = 16 * 1024
CHECKPOINT_EVERY_N_WRITES = 50
READ_OPEN_RETRY_SECONDS = 60.0
_WRITE_RETRY_MIN_S = 0.020
_WRITE_RETRY_MAX_S = 0.150
_WRITE_RETRY_SLOW_MIN_S = 0.250
_WRITE_RETRY_SLOW_MAX_S = 1.000
_WRITE_RETRY_SLOW_AFTER_S = 2.0
_WAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024
PERFORMANCE_TRACK = "sqlite"
_LOCK_WAIT_SPAN_MIN_MS = 1.0

Synchronous = Literal["FULL", "NORMAL"]


class ConnectionRuntime:
    """Own every live writer and reader connection for one database file.

    One serialized writer runs whole transactions under ``BEGIN IMMEDIATE`` with
    jittered busy retry; a bounded LIFO pool of read-only connections serves
    reads in WAL mode, and the writer serves them in rollback-journal mode.
    Each write attempt is measured as ``sqlite.<name>.write`` after
    ``sqlite.<name>.write_wait`` for the connection lock; each read transaction,
    including the caller's work, as ``sqlite.<name>.read``.
    """

    def __init__(
        self,
        path: Path,
        *,
        name: str,
        synchronous: Synchronous,
        application_id: int,
        format_generation: int,
        connection_setup: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.name = name
        self._synchronous = synchronous
        self._connection_setup = connection_setup
        self._application_id = application_id
        self._format_generation = format_generation
        self._write_metric = f"sqlite.{name}.write"
        self._write_wait_metric = f"sqlite.{name}.write_wait"
        self._read_metric = f"sqlite.{name}.read"
        self._lock = threading.RLock()
        self._writer: sqlite3.Connection | None = None
        self._closed = False
        self._wal_active = False
        self._write_count = 0
        self._readers: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(
            maxsize=READ_CONNECTION_LIMIT
        )
        self._reader_connections: set[sqlite3.Connection] = set()
        self._reader_permits = threading.BoundedSemaphore(READ_CONNECTION_LIMIT)
        self._reader_lock = threading.Lock()
        self._reader_access_closed = False
        self._reader_open_failed_at = 0.0
        self._reader_permit_exhausted = 0

    @property
    def label(self) -> str:
        return f"{self.name} ({self.path.name})"

    def open_writer(
        self, verify: Callable[[sqlite3.Connection], None] | None = None
    ) -> sqlite3.Connection:
        """Open, verify and configure the writer; close it on every failure.

        ``verify`` inspects the file before the journal policy may change it, so
        a foreign or newer database is refused untouched.
        """
        with self._lock:
            if self._writer is not None:
                return self._writer
            if self._closed:
                raise DatabaseUnavailableError(f"{self.label} is closed")
            connection: sqlite3.Connection | None = None
            try:
                connection = connect_tracked(
                    self.path,
                    tracking_path=self.path,
                    isolation_level=None,
                    check_same_thread=False,
                    timeout=BUSY_TIMEOUT_MS / 1000,
                )
                connection.row_factory = sqlite3.Row
                if self._connection_setup is not None:
                    self._connection_setup(connection)
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
                connection.execute(f"PRAGMA cache_size=-{WRITER_CACHE_KIB}")
                if verify is not None:
                    verify(connection)
                self._wal_active = apply_wal_with_fallback(connection, db_label=self.label) == "wal"
                connection.execute(f"PRAGMA synchronous={self._synchronous}")
                connection.execute("PRAGMA wal_autocheckpoint=1000")
                connection.execute(f"PRAGMA journal_size_limit={_WAL_SIZE_LIMIT_BYTES}")
                self._writer = connection
                return connection
            except BaseException as exc:
                if connection is not None:
                    with contextlib.suppress(BaseException):
                        connection.close()
                if isinstance(exc, sqlite3.Error):
                    translated = classified_error(exc, f"{self.label} cannot be opened safely")
                    if translated is None and isinstance(exc, sqlite3.DatabaseError):
                        translated = DatabaseCorruptError(f"{self.label} cannot be opened safely")
                    if translated is not None:
                        raise translated from exc
                raise

    @property
    def writer(self) -> sqlite3.Connection:
        with self._lock:
            if self._writer is None or self._closed:
                raise DatabaseUnavailableError(f"{self.label} is closed")
            return self._writer

    def execute_write(
        self,
        operation: Callable[[sqlite3.Connection], Any],
        *,
        patience_s: float = WRITE_PATIENCE_S,
    ) -> Any:
        """Run one whole idempotent transaction with busy-only retry.

        Classified SQLite corruption and unavailability escape as kernel errors;
        constraint errors, API misuse and the operation's own exceptions reach
        the caller unchanged after the rollback.
        """
        deadline = time.monotonic() + patience_s
        while True:
            try:
                waiting = time.perf_counter()
                with self._lock:
                    record_span(
                        self._write_wait_metric,
                        waiting,
                        track=PERFORMANCE_TRACK,
                        name=f"{self.name} write wait",
                        min_span_ms=_LOCK_WAIT_SPAN_MIN_MS,
                    )
                    connection = self.writer
                    with measure(
                        self._write_metric, track=PERFORMANCE_TRACK, name=f"{self.name} write"
                    ):
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            result = operation(connection)
                            connection.execute("COMMIT")
                        except BaseException:
                            with contextlib.suppress(BaseException):
                                if connection.in_transaction:
                                    connection.execute("ROLLBACK")
                            raise
                    self._write_count += 1
                    checkpoint = self._write_count % CHECKPOINT_EVERY_N_WRITES == 0
                if checkpoint:
                    self.checkpoint()
                return result
            except DatabaseError:
                raise
            except BaseException as exc:
                if classify_unavailable(exc):
                    if self._sleep_before_retry(deadline, patience_s):
                        continue
                    raise DatabaseUnavailableError(
                        f"{self.label} write stayed busy for {patience_s:.1f}s"
                    ) from exc
                translated = classified_error(exc, f"{self.label} write failed")
                if translated is not None:
                    raise translated from exc
                raise

    @staticmethod
    def _sleep_before_retry(deadline: float, patience_s: float) -> bool:
        now = time.monotonic()
        if now >= deadline:
            return False
        elapsed = now - (deadline - patience_s)
        if elapsed >= _WRITE_RETRY_SLOW_AFTER_S:
            delay = random.uniform(_WRITE_RETRY_SLOW_MIN_S, _WRITE_RETRY_SLOW_MAX_S)
        else:
            delay = random.uniform(_WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S)
        time.sleep(min(delay, max(deadline - now, 0.001)))
        return True

    @contextlib.contextmanager
    def read_ctx(self) -> Iterator[sqlite3.Connection]:
        """Yield one bounded read connection, or the serialized writer.

        Errors raised by the caller's statements reach the caller unchanged.
        """
        with (
            measure(self._read_metric, track=PERFORMANCE_TRACK, name=f"{self.name} read"),
            self._read_transaction() as connection,
        ):
            yield connection

    @contextlib.contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._writer is None or self._closed:
                raise DatabaseUnavailableError(f"{self.label} is closed")
            wal_active = self._wal_active
        connection = self._checkout_reader() if wal_active else None
        if connection is None:
            with self._lock:
                writer = self.writer
                writer.execute("BEGIN")
                try:
                    yield writer
                    writer.execute("COMMIT")
                except BaseException:
                    with contextlib.suppress(BaseException):
                        if writer.in_transaction:
                            writer.execute("ROLLBACK")
                    raise
            return
        healthy = True
        try:
            connection.execute("BEGIN")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            healthy = False
            with contextlib.suppress(BaseException):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            raise
        finally:
            if not (healthy and self._return_reader(connection)):
                self._close_reader(connection)

    def _checkout_reader(self) -> sqlite3.Connection | None:
        with self._reader_lock:
            if self._reader_access_closed:
                return None
            if self._reader_open_failed_at and (
                time.monotonic() - self._reader_open_failed_at < READ_OPEN_RETRY_SECONDS
            ):
                return None
        try:
            return self._readers.get_nowait()
        except queue.Empty:
            pass
        if not self._reader_permits.acquire(blocking=False):
            with self._reader_lock:
                self._reader_permit_exhausted += 1
            return None
        connection: sqlite3.Connection | None = None
        try:
            connection = connect_tracked(
                readonly_sqlite_uri(self.path),
                tracking_path=self.path,
                uri=True,
                isolation_level=None,
                check_same_thread=False,
                timeout=5.0,
            )
            connection.row_factory = sqlite3.Row
            if self._connection_setup is not None:
                self._connection_setup(connection)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA cache_size=-{READER_CACHE_KIB}")
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            if application_id != self._application_id:
                raise DatabaseCorruptError(f"{self.label}: reader application identity mismatch")
            generation = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if generation != self._format_generation:
                raise DatabaseCorruptError(f"{self.label}: reader format generation mismatch")
            with self._reader_lock:
                self._reader_connections.add(connection)
            return connection
        except BaseException:
            if connection is not None:
                with contextlib.suppress(BaseException):
                    connection.close()
            with self._reader_lock:
                self._reader_open_failed_at = time.monotonic()
            self._reader_permits.release()
            return None

    def _return_reader(self, connection: sqlite3.Connection) -> bool:
        with self._reader_lock:
            if self._reader_access_closed:
                return False
            try:
                self._readers.put_nowait(connection)
                return True
            except queue.Full:
                return False

    def _close_reader(self, connection: sqlite3.Connection) -> None:
        with contextlib.suppress(BaseException):
            connection.close()
        with self._reader_lock:
            self._reader_connections.discard(connection)
        with contextlib.suppress(ValueError):
            self._reader_permits.release()

    def checkpoint(self) -> None:
        """Run a non-blocking checkpoint without changing journal mode."""
        with self._lock:
            if self._writer is None or self._closed or not self._wal_active:
                return
            try:
                result = self._writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                if result and len(result) > 1 and result[1] > 0:
                    _LOGGER.debug("WAL checkpoint left %s/%s pages pending", result[2], result[1])
            except Exception as exc:
                _LOGGER.warning("WAL checkpoint failed for %s: %s", self.label, exc)

    def backup(
        self,
        destination: Path,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        """Write one consistent, durable copy of the live database to ``destination``.

        The copy is a single ``copy_database`` pass from a read-only connection,
        so committing writers can neither restart nor tear it. With WAL the reader
        works from its own snapshot while writers and readers continue. In
        rollback-journal mode that reader's lock would block every commit and
        let short-budget writes fail as busy, so the copy holds this runtime's
        connection lock instead: writes and reads queue in Python until the copy
        finishes. Returns ``False`` when ``cancelled`` stopped the copy.
        """
        destination = Path(destination).expanduser().resolve()
        if destination.exists():
            raise DatabaseUnavailableError(f"backup destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        admission: contextlib.AbstractContextManager[Any] = (
            contextlib.nullcontext() if self.wal_active() else self._lock
        )
        try:
            with admission:
                with self._lock:
                    if self._writer is None or self._closed:
                        raise DatabaseUnavailableError(f"{self.label} is closed")
                source = connect_tracked(
                    readonly_sqlite_uri(self.path),
                    tracking_path=self.path,
                    uri=True,
                    isolation_level=None,
                    check_same_thread=False,
                    timeout=BUSY_TIMEOUT_MS / 1000,
                )
                try:
                    copied = copy_database(source, temporary, cancelled=cancelled)
                finally:
                    source.close()
            if not copied or (cancelled is not None and cancelled()):
                remove_database_files(temporary)
                return False
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            return True
        except (sqlite3.Error, OSError) as exc:
            remove_database_files(temporary)
            raise DatabaseUnavailableError(f"{self.label} backup failed: {destination}") from exc

    def close(self) -> None:
        """Close pooled readers and the writer, releasing all tracking entries."""
        with self._reader_lock:
            self._reader_access_closed = True
        while True:
            try:
                connection = self._readers.get_nowait()
            except queue.Empty:
                break
            self._close_reader(connection)
        with self._lock:
            if self._closed:
                return
            self._closed = True
            writer, self._writer = self._writer, None
            if writer is not None:
                if self._wal_active:
                    with contextlib.suppress(BaseException):
                        writer.execute("PRAGMA wal_checkpoint(PASSIVE)")
                with contextlib.suppress(BaseException):
                    writer.close()
        with self._reader_lock:
            outstanding = list(self._reader_connections)
        for connection in outstanding:
            self._close_reader(connection)

    def is_closed(self) -> bool:
        # This monotonic flag is only an observation, not a connection lease.
        # Async admission must not wait on a writer transaction on the Event Loop;
        # actual connection access and close still check it under the lock.
        return self._closed

    def wal_active(self) -> bool:
        with self._lock:
            return self._wal_active

    def live_connection_count(self) -> int:
        return tracked_connection_count(self.path)

    def reader_stats(self) -> tuple[int, int]:
        with self._reader_lock:
            return len(self._reader_connections), self._reader_permit_exhausted
