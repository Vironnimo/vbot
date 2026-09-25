"""SQLite FTS5 Recall backend for Session search.

Message pages come from the Session store's own search. Literal Passage
retrieval for Hybrid fusion runs over ``recall/session_index.sqlite``, a
disposable projection opened through the kernel (``core/database``): the shared
Passage catalog (:mod:`core.recall._passage_catalog`) plus two FTS5 tables over
Passage text, a trigram table for substring matches and a ``unicode61`` token
table for one- and two-character terms. Each Passage is ranked once and
reported for one candidate Session, so a fork and its origin never both return
the history they share.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.database import APPLICATION_IDS, DISPOSABLE, DatabaseSpec
from core.recall._passage_catalog import (
    CATALOG_SCHEMA,
    VIEWED_BY_SESSIONS,
    Candidates,
    PassageCatalog,
    StoredPassage,
    candidate_refs,
    passage_columns,
    projection_version,
    refs_parameter,
    reported_sessions,
    stored_passage,
    time_bounds,
)
from core.recall.canonical import (
    CanonicalSessionRecallBackend,
    RecallScope,
    _check_snapshot,
    compact_text,
    first_match_span,
    text_matches_search_request,
)
from core.recall.recall import (
    RecallBackendContext,
    RecallSearchCapabilities,
    RecallSearchHit,
    RecallSearchPage,
    RecallSearchRequest,
)

_INDEX_DIR_NAME = "recall"
_INDEX_FILE_NAME = "session_index.sqlite"
# Bump when the index tables or the meaning of their rows change; the kernel
# discards and rebuilds an index built for another version.
_LAYOUT_VERSION = 1
# FTS5 trigram needs at least three characters; shorter values use the token index.
_TRIGRAM_MIN_CHARS = 3
_TRIGRAM_TABLE = "passages_fts"
_TOKEN_TABLE = "passages_fts_tokens"
_TRIGRAM_RANKING = "bm25_trigram"

# FTS5 tables are virtual, which the kernel's declared schema cannot hold, so
# they are created on open together with the triggers that keep them in step
# with ``passages``. Passage rows never change after insertion.
_FTS_TABLES = {
    _TRIGRAM_TABLE: (
        f"CREATE VIRTUAL TABLE {_TRIGRAM_TABLE} USING fts5("
        "text, content='passages', content_rowid='passage_ref', tokenize='trigram')"
    ),
    _TOKEN_TABLE: (
        f"CREATE VIRTUAL TABLE {_TOKEN_TABLE} USING fts5("
        "text, content='passages', content_rowid='passage_ref')"
    ),
}
_FTS_TRIGGERS = {
    "passages_fts_insert": f"""
        CREATE TRIGGER passages_fts_insert AFTER INSERT ON passages BEGIN
          INSERT INTO {_TRIGRAM_TABLE}(rowid, text) VALUES (new.passage_ref, new.text);
          INSERT INTO {_TOKEN_TABLE}(rowid, text) VALUES (new.passage_ref, new.text);
        END
    """,
    "passages_fts_delete": f"""
        CREATE TRIGGER passages_fts_delete AFTER DELETE ON passages BEGIN
          INSERT INTO {_TRIGRAM_TABLE}({_TRIGRAM_TABLE}, rowid, text)
            VALUES ('delete', old.passage_ref, old.text);
          INSERT INTO {_TOKEN_TABLE}({_TOKEN_TABLE}, rowid, text)
            VALUES ('delete', old.passage_ref, old.text);
        END
    """,
}


def _prepare_fts(connection: sqlite3.Connection) -> None:
    """Create missing FTS tables and triggers; a new table indexes existing rows."""
    names = [*_FTS_TABLES, *_FTS_TRIGGERS]
    present = {
        str(row[0])
        for row in connection.execute(
            f"SELECT name FROM sqlite_master WHERE name IN ({', '.join('?' for _ in names)})",
            names,
        )
    }
    if present.issuperset(names):
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        for name, sql in _FTS_TABLES.items():
            if name not in present:
                connection.execute(sql)
                connection.execute(f"INSERT INTO {name}({name}) VALUES ('rebuild')")
        for name, sql in _FTS_TRIGGERS.items():
            if name not in present:
                connection.execute(sql)
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def recall_index_database_spec(path: Path) -> DatabaseSpec:
    """Declare the disposable literal Passage index at ``path``."""
    return DatabaseSpec(
        name="recall_index",
        path=path,
        profile=DISPOSABLE,
        application_id=APPLICATION_IDS["recall_index"],
        format_generation=1,
        schema_sql=CATALOG_SCHEMA,
        projection_version=projection_version(_LAYOUT_VERSION),
        after_open=_prepare_fts,
    )


class SqliteFtsRecallBackend(CanonicalSessionRecallBackend):
    """Recall backend backed by a disposable SQLite FTS index."""

    def __init__(self, context: RecallBackendContext) -> None:
        super().__init__(context.sessions)
        self.data_dir = context.data_dir
        self.index_path = self.data_dir / _INDEX_DIR_NAME / _INDEX_FILE_NAME
        self.logger = context.logger
        self._catalog = PassageCatalog(recall_index_database_spec(self.index_path))

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
        return await self.sessions.run_async(self._search_page, request, use_fts=True)

    async def search_passages(self, request: RecallSearchRequest) -> RecallSearchPage:
        """Return Passage-level literal ranking for Hybrid fusion."""

        prepared = await self.prepare_passage_search(request)
        return await prepared.page(request.offset, request.limit)

    async def prepare_passage_search(
        self, request: RecallSearchRequest, scope: RecallScope | None = None
    ) -> PreparedPassageSearch:
        """Reconcile the Passage index for the request's candidates once.

        The prepared search ranks at any depth without repeating freshness work.
        A caller that already read the request's scope passes it. A damaged
        index is discarded and rebuilt once; an index that still fails, or is
        busy or unavailable, fails the search.
        """

        if scope is None:
            scope = await self.sessions.run_async(self._read_scope, request)
        _check_snapshot(request, scope.snapshot_id)
        query = _passage_query(request)
        if query is not None and scope.candidates:
            await self._catalog.recovering(
                lambda: self._catalog.refresh(
                    self.sessions, request.agent_id, request.project_id, scope
                ),
                warning=lambda error: self._warning(
                    "SQLite Passage index failed; rebuilding once: %s", error
                ),
            )
        return PreparedPassageSearch(self, request, scope, query)

    async def remove_session(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        """Evict one Session from the Passage index (delete-time cleanup).

        Passages another indexed Session still shows stay. Deleting from an
        empty index is a harmless no-op.
        """
        await self._catalog.remove_session(agent_id, project_id, session_id)

    async def aclose(self) -> None:
        self.close()

    def close(self) -> None:
        """Release the index database; later searches fail as unavailable."""
        self._catalog.close()

    async def _query_passage_page(
        self,
        request: RecallSearchRequest,
        scope: RecallScope,
        query: _PassageQuery,
        offset: int,
        limit: int,
    ) -> RecallSearchPage:
        candidates = Candidates.of(request.agent_id, request.project_id, scope)
        try:
            matches = await self._catalog.read(
                lambda connection: _matching_passages(
                    connection, request, candidates, query, offset + limit + 1
                )
            )
        except Exception as error:
            await self._catalog.discard_if_damaged(error)
            raise
        hits = tuple(
            _passage_hit(passage, session_id, rank, request)
            for passage, session_id, rank in matches[offset : offset + limit]
        )
        return RecallSearchPage(
            hits=hits,
            result_type="passage",
            ranking=query.ranking,
            snapshot_id=scope.snapshot_id,
            has_more=len(matches) > offset + limit,
            total_candidate_sessions=len(scope.candidates),
        )

    def _warning(self, message: str, *args: object) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            self.logger.warning(message, *args)


def _matching_passages(
    connection: sqlite3.Connection,
    request: RecallSearchRequest,
    candidates: Candidates,
    query: _PassageQuery,
    wanted: int,
) -> list[tuple[StoredPassage, str, float]]:
    """Return up to *wanted* ranked Passages whose text matches the query literally.

    FTS supplies candidates in rank order; a token-index candidate matches
    whole tokens only (``C#`` is the token ``c``), so each candidate is checked
    before it counts and pages stay full. Each Passage carries the candidate
    Session it is reported for.
    """

    refs = candidate_refs(connection, candidates)
    if not refs:
        return []
    table = query.table
    conditions = [f"{table} MATCH ?", f"p.passage_ref IN ({VIEWED_BY_SESSIONS})"]
    parameters: list[Any] = [query.expression, refs_parameter(refs)]
    bounds, bound_parameters = time_bounds("p", request.since, request.until, lenient=False)
    conditions.extend(bounds)
    parameters.extend(bound_parameters)
    sql = f"""
        SELECT {passage_columns("p")}, bm25({table}) AS rank
        FROM {table}
        JOIN passages AS p ON p.passage_ref = {table}.rowid
        WHERE {" AND ".join(conditions)}
        ORDER BY rank ASC, p.start_timestamp DESC, p.passage_id ASC, p.passage_ref ASC
    """
    matched: list[tuple[StoredPassage, float]] = []
    with closing(connection.execute(sql, parameters)) as cursor:
        for row in cursor:
            if text_matches_search_request(str(row["text"]), request):
                matched.append((stored_passage(row), float(row["rank"])))
                if len(matched) >= wanted:
                    break
    reported = reported_sessions(connection, [passage.passage_ref for passage, _ in matched], refs)
    return [(passage, reported[passage.passage_ref], rank) for passage, rank in matched]


@dataclass(frozen=True)
class _PassageQuery:
    """One FTS table and ``MATCH`` expression for a literal Passage query."""

    table: str
    expression: str
    ranking: str


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
        return await self._backend._query_passage_page(
            self._request, self._scope, self._query, offset, limit
        )


def _passage_hit(
    passage: StoredPassage, session_id: str, rank: float, request: RecallSearchRequest
) -> RecallSearchHit:
    start, end = first_match_span(passage.text, request.query, request.match_mode)
    return RecallSearchHit(
        result_type="passage",
        session_id=session_id,
        message_id=passage.start_message_id,
        role=passage.start_role,
        timestamp=passage.start_timestamp,
        text=passage.text,
        score=rank,
        passage_id=passage.passage_id,
        start_message_id=passage.start_message_id,
        end_message_id=passage.end_message_id,
        end_timestamp=passage.end_timestamp,
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


def _quote_fts_value(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
