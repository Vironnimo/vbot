"""SQLite FTS5 recall backend for Session search."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.recall.jsonl import (
    JsonlSessionRecallBackend,
    compact_text,
    message_index_by_id,
    message_match_payload,
    message_matches_request,
    message_search_text,
    query_terms,
    render_message_matches,
    request_payload,
    text_matches_query,
)
from core.recall.recall import JsonObject, RecallBackendContext, RecallRequest
from core.sessions import is_skill_context_note

_INDEX_DIR_NAME = "recall"
_INDEX_FILE_NAME = "session_index.sqlite"
_SQLITE_BUSY_TIMEOUT_MS = 1000
# Bump when the on-disk index schema changes; mismatched indexes are dropped and rebuilt.
# v2 → rows are project-scoped (``project_id`` column in the index keys) so the
#      same session UUID under a project vs. the global scope never collides.
_SCHEMA_VERSION = 2
# FTS5 trigram needs at least three characters; shorter queries fall back to the JSONL scan.
_TRIGRAM_MIN_CHARS = 3
# Sentinel stored for the identity/global scope (``project_id is None``). An
# empty string keeps the PRIMARY KEY/UNIQUE constraints reliable — SQLite treats
# NULLs as distinct, which would defeat the per-scope uniqueness the column adds.
_GLOBAL_SCOPE = ""


def _scope(project_id: str | None) -> str:
    """Map a recall project scope to the index's stored scope value.

    ``None`` (identity/global recall) maps to the ``_GLOBAL_SCOPE`` sentinel so
    the on-disk rows for the global scope never share a key with a project's
    same-UUID session.
    """

    return project_id if project_id is not None else _GLOBAL_SCOPE


class SqliteFtsRecallBackend(JsonlSessionRecallBackend):
    """Recall backend backed by a disposable SQLite FTS index."""

    def __init__(self, context: RecallBackendContext) -> None:
        super().__init__(context.sessions)
        self.data_dir = context.data_dir
        self.index_path = self.data_dir / _INDEX_DIR_NAME / _INDEX_FILE_NAME
        self.logger = context.logger
        self._fallback = JsonlSessionRecallBackend(context.sessions)

    def search(self, request: RecallRequest) -> JsonObject:
        summaries = self.candidate_session_summaries(request)
        if request.query is None:
            return self.session_summary_result(request, summaries)
        if not summaries:
            return self._message_result(request, [], searched_sessions=0, total_candidates=0)

        try:
            return self._search_with_sqlite(request, summaries)
        except (OSError, sqlite3.DatabaseError) as error:
            self._warning("SQLite recall index failed; rebuilding once: %s", error)
            self._delete_index_file()

        try:
            return self._search_with_sqlite(request, summaries)
        except (OSError, sqlite3.DatabaseError) as error:
            self._warning(
                "SQLite recall index rebuild failed; falling back to JSONL scan: %s", error
            )
            return self._fallback.search(request)

    def _search_with_sqlite(
        self,
        request: RecallRequest,
        summaries: list[JsonObject],
    ) -> JsonObject:
        expression = _fts_expression(request)
        if expression is None:
            return self._fallback.search(request)

        with closing(self._connect()) as connection:
            self._initialize_schema(connection)
            self._cleanup_missing_sessions(connection, request, summaries)
            self._ensure_indexed(connection, request, summaries)
            rows = self._query_matches(connection, request, summaries, expression)
        matches = self._hydrate_matches(request, summaries, rows)
        truncated = len(matches) > request.limit
        return self._message_result(
            request,
            matches[: request.limit],
            searched_sessions=len(summaries),
            total_candidates=len(summaries),
            truncated=truncated,
        )

    def _connect(self) -> sqlite3.Connection:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.index_path)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError as error:
            self._warning("Could not enable WAL for SQLite recall index: %s", error)
        return connection

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != _SCHEMA_VERSION:
            connection.executescript(
                """
                DROP TABLE IF EXISTS messages_fts;
                DROP TABLE IF EXISTS messages;
                DROP TABLE IF EXISTS indexed_sessions;
                """
            )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS indexed_sessions (
              agent_id TEXT NOT NULL,
              project_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              session_mtime_ns INTEGER NOT NULL,
              session_size_bytes INTEGER NOT NULL,
              indexed_at TEXT NOT NULL,
              PRIMARY KEY (agent_id, project_id, session_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
              row_id INTEGER PRIMARY KEY,
              agent_id TEXT NOT NULL,
              project_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              message_id TEXT NOT NULL,
              message_index INTEGER NOT NULL,
              timestamp TEXT NOT NULL,
              role TEXT NOT NULL,
              search_text TEXT NOT NULL,
              UNIQUE (agent_id, project_id, session_id, message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session
              ON messages(agent_id, project_id, session_id, message_index);

            CREATE INDEX IF NOT EXISTS idx_messages_time
              ON messages(agent_id, project_id, timestamp);

            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(
              search_text,
              content='messages',
              content_rowid='row_id',
              tokenize='trigram'
            );
            """
        )
        if version != _SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def _cleanup_missing_sessions(
        self,
        connection: sqlite3.Connection,
        request: RecallRequest,
        summaries: list[JsonObject],
    ) -> None:
        agent_id = request.agent_id
        scope = _scope(request.project_id)
        active_session_ids = {str(summary["id"]) for summary in summaries}
        indexed_session_ids = {
            str(row["session_id"])
            for row in connection.execute(
                "SELECT session_id FROM indexed_sessions WHERE agent_id = ? AND project_id = ?",
                (agent_id, scope),
            )
        }
        for session_id in sorted(indexed_session_ids - active_session_ids):
            self._delete_session_rows(connection, agent_id, scope, session_id)
        connection.commit()

    def _ensure_indexed(
        self,
        connection: sqlite3.Connection,
        request: RecallRequest,
        summaries: list[JsonObject],
    ) -> None:
        agent_id = request.agent_id
        scope = _scope(request.project_id)
        for summary in summaries:
            session_id = str(summary["id"])
            session = self.sessions.get(agent_id, session_id, request.project_id)
            stat = session.path.stat()
            indexed = connection.execute(
                """
                SELECT session_mtime_ns, session_size_bytes
                FROM indexed_sessions
                WHERE agent_id = ? AND project_id = ? AND session_id = ?
                """,
                (agent_id, scope, session_id),
            ).fetchone()
            if (
                indexed is not None
                and int(indexed["session_mtime_ns"]) == stat.st_mtime_ns
                and int(indexed["session_size_bytes"]) == stat.st_size
            ):
                continue
            self._reindex_session(
                connection,
                agent_id,
                scope,
                session_id,
                session.load(),
                mtime_ns=stat.st_mtime_ns,
                size_bytes=stat.st_size,
            )

    def _reindex_session(
        self,
        connection: sqlite3.Connection,
        agent_id: str,
        scope: str,
        session_id: str,
        messages: list[Any],
        *,
        mtime_ns: int,
        size_bytes: int,
    ) -> None:
        with connection:
            self._delete_session_rows(connection, agent_id, scope, session_id)
            for message_index, message in enumerate(messages):
                if is_skill_context_note(message):
                    continue
                search_text = compact_text(message_search_text(message))
                cursor = connection.execute(
                    """
                    INSERT INTO messages (
                      agent_id,
                      project_id,
                      session_id,
                      message_id,
                      message_index,
                      timestamp,
                      role,
                      search_text
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent_id,
                        scope,
                        session_id,
                        message.id,
                        message_index,
                        message.timestamp,
                        message.role,
                        search_text,
                    ),
                )
                row_id = cursor.lastrowid
                if row_id is None:
                    raise sqlite3.DatabaseError("failed to insert recall message row")
                connection.execute(
                    "INSERT INTO messages_fts(rowid, search_text) VALUES (?, ?)",
                    (row_id, search_text),
                )
            connection.execute(
                """
                INSERT INTO indexed_sessions (
                  agent_id,
                  project_id,
                  session_id,
                  session_mtime_ns,
                  session_size_bytes,
                  indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    scope,
                    session_id,
                    mtime_ns,
                    size_bytes,
                    datetime.now(UTC).isoformat(),
                ),
            )

    @staticmethod
    def _delete_session_rows(
        connection: sqlite3.Connection,
        agent_id: str,
        scope: str,
        session_id: str,
    ) -> None:
        row_ids = [
            int(row["row_id"])
            for row in connection.execute(
                "SELECT row_id FROM messages "
                "WHERE agent_id = ? AND project_id = ? AND session_id = ?",
                (agent_id, scope, session_id),
            )
        ]
        for row_id in row_ids:
            connection.execute("DELETE FROM messages_fts WHERE rowid = ?", (row_id,))
        connection.execute(
            "DELETE FROM messages WHERE agent_id = ? AND project_id = ? AND session_id = ?",
            (agent_id, scope, session_id),
        )
        connection.execute(
            "DELETE FROM indexed_sessions WHERE agent_id = ? AND project_id = ? AND session_id = ?",
            (agent_id, scope, session_id),
        )

    def _query_matches(
        self,
        connection: sqlite3.Connection,
        request: RecallRequest,
        summaries: list[JsonObject],
        expression: str,
    ) -> list[sqlite3.Row]:
        session_ids = [str(summary["id"]) for summary in summaries]
        session_placeholders = ", ".join("?" for _ in session_ids)
        role_placeholders = ", ".join("?" for _ in request.roles)
        conditions = [
            "messages_fts MATCH ?",
            "m.agent_id = ?",
            "m.project_id = ?",
            f"m.session_id IN ({session_placeholders})",
            f"m.role IN ({role_placeholders})",
        ]
        parameters: list[Any] = [
            expression,
            request.agent_id,
            _scope(request.project_id),
            *session_ids,
            *request.roles,
        ]
        if request.since is not None:
            conditions.append("m.timestamp >= ?")
            parameters.append(request.since.isoformat())
        if request.until is not None:
            conditions.append("m.timestamp <= ?")
            parameters.append(request.until.isoformat())

        direction = "DESC" if request.sort == "newest" else "ASC"
        parameters.append(request.limit + 1)
        sql = f"""
            SELECT
              m.session_id,
              m.message_id,
              m.message_index,
              m.timestamp,
              bm25(messages_fts) AS rank
            FROM messages_fts
            JOIN messages AS m ON m.row_id = messages_fts.rowid
            WHERE {" AND ".join(conditions)}
            ORDER BY m.timestamp {direction}, m.session_id ASC, m.message_index ASC
            LIMIT ?
        """
        return list(connection.execute(sql, parameters))

    def _hydrate_matches(
        self,
        request: RecallRequest,
        summaries: list[JsonObject],
        rows: Iterable[sqlite3.Row],
    ) -> list[JsonObject]:
        summaries_by_id = {str(summary["id"]): summary for summary in summaries}
        messages_by_session: dict[str, list[Any]] = {}
        matches: list[JsonObject] = []
        for row in rows:
            session_id = str(row["session_id"])
            summary = summaries_by_id.get(session_id)
            if summary is None:
                continue
            if session_id not in messages_by_session:
                messages_by_session[session_id] = self.sessions.get(
                    request.agent_id,
                    session_id,
                    request.project_id,
                ).load()
            messages = messages_by_session[session_id]
            message_index = message_index_by_id(messages, str(row["message_id"]))
            if message_index is None:
                continue
            message = messages[message_index]
            if not message_matches_request(message, request):
                continue
            text = message_search_text(message)
            if not text_matches_query(text, request):
                continue
            matches.append(message_match_payload(request, summary, messages, message_index, text))
        return matches

    @staticmethod
    def _message_result(
        request: RecallRequest,
        matches: list[JsonObject],
        *,
        searched_sessions: int,
        total_candidates: int,
        truncated: bool = False,
    ) -> JsonObject:
        return {
            "content": render_message_matches(request, matches, truncated=truncated),
            "matches": matches,
            "truncated": truncated,
            "searched_sessions": searched_sessions,
            "total_candidate_sessions": total_candidates,
            "request": request_payload(request),
        }

    def _delete_index_file(self) -> None:
        for path in self._index_files():
            path.unlink(missing_ok=True)

    def _index_files(self) -> list[Path]:
        return [
            self.index_path,
            self.index_path.with_name(f"{self.index_path.name}-wal"),
            self.index_path.with_name(f"{self.index_path.name}-shm"),
        ]

    def _warning(self, message: str, *args: object) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            self.logger.warning(message, *args)


def _fts_expression(request: RecallRequest) -> str | None:
    # Trigram MATCH does substring lookup, mirroring the JSONL scanner's `term in haystack`.
    # Terms are split the same way as the JSONL backend so both backends agree on what a term is.
    if request.query is None:
        return None
    if request.match_mode == "phrase":
        phrase = compact_text(request.query).casefold()
        if len(phrase) < _TRIGRAM_MIN_CHARS:
            return None
        return _quote_fts_value(phrase)

    terms = query_terms(request.query)
    if not terms or any(len(term) < _TRIGRAM_MIN_CHARS for term in terms):
        return None
    operator = " OR " if request.match_mode == "any_term" else " AND "
    return operator.join(_quote_fts_value(term) for term in terms)


def _quote_fts_value(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
