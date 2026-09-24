"""SQLite FTS5 recall backend for Session search."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.database import required_journal_mode
from core.recall.canonical import (
    CanonicalSessionRecallBackend,
    RecallScope,
    _check_snapshot,
    _session_address,
    compact_text,
    first_match_span,
    text_matches_search_request,
)
from core.recall.passages import Passage, build_session_passages
from core.recall.recall import (
    RecallBackendContext,
    RecallSearchCapabilities,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
)
from core.sessions import SessionNotFoundError

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
# v7 → rows carry a text hash for incremental reindexing, triggers keep the FTS
#      tables in step, and a unicode61 token index answers short terms.
_SCHEMA_VERSION = 7
# FTS5 trigram needs at least three characters; shorter values use the token index.
_TRIGRAM_MIN_CHARS = 3
_TRIGRAM_TABLE = "passages_fts"
_TOKEN_TABLE = "passages_fts_tokens"
_TRIGRAM_RANKING = "bm25_trigram"
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
        A caller that already read the request's scope passes it. An index that
        cannot be reconciled even after one rebuild fails the search.
        """

        if scope is None:
            scope = await asyncio.to_thread(self._read_scope, request)
        _check_snapshot(request, scope.snapshot_id)
        query = _passage_query(request)
        if query is not None and scope.candidates:
            async with self._index_lock:
                await self._refresh_passage_index(request, scope)
        return PreparedPassageSearch(self, request, scope, query)

    async def _refresh_passage_index(
        self, request: RecallSearchRequest, scope: RecallScope
    ) -> None:
        """Reconcile the disposable index, rebuilding a failed file once."""

        try:
            await asyncio.to_thread(self._sync_passage_index, request, scope)
            return
        except (OSError, sqlite3.DatabaseError) as error:
            self._warning("SQLite Passage index failed; rebuilding once: %s", error)
            await asyncio.to_thread(self._delete_index_file)
        await asyncio.to_thread(self._sync_passage_index, request, scope)

    def _sync_passage_index(self, request: RecallSearchRequest, scope: RecallScope) -> None:
        """Bring the candidates' Passages up to date in one write transaction.

        One stamp read finds indexed Sessions that left the scope and
        candidates whose ``(generation_id, history_revision)`` changed. A
        changed Session is reread together with the version its history
        belongs to, and its Passages are diffed against stored rows: a row
        survives only when its Passage id, text hash and boundaries all match,
        so unchanged Passages keep their FTS entries. Only the request's
        candidates are reconciled; Sessions that left the scope are pruned.
        """

        agent_id = request.agent_id
        project = _scope(request.project_id)
        with closing(self._connect()) as connection:
            self._initialize_schema(connection)
            indexed = self._read_stamps(connection, agent_id, project)
            pruned = set(indexed) - scope.live_session_ids
            changes: list[_SessionPassages] = []
            for session_id, version in sorted(scope.candidates.items()):
                previous = indexed.get(session_id)
                if previous == version:
                    continue
                try:
                    batch = self.sessions.get(_session_address(request, session_id)).load_since(
                        None
                    )
                except SessionNotFoundError:
                    # Vanished since the scope read: its rows describe no live Session.
                    if previous is not None:
                        pruned.add(session_id)
                    continue
                if batch is None:  # Only a stale cursor yields no batch; a full read has none.
                    continue
                changes.append(
                    _SessionPassages(
                        session_id=session_id,
                        previous=previous,
                        version=(batch.cursor.generation_id, batch.cursor.history_revision),
                        passages=tuple(build_session_passages(batch.active_messages)),
                    )
                )
            if not pruned and not changes:
                return
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._delete_sessions(connection, agent_id, project, pruned)
                for change in changes:
                    self._apply_session_change(connection, agent_id, project, change)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _read_stamps(
        connection: sqlite3.Connection, agent_id: str, project: str
    ) -> dict[str, tuple[str, int]]:
        return {
            str(row["session_id"]): (str(row["generation_id"]), int(row["history_revision"]))
            for row in connection.execute(
                "SELECT session_id, generation_id, history_revision FROM indexed_sessions "
                "WHERE agent_id = ? AND project_id = ?",
                (agent_id, project),
            )
        }

    @staticmethod
    def _apply_session_change(
        connection: sqlite3.Connection,
        agent_id: str,
        project: str,
        change: _SessionPassages,
    ) -> None:
        """Diff one Session's Passages against its stored rows and write the difference.

        A Session whose stamp moved since planning was refreshed by another
        writer and is skipped.
        """

        scope = (agent_id, project, change.session_id)
        stamp = connection.execute(
            "SELECT generation_id, history_revision FROM indexed_sessions "
            "WHERE agent_id = ? AND project_id = ? AND session_id = ?",
            scope,
        ).fetchone()
        current = None if stamp is None else (str(stamp[0]), int(stamp[1]))
        if current != change.previous:
            return
        stored: dict[tuple[str, ...], list[int]] = {}
        for row in connection.execute(
            "SELECT row_id, passage_id, text_hash, start_message_id, end_message_id, "
            "start_timestamp, end_timestamp, start_role, end_role FROM passages "
            "WHERE agent_id = ? AND project_id = ? AND session_id = ?",
            scope,
        ):
            key = tuple(str(value) for value in tuple(row)[1:])
            stored.setdefault(key, []).append(int(row["row_id"]))
        inserts: list[tuple[Any, ...]] = []
        for passage in change.passages:
            key = _passage_key(passage)
            row_ids = stored.get(key)
            if row_ids:
                row_ids.pop()
                continue
            inserts.append((*scope, *key, passage.text))
        # Triggers keep both FTS tables in step with ``passages``.
        connection.executemany(
            "DELETE FROM passages WHERE row_id = ?",
            [(row_id,) for row_ids in stored.values() for row_id in row_ids],
        )
        connection.executemany(
            """
            INSERT INTO passages (
              agent_id, project_id, session_id, passage_id, text_hash,
              start_message_id, end_message_id, start_timestamp, end_timestamp,
              start_role, end_role, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            inserts,
        )
        connection.execute(
            """
            INSERT INTO indexed_sessions (
              agent_id, project_id, session_id, generation_id, history_revision, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (agent_id, project_id, session_id) DO UPDATE SET
              generation_id = excluded.generation_id,
              history_revision = excluded.history_revision,
              indexed_at = excluded.indexed_at
            """,
            (*scope, *change.version, datetime.now(UTC).isoformat()),
        )

    @staticmethod
    def _delete_sessions(
        connection: sqlite3.Connection,
        agent_id: str,
        project: str,
        session_ids: Iterable[str],
    ) -> None:
        """Delete the rows and stamps of *session_ids* set-wise."""

        selected = json.dumps(sorted(session_ids))
        if selected == "[]":
            return
        for table in ("passages", "indexed_sessions"):
            connection.execute(
                f"DELETE FROM {table} WHERE agent_id = ? AND project_id = ? "
                "AND session_id IN (SELECT value FROM json_each(?))",
                (agent_id, project, selected),
            )

    def _query_passage_page(
        self,
        request: RecallSearchRequest,
        scope: RecallScope,
        query: _PassageQuery,
        offset: int,
        limit: int,
    ) -> RecallSearchPage:
        with closing(self._connect()) as connection:
            rows = self._matching_passages(
                connection, request, sorted(scope.candidates), query, offset + limit + 1
            )
        hits = tuple(_passage_hit_from_row(row, request) for row in rows[offset : offset + limit])
        return RecallSearchPage(
            hits=hits,
            result_type="passage",
            ranking=query.ranking,
            snapshot_id=scope.snapshot_id,
            has_more=len(rows) > offset + limit,
            total_candidate_sessions=len(scope.candidates),
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
                DROP TRIGGER IF EXISTS passages_after_insert;
                DROP TRIGGER IF EXISTS passages_after_delete;
                DROP TABLE IF EXISTS messages_fts;
                DROP TABLE IF EXISTS messages;
                DROP TABLE IF EXISTS passages_fts;
                DROP TABLE IF EXISTS passages_fts_tokens;
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
              text_hash TEXT NOT NULL,
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

            CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts_tokens
            USING fts5(
              search_text,
              content='passages',
              content_rowid='row_id'
            );

            CREATE TRIGGER IF NOT EXISTS passages_after_insert AFTER INSERT ON passages BEGIN
              INSERT INTO passages_fts(rowid, search_text) VALUES (new.row_id, new.search_text);
              INSERT INTO passages_fts_tokens(rowid, search_text)
                VALUES (new.row_id, new.search_text);
            END;

            CREATE TRIGGER IF NOT EXISTS passages_after_delete AFTER DELETE ON passages BEGIN
              INSERT INTO passages_fts(passages_fts, rowid, search_text)
                VALUES ('delete', old.row_id, old.search_text);
              INSERT INTO passages_fts_tokens(passages_fts_tokens, rowid, search_text)
                VALUES ('delete', old.row_id, old.search_text);
            END;
            """
        )
        if version != _SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    async def remove_session(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        """Evict one session's rows from the FTS index (delete-time cleanup).

        Active counterpart to the pruning in ``_sync_passage_index``: session
        deletion calls it so a removed session leaves keyword search
        immediately. Deleting from a freshly initialized or empty index is a
        harmless no-op.
        """
        async with self._index_lock:
            await asyncio.to_thread(self._remove_session, agent_id, session_id, project_id)

    def _remove_session(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        with closing(self._connect()) as connection:
            self._initialize_schema(connection)
            with connection:
                self._delete_sessions(connection, agent_id, _scope(project_id), (session_id,))

    @staticmethod
    def _matching_passages(
        connection: sqlite3.Connection,
        request: RecallSearchRequest,
        session_ids: list[str],
        query: _PassageQuery,
        wanted: int,
    ) -> list[sqlite3.Row]:
        """Return up to *wanted* ranked Passages whose text matches the query literally.

        FTS supplies candidates in rank order; a token-index candidate matches
        whole tokens only (``C#`` is the token ``c``), so each candidate is
        checked before it counts and pages stay full.
        """

        table = query.table
        conditions = [
            f"{table} MATCH ?",
            "p.agent_id = ?",
            "p.project_id = ?",
            "p.session_id IN (SELECT value FROM json_each(?))",
        ]
        parameters: list[Any] = [
            query.expression,
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
              bm25({table}) AS rank
            FROM {table}
            JOIN passages AS p ON p.row_id = {table}.rowid
            WHERE {" AND ".join(conditions)}
            ORDER BY rank ASC, p.start_timestamp DESC, p.session_id ASC, p.passage_id ASC
        """
        matched: list[sqlite3.Row] = []
        with closing(connection.execute(sql, parameters)) as cursor:
            for row in cursor:
                if text_matches_search_request(str(row["search_text"]), request):
                    matched.append(row)
                    if len(matched) >= wanted:
                        break
        return matched

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


@dataclass(frozen=True)
class _PassageQuery:
    """One FTS table and ``MATCH`` expression for a literal Passage query."""

    table: str
    expression: str
    ranking: str


@dataclass(frozen=True)
class _SessionPassages:
    """Current Passages of one changed Session and the stamp they replace."""

    session_id: str
    previous: tuple[str, int] | None
    version: tuple[str, int]
    passages: tuple[Passage, ...]


class PreparedPassageSearch:
    """Literal Passage ranking over an index reconciled once for one search."""

    def __init__(
        self,
        backend: SqliteFtsRecallBackend,
        request: RecallSearchRequest,
        scope: RecallScope,
        query: _PassageQuery | None,
    ) -> None:
        self._backend = backend
        self._request = request
        self._scope = scope
        self._query = query

    async def page(self, offset: int, limit: int) -> RecallSearchPage:
        if self._query is None or not self._scope.candidates:
            return RecallSearchPage(
                hits=(),
                result_type="passage",
                ranking=_TRIGRAM_RANKING if self._query is None else self._query.ranking,
                snapshot_id=self._scope.snapshot_id,
                has_more=False,
                total_candidate_sessions=len(self._scope.candidates),
            )
        async with self._backend._index_lock:
            return await asyncio.to_thread(
                self._backend._query_passage_page,
                self._request,
                self._scope,
                self._query,
                offset,
                limit,
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


def _passage_query(request: RecallSearchRequest) -> _PassageQuery | None:
    """Choose the trigram index when every value has three characters, else tokens.

    Values keep their spelling; both indexes fold case themselves.
    """

    compact = compact_text(request.query)
    if request.match_mode == "phrase":
        values = [compact] if compact else []
        operator = ""
    else:
        values = [term for term in compact.split(" ") if term]
        operator = " OR " if request.match_mode == "any_term" else " AND "
    if not values:
        return None
    expression = operator.join(_quote_fts_value(value) for value in values)
    if all(len(value) >= _TRIGRAM_MIN_CHARS for value in values):
        return _PassageQuery(_TRIGRAM_TABLE, expression, _TRIGRAM_RANKING)
    return _PassageQuery(_TOKEN_TABLE, expression, "bm25_token")


def _passage_key(passage: Passage) -> tuple[str, ...]:
    """Identity of a stored Passage row; the order matches the stored-row read."""

    return (
        passage.passage_id,
        hashlib.sha256(passage.text.encode("utf-8")).hexdigest(),
        passage.start_message_id,
        passage.end_message_id,
        passage.start_timestamp,
        passage.end_timestamp,
        passage.start_role,
        passage.end_role,
    )


def _quote_fts_value(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
