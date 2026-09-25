"""Exact, bounded Message search within one supplied read transaction.

Search reads current entries only: an entry is eligible while the current view
of some eligible Session contains it. A hit is reported for the Session that
owns the entry when that Session is eligible and still shows it; otherwise for
the newest eligible Session that inherits it. Each entry is one candidate, so a
fork never duplicates its origin's hits.
"""
# ruff: noqa: E501

from __future__ import annotations

import builtins
import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from core.sessions import _store_fts, _store_values
from core.sessions._types import SessionSearchHit, SessionSearchOrder, SessionSearchResult
from core.sessions.schema import FTS_TABLE, FTS_TRIGRAM_TABLE
from core.utils.timestamps import canonical_timestamp

# Candidates are checked in batches whose text is read by key; the first batch
# is sized to the request so a typical page reads only the rows it returns.
_MIN_FIRST_BATCH = 16
_CHECK_BATCH = 256

_Batch = builtins.list[tuple[int, float]]

# The entries current in an eligible Session's view: its own non-superseded
# entries, and the ancestor entries its lineage segments admit.
_OWN_CURRENT = (
    "SELECT e.entry_key, e.created_at FROM entries AS e "
    "WHERE e.session_key IN (SELECT session_key FROM eligible) AND e.superseded_at_seq IS NULL"
)
_INHERITED_CURRENT = (
    "SELECT e.entry_key, e.created_at FROM session_lineage AS l "
    "JOIN entries AS e ON e.session_key = l.ancestor_key "
    "AND e.seq >= l.from_seq AND e.seq < l.upto_seq "
    "WHERE l.session_key IN (SELECT session_key FROM eligible) "
    "AND (e.superseded_at_seq IS NULL OR e.superseded_at_seq >= l.as_of_seq)"
)
# The Session a hit is reported for (see the module docstring).
_REPORTED_SESSION = (
    "COALESCE("
    "CASE WHEN e.superseded_at_seq IS NULL "
    "AND e.session_key IN (SELECT session_key FROM eligible) THEN e.session_key END, "
    "(SELECT l.session_key FROM session_lineage AS l WHERE l.ancestor_key = e.session_key "
    "AND e.seq >= l.from_seq AND e.seq < l.upto_seq "
    "AND (e.superseded_at_seq IS NULL OR e.superseded_at_seq >= l.as_of_seq) "
    "AND l.session_key IN (SELECT session_key FROM eligible) "
    "ORDER BY l.session_key DESC LIMIT 1))"
)


@dataclass(frozen=True)
class _Filter:
    """The eligible Sessions (a ``WITH eligible`` clause) and the entry filter."""

    eligible_sql: str
    eligible_params: tuple[Any, ...]
    entry_sql: str
    entry_params: tuple[Any, ...]

    def current_entries(self) -> tuple[str, list[Any]]:
        """``SELECT entry_key, created_at`` of every eligible current entry, deduplicated."""
        where = f" AND {self.entry_sql}" if self.entry_sql else ""
        return (
            f"{_OWN_CURRENT}{where} UNION {_INHERITED_CURRENT}{where}",
            [*self.entry_params, *self.entry_params],
        )


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
    """Return up to ``limit`` exactly matching current Messages in ``order``.

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

    search_filter = _search_filter(
        project_id=project_id,
        agent_id=agent_id,
        session_id=session_id,
        roles=roles,
        since=since,
        until=until,
        excluded_session_ids=excluded_session_ids,
        include_subagents=include_subagents,
    )
    check = _Check(connection, search_filter, matches=matches, limit=limit)

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
                    connection, table, _fts_expression(compact, match_mode), search_filter, order
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
    hits, complete = check.run(_scan_candidates(connection, search_filter, order), ranked=False)
    return SessionSearchResult(tuple(hits), complete, "scan", fallback_reason)


def _search_filter(
    *,
    project_id: str | None,
    agent_id: str | None,
    session_id: str | None,
    roles: Sequence[str] | None,
    since: str | None,
    until: str | None,
    excluded_session_ids: Sequence[str],
    include_subagents: bool,
) -> _Filter:
    """Return the eligible live Sessions and the entry filter of one search."""
    eligible = (
        "eligible (session_key) AS MATERIALIZED (SELECT s.session_key FROM sessions AS s "
        "WHERE s.state = 'live' AND s.project_id = ? AND "
        + _store_values._recall_visibility_sql(include_subagents=include_subagents)
    )
    eligible_params: list[Any] = [project_id if project_id is not None else ""]
    if agent_id is not None:
        eligible += " AND s.agent_id = ?"
        eligible_params.append(agent_id)
    if session_id is not None:
        eligible += " AND s.session_id = ?"
        eligible_params.append(session_id)
    if excluded_session_ids:
        eligible += " AND s.session_id NOT IN (SELECT value FROM json_each(?))"
        eligible_params.append(_store_values._json_list(excluded_session_ids))
    eligible += ")"
    clauses: list[str] = []
    entry_params: list[Any] = []
    if roles is not None:
        clauses.append("e.role IN (SELECT value FROM json_each(?))")
        entry_params.append(json.dumps(list(roles)))
    # Stored times are canonical, so text order is time order and exact.
    if since is not None:
        clauses.append("e.created_at >= ?")
        entry_params.append(canonical_timestamp(since))
    if until is not None:
        clauses.append("e.created_at <= ?")
        entry_params.append(canonical_timestamp(until))
    return _Filter(eligible, tuple(eligible_params), " AND ".join(clauses), tuple(entry_params))


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
    return "{content search_text} : (" + expression + ")"


def _fts_candidates(
    connection: sqlite3.Connection,
    fts_table: str,
    expression: str,
    search_filter: _Filter,
    order: SessionSearchOrder,
) -> sqlite3.Cursor:
    """Enumerate eligible FTS hits by rank, or by Message time.

    One full ``MATCH`` scan ranks every hit, so bm25 statistics stay those of
    the whole index; rank order breaks ties by key here and by recency when
    the candidates are checked.
    """
    budget = _store_values._SEARCH_CANDIDATE_LIMIT + 1
    current_sql, current_params = search_filter.current_entries()
    if order == "relevance":
        return connection.execute(
            f"WITH {search_filter.eligible_sql}, "
            f"ranked (k, rank) AS MATERIALIZED (SELECT rowid, bm25({fts_table}) FROM {fts_table} "
            f"WHERE {fts_table} MATCH ? AND +rowid IN (SELECT entry_key FROM ({current_sql}))) "
            "SELECT k, rank FROM ranked ORDER BY rank, k LIMIT ?",
            (*search_filter.eligible_params, expression, *current_params, budget),
        )
    direction = "ASC" if order == "oldest" else "DESC"
    return connection.execute(
        f"WITH {search_filter.eligible_sql} "
        f"SELECT c.entry_key, 0.0 FROM ({current_sql}) AS c "
        f"WHERE c.entry_key IN (SELECT rowid FROM {fts_table} WHERE {fts_table} MATCH ?) "
        f"ORDER BY c.created_at {direction}, c.entry_key {direction} LIMIT ?",
        (*search_filter.eligible_params, *current_params, expression, budget),
    )


def _scan_candidates(
    connection: sqlite3.Connection, search_filter: _Filter, order: SessionSearchOrder
) -> sqlite3.Cursor:
    """Enumerate eligible entries by Message time; relevance scans newest first."""
    direction = "ASC" if order == "oldest" else "DESC"
    current_sql, current_params = search_filter.current_entries()
    return connection.execute(
        f"WITH {search_filter.eligible_sql} "
        f"SELECT c.entry_key, 0.0 FROM ({current_sql}) AS c "
        f"ORDER BY c.created_at {direction}, c.entry_key {direction} LIMIT ?",
        (
            *search_filter.eligible_params,
            *current_params,
            _store_values._SEARCH_CANDIDATE_LIMIT + 1,
        ),
    )


class _Check:
    """Check ordered candidates against the literal query within the budget."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        search_filter: _Filter,
        *,
        matches: Callable[[str], bool],
        limit: int,
    ) -> None:
        from core.recall.canonical import (
            RECALL_TOOL_RESULT_NAMES,
            SESSION_RECALL_CONVERSATION_ROLES,
        )

        self._connection = connection
        self._filter = search_filter
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
                            message_id=str(row["entry_id"]),
                            role=str(row["role"]),
                            timestamp=str(row["created_at"]),
                            text=text,
                            rank=ranks[key],
                        )
                    )
                    if len(hits) == self._limit:
                        return hits, True
        return hits, True

    def _rows(self, keys: Iterable[int]) -> dict[int, sqlite3.Row]:
        """Read each candidate's text and the Session its hit is reported for."""
        return {
            int(row["entry_key"]): row
            for row in self._connection.execute(
                f"WITH {self._filter.eligible_sql}, "
                f"candidates (entry_key, reported_key) AS (SELECT e.entry_key, {_REPORTED_SESSION} "
                "FROM entries AS e WHERE e.entry_key IN (SELECT value FROM json_each(?))) "
                "SELECT e.entry_key, e.entry_id, e.role, e.created_at, "
                "COALESCE(t.content, t.search_text) AS text, c.name AS tool_name, "
                "s.project_id, s.agent_id, s.session_id "
                "FROM candidates AS k JOIN entries AS e ON e.entry_key = k.entry_key "
                "JOIN sessions AS s ON s.session_key = k.reported_key "
                "LEFT JOIN entry_text AS t ON t.entry_key = e.entry_key "
                "LEFT JOIN tool_calls AS c ON c.result_entry_key = e.entry_key",
                (*self._filter.eligible_params, _store_values._key_list(list(keys))),
            )
        }

    def _text(self, row: sqlite3.Row) -> str:
        """Return an entry's conversation text, or ``""`` when search ignores it.

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
    """Order keys like ``ORDER BY rank, created_at DESC, entry_key``."""
    stamps = {key: rows[key]["created_at"] if key in rows else None for key in ranks}
    ordered = sorted(ranks)
    ordered.sort(key=lambda key: (stamps[key] is not None, stamps[key] or ""), reverse=True)
    ordered.sort(key=ranks.__getitem__)
    return ordered
