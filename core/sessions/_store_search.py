"""Exact, bounded Message search within one supplied read transaction."""
# ruff: noqa: E501

from __future__ import annotations

import builtins
import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import closing
from typing import Any

from core.sessions import (
    _store_fts,
    _store_values,
)
from core.sessions._types import SessionSearchHit, SessionSearchOrder, SessionSearchResult
from core.sessions.schema import (
    FTS_TABLE,
    FTS_TRIGRAM_TABLE,
)

# Candidates are checked in batches whose text is read by key; the first batch
# is sized to the request so a typical page reads only the rows it returns.
_MIN_FIRST_BATCH = 16
_CHECK_BATCH = 256
_PROJECTION_SQL = (
    "SELECT m.message_key, s.project_id, s.agent_id, s.session_id, m.message_id, m.role, "
    "m.timestamp, COALESCE(m.content, m.content_search) AS text, t.name AS tool_name "
    "FROM history_records AS m JOIN sessions AS s ON s.session_key = m.session_key "
    "LEFT JOIN tool_calls AS t ON t.result_key = m.message_key "
    f"WHERE {_store_values._KEYED_RECORDS}"
)

_Batch = builtins.list[tuple[int, float]]


def search(
    connection: sqlite3.Connection,
    query: str,
    *,
    project_id: str | None,
    agent_id: str | None,
    session_id: str | None = None,
    match_mode: str = "all_terms",
    order: SessionSearchOrder = "relevance",
    limit: int = _store_values._SEARCH_RESULT_LIMIT,
    roles: Sequence[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    excluded_session_ids: Sequence[str] = (),
    include_subagents: bool = False,
    use_fts: bool = True,
    fallback_reason: str | None = None,
) -> SessionSearchResult:
    """Return up to ``limit`` exactly matching active Messages in ``order``.

    FTS or a scan only enumerates candidates in order; each candidate's
    conversation text is checked against the literal query before it counts,
    so the result is full whenever enough matches exist. At most
    ``_SEARCH_CANDIDATE_LIMIT`` candidates are checked; a result cut short by
    that budget is marked incomplete. Scans order by Message time, newest first
    for relevance. ``fallback_reason`` labels a scan the caller forced with
    ``use_fts=False``. All SQL runs in the caller's read transaction.
    """
    empty = SessionSearchResult((), True, "fts" if use_fts else "scan", fallback_reason)
    if not query or not query.strip() or limit <= 0 or (roles is not None and not roles):
        return empty
    compact = re.sub(r"\s+", " ", query).strip()
    if not compact:
        return empty

    folded = compact.casefold()
    folded_terms = [term for term in folded.split(" ") if term]

    def matches(text: str) -> bool:
        # str.split() and the regex ``\s`` class share one whitespace definition.
        haystack = " ".join(text.split()).casefold()
        if match_mode == "phrase":
            return folded in haystack
        if match_mode == "any_term":
            return any(term in haystack for term in folded_terms)
        return all(term in haystack for term in folded_terms)

    where, params = _record_filter(
        project_id=project_id,
        agent_id=agent_id,
        session_id=session_id,
        roles=roles,
        since=since,
        until=until,
        excluded_session_ids=excluded_session_ids,
        include_subagents=include_subagents,
    )
    check = _Check(connection, matches=matches, limit=limit)

    if use_fts:
        if _store_fts._fts_health_from_connection(connection, verify_coverage=False).available:
            tool_inclusive = roles is None or "tool" in roles
            table = (
                FTS_TABLE
                if tool_inclusive or not _trigram_supported(compact, match_mode)
                else FTS_TRIGRAM_TABLE
            )
            hits, complete = check.run(
                _fts_candidates(
                    connection, table, _fts_expression(compact, match_mode), where, params, order
                ),
                ranked=order == "relevance",
            )
            # The trigram index omits Tool rows, so a Tool-inclusive search uses
            # whole-token FTS; when that finds nothing, substrings are scanned.
            if hits or not tool_inclusive:
                return SessionSearchResult(tuple(hits), complete, "fts")
            fallback_reason = "tool_inclusive"
        else:
            fallback_reason = "fts_unavailable"
    hits, complete = check.run(_scan_candidates(connection, where, params, order), ranked=False)
    return SessionSearchResult(tuple(hits), complete, "scan", fallback_reason)


def _record_filter(
    *,
    project_id: str | None,
    agent_id: str | None,
    session_id: str | None,
    roles: Sequence[str] | None,
    since: str | None,
    until: str | None,
    excluded_session_ids: Sequence[str],
    include_subagents: bool,
) -> tuple[str, list[Any]]:
    """Return the eligible ``history_records AS m`` filter and its parameters.

    Every term refers only to view columns and uncorrelated subqueries, so SQLite
    pushes the live Session scope into each view branch and its Session index.
    The terms also avoid equality on view columns (``active <> 0``, a JSON role
    list): an equality lets SQLite build an automatic index over the scoped view
    rows when this filter runs inside an ``IN`` subquery.
    """
    scope = (
        "SELECT session_key FROM sessions WHERE status = 'live' AND project_id = ? AND "
        + _store_values._recall_visibility_sql(include_subagents=include_subagents)
    )
    params: list[Any] = [project_id if project_id is not None else ""]
    if agent_id is not None:
        scope += " AND agent_id = ?"
        params.append(agent_id)
    if session_id is not None:
        scope += " AND session_id = ?"
        params.append(session_id)
    if excluded_session_ids:
        placeholders = ", ".join("?" for _ in excluded_session_ids)
        scope += f" AND session_id NOT IN ({placeholders})"
        params.extend(excluded_session_ids)
    where = f"m.session_key IN ({scope}) AND m.active <> 0"
    if roles is not None:
        where += " AND m.role IN (SELECT value FROM json_each(?))"
        params.append(json.dumps(list(roles)))
    if since is not None:
        where += " AND julianday(m.timestamp) >= julianday(?)"
        params.append(since)
    if until is not None:
        where += " AND julianday(m.timestamp) <= julianday(?)"
        params.append(until)
    return where, params


def _trigram_supported(compact: str, match_mode: str) -> bool:
    if match_mode == "phrase":
        return len(compact) >= 3
    terms = [term for term in compact.split(" ") if term]
    return bool(terms) and all(len(term) >= 3 for term in terms)


def _fts_expression(compact: str, match_mode: str) -> str:
    """Build a ``MATCH`` expression over the conversation-text columns only."""
    if match_mode == "phrase":
        expression = '"' + compact.replace('"', '""') + '"'
    else:
        expression = (" OR " if match_mode == "any_term" else " AND ").join(
            '"' + term.replace('"', '""') + '"' for term in compact.split(" ") if term
        )
    return "{content content_search} : (" + expression + ")"


def _fts_candidates(
    connection: sqlite3.Connection,
    fts_table: str,
    expression: str,
    where: str,
    params: Sequence[Any],
    order: SessionSearchOrder,
) -> sqlite3.Cursor:
    """Enumerate eligible FTS hits by rank, or by Message time.

    One full ``MATCH`` scan ranks every hit, so bm25 statistics stay those of
    the whole index; rank order breaks ties by key here and by recency when
    the candidates are checked.
    """
    budget = _store_values._SEARCH_CANDIDATE_LIMIT + 1
    if order == "relevance":
        return connection.execute(
            f"WITH eligible(k, rank) AS MATERIALIZED ("
            f"SELECT rowid, bm25({fts_table}) FROM {fts_table} "
            f"WHERE {fts_table} MATCH ? "
            f"AND +rowid IN (SELECT m.message_key FROM history_records AS m WHERE {where})) "
            "SELECT k, rank FROM eligible ORDER BY rank, k LIMIT ?",
            (expression, *params, budget),
        )
    direction = "ASC" if order == "oldest" else "DESC"
    return connection.execute(
        f"SELECT m.message_key, 0.0 FROM history_records AS m WHERE {where} "
        f"AND m.message_key IN (SELECT rowid FROM {fts_table} WHERE {fts_table} MATCH ?) "
        f"ORDER BY julianday(m.timestamp) {direction}, m.message_key {direction} LIMIT ?",
        (*params, expression, budget),
    )


def _scan_candidates(
    connection: sqlite3.Connection,
    where: str,
    params: Sequence[Any],
    order: SessionSearchOrder,
) -> sqlite3.Cursor:
    """Enumerate eligible records by Message time; relevance scans newest first."""
    direction = "ASC" if order == "oldest" else "DESC"
    return connection.execute(
        f"SELECT m.message_key, 0.0 FROM history_records AS m WHERE {where} "
        f"ORDER BY julianday(m.timestamp) {direction}, m.message_key {direction} LIMIT ?",
        (*params, _store_values._SEARCH_CANDIDATE_LIMIT + 1),
    )


class _Check:
    """Check ordered candidates against the literal query within the budget."""

    def __init__(
        self, connection: sqlite3.Connection, *, matches: Callable[[str], bool], limit: int
    ) -> None:
        from core.recall.canonical import (
            RECALL_TOOL_RESULT_NAMES,
            SESSION_RECALL_CONVERSATION_ROLES,
        )

        self._connection = connection
        self._matches = matches
        self._limit = limit
        self._roles = frozenset(SESSION_RECALL_CONVERSATION_ROLES)
        self._artifact_tools = RECALL_TOOL_RESULT_NAMES

    def run(
        self, cursor: sqlite3.Cursor, *, ranked: bool
    ) -> tuple[builtins.list[SessionSearchHit], bool]:
        """Return the first ``limit`` matches and whether the budget covered them."""
        budget = _store_values._SEARCH_CANDIDATE_LIMIT
        hits: builtins.list[SessionSearchHit] = []
        with closing(cursor):
            for batch in _batches(cursor, first=self._limit, keep_ties=ranked):
                ranks = dict(batch)
                rows = self._rows(ranks)
                keys = _rank_order(ranks, rows) if ranked else builtins.list(ranks)
                for key in keys:
                    if budget == 0:
                        return hits, False
                    budget -= 1
                    row = rows.get(key)
                    if row is None:
                        continue
                    text = self._text(row)
                    if not text or not self._matches(text):
                        continue
                    hits.append(
                        SessionSearchHit(
                            address=_store_values._address(row),
                            message_id=str(row["message_id"]),
                            role=str(row["role"]),
                            timestamp=str(row["timestamp"] or ""),
                            text=text,
                            rank=ranks[key],
                        )
                    )
                    if len(hits) == self._limit:
                        return hits, True
        return hits, True

    def _rows(self, keys: Iterable[int]) -> dict[int, sqlite3.Row]:
        return {
            int(row["message_key"]): row
            for row in self._connection.execute(_PROJECTION_SQL, (json.dumps(list(keys)),))
        }

    def _text(self, row: sqlite3.Row) -> str:
        """Return a record's conversation text, or ``""`` when search ignores it.

        Search reads only conversation roles, never notes or Skill contexts,
        and never the persisted results of earlier Recall searches.
        """
        role = row["role"]
        if role not in self._roles or (role == "tool" and row["tool_name"] in self._artifact_tools):
            return ""
        return str(row["text"] or "")


def _batches(rows: Iterable[Any], *, first: int, keep_ties: bool) -> Iterator[_Batch]:
    """Group ordered ``(key, rank)`` rows; ranked batches never split a rank."""
    size = min(max(first, _MIN_FIRST_BATCH), _CHECK_BATCH)
    batch: _Batch = []
    for key, rank in rows:
        if len(batch) >= size and not (keep_ties and float(rank) == batch[-1][1]):
            yield batch
            batch, size = [], _CHECK_BATCH
        batch.append((int(key), float(rank)))
    if batch:
        yield batch


def _rank_order(ranks: dict[int, float], rows: dict[int, sqlite3.Row]) -> builtins.list[int]:
    """Order keys like ``ORDER BY rank, timestamp DESC, message_key``."""
    stamps = {key: rows[key]["timestamp"] if key in rows else None for key in ranks}
    ordered = sorted(ranks)
    ordered.sort(key=lambda key: (stamps[key] is not None, stamps[key] or ""), reverse=True)
    ordered.sort(key=ranks.__getitem__)
    return ordered
