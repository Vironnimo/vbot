"""Disposable SQLite read model for Statistics-relevant Session facts."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from core.chat.messages import ChatMessage, MessageRole
from core.sessions import (
    FORK_SOURCE_META_KEY,
    ChatSession,
    SessionAddress,
    SessionReadBatch,
    SessionReadCursor,
    skill_context_note_name,
    skill_tool_activation_name,
)
from core.sessions.schema import required_journal_mode
from core.statistics.skills import SEEN_SKILLS_META_KEY
from core.tools import is_tool_result_envelope, tool_failure, tool_success

JsonObject = dict[str, Any]

_INDEX_DIRECTORY = "statistics"
_INDEX_FILENAME = "session-statistics.sqlite"
_GLOBAL_SCOPE = ""
_SCHEMA_VERSION = 4
_SQLITE_BUSY_TIMEOUT_MS = 1000


class StatisticsIndexError(RuntimeError):
    """The disposable Statistics index could not be read consistently."""


class StatisticsSessionSource(Protocol):
    """Session surface required by the derived index."""

    data_dir: Path

    def get(self, address: SessionAddress) -> ChatSession: ...

    def history_version(self, address: SessionAddress) -> tuple[str, int]: ...


@dataclass(frozen=True)
class StatisticsScope:
    """One identity or Project Session scope in report order."""

    project_id: str | None
    agent_id: str
    display_key: str
    summaries: tuple[JsonObject, ...]


@dataclass(frozen=True)
class IndexedStatisticsSession:
    """One compact Session projection hydrated from SQLite."""

    summary: JsonObject
    messages: tuple[ChatMessage, ...]


class StatisticsIndex:
    """Persist and incrementally reconcile compact Session projections."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.index_path = self.data_dir / _INDEX_DIRECTORY / _INDEX_FILENAME
        self._lock = threading.RLock()
        self._snapshot_cache: dict[tuple[str, str, str], IndexedStatisticsSession] | None = None
        self._snapshot_generation: int | None = None

    def snapshot(
        self,
        sessions: StatisticsSessionSource,
        scopes: tuple[StatisticsScope, ...],
    ) -> dict[tuple[str, str, str], IndexedStatisticsSession]:
        """Reconcile canonical sources and return one consistent compact snapshot."""
        with self._lock:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                self._initialize_schema(connection)
                with connection:
                    changes_before = connection.total_changes
                    current_keys = self._reconcile(connection, sessions, scopes)
                    self._prune_missing(connection, current_keys)
                    changed = connection.total_changes != changes_before
                    if changed:
                        connection.execute(
                            """
                            UPDATE statistics_index_state
                            SET generation = generation + 1
                            WHERE id = 1
                            """
                        )
                generation = int(
                    connection.execute(
                        "SELECT generation FROM statistics_index_state WHERE id = 1"
                    ).fetchone()[0]
                )
                if (
                    not changed
                    and self._snapshot_cache is not None
                    and self._snapshot_generation == generation
                ):
                    return self._snapshot_cache
                snapshot = self._load_snapshot(connection)
                self._snapshot_cache = snapshot
                self._snapshot_generation = generation
                return snapshot

    def discard(self) -> None:
        """Delete the disposable database and its SQLite sidecars."""
        with self._lock:
            self._snapshot_cache = None
            self._snapshot_generation = None
            for path in (
                self.index_path,
                Path(f"{self.index_path}-wal"),
                Path(f"{self.index_path}-shm"),
                Path(f"{self.index_path}-journal"),
            ):
                path.unlink(missing_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.index_path,
            timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            journal_mode = required_journal_mode(sqlite3.sqlite_version_info)
            connection.execute(f"PRAGMA journal_mode = {journal_mode.upper()}")
            connection.execute("PRAGMA synchronous = NORMAL")
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != _SCHEMA_VERSION:
            connection.executescript(
                """
                DROP TABLE IF EXISTS statistics_records;
                DROP TABLE IF EXISTS statistics_sessions;
                DROP TABLE IF EXISTS statistics_index_state;
                """
            )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS statistics_sessions (
                project_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                generation_id TEXT NOT NULL,
                history_revision INTEGER NOT NULL,
                cursor_generation_id TEXT NOT NULL,
                cursor_history_revision INTEGER NOT NULL,
                cursor_next_seq INTEGER NOT NULL,
                cursor_message_count INTEGER NOT NULL,
                cursor_last_message_id TEXT,
                fork_message_count INTEGER NOT NULL,
                summary_json TEXT NOT NULL,
                PRIMARY KEY (project_id, agent_id, session_id)
            );
            CREATE TABLE IF NOT EXISTS statistics_records (
                project_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (project_id, agent_id, session_id, ordinal),
                FOREIGN KEY (project_id, agent_id, session_id)
                    REFERENCES statistics_sessions(project_id, agent_id, session_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS statistics_index_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                generation INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO statistics_index_state(id, generation) VALUES (1, 0);
            """
        )
        if version != _SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            connection.commit()

    def _reconcile(
        self,
        connection: sqlite3.Connection,
        sessions: StatisticsSessionSource,
        scopes: tuple[StatisticsScope, ...],
    ) -> set[tuple[str, str, str]]:
        current_keys: set[tuple[str, str, str]] = set()
        for scope in scopes:
            project_key = _scope_key(scope.project_id)
            for raw_summary in scope.summaries:
                session_id = str(raw_summary["id"])
                key = (project_key, scope.agent_id, session_id)
                current_keys.add(key)
                summary = _statistics_summary(raw_summary)
                handle = sessions.get(
                    SessionAddress(
                        project_id=scope.project_id, agent_id=scope.agent_id, session_id=session_id
                    )
                )
                self._reconcile_session(connection, sessions, handle, key, summary)
        return current_keys

    def _reconcile_session(
        self,
        connection: sqlite3.Connection,
        sessions: StatisticsSessionSource,
        session: ChatSession,
        key: tuple[str, str, str],
        summary: JsonObject,
    ) -> None:
        row = connection.execute(
            """
            SELECT * FROM statistics_sessions
            WHERE project_id = ? AND agent_id = ? AND session_id = ?
            """,
            key,
        ).fetchone()
        address = SessionAddress(project_id=key[0] or None, agent_id=key[1], session_id=key[2])
        generation_id, history_revision = sessions.history_version(address)
        summary_json = _compact_json(summary)
        if row is None:
            self._replace_session(connection, session, key, summary_json)
            return

        existing_count = int(row["cursor_message_count"])
        effective_fork = _effective_fork_message_count(summary, existing_count)
        if (
            str(row["generation_id"]) == generation_id
            and int(row["history_revision"]) == history_revision
            and effective_fork == int(row["fork_message_count"])
        ):
            if summary_json != str(row["summary_json"]):
                connection.execute(
                    """
                    UPDATE statistics_sessions SET summary_json = ?
                    WHERE project_id = ? AND agent_id = ? AND session_id = ?
                    """,
                    (summary_json, *key),
                )
            return

        if str(row["generation_id"]) != generation_id or effective_fork != int(
            row["fork_message_count"]
        ):
            self._replace_session(connection, session, key, summary_json)
            return

        cursor = SessionReadCursor(
            generation_id=str(row["cursor_generation_id"]),
            history_revision=int(row["cursor_history_revision"]),
            next_seq=int(row["cursor_next_seq"]),
            message_count=int(row["cursor_message_count"]),
            last_message_id=cast(str | None, row["cursor_last_message_id"]),
        )
        batch = session.load_since(cursor)
        if batch is None:
            self._replace_session(connection, session, key, summary_json)
            return
        self._append_batch(
            connection,
            session,
            key,
            summary_json,
            batch,
            first_ordinal=cursor.next_seq,
            fork_message_count=effective_fork,
        )

    def _replace_session(
        self,
        connection: sqlite3.Connection,
        session: ChatSession,
        key: tuple[str, str, str],
        summary_json: str,
    ) -> None:
        batch = session.load_since()
        if batch is None:
            raise StatisticsIndexError(f"could not read canonical Session {key[2]}")
        summary = _json_object(summary_json)
        fork_message_count = _effective_fork_message_count(
            summary,
            batch.cursor.message_count,
        )
        connection.execute(
            """
            INSERT INTO statistics_sessions (
                project_id, agent_id, session_id,
                generation_id, history_revision, cursor_generation_id,
                cursor_history_revision, cursor_next_seq,
                cursor_message_count, cursor_last_message_id,
                fork_message_count, summary_json
            ) VALUES (?, ?, ?, '', 0, '', 0, 0, 0, NULL, ?, ?)
            ON CONFLICT(project_id, agent_id, session_id) DO UPDATE SET
                generation_id = '',
                history_revision = 0,
                cursor_generation_id = '',
                cursor_history_revision = 0,
                cursor_next_seq = 0,
                cursor_message_count = 0,
                cursor_last_message_id = NULL,
                fork_message_count = excluded.fork_message_count,
                summary_json = excluded.summary_json
            """,
            (*key, fork_message_count, summary_json),
        )
        connection.execute(
            """
            DELETE FROM statistics_records
            WHERE project_id = ? AND agent_id = ? AND session_id = ?
            """,
            key,
        )
        self._append_batch(
            connection,
            session,
            key,
            summary_json,
            batch,
            first_ordinal=0,
            fork_message_count=fork_message_count,
        )

    @staticmethod
    def _append_batch(
        connection: sqlite3.Connection,
        session: ChatSession,
        key: tuple[str, str, str],
        summary_json: str,
        batch: SessionReadBatch,
        *,
        first_ordinal: int,
        fork_message_count: int,
    ) -> None:
        rows = []
        for offset, message in enumerate(batch.messages):
            ordinal = first_ordinal + offset
            if ordinal < fork_message_count:
                continue
            rows.append((*key, ordinal, _compact_json(_project_message(message))))
        if rows:
            connection.executemany(
                """
                INSERT INTO statistics_records (
                    project_id, agent_id, session_id, ordinal, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )

        connection.execute(
            """
            UPDATE statistics_sessions SET
                generation_id = ?,
                history_revision = ?,
                cursor_generation_id = ?,
                cursor_history_revision = ?,
                cursor_next_seq = ?,
                cursor_message_count = ?,
                cursor_last_message_id = ?,
                fork_message_count = ?,
                summary_json = ?
            WHERE project_id = ? AND agent_id = ? AND session_id = ?
            """,
            (
                batch.cursor.generation_id,
                batch.cursor.history_revision,
                batch.cursor.generation_id,
                batch.cursor.history_revision,
                batch.cursor.next_seq,
                batch.cursor.message_count,
                batch.cursor.last_message_id,
                fork_message_count,
                summary_json,
                *key,
            ),
        )

    @staticmethod
    def _prune_missing(
        connection: sqlite3.Connection,
        current_keys: set[tuple[str, str, str]],
    ) -> None:
        stored_keys = {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT project_id, agent_id, session_id FROM statistics_sessions"
            )
        }
        for key in stored_keys - current_keys:
            connection.execute(
                """
                DELETE FROM statistics_sessions
                WHERE project_id = ? AND agent_id = ? AND session_id = ?
                """,
                key,
            )

    @staticmethod
    def _load_snapshot(
        connection: sqlite3.Connection,
    ) -> dict[tuple[str, str, str], IndexedStatisticsSession]:
        summaries: dict[tuple[str, str, str], JsonObject] = {}
        messages: dict[tuple[str, str, str], list[ChatMessage]] = {}
        for row in connection.execute(
            """
            SELECT project_id, agent_id, session_id, summary_json
            FROM statistics_sessions
            """
        ):
            key = (str(row["project_id"]), str(row["agent_id"]), str(row["session_id"]))
            summaries[key] = _json_object(str(row["summary_json"]))
            messages[key] = []
        for row in connection.execute(
            """
            SELECT project_id, agent_id, session_id, ordinal, payload_json
            FROM statistics_records
            ORDER BY project_id, agent_id, session_id, ordinal
            """
        ):
            key = (str(row["project_id"]), str(row["agent_id"]), str(row["session_id"]))
            payload = _json_object(str(row["payload_json"]))
            messages[key].append(_message_from_projection(payload, int(row["ordinal"])))
        return {
            key: IndexedStatisticsSession(summary=summary, messages=tuple(messages[key]))
            for key, summary in summaries.items()
        }


def _scope_key(project_id: str | None) -> str:
    return project_id if project_id is not None else _GLOBAL_SCOPE


def statistics_session_key(
    project_id: str | None,
    agent_id: str,
    session_id: str,
) -> tuple[str, str, str]:
    """Return the persisted composite key for one scoped Session."""
    return (_scope_key(project_id), agent_id, session_id)


def _statistics_summary(summary: JsonObject) -> JsonObject:
    projected: JsonObject = {
        "id": str(summary["id"]),
        "created_at": summary.get("created_at"),
        "last_active_at": summary.get("last_active_at"),
    }
    for key in ("title", FORK_SOURCE_META_KEY, SEEN_SKILLS_META_KEY):
        if key in summary:
            projected[key] = summary[key]
    return projected


def _effective_fork_message_count(summary: JsonObject, total_messages: int) -> int:
    fork_source = summary.get(FORK_SOURCE_META_KEY)
    if not isinstance(fork_source, dict):
        return 0
    value = fork_source.get("message_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > total_messages:
        return 0
    return value


def _project_message(message: ChatMessage) -> JsonObject:
    payload: JsonObject = {
        "timestamp": message.timestamp,
        "role": message.role,
    }
    if message.role == "assistant":
        if isinstance(message.content, str) and message.content.strip():
            payload["content"] = "visible"
        if message.model is not None:
            payload["model"] = message.model
        if message.usage is not None:
            payload["usage"] = _project_usage(message.usage, assistant=True)
    elif message.role == "tool":
        if message.name is not None:
            payload["name"] = message.name
        content = _project_tool_content(message)
        if content is not None:
            payload["content"] = content
        timing = _project_timing(message.timing)
        if timing is not None:
            payload["timing"] = timing
    elif message.role == "note":
        skill_name = skill_context_note_name(message)
        if skill_name is not None:
            payload["content"] = (
                '[skill-context] {"name":'
                + json.dumps(skill_name, ensure_ascii=False)
                + ',"content":"indexed"}'
            )
    elif message.role == "error":
        if message.error_kind is not None:
            payload["error_kind"] = message.error_kind
    elif message.role == "compaction_checkpoint":
        if message.compaction_strategy is not None:
            payload["compaction_strategy"] = message.compaction_strategy
        if message.usage is not None:
            payload["usage"] = _project_usage(message.usage, assistant=False)
    elif message.role == "run_summary":
        if message.run_id is not None:
            payload["run_id"] = message.run_id
        if message.status is not None:
            payload["status"] = message.status
        timing = _project_timing(message.timing)
        if timing is not None:
            payload["timing"] = timing
    return payload


def _project_usage(usage: JsonObject, *, assistant: bool) -> JsonObject:
    keys = (
        (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "estimated",
            "input_tokens_estimated",
            "output_tokens_estimated",
            "cache_read_tokens",
            "cache_write_tokens",
        )
        if assistant
        else ("context_tokens_before", "context_tokens_after")
    )
    return {key: usage[key] for key in keys if key in usage}


def _project_timing(timing: JsonObject | None) -> JsonObject | None:
    if timing is None:
        return None
    projected: JsonObject = {
        key: timing[key] for key in ("started_at", "completed_at", "duration_ms") if key in timing
    }
    return projected or None


def _project_tool_content(message: ChatMessage) -> str | None:
    activation_name = skill_tool_activation_name(message)
    if activation_name is not None:
        envelope = tool_success(
            {
                "status": "loaded",
                "name": activation_name,
                "content": "indexed",
            }
        )
        return _compact_json(envelope)
    if not isinstance(message.content, str):
        return None
    try:
        envelope = json.loads(message.content)
    except (TypeError, ValueError):
        return None
    if not isinstance(envelope, dict) or not is_tool_result_envelope(envelope):
        return None
    if envelope["ok"]:
        return _compact_json(tool_success({}))
    error = envelope["error"]
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        return None
    return _compact_json(tool_failure(error["code"], "indexed"))


def _message_from_projection(payload: JsonObject, ordinal: int) -> ChatMessage:
    usage = payload.get("usage")
    timing = payload.get("timing")
    return ChatMessage(
        id=f"statistics-{ordinal}",
        timestamp=str(payload["timestamp"]),
        role=cast(MessageRole, payload["role"]),
        content=cast(str | None, payload.get("content")),
        model=cast(str | None, payload.get("model")),
        usage=dict(usage) if isinstance(usage, dict) else None,
        timing=dict(timing) if isinstance(timing, dict) else None,
        name=cast(str | None, payload.get("name")),
        error_kind=cast(str | None, payload.get("error_kind")),
        compaction_strategy=cast(str | None, payload.get("compaction_strategy")),
        run_id=cast(str | None, payload.get("run_id")),
        status=cast(str | None, payload.get("status")),
    )


def _compact_json(value: JsonObject) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_object(value: str) -> JsonObject:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        raise StatisticsIndexError("invalid JSON in Statistics index") from error
    if not isinstance(parsed, dict):
        raise StatisticsIndexError("Statistics index JSON row must be an object")
    return parsed
