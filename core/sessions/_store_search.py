"""Bounded FTS and canonical Message search within one supplied snapshot."""
# ruff: noqa: E501

from __future__ import annotations

import builtins
import re
import sqlite3
from collections.abc import Sequence
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
    from core.sessions._types import SessionAddress


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
    use_fts: bool = True,
    fallback_reason: str = "fts_unavailable",
) -> builtins.list[tuple[SessionAddress, str, str, str, float]]:
    """Search canonical Messages through FTS or a truthful projection fallback."""
    if not query or not query.strip() or limit <= 0 or (roles is not None and not roles):
        return []
    compact = re.sub(r"\s+", " ", query).strip().casefold()
    if not compact:
        return []

    def matches(text: str) -> bool:
        haystack = re.sub(r"\s+", " ", text).strip().casefold()
        if match_mode == "phrase":
            return compact in haystack
        terms = [term for term in compact.split(" ") if term]
        if match_mode == "any_term":
            return any(term in haystack for term in terms)
        return all(term in haystack for term in terms)

    def canonical_rows(
        connection: sqlite3.Connection,
        *,
        fallback_reason: str,
    ) -> _store_values._FtsSearchRows:
        sql = (
            f"SELECT s.project_id, s.agent_id, s.session_id, {_store_values._MESSAGE_RECORD_COLUMNS} "
            f"FROM messages AS m {_store_values._MESSAGE_RECORD_JOINS} "
            "JOIN sessions AS s ON s.session_key = m.session_key "
            "WHERE s.status = 'live' AND m.active = 1"
        )
        params: list[Any] = []
        if project_id is not None:
            sql += " AND s.project_id = ?"
            params.append(project_id)
        else:
            sql += " AND s.project_id = ''"
        if agent_id is not None:
            sql += " AND s.agent_id = ?"
            params.append(agent_id)
        if session_id is not None:
            sql += " AND s.session_id = ?"
            params.append(session_id)
        if roles is not None:
            placeholders = ", ".join("?" for _ in roles)
            sql += f" AND m.role IN ({placeholders})"
            params.extend(roles)
        if since is not None:
            sql += " AND julianday(m.timestamp) >= julianday(?)"
            params.append(since)
        if until is not None:
            sql += " AND julianday(m.timestamp) <= julianday(?)"
            params.append(until)
        if excluded_session_ids:
            placeholders = ", ".join("?" for _ in excluded_session_ids)
            sql += f" AND s.session_id NOT IN ({placeholders})"
            params.extend(excluded_session_ids)
        sql += " ORDER BY julianday(m.timestamp) DESC, m.message_key DESC LIMIT ?"
        params.append(_store_values._CANONICAL_SEARCH_SCAN_LIMIT + 1)
        result: builtins.list[tuple[SessionAddress, str, str, str, float]] = []
        candidates = connection.execute(sql, params).fetchmany(
            _store_values._CANONICAL_SEARCH_SCAN_LIMIT + 1
        )
        complete = len(candidates) <= _store_values._CANONICAL_SEARCH_SCAN_LIMIT
        for row in candidates[: _store_values._CANONICAL_SEARCH_SCAN_LIMIT]:
            message = _store_codec.message_from_row(row)
            if not matches(_store_fts._search_projection(message)):
                continue
            payload = _store_codec._message_payload(row)
            from core.sessions._types import SessionAddress as SessionAddr

            result.append(
                (
                    SessionAddr(
                        project_id=row["project_id"] or None,
                        agent_id=row["agent_id"],
                        session_id=row["session_id"],
                    ),
                    str(row["message_id"]),
                    str(row["timestamp"]),
                    payload,
                    0.0,
                )
            )
            if len(result) >= limit:
                complete = True
                break
        return _store_values._FtsSearchRows(
            result,
            source="canonical",
            complete=complete,
            fallback_reason=fallback_reason,
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

    if (
        not use_fts
        or not _store_fts._fts_health_from_connection(connection, verify_coverage=False).available
    ):
        return canonical_rows(connection, fallback_reason=fallback_reason)

    def query_fts(
        fts_table: str,
    ) -> _store_values._FtsSearchRows:
        sql = (
            f"SELECT s.project_id, s.agent_id, s.session_id, "
            f"{_store_values._MESSAGE_RECORD_COLUMNS}, bm25({fts_table}) AS rank "
            f"FROM {fts_table} "
            f"JOIN messages AS m ON m.message_key = {fts_table}.rowid "
            f"{_store_values._MESSAGE_RECORD_JOINS} "
            "JOIN sessions AS s ON s.session_key = m.session_key "
            f"WHERE {fts_table} MATCH ? AND s.status = 'live' AND m.active = 1"
        )
        params: list[Any] = [expression]
        if project_id is not None:
            sql += " AND s.project_id = ?"
            params.append(project_id)
        else:
            sql += " AND s.project_id = ''"
        if agent_id is not None:
            sql += " AND s.agent_id = ?"
            params.append(agent_id)
        if session_id is not None:
            sql += " AND s.session_id = ?"
            params.append(session_id)
        if roles is not None:
            placeholders = ", ".join("?" for _ in roles)
            sql += f" AND m.role IN ({placeholders})"
            params.extend(roles)
        if since is not None:
            sql += " AND julianday(m.timestamp) >= julianday(?)"
            params.append(since)
        if until is not None:
            sql += " AND julianday(m.timestamp) <= julianday(?)"
            params.append(until)
        if excluded_session_ids:
            placeholders = ", ".join("?" for _ in excluded_session_ids)
            sql += f" AND s.session_id NOT IN ({placeholders})"
            params.extend(excluded_session_ids)
        sql += " ORDER BY rank, m.timestamp DESC, m.message_key LIMIT ?"
        params.append(limit)
        rows = connection.execute(sql, params).fetchmany(limit)
        found: builtins.list[tuple[SessionAddress, str, str, str, float]] = []
        for row in rows:
            from core.sessions._types import SessionAddress as SessionAddr

            found.append(
                (
                    SessionAddr(
                        project_id=row["project_id"] or None,
                        agent_id=row["agent_id"],
                        session_id=row["session_id"],
                    ),
                    str(row["message_id"]),
                    str(row["timestamp"]),
                    _store_codec._message_payload(row),
                    float(row["rank"]) if row["rank"] is not None else 0.0,
                )
            )
        return _store_values._FtsSearchRows(found, source="fts")

    result = query_fts(FTS_TABLE)
    if not result and trigram_supported and not (roles is not None and "tool" in roles):
        result = query_fts(FTS_TRIGRAM_TABLE)
    if not result and (roles is None or "tool" in roles):
        result = canonical_rows(connection, fallback_reason="tool_inclusive")
    return result
