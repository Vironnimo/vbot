"""SQLite FTS5 recall backend for Session search."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.recall.canonical import (
    CanonicalSessionRecallBackend,
    RecallScope,
    _check_snapshot,
    _session_address,
    compact_text,
    first_match_span,
    parse_persisted_timestamp,
    query_terms,
    text_matches_search_request,
)
from core.recall.passages import build_session_passages
from core.recall.recall import (
    RecallBackendContext,
    RecallSearchCapabilities,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
)
from core.sessions.schema import required_journal_mode

_INDEX_DIR_NAME = "recall"
_INDEX_FILE_NAME = "session_index.sqlite"
_SQLITE_BUSY_TIMEOUT_MS = 1000


# Bump when the on-disk index schema changes; mismatched indexes are dropped and rebuilt.
# v2 → rows are project-scoped (``project_id`` column in the index keys) so the
#      same session UUID under a project vs. the global scope never collides.
# v3 → persisted session_search results are excluded before candidate limiting.
# v4 → a shared Passage FTS index powers Hybrid's literal retrieval arm.
# v5 → canonical Session generations/revisions replace filesystem freshness,
#      and this disposable index owns Passage retrieval only while message
#      search moves into the canonical Session store.
# v6 → conversation-only Passage policy with separate Compaction summaries.
_SCHEMA_VERSION = 6
# FTS5 trigram needs at least three characters; shorter queries fall back to the canonical scan.
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


class SqliteFtsRecallBackend(CanonicalSessionRecallBackend):
    """Recall backend backed by a disposable SQLite FTS index."""

    def __init__(self, context: RecallBackendContext) -> None:
        super().__init__(context.sessions)
        self.data_dir = context.data_dir
        self.index_path = self.data_dir / _INDEX_DIR_NAME / _INDEX_FILE_NAME
        self.logger = context.logger
        self._index_lock = asyncio.Lock()

    @staticmethod
    def search_capabilities() -> RecallSearchCapabilities:
        query_description = (
            "Distinctive words to find, ignoring case. Every "
            "whitespace-separated term must occur. One- or "
            "two-character terms require whole-token matching for the query; otherwise terms "
            "also match inside words. Matches are ranked by text relevance."
        )
        return RecallSearchCapabilities(
            result_type="message",
            guidance=query_description,
            tool_summary=("Find text from past conversations, ranked by relevance."),
            query_description=query_description,
            match_argument="match",
            match_modes=("all_terms", "any_term", "phrase"),
            order_modes=("relevance", "newest", "oldest"),
            default_order="relevance",
            supports_roles=True,
        )

    async def search_page(self, request: RecallSearchRequest) -> RecallSearchPage:
        return await asyncio.to_thread(self._search_page, request, use_fts=True)

    async def search_passages(self, request: RecallSearchRequest) -> RecallSearchPage:
        """Return Passage-level literal ranking for Hybrid fusion."""

        prepared = await self.prepare_passage_search(request)
        return await prepared.page(request.offset, request.limit)

    async def prepare_passage_search(
        self, request: RecallSearchRequest, scope: RecallScope | None = None
    ) -> PreparedPassageSearch:
        """Reconcile the Passage index for the request's candidates once.

        The prepared search ranks at any depth without repeating freshness work.
        A caller that already read the request's scope passes it.
        """

        if scope is None:
            scope = await asyncio.to_thread(self._read_scope, request)
        _check_snapshot(request, scope.snapshot_id)
        expression = _fts_expression_search(request)
        if expression is not None and scope.candidates:
            async with self._index_lock:
                if not await self._refresh_passage_index(request, scope):
                    expression = None
        return PreparedPassageSearch(self, request, scope, expression)

    async def _refresh_passage_index(
        self, request: RecallSearchRequest, scope: RecallScope
    ) -> bool:
        """Reconcile the disposable index, rebuilding a failed file once."""

        try:
            await asyncio.to_thread(self._sync_passage_index, request, scope)
            return True
        except (OSError, sqlite3.DatabaseError) as error:
            self._warning("SQLite Passage index failed; rebuilding once: %s", error)
            await asyncio.to_thread(self._delete_index_file)
        try:
            await asyncio.to_thread(self._sync_passage_index, request, scope)
            return True
        except (OSError, sqlite3.DatabaseError) as error:
            self._warning("SQLite Passage index rebuild failed: %s", error)
        return False

    def _sync_passage_index(self, request: RecallSearchRequest, scope: RecallScope) -> None:
        with closing(self._connect()) as connection:
            self._initialize_schema(connection)
            self._cleanup_missing_sessions(connection, request, scope.live_session_ids)
            self._ensure_indexed(connection, request, scope.candidates)

    def _query_passage_page(
        self,
        request: RecallSearchRequest,
        scope: RecallScope,
        expression: str,
        offset: int,
        limit: int,
    ) -> RecallSearchPage:
        with closing(self._connect()) as connection:
            rows = self._query_passages(
                connection, request, sorted(scope.candidates), expression, offset, limit
            )
        has_more = len(rows) > limit
        hits = tuple(_passage_hit_from_row(row, request) for row in rows[:limit])
        return RecallSearchPage(
            hits=hits,
            result_type="passage",
            ranking="bm25_trigram",
            snapshot_id=scope.snapshot_id,
            has_more=has_more,
            total_candidate_sessions=len(scope.candidates),
        )

    def _rank_scanned_passages(
        self,
        request: RecallSearchRequest,
        session_ids: list[str],
    ) -> list[RecallSearchHit]:
        ranked: list[tuple[float, str, str, RecallSearchHit]] = []
        for session_id in session_ids:
            messages = self.sessions.get(_session_address(request, session_id)).load_active()
            for passage in build_session_passages(messages):
                if not _passage_in_time_range(
                    passage.start_timestamp,
                    passage.end_timestamp,
                    request,
                ) or not text_matches_search_request(passage.text, request):
                    continue
                start, end = first_match_span(passage.text, request.query, request.match_mode)
                score = -float(passage.text.casefold().count(request.query.casefold()))
                hit = RecallSearchHit(
                    result_type="passage",
                    session_id=session_id,
                    message_id=passage.start_message_id,
                    role=passage.start_role,
                    timestamp=passage.start_timestamp,
                    text=passage.text,
                    score=score,
                    passage_id=passage.passage_id,
                    start_message_id=passage.start_message_id,
                    end_message_id=passage.end_message_id,
                    end_timestamp=passage.end_timestamp,
                    match_start=start,
                    match_end=end,
                    sources=("literal",),
                )
                ranked.append((score, passage.start_timestamp, session_id, hit))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in ranked]

    def _connect(self) -> sqlite3.Connection:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.index_path)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        try:
            journal_mode = required_journal_mode(sqlite3.sqlite_version_info)
            connection.execute(f"PRAGMA journal_mode={journal_mode.upper()}")
            connection.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError as error:
            self._warning("Could not configure the SQLite recall index journal: %s", error)
        return connection

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != _SCHEMA_VERSION:
            connection.executescript(
                """
                DROP TABLE IF EXISTS messages_fts;
                DROP TABLE IF EXISTS messages;
                DROP TABLE IF EXISTS passages_fts;
                DROP TABLE IF EXISTS passages;
                DROP TABLE IF EXISTS indexed_sessions;
                """
            )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS indexed_sessions (
              agent_id TEXT NOT NULL,
              project_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              generation_id TEXT NOT NULL,
              history_revision INTEGER NOT NULL,
              indexed_at TEXT NOT NULL,
              PRIMARY KEY (agent_id, project_id, session_id)
            );

            CREATE TABLE IF NOT EXISTS passages (
              row_id INTEGER PRIMARY KEY,
              agent_id TEXT NOT NULL,
              project_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              passage_id TEXT NOT NULL,
              start_message_id TEXT NOT NULL,
              end_message_id TEXT NOT NULL,
              start_timestamp TEXT NOT NULL,
              end_timestamp TEXT NOT NULL,
              start_role TEXT NOT NULL,
              end_role TEXT NOT NULL,
              search_text TEXT NOT NULL,
              UNIQUE (agent_id, project_id, session_id, passage_id)
            );

            CREATE INDEX IF NOT EXISTS idx_passages_scope
              ON passages(agent_id, project_id, session_id, start_timestamp);

            CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts
            USING fts5(
              search_text,
              content='passages',
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
        request: RecallSearchRequest,
        active_session_ids: frozenset[str],
    ) -> None:
        agent_id = request.agent_id
        scope = _scope(request.project_id)
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
        request: RecallSearchRequest,
        versions: dict[str, tuple[str, int]],
    ) -> None:
        agent_id = request.agent_id
        scope = _scope(request.project_id)
        for session_id, (generation_id, history_revision) in sorted(versions.items()):
            address = _session_address(request, session_id)
            indexed = connection.execute(
                """
                SELECT generation_id, history_revision
                FROM indexed_sessions
                WHERE agent_id = ? AND project_id = ? AND session_id = ?
                """,
                (agent_id, scope, session_id),
            ).fetchone()
            if (
                indexed is not None
                and str(indexed["generation_id"]) == generation_id
                and int(indexed["history_revision"]) == history_revision
            ):
                continue
            session = self.sessions.get(address)
            self._reindex_session(
                connection,
                agent_id,
                scope,
                session_id,
                session.load_active(),
                generation_id=generation_id,
                history_revision=history_revision,
            )

    def _reindex_session(
        self,
        connection: sqlite3.Connection,
        agent_id: str,
        scope: str,
        session_id: str,
        messages: list[Any],
        *,
        generation_id: str,
        history_revision: int,
    ) -> None:
        with connection:
            self._delete_session_rows(connection, agent_id, scope, session_id)
            for passage in build_session_passages(messages):
                cursor = connection.execute(
                    """
                    INSERT INTO passages (
                      agent_id, project_id, session_id, passage_id,
                      start_message_id, end_message_id, start_timestamp, end_timestamp,
                      start_role, end_role, search_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent_id,
                        scope,
                        session_id,
                        passage.passage_id,
                        passage.start_message_id,
                        passage.end_message_id,
                        passage.start_timestamp,
                        passage.end_timestamp,
                        passage.start_role,
                        passage.end_role,
                        passage.text,
                    ),
                )
                row_id = cursor.lastrowid
                if row_id is None:
                    raise sqlite3.DatabaseError("failed to insert recall Passage row")
                connection.execute(
                    "INSERT INTO passages_fts(rowid, search_text) VALUES (?, ?)",
                    (row_id, passage.text),
                )
            connection.execute(
                """
                INSERT INTO indexed_sessions (
                  agent_id,
                  project_id,
                  session_id,
                  generation_id,
                  history_revision,
                  indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    scope,
                    session_id,
                    generation_id,
                    history_revision,
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
        passage_row_ids = [
            int(row["row_id"])
            for row in connection.execute(
                "SELECT row_id FROM passages "
                "WHERE agent_id = ? AND project_id = ? AND session_id = ?",
                (agent_id, scope, session_id),
            )
        ]
        for row_id in passage_row_ids:
            connection.execute("DELETE FROM passages_fts WHERE rowid = ?", (row_id,))
        connection.execute(
            "DELETE FROM passages WHERE agent_id = ? AND project_id = ? AND session_id = ?",
            (agent_id, scope, session_id),
        )
        connection.execute(
            "DELETE FROM indexed_sessions WHERE agent_id = ? AND project_id = ? AND session_id = ?",
            (agent_id, scope, session_id),
        )

    async def remove_session(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        """Evict one session's rows from the FTS index (delete-time cleanup).

        Active counterpart to ``_cleanup_missing_sessions`` (the on-search
        staleness drop): session deletion calls it so a removed session leaves
        keyword search immediately. Mirrors the index path's transaction shape
        (``_connect`` → ensure schema → ``with connection:`` →
        ``_delete_session_rows``); deleting from a freshly initialized or empty
        index is a harmless no-op.
        """
        async with self._index_lock:
            await asyncio.to_thread(self._remove_session, agent_id, session_id, project_id)

    def _remove_session(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        scope = _scope(project_id)
        with closing(self._connect()) as connection:
            self._initialize_schema(connection)
            with connection:
                self._delete_session_rows(connection, agent_id, scope, session_id)

    def _query_passages(
        self,
        connection: sqlite3.Connection,
        request: RecallSearchRequest,
        session_ids: list[str],
        expression: str,
        offset: int,
        limit: int,
    ) -> list[sqlite3.Row]:
        conditions = [
            "passages_fts MATCH ?",
            "p.agent_id = ?",
            "p.project_id = ?",
            "p.session_id IN (SELECT value FROM json_each(?))",
        ]
        parameters: list[Any] = [
            expression,
            request.agent_id,
            _scope(request.project_id),
            json.dumps(session_ids),
        ]
        if request.since is not None:
            conditions.append("julianday(p.end_timestamp) >= julianday(?)")
            parameters.append(request.since.isoformat())
        if request.until is not None:
            conditions.append("julianday(p.start_timestamp) <= julianday(?)")
            parameters.append(request.until.isoformat())
        parameters.extend((limit + 1, offset))
        sql = f"""
            SELECT
              p.session_id,
              p.passage_id,
              p.start_message_id,
              p.end_message_id,
              p.start_timestamp,
              p.end_timestamp,
              p.start_role,
              p.end_role,
              p.search_text,
              bm25(passages_fts) AS rank
            FROM passages_fts
            JOIN passages AS p ON p.row_id = passages_fts.rowid
            WHERE {" AND ".join(conditions)}
            ORDER BY rank ASC, p.start_timestamp DESC, p.session_id ASC, p.passage_id ASC
            LIMIT ? OFFSET ?
        """
        return list(connection.execute(sql, parameters))

    def _delete_index_file(self) -> None:
        for path in self._index_files():
            path.unlink(missing_ok=True)

    def _index_files(self) -> list[Path]:
        return [
            self.index_path,
            self.index_path.with_name(f"{self.index_path.name}-wal"),
            self.index_path.with_name(f"{self.index_path.name}-shm"),
            self.index_path.with_name(f"{self.index_path.name}-journal"),
        ]

    def _warning(self, message: str, *args: object) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            self.logger.warning(message, *args)


class PreparedPassageSearch:
    """Literal Passage ranking over an index reconciled once for one search.

    Without a usable trigram expression or index, the ranking comes from one
    canonical scan that later pages slice.
    """

    def __init__(
        self,
        backend: SqliteFtsRecallBackend,
        request: RecallSearchRequest,
        scope: RecallScope,
        expression: str | None,
    ) -> None:
        self._backend = backend
        self._request = request
        self._scope = scope
        self._expression = expression
        self._scanned: list[RecallSearchHit] | None = None

    async def page(self, offset: int, limit: int) -> RecallSearchPage:
        if self._expression is not None:
            if not self._scope.candidates:
                return self._page((), ranking="bm25_trigram", has_more=False)
            async with self._backend._index_lock:
                try:
                    return await asyncio.to_thread(
                        self._backend._query_passage_page,
                        self._request,
                        self._scope,
                        self._expression,
                        offset,
                        limit,
                    )
                except (OSError, sqlite3.DatabaseError) as error:
                    self._backend._warning(
                        "SQLite Passage query failed; scanning canonical history: %s", error
                    )
                    self._expression = None
        if self._scanned is None:
            self._scanned = await asyncio.to_thread(
                self._backend._rank_scanned_passages, self._request, sorted(self._scope.candidates)
            )
        selected = self._scanned[offset : offset + limit]
        return self._page(
            tuple(selected),
            ranking="substring_scan_relevance",
            has_more=offset + len(selected) < len(self._scanned),
        )

    def _page(
        self,
        hits: tuple[RecallSearchHit, ...],
        *,
        ranking: str,
        has_more: bool,
    ) -> RecallSearchPage:
        return RecallSearchPage(
            hits=hits,
            result_type="passage",
            ranking=ranking,
            snapshot_id=self._scope.snapshot_id,
            has_more=has_more,
            total_candidate_sessions=len(self._scope.candidates),
        )


def _passage_hit_from_row(row: sqlite3.Row, request: RecallSearchRequest) -> RecallSearchHit:
    text = str(row["search_text"])
    start, end = first_match_span(text, request.query, request.match_mode)
    return RecallSearchHit(
        result_type="passage",
        session_id=str(row["session_id"]),
        message_id=str(row["start_message_id"]),
        role=str(row["start_role"]),
        timestamp=str(row["start_timestamp"]),
        text=text,
        score=float(row["rank"]),
        passage_id=str(row["passage_id"]),
        start_message_id=str(row["start_message_id"]),
        end_message_id=str(row["end_message_id"]),
        end_timestamp=str(row["end_timestamp"]),
        match_start=start,
        match_end=end,
        sources=("literal",),
    )


def _passage_in_time_range(
    start_timestamp: str,
    end_timestamp: str,
    request: RecallSearchRequest,
) -> bool:
    start = parse_persisted_timestamp(start_timestamp)
    end = parse_persisted_timestamp(end_timestamp)
    if request.since is not None and (end is None or end < request.since):
        return False
    return not (request.until is not None and (start is None or start > request.until))


def _fts_expression_search(request: RecallSearchRequest) -> str | None:
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
