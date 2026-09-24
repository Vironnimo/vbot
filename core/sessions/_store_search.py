"""Bounded FTS and canonical Message search within one supplied snapshot."""
# ruff: noqa: E501

from __future__ import annotations

import builtins
import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from core.sessions import (
    _store_codec,
    _store_fts,
    _store_values,
)
from core.sessions.schema import (
    FTS_TABLE,
    FTS_TRIGRAM_TABLE,
)

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions._types import SessionAddress

_SearchRow = tuple["SessionAddress", str, str, str, float]
_CANONICAL_FETCH_BATCH = 250
_KEYED_RECORDS_SQL = (
    f"SELECT s.project_id, s.agent_id, s.session_id, {_store_values._MESSAGE_RECORD_COLUMNS} "
    f"FROM history_records AS m {_store_values._MESSAGE_RECORD_JOINS} "
    "JOIN sessions AS s ON s.session_key = m.session_key "
    f"WHERE {_store_values._KEYED_RECORDS}"
)


def search(
    connection: sqlite3.Connection,
    query: str,
    *,
    project_id: str | None,
    agent_id: str | None,
    session_id: str | None = None,
    match_mode: str = "all_terms",
    limit: int = _store_values._SEARCH_RESULT_LIMIT,
    roles: Sequence[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    excluded_session_ids: Sequence[str] = (),
    include_subagents: bool = False,
    use_fts: bool = True,
    fallback_reason: str = "fts_unavailable",
) -> Callable[[], builtins.list[_SearchRow]]:
    """Search canonical Messages through FTS or a truthful projection fallback.

    All SQL runs in the caller's read transaction. The returned decoder builds
    the result rows from the selected records and runs after that transaction.
    """
    if not query or not query.strip() or limit <= 0 or (roles is not None and not roles):
        return list
    compact = re.sub(r"\s+", " ", query).strip()
    if not compact:
        return list

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

    terms = [term for term in compact.split(" ") if term]
    if match_mode == "phrase":
        escaped = compact.replace('"', '""')
        expression = f'"{escaped}"'
        trigram_supported = len(compact) >= 3
    else:
        expression = (" OR " if match_mode == "any_term" else " AND ").join(
            f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms
        )
        trigram_supported = bool(terms) and all(len(term) >= 3 for term in terms)

    expression = "{content content_search} : (" + expression + ")"

    if (
        not use_fts
        or not _store_fts._fts_health_from_connection(connection, verify_coverage=False).available
    ):
        return _canonical_rows(
            connection, where, params, matches=matches, limit=limit, fallback_reason=fallback_reason
        )

    hits = _fts_hits(
        connection,
        FTS_TRIGRAM_TABLE
        if trigram_supported and roles is not None and "tool" not in roles
        else FTS_TABLE,
        expression,
        where,
        params,
        limit=limit,
    )
    if not hits and (roles is None or "tool" in roles):
        return _canonical_rows(
            connection,
            where,
            params,
            matches=matches,
            limit=limit,
            fallback_reason="tool_inclusive",
        )
    return lambda: _store_values._FtsSearchRows(
        [
            (
                _store_values._address(row),
                str(row["message_id"]),
                str(row["timestamp"]),
                _store_codec._message_payload(row),
                rank,
            )
            for row, rank in hits
        ],
        source="fts",
    )


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


def _fts_hits(
    connection: sqlite3.Connection,
    fts_table: str,
    expression: str,
    where: str,
    params: Sequence[Any],
    *,
    limit: int,
) -> builtins.list[tuple[sqlite3.Row, float]]:
    """Return the best-ranked eligible FTS records, ordered by rank, recency, and key.

    One full ``MATCH`` scan ranks every hit, so bm25 statistics stay those of the
    whole index. Only the ``limit`` best ranks and their ties leave SQLite; their
    records are read by key.
    """
    ranked = connection.execute(
        f"WITH eligible(k, rank) AS MATERIALIZED ("
        f"SELECT rowid, bm25({fts_table}) FROM {fts_table} "
        f"WHERE {fts_table} MATCH ? "
        f"AND +rowid IN (SELECT m.message_key FROM history_records AS m WHERE {where})) "
        "SELECT k, rank FROM eligible WHERE rank <= COALESCE("
        "(SELECT rank FROM eligible ORDER BY rank LIMIT 1 OFFSET ?), rank)",
        (expression, *params, limit - 1),
    ).fetchall()
    ranks: dict[int, float] = {int(key): float(rank) for key, rank in ranked}
    if len(ranks) > limit:
        stamps = dict(
            connection.execute(
                "SELECT m.message_key, m.timestamp FROM history_records AS m "
                f"WHERE {_store_values._KEYED_RECORDS}",
                (json.dumps(list(ranks)),),
            ).fetchall()
        )
        kept = _rank_order(ranks, stamps)[:limit]
        ranks = {key: ranks[key] for key in kept}
    rows = _addressed_records(connection, list(ranks))
    order = _rank_order(ranks, {key: row["timestamp"] for key, row in rows.items()})
    return [(rows[key], ranks[key]) for key in order]


def _rank_order(ranks: dict[int, float], stamps: dict[int, Any]) -> builtins.list[int]:
    """Order keys like ``ORDER BY rank, timestamp DESC, message_key``."""
    ordered = sorted(ranks)
    ordered.sort(key=lambda key: (stamps[key] is not None, stamps[key] or ""), reverse=True)
    ordered.sort(key=ranks.__getitem__)
    return ordered


def _canonical_rows(
    connection: sqlite3.Connection,
    where: str,
    params: Sequence[Any],
    *,
    matches: Callable[[str], bool],
    limit: int,
    fallback_reason: str,
) -> Callable[[], builtins.list[_SearchRow]]:
    """Match the newest eligible records in Python, bounded by the scan limit.

    Only keys are ordered in SQLite; full records are read in newest-first
    batches and decoding stops once ``limit`` records match.
    """
    scan_limit = _store_values._CANONICAL_SEARCH_SCAN_LIMIT
    keys = [
        int(row[0])
        for row in connection.execute(
            f"SELECT m.message_key FROM history_records AS m WHERE {where} "
            "ORDER BY julianday(m.timestamp) DESC, m.message_key DESC LIMIT ?",
            (*params, scan_limit + 1),
        )
    ]
    complete = len(keys) <= scan_limit
    del keys[scan_limit:]
    matched: builtins.list[tuple[sqlite3.Row, ChatMessage]] = []
    for start in range(0, len(keys), _CANONICAL_FETCH_BATCH):
        batch = keys[start : start + _CANONICAL_FETCH_BATCH]
        rows = _addressed_records(connection, batch)
        for key in batch:
            message = _store_codec.message_from_row(rows[key])
            if matches(_store_fts._search_projection(message)):
                matched.append((rows[key], message))
            if len(matched) >= limit:
                break
        if len(matched) >= limit:
            complete = True
            break
    return lambda: _store_values._FtsSearchRows(
        [
            (
                _store_values._address(row),
                str(row["message_id"]),
                str(row["timestamp"]),
                _store_codec._message_json(message),
                0.0,
            )
            for row, message in matched
        ],
        source="canonical",
        complete=complete,
        fallback_reason=fallback_reason,
    )


def _addressed_records(
    connection: sqlite3.Connection, keys: Sequence[int]
) -> dict[int, sqlite3.Row]:
    if not keys:
        return {}
    return {
        int(row["message_key"]): row
        for row in connection.execute(_KEYED_RECORDS_SQL, (json.dumps(list(keys)),))
    }
