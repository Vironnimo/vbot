"""Low-level SQLite runtime for the canonical Session database.

Centralizes the mechanisms that are easy to get subtly wrong when they are
scattered across a domain store: POSIX lock-safe connection tracking,
journal-mode selection with WAL-reset and filesystem fallbacks, PRAGMA
verification, time-based jittered writer patience, peak-bounded reader pool,
and periodic PASSIVE checkpoints.

Adopted from the Hermes agent's battle-tested ``hermes_state.py`` and
``hermes_cli/sqlite_safe_read.py``. See ``.vorch/plans/sqlite-sessions/
hermes-reference.md`` for the traceability matrix.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import random
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.sessions.errors import SessionStoreCorruptError

_LOGGER = logging.getLogger("vbot.sessions")

# ---------------------------------------------------------------------------
# Connection tracking (POSIX lock safety)
# ---------------------------------------------------------------------------

# Guards both the registry and the syscalls it describes. Reentrant because
# connect_tracked -> _canonical_db_path stays on one thread.
_live_lock = threading.RLock()
# canonical path -> number of live connections opened by this process
_live_connections: dict[str, int] = {}


class UntrackableConnectionError(RuntimeError):
    """A file-backed connection could not be tracked."""


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
    if not row or len(row) < 3:
        return None
    path_str = row[2]
    if not path_str:
        return None
    return _key(path_str)


def has_live_connection(path: Path | str) -> bool:
    with _live_lock:
        return _key(path) in _live_connections


def _track(path: str) -> None:
    with _live_lock:
        _live_connections[path] = _live_connections.get(path, 0) + 1


def _untrack(path: str) -> None:
    with _live_lock:
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
            super().close()  # type: ignore[misc]
            if path is not None:
                self._vbot_tracked_path = None  # type: ignore[attr-defined]
                _untrack(path)


class TrackedConnection(_TrackingMixin, sqlite3.Connection):
    """sqlite3.Connection that untracks its path exactly once on close."""


_tracked_factory_cache: dict[type, type] = {}


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
    cls = type(conn)
    if issubclass(cls, _TrackingMixin):
        return conn
    try:
        conn.__class__ = _tracking_factory(cls)  # type: ignore[assignment]
        return conn
    except TypeError as exc:
        raise UntrackableConnectionError(
            f"connection to {resolved} uses factory {cls.__name__} which cannot release "
            "its tracking entry on close"
        ) from exc


def connect_tracked(
    path: Path | str,
    *,
    tracking_path: Path | str | None = None,
    connect_fn=None,
    **kwargs: Any,
) -> sqlite3.Connection:
    """sqlite3.connect that registers the connection for its fd lifetime."""
    opener = connect_fn if connect_fn is not None else sqlite3.connect
    kwargs["factory"] = _tracking_factory(kwargs.get("factory", sqlite3.Connection))
    with _live_lock:
        conn = opener(str(path), **kwargs)
        try:
            resolved = (
                _key(tracking_path) if tracking_path is not None else _canonical_db_path(conn)
            )
            if resolved is None:
                return conn  # type: ignore[no-any-return]
            if not isinstance(conn, _TrackingMixin):
                conn = _retrofit_tracking(conn, resolved)
            conn._vbot_tracked_path = resolved  # type: ignore[attr-defined]
            _live_connections[resolved] = _live_connections.get(resolved, 0) + 1
            return conn  # type: ignore[no-any-return]
        except Exception:
            with contextlib.suppress(Exception):
                sqlite3.Connection.close(conn)
            raise


def page_count_bytes(conn: sqlite3.Connection) -> int | None:
    """Logical DB size via PRAGMAs, never a raw open."""
    try:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    except (sqlite3.Error, TypeError, IndexError) as exc:
        _LOGGER.debug("page_count/page_size unavailable: %s", exc)
        return None
    try:
        return int(page_count) * int(page_size)
    except (TypeError, ValueError):
        return None


def read_header_bytes_preopen(
    path: Path | str, *, length: int = 100, force: bool = False
) -> bytes | None:
    """Read first *length* bytes only when no live connection exists."""
    with _live_lock:
        if not force and _key(path) in _live_connections:
            _LOGGER.debug("refusing byte-level read of %s: a live connection exists", path)
            return None
        try:
            with open(path, "rb") as handle:
                return handle.read(length)
        except OSError:
            return None


@contextlib.contextmanager
def offline_file_access(path: Path | str, *, what: str = "read"):
    """Hold _live_lock across a raw file operation."""
    with _live_lock:
        if _key(path) in _live_connections:
            raise LiveConnectionError(
                f"Refusing to {what} {path}: a connection is still open in this process, "
                "and raw file access would cancel its POSIX advisory locks."
            )
        yield


# ---------------------------------------------------------------------------
# SQLite version reporting
# ---------------------------------------------------------------------------


def sqlite_source_id() -> str:
    try:
        conn = sqlite3.connect(":memory:")
        try:
            row = conn.execute("SELECT sqlite_source_id()").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return ""
    if not row or row[0] is None:
        return ""
    return str(row[0])


# ---------------------------------------------------------------------------
# Journal mode and durability
# ---------------------------------------------------------------------------

_WAL_INCOMPAT_MARKERS = (
    "locking protocol",
    "not authorized",
    "disk i/o error",
)
_WAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024  # 64 MiB

_wal_fallback_warned: set[str] = set()
_wal_fallback_lock = threading.Lock()
_wal_reset_warned: set[str] = set()
_wal_reset_lock = threading.Lock()


def _on_disk_journal_mode(conn: sqlite3.Connection) -> str | None:
    """Read journal_mode from on-disk header, with transient EIO retry."""
    last_exc: Exception | None = None
    for _ in range(4):
        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if "disk i/o error" not in str(exc).lower():
                return None
            time.sleep(0.05)
            continue
        if row is None:
            return None
        mode = row[0]
        if isinstance(mode, bytes):
            try:
                mode = mode.decode("ascii")
            except UnicodeDecodeError:
                return None
        return str(mode).strip().lower() if mode is not None else None
    if last_exc is not None:
        _LOGGER.debug("_on_disk_journal_mode retries exhausted: %s", last_exc)
    return None


def _set_journal_mode_no_wait(conn: sqlite3.Connection, mode: str) -> str:
    """PRAGMA journal_mode=<mode> with busy_timeout=0 so concurrent opener fails fast."""
    previous_timeout = 0
    try:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        if row and row[0] is not None:
            previous_timeout = int(row[0])
    except (sqlite3.OperationalError, TypeError, ValueError):
        previous_timeout = 0
    conn.execute("PRAGMA busy_timeout=0")
    try:
        row = conn.execute(f"PRAGMA journal_mode={mode}").fetchone()
        return str(row[0]).strip().lower() if row and row[0] is not None else ""
    finally:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(f"PRAGMA busy_timeout={previous_timeout}")


def _apply_wal_size_limit(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(f"PRAGMA journal_size_limit={_WAL_SIZE_LIMIT_BYTES}")
    except sqlite3.OperationalError as exc:
        _LOGGER.debug("journal_size_limit not applied: %s", exc)


def _apply_macos_checkpoint_barrier(conn: sqlite3.Connection) -> None:
    if sys.platform != "darwin":
        return
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("PRAGMA checkpoint_fullfsync=1")


def _enforce_macos_synchronous_full(conn: sqlite3.Connection) -> None:
    if sys.platform != "darwin":
        return
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("PRAGMA synchronous=FULL")


def _log_wal_fallback_once(db_label: str, exc: Exception) -> None:
    with _wal_fallback_lock:
        if db_label in _wal_fallback_warned:
            return
        _wal_fallback_warned.add(db_label)
    _LOGGER.error(
        "%s: WAL unsupported on this filesystem (%s) — falling back to DELETE. "
        "See https://www.sqlite.org/wal.html (once per process per database).",
        db_label,
        exc,
    )


def _log_wal_reset_once(db_label: str, *, kept_wal: bool, indeterminate: bool = False) -> None:
    with _wal_reset_lock:
        if db_label in _wal_reset_warned:
            return
        _wal_reset_warned.add(db_label)
    if indeterminate:
        action = "journal mode could not be verified (database is locked); leaving mode untouched"
    elif kept_wal:
        action = "is already in WAL mode — leaving WAL in place (no live downgrade)"
    else:
        action = "using journal_mode=DELETE instead of WAL"
    _LOGGER.warning(
        "%s: linked SQLite %s is WAL-reset vulnerable — %s. Upgrade to 3.51.3+ (or 3.50.7/3.44.6).",
        db_label,
        sqlite3.sqlite_version,
        action,
    )


def _apply_delete_for_wal_reset_bug(conn: sqlite3.Connection, *, db_label: str) -> str:
    current = _on_disk_journal_mode(conn)
    if current == "wal":
        _log_wal_reset_once(db_label, kept_wal=True)
        _apply_wal_size_limit(conn)
        _apply_macos_checkpoint_barrier(conn)
        _enforce_macos_synchronous_full(conn)
        return "wal"
    if current is None:
        _log_wal_reset_once(db_label, kept_wal=True, indeterminate=True)
        return "wal"
    try:
        actual = _set_journal_mode_no_wait(conn, "DELETE")
    except sqlite3.OperationalError as exc:
        lowered = str(exc).lower()
        if "locked" in lowered or "busy" in lowered:
            _log_wal_reset_once(db_label, kept_wal=True, indeterminate=True)
            return current or "delete"
        return current or "delete"
    _log_wal_reset_once(db_label, kept_wal=False)
    return actual or "delete"


def apply_wal_with_fallback(conn: sqlite3.Connection, *, db_label: str = "sessions.db") -> str:
    """Set journal_mode=WAL with Hermes fallbacks; return effective mode."""
    from core.sessions.schema import is_wal_reset_vulnerable, required_journal_mode

    if is_wal_reset_vulnerable(sqlite3.sqlite_version_info):
        return _apply_delete_for_wal_reset_bug(conn, db_label=db_label)

    current_mode = _on_disk_journal_mode(conn)
    if current_mode == "wal":
        _apply_wal_size_limit(conn)
        _apply_macos_checkpoint_barrier(conn)
        _enforce_macos_synchronous_full(conn)
        return "wal"

    # vBot has no operator journal_mode config; decide from required_journal_mode.
    # required_journal_mode already respects the WAL-reset gate, so we only
    # handle filesystem incompatibility here.
    required = required_journal_mode(sqlite3.sqlite_version_info)
    if required == "delete":
        if current_mode is None:
            raise sqlite3.OperationalError(
                "could not verify journal mode before applying DELETE (database is locked)"
            )
        actual = _set_journal_mode_no_wait(conn, "DELETE")
        if actual != "delete":
            raise sqlite3.OperationalError(f"could not set DELETE (got {actual!r})")
        return actual

    try:
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        mode = str(row[0]).strip().lower() if row and row[0] is not None else ""
        if mode == "wal":
            _apply_wal_size_limit(conn)
            _apply_macos_checkpoint_barrier(conn)
            _enforce_macos_synchronous_full(conn)
            return "wal"
        # Silent refusal (macOS NFS/SMB): pragma returned non-WAL without raising.
        silent_exc = sqlite3.OperationalError(f"journal_mode=WAL refused (still {mode!r})")
        if current_mode == "wal" or current_mode is None:
            raise silent_exc
        _log_wal_fallback_once(db_label, silent_exc)
        # Already proved we are not WAL, so try DELETE as fallback.
        try:
            actual = _set_journal_mode_no_wait(conn, "DELETE")
            return actual or "delete"
        except sqlite3.OperationalError:
            return "delete"
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if not any(marker in msg for marker in _WAL_INCOMPAT_MARKERS):
            raise
        if "disk i/o error" in msg:
            for _ in range(2):
                time.sleep(0.05)
                try:
                    row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                except sqlite3.OperationalError as retry_exc:
                    if "disk i/o error" not in str(retry_exc).lower():
                        raise
                    exc = retry_exc
                    continue
                mode = str(row[0]).strip().lower() if row and row[0] is not None else ""
                if mode == "wal":
                    _apply_wal_size_limit(conn)
                    _apply_macos_checkpoint_barrier(conn)
                    _enforce_macos_synchronous_full(conn)
                    return "wal"
                break
        existing = _on_disk_journal_mode(conn)
        if existing == "wal" or existing is None:
            raise
        _log_wal_fallback_once(db_label, exc)
        with contextlib.suppress(sqlite3.OperationalError):
            _set_journal_mode_no_wait(conn, "DELETE")
        return "delete"


# ---------------------------------------------------------------------------
# Writer patience and checkpoint
# ---------------------------------------------------------------------------

# Budgets (seconds)
WRITE_PATIENCE_S = 20.0
TRANSCRIPT_WRITE_PATIENCE_S = 60.0
ACTIVITY_WRITE_PATIENCE_S = 0.5

_WRITE_RETRY_MIN_S = 0.020
_WRITE_RETRY_MAX_S = 0.150
_WRITE_RETRY_SLOW_MIN_S = 0.250
_WRITE_RETRY_SLOW_MAX_S = 1.000
_WRITE_RETRY_SLOW_AFTER_S = 2.0
_CHECKPOINT_EVERY_N_WRITES = 50
_READ_POOL_MAX = 8
_READ_OPEN_RETRY_SECONDS = 60.0


def _is_no_more_rows(exc: BaseException) -> bool:
    return "no more rows available" in str(exc).lower()


def is_busy_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def classify_unavailable(exc: BaseException) -> bool:
    """Return True if *exc* is a transient busy/locked that may be retried."""
    if isinstance(exc, sqlite3.OperationalError):
        if is_busy_error(exc):
            return True
        if _is_no_more_rows(exc):
            return True
    if isinstance(exc, sqlite3.DatabaseError) and _is_no_more_rows(exc):
        return True
    if isinstance(exc, sqlite3.InterfaceError) and _is_no_more_rows(exc):
        return True
    # sqlite3.Error is the base; check message as well for wrapped cases.
    return bool(isinstance(exc, sqlite3.Error) and _is_no_more_rows(exc))


# ---------------------------------------------------------------------------
# SQLiteRuntime — one writer + bounded reader pool + checkpoint
# ---------------------------------------------------------------------------


class SQLiteRuntime:
    """Owns one canonical database file's low-level connections and policies."""

    def __init__(
        self,
        db_path: Path,
        *,
        db_label: str = "sessions.db",
    ) -> None:
        self.db_path = Path(db_path)
        self.db_label = db_label
        self._lock = threading.RLock()
        self._writer: sqlite3.Connection | None = None
        self._wal_active = False
        self._write_count = 0
        self._closed = False

        # Reader pool (WAL only)
        self._read_pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(
            maxsize=_READ_POOL_MAX
        )
        self._read_permits = threading.BoundedSemaphore(_READ_POOL_MAX)
        self._read_conns_lock = threading.Lock()
        self._read_conns_closed = False
        self._read_open_failed_at = 0.0
        self._read_permit_exhausted = 0

    # -- writer open --

    def open_writer(
        self,
        *,
        create: bool = False,
        database_id: str | None = None,
    ) -> sqlite3.Connection:
        """Open (or create) the writer connection with full PRAGMA suite."""
        from core.sessions.schema import (
            APPLICATION_ID,
            DATABASE_ID_META_KEY,
            SCHEMA_SQL,
            SCHEMA_VERSION,
        )

        if self._writer is not None:
            return self._writer
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.db_path.exists()
        conn = connect_tracked(
            str(self.db_path),
            check_same_thread=False,
            timeout=1.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        try:
            # Probe with retries for busy - writer may be contended at open.
            deadline = time.monotonic() + WRITE_PATIENCE_S
            while True:
                try:
                    conn.execute("PRAGMA foreign_keys=ON")
                    break
                except sqlite3.OperationalError as exc:
                    if not is_busy_error(exc) or time.monotonic() >= deadline:
                        raise
                    time.sleep(random.uniform(_WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S))

            # Journal mode selection with fallback
            wal_mode = apply_wal_with_fallback(conn, db_label=self.db_label)
            self._wal_active = wal_mode == "wal"

            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
            conn.execute("PRAGMA journal_size_limit=67108864")
            _apply_macos_checkpoint_barrier(conn)
            if self._wal_active:
                _enforce_macos_synchronous_full(conn)

            # Busy timeout for the writer itself (application retry is the main patience)
            conn.execute("PRAGMA busy_timeout=1000")

            if create and not existed:
                assert database_id is not None
                conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + SCHEMA_SQL
                    + f"\nPRAGMA application_id = {APPLICATION_ID};"
                    + f"\nPRAGMA user_version = {SCHEMA_VERSION};"
                    + "\nINSERT INTO store_meta (key, value) VALUES ('"
                    + DATABASE_ID_META_KEY
                    + f"', '{database_id}');\nCOMMIT;"
                )
                self._verify_identity(conn)
                self._verify_integrity(conn)
            else:
                # Verify and reconcile if needed (caller does identity/integrity checks outside)
                pass

            self._writer = conn
            return conn
        except Exception:
            with contextlib.suppress(Exception):
                conn.close()
            raise

    def set_writer(self, conn: sqlite3.Connection, *, wal_active: bool) -> None:
        self._writer = conn
        self._wal_active = wal_active

    # -- checkpoint --

    def _try_wal_checkpoint(self) -> None:
        """Best-effort PASSIVE checkpoint; never raises."""
        if not self._wal_active or self._writer is None:
            return
        try:
            with self._lock:
                result = self._writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                if result and len(result) > 1 and result[1] > 0:
                    _LOGGER.debug(
                        "WAL checkpoint: %d/%d pages pending",
                        result[2] if len(result) > 2 else 0,
                        result[1],
                    )
        except Exception as exc:
            _LOGGER.warning("WAL checkpoint (PASSIVE) failed: %s", exc)

    def checkpoint_on_close(self) -> None:
        if not self._wal_active or self._writer is None:
            return
        try:
            self._writer.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception as exc:
            _LOGGER.debug("WAL checkpoint at close failed: %s", exc)

    # -- reader pool --

    def _get_read_conn(self) -> sqlite3.Connection | None:
        if not self._wal_active:
            return None
        with self._read_conns_lock:
            if self._read_conns_closed:
                return None
            if (
                self._read_open_failed_at
                and time.monotonic() - self._read_open_failed_at < _READ_OPEN_RETRY_SECONDS
            ):
                return None
        if not self._read_permits.acquire(blocking=False):
            with self._read_conns_lock:
                self._read_permit_exhausted += 1
            _LOGGER.debug(
                "read pool at capacity (%d) for %s; using writer", _READ_POOL_MAX, self.db_path
            )
            return None
        conn: sqlite3.Connection | None = None
        try:
            conn = connect_tracked(
                f"file:{self.db_path}?mode=ro",
                tracking_path=self.db_path,
                uri=True,
                check_same_thread=False,
                timeout=5.0,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            # Verify version quickly; reader must match writer generation
            # Minimal check — full shape already verified on writer open.
            from core.sessions.schema import APPLICATION_ID, SCHEMA_VERSION

            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if int(version) != SCHEMA_VERSION:
                raise SessionStoreCorruptError(f"stale reader version {version}")
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            if int(app_id) != APPLICATION_ID:
                raise SessionStoreCorruptError("not a vBot Session database")
        except sqlite3.Error:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
            else:
                # _get failed before open but permit was taken — release
                pass
            with self._read_conns_lock:
                self._read_open_failed_at = time.monotonic()
            _LOGGER.debug("read-only connection open failed for %s", self.db_path, exc_info=True)
            self._read_permits.release()
            return None
        except BaseException:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
            self._read_permits.release()
            raise
        return conn

    def _close_read_conn(self, conn: sqlite3.Connection) -> None:
        try:
            conn.close()
        except Exception as exc:
            _LOGGER.warning("read-conn close failed for %s: %s", self.db_path, exc)
        finally:
            try:
                self._read_permits.release()
            except ValueError:
                _LOGGER.warning("read permit over-release for %s", self.db_path)

    def _checkout_read_conn(self) -> sqlite3.Connection | None:
        if not self._wal_active:
            return None
        try:
            return self._read_pool.get_nowait()
        except queue.Empty:
            return self._get_read_conn()

    @contextlib.contextmanager
    def read_ctx(self):
        """Yield a connection for read-only statements (pool or writer)."""
        conn = self._checkout_read_conn()
        if conn is not None:
            try:
                yield conn
            finally:
                returned = False
                with self._read_conns_lock:
                    if not self._read_conns_closed:
                        try:
                            self._read_pool.put_nowait(conn)
                            returned = True
                        except queue.Full:
                            pass
                if not returned:
                    self._close_read_conn(conn)
            return
        # Degrade to writer lock
        with self._lock:
            if self._writer is None or self._closed:
                from core.chat.errors import ChatSessionError

                raise ChatSessionError("Session store is closed")
            yield self._writer

    # -- writer execution with patience --

    def execute_write(
        self,
        fn: Callable[[sqlite3.Connection], Any],
        *,
        patience_s: float = WRITE_PATIENCE_S,
    ) -> Any:
        """Run *fn(conn)* inside BEGIN IMMEDIATE with jittered busy retry.

        Releases the Python lock during sleep so other writers can make progress
        on the SQLite lock. Rolls back on every BaseException path without
        masking the original error. Counts a write only after commit and
        triggers a PASSIVE checkpoint every N commits; checkpoint failure is
        diagnostic hygiene and never fails the transaction.
        """
        deadline = time.monotonic() + patience_s
        while True:
            try:
                with self._lock:
                    if self._closed or self._writer is None:
                        from core.chat.errors import ChatSessionError

                        raise ChatSessionError("Session store is closed")
                    self._writer.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._writer)
                        self._writer.execute("COMMIT")
                    except BaseException:
                        try:
                            if self._writer.in_transaction:
                                self._writer.execute("ROLLBACK")
                        except Exception:
                            pass
                        raise
                # Success — hygiene outside the lock
                self._write_count += 1
                if self._write_count % _CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.Error as exc:
                if classify_unavailable(exc) and self._sleep_before_retry(deadline, patience_s):
                    continue
                raise
            except Exception:
                raise

    def _sleep_before_retry(self, deadline: float, patience_s: float) -> bool:
        now = time.monotonic()
        if now >= deadline:
            return False
        elapsed = now - (deadline - patience_s)
        if elapsed >= _WRITE_RETRY_SLOW_AFTER_S:
            jitter = random.uniform(_WRITE_RETRY_SLOW_MIN_S, _WRITE_RETRY_SLOW_MAX_S)
        else:
            jitter = random.uniform(_WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S)
        time.sleep(min(jitter, max(deadline - now, 0.001)))
        return True

    # -- verification helpers (thin wrappers) --

    def _verify_identity(self, conn: sqlite3.Connection) -> None:
        from core.sessions.schema import APPLICATION_ID, SCHEMA_VERSION

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if int(version) != SCHEMA_VERSION:
            raise SessionStoreCorruptError(f"unsupported version {version}")
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
        if int(app_id) != APPLICATION_ID:
            raise SessionStoreCorruptError("not a vBot Session database")

    def _verify_integrity(self, conn: sqlite3.Connection) -> None:
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        if str(integrity) != "ok":
            raise SessionStoreCorruptError(f"integrity check failed: {integrity}")
        fk = conn.execute("PRAGMA foreign_key_check").fetchone()
        if fk is not None:
            raise SessionStoreCorruptError("foreign-key check failed")

    # -- lifecycle --

    def close(self) -> None:
        with self._read_conns_lock:
            self._read_conns_closed = True
        while True:
            try:
                conn = self._read_pool.get_nowait()
            except queue.Empty:
                break
            self._close_read_conn(conn)
        with self._lock:
            if self._writer is not None:
                if not self._closed:
                    try:
                        self._writer.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    except Exception as exc:
                        _LOGGER.debug("checkpoint at close failed: %s", exc)
                conn, self._writer = self._writer, None
                with contextlib.suppress(Exception):
                    conn.close()
            self._closed = True

    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def wal_active(self) -> bool:
        return self._wal_active

    def live_connection_count(self) -> int:
        with _live_lock:
            return _live_connections.get(_key(self.db_path), 0)
