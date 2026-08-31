"""Transactional SQLite persistence hidden behind the Session domain."""
# ruff: noqa: E501

from __future__ import annotations

import builtins
import json
import logging
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import contextmanager, suppress
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.chat.errors import ChatSessionError
from core.sessions.errors import (
    FtsHealth,
    QuarantineResult,
    SessionStorageFormatError,
    SessionStoreCorruptError,
    SessionStoreSchemaMismatchError,
    SessionStoreUnavailableError,
)
from core.sessions.format import (
    MARKER_STATE_BOOTSTRAP,
    publish_ready_marker,
    read_session_store_marker,
    validate_session_store_paths,
)
from core.sessions.schema import (
    APPLICATION_ID,
    DATABASE_ID_META_KEY,
    FTS_COMPLETED_HIGH_WATER_KEY,
    FTS_DEGRADED_REASON_KEY,
    FTS_GENERATION_KEY,
    FTS_SQL,
    FTS_SQL_FALLBACK,
    FTS_STALE_KEY,
    FTS_STORAGE_VERSION,
    FTS_STORAGE_VERSION_KEY,
    FTS_TARGET_HIGH_WATER_KEY,
    SCHEMA_CONVERSION_FLOOR,
    SCHEMA_VERSION,
    reconcile_schema,
)
from core.sessions.sqlite_runtime import (
    ACTIVITY_WRITE_PATIENCE_S,
    TRANSCRIPT_WRITE_PATIENCE_S,
    WRITE_PATIENCE_S,
    SQLiteRuntime,
)

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions.sessions import SessionAddress, SessionReadCursor

_LOGGER = logging.getLogger("vbot.sessions")

JsonObject = dict[str, Any]


_FTS_BATCH_SIZE = 100
_FTS_REBUILD_THROTTLE_S = 0.01
_FTS_REBUILD_HOOK: Callable[[str, int], None] | None = None


def _fts_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _set_fts_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute("INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)", (key, value))


def _search_projection(message_json: str) -> str:
    """Build the Recall-owned text projection without indexing raw JSON."""
    message = _message_from_json(message_json)
    from core.recall.canonical import (
        SESSION_RECALL_CONVERSATION_ROLES,
        is_recall_artifact_message,
        message_search_text,
    )
    from core.sessions.sessions import is_skill_context_note

    if (
        message.role not in SESSION_RECALL_CONVERSATION_ROLES
        or is_recall_artifact_message(message)
        or is_skill_context_note(message)
    ):
        return ""
    return message_search_text(message)


def _fts_table_exists(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'"
        ).fetchone()
        is not None
    )


def _fts_content_exists(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'message_search'"
        ).fetchone()
        is not None
    )


def _drop_fts_triggers(connection: sqlite3.Connection) -> None:
    for trigger in ("messages_fts_insert", "messages_fts_delete", "messages_fts_update"):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _fts_coverage_ok(connection: sqlite3.Connection) -> tuple[bool, str | None]:
    if not _fts_table_exists(connection) or not _fts_content_exists(connection):
        return False, "FTS tables are missing"
    missing_projection = connection.execute(
        """
        SELECT 1
        FROM messages AS m
        LEFT JOIN message_search AS ms ON ms.message_key = m.message_key
        WHERE ms.message_key IS NULL
        LIMIT 1
        """
    ).fetchone()
    if missing_projection is not None:
        return False, "canonical Messages are missing FTS projections"
    missing_index = connection.execute(
        """
        SELECT 1
        FROM message_search AS ms
        LEFT JOIN messages_fts AS f ON f.rowid = ms.message_key
        WHERE ms.search_text <> '' AND f.rowid IS NULL
        LIMIT 1
        """
    ).fetchone()
    if missing_index is not None:
        return False, "FTS rows are missing searchable projections"
    unexpected_index = connection.execute(
        """
        SELECT 1
        FROM messages_fts AS f
        LEFT JOIN message_search AS ms ON ms.message_key = f.rowid
        WHERE ms.message_key IS NULL
        LIMIT 1
        """
    ).fetchone()
    if unexpected_index is not None:
        return False, "FTS contains rows outside the searchable projection"
    return True, None


def _fts_health_from_connection(connection: sqlite3.Connection) -> FtsHealth:
    if not _fts_table_exists(connection) or not _fts_content_exists(connection):
        return FtsHealth(state="unavailable", reason="FTS tables are missing")
    storage_version = _fts_meta(connection, FTS_STORAGE_VERSION_KEY)
    generation = _fts_meta(connection, FTS_GENERATION_KEY)
    target = _fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY)
    completed = _fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY)
    stale = _fts_meta(connection, FTS_STALE_KEY)
    reason = _fts_meta(connection, FTS_DEGRADED_REASON_KEY)
    if storage_version != str(FTS_STORAGE_VERSION):
        return FtsHealth(
            state="degraded",
            reason="FTS storage version is missing or unsupported",
            generation=generation,
        )
    if not generation:
        return FtsHealth(state="degraded", reason="FTS rebuild generation is missing")
    try:
        target_value = int(target) if target is not None else -1
        completed_value = int(completed) if completed is not None else -1
    except (TypeError, ValueError):
        return FtsHealth(
            state="degraded",
            reason="FTS high-water metadata is malformed",
            generation=generation,
        )
    if target_value < 0 or completed_value < 0 or completed_value > target_value:
        return FtsHealth(
            state="degraded",
            reason="FTS high-water metadata is invalid",
            generation=generation,
            target_high_water=target_value,
            completed_high_water=completed_value,
        )
    coverage_ok, coverage_reason = _fts_coverage_ok(connection)
    if stale is not None or completed_value != target_value or not coverage_ok:
        rebuilding = stale == "rebuilding" or completed_value != target_value
        return FtsHealth(
            state="rebuilding" if rebuilding else "degraded",
            reason=reason or stale or coverage_reason or "FTS coverage is incomplete",
            generation=generation,
            target_high_water=target_value,
            completed_high_water=completed_value,
        )
    return FtsHealth(
        state="healthy",
        generation=generation,
        target_high_water=target_value,
        completed_high_water=completed_value,
    )


def _fts_rebuild_boundary(stage: str, high_water: int) -> None:
    if _FTS_REBUILD_HOOK is not None:
        _FTS_REBUILD_HOOK(stage, high_water)


def _ensure_fts_schema(connection: sqlite3.Connection) -> None:
    """Create or repair the derived FTS projection without weakening canonical storage."""
    try:
        health = _fts_health_from_connection(connection)
        if health.available:
            return
        if _fts_table_exists(connection):
            _drop_fts_triggers(connection)
            connection.execute("DROP TABLE IF EXISTS messages_fts")
        try:
            connection.executescript(FTS_SQL)
        except sqlite3.Error:
            connection.execute("DROP TABLE IF EXISTS messages_fts")
            connection.executescript(FTS_SQL_FALLBACK)
        _set_fts_meta(connection, FTS_STORAGE_VERSION_KEY, str(FTS_STORAGE_VERSION))
        _set_fts_meta(connection, FTS_GENERATION_KEY, uuid.uuid4().hex)
        _set_fts_meta(connection, FTS_STALE_KEY, "rebuilding")
        _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, "FTS rebuild in progress")
        connection.commit()
        _backfill_message_search(connection)
        _finish_fts_rebuild(connection)
    except sqlite3.Error as exc:
        _set_fts_meta(connection, FTS_STALE_KEY, "1")
        _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, f"FTS unavailable: {exc}")
        connection.commit()
        _LOGGER.warning("Session FTS unavailable, using canonical scan: %s", exc)


def _backfill_message_search(connection: sqlite3.Connection) -> None:
    """Populate missing canonical projections in committed bounded batches."""
    generation = _fts_meta(connection, FTS_GENERATION_KEY) or uuid.uuid4().hex
    _set_fts_meta(connection, FTS_GENERATION_KEY, generation)
    previous = _fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY)
    try:
        completed = max(0, int(previous)) if previous is not None else 0
    except ValueError:
        completed = 0
    target = int(
        connection.execute("SELECT COALESCE(MAX(message_key), 0) FROM messages").fetchone()[0]
    )
    _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, str(target))
    _set_fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY, str(completed))
    connection.commit()
    while True:
        target = max(
            target,
            int(
                connection.execute("SELECT COALESCE(MAX(message_key), 0) FROM messages").fetchone()[
                    0
                ]
            ),
        )
        _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, str(target))
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute(
                """
                SELECT m.message_key, m.message_json
                FROM messages AS m
                LEFT JOIN message_search AS ms ON ms.message_key = m.message_key
                WHERE ms.message_key IS NULL AND m.message_key <= ?
                ORDER BY m.message_key
                LIMIT ?
                """,
                (target, _FTS_BATCH_SIZE),
            ).fetchall()
            if not rows:
                connection.execute("COMMIT")
                break
            batch_max = int(rows[-1][0])
            _fts_rebuild_boundary("before_batch_commit", batch_max)
            for row in rows:
                connection.execute(
                    "INSERT INTO message_search (message_key, search_text) VALUES (?, ?)",
                    (int(row[0]), _search_projection(str(row[1]))),
                )
            _set_fts_meta(
                connection,
                FTS_COMPLETED_HIGH_WATER_KEY,
                str(max(completed, batch_max)),
            )
            connection.execute("COMMIT")
            completed = max(completed, batch_max)
            _fts_rebuild_boundary("after_batch_commit", batch_max)
        except BaseException:
            with suppress(BaseException):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            raise
        time.sleep(_FTS_REBUILD_THROTTLE_S)


def _finish_fts_rebuild(connection: sqlite3.Connection) -> None:
    """Rebuild FTS rows and clear stale only after explicit coverage checks."""
    connection.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
    coverage_ok, coverage_reason = _fts_coverage_ok(connection)
    if not coverage_ok:
        _set_fts_meta(
            connection,
            FTS_DEGRADED_REASON_KEY,
            coverage_reason or "FTS coverage is incomplete",
        )
        connection.commit()
        return
    final_target = int(
        connection.execute("SELECT COALESCE(MAX(message_key), 0) FROM messages").fetchone()[0]
    )
    _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, str(final_target))
    _set_fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY, str(final_target))
    _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, "")
    connection.execute("DELETE FROM store_meta WHERE key = ?", (FTS_STALE_KEY,))
    _fts_rebuild_boundary("after_final_clear", final_target)
    connection.commit()


def _detach_fts(connection: sqlite3.Connection, reason: str = "FTS write failed") -> None:
    """Mark FTS degraded and remove only its virtual table/triggers."""
    with suppress(sqlite3.Error):
        _set_fts_meta(connection, FTS_STALE_KEY, "1")
        _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, reason)
        _drop_fts_triggers(connection)
        connection.execute("DROP TABLE IF EXISTS messages_fts")
    _LOGGER.warning("Session FTS detached after corruption; canonical writes continue")


def _insert_message_search(connection: sqlite3.Connection, message_key: int, payload: str) -> None:
    """Maintain the derived projection inside the canonical write transaction."""
    if not _fts_content_exists(connection) or _fts_meta(connection, FTS_STALE_KEY) is not None:
        return
    connection.execute(
        "INSERT INTO message_search (message_key, search_text) VALUES (?, ?)",
        (message_key, _search_projection(payload)),
    )


def _mark_fts_write(connection: sqlite3.Connection) -> None:
    if not _fts_table_exists(connection) or _fts_meta(connection, FTS_STALE_KEY) is not None:
        return
    target = int(
        connection.execute("SELECT COALESCE(MAX(message_key), 0) FROM messages").fetchone()[0]
    )
    _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, str(target))
    _set_fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY, str(target))


# Application-level patience is budgeted in seconds; SQLite's own busy handler is
# kept short so contention surfaces for jittered retry.
BUSY_TIMEOUT_MS = 1_000


class SessionStore:
    """One canonical SQLite database with explicit read/write snapshots."""

    def __init__(self, path: Path, *, _offline: bool = False) -> None:
        self.path = Path(path)
        self._runtime = SQLiteRuntime(self.path)
        self._offline = _offline
        try:
            self._writer = self._open_runtime(offline=_offline)
        except BaseException:
            self._runtime.close()
            raise

    def _open_runtime(self, *, offline: bool) -> sqlite3.Connection:
        if offline:
            database_id = uuid.uuid4().hex if not self.path.exists() else None
            writer = self._runtime.open_writer(
                create=not self.path.exists(), database_id=database_id
            )
            self._reconcile_open_database(writer, expected_database_id=None)
            return writer

        validate_session_store_paths(self.path.parent, self.path)
        marker = read_session_store_marker(self.path.parent)
        if marker is None:
            raise SessionStorageFormatError(
                f"the data directory does not authorize a current-format Session store: "
                f"{self.path.parent}; initialize the data directory or install a converted "
                "Session database first"
            )
        if int(marker["schema_version"]) != SCHEMA_VERSION:
            raise SessionStoreSchemaMismatchError(
                "Session store marker schema does not match the Runtime: "
                f"schema version {marker['schema_version']}"
            )
        database_id = str(marker["database_id"])
        if marker["state"] == MARKER_STATE_BOOTSTRAP:
            writer = self._runtime.open_writer(create=True, database_id=database_id)
            self._reconcile_open_database(writer, expected_database_id=database_id)
            publish_ready_marker(self.path.parent, database_id)
            return writer
        if not self.path.exists():
            from core.sessions.snapshots import auto_restore_if_needed

            if not auto_restore_if_needed(self.path.parent, self.path):
                raise SessionStoreUnavailableError(
                    f"the Session database is missing although the store is ready: {self.path}"
                )
        try:
            writer = self._runtime.open_writer(expected_database_id=database_id)
            self._reconcile_open_database(writer, expected_database_id=database_id)
            return writer
        except (SessionStoreCorruptError, sqlite3.DatabaseError, OSError):
            self._runtime.close()
            from core.sessions.snapshots import auto_restore_if_needed

            if auto_restore_if_needed(self.path.parent, self.path):
                self._runtime = SQLiteRuntime(self.path)
                writer = self._runtime.open_writer(expected_database_id=database_id)
                self._reconcile_open_database(writer, expected_database_id=database_id)
                return writer
            raise

    def _reconcile_open_database(
        self, connection: sqlite3.Connection, *, expected_database_id: str | None
    ) -> None:
        if expected_database_id is not None:
            self._verify_database_identity(connection, self.path, expected_database_id)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != 0 and version < SCHEMA_CONVERSION_FLOOR:
            raise SessionStoreSchemaMismatchError(
                f"Session database schema {version} requires the offline converter"
            )
        applied = reconcile_schema(connection)
        if applied:
            _LOGGER.info(
                "Reconciled Session database schema at %s: %s",
                self.path,
                "; ".join(applied),
            )
        _ensure_fts_schema(connection)
        self._verify_connection(connection, self.path)

    @staticmethod
    def _verify_connection(connection: sqlite3.Connection, path: Path) -> None:
        SessionStore._verify_integrity(connection, path)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise SessionStoreSchemaMismatchError(
                f"unsupported Session database version {version} at {path}"
            )
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        if application_id != APPLICATION_ID:
            raise SessionStoreCorruptError(f"not a vBot Session database: {path}")

    @staticmethod
    def _verify_integrity(connection: sqlite3.Connection, path: Path) -> None:
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if str(integrity) != "ok":
            raise SessionStoreCorruptError(
                f"Session database integrity check failed at {path}: {integrity}"
            )
        foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_error is not None:
            raise SessionStoreCorruptError(f"Session foreign-key check failed at {path}")

    @staticmethod
    def _verify_database_identity(
        connection: sqlite3.Connection, path: Path, database_id: str
    ) -> None:
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

    # The Session domain uses this compact facade; connection ownership and
    # synchronization live exclusively in SQLiteRuntime.
    @contextmanager
    def _transaction(self, *, write: bool, patience_s: float = WRITE_PATIENCE_S):
        if write:
            raise RuntimeError("SessionStore write transactions use _execute_write")
        with self._runtime.read_ctx() as connection:
            yield connection

    def _execute_write(
        self, func: Callable[[sqlite3.Connection], Any], patience_s: float = WRITE_PATIENCE_S
    ) -> Any:
        fts_retried = False
        while True:
            try:
                return self._runtime.execute_write(func, patience_s=patience_s)
            except sqlite3.Error as exc:
                message = str(exc).lower()
                if not fts_retried and any(
                    marker in message for marker in ("fts", "messages_fts", "message_search")
                ):
                    fts_retried = True
                    with suppress(Exception):
                        self._runtime.execute_write(_detach_fts, patience_s=patience_s)
                    continue
                raise SessionStoreUnavailableError(
                    f"Session database write failed: {self.path}"
                ) from exc

    def _try_wal_checkpoint(self) -> None:
        self._runtime.checkpoint()

    def _try_fts_merge(self) -> None:
        def merge(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('merge', 16)")

        with suppress(Exception):
            self._runtime.execute_write(merge, patience_s=ACTIVITY_WRITE_PATIENCE_S)

    @contextmanager
    def _read_transaction(self):
        with self._runtime.read_ctx() as connection:
            yield connection

    def close(self) -> None:
        self._runtime.close()

    def checkpoint(self) -> None:
        self._runtime.checkpoint()

    def backup(self, destination: Path) -> None:
        self._runtime.backup(destination)

    def snapshot_revisions(self) -> tuple[int, int]:
        """Return database-wide history/state revisions for snapshot coalescing."""
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(history_revision), 0), COALESCE(MAX(state_revision), 0) FROM sessions"
            ).fetchone()
        return int(row[0]), int(row[1])

    def status_projection(self) -> JsonObject:
        """Return operator-safe health, snapshot, and incident state."""
        from core.sessions.snapshots import read_recovery_incident, snapshot_summaries

        marker = read_session_store_marker(self.path.parent)
        if marker is None:
            raise SessionStorageFormatError("current-format Session marker is missing")
        fts = self.fts_health()
        incident = read_recovery_incident(self.path.parent)
        snapshots = snapshot_summaries(
            self.path.parent, expected_database_id=str(marker["database_id"])
        )
        active_incident = incident if incident and not incident.get("acknowledged", False) else None
        return {
            "state": "recovered_with_incident" if active_incident else "ready",
            "database_id": str(marker["database_id"]),
            "marker_state": marker["state"],
            "schema_version": int(marker["schema_version"]),
            "fts": {
                "state": fts.state,
                "reason": fts.reason,
                "generation": fts.generation,
                "target_high_water": fts.target_high_water,
                "completed_high_water": fts.completed_high_water,
            },
            "snapshots": snapshots,
            "incident": active_incident,
        }

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

    def import_generation(
        self,
        address: SessionAddress,
        *,
        generation_id: str,
        messages: Sequence[ChatMessage],
        metadata: JsonObject,
        activity: JsonObject,
        continuation: Sequence[JsonObject],
        archived: bool,
        created_at: str,
    ) -> None:
        """Import one complete generation in one offline-only transaction.

        The converter is the only caller that may supply a generation id. Keeping
        this operation on the canonical transaction owner means imported rows use
        exactly the same validation, projections, and foreign-key behavior as
        normal Session writes, without exposing a migration hook to Runtime.
        """
        if not self._offline:
            raise ChatSessionError("generation import is available only to the offline converter")
        if (
            not isinstance(generation_id, str)
            or len(generation_id) != 32
            or any(character not in "0123456789abcdef" for character in generation_id)
        ):
            raise ChatSessionError("offline generation id is invalid")
        metadata_payload = _json_object(metadata, "session metadata")
        activity_payload = _json_object(activity, "session activity")
        encoded_messages = [_message_row(message) for message in messages]
        encoded_continuation = [
            _json_object(record, "continuation record") for record in continuation
        ]

        def _fn(connection: sqlite3.Connection) -> None:
            try:
                cursor = connection.execute(
                    "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, status, "
                    "created_at, last_message_at, archived_at, message_count, last_message_id, "
                    "history_revision, state_revision, metadata_json, activity_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        *self._scope(address),
                        "archived" if archived else "live",
                        created_at,
                        encoded_messages[-1][2] if encoded_messages else None,
                        created_at if archived else None,
                        len(encoded_messages),
                        encoded_messages[-1][0] if encoded_messages else None,
                        1 if encoded_messages else 0,
                        1
                        if encoded_messages or metadata_payload != "{}" or activity_payload != "{}"
                        else 0,
                        metadata_payload,
                        activity_payload,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ChatSessionError(
                    f"offline Session generation conflicts: {address.session_id}"
                ) from exc
            session_key = cursor.lastrowid
            if session_key is None:
                raise SessionStoreCorruptError("SQLite did not return an offline Session key")
            for sequence, (message_id, role, timestamp, payload) in enumerate(encoded_messages):
                message_cursor = connection.execute(
                    "INSERT INTO messages (session_key, seq, message_id, role, timestamp, message_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (session_key, sequence, message_id, role, timestamp, payload),
                )
                message_key = message_cursor.lastrowid
                if message_key is None:
                    raise SessionStoreCorruptError("SQLite did not return an offline message key")
                _insert_message_search(connection, int(message_key), payload)
            for sequence, payload in enumerate(encoded_continuation):
                connection.execute(
                    "INSERT INTO continuation_records (session_key, seq, record_json) VALUES (?, ?, ?)",
                    (session_key, sequence, payload),
                )
            _mark_fts_write(connection)

        self._execute_write(_fn, patience_s=TRANSCRIPT_WRITE_PATIENCE_S)

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
            for index, (message_id, role, timestamp, payload) in enumerate(encoded):
                cursor = connection.execute(
                    "INSERT INTO messages (session_key, seq, message_id, role, timestamp, message_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (session_key, next_seq + index, message_id, role, timestamp, payload),
                )
                message_key = cursor.lastrowid
                if message_key is None:
                    raise SessionStoreCorruptError("SQLite did not return a canonical message key")
                _insert_message_search(connection, int(message_key), payload)
            last_id, _role, last_timestamp, _payload = encoded[-1]
            connection.execute(
                "UPDATE sessions SET message_count = message_count + ?, last_message_at = ?, last_message_id = ?, history_revision = history_revision + 1, state_revision = state_revision + 1 WHERE session_key = ?",
                (len(encoded), last_timestamp, last_id, session_key),
            )
            _mark_fts_write(connection)

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

    def fts_health(self) -> FtsHealth:
        """Return the integrated FTS state after checking metadata and coverage."""
        try:
            with self._transaction(write=False) as connection:
                return _fts_health_from_connection(connection)
        except Exception as exc:
            return FtsHealth(state="unavailable", reason=f"FTS health check failed: {exc}")

    def is_fts_available(self) -> bool:
        """Return true only when FTS metadata and all projection joins are verified."""
        return self.fts_health().available

    def fts_search(
        self,
        query: str,
        *,
        project_id: str | None,
        agent_id: str | None,
        session_id: str | None = None,
        match_mode: str = "all_terms",
    ) -> builtins.list[tuple[SessionAddress, str, str, str, float]]:
        """Search canonical Messages through FTS or a truthful projection fallback."""
        if not query or not query.strip():
            return []
        compact = re.sub(r"\s+", " ", query).strip().casefold()
        if not compact:
            return []

        def matches(text: str) -> bool:
            haystack = re.sub(r"\s+", " ", text).strip().casefold()
            if match_mode == "phrase":
                return compact in haystack
            terms = [term for term in compact.split(" ") if term]
            if match_mode == "any_term":
                return any(term in haystack for term in terms)
            return all(term in haystack for term in terms)

        def canonical_rows(
            connection: sqlite3.Connection,
        ) -> builtins.list[tuple[SessionAddress, str, str, str, float]]:
            sql = """
                SELECT s.project_id, s.agent_id, s.session_id, m.message_id,
                       m.timestamp, m.message_json, m.message_key
                FROM messages AS m
                JOIN sessions AS s ON s.session_key = m.session_key
                WHERE s.status = 'live'
            """
            params: list[Any] = []
            if project_id is not None:
                sql += " AND s.project_id = ?"
                params.append(project_id)
            else:
                sql += " AND s.project_id = ''"
            if agent_id is not None:
                sql += " AND s.agent_id = ?"
                params.append(agent_id)
            if session_id is not None:
                sql += " AND s.session_id = ?"
                params.append(session_id)
            sql += " ORDER BY m.timestamp DESC, m.message_key"
            result: builtins.list[tuple[SessionAddress, str, str, str, float]] = []
            for row in connection.execute(sql, params).fetchall():
                payload = str(row["message_json"])
                if not matches(_search_projection(payload)):
                    continue
                from core.sessions.sessions import SessionAddress as SessionAddr

                result.append(
                    (
                        SessionAddr(
                            project_id=row["project_id"] or None,
                            agent_id=row["agent_id"],
                            session_id=row["session_id"],
                        ),
                        str(row["message_id"]),
                        str(row["timestamp"]),
                        payload,
                        0.0,
                    )
                )
            return result

        terms = [term for term in compact.split(" ") if term]
        if match_mode == "phrase":
            escaped = compact.replace('"', '""')
            expression = f'"{escaped}"'
            fts_supported = len(compact) >= 3
        else:
            expression = (" OR " if match_mode == "any_term" else " AND ").join(
                f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms
            )
            fts_supported = bool(terms) and all(len(term) >= 3 for term in terms)
        if not fts_supported:
            with self._transaction(write=False) as connection:
                return canonical_rows(connection)

        try:
            with self._transaction(write=False) as connection:
                if not _fts_health_from_connection(connection).available:
                    return canonical_rows(connection)
                sql = """
                    SELECT s.project_id, s.agent_id, s.session_id, m.message_id,
                           m.timestamp, m.message_json, bm25(messages_fts) AS rank
                    FROM messages_fts
                    JOIN messages AS m ON m.message_key = messages_fts.rowid
                    JOIN sessions AS s ON s.session_key = m.session_key
                    WHERE messages_fts MATCH ? AND s.status = 'live'
                """
                params: list[Any] = [expression]
                if project_id is not None:
                    sql += " AND s.project_id = ?"
                    params.append(project_id)
                else:
                    sql += " AND s.project_id = ''"
                if agent_id is not None:
                    sql += " AND s.agent_id = ?"
                    params.append(agent_id)
                if session_id is not None:
                    sql += " AND s.session_id = ?"
                    params.append(session_id)
                sql += " ORDER BY rank, m.timestamp DESC, m.message_key"
                rows = connection.execute(sql, params).fetchall()
                result: builtins.list[tuple[SessionAddress, str, str, str, float]] = []
                for row in rows:
                    from core.sessions.sessions import SessionAddress as SessionAddr

                    result.append(
                        (
                            SessionAddr(
                                project_id=row["project_id"] or None,
                                agent_id=row["agent_id"],
                                session_id=row["session_id"],
                            ),
                            str(row["message_id"]),
                            str(row["timestamp"]),
                            str(row["message_json"]),
                            float(row["rank"]) if row["rank"] is not None else 0.0,
                        )
                    )
                return result
        except sqlite3.Error as exc:
            if "fts" in str(exc).lower() or "messages_fts" in str(exc).lower():
                error_message = str(exc)
                with suppress(Exception):
                    self._execute_write(lambda connection: _detach_fts(connection, error_message))
            with self._transaction(write=False) as connection:
                return canonical_rows(connection)

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
            source_rows = connection.execute(
                "SELECT seq, message_id, role, timestamp, message_json FROM messages WHERE session_key = ? ORDER BY seq",
                (state["session_key"],),
            ).fetchall()
            for row in source_rows:
                cursor = connection.execute(
                    "INSERT INTO messages (session_key, seq, message_id, role, timestamp, message_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        target_row.lastrowid,
                        row["seq"],
                        row["message_id"],
                        row["role"],
                        row["timestamp"],
                        row["message_json"],
                    ),
                )
                message_key = cursor.lastrowid
                if message_key is None:
                    raise SessionStoreCorruptError("SQLite did not return a canonical message key")
                _insert_message_search(connection, int(message_key), str(row["message_json"]))
            _mark_fts_write(connection)

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


def _message_from_json(payload: str) -> ChatMessage:
    from core.chat.messages import ChatMessage

    try:
        return ChatMessage.from_dict(json.loads(payload))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SessionStoreCorruptError("invalid canonical Session message") from exc


QUARANTINE_DIRECTORY_NAME = "session-quarantine"


def quarantine_database(database_path: Path) -> QuarantineResult:
    """Move a distrusted database bundle through the snapshot owner."""

    from core.sessions.snapshots import quarantine_database as quarantine

    return quarantine(Path(database_path))


def database_sidecar_paths(database_path: Path) -> tuple[Path, ...]:
    """Return the SQLite sidecar files for *database_path*."""

    return (
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
        Path(f"{database_path}-journal"),
    )
