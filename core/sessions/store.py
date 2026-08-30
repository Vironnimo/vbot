"""Transactional SQLite persistence hidden behind the Session domain."""
# ruff: noqa: E501

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import uuid
from collections.abc import Callable, Sequence
from contextlib import closing, contextmanager, suppress
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.chat.errors import ChatSessionError
from core.sessions.errors import (
    SessionConversionIncompleteError,
    SessionConversionRequiredError,
    SessionStorageConflictError,
    SessionStoreCorruptError,
    SessionStoreUnavailableError,
)
from core.sessions.schema import (
    APPLICATION_ID,
    JOURNAL_MODE_DELETE,
    JOURNAL_MODE_WAL,
    MINIMUM_SQLITE_VERSION,
    SCHEMA_CONVERSION_FLOOR,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    reconcile_schema,
    required_journal_mode,
)
from core.settings import is_valid_agent_id, is_valid_project_id

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions.sessions import SessionAddress, SessionReadCursor

_LOGGER = logging.getLogger("vbot.sessions")

JsonObject = dict[str, Any]
CONVERSION_MARKER_NAME = "session-conversion.json"
READ_CONNECTION_LIMIT = 8
BUSY_TIMEOUT_MS = 5_000


class SessionStore:
    """One canonical SQLite database with explicit read/write snapshots."""

    def __init__(self, path: Path, *, allow_conversion_marker: bool = False) -> None:
        self.path = path
        self._lifetime_lock = threading.RLock()
        self._writer_lock = threading.RLock()
        self._closed = False
        self._writer = self._open(path, allow_conversion_marker=allow_conversion_marker)
        self._readers: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(
            maxsize=READ_CONNECTION_LIMIT
        )
        try:
            for _ in range(READ_CONNECTION_LIMIT):
                self._readers.put_nowait(self._open_reader(path))
        except Exception:
            self._writer.close()
            while not self._readers.empty():
                self._readers.get_nowait().close()
            raise

    @classmethod
    def _open(cls, path: Path, *, allow_conversion_marker: bool) -> sqlite3.Connection:
        preflight_session_storage(path, allow_conversion_marker=allow_conversion_marker)
        if sqlite3.sqlite_version_info < MINIMUM_SQLITE_VERSION:
            actual = ".".join(map(str, sqlite3.sqlite_version_info))
            required = ".".join(map(str, MINIMUM_SQLITE_VERSION))
            raise SessionStoreCorruptError(
                f"SQLite {actual} is unsupported; Sessions require SQLite {required} or newer"
            )
        existed = path.exists()
        if existed and path.stat().st_size == 0:
            # An existing zero-byte file is the classic storage-death artifact
            # (SD card, crashed process); quarantine it and start fresh so the
            # server cannot be bricked by an unrecoverable file.
            quarantine_database(path)
            existed = False
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            requested_mode = required_journal_mode(sqlite3.sqlite_version_info)
            previous_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            mode = str(
                connection.execute(f"PRAGMA journal_mode = {requested_mode.upper()}").fetchone()[0]
            ).lower()
            if mode == JOURNAL_MODE_WAL and requested_mode == JOURNAL_MODE_DELETE:
                raise SessionStoreCorruptError(
                    f"cannot leave WAL mode on WAL-reset-vulnerable SQLite "
                    f"{sqlite3.sqlite_version}: {path}"
                )
            if mode not in {JOURNAL_MODE_WAL, JOURNAL_MODE_DELETE}:
                raise SessionStoreCorruptError(f"cannot set Session journal mode: {mode}")
            if mode != previous_mode:
                _LOGGER.info(
                    "Session database journal mode changed from %s to %s at %s (SQLite %s)",
                    previous_mode,
                    mode,
                    path,
                    sqlite3.sqlite_version,
                )
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA wal_autocheckpoint = 1000")
            connection.execute("PRAGMA journal_size_limit = 67108864")
            if not existed:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + SCHEMA_SQL
                    + f"\nPRAGMA application_id = {APPLICATION_ID};"
                    + f"\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
                )
                cls._verify_connection(connection, path)
            else:
                cls._verify_schema_guard(connection, path)
                cls._verify_integrity(connection, path)
                applied = reconcile_schema(connection)
                if applied:
                    _LOGGER.info(
                        "Reconciled Session database schema at %s: %s", path, "; ".join(applied)
                    )
            return connection
        except sqlite3.DatabaseError as exc:
            with suppress(UnboundLocalError):
                connection.close()
            if not existed:
                for created in (
                    path,
                    Path(f"{path}-wal"),
                    Path(f"{path}-shm"),
                    Path(f"{path}-journal"),
                ):
                    with suppress(OSError):
                        created.unlink()
                raise SessionStoreCorruptError(
                    f"Session database cannot be opened safely: {path}"
                ) from exc
            # An existing database that fails validation or integrity is
            # quarantined with its sidecar files and replaced by a fresh
            # database so the runtime degrades to an (empty) canonical store
            # instead of refusing to start entirely. Version-protocol
            # failures (newer schemas, conversion floor) surface above as
            # their own hard errors and never quarantine.
            if not cls._quarantine_files(path):
                raise SessionStoreCorruptError(
                    f"Session database cannot be opened safely: {path}"
                ) from exc
            return cls._open(path, allow_conversion_marker=allow_conversion_marker)
        except OSError as exc:
            with suppress(UnboundLocalError):
                connection.close()
            raise SessionStoreUnavailableError(
                f"Session database cannot be opened safely: {path}"
            ) from exc

    @classmethod
    def _open_reader(cls, path: Path) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
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
    def _quarantine_files(path: Path) -> bool:
        """Quarantine a distrusted database; False when nothing could be moved."""
        outcome = quarantine_database(path)
        return outcome is not None

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
    def _transaction(self, *, write: bool):
        if write:
            with self._write_transaction() as connection:
                yield connection
        else:
            with self._read_transaction() as connection:
                yield connection

    @contextmanager
    def _write_transaction(self):
        with self._writer_lock:
            with self._lifetime_lock:
                if self._closed:
                    raise ChatSessionError("Session store is closed")
            try:
                self._writer.execute("BEGIN IMMEDIATE")
                yield self._writer
                self._writer.execute("COMMIT")
            except Exception as exc:
                with suppress(sqlite3.Error):
                    if self._writer.in_transaction:
                        self._writer.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise SessionStoreUnavailableError(
                        f"Session database write failed: {self.path}"
                    ) from exc
                raise

    @contextmanager
    def _read_transaction(self):
        with self._lifetime_lock:
            if self._closed:
                raise ChatSessionError("Session store is closed")
        while True:
            with self._lifetime_lock:
                if self._closed:
                    raise ChatSessionError("Session store is closed")
            try:
                connection = self._readers.get(timeout=0.1)
                break
            except queue.Empty:
                continue
        healthy = True
        try:
            connection.execute("BEGIN")
            yield connection
            connection.execute("COMMIT")
        except Exception as exc:
            with suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                healthy = False
                raise SessionStoreUnavailableError(
                    f"Session database read failed: {self.path}"
                ) from exc
            raise
        finally:
            with self._lifetime_lock:
                closed = self._closed
            if closed or not healthy:
                connection.close()
                if not closed:
                    with suppress(SessionStoreCorruptError):
                        self._readers.put_nowait(self._open_reader(self.path))
            else:
                self._readers.put(connection)

    def close(self) -> None:
        with self._lifetime_lock:
            if not self._closed:
                self._closed = True
                while not self._readers.empty():
                    self._readers.get_nowait().close()
        with self._writer_lock:
            self._writer.close()

    def checkpoint(self) -> None:
        """Flush the WAL into the main database for publication or offline copying."""
        with self._writer_lock, self._lifetime_lock:
            if self._closed:
                raise ChatSessionError("Session store is closed")
            mode = str(self._writer.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if mode != JOURNAL_MODE_WAL:
                return  # Rollback-journal mode has no WAL to flush.
            result = self._writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result is None or int(result[0]) != 0:
                raise SessionStoreUnavailableError("Session database checkpoint is busy")

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
        with self._transaction(write=True) as connection:
            try:
                connection.execute(
                    "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, *self._scope(address), timestamp),
                )
            except sqlite3.IntegrityError as exc:
                raise ChatSessionError(f"session already exists: {address.session_id}") from exc

    def ensure_live(self, address: SessionAddress) -> None:
        """Atomically return an existing live Session or create a new generation."""
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self._transaction(write=True) as connection:
            if self._find_live(connection, address) is not None:
                return
            connection.execute(
                "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, *self._scope(address), timestamp),
            )

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
        with self._transaction(write=True) as connection:
            state = self._require_live(connection, address)
            connection.execute(
                "UPDATE sessions SET metadata_json = ?, state_revision = state_revision + 1 WHERE session_key = ?",
                (payload, state["session_key"]),
            )

    def mutate_metadata(
        self, address: SessionAddress, mutation: Callable[[JsonObject], None]
    ) -> tuple[JsonObject, JsonObject]:
        """Apply one metadata read-modify-write under the writer transaction."""
        with self._transaction(write=True) as connection:
            state = self._require_live(connection, address)
            previous = _json_from_payload(state["metadata_json"], "session metadata")
            updated = deepcopy(previous)
            mutation(updated)
            payload = _json_object(updated, "session metadata")
            connection.execute(
                "UPDATE sessions SET metadata_json = ?, state_revision = state_revision + 1 WHERE session_key = ?",
                (payload, state["session_key"]),
            )
        return previous, updated

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
        with self._transaction(write=True) as connection:
            state = self._require_live(connection, address)
            connection.execute(
                "UPDATE sessions SET activity_json = ?, state_revision = state_revision + 1 WHERE session_key = ?",
                (payload, state["session_key"]),
            )

    def mutate_activity(
        self, address: SessionAddress, mutation: Callable[[JsonObject], None]
    ) -> tuple[JsonObject, JsonObject]:
        """Apply one activity read-modify-write under the writer transaction."""
        with self._transaction(write=True) as connection:
            state = self._require_live(connection, address)
            previous = _json_from_payload(state["activity_json"], "session activity")
            updated = deepcopy(previous)
            mutation(updated)
            payload = _json_object(updated, "session activity")
            connection.execute(
                "UPDATE sessions SET activity_json = ?, state_revision = state_revision + 1 WHERE session_key = ?",
                (payload, state["session_key"]),
            )
        return previous, updated

    def append_messages(self, address: SessionAddress, messages: Sequence[ChatMessage]) -> None:
        if not messages:
            return
        encoded = [_message_row(message) for message in messages]
        with self._transaction(write=True) as connection:
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
        with self._transaction(write=True) as connection:
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

    def clear_continuation(self, address: SessionAddress) -> None:
        with self._transaction(write=True) as connection:
            state = self._require_live(connection, address)
            connection.execute(
                "DELETE FROM continuation_records WHERE session_key = ?",
                (state["session_key"],),
            )
            self._touch_state(connection, int(state["session_key"]))

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
        with self._transaction(write=False) as connection:
            for (project_id, agent_id), session_ids in by_scope.items():
                placeholders = ", ".join("?" for _ in session_ids)
                rows = connection.execute(
                    "SELECT project_id, agent_id, session_id, generation_id, history_revision "
                    "FROM sessions WHERE project_id = ? AND agent_id = ? AND status = 'live' "
                    f"AND session_id IN ({placeholders})",
                    (project_id, agent_id, *session_ids),
                ).fetchall()
                for row in rows:
                    address = self._address(row)
                    versions[address] = (
                        str(row["generation_id"]),
                        int(row["history_revision"]),
                    )
        return versions

    def archive(self, address: SessionAddress) -> None:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self._transaction(write=True) as connection:
            state = self._require_live(connection, address)
            connection.execute(
                "UPDATE sessions SET status = 'archived', archived_at = ?, state_revision = state_revision + 1 WHERE session_key = ?",
                (timestamp, state["session_key"]),
            )

    def move(self, source: SessionAddress, target: SessionAddress, metadata: JsonObject) -> None:
        """Relocate one complete Session row and its dependent rows atomically."""
        payload = _json_object(metadata, "session metadata")
        with self._transaction(write=True) as connection:
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

    def fork(self, source: SessionAddress, target: SessionAddress, metadata: JsonObject) -> None:
        """Copy canonical history to a new live Session without activity/journal state."""
        payload = _json_object(metadata, "session metadata")
        with self._transaction(write=True) as connection:
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

    def restore(self, address: SessionAddress) -> None:
        with self._transaction(write=True) as connection:
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

    def delete(self, address: SessionAddress) -> None:
        with self._transaction(write=True) as connection:
            state = self._require_live(connection, address)
            connection.execute(
                "DELETE FROM sessions WHERE session_key = ?", (state["session_key"],)
            )

    def retarget_identity_agent(self, old_agent_id: str, new_agent_id: str) -> None:
        """Rename every global Session address for one Identity Agent atomically."""
        with self._transaction(write=True) as connection:
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

    def archive_identity_agent_sessions(self, agent_id: str) -> None:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self._transaction(write=True) as connection:
            connection.execute(
                "UPDATE sessions SET status = 'archived', archived_at = ?, state_revision = state_revision + 1 "
                "WHERE project_id = '' AND agent_id = ? AND status = 'live'",
                (timestamp, agent_id),
            )

    def archive_project_sessions(self, project_id: str) -> None:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self._transaction(write=True) as connection:
            connection.execute(
                "UPDATE sessions SET status = 'archived', archived_at = ?, state_revision = state_revision + 1 "
                "WHERE project_id = ? AND status = 'live'",
                (timestamp, project_id),
            )

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


def _message_from_json(payload: str) -> ChatMessage:
    from core.chat.messages import ChatMessage

    try:
        return ChatMessage.from_dict(json.loads(payload))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SessionStoreCorruptError("invalid canonical Session message") from exc


def preflight_session_storage(path: Path, *, allow_conversion_marker: bool = False) -> str:
    """Reject ambiguous legacy/canonical states before SQLite can create a file."""
    data_dir = path.parent
    marker = data_dir / CONVERSION_MARKER_NAME
    if marker.exists() and not allow_conversion_marker:
        raise SessionConversionIncompleteError(
            "Session conversion is incomplete; resume the offline converter before starting vBot"
        )
    legacy = _legacy_paths(data_dir)
    database_exists = path.exists()
    if database_exists and legacy:
        raise SessionStorageConflictError(
            "both sessions.db and legacy JSONL Sessions exist; resolve the storage conflict offline"
        )
    if legacy:
        raise SessionConversionRequiredError(
            "legacy JSONL Sessions require offline conversion before starting vBot"
        )
    return "sqlite" if database_exists else "fresh"


QUARANTINE_DIRECTORY_NAME = "session-quarantine"


def quarantine_database(database_path: Path) -> Path | None:
    """Move a distrusted database plus sidecars into the quarantine directory.

    The bytes are preserved and never overwritten: an existing quarantine
    entry for the same timestamp grows a numeric suffix. Returns the
    quarantine path, or ``None`` when nothing could be moved (the caller
    then refuses the fresh-database fallback and still surfaces the error).
    """
    database_path = Path(database_path)
    data_dir = database_path.parent
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    destination_root = data_dir / QUARANTINE_DIRECTORY_NAME
    destination_root.mkdir(parents=True, exist_ok=True)
    batch = destination_root / f"{timestamp}-quarantine"
    moved_any = False
    for sidecar_path in (database_path, *database_sidecar_paths(database_path)):
        if not sidecar_path.exists():
            continue
        target = batch / sidecar_path.name
        with suppress(OSError):
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(sidecar_path, target)
            moved_any = True
    if not moved_any:
        return None
    _LOGGER.error(
        "Session database quarantined at %s (original bytes preserved); "
        "a fresh Session database starts empty; restore from "
        "session-backups if available",
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


def _legacy_paths(data_dir: Path) -> list[Path]:
    paths: list[Path] = []
    identity_root = data_dir / "agents"
    if identity_root.exists():
        for candidate in identity_root.glob("*/sessions/*.jsonl"):
            if candidate.name.endswith(".continuation.jsonl"):
                continue
            if not is_valid_agent_id(candidate.parent.parent.name) or not _valid_session_id(
                candidate.stem
            ):
                raise SessionStorageConflictError(f"invalid live legacy Session path: {candidate}")
            paths.append(candidate)
    project_root = data_dir / "projects"
    if project_root.exists():
        for candidate in project_root.glob("*/agents/*/sessions/*.jsonl"):
            if candidate.name.endswith(".continuation.jsonl"):
                continue
            if (
                not is_valid_project_id(candidate.parents[3].name)
                or not is_valid_agent_id(candidate.parent.parent.name)
                or not _valid_session_id(candidate.stem)
            ):
                raise SessionStorageConflictError(f"invalid live legacy Session path: {candidate}")
            paths.append(candidate)
    archive_root = data_dir / "archive" / "sessions"
    for candidate in archive_root.glob("agents/*/*.jsonl"):
        if not candidate.name.endswith(".continuation.jsonl"):
            paths.append(candidate)
    for candidate in archive_root.glob("projects/*/agents/*/*.jsonl"):
        if not candidate.name.endswith(".continuation.jsonl"):
            paths.append(candidate)
    return paths


def _valid_session_id(value: str) -> bool:
    return (
        bool(value)
        and value[0].isalnum()
        and all(
            character.isascii() and (character.isalnum() or character in "-_")
            for character in value
        )
        and len(value) <= 128
    )
