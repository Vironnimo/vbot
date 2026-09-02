"""SQLite FTS5 recall backend for Session search."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from core.chat.messages import ChatMessage
from core.recall.canonical import (
    CanonicalSessionRecallBackend,
    compact_text,
    first_match_span,
    message_index_by_id,
    message_match_payload,
    message_matches_request,
    message_matches_search_request,
    message_search_text,
    parse_persisted_timestamp,
    query_terms,
    render_message_matches,
    request_payload,
    text_matches_query,
    text_matches_search_request,
)
from core.recall.passages import build_session_passages
from core.recall.recall import (
    JsonObject,
    RecallBackendContext,
    RecallRequest,
    RecallSearchCapabilities,
    RecallSearchError,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
)
from core.sessions import SessionAddress
from core.sessions.schema import required_journal_mode

_INDEX_DIR_NAME = "recall"
_INDEX_FILE_NAME = "session_index.sqlite"
_SQLITE_BUSY_TIMEOUT_MS = 1000


def _session_address(request: Any, session_id: str) -> SessionAddress:
    """Address the one Session of a search request's scope."""
    return SessionAddress(
        project_id=request.project_id, agent_id=request.agent_id, session_id=session_id
    )


# Bump when the on-disk index schema changes; mismatched indexes are dropped and rebuilt.
# v2 → rows are project-scoped (``project_id`` column in the index keys) so the
#      same session UUID under a project vs. the global scope never collides.
# v3 → persisted session_search results are excluded before candidate limiting.
# v4 → a shared Passage FTS index powers Hybrid's literal retrieval arm.
# v5 → canonical Session generations/revisions replace filesystem freshness,
#      and this disposable index owns Passage retrieval only while message
#      search moves into the canonical Session store.
_SCHEMA_VERSION = 5
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
        self._fallback = CanonicalSessionRecallBackend(context.sessions)
        self._index_lock = asyncio.Lock()

    async def search(self, request: RecallRequest) -> JsonObject:
        summaries = await asyncio.to_thread(self.candidate_session_summaries, request)
        if request.query is None:
            return self.session_summary_result(request, summaries)
        if not summaries:
            return self._message_result(request, [], searched_sessions=0, total_candidates=0)
        expression = _fts_expression(request)
        if expression is None:
            return await self._fallback.search(request)
        # Integrated FTS is the only lexical index; fallback is canonical scan.
        if self.sessions.is_fts_available():
            try:
                return await asyncio.to_thread(self._search_with_canonical_fts, request, summaries)
            except Exception as error:  # pragma: no cover - fallback
                self._warning("Canonical FTS search failed; falling back: %s", error)
        return await self._fallback.search(request)

    def _search_with_canonical_fts(
        self, request: RecallRequest, summaries: list[JsonObject]
    ) -> JsonObject:
        # Use integrated FTS; combine with canonical scan fallback for gap if needed.
        # For now, canonical FTS is synchronous so no gap.
        rows = self.sessions.fts_search(
            request.query or "",
            project_id=request.project_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            match_mode=request.match_mode,
            limit=max(256, (request.limit + 1) * 8),
            roles=request.roles,
            since=None if request.since is None else request.since.isoformat(),
            until=None if request.until is None else request.until.isoformat(),
        )
        # Rows carry one reconstructed payload from the normalized canonical tables.
        summaries_by_id = {str(summary["id"]): summary for summary in summaries}
        messages_by_session: dict[str, list[Any]] = {}
        matches: list[JsonObject] = []
        for address, message_id, _ts, _mj, _rank in rows:
            session_id = address.session_id
            if session_id not in summaries_by_id:
                continue
            if session_id not in messages_by_session:
                messages_by_session[session_id] = self.sessions.get(address).load_active()
            messages = messages_by_session[session_id]
            idx = message_index_by_id(messages, message_id)
            if idx is None:
                continue
            message = messages[idx]
            if not message_matches_request(message, request):
                continue
            text = message_search_text(message)
            if not text_matches_query(text, request):
                continue
            matches.append(
                message_match_payload(request, summaries_by_id[session_id], messages, idx, text)
            )
            if len(matches) >= request.limit + 1:
                break
        truncated = len(matches) > request.limit
        return self._message_result(
            request,
            matches[: request.limit],
            searched_sessions=len(summaries),
            total_candidates=len(summaries),
            truncated=truncated,
        )

    def _search_page_with_canonical_fts(
        self, request: RecallSearchRequest, summaries: list[JsonObject], snapshot_id: str
    ) -> RecallSearchPage:
        rows = self.sessions.fts_search(
            request.query,
            project_id=request.project_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            match_mode=request.match_mode,
            limit=request.offset + request.limit + 1,
            roles=request.roles,
            since=None if request.since is None else request.since.isoformat(),
            until=None if request.until is None else request.until.isoformat(),
            excluded_session_ids=request.excluded_session_ids,
        )
        summaries_by_id = {str(s["id"]): s for s in summaries}
        hits: list[tuple[float, str, str, RecallSearchHit]] = []
        for address, message_id, timestamp, message_payload, rank in rows:
            session_id = address.session_id
            if session_id not in summaries_by_id:
                continue
            message = _message_from_fts_payload(message_payload)
            if str(message.id) != message_id:
                continue
            if not message_matches_search_request(message, request):
                continue
            text = message_search_text(message)
            if not text_matches_search_request(text, request):
                continue
            sort_rank = float(rank) if request.order == "relevance" else 0.0
            hits.append(
                (
                    sort_rank,
                    timestamp,
                    session_id,
                    RecallSearchHit(
                        result_type="message",
                        session_id=session_id,
                        message_id=str(message.id),
                        role=str(message.role),
                        timestamp=str(message.timestamp),
                        text=text,
                        score=float(rank),
                        match_start=first_match_span(text, request.query, request.match_mode)[0],
                        match_end=first_match_span(text, request.query, request.match_mode)[1],
                    ),
                )
            )

        def _ts(value: str) -> float:
            parsed_ts = parse_persisted_timestamp(value)
            return parsed_ts.timestamp() if parsed_ts is not None else 0.0

        if request.order == "relevance":
            hits.sort(key=lambda x: (x[0], -_ts(x[1])))
        elif request.order == "newest":
            hits.sort(key=lambda x: _ts(x[1]), reverse=True)
        else:
            hits.sort(key=lambda x: _ts(x[1]))
        page_hits = hits[request.offset : request.offset + request.limit + 1]
        has_more = len(page_hits) > request.limit
        page_hits = page_hits[: request.limit]
        return RecallSearchPage(
            hits=tuple(h[3] for h in page_hits),
            result_type="message",
            ranking="bm25" if request.order == "relevance" else f"message_time_{request.order}",
            snapshot_id=snapshot_id,
            has_more=has_more,
            total_candidate_sessions=len(summaries),
        )

    def search_capabilities(self) -> RecallSearchCapabilities:
        query_description = (
            "Literal terms to find. Every whitespace-separated term must occur. One- or "
            "two-character terms match whole tokens; longer terms also match inside words. "
            "Omit to list recent Sessions. Matches are ranked by text relevance."
        )
        return RecallSearchCapabilities(
            result_type="message",
            guidance=query_description,
            tool_summary=(
                "Find persisted Sessions and relevance-ranked literal matches in past "
                "conversations."
            ),
            query_description=query_description,
            match_argument="match",
            match_modes=("all_terms", "any_term", "phrase"),
            order_modes=("relevance", "newest", "oldest"),
            default_order="relevance",
            supports_roles=True,
        )

    async def search_page(self, request: RecallSearchRequest) -> RecallSearchPage:
        summaries = await asyncio.to_thread(self._search_candidate_summaries, request)
        snapshot_id = await asyncio.to_thread(self._search_snapshot, request, summaries)
        if request.snapshot_id is not None and request.snapshot_id != snapshot_id:
            raise RecallSearchError(
                "stale_cursor", "Session search source changed; repeat the search."
            )
        # Integrated FTS only; otherwise canonical scan.
        if self.sessions.is_fts_available():
            try:
                return await asyncio.to_thread(
                    self._search_page_with_canonical_fts, request, summaries, snapshot_id
                )
            except Exception as error:  # pragma: no cover
                self._warning("Canonical FTS page failed; falling back: %s", error)
        fallback_request = (
            replace(request, order="newest") if request.order == "relevance" else request
        )
        page = await self._fallback.search_page(fallback_request)
        return replace(page, ranking=f"substring_scan_{fallback_request.order}")

    async def search_passages(self, request: RecallSearchRequest) -> RecallSearchPage:
        """Return Passage-level literal ranking for Hybrid fusion."""

        summaries = await asyncio.to_thread(self._search_candidate_summaries, request)
        snapshot_id = await asyncio.to_thread(self._search_snapshot, request, summaries)
        if request.snapshot_id is not None and request.snapshot_id != snapshot_id:
            raise RecallSearchError(
                "stale_cursor", "Session search source changed; repeat the search."
            )
        expression = _fts_expression_search(request)
        if expression is None:
            return await asyncio.to_thread(self._scan_passages, request, summaries, snapshot_id)
        async with self._index_lock:
            try:
                return await asyncio.to_thread(
                    self._search_passages_with_sqlite,
                    request,
                    summaries,
                    expression,
                    snapshot_id,
                )
            except (OSError, sqlite3.DatabaseError) as error:
                self._warning("SQLite Passage index failed; rebuilding once: %s", error)
                await asyncio.to_thread(self._delete_index_file)
            try:
                return await asyncio.to_thread(
                    self._search_passages_with_sqlite,
                    request,
                    summaries,
                    expression,
                    snapshot_id,
                )
            except (OSError, sqlite3.DatabaseError) as error:
                self._warning("SQLite Passage index rebuild failed: %s", error)
        return await asyncio.to_thread(self._scan_passages, request, summaries, snapshot_id)

    def _search_passages_with_sqlite(
        self,
        request: RecallSearchRequest,
        summaries: list[JsonObject],
        expression: str,
        snapshot_id: str,
    ) -> RecallSearchPage:
        if not summaries:
            return _empty_passage_page(snapshot_id)
        with closing(self._connect()) as connection:
            self._initialize_schema(connection)
            self._cleanup_missing_sessions(connection, request)
            self._ensure_indexed(connection, request, summaries)
            rows = self._query_passages(connection, request, summaries, expression)
        has_more = len(rows) > request.limit
        hits = tuple(_passage_hit_from_row(row, request) for row in rows[: request.limit])
        return RecallSearchPage(
            hits=hits,
            result_type="passage",
            ranking="bm25_trigram",
            snapshot_id=snapshot_id,
            has_more=has_more,
            total_candidate_sessions=len(summaries),
        )

    def _scan_passages(
        self,
        request: RecallSearchRequest,
        summaries: list[JsonObject],
        snapshot_id: str,
    ) -> RecallSearchPage:
        ranked: list[tuple[float, str, str, RecallSearchHit]] = []
        for summary in summaries:
            session_id = str(summary["id"])
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
        page = ranked[request.offset : request.offset + request.limit]
        return RecallSearchPage(
            hits=tuple(item[3] for item in page),
            result_type="passage",
            ranking="substring_scan_relevance",
            snapshot_id=snapshot_id,
            has_more=request.offset + len(page) < len(ranked),
            total_candidate_sessions=len(summaries),
        )

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
        request: RecallRequest | RecallSearchRequest,
    ) -> None:
        agent_id = request.agent_id
        scope = _scope(request.project_id)
        active_session_ids = {
            str(summary["id"])
            for summary in cast(
                list[JsonObject], self.sessions.list_summaries(agent_id, request.project_id)
            )
        }
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
        request: RecallRequest | RecallSearchRequest,
        summaries: list[JsonObject],
    ) -> None:
        agent_id = request.agent_id
        scope = _scope(request.project_id)
        # One batched canonical-freshness query for the whole scope instead
        # of one query per Session.
        addresses = [_session_address(request, str(summary["id"])) for summary in summaries]
        versions = self.sessions.list_history_versions(addresses)
        for summary in summaries:
            session_id = str(summary["id"])
            address = _session_address(request, session_id)
            version = versions.get(address)
            if version is None:
                # The Session vanished between listing and indexing; skip and
                # let the cleanup pass remove any stale indexed rows.
                continue
            generation_id, history_revision = version
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
        summaries: list[JsonObject],
        expression: str,
    ) -> list[sqlite3.Row]:
        session_ids = [str(summary["id"]) for summary in summaries]
        session_placeholders = ", ".join("?" for _ in session_ids)
        conditions = [
            "passages_fts MATCH ?",
            "p.agent_id = ?",
            "p.project_id = ?",
            f"p.session_id IN ({session_placeholders})",
        ]
        parameters: list[Any] = [
            expression,
            request.agent_id,
            _scope(request.project_id),
            *session_ids,
        ]
        if request.since is not None:
            conditions.append("julianday(p.end_timestamp) >= julianday(?)")
            parameters.append(request.since.isoformat())
        if request.until is not None:
            conditions.append("julianday(p.start_timestamp) <= julianday(?)")
            parameters.append(request.until.isoformat())
        parameters.extend((request.limit + 1, request.offset))
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
            self.index_path.with_name(f"{self.index_path.name}-journal"),
        ]

    def _warning(self, message: str, *args: object) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            self.logger.warning(message, *args)


def _empty_passage_page(snapshot_id: str) -> RecallSearchPage:
    return RecallSearchPage(
        hits=(),
        result_type="passage",
        ranking="bm25_trigram",
        snapshot_id=snapshot_id,
        has_more=False,
        total_candidate_sessions=0,
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


def _fts_expression(request: RecallRequest) -> str | None:
    # Trigram MATCH does substring lookup, mirroring the canonical scanner's `term in haystack`.
    # Terms are split like the canonical backend so both agree on what a term is.
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


def _message_from_fts_payload(payload: str) -> ChatMessage:
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("canonical FTS payload must be a Message object")
    return ChatMessage.from_dict(cast(JsonObject, data))
