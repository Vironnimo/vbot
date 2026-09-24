"""Session catalog, metadata and revision queries in a supplied snapshot."""
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from core.chat.errors import ChatSessionError
from core.sessions import _store_codec, _store_values
from core.sessions._metadata import (
    _completion_activity_from_state,
    _session_list_summary_from_state,
)
from core.sessions._types import (
    JsonObject,
    SessionHistoryRevision,
    SessionListCursor,
    SessionListPage,
)
from core.sessions.errors import SessionNotFoundError

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions._types import SessionAddress, SessionListFilters, SessionRecallVisibility


def exists(connection: sqlite3.Connection, address: SessionAddress) -> bool:
    """Probe the live address index for one Session."""
    return (
        connection.execute(
            "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? "
            "AND status = 'live'",
            _store_values._scope(address),
        ).fetchone()
        is not None
    )


# Three bound values per address keep one batch well below SQLite's variable limit.
_EXISTING_ADDRESS_BATCH_SIZE = 300


def existing_addresses(
    connection: sqlite3.Connection, addresses: Sequence[SessionAddress]
) -> set[SessionAddress]:
    """Return the live subset of *addresses*, one indexed statement per batch."""
    wanted = list(dict.fromkeys(addresses))
    found: set[SessionAddress] = set()
    for start in range(0, len(wanted), _EXISTING_ADDRESS_BATCH_SIZE):
        batch = wanted[start : start + _EXISTING_ADDRESS_BATCH_SIZE]
        rows = connection.execute(
            "SELECT project_id, agent_id, session_id FROM sessions WHERE status = 'live' "
            "AND (project_id, agent_id, session_id) IN (VALUES "
            + ", ".join("(?, ?, ?)" for _ in batch)
            + ")",
            [value for address in batch for value in _store_values._scope(address)],
        ).fetchall()
        found.update(_store_values._address(row) for row in rows)
    return found


def descriptor_sources(
    connection: sqlite3.Connection, addresses: Sequence[SessionAddress]
) -> Callable[
    [], dict[SessionAddress, tuple[JsonObject, int, ChatMessage | None, SessionRecallVisibility]]
]:
    """Load compact descriptor inputs for many Sessions in set-oriented reads.

    The returned decoder projects metadata and first User Messages after the
    read transaction. Each source carries the Session's Recall visibility.
    """
    selected: list[tuple[sqlite3.Row, sqlite3.Row | None]] = []
    by_scope: dict[tuple[str, str], list[str]] = {}
    for address in addresses:
        by_scope.setdefault((address.project_id or "", address.agent_id), []).append(
            address.session_id
        )
    for (project_id, agent_id), session_ids in by_scope.items():
        for start in range(0, len(session_ids), _store_values._DESCRIPTOR_SOURCE_BATCH_SIZE):
            chunk = session_ids[start : start + _store_values._DESCRIPTOR_SOURCE_BATCH_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            states = connection.execute(
                f"SELECT session_key, message_count, {_store_values._SESSION_LIST_COLUMNS}, "
                f"{_store_values._RECALL_VISIBILITY_SQL} AS recall_visibility "
                "FROM sessions WHERE project_id = ? AND agent_id = ? "
                "AND status = 'live' "
                f"AND session_id IN ({placeholders})",
                (project_id, agent_id, *chunk),
            ).fetchall()
            if not states:
                continue
            session_keys = [int(state["session_key"]) for state in states]
            key_placeholders = ", ".join("?" for _ in session_keys)
            # User records exist only in the messages branch of history_records,
            # so each first User Message key comes from one index probe.
            first_user_rows = connection.execute(
                _store_values._message_records_sql(
                    where=(
                        "m.message_key IN (SELECT (SELECT u.message_key FROM messages AS u "
                        "WHERE u.session_key = s.session_key AND u.role = 'user' "
                        "ORDER BY u.seq LIMIT 1) FROM sessions AS s "
                        f"WHERE s.session_key IN ({key_placeholders}))"
                    ),
                    order_by="ORDER BY m.session_key",
                ),
                session_keys,
            ).fetchall()
            first_users = {int(row["session_key"]): row for row in first_user_rows}
            selected.extend((state, first_users.get(int(state["session_key"]))) for state in states)
    return lambda: {
        _store_values._address(state): (
            _store_values._session_projected_metadata_from_state(state),
            int(state["message_count"]),
            None if first_user is None else _store_codec.message_from_row(first_user),
            cast("SessionRecallVisibility", str(state["recall_visibility"])),
        )
        for state, first_user in selected
    }


def _live_scope_filter(
    *,
    project_id: str | None,
    agent_id: str | None,
    include_all_scopes: bool,
    exclude_owner_managed: bool,
) -> tuple[str, list[str]]:
    clauses = ["status = 'live'"]
    params: list[str] = []
    if not include_all_scopes:
        clauses.append("project_id = ?")
        params.append(project_id or "")
    if agent_id is not None:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if exclude_owner_managed:
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM temporary_session_bindings AS owner_binding "
            "WHERE owner_binding.session_key = sessions.session_key)"
        )
    return " AND ".join(clauses), params


def list_addresses(
    connection: sqlite3.Connection,
    *,
    project_id: str | None = None,
    agent_id: str | None = None,
    include_all_scopes: bool = False,
    exclude_owner_managed: bool = False,
) -> list[SessionAddress]:
    where, params = _live_scope_filter(
        project_id=project_id,
        agent_id=agent_id,
        include_all_scopes=include_all_scopes,
        exclude_owner_managed=exclude_owner_managed,
    )
    rows = connection.execute(
        f"SELECT project_id, agent_id, session_id FROM sessions WHERE {where} ORDER BY session_id",
        params,
    ).fetchall()
    return [_store_values._address(row) for row in rows]


def list_agent_ids(
    connection: sqlite3.Connection, project_id: str | None, *, exclude_owner_managed: bool
) -> list[str]:
    """Return each Agent id owning a live Session in one scope, sorted."""
    where, params = _live_scope_filter(
        project_id=project_id,
        agent_id=None,
        include_all_scopes=False,
        exclude_owner_managed=exclude_owner_managed,
    )
    rows = connection.execute(
        f"SELECT DISTINCT agent_id FROM sessions WHERE {where} ORDER BY agent_id",
        params,
    ).fetchall()
    return [str(row["agent_id"]) for row in rows]


def metadata_value(connection: sqlite3.Connection, address: SessionAddress, key: str) -> Any:
    """Read one live Session metadata value without decoding the complete metadata.

    A projected key reads its dedicated column; any other key (or a projected key
    whose value did not fit its column) reads only its member of the open-ended
    metadata JSON. A missing key reads as ``None``.
    """
    if not key.isidentifier():
        raise ValueError(f"unsupported Session metadata key: {key!r}")
    column = _store_values._PROJECTED_METADATA_COLUMNS.get(key, "NULL")
    row = connection.execute(
        f"SELECT {column}, json_quote(json_extract(metadata_json, ?)) FROM sessions "
        "WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'live'",
        (f"$.{key}", *_store_values._scope(address)),
    ).fetchone()
    if row is None:
        raise SessionNotFoundError(f"session does not exist: {address.session_id}")
    if row[0] is not None:
        return _store_values._projected_metadata_value(key, row[0])
    return json.loads(row[1])


def _summary_metadata_columns(metadata_keys: Sequence[str]) -> tuple[tuple[str, ...], str]:
    """Validate requested summary metadata and select each key as ``metadata_<key>_json``."""
    selected_keys = tuple(dict.fromkeys(metadata_keys))
    unknown = set(selected_keys) - _store_values._SUMMARY_METADATA_COLUMNS.keys()
    if unknown:
        raise ChatSessionError(
            f"unsupported Session summary metadata: {', '.join(sorted(unknown))}"
        )
    return selected_keys, "".join(
        f", json_extract(metadata_json, '{_store_values._SUMMARY_METADATA_COLUMNS[key]}') "
        f"AS metadata_{key}_json"
        for key in selected_keys
    )


def _summary(state: sqlite3.Row, metadata_keys: Sequence[str] = ()) -> JsonObject:
    """Decode one Session-list row, adding each selected metadata value that is set."""
    summary = _session_list_summary_from_state(state)
    for key in metadata_keys:
        payload = state[f"metadata_{key}_json"]
        if payload is not None:
            summary[key] = json.loads(str(payload))
    return summary


def list_summaries(
    connection: sqlite3.Connection,
    project_id: str | None,
    agent_id: str,
    *,
    metadata_keys: Sequence[str] = (),
) -> Callable[[], list[JsonObject]]:
    """Select one scope's live Session-list rows, most recently active first.

    The returned decoder builds the summaries after the read transaction.
    """
    selected_keys, metadata_columns = _summary_metadata_columns(metadata_keys)
    rows = connection.execute(
        f"SELECT {_store_values._SESSION_LIST_COLUMNS}{metadata_columns} FROM sessions "
        "WHERE status = 'live' AND project_id = ? AND agent_id = ? "
        "ORDER BY active_sort DESC, session_id",
        (project_id or "", agent_id),
    ).fetchall()
    return lambda: [_summary(row, selected_keys) for row in rows]


def list_summaries_page(
    connection: sqlite3.Connection,
    scopes: Sequence[tuple[str | None, str]],
    *,
    limit: int,
    cursor: SessionListCursor | None,
    filters: SessionListFilters,
    required_address: SessionAddress | None,
) -> Callable[[], SessionListPage]:
    """Read one bounded, globally ordered Session-list page from normalized columns.

    A live ``required_address`` within ``scopes`` is appended when the page lacks
    it; if the filters hide it, it also counts toward the total. The returned
    decoder builds the page after the read transaction.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ChatSessionError("Session list limit must be a positive integer")
    normalized_scopes = tuple(
        dict.fromkeys((project_id or "", agent_id) for project_id, agent_id in scopes)
    )
    if not normalized_scopes:
        return lambda: SessionListPage(sessions=(), next_cursor=None, total_count=0)
    scope_sql = (
        "(" + " OR ".join("(project_id = ? AND agent_id = ?)" for _scope in normalized_scopes) + ")"
    )
    scope_params = [value for scope in normalized_scopes for value in scope]
    visibility_sql, visibility_params = _store_values._session_list_visibility_sql(
        include_subagents=filters.include_subagents,
        include_memory_reflections=filters.include_memory_reflections,
        include_skill_reflections=filters.include_skill_reflections,
        include_cron=filters.include_cron,
        include_channels=filters.include_channels,
    )
    base_where = f"status = 'live' AND {scope_sql}"
    page_where = ""
    page_params: list[Any] = []
    if cursor is not None:
        page_where = (
            "WHERE active_sort < ? OR (active_sort = ? AND "
            "(project_id, agent_id, session_id) > (?, ?, ?))"
        )
        page_params.extend(
            (
                cursor.active_sort,
                cursor.active_sort,
                cursor.project_id or "",
                cursor.agent_id,
                cursor.session_id,
            )
        )
    total = int(
        connection.execute(
            f"SELECT COUNT(*) FROM sessions WHERE {base_where} AND {visibility_sql}",
            (*scope_params, *visibility_params),
        ).fetchone()[0]
    )
    fetched = connection.execute(
        f"WITH candidates AS (SELECT {_store_values._SESSION_LIST_COLUMNS} FROM sessions "
        f"WHERE {base_where} AND {visibility_sql}) "
        f"SELECT * FROM candidates {page_where} "
        "ORDER BY active_sort DESC, project_id, agent_id, session_id LIMIT ?",
        (*scope_params, *visibility_params, *page_params, limit + 1),
    ).fetchall()
    has_more = len(fetched) > limit
    rows = fetched[:limit]
    required_row: sqlite3.Row | None = None
    if required_address is not None:
        required_scope = _store_values._scope(required_address)
        if (required_scope[0], required_scope[1]) in normalized_scopes:
            required_row = connection.execute(
                f"SELECT {_store_values._SESSION_LIST_COLUMNS}, "
                f"CASE WHEN {visibility_sql} THEN 1 ELSE 0 END AS list_visible "
                "FROM sessions WHERE status = 'live' AND project_id = ? "
                "AND agent_id = ? AND session_id = ?",
                (*visibility_params, *required_scope),
            ).fetchone()
    if required_row is not None and not bool(required_row["list_visible"]):
        total += 1
    return lambda: _summaries_page(rows, required_row, total, has_more=has_more)


def _summaries_page(
    rows: Sequence[sqlite3.Row],
    required_row: sqlite3.Row | None,
    total_count: int,
    *,
    has_more: bool,
) -> SessionListPage:
    summaries = [_summary(row) for row in rows]
    if required_row is not None and _store_values._address(required_row) not in {
        _store_values._address(row) for row in rows
    }:
        summaries.append(_summary(required_row))
    last = rows[-1] if has_more and rows else None
    return SessionListPage(
        sessions=tuple(summaries),
        next_cursor=None
        if last is None
        else SessionListCursor(
            active_sort=float(last["active_sort"]),
            project_id=str(last["project_id"]) or None,
            agent_id=str(last["agent_id"]),
            session_id=str(last["session_id"]),
        ),
        total_count=total_count,
    )


def summary(
    connection: sqlite3.Connection, address: SessionAddress
) -> Callable[[], JsonObject | None]:
    """Select one live Session's list columns by its exact address; absent is ``None``."""
    row = connection.execute(
        f"SELECT {_store_values._SESSION_LIST_COLUMNS} FROM sessions "
        "WHERE status = 'live' AND project_id = ? AND agent_id = ? AND session_id = ?",
        _store_values._scope(address),
    ).fetchone()
    return lambda: None if row is None else _summary(row)


# Two bound values per scope; the chunk stays far below SQLite's variable limit.
_COMPLETION_ACTIVITY_SCOPE_BATCH_SIZE = 400


def list_completion_activity(
    connection: sqlite3.Connection, scopes: Sequence[tuple[str | None, str]]
) -> dict[tuple[str | None, str], list[JsonObject]]:
    """Map every ``(project_id, agent_id)`` scope to its live Sessions with a completion.

    Sessions without a latest completion are omitted. The scopes join the live
    address index, so each scope is one index search in the caller's snapshot.
    """
    result: dict[tuple[str | None, str], list[JsonObject]] = {
        (project_id or None, agent_id): [] for project_id, agent_id in scopes
    }
    normalized = tuple((project_id or "", agent_id) for project_id, agent_id in result)
    for start in range(0, len(normalized), _COMPLETION_ACTIVITY_SCOPE_BATCH_SIZE):
        chunk = normalized[start : start + _COMPLETION_ACTIVITY_SCOPE_BATCH_SIZE]
        values = ", ".join("(?, ?)" for _scope in chunk)
        for state in connection.execute(
            f"WITH scopes(project_id, agent_id) AS (VALUES {values}) "
            "SELECT s.project_id, s.agent_id, s.session_id, s.latest_completion_run_id, "
            "s.latest_completion_status, s.latest_completion_at, s.read_completion_run_id "
            "FROM scopes JOIN sessions AS s "
            "ON s.project_id = scopes.project_id AND s.agent_id = scopes.agent_id "
            "WHERE s.status = 'live' AND s.latest_completion_run_id IS NOT NULL "
            "ORDER BY s.project_id, s.agent_id, s.session_id",
            [value for scope in chunk for value in scope],
        ).fetchall():
            result[(state["project_id"] or None, state["agent_id"])].append(
                {"id": state["session_id"], **_completion_activity_from_state(state)}
            )
    return result


def session_ids_with_messages(
    connection: sqlite3.Connection,
    project_id: str | None,
    agent_id: str,
    roles: Sequence[str],
    since: datetime | None,
    until: datetime | None,
) -> set[str]:
    """Select Sessions containing a matching Message without loading histories."""
    role_values = tuple(dict.fromkeys(roles))
    if not role_values:
        return set()
    # Scope history_records by a subquery rather than a join so SQLite pushes it
    # into every view branch. The JSON role list keeps a single role from
    # becoming an equality that invites an automatic index on the view rows.
    clauses = [
        "m.session_key IN (SELECT session_key FROM sessions "
        "WHERE status = 'live' AND project_id = ? AND agent_id = ?)",
        "m.role IN (SELECT value FROM json_each(?))",
    ]
    params: list[str] = [project_id or "", agent_id, json.dumps(role_values)]
    if since is not None:
        clauses.append("julianday(m.timestamp) >= julianday(?)")
        params.append(since.isoformat())
    if until is not None:
        clauses.append("julianday(m.timestamp) <= julianday(?)")
        params.append(until.isoformat())
    rows = connection.execute(
        "SELECT session_id FROM sessions WHERE session_key IN "
        "(SELECT m.session_key FROM history_records AS m WHERE " + " AND ".join(clauses) + ")",
        params,
    ).fetchall()
    return {str(row["session_id"]) for row in rows}


def list_history_revisions(
    connection: sqlite3.Connection, project_id: str | None, agent_id: str
) -> list[SessionHistoryRevision]:
    """Return every live Session version of one scope with its Recall visibility."""
    rows = connection.execute(
        "SELECT project_id, agent_id, session_id, generation_id, history_revision, "
        f"{_store_values._RECALL_VISIBILITY_SQL} AS recall_visibility "
        "FROM sessions WHERE status = 'live' AND project_id = ? AND agent_id = ? ORDER BY session_id",
        (project_id or "", agent_id),
    ).fetchall()
    return [
        SessionHistoryRevision(
            _store_values._address(row),
            str(row["generation_id"]),
            int(row["history_revision"]),
            cast("SessionRecallVisibility", str(row["recall_visibility"])),
        )
        for row in rows
    ]


def list_history_versions(
    connection: sqlite3.Connection, addresses: Sequence[SessionAddress]
) -> dict[SessionAddress, tuple[str, int]]:
    """Return the generation id and history revision for many addresses at once.

    Addresses without a live row are absent from the result. Derived
    projections (Statistics, Recall indexes) refresh their freshness
    stamps with this one query instead of one query per Session.
    """
    versions: dict[SessionAddress, tuple[str, int]] = {}
    # One query per distinct scope: the scope columns are the leading
    # partial-index columns, so each query is an index scan over that
    # scope and no IN-list size limits come into play.
    by_scope: dict[tuple[str, str], list[str]] = {}
    for address in addresses:
        by_scope.setdefault((address.project_id or "", address.agent_id), []).append(
            address.session_id
        )
    # SQLite variable limit is 999; chunk per scope to stay well under it and
    # retain one read snapshot across all chunks.
    chunk_size = 900
    for (project_id, agent_id), session_ids in by_scope.items():
        for start in range(0, len(session_ids), chunk_size):
            chunk = session_ids[start : start + chunk_size]
            placeholders = ", ".join("?" for _ in chunk)
            rows = connection.execute(
                "SELECT project_id, agent_id, session_id, generation_id, history_revision "
                "FROM sessions WHERE project_id = ? AND agent_id = ? AND status = 'live' "
                f"AND session_id IN ({placeholders})",
                (project_id, agent_id, *chunk),
            ).fetchall()
            for row in rows:
                address = _store_values._address(row)
                versions[address] = (
                    str(row["generation_id"]),
                    int(row["history_revision"]),
                )
    return versions
