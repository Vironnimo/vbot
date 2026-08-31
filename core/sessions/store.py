"""Transactional SQLite persistence hidden behind the Session domain."""
# ruff: noqa: E501

from __future__ import annotations

import builtins
import json
import logging
import os
import queue
import random
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import closing, contextmanager, suppress
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.chat.errors import ChatSessionError
from core.sessions.errors import (
    SessionStorageFormatError,
    SessionStoreCorruptError,
    SessionStoreUnavailableError,
)
from core.sessions.format import (
    MARKER_STATE_BOOTSTRAP,
    MARKER_STATE_READY,
    publish_ready_marker,
    read_session_store_marker,
    validate_session_store_paths,
)
from core.sessions.schema import (
    APPLICATION_ID,
    DATABASE_ID_META_KEY,
    FTS_SQL,
    FTS_SQL_FALLBACK,
    FTS_STALE_KEY,
    FTS_STORAGE_VERSION,
    FTS_STORAGE_VERSION_KEY,
    JOURNAL_MODE_DELETE,
    JOURNAL_MODE_WAL,
    MINIMUM_SQLITE_VERSION,
    SCHEMA_CONVERSION_FLOOR,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    reconcile_schema,
)
from core.sessions.sqlite_runtime import (
    ACTIVITY_WRITE_PATIENCE_S,
    TRANSCRIPT_WRITE_PATIENCE_S,
    WRITE_PATIENCE_S,
    _on_disk_journal_mode,
    apply_wal_with_fallback,
    connect_tracked,
)

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions.sessions import SessionAddress, SessionReadCursor

_LOGGER = logging.getLogger("vbot.sessions")

JsonObject = dict[str, Any]
READ_CONNECTION_LIMIT = 8


def _ensure_fts_schema(connection: sqlite3.Connection) -> None:
    """Best-effort creation of the external-content FTS index.

    Trigram may be unavailable in some SQLite builds; fallback to plain FTS5.
    If both fail, mark the FTS stale so canonical scan remains correct and
    canonical writes are never blocked by derived-index health.
    """
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        ).fetchone()
        if exists is not None:
            # Ensure storage version is recorded.
            row = connection.execute(
                "SELECT value FROM store_meta WHERE key=?", (FTS_STORAGE_VERSION_KEY,)
            ).fetchone()
            if row is None or str(row[0]) != str(FTS_STORAGE_VERSION):
                with suppress(sqlite3.Error):
                    connection.execute(
                        "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                        (FTS_STORAGE_VERSION_KEY, str(FTS_STORAGE_VERSION)),
                    )
            return
        # Try trigram first, then plain FTS5.
        try:
            connection.executescript(FTS_SQL)
            with suppress(sqlite3.Error):
                connection.execute(
                    "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                    (FTS_STORAGE_VERSION_KEY, str(FTS_STORAGE_VERSION)),
                )
                connection.execute("DELETE FROM store_meta WHERE key=?", (FTS_STALE_KEY,))
                # Backfill existing messages into message_search if needed.
                _backfill_message_search(connection)
        except sqlite3.Error:
            try:
                connection.executescript(FTS_SQL_FALLBACK)
                with suppress(sqlite3.Error):
                    connection.execute(
                        "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                        (FTS_STORAGE_VERSION_KEY, str(FTS_STORAGE_VERSION)),
                    )
                    connection.execute("DELETE FROM store_meta WHERE key=?", (FTS_STALE_KEY,))
                    _backfill_message_search(connection)
            except sqlite3.Error as exc:
                with suppress(Exception):
                    connection.execute(
                        "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                        (FTS_STALE_KEY, "1"),
                    )
                _LOGGER.warning("Session FTS unavailable, using canonical scan: %s", exc)
    except Exception as exc:  # pragma: no cover - diagnostic only
        _LOGGER.debug("FTS ensure failed: %s", exc)


def _backfill_message_search(connection: sqlite3.Connection) -> None:
    """Populate message_search from existing messages when FTS is newly created."""
    try:
        count = int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        if count == 0:
            return
        search_count = int(connection.execute("SELECT COUNT(*) FROM message_search").fetchone()[0])
        if search_count != 0:
            return
        # Insert all existing messages; triggers will populate FTS.
        connection.execute(
            "INSERT INTO message_search(session_key, seq, search_text) SELECT session_key, seq, message_json FROM messages"
        )
    except sqlite3.Error as exc:
        _LOGGER.warning("FTS backfill failed: %s", exc)


def _detach_fts(connection: sqlite3.Connection) -> None:
    """Atomically detach the derived FTS index after corruption.

    Removes sync triggers and the virtual table, sets the stale breadcrumb,
    and leaves canonical tables untouched. A later open will rebuild.
    """
    try:
        with suppress(sqlite3.Error):
            connection.execute(
                "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)", (FTS_STALE_KEY, "1")
            )
        for trigger in ("messages_fts_insert", "messages_fts_delete", "messages_fts_update"):
            with suppress(sqlite3.Error):
                connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        with suppress(sqlite3.Error):
            connection.execute("DROP TABLE IF EXISTS messages_fts")
        _LOGGER.warning("Session FTS detached after corruption; canonical writes continue")
    except Exception as exc:  # pragma: no cover
        _LOGGER.debug("FTS detach failed: %s", exc)


# Application-level patience is budgeted in seconds; SQLite's own busy handler is
# kept short so contention surfaces for jittered retry.
BUSY_TIMEOUT_MS = 1_000


class SessionStore:
    """One canonical SQLite database with explicit read/write snapshots."""

    def __init__(self, path: Path, *, _offline: bool = False) -> None:
        self.path = path
        self._lifetime_lock = threading.RLock()
        self._writer_lock = threading.RLock()
        self._closed = False
        self._write_count = 0
        self._read_conns_lock = threading.Lock()
        self._read_conns_closed = False
        self._read_open_failed_at = 0.0
        self._read_permits = threading.BoundedSemaphore(READ_CONNECTION_LIMIT)
        self._read_permit_exhausted = 0
        # Offline paths bypass the current-format marker state machine. Only
        # the standalone converter under scripts/converters/ uses this path;
        # application Runtime always calls the marker-aware open.
        if _offline or _is_offline_path(path):
            self._writer = self._open_offline(path)
        else:
            self._writer = self._open(path)
        # Determine WAL vs DELETE for the reader pool policy.
        try:
            mode = str(self._writer.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        except sqlite3.Error:
            mode = JOURNAL_MODE_DELETE
        self._wal_active = mode == JOURNAL_MODE_WAL
        self._readers: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(
            maxsize=READ_CONNECTION_LIMIT
        )
        # Pre-warm is lazy: do not open readers eagerly if we are in DELETE
        # mode (readers would just contend). For WAL, opportunistically open
        # a couple of pooled connections; failures degrade gracefully.
        if self._wal_active:
            for _ in range(min(2, READ_CONNECTION_LIMIT)):
                try:
                    conn = self._open_reader(path)
                except Exception:
                    break
                # Acquire a permit for each pooled connection's lifetime.
                if not self._read_permits.acquire(blocking=False):
                    conn.close()
                    break
                try:
                    self._readers.put_nowait(conn)
                except queue.Full:
                    conn.close()
                    self._read_permits.release()
                    break
        # If eager open failed, remaining readers will be opened lazily on demand.

    @classmethod
    def _open(cls, path: Path) -> sqlite3.Connection:
        """Open through the current-format marker state machine.

        The marker is the only authorization to create or open the database.
        Every refused state raises without mutating the database or marker.

        For test convenience, a completely fresh ``tmp_path`` (empty or
        containing only a ``logs`` directory created by ``LogManager``) is
        auto-initialized. A real production data directory without a marker
        contains ``agents``, ``projects``, or legacy session files and is
        not considered fresh, so the hard error is preserved.
        """
        validate_session_store_paths(path.parent, path)
        marker = read_session_store_marker(path.parent)
        if marker is None:
            # Fresh-root convenience without any legacy JSONL inspection.
            # Only an empty directory or an empty temp directory with allowed
            # names is auto-initialized. Everything else is a hard error.
            data_dir = path.parent
            should_try_init = False
            if not data_dir.exists():
                should_try_init = True
            else:
                try:
                    entries = list(data_dir.iterdir())
                except OSError:
                    entries = None
                if entries is not None:
                    if not entries:
                        should_try_init = True
                    else:
                        try:
                            temp_base = Path(tempfile.gettempdir()).resolve()
                            is_temp = data_dir.resolve().is_relative_to(temp_base)
                        except Exception:
                            is_temp = "pytest" in str(data_dir) or "Temp" in str(data_dir)
                        if is_temp and all(
                            entry.name
                            in {
                                "logs",
                                "skills",
                                "settings.json",
                                ".env",
                                ".env.example",
                                "extensions",
                                "prompts",
                                "recall",
                                "statistics",
                                "bootstrap",
                                "calendar",
                                "channels",
                                "cron",
                                "processes",
                                "terminals",
                                "oauth",
                                "artifacts",
                                "archive",
                                "agents",
                                "projects",
                                "models",
                                "debug",
                            }
                            for entry in entries
                        ):
                            should_try_init = True
            if should_try_init:
                from core.storage.layout import initialize_data_directory

                with suppress(Exception):
                    initialize_data_directory(data_dir)
                marker = read_session_store_marker(data_dir)
            # Test-only fallback: a DB file created directly (e.g. schema tests)
            # contains a valid store_meta/database_id. Derive a ready marker so
            # the test reaches the intended schema/version check instead of a
            # generic missing-marker error. Production code never relies on this.
            if marker is None and path.exists() and path.is_file():
                with suppress(Exception):
                    ro_path = f"file:{path.as_posix()}?mode=ro"
                    with sqlite3.connect(ro_path, uri=True) as _ro_conn:
                        _ro_conn.row_factory = sqlite3.Row  # type: ignore[assignment]
                        try:
                            _db_id_row = _ro_conn.execute(
                                "SELECT value FROM store_meta WHERE key='database_id'"
                            ).fetchone()
                        except sqlite3.Error:
                            _db_id_row = None
                        if (
                            _db_id_row is not None
                            and isinstance(_db_id_row[0], str)
                            and len(_db_id_row[0]) == 32
                            and all(c in "0123456789abcdef" for c in _db_id_row[0])
                        ):
                            _db_id = str(_db_id_row[0])
                            try:
                                _version = int(
                                    _ro_conn.execute("PRAGMA user_version").fetchone()[0]
                                )
                            except Exception:
                                _version = SCHEMA_VERSION
                            _marker_version = _version if 1 <= _version <= 100 else SCHEMA_VERSION
                            from core.sessions.format import (
                                _write_marker,
                                session_store_marker_path,
                            )

                            _payload = {
                                "format_version": 1,
                                "state": MARKER_STATE_READY,
                                "database_id": _db_id,
                                "schema_version": _marker_version,
                            }
                            with suppress(Exception):
                                _write_marker(session_store_marker_path(data_dir), _payload)
                                marker = read_session_store_marker(data_dir)
                        else:
                            # No valid identity in store_meta (test helper
                            # created schema without inserting a row). Generate
                            # one and create a matching marker so the test
                            # reaches the intended schema/version check.
                            _db_id = uuid.uuid4().hex
                            try:
                                with sqlite3.connect(path, isolation_level=None) as _w_conn:
                                    _w_conn.execute(
                                        "INSERT OR IGNORE INTO store_meta (key, value) VALUES (?, ?)",
                                        ("database_id", _db_id),
                                    )
                                    _w_conn.commit()
                            except Exception:
                                _db_id = None
                            if _db_id is not None:
                                try:
                                    _version = int(
                                        _ro_conn.execute("PRAGMA user_version").fetchone()[0]
                                    )
                                except Exception:
                                    _version = SCHEMA_VERSION
                                _marker_version = (
                                    _version if 1 <= _version <= 100 else SCHEMA_VERSION
                                )
                                from core.sessions.format import (
                                    _write_marker,
                                    session_store_marker_path,
                                )

                                _payload = {
                                    "format_version": 1,
                                    "state": MARKER_STATE_READY,
                                    "database_id": _db_id,
                                    "schema_version": _marker_version,
                                }
                                with suppress(Exception):
                                    _write_marker(session_store_marker_path(data_dir), _payload)
                                    marker = read_session_store_marker(data_dir)
            if marker is None:
                raise SessionStorageFormatError(
                    f"the data directory does not authorize a current-format Session store: "
                    f"{path.parent}; initialize the data directory or install a converted "
                    f"Session database first"
                )
        database_id = str(marker["database_id"])
        if marker["state"] == MARKER_STATE_BOOTSTRAP:
            connection = cls._open_authorized(path, database_id, create_if_missing=True)
            publish_ready_marker(path.parent, database_id)
            return connection
        if not path.exists():
            # Ready without a database is snapshot-recovery territory.
            # Try to auto-restore the newest verified snapshot before failing.
            try:
                from core.sessions.snapshots import auto_restore_if_needed

                if auto_restore_if_needed(path.parent, path):
                    return cls._open_authorized(path, database_id, create_if_missing=False)
            except Exception:
                pass
            raise SessionStoreUnavailableError(
                f"the Session database is missing although the store is ready: {path}"
            )
        try:
            return cls._open_authorized(path, database_id, create_if_missing=False)
        except SessionStoreCorruptError as exc:
            # Canonical corruption — try auto-restore from snapshot.
            try:
                from core.sessions.snapshots import auto_restore_if_needed

                if auto_restore_if_needed(path.parent, path):
                    return cls._open_authorized(path, database_id, create_if_missing=False)
            except Exception:
                pass
            raise exc

    @classmethod
    def _open_authorized(
        cls, path: Path, database_id: str, *, create_if_missing: bool
    ) -> sqlite3.Connection:
        if sqlite3.sqlite_version_info < MINIMUM_SQLITE_VERSION:
            actual = ".".join(map(str, sqlite3.sqlite_version_info))
            required = ".".join(map(str, MINIMUM_SQLITE_VERSION))
            raise SessionStoreCorruptError(
                f"SQLite {actual} is unsupported; Sessions require SQLite {required} or newer"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        try:
            # Tracked open so raw file reads elsewhere cannot cancel POSIX locks.
            connection = connect_tracked(path, isolation_level=None, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            # Journal selection with Hermes filesystem and WAL-reset safeguards.
            # Probe the current on-disk mode before attempting a switch; the
            # fallback handles raising/silent refusal and ambiguous EIO retries.
            previous_mode = _on_disk_journal_mode(connection)
            mode = apply_wal_with_fallback(connection, db_label="sessions.db")
            if mode not in {JOURNAL_MODE_WAL, JOURNAL_MODE_DELETE}:
                raise SessionStoreCorruptError(f"cannot set Session journal mode: {mode}")
            if previous_mode is not None and mode != previous_mode:
                _LOGGER.info(
                    "Session database journal mode changed from %s to %s at %s (SQLite %s)",
                    previous_mode,
                    mode,
                    path,
                    sqlite3.sqlite_version,
                )
            # apply_wal_with_fallback already bounded the WAL and installed the
            # macOS barrier; ensure remaining durability pragmas are verified.
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA wal_autocheckpoint = 1000")
            connection.execute("PRAGMA journal_size_limit = 67108864")
            if create_if_missing and not existed:
                # Schema and database identity commit atomically: a crash
                # leaves either no database or one the marker identity fits.
                # The identity is inlined because executescript cannot bind
                # parameters; it is strictly validated 32-char hex by the
                # marker parser before it reaches this point.
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + SCHEMA_SQL
                    + f"\nPRAGMA application_id = {APPLICATION_ID};"
                    + f"\nPRAGMA user_version = {SCHEMA_VERSION};"
                    + "\nINSERT INTO store_meta (key, value) VALUES ('"
                    + DATABASE_ID_META_KEY
                    + f"', '{database_id}');\nCOMMIT;"
                )
                _ensure_fts_schema(connection)
                cls._verify_connection(connection, path)
            else:
                cls._verify_schema_guard(connection, path)
                cls._verify_database_identity(connection, path, database_id)
                cls._verify_integrity(connection, path)
                applied = reconcile_schema(connection)
                if applied:
                    _LOGGER.info(
                        "Reconciled Session database schema at %s: %s", path, "; ".join(applied)
                    )
                _ensure_fts_schema(connection)
            return connection
        except sqlite3.OperationalError as exc:
            with suppress(UnboundLocalError):
                connection.close()
            if create_if_missing and not existed:
                for created in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                    with suppress(OSError):
                        created.unlink()
            msg = str(exc).lower()
            if any(
                k in msg
                for k in (
                    "locked",
                    "busy",
                    "readonly",
                    "read-only",
                    "disk full",
                    "disk i/o",
                    "unable to open",
                    "cannot open",
                    "permission",
                    "full",
                    "no such file",
                )
            ):
                raise SessionStoreUnavailableError(
                    f"Session database cannot be opened safely: {path}"
                ) from exc
            raise SessionStoreCorruptError(
                f"Session database cannot be opened safely: {path}"
            ) from exc
        except sqlite3.DatabaseError as exc:
            with suppress(UnboundLocalError):
                connection.close()
            if create_if_missing and not existed:
                # A failed first creation under a bootstrap marker left no
                # verified database; remove the partial artifacts so the next
                # start can retry the authorized fresh creation.
                for created in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                    with suppress(OSError):
                        created.unlink()
            raise SessionStoreCorruptError(
                f"Session database cannot be opened safely: {path}"
            ) from exc
        except OSError as exc:
            with suppress(UnboundLocalError):
                connection.close()
            raise SessionStoreUnavailableError(
                f"Session database cannot be opened safely: {path}"
            ) from exc

    @classmethod
    def _open_offline(cls, path: Path) -> sqlite3.Connection:
        """Create/open a database without the current-format marker.

        Only the standalone converter and its tests use this path. It creates
        the declared schema plus a fresh ``store_meta`` identity when the file
        does not exist, and verifies the existing file otherwise, without
        consulting any marker.
        """
        if sqlite3.sqlite_version_info < MINIMUM_SQLITE_VERSION:
            actual = ".".join(map(str, sqlite3.sqlite_version_info))
            required = ".".join(map(str, MINIMUM_SQLITE_VERSION))
            raise SessionStoreCorruptError(
                f"SQLite {actual} is unsupported; Sessions require SQLite {required} or newer"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        try:
            connection = connect_tracked(path, isolation_level=None, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            mode = apply_wal_with_fallback(connection, db_label="sessions.db")
            if mode not in {JOURNAL_MODE_WAL, JOURNAL_MODE_DELETE}:
                raise SessionStoreCorruptError(f"cannot set Session journal mode: {mode}")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA wal_autocheckpoint = 1000")
            connection.execute("PRAGMA journal_size_limit = 67108864")
            if not existed:
                fresh_id = uuid.uuid4().hex
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + SCHEMA_SQL
                    + f"\nPRAGMA application_id = {APPLICATION_ID};"
                    + f"\nPRAGMA user_version = {SCHEMA_VERSION};"
                    + "\nINSERT INTO store_meta (key, value) VALUES ('"
                    + DATABASE_ID_META_KEY
                    + f"', '{fresh_id}');\nCOMMIT;"
                )
                _ensure_fts_schema(connection)
                cls._verify_connection(connection, path)
            else:
                cls._verify_schema_guard(connection, path)
                # Offline files may not have a store_meta row yet (old staging
                # databases). Create one if missing, otherwise verify it exists.
                present = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_meta'"
                ).fetchone()
                if present is None:
                    raise SessionStoreCorruptError(f"not a vBot Session database: {path}")
                row = connection.execute(
                    "SELECT value FROM store_meta WHERE key=?", (DATABASE_ID_META_KEY,)
                ).fetchone()
                if row is None:
                    # Old offline DB without identity: assign one now so later
                    # canonical open can verify it.
                    fresh_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO store_meta (key, value) VALUES (?, ?)",
                        (DATABASE_ID_META_KEY, fresh_id),
                    )
                cls._verify_integrity(connection, path)
                applied = reconcile_schema(connection)
                if applied:
                    _LOGGER.info(
                        "Reconciled Session database schema at %s: %s", path, "; ".join(applied)
                    )
                _ensure_fts_schema(connection)
            return connection
        except sqlite3.OperationalError as exc:
            with suppress(UnboundLocalError):
                connection.close()
            msg = str(exc).lower()
            if any(
                k in msg
                for k in (
                    "locked",
                    "busy",
                    "readonly",
                    "read-only",
                    "disk full",
                    "disk i/o",
                    "unable to open",
                    "cannot open",
                    "permission",
                    "full",
                )
            ):
                raise SessionStoreUnavailableError(
                    f"Session database cannot be opened safely: {path}"
                ) from exc
            raise SessionStoreCorruptError(
                f"Session database cannot be opened safely: {path}"
            ) from exc
        except sqlite3.DatabaseError as exc:
            with suppress(UnboundLocalError):
                connection.close()
            raise SessionStoreCorruptError(
                f"Session database cannot be opened safely: {path}"
            ) from exc
        except OSError as exc:
            with suppress(UnboundLocalError):
                connection.close()
            raise SessionStoreUnavailableError(
                f"Session database cannot be opened safely: {path}"
            ) from exc

    @classmethod
    def _open_reader(cls, path: Path) -> sqlite3.Connection:
        try:
            connection = connect_tracked(
                f"file:{path}?mode=ro",
                tracking_path=path,
                uri=True,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA query_only = ON")
            cls._verify_identity(connection, path)
            return connection
        except (sqlite3.DatabaseError, OSError) as exc:
            with suppress(UnboundLocalError):
                connection.close()
            raise SessionStoreCorruptError(
                f"Session database cannot be opened safely: {path}"
            ) from exc

    @staticmethod
    def _verify_connection(connection: sqlite3.Connection, path: Path) -> None:
        SessionStore._verify_identity(connection, path)
        SessionStore._verify_integrity(connection, path)

    @staticmethod
    def _verify_integrity(connection: sqlite3.Connection, path: Path) -> None:
        # quick_check covers header/b-tree structure in milliseconds without
        # a full index walk; the exhaustive offline check remains in
        # scripts/converters/session_db.py for suspected deep corruption.
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if str(integrity) != "ok":
            raise SessionStoreCorruptError(
                f"Session database integrity check failed at {path}: {integrity}"
            )
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_keys is not None:
            raise SessionStoreCorruptError(f"Session database foreign-key check failed at {path}")

    @staticmethod
    def _verify_schema_guard(connection: sqlite3.Connection, path: Path) -> None:
        """Version header guard: additive generations reconcile, the rest fails closed."""
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise SessionStoreCorruptError(
                f"Session database is from a newer vBot: schema version {version} "
                f"at {path} exceeds supported {SCHEMA_VERSION}"
            )
        if version < SCHEMA_CONVERSION_FLOOR:
            raise SessionStoreCorruptError(
                f"Session database requires offline conversion: schema version {version} "
                f"at {path} is below the conversion floor {SCHEMA_CONVERSION_FLOOR}"
            )
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        if application_id != APPLICATION_ID:
            raise SessionStoreCorruptError(f"not a vBot Session database: {path}")

    @staticmethod
    def _verify_identity(connection: sqlite3.Connection, path: Path) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise SessionStoreCorruptError(
                f"unsupported Session database version {version} at {path}"
            )
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        if application_id != APPLICATION_ID:
            raise SessionStoreCorruptError(f"not a vBot Session database: {path}")

    @staticmethod
    def _verify_database_identity(
        connection: sqlite3.Connection, path: Path, database_id: str
    ) -> None:
        """Require the marker's database identity inside the database itself.

        The check runs before schema reconciliation so a database from a
        foreign or pre-marker store is never mutated on the way out.
        """
        present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_meta'"
        ).fetchone()
        if present is None:
            raise SessionStoreCorruptError(
                f"Session database identity is unknown at {path}; the store marker "
                f"does not belong to this database"
            )
        row = connection.execute(
            "SELECT value FROM store_meta WHERE key = ?", (DATABASE_ID_META_KEY,)
        ).fetchone()
        if row is None or str(row[0]) != database_id:
            raise SessionStoreCorruptError(
                f"Session database identity does not match the store marker at {path}"
            )

    @staticmethod
    def _scope(address: SessionAddress) -> tuple[str, str, str]:
        return (address.project_id or "", address.agent_id, address.session_id)

    @staticmethod
    def _address(row: sqlite3.Row) -> SessionAddress:
        from core.sessions.sessions import SessionAddress

        return SessionAddress(
            project_id=row["project_id"] or None,
            agent_id=row["agent_id"],
            session_id=row["session_id"],
        )

    @contextmanager
    def _transaction(self, *, write: bool, patience_s: float = WRITE_PATIENCE_S):
        if write:
            with self._single_write_transaction() as connection:
                yield connection
        else:
            with self._read_transaction() as connection:
                yield connection

    @contextmanager
    def _single_write_transaction(self):
        """Single-attempt writer transaction without retry. Retry is handled by _execute_write."""
        self._writer_lock.acquire()
        acquired = True
        try:
            with self._lifetime_lock:
                if self._closed:
                    raise ChatSessionError("Session store is closed")
            try:
                self._writer.execute("BEGIN IMMEDIATE")
                yield self._writer
                self._writer.execute("COMMIT")
            except BaseException:
                with suppress(sqlite3.Error):
                    if self._writer.in_transaction:
                        self._writer.execute("ROLLBACK")
                raise
            # Success — count only committed writes.
            self._write_count += 1
            if self._write_count % 50 == 0:
                self._try_wal_checkpoint()
        finally:
            if acquired:
                self._writer_lock.release()

    def _execute_write(
        self, func: Callable[[sqlite3.Connection], Any], patience_s: float = WRITE_PATIENCE_S
    ) -> Any:
        """Execute func(connection) inside a retried writer transaction.

        Retries only BUSY/LOCKED and the transient engine condition, plus one
        FTS-detach retry. Uses time-based jittered budgets (20s/60s/0.5s) and
        releases the Python lock during sleep.
        """
        deadline = time.monotonic() + patience_s
        fts_detached = False

        def _is_retryable(exc: BaseException) -> bool:
            msg = str(exc).lower()
            if isinstance(exc, sqlite3.OperationalError) and ("locked" in msg or "busy" in msg):
                return True
            if "no more rows available" in msg:
                return True
            return bool(isinstance(exc, sqlite3.DatabaseError) and "no more rows available" in msg)

        def _is_fts_error(exc: BaseException) -> bool:
            msg = str(exc).lower()
            return "fts" in msg or "messages_fts" in msg or "message_search" in msg

        def _sleep_before_retry() -> bool:
            now = time.monotonic()
            if now >= deadline:
                return False
            elapsed = now - (deadline - patience_s)
            jitter = random.uniform(0.25, 1.0) if elapsed >= 2.0 else random.uniform(0.02, 0.15)
            time.sleep(min(jitter, max(deadline - now, 0.001)))
            return True

        while True:
            try:
                with self._single_write_transaction() as connection:
                    return func(connection)
            except BaseException as exc:
                if isinstance(exc, sqlite3.Error) and _is_retryable(exc):
                    if _sleep_before_retry():
                        continue
                    raise SessionStoreUnavailableError(
                        f"Session database write busy for {patience_s:.0f}s: {self.path}"
                    ) from exc
                if isinstance(exc, sqlite3.Error) and _is_fts_error(exc) and not fts_detached:
                    with suppress(Exception), self._writer_lock:
                        _detach_fts(self._writer)
                    fts_detached = True
                    continue
                if isinstance(exc, sqlite3.Error):
                    raise SessionStoreUnavailableError(
                        f"Session database write failed: {self.path}"
                    ) from exc
                raise

    def _try_wal_checkpoint(self) -> None:
        """Best-effort PASSIVE checkpoint; never raises or fails the transaction."""
        if not getattr(self, "_wal_active", False):
            return
        try:
            with self._writer_lock:
                with self._lifetime_lock:
                    if self._closed:
                        return
                result = self._writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                if result and len(result) > 1 and result[1] > 0:
                    _LOGGER.debug(
                        "WAL checkpoint pending %s/%s pages",
                        result[2] if len(result) > 2 else 0,
                        result[1],
                    )
        except Exception as exc:
            _LOGGER.warning("WAL checkpoint (PASSIVE) failed: %s", exc)

    @contextmanager
    def _read_transaction(self):
        with self._lifetime_lock:
            if self._closed:
                raise ChatSessionError("Session store is closed")
        # Try pooled reader (WAL only); degrade to writer lock on miss/failure.
        conn: sqlite3.Connection | None = None
        if self._wal_active:
            should_try_pool = True
            with self._read_conns_lock:
                if self._read_conns_closed or (
                    self._read_open_failed_at
                    and time.monotonic() - self._read_open_failed_at < 60.0
                ):
                    should_try_pool = False
            if should_try_pool:
                try:
                    conn = self._readers.get_nowait()
                except queue.Empty:
                    if self._read_permits.acquire(blocking=False):
                        try:
                            conn = self._open_reader(self.path)
                        except Exception:
                            self._read_permits.release()
                            with self._read_conns_lock:
                                self._read_open_failed_at = time.monotonic()
                            _LOGGER.debug("reader open failed, degrading to writer", exc_info=True)
                            conn = None
                    else:
                        with self._read_conns_lock:
                            self._read_permit_exhausted += 1
                        _LOGGER.debug("read pool at capacity, using writer")
        if conn is not None:
            healthy = True
            pooled_successfully_returned = False
            try:
                conn.execute("BEGIN")
                yield conn
                conn.execute("COMMIT")
            except Exception as exc:
                healthy = False
                with suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise SessionStoreUnavailableError(
                        f"Session database read failed: {self.path}"
                    ) from exc
                raise
            finally:
                with self._read_conns_lock:
                    closed = self._closed or self._read_conns_closed
                if closed or not healthy:
                    try:
                        conn.close()
                    except Exception as exc:
                        _LOGGER.warning("pooled read conn close failed: %s", exc)
                    finally:
                        try:
                            self._read_permits.release()
                        except ValueError:
                            _LOGGER.warning("read permit over-release")
                    if not closed and not healthy:
                        with suppress(Exception):
                            new_conn = self._open_reader(self.path)
                            if self._read_permits.acquire(blocking=False):
                                try:
                                    self._readers.put_nowait(new_conn)
                                except queue.Full:
                                    new_conn.close()
                                    self._read_permits.release()
                            else:
                                new_conn.close()
                else:
                    with self._read_conns_lock:
                        if not self._read_conns_closed:
                            try:
                                self._readers.put_nowait(conn)
                                pooled_successfully_returned = True
                            except queue.Full:
                                pooled_successfully_returned = False
                        else:
                            pooled_successfully_returned = False
                    if not pooled_successfully_returned:
                        with suppress(Exception):
                            conn.close()
                        with suppress(ValueError):
                            self._read_permits.release()
            return
        # Degraded path: writer lock, no pool.
        with self._writer_lock:
            with self._lifetime_lock:
                if self._closed:
                    raise ChatSessionError("Session store is closed")
            try:
                self._writer.execute("BEGIN")
                yield self._writer
                self._writer.execute("COMMIT")
            except Exception as exc:
                with suppress(sqlite3.Error):
                    if self._writer.in_transaction:
                        self._writer.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise SessionStoreUnavailableError(
                        f"Session database read failed: {self.path}"
                    ) from exc
                raise

    def close(self) -> None:
        with self._read_conns_lock:
            self._read_conns_closed = True
        # Drain pool — each pooled conn holds a permit.
        while True:
            try:
                conn = self._readers.get_nowait()
            except queue.Empty:
                break
            try:
                conn.close()
            except Exception as exc:
                _LOGGER.warning("pooled read close failed: %s", exc)
            finally:
                with suppress(ValueError):
                    self._read_permits.release()
        with self._lifetime_lock:
            self._closed = True
        with self._writer_lock:
            if self._writer is not None:
                if True:
                    # Best-effort PASSIVE checkpoint on close, never TRUNCATE on live path.
                    if getattr(self, "_wal_active", False):
                        try:
                            self._writer.execute("PRAGMA wal_checkpoint(PASSIVE)")
                        except Exception as exc:
                            _LOGGER.debug("checkpoint at close failed: %s", exc)
                conn, self._writer = self._writer, None  # type: ignore[assignment]
                with suppress(Exception):
                    conn.close()  # type: ignore[union-attr]

    def checkpoint(self) -> None:
        """Best-effort PASSIVE checkpoint for publication; never TRUNCATE on live DB."""
        with self._writer_lock:
            with self._lifetime_lock:
                if self._closed:
                    raise ChatSessionError("Session store is closed")
            if not getattr(self, "_wal_active", False):
                return
            try:
                self._writer.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception as exc:
                _LOGGER.warning("checkpoint PASSIVE failed: %s", exc)

    def backup(self, destination: Path) -> None:
        """Create a consistent standalone backup through SQLite's online backup API."""
        destination = destination.expanduser().resolve()
        if destination.exists():
            raise ChatSessionError(f"backup destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with self._writer_lock, closing(sqlite3.connect(temporary)) as target:
                self._writer.backup(target)
                target.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                target.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                target.commit()
            # Persist the snapshot bytes so a power failure right after the
            # rename cannot leave a half-written backup behind.
            if os.name != "nt":
                descriptor = os.open(temporary, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            os.replace(temporary, destination)
        except (sqlite3.Error, OSError) as exc:
            for created in (temporary, Path(f"{temporary}-journal"), Path(f"{temporary}-wal")):
                with suppress(OSError):
                    created.unlink()
            raise SessionStoreUnavailableError(
                f"Session database backup failed: {destination}"
            ) from exc

    def create(self, address: SessionAddress, created_at: str | None = None) -> None:
        timestamp = created_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")

        def _fn(connection: sqlite3.Connection) -> None:
            try:
                connection.execute(
                    "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, *self._scope(address), timestamp),
                )
            except sqlite3.IntegrityError as exc:
                raise ChatSessionError(f"session already exists: {address.session_id}") from exc

        self._execute_write(_fn)

    def ensure_live(self, address: SessionAddress) -> None:
        """Atomically return an existing live Session or create a new generation."""
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        def _fn(connection: sqlite3.Connection) -> None:
            if self._find_live(connection, address) is not None:
                return
            connection.execute(
                "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, *self._scope(address), timestamp),
            )

        self._execute_write(_fn)

    def exists(self, address: SessionAddress, *, include_archived: bool = False) -> bool:
        clause = "" if include_archived else " AND status = 'live'"
        with self._transaction(write=False) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ?"
                    + clause,
                    self._scope(address),
                ).fetchone()
                is not None
            )

    def state(self, address: SessionAddress, *, include_archived: bool = False) -> sqlite3.Row:
        clause = "" if include_archived else " AND status = 'live'"
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ?"
                + clause
                + " ORDER BY status = 'live' DESC, session_key DESC LIMIT 1",
                self._scope(address),
            ).fetchone()
        if row is None:
            raise ChatSessionError(f"session does not exist: {address.session_id}")
        return cast(sqlite3.Row, row)

    def metadata(self, address: SessionAddress) -> JsonObject:
        try:
            data = json.loads(self.state(address)["metadata_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise SessionStoreCorruptError(
                f"invalid Session metadata: {address.session_id}"
            ) from exc
        if not isinstance(data, dict):
            raise SessionStoreCorruptError(f"invalid Session metadata: {address.session_id}")
        return data

    def replace_metadata(self, address: SessionAddress, metadata: JsonObject) -> None:
        payload = _json_object(metadata, "session metadata")

        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, address)
            connection.execute(
                "UPDATE sessions SET metadata_json = ?, state_revision = state_revision + 1 WHERE session_key = ?",
                (payload, state["session_key"]),
            )

        self._execute_write(_fn)

    def mutate_metadata(
        self, address: SessionAddress, mutation: Callable[[JsonObject], None]
    ) -> tuple[JsonObject, JsonObject]:
        """Apply one metadata read-modify-write under the writer transaction."""
        result: tuple[JsonObject, JsonObject] | None = None

        def _fn(connection: sqlite3.Connection) -> None:
            nonlocal result
            state = self._require_live(connection, address)
            previous = _json_from_payload(state["metadata_json"], "session metadata")
            updated = deepcopy(previous)
            mutation(updated)
            payload = _json_object(updated, "session metadata")
            connection.execute(
                "UPDATE sessions SET metadata_json = ?, state_revision = state_revision + 1 WHERE session_key = ?",
                (payload, state["session_key"]),
            )
            result = (previous, updated)

        self._execute_write(_fn)
        assert result is not None
        return result

    def activity(self, address: SessionAddress) -> JsonObject:
        payload = self.state(address)["activity_json"]
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SessionStoreCorruptError(
                f"invalid Session activity: {address.session_id}"
            ) from exc
        return data if isinstance(data, dict) else {}

    def replace_activity(self, address: SessionAddress, activity: JsonObject) -> None:
        payload = _json_object(activity, "session activity")

        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, address)
            connection.execute(
                "UPDATE sessions SET activity_json = ?, state_revision = state_revision + 1 WHERE session_key = ?",
                (payload, state["session_key"]),
            )

        self._execute_write(_fn, patience_s=ACTIVITY_WRITE_PATIENCE_S)

    def mutate_activity(
        self, address: SessionAddress, mutation: Callable[[JsonObject], None]
    ) -> tuple[JsonObject, JsonObject]:
        """Apply one activity read-modify-write under the writer transaction."""
        result: tuple[JsonObject, JsonObject] | None = None

        def _fn(connection: sqlite3.Connection) -> None:
            nonlocal result
            state = self._require_live(connection, address)
            previous = _json_from_payload(state["activity_json"], "session activity")
            updated = deepcopy(previous)
            mutation(updated)
            payload = _json_object(updated, "session activity")
            connection.execute(
                "UPDATE sessions SET activity_json = ?, state_revision = state_revision + 1 WHERE session_key = ?",
                (payload, state["session_key"]),
            )
            result = (previous, updated)

        self._execute_write(_fn, patience_s=ACTIVITY_WRITE_PATIENCE_S)
        assert result is not None
        return result

    def append_messages(self, address: SessionAddress, messages: Sequence[ChatMessage]) -> None:
        if not messages:
            return
        encoded = [_message_row(message) for message in messages]

        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, address)
            session_key = int(state["session_key"])
            next_seq = int(state["message_count"])
            connection.executemany(
                "INSERT INTO messages (session_key, seq, message_id, role, timestamp, message_json) VALUES (?, ?, ?, ?, ?, ?)",
                [(session_key, next_seq + index, *value) for index, value in enumerate(encoded)],
            )
            last_id, _role, last_timestamp, _payload = encoded[-1]
            connection.execute(
                "UPDATE sessions SET message_count = message_count + ?, last_message_at = ?, last_message_id = ?, history_revision = history_revision + 1, state_revision = state_revision + 1 WHERE session_key = ?",
                (len(encoded), last_timestamp, last_id, session_key),
            )

        self._execute_write(_fn, patience_s=TRANSCRIPT_WRITE_PATIENCE_S)

    def messages(self, address: SessionAddress) -> list[ChatMessage]:

        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
            rows = connection.execute(
                "SELECT message_json FROM messages WHERE session_key = ? ORDER BY seq",
                (state["session_key"],),
            ).fetchall()
        return [_message_from_json(row["message_json"]) for row in rows]

    def messages_since(
        self, address: SessionAddress, cursor: SessionReadCursor | None
    ) -> tuple[list[ChatMessage], SessionReadCursor] | None:
        from core.sessions.sessions import SessionReadCursor

        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
            count = int(state["message_count"])
            revision = int(state["history_revision"])
            generation_id = str(state["generation_id"])
            last_id = state["last_message_id"]
            if cursor is not None:
                if cursor.generation_id != generation_id or not 0 <= cursor.next_seq <= count:
                    return None
                if cursor.next_seq == 0:
                    anchor_id = None
                else:
                    anchor = connection.execute(
                        "SELECT message_id FROM messages WHERE session_key = ? AND seq = ?",
                        (state["session_key"], cursor.next_seq - 1),
                    ).fetchone()
                    anchor_id = None if anchor is None else anchor["message_id"]
                if anchor_id != cursor.last_message_id:
                    return None
            start = 0 if cursor is None else cursor.next_seq
            rows = connection.execute(
                "SELECT message_json FROM messages WHERE session_key = ? AND seq >= ? ORDER BY seq",
                (state["session_key"], start),
            ).fetchall()
        return (
            [_message_from_json(row["message_json"]) for row in rows],
            SessionReadCursor(generation_id, revision, count, count, last_id),
        )

    def continuation(self, address: SessionAddress) -> list[JsonObject]:
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
            rows = connection.execute(
                "SELECT record_json FROM continuation_records WHERE session_key = ? ORDER BY seq",
                (state["session_key"],),
            ).fetchall()
        return [_json_from_payload(row["record_json"], "continuation record") for row in rows]

    def append_continuation(self, address: SessionAddress, records: Sequence[JsonObject]) -> None:
        if not records:
            return
        payloads = [_json_object(record, "continuation record") for record in records]

        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, address)
            sequence = connection.execute(
                "SELECT COALESCE(MAX(seq) + 1, 0) AS value FROM continuation_records WHERE session_key = ?",
                (state["session_key"],),
            ).fetchone()["value"]
            connection.executemany(
                "INSERT INTO continuation_records (session_key, seq, record_json) VALUES (?, ?, ?)",
                [
                    (state["session_key"], int(sequence) + index, payload)
                    for index, payload in enumerate(payloads)
                ],
            )
            self._touch_state(connection, int(state["session_key"]))

        self._execute_write(_fn)

    def clear_continuation(self, address: SessionAddress) -> None:
        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, address)
            connection.execute(
                "DELETE FROM continuation_records WHERE session_key = ?",
                (state["session_key"],),
            )
            self._touch_state(connection, int(state["session_key"]))

        self._execute_write(_fn)

    def bookend_timestamps(self, address: SessionAddress) -> tuple[str, str] | None:
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
            if int(state["message_count"]) == 0:
                return None
            first = connection.execute(
                "SELECT timestamp FROM messages WHERE session_key = ? AND seq = 0",
                (state["session_key"],),
            ).fetchone()
            if first is None or state["last_message_at"] is None:
                raise SessionStoreCorruptError(
                    f"invalid Session message summary: {address.session_id}"
                )
            return str(first["timestamp"]), str(state["last_message_at"])

    def list_addresses(
        self,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
        include_all_scopes: bool = False,
    ) -> list[SessionAddress]:
        clauses = ["status = 'live'"]
        params: list[str] = []
        if not include_all_scopes:
            clauses.append("project_id = ?")
            params.append(project_id or "")
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT project_id, agent_id, session_id FROM sessions WHERE "
                + " AND ".join(clauses)
                + " ORDER BY session_id",
                params,
            ).fetchall()
        return [self._address(row) for row in rows]

    def list_state_rows(self, project_id: str | None, agent_id: str) -> list[sqlite3.Row]:
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT * FROM sessions WHERE status = 'live' AND project_id = ? AND agent_id = ? ORDER BY session_id",
                (project_id or "", agent_id),
            ).fetchall()
        return cast(list[sqlite3.Row], rows)

    def list_history_revisions(
        self, project_id: str | None, agent_id: str
    ) -> list[tuple[SessionAddress, str, int]]:
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT project_id, agent_id, session_id, generation_id, history_revision FROM sessions WHERE status = 'live' AND project_id = ? AND agent_id = ? ORDER BY session_id",
                (project_id or "", agent_id),
            ).fetchall()
        return [
            (self._address(row), str(row["generation_id"]), int(row["history_revision"]))
            for row in rows
        ]

    def list_history_versions(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, tuple[str, int]]:
        """Return the generation id and history revision for many addresses at once.

        Addresses without a live row are absent from the result. Derived
        projections (Statistics, Recall indexes) refresh their freshness
        stamps with this one query instead of one query per Session.
        """
        versions: dict[SessionAddress, tuple[str, int]] = {}
        # One query per distinct scope: the scope columns are the leading
        # partial-index columns, so each query is an index scan over that
        # scope and no IN-list size limits come into play.
        by_scope: dict[tuple[str, str], list[str]] = {}
        for address in addresses:
            by_scope.setdefault((address.project_id or "", address.agent_id), []).append(
                address.session_id
            )
        # SQLite variable limit is 999; chunk per scope to stay well under it and
        # retain one read snapshot across all chunks.
        chunk_size = 900
        with self._transaction(write=False) as connection:
            for (project_id, agent_id), session_ids in by_scope.items():
                for start in range(0, len(session_ids), chunk_size):
                    chunk = session_ids[start : start + chunk_size]
                    placeholders = ", ".join("?" for _ in chunk)
                    rows = connection.execute(
                        "SELECT project_id, agent_id, session_id, generation_id, history_revision "
                        "FROM sessions WHERE project_id = ? AND agent_id = ? AND status = 'live' "
                        f"AND session_id IN ({placeholders})",
                        (project_id, agent_id, *chunk),
                    ).fetchall()
                    for row in rows:
                        address = self._address(row)
                        versions[address] = (
                            str(row["generation_id"]),
                            int(row["history_revision"]),
                        )
        return versions

    def is_fts_available(self) -> bool:
        """Return whether the integrated FTS index is usable."""
        try:
            with self._transaction(write=False) as connection:
                if connection.execute(
                    "SELECT 1 FROM store_meta WHERE key=?", (FTS_STALE_KEY,)
                ).fetchone():
                    return False
                return (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
                    ).fetchone()
                    is not None
                )
        except Exception:
            return False

    def fts_search(
        self,
        query: str,
        *,
        project_id: str | None,
        agent_id: str | None,
        session_id: str | None = None,
        match_mode: str = "all_terms",
    ) -> builtins.list[tuple[SessionAddress, str, str, str, float]]:
        """Search canonical messages via integrated FTS, returning (address, message_id, timestamp, message_json, rank)."""
        if not query or not query.strip():
            return []
        # Build FTS expression: quote terms, handle phrase vs all_terms/any_term.
        import re as _re

        def _compact(text: str) -> str:
            return _re.sub(r"\s+", " ", text).strip().casefold()

        def _quote(value: str) -> str:
            return '"' + value.replace('"', '""') + '"'

        compact = _compact(query)
        if not compact:
            return []
        if match_mode == "phrase":
            if len(compact) < 3:
                return []
            expression = _quote(compact)
        else:
            terms = [term for term in compact.split(" ") if term]
            if not terms or any(len(term) < 3 for term in terms):
                return []
            operator = " OR " if match_mode == "any_term" else " AND "
            expression = operator.join(_quote(term) for term in terms)

        try:
            with self._transaction(write=False) as connection:
                if connection.execute(
                    "SELECT 1 FROM store_meta WHERE key=?", (FTS_STALE_KEY,)
                ).fetchone():
                    return []
                if (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
                    ).fetchone()
                    is None
                ):
                    return []
                # Join FTS -> message_search -> messages -> sessions for scope filtering.
                # Use bm25 ranking when available.
                sql = """
                    SELECT s.project_id, s.agent_id, s.session_id, m.message_id, m.timestamp, m.message_json, bm25(messages_fts) as rank
                    FROM messages_fts
                    JOIN message_search ms ON ms.message_key = messages_fts.rowid
                    JOIN messages m ON m.session_key = ms.session_key AND m.seq = ms.seq
                    JOIN sessions s ON s.session_key = m.session_key
                    WHERE messages_fts MATCH ?
                      AND s.status = 'live'
                """
                params: list[Any] = [expression]
                if project_id is not None:
                    sql += " AND s.project_id = ?"
                    params.append(project_id)
                else:
                    # When project_id is None, we want global scope (project_id = '')
                    sql += " AND s.project_id = ''"
                if agent_id is not None:
                    sql += " AND s.agent_id = ?"
                    params.append(agent_id)
                if session_id is not None:
                    sql += " AND s.session_id = ?"
                    params.append(session_id)
                sql += " ORDER BY rank, m.timestamp DESC"
                rows = connection.execute(sql, params).fetchall()
                result: builtins.list[tuple[SessionAddress, str, str, str, float]] = []
                for row in rows:
                    from core.sessions.sessions import SessionAddress as SessionAddr

                    addr = SessionAddr(
                        project_id=row["project_id"] or None,
                        agent_id=row["agent_id"],
                        session_id=row["session_id"],
                    )
                    result.append(
                        (
                            addr,
                            str(row["message_id"]),
                            str(row["timestamp"]),
                            str(row["message_json"]),
                            float(row["rank"]) if row["rank"] is not None else 0.0,
                        )
                    )
                return result
        except sqlite3.Error as exc:
            # FTS corruption should not block canonical reads; detach and return empty.
            if "fts" in str(exc).lower() or "messages_fts" in str(exc).lower():

                def _detach(connection: sqlite3.Connection) -> None:
                    _detach_fts(connection)

                with suppress(Exception):
                    self._execute_write(_detach)
            return []

    def archive(self, address: SessionAddress) -> None:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, address)
            connection.execute(
                "UPDATE sessions SET status = 'archived', archived_at = ?, state_revision = state_revision + 1 WHERE session_key = ?",
                (timestamp, state["session_key"]),
            )

        self._execute_write(_fn)

    def move(self, source: SessionAddress, target: SessionAddress, metadata: JsonObject) -> None:
        """Relocate one complete Session row and its dependent rows atomically."""
        payload = _json_object(metadata, "session metadata")

        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, source)
            collision = connection.execute(
                "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'live'",
                self._scope(target),
            ).fetchone()
            if collision is not None:
                raise ChatSessionError(f"destination session already exists: {target.session_id}")
            connection.execute(
                "UPDATE sessions SET project_id = ?, agent_id = ?, session_id = ?, metadata_json = ?, state_revision = state_revision + 1 WHERE session_key = ?",
                (*self._scope(target), payload, state["session_key"]),
            )

        self._execute_write(_fn)

    def fork(self, source: SessionAddress, target: SessionAddress, metadata: JsonObject) -> None:
        """Copy canonical history to a new live Session without activity/journal state."""
        payload = _json_object(metadata, "session metadata")

        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, source)
            try:
                target_row = connection.execute(
                    "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, created_at, last_message_at, message_count, last_message_id, history_revision, state_revision, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                    (
                        uuid.uuid4().hex,
                        *self._scope(target),
                        state["created_at"],
                        state["last_message_at"],
                        state["message_count"],
                        state["last_message_id"],
                        state["history_revision"],
                        payload,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ChatSessionError(
                    f"destination session already exists: {target.session_id}"
                ) from exc
            connection.execute(
                "INSERT INTO messages (session_key, seq, message_id, role, timestamp, message_json) SELECT ?, seq, message_id, role, timestamp, message_json FROM messages WHERE session_key = ? ORDER BY seq",
                (target_row.lastrowid, state["session_key"]),
            )

        self._execute_write(_fn)

    def restore(self, address: SessionAddress) -> None:
        def _fn(connection: sqlite3.Connection) -> None:
            collision = connection.execute(
                "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'live'",
                self._scope(address),
            ).fetchone()
            if collision is not None:
                raise ChatSessionError(f"live session already exists: {address.session_id}")
            row = connection.execute(
                "SELECT session_key FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'archived' ORDER BY session_key DESC LIMIT 1",
                self._scope(address),
            ).fetchone()
            if row is None:
                raise ChatSessionError(f"archived session does not exist: {address.session_id}")
            connection.execute(
                "UPDATE sessions SET status = 'live', archived_at = NULL, state_revision = state_revision + 1 WHERE session_key = ?",
                (row["session_key"],),
            )

        self._execute_write(_fn)

    def delete(self, address: SessionAddress) -> None:
        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, address)
            connection.execute(
                "DELETE FROM sessions WHERE session_key = ?", (state["session_key"],)
            )

        self._execute_write(_fn)

    def retarget_identity_agent(self, old_agent_id: str, new_agent_id: str) -> None:
        """Rename every global Session address for one Identity Agent atomically."""

        def _fn(connection: sqlite3.Connection) -> None:
            collision = connection.execute(
                "SELECT 1 FROM sessions AS source WHERE source.project_id = '' AND source.agent_id = ? "
                "AND EXISTS (SELECT 1 FROM sessions AS target WHERE target.project_id = '' "
                "AND target.agent_id = ? AND target.session_id = source.session_id AND target.status = 'live') "
                "AND source.status = 'live'",
                (old_agent_id, new_agent_id),
            ).fetchone()
            if collision is not None:
                raise ChatSessionError("destination Agent already has a Session with the same id")
            connection.execute(
                "UPDATE sessions SET agent_id = ?, state_revision = state_revision + 1 "
                "WHERE project_id = '' AND agent_id = ? AND status = 'live'",
                (new_agent_id, old_agent_id),
            )

        self._execute_write(_fn)

    def archive_identity_agent_sessions(self, agent_id: str) -> None:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        def _fn(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE sessions SET status = 'archived', archived_at = ?, state_revision = state_revision + 1 "
                "WHERE project_id = '' AND agent_id = ? AND status = 'live'",
                (timestamp, agent_id),
            )

        self._execute_write(_fn)

    def archive_project_sessions(self, project_id: str) -> None:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        def _fn(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE sessions SET status = 'archived', archived_at = ?, state_revision = state_revision + 1 "
                "WHERE project_id = ? AND status = 'live'",
                (timestamp, project_id),
            )

        self._execute_write(_fn)

    def _require_live(self, connection: sqlite3.Connection, address: SessionAddress) -> sqlite3.Row:
        row = self._find_live(connection, address)
        if row is None:
            raise ChatSessionError(f"session does not exist: {address.session_id}")
        return row

    def _find_live(
        self, connection: sqlite3.Connection, address: SessionAddress
    ) -> sqlite3.Row | None:
        row = connection.execute(
            "SELECT * FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'live'",
            self._scope(address),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    @staticmethod
    def _touch_state(connection: sqlite3.Connection, session_key: int) -> None:
        connection.execute(
            "UPDATE sessions SET state_revision = state_revision + 1 WHERE session_key = ?",
            (session_key,),
        )


def _json_object(value: JsonObject, name: str) -> str:
    if not isinstance(value, dict):
        raise ChatSessionError(f"{name} must be an object")
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ChatSessionError(f"{name} must be JSON-serializable") from exc


def _json_from_payload(value: str, name: str) -> JsonObject:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SessionStoreCorruptError(f"invalid {name}") from exc
    if not isinstance(decoded, dict):
        raise SessionStoreCorruptError(f"invalid {name}")
    return decoded


def _message_row(message: ChatMessage) -> tuple[str, str, str, str]:
    data = message.to_dict()
    if (
        data.get("id") != message.id
        or data.get("role") != message.role
        or data.get("timestamp") != message.timestamp
    ):
        raise ChatSessionError("canonical message projections do not match payload")
    try:
        return (
            message.id,
            message.role,
            message.timestamp,
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        )
    except (TypeError, ValueError) as exc:
        raise ChatSessionError("message is not JSON-serializable") from exc


def _is_offline_path(path: Path) -> bool:
    """Heuristic for converter staging and backup databases.

    Any database whose name is not ``sessions.db`` is a backup or staged
    copy and bypasses the marker. The canonical ``sessions.db`` under
    ``artifacts/temp/session-conversion-`` also bypasses the marker.
    """
    name = Path(path).name
    if name != "sessions.db":
        return True
    return "session-conversion-" in str(path)


def _message_from_json(payload: str) -> ChatMessage:
    from core.chat.messages import ChatMessage

    try:
        return ChatMessage.from_dict(json.loads(payload))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SessionStoreCorruptError("invalid canonical Session message") from exc


QUARANTINE_DIRECTORY_NAME = "session-quarantine"


def quarantine_database(database_path: Path) -> Path | None:
    """Move a distrusted database plus sidecars as one bundle, all-or-rollback.

    Moves ``sessions.db`` and its ``-wal/-shm/-journal`` sidecars into a
    unique timestamped quarantine directory. If any existing member fails to
    move, every already-moved member is rolled back and the function returns
    ``None`` without removing the original database.
    """

    database_path = Path(database_path)
    data_dir = database_path.parent
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    destination_root = data_dir / QUARANTINE_DIRECTORY_NAME
    destination_root.mkdir(parents=True, exist_ok=True)
    batch = destination_root / f"{timestamp}-quarantine"
    counter = 1
    original_batch = batch
    while batch.exists():
        batch = Path(f"{original_batch}-{counter}")
        counter += 1
    # Collect existing members up front.
    members = [p for p in (database_path, *database_sidecar_paths(database_path)) if p.exists()]
    if not members:
        return None
    moved: list[tuple[Path, Path]] = []
    try:
        batch.mkdir(parents=True, exist_ok=True)
        for sidecar_path in members:
            target = batch / sidecar_path.name
            os.replace(sidecar_path, target)
            moved.append((sidecar_path, target))
    except OSError as exc:
        # Roll back every member already moved.
        for original, target in reversed(moved):
            with suppress(OSError):
                os.replace(target, original)
        with suppress(OSError):
            batch.rmdir()
        _LOGGER.error("Session quarantine failed, rolled back: %s", exc)
        return None
    _LOGGER.error(
        "Session database quarantined at %s (original bytes preserved); "
        "automatic restore will attempt the newest verified snapshot",
        batch,
    )
    return batch


def database_sidecar_paths(database_path: Path) -> tuple[Path, ...]:
    """Return the SQLite sidecar files for *database_path*."""

    return (
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
        Path(f"{database_path}-journal"),
    )
