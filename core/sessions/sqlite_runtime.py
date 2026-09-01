"""One low-level owner for canonical Session SQLite connections and policy."""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import random
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.sessions.errors import (
    SessionStoreCorruptError,
    SessionStoreSchemaMismatchError,
    SessionStoreUnavailableError,
)

_LOGGER = logging.getLogger("vbot.sessions")

WRITE_PATIENCE_S = 20.0
TRANSCRIPT_WRITE_PATIENCE_S = 60.0
ACTIVITY_WRITE_PATIENCE_S = 0.5
READ_CONNECTION_LIMIT = 8
CHECKPOINT_EVERY_N_WRITES = 50
READ_OPEN_RETRY_SECONDS = 60.0
BUSY_TIMEOUT_MS = 1_000
_WRITE_RETRY_MIN_S = 0.020
_WRITE_RETRY_MAX_S = 0.150
_WRITE_RETRY_SLOW_MIN_S = 0.250
_WRITE_RETRY_SLOW_MAX_S = 1.000
_WRITE_RETRY_SLOW_AFTER_S = 2.0
_WAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024
_WAL_INCOMPAT_MARKERS = ("locking protocol", "not authorized", "disk i/o error")

_live_lock = threading.RLock()
_live_connections: dict[str, int] = {}
_tracked_factory_cache: dict[type, type] = {}
_wal_fallback_warned: set[str] = set()
_wal_reset_warned: set[str] = set()
_wal_reset_info_logged: set[str] = set()
_diagnostic_lock = threading.Lock()


class _BackupCancelledError(Exception):
    """Internal cooperative stop signal for a chunked SQLite backup."""


class UntrackableConnectionError(RuntimeError):
    """A file-backed connection could not be tracked until close."""


class LiveConnectionError(RuntimeError):
    """A raw file operation was attempted while a connection is live."""


def _key(path: Path | str) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def _canonical_db_path(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    if not row or len(row) < 3 or not row[2]:
        return None
    return _key(str(row[2]))


def has_live_connection(path: Path | str) -> bool:
    with _live_lock:
        return _key(path) in _live_connections


def tracked_connection_count(path: Path | str) -> int:
    with _live_lock:
        return _live_connections.get(_key(path), 0)


def _track(path: str) -> None:
    _live_connections[path] = _live_connections.get(path, 0) + 1


def _untrack(path: str) -> None:
    remaining = _live_connections.get(path, 0) - 1
    if remaining > 0:
        _live_connections[path] = remaining
    else:
        _live_connections.pop(path, None)


class _TrackingMixin:
    _vbot_tracked_path: str | None = None

    def close(self) -> None:  # type: ignore[override]
        with _live_lock:
            path = getattr(self, "_vbot_tracked_path", None)
            try:
                super().close()  # type: ignore[misc]
            finally:
                if path is not None:
                    self._vbot_tracked_path = None  # type: ignore[attr-defined]
                    _untrack(path)


class TrackedConnection(_TrackingMixin, sqlite3.Connection):
    """SQLite connection that releases its registry entry exactly once."""


def _tracking_factory(factory: type) -> type:
    if factory is sqlite3.Connection:
        return TrackedConnection
    if issubclass(factory, _TrackingMixin):
        return factory
    cached = _tracked_factory_cache.get(factory)
    if cached is None:
        cached = type(f"Tracked{factory.__name__}", (_TrackingMixin, factory), {})
        _tracked_factory_cache[factory] = cached
    return cached


def _retrofit_tracking(conn: sqlite3.Connection, resolved: str) -> sqlite3.Connection:
    connection_type = type(conn)
    if issubclass(connection_type, _TrackingMixin):
        return conn
    try:
        conn.__class__ = _tracking_factory(connection_type)  # type: ignore[assignment]
        return conn
    except TypeError as exc:
        raise UntrackableConnectionError(
            f"connection to {resolved} uses factory {connection_type.__name__} "
            "without close tracking"
        ) from exc


def connect_tracked(
    path: Path | str,
    *,
    tracking_path: Path | str | None = None,
    connect_fn: Callable[..., sqlite3.Connection] | None = None,
    **kwargs: Any,
) -> sqlite3.Connection:
    """Open and register a connection while holding the registry lock."""
    opener = connect_fn or sqlite3.connect
    kwargs["factory"] = _tracking_factory(kwargs.get("factory", sqlite3.Connection))
    conn: sqlite3.Connection | None = None
    with _live_lock:
        try:
            conn = opener(str(path), **kwargs)
            resolved = (
                _key(tracking_path) if tracking_path is not None else _canonical_db_path(conn)
            )
            if resolved is None:
                return conn
            conn = _retrofit_tracking(conn, resolved)
            conn._vbot_tracked_path = resolved  # type: ignore[attr-defined]
            _track(resolved)
            return conn
        except BaseException:
            if conn is not None:
                with contextlib.suppress(BaseException):
                    conn.close()
            raise


def page_count_bytes(conn: sqlite3.Connection) -> int | None:
    """Return the logical database size without opening a raw connection."""
    try:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        return int(page_count) * int(page_size)
    except (sqlite3.Error, TypeError, IndexError, ValueError):
        return None


def read_header_bytes_preopen(
    path: Path | str, *, length: int = 100, force: bool = False
) -> bytes | None:
    """Read a database header only while no tracked connection is live."""
    with _live_lock:
        if not force and _key(path) in _live_connections:
            return None
        try:
            with open(path, "rb") as handle:
                return handle.read(length)
        except OSError:
            return None


@contextlib.contextmanager
def offline_file_access(path: Path | str, *, what: str = "read"):
    """Hold the tracking lock across a raw database-file operation."""
    with _live_lock:
        if _key(path) in _live_connections:
            raise LiveConnectionError(
                f"Refusing to {what} {path}: a tracked SQLite connection is still open"
            )
        yield


def sqlite_source_id() -> str:
    try:
        with sqlite3.connect(":memory:") as conn:
            row = conn.execute("SELECT sqlite_source_id()").fetchone()
    except sqlite3.Error:
        return ""
    return "" if not row or row[0] is None else str(row[0])


def _on_disk_journal_mode(conn: sqlite3.Connection) -> str | None:
    last_error: sqlite3.OperationalError | None = None
    for _ in range(4):
        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
        except sqlite3.OperationalError as exc:
            last_error = exc
            if "disk i/o error" not in str(exc).lower():
                return None
            time.sleep(0.05)
            continue
        if not row or row[0] is None:
            return None
        value = row[0]
        if isinstance(value, bytes):
            try:
                value = value.decode("ascii")
            except UnicodeDecodeError:
                return None
        return str(value).strip().lower()
    if last_error:
        _LOGGER.debug("journal mode probe retries exhausted: %s", last_error)
    return None


def _set_journal_mode_no_wait(conn: sqlite3.Connection, mode: str) -> str:
    previous_timeout = 0
    with contextlib.suppress(sqlite3.Error, TypeError, ValueError):
        previous_timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    conn.execute("PRAGMA busy_timeout=0")
    try:
        row = conn.execute(f"PRAGMA journal_mode={mode}").fetchone()
        return "" if not row or row[0] is None else str(row[0]).strip().lower()
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"PRAGMA busy_timeout={previous_timeout}")


def _apply_macos_durability(conn: sqlite3.Connection) -> None:
    if sys.platform == "darwin":
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("PRAGMA checkpoint_fullfsync=1")
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("PRAGMA synchronous=FULL")


def _apply_wal_size_limit(conn: sqlite3.Connection) -> None:
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute(f"PRAGMA journal_size_limit={_WAL_SIZE_LIMIT_BYTES}")


def _log_once(
    bucket: set[str],
    label: str,
    message: str,
    *args: object,
    level: int = logging.WARNING,
) -> None:
    with _diagnostic_lock:
        if label in bucket:
            return
        bucket.add(label)
    _LOGGER.log(level, message, *args)


def _apply_delete_for_wal_reset_bug(conn: sqlite3.Connection, *, db_label: str) -> str:
    current = _on_disk_journal_mode(conn)
    if current == "wal":
        _log_once(
            _wal_reset_warned,
            db_label,
            "%s: SQLite %s is WAL-reset vulnerable; keeping existing WAL without live downgrade",
            db_label,
            sqlite3.sqlite_version,
        )
        _apply_wal_size_limit(conn)
        _apply_macos_durability(conn)
        return "wal"
    if current is None:
        raise SessionStoreUnavailableError(
            f"{db_label}: journal mode is indeterminate while SQLite is WAL-reset vulnerable"
        )
    try:
        actual = _set_journal_mode_no_wait(conn, "DELETE")
    except sqlite3.OperationalError as exc:
        if "busy" in str(exc).lower() or "locked" in str(exc).lower():
            raise SessionStoreUnavailableError(
                f"{db_label}: journal mode is busy while selecting DELETE"
            ) from exc
        raise
    _log_once(
        _wal_reset_info_logged,
        db_label,
        "%s: using safe journal_mode=DELETE because SQLite %s has the WAL-reset issue",
        db_label,
        sqlite3.sqlite_version,
        level=logging.INFO,
    )
    return actual or "delete"


def apply_wal_with_fallback(conn: sqlite3.Connection, *, db_label: str = "sessions.db") -> str:
    """Select a safe journal mode without downgrading an existing WAL live."""
    from core.sessions.schema import is_wal_reset_vulnerable, required_journal_mode

    if is_wal_reset_vulnerable(sqlite3.sqlite_version_info):
        return _apply_delete_for_wal_reset_bug(conn, db_label=db_label)
    current = _on_disk_journal_mode(conn)
    if current == "wal":
        _apply_wal_size_limit(conn)
        _apply_macos_durability(conn)
        return "wal"
    required = required_journal_mode(sqlite3.sqlite_version_info)
    if required == "delete":
        if current is None:
            raise SessionStoreUnavailableError(f"{db_label}: journal mode cannot be verified")
        actual = _set_journal_mode_no_wait(conn, "DELETE")
        if actual != "delete":
            raise SessionStoreUnavailableError(f"{db_label}: DELETE journal mode was refused")
        return actual
    try:
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        mode = "" if not row or row[0] is None else str(row[0]).strip().lower()
        if mode == "wal":
            _apply_wal_size_limit(conn)
            _apply_macos_durability(conn)
            return "wal"
        if current in {"wal", None}:
            raise SessionStoreUnavailableError(
                f"{db_label}: WAL was refused and the existing mode is indeterminate"
            )
        _log_once(
            _wal_fallback_warned,
            db_label,
            "%s: WAL unsupported on this filesystem; falling back to DELETE",
            db_label,
        )
        return _set_journal_mode_no_wait(conn, "DELETE") or "delete"
    except sqlite3.OperationalError as exc:
        lowered = str(exc).lower()
        if not any(marker in lowered for marker in _WAL_INCOMPAT_MARKERS):
            raise
        if "disk i/o error" in lowered:
            for _ in range(2):
                time.sleep(0.05)
                try:
                    row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                except sqlite3.OperationalError as retry_error:
                    if "disk i/o error" not in str(retry_error).lower():
                        raise
                    exc = retry_error
                    continue
                mode = "" if not row or row[0] is None else str(row[0]).strip().lower()
                if mode == "wal":
                    _apply_wal_size_limit(conn)
                    _apply_macos_durability(conn)
                    return "wal"
                break
        current = _on_disk_journal_mode(conn)
        if current in {"wal", None}:
            raise SessionStoreUnavailableError(
                f"{db_label}: journal mode could not be safely selected"
            ) from exc
        _log_once(
            _wal_fallback_warned,
            db_label,
            "%s: WAL refused (%s); falling back to DELETE",
            db_label,
            exc,
        )
        return _set_journal_mode_no_wait(conn, "DELETE") or "delete"


def is_busy_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "busy" in message or "locked" in message


def _is_transient_cursor_error(exc: BaseException) -> bool:
    return "no more rows available" in str(exc).lower()


def classify_unavailable(exc: BaseException) -> bool:
    """Return whether a database error is safe to retry as transient contention."""
    return isinstance(exc, sqlite3.Error) and (
        is_busy_error(exc) or _is_transient_cursor_error(exc)
    )


class SQLiteRuntime:
    """Own every live writer/reader connection for one Session database."""

    def __init__(self, db_path: Path, *, db_label: str = "sessions.db") -> None:
        self.db_path = Path(db_path)
        self.db_label = db_label
        self._lock = threading.RLock()
        self._writer: sqlite3.Connection | None = None
        self._writer_owned = False
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

    def open_writer(
        self,
        *,
        create: bool = False,
        database_id: str | None = None,
        expected_database_id: str | None = None,
    ) -> sqlite3.Connection:
        """Open, classify, and configure the writer; close it on every failure."""
        from core.sessions.schema import (
            APPLICATION_ID,
            DATABASE_ID_META_KEY,
            SCHEMA_CONVERSION_FLOOR,
            SCHEMA_SQL,
            SCHEMA_VERSION,
        )

        with self._lock:
            if self._writer is not None:
                return self._writer
            if self._closed:
                raise RuntimeError("SQLite runtime is closed")
            existed = self.db_path.exists()
            connection: sqlite3.Connection | None = None
            try:
                connection = connect_tracked(
                    self.db_path,
                    tracking_path=self.db_path,
                    isolation_level=None,
                    check_same_thread=False,
                    timeout=1.0,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
                if not create or existed:
                    app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
                    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if app_id != APPLICATION_ID:
                        raise SessionStoreCorruptError(
                            f"not a vBot Session database: {self.db_path}"
                        )
                    if version > SCHEMA_VERSION:
                        raise SessionStoreSchemaMismatchError(
                            f"Session database is from a newer vBot: schema version {version}"
                        )
                    if version < SCHEMA_CONVERSION_FLOOR:
                        raise SessionStoreSchemaMismatchError(
                            "Session database requires offline conversion: "
                            f"schema version {version}"
                        )
                    if expected_database_id is not None:
                        row = connection.execute(
                            "SELECT value FROM store_meta WHERE key = ?", (DATABASE_ID_META_KEY,)
                        ).fetchone()
                        if row is None or str(row[0]) != expected_database_id:
                            raise SessionStoreCorruptError(
                                "Session database identity does not match the store marker "
                                f"at {self.db_path}"
                            )
                self._wal_active = (
                    apply_wal_with_fallback(connection, db_label=self.db_label) == "wal"
                )
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA wal_autocheckpoint=1000")
                connection.execute(f"PRAGMA journal_size_limit={_WAL_SIZE_LIMIT_BYTES}")
                if create and not existed:
                    if database_id is None:
                        raise SessionStoreCorruptError("new Session database has no identity")
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        + SCHEMA_SQL
                        + f"\nPRAGMA application_id = {APPLICATION_ID};"
                        + f"\nPRAGMA user_version = {SCHEMA_VERSION};"
                        + "\nINSERT INTO store_meta (key, value) VALUES ('"
                        + DATABASE_ID_META_KEY
                        + f"', '{database_id}');\nCOMMIT;"
                    )
                self._writer = connection
                self._writer_owned = True
                return connection
            except BaseException as exc:
                if connection is not None:
                    with contextlib.suppress(BaseException):
                        connection.close()
                if isinstance(exc, sqlite3.OperationalError) and (
                    is_busy_error(exc)
                    or any(
                        marker in str(exc).lower()
                        for marker in (
                            "readonly",
                            "read-only",
                            "disk full",
                            "disk i/o",
                            "unable to open",
                            "cannot open",
                            "permission",
                        )
                    )
                ):
                    raise SessionStoreUnavailableError(
                        f"Session database cannot be opened safely: {self.db_path}"
                    ) from exc
                if isinstance(exc, sqlite3.DatabaseError):
                    raise SessionStoreCorruptError(
                        f"Session database cannot be opened safely: {self.db_path}"
                    ) from exc
                raise

    def set_writer(self, conn: sqlite3.Connection, *, wal_active: bool) -> None:
        """Adopt an already configured writer for narrow offline callers."""
        with self._lock:
            if self._writer is not None or self._closed:
                raise RuntimeError("SQLite runtime already owns a writer")
            self._writer = conn
            self._writer_owned = False
            self._wal_active = wal_active

    @property
    def writer(self) -> sqlite3.Connection:
        with self._lock:
            if self._writer is None or self._closed:
                raise RuntimeError("SQLite runtime is closed")
            return self._writer

    def execute_write(
        self,
        fn: Callable[[sqlite3.Connection], Any],
        *,
        patience_s: float = WRITE_PATIENCE_S,
    ) -> Any:
        """Run a whole idempotent transaction with busy-only retry."""
        deadline = time.monotonic() + patience_s
        while True:
            try:
                with self._lock:
                    connection = self.writer
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(connection)
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
            except BaseException as exc:
                if not classify_unavailable(exc):
                    raise
                if not self._sleep_before_retry(deadline, patience_s):
                    raise SessionStoreUnavailableError(
                        f"Session database write busy for {patience_s:.0f}s: {self.db_path}"
                    ) from exc

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
    def read_ctx(self):
        """Yield one bounded read connection or the serialized writer."""
        with self._lock:
            if self._writer is None or self._closed:
                raise RuntimeError("SQLite runtime is closed")
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
            continue_return = healthy and self._return_reader(connection)
            if not continue_return:
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
                f"file:{self.db_path.as_posix()}?mode=ro",
                tracking_path=self.db_path,
                uri=True,
                isolation_level=None,
                check_same_thread=False,
                timeout=5.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            from core.sessions.schema import APPLICATION_ID, SCHEMA_VERSION

            if int(connection.execute("PRAGMA application_id").fetchone()[0]) != APPLICATION_ID:
                raise SessionStoreCorruptError("reader application identity mismatch")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
                raise SessionStoreCorruptError("reader schema version mismatch")
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
                _LOGGER.warning("WAL checkpoint failed for %s: %s", self.db_path, exc)

    def backup(
        self,
        destination: Path,
        *,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """Create a cancellable online backup without monopolizing the writer."""
        destination = Path(destination).expanduser().resolve()
        if destination.exists():
            raise RuntimeError(f"backup destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        source: sqlite3.Connection | None = None
        try:
            with self._lock:
                if self._writer is None or self._closed:
                    raise RuntimeError("SQLite runtime is closed")
            source = connect_tracked(
                f"file:{self.db_path.as_posix()}?mode=ro",
                tracking_path=self.db_path,
                uri=True,
                isolation_level=None,
                check_same_thread=False,
                timeout=1.0,
            )
            source.execute("PRAGMA query_only=ON")
            source.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            target = sqlite3.connect(temporary)
            try:

                def progress(_status: int, _remaining: int, _total: int) -> None:
                    if cancel_event is not None and cancel_event.is_set():
                        raise _BackupCancelledError

                source.backup(target, pages=256, progress=progress, sleep=0.01)
                target.commit()
            finally:
                target.close()
                source.close()
                source = None
            with contextlib.suppress(OSError):
                descriptor = os.open(temporary, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            if cancel_event is not None and cancel_event.is_set():
                raise _BackupCancelledError
            os.replace(temporary, destination)
            return True
        except _BackupCancelledError:
            for candidate in (temporary, Path(f"{temporary}-wal"), Path(f"{temporary}-journal")):
                with contextlib.suppress(OSError):
                    candidate.unlink()
            return False
        except (sqlite3.Error, OSError) as exc:
            for candidate in (temporary, Path(f"{temporary}-wal"), Path(f"{temporary}-journal")):
                with contextlib.suppress(OSError):
                    candidate.unlink()
            raise SessionStoreUnavailableError(
                f"Session database backup failed: {destination}"
            ) from exc
        finally:
            if source is not None:
                with contextlib.suppress(BaseException):
                    source.close()

    def close(self) -> None:
        """Close pooled readers and writer, releasing all tracking entries."""
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
        with self._lock:
            return self._closed

    def wal_active(self) -> bool:
        with self._lock:
            return self._wal_active

    def live_connection_count(self) -> int:
        return tracked_connection_count(self.db_path)

    def reader_stats(self) -> tuple[int, int]:
        with self._reader_lock:
            return len(self._reader_connections), self._reader_permit_exhausted
