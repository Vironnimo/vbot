"""SQLite connection tracking, journal policy, error classification and copies.

Every vBot database opens its connections through this module, so a raw file
operation (quarantine, discard, restore) can prove that no tracked connection
to the same file is live in this process.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.database.errors import (
    DatabaseCorruptError,
    DatabaseError,
    DatabaseUnavailableError,
)

_LOGGER = logging.getLogger("vbot.database")

JOURNAL_MODE_WAL = "wal"
JOURNAL_MODE_DELETE = "delete"
BUSY_TIMEOUT_MS = 1_000
_WAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024
_WAL_INCOMPAT_MARKERS = ("locking protocol", "not authorized", "disk i/o error")
_SQLITE_PRIMARY_CODE_MASK = 0xFF
_COPY_PROGRESS_OPCODES = 10_000
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_SQLITE_CORRUPTION_CODES = frozenset(
    {
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_FORMAT,
        sqlite3.SQLITE_NOTADB,
    }
)
_SQLITE_UNAVAILABLE_CODES = frozenset(
    {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_INTERRUPT,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_NOMEM,
        sqlite3.SQLITE_PERM,
        sqlite3.SQLITE_PROTOCOL,
        sqlite3.SQLITE_READONLY,
    }
)
_SQLITE_CORRUPTION_MARKERS = (
    "database disk image is malformed",
    "database corruption",
    "file is not a database",
    "malformed database schema",
)
_SQLITE_UNAVAILABLE_MARKERS = (
    "attempt to write a readonly database",
    "attempt to write a read-only database",
    "database or disk is full",
    "disk full",
    "disk i/o error",
    "unable to open database",
    "cannot open database",
    "permission denied",
)

_live_lock = threading.RLock()
_live_connections: dict[str, int] = {}
_tracked_factory_cache: dict[type, type] = {}
_wal_fallback_warned: set[str] = set()
_wal_reset_warned: set[str] = set()
_wal_reset_info_logged: set[str] = set()
_diagnostic_lock = threading.Lock()


class UntrackableConnectionError(RuntimeError):
    """A file-backed connection could not be tracked until close."""


def _key(path: Path | str) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def readonly_sqlite_uri(path: Path | str) -> str:
    """Return a percent-escaped SQLite URI for an existing read-only database."""

    return f"{Path(path).expanduser().resolve().as_uri()}?mode=ro"


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


def database_files(path: Path) -> tuple[Path, ...]:
    """The database file and every SQLite sidecar name it can own."""
    return (path, *(Path(f"{path}{suffix}") for suffix in _SIDECAR_SUFFIXES))


def remove_database_files(path: Path) -> None:
    """Best-effort removal of a database file and its sidecars."""
    for candidate in database_files(path):
        with contextlib.suppress(OSError):
            candidate.unlink()


def copy_database(
    source: sqlite3.Connection,
    destination: Path,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Write one consistent, standalone copy of ``source`` to a new file.

    ``VACUUM INTO`` copies the whole database inside a single read transaction,
    so commits on other connections can neither restart nor tear the copy, as
    they do with a stepped ``Connection.backup``. The copy keeps rowids, the
    application id and the schema version, drops the freelist, and is always a
    rollback-journal file without WAL sidecars. ``source`` must be outside a
    transaction and must not use ``query_only``, which rejects ``VACUUM INTO``;
    open it with ``mode=ro`` instead. The caller owns the output's durability.
    Returns ``False`` when ``cancelled`` stopped the copy. The destination and
    its sidecars must not exist; partial output is removed on every failure.
    """
    destination = Path(destination)
    existing = [path for path in database_files(destination) if path.exists()]
    if existing:
        raise FileExistsError(f"database copy destination already exists: {existing[0]}")
    if cancelled is not None:
        source.set_progress_handler(lambda: int(cancelled()), _COPY_PROGRESS_OPCODES)
    try:
        source.execute("VACUUM INTO ?", (str(destination),))
        return True
    except BaseException as exc:
        remove_database_files(destination)
        if isinstance(exc, sqlite3.Error) and cancelled is not None and cancelled():
            return False
        raise
    finally:
        if cancelled is not None:
            source.set_progress_handler(None, 0)


def sqlite_source_id() -> str:
    """The exact SQLite build, recorded in snapshot manifests."""
    try:
        with contextlib.closing(sqlite3.connect(":memory:")) as conn:
            row = conn.execute("SELECT sqlite_source_id()").fetchone()
    except sqlite3.Error:
        return ""
    return "" if not row or row[0] is None else str(row[0])


def _version_tuple(parts: tuple[int, ...]) -> tuple[int, int, int]:
    values = [int(part) for part in parts]
    values.extend([0] * (3 - len(values)))
    return values[0], values[1], values[2]


def is_wal_reset_vulnerable(version_info: tuple[int, ...]) -> bool:
    """Return whether *version_info* carries SQLite's WAL-reset corruption bug.

    Affected are 3.7.0 (2010-07-21) through 3.51.2 (2026-01-09); fixed in
    3.51.3 and later, with source backports in 3.50.7 and 3.44.6
    (https://sqlite.org/wal.html, "The WAL-Reset Bug"). The race needs WAL
    plus two connections writing or checkpointing at the same instant;
    pre-WAL builds cannot hit it.
    """
    info = _version_tuple(version_info)
    if info < (3, 7, 0):
        return False
    return not (
        info >= (3, 51, 3) or (3, 50, 7) <= info < (3, 51, 0) or (3, 44, 6) <= info < (3, 45, 0)
    )


def required_journal_mode(version_info: tuple[int, ...]) -> str:
    """The one journal policy per SQLite build: rollback journal on WAL-reset-vulnerable
    builds, WAL otherwise."""
    if is_wal_reset_vulnerable(version_info):
        return JOURNAL_MODE_DELETE
    return JOURNAL_MODE_WAL


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
        raise DatabaseUnavailableError(
            f"{db_label}: journal mode is indeterminate while SQLite is WAL-reset vulnerable"
        )
    try:
        actual = _set_journal_mode_no_wait(conn, "DELETE")
    except sqlite3.OperationalError as exc:
        if "busy" in str(exc).lower() or "locked" in str(exc).lower():
            raise DatabaseUnavailableError(
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


def apply_wal_with_fallback(conn: sqlite3.Connection, *, db_label: str) -> str:
    """Select a safe journal mode without downgrading an existing WAL live."""
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
            raise DatabaseUnavailableError(f"{db_label}: journal mode cannot be verified")
        actual = _set_journal_mode_no_wait(conn, "DELETE")
        if actual != "delete":
            raise DatabaseUnavailableError(f"{db_label}: DELETE journal mode was refused")
        return actual
    try:
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        mode = "" if not row or row[0] is None else str(row[0]).strip().lower()
        if mode == "wal":
            _apply_wal_size_limit(conn)
            _apply_macos_durability(conn)
            return "wal"
        if current in {"wal", None}:
            raise DatabaseUnavailableError(
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
            raise DatabaseUnavailableError(
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


def classify_write_error(exc: BaseException) -> str:
    """Classify an escaped SQLite write error without hiding programming defects.

    Constraint errors deliberately remain ``other``: operation owners translate
    the expected constraints they create, while an unexpected constraint or API
    misuse remains visible to its caller instead of masquerading as an outage.
    """
    if not isinstance(exc, sqlite3.Error):
        return "other"
    code = getattr(exc, "sqlite_errorcode", None)
    primary_code = code & _SQLITE_PRIMARY_CODE_MASK if isinstance(code, int) else None
    message = str(exc).lower()
    if primary_code in _SQLITE_CORRUPTION_CODES or any(
        marker in message for marker in _SQLITE_CORRUPTION_MARKERS
    ):
        return "corrupt"
    if primary_code in _SQLITE_UNAVAILABLE_CODES or any(
        marker in message for marker in _SQLITE_UNAVAILABLE_MARKERS
    ):
        return "unavailable"
    return "other"


def classified_error(exc: BaseException, message: str) -> DatabaseError | None:
    """Translate a classified SQLite failure into the shared kernel error.

    Returns ``None`` for failures the caller owns: constraint errors, API
    misuse and every non-SQLite exception.
    """
    classification = classify_write_error(exc)
    if classification == "corrupt":
        return DatabaseCorruptError(message)
    if classification == "unavailable" or (isinstance(exc, sqlite3.Error) and is_busy_error(exc)):
        return DatabaseUnavailableError(message)
    return None
