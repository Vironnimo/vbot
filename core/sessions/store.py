"""Transactional SQLite persistence hidden behind the Session domain."""
# ruff: noqa: E501

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from collections.abc import Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.chat.errors import ChatSessionError
from core.sessions.errors import (
    SessionConversionIncompleteError,
    SessionConversionRequiredError,
    SessionStorageConflictError,
    SessionStoreCorruptError,
)
from core.sessions.schema import MINIMUM_SQLITE_VERSION, SCHEMA_SQL, SCHEMA_VERSION
from core.settings import is_valid_agent_id, is_valid_project_id

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions.sessions import SessionAddress, SessionReadCursor

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
            raise SessionStoreCorruptError(f"Session database is empty: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() not in {"wal", "delete"}:
                raise SessionStoreCorruptError(f"cannot set Session journal mode: {mode}")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA wal_autocheckpoint = 1000")
            connection.execute("PRAGMA journal_size_limit = 67108864")
            if not existed:
                connection.executescript(SCHEMA_SQL)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            cls._verify_connection(connection, path)
            return connection
        except (sqlite3.DatabaseError, OSError) as exc:
            with suppress(UnboundLocalError):
                connection.close()
            raise SessionStoreCorruptError(
                f"Session database cannot be opened safely: {path}"
            ) from exc

    @classmethod
    def _open_reader(cls, path: Path) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            cls._verify_connection(connection, path)
            return connection
        except (sqlite3.DatabaseError, OSError) as exc:
            with suppress(UnboundLocalError):
                connection.close()
            raise SessionStoreCorruptError(
                f"Session database cannot be opened safely: {path}"
            ) from exc

    @staticmethod
    def _verify_connection(connection: sqlite3.Connection, path: Path) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise SessionStoreCorruptError(
                f"unsupported Session database version {version} at {path}"
            )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SessionStoreCorruptError(f"Session database integrity check failed at {path}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_keys is not None:
            raise SessionStoreCorruptError(f"Session database foreign-key check failed at {path}")

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
            except Exception:
                if self._writer.in_transaction:
                    self._writer.execute("ROLLBACK")
                raise

    @contextmanager
    def _read_transaction(self):
        with self._lifetime_lock:
            if self._closed:
                raise ChatSessionError("Session store is closed")
        connection = self._readers.get()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            with self._lifetime_lock:
                closed = self._closed
            if closed:
                connection.close()
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

    def create(self, address: SessionAddress, created_at: str | None = None) -> None:
        timestamp = created_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self._transaction(write=True) as connection:
            try:
                connection.execute(
                    "INSERT INTO sessions (project_id, agent_id, session_id, created_at) VALUES (?, ?, ?, ?)",
                    (*self._scope(address), timestamp),
                )
            except sqlite3.IntegrityError as exc:
                raise ChatSessionError(f"session already exists: {address.session_id}") from exc

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
                + clause,
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
            self._require_live(connection, address)
            connection.execute(
                "UPDATE sessions SET metadata_json = ?, state_revision = state_revision + 1 WHERE project_id = ? AND agent_id = ? AND session_id = ?",
                (payload, *self._scope(address)),
            )

    def activity(self, address: SessionAddress) -> JsonObject:
        payload = self.state(address)["activity_json"]
        if payload is None:
            return {}
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
            self._require_live(connection, address)
            connection.execute(
                "UPDATE sessions SET activity_json = ?, state_revision = state_revision + 1 WHERE project_id = ? AND agent_id = ? AND session_id = ?",
                (payload, *self._scope(address)),
            )

    def append_messages(self, address: SessionAddress, messages: Sequence[ChatMessage]) -> None:
        if not messages:
            return
        encoded = [_message_row(message) for message in messages]
        with self._transaction(write=True) as connection:
            state = self._require_live(connection, address)
            next_seq = int(state["message_count"])
            connection.executemany(
                "INSERT INTO messages (project_id, agent_id, session_id, seq, message_id, role, timestamp, message_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (*self._scope(address), next_seq + index, *value)
                    for index, value in enumerate(encoded)
                ],
            )
            last_id, _role, last_timestamp, _payload = encoded[-1]
            connection.execute(
                "UPDATE sessions SET message_count = message_count + ?, last_message_at = ?, last_message_id = ?, history_revision = history_revision + 1, state_revision = state_revision + 1 WHERE project_id = ? AND agent_id = ? AND session_id = ?",
                (len(encoded), last_timestamp, last_id, *self._scope(address)),
            )

    def messages(self, address: SessionAddress) -> list[ChatMessage]:

        with self._transaction(write=False) as connection:
            self._require_live(connection, address)
            rows = connection.execute(
                "SELECT message_json FROM messages WHERE project_id = ? AND agent_id = ? AND session_id = ? ORDER BY seq",
                self._scope(address),
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
            last_id = state["last_message_id"]
            if cursor is not None and (
                cursor.history_revision != revision
                or cursor.next_seq != count
                or cursor.message_count != count
                or cursor.last_message_id != last_id
            ):
                return None
            start = 0 if cursor is None else cursor.next_seq
            rows = connection.execute(
                "SELECT message_json FROM messages WHERE project_id = ? AND agent_id = ? AND session_id = ? AND seq >= ? ORDER BY seq",
                (*self._scope(address), start),
            ).fetchall()
        return (
            [_message_from_json(row["message_json"]) for row in rows],
            SessionReadCursor(revision, count, count, last_id),
        )

    def continuation(self, address: SessionAddress) -> list[JsonObject]:
        with self._transaction(write=False) as connection:
            self._require_live(connection, address)
            rows = connection.execute(
                "SELECT record_json FROM continuation_records WHERE project_id = ? AND agent_id = ? AND session_id = ? ORDER BY seq",
                self._scope(address),
            ).fetchall()
        return [_json_from_payload(row["record_json"], "continuation record") for row in rows]

    def append_continuation(self, address: SessionAddress, records: Sequence[JsonObject]) -> None:
        if not records:
            return
        payloads = [_json_object(record, "continuation record") for record in records]
        with self._transaction(write=True) as connection:
            self._require_live(connection, address)
            sequence = connection.execute(
                "SELECT COALESCE(MAX(seq) + 1, 0) AS value FROM continuation_records WHERE project_id = ? AND agent_id = ? AND session_id = ?",
                self._scope(address),
            ).fetchone()["value"]
            connection.executemany(
                "INSERT INTO continuation_records (project_id, agent_id, session_id, seq, record_json) VALUES (?, ?, ?, ?, ?)",
                [
                    (*self._scope(address), int(sequence) + index, payload)
                    for index, payload in enumerate(payloads)
                ],
            )
            self._touch_state(connection, address)

    def clear_continuation(self, address: SessionAddress) -> None:
        with self._transaction(write=True) as connection:
            self._require_live(connection, address)
            connection.execute(
                "DELETE FROM continuation_records WHERE project_id = ? AND agent_id = ? AND session_id = ?",
                self._scope(address),
            )
            self._touch_state(connection, address)

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

    def list_history_revisions(
        self, project_id: str | None, agent_id: str
    ) -> list[tuple[SessionAddress, int]]:
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT project_id, agent_id, session_id, history_revision FROM sessions WHERE status = 'live' AND project_id = ? AND agent_id = ? ORDER BY session_id",
                (project_id or "", agent_id),
            ).fetchall()
        return [(self._address(row), int(row["history_revision"])) for row in rows]

    def archive(self, address: SessionAddress) -> None:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self._transaction(write=True) as connection:
            self._require_live(connection, address)
            connection.execute(
                "UPDATE sessions SET status = 'archived', archived_at = ?, state_revision = state_revision + 1 WHERE project_id = ? AND agent_id = ? AND session_id = ?",
                (timestamp, *self._scope(address)),
            )

    def move(self, source: SessionAddress, target: SessionAddress, metadata: JsonObject) -> None:
        """Relocate one complete Session row and its dependent rows atomically."""
        payload = _json_object(metadata, "session metadata")
        with self._transaction(write=True) as connection:
            self._require_live(connection, source)
            collision = connection.execute(
                "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ?",
                self._scope(target),
            ).fetchone()
            if collision is not None:
                raise ChatSessionError(f"destination session already exists: {target.session_id}")
            connection.execute(
                "UPDATE sessions SET project_id = ?, agent_id = ?, session_id = ?, metadata_json = ?, state_revision = state_revision + 1 WHERE project_id = ? AND agent_id = ? AND session_id = ?",
                (*self._scope(target), payload, *self._scope(source)),
            )

    def fork(self, source: SessionAddress, target: SessionAddress, metadata: JsonObject) -> None:
        """Copy canonical history to a new live Session without activity/journal state."""
        payload = _json_object(metadata, "session metadata")
        with self._transaction(write=True) as connection:
            state = self._require_live(connection, source)
            try:
                connection.execute(
                    "INSERT INTO sessions (project_id, agent_id, session_id, created_at, last_message_at, message_count, last_message_id, history_revision, state_revision, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                    (
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
                "INSERT INTO messages (project_id, agent_id, session_id, seq, message_id, role, timestamp, message_json) SELECT ?, ?, ?, seq, message_id, role, timestamp, message_json FROM messages WHERE project_id = ? AND agent_id = ? AND session_id = ? ORDER BY seq",
                (*self._scope(target), *self._scope(source)),
            )

    def restore(self, address: SessionAddress) -> None:
        with self._transaction(write=True) as connection:
            row = connection.execute(
                "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'archived'",
                self._scope(address),
            ).fetchone()
            if row is None:
                raise ChatSessionError(f"archived session does not exist: {address.session_id}")
            connection.execute(
                "UPDATE sessions SET status = 'live', archived_at = NULL, state_revision = state_revision + 1 WHERE project_id = ? AND agent_id = ? AND session_id = ?",
                self._scope(address),
            )

    def delete(self, address: SessionAddress) -> None:
        with self._transaction(write=True) as connection:
            connection.execute(
                "DELETE FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ?",
                self._scope(address),
            )

    def retarget_identity_agent(self, old_agent_id: str, new_agent_id: str) -> None:
        """Rename every global Session address for one Identity Agent atomically."""
        with self._transaction(write=True) as connection:
            collision = connection.execute(
                "SELECT 1 FROM sessions AS source WHERE source.project_id = '' AND source.agent_id = ? "
                "AND EXISTS (SELECT 1 FROM sessions AS target WHERE target.project_id = '' "
                "AND target.agent_id = ? AND target.session_id = source.session_id)",
                (old_agent_id, new_agent_id),
            ).fetchone()
            if collision is not None:
                raise ChatSessionError("destination Agent already has a Session with the same id")
            connection.execute(
                "UPDATE sessions SET agent_id = ?, state_revision = state_revision + 1 "
                "WHERE project_id = '' AND agent_id = ?",
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

    def _require_live(self, connection: sqlite3.Connection, address: SessionAddress) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'live'",
            self._scope(address),
        ).fetchone()
        if row is None:
            raise ChatSessionError(f"session does not exist: {address.session_id}")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _touch_state(connection: sqlite3.Connection, address: SessionAddress) -> None:
        connection.execute(
            "UPDATE sessions SET state_revision = state_revision + 1 WHERE project_id = ? AND agent_id = ? AND session_id = ?",
            SessionStore._scope(address),
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
    legacy = _live_legacy_paths(data_dir)
    database_exists = path.exists()
    if database_exists and legacy:
        raise SessionStorageConflictError(
            "both sessions.db and live JSONL Sessions exist; resolve the storage conflict offline"
        )
    if legacy:
        raise SessionConversionRequiredError(
            "live JSONL Sessions require offline conversion before starting vBot"
        )
    return "sqlite" if database_exists else "fresh"


def _live_legacy_paths(data_dir: Path) -> list[Path]:
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
