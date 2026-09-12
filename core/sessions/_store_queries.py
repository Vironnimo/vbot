"""Session catalog, metadata and revision queries in a supplied snapshot."""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from core.chat.errors import ChatSessionError
from core.sessions import _store_codec, _store_values
from core.sessions._types import JsonObject
from core.sessions.errors import SessionNotFoundError

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions._types import SessionAddress


def exists(
    connection: sqlite3.Connection, address: SessionAddress, *, include_archived: bool = False
) -> bool:
    clause = "" if include_archived else " AND status = 'live'"
    return (
        connection.execute(
            "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ?"
            + clause,
            _store_values._scope(address),
        ).fetchone()
        is not None
    )


def state(
    connection: sqlite3.Connection, address: SessionAddress, *, include_archived: bool = False
) -> sqlite3.Row:
    clause = "" if include_archived else " AND status = 'live'"
    row = connection.execute(
        "SELECT * FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ?"
        + clause
        + " ORDER BY status = 'live' DESC, session_key DESC LIMIT 1",
        _store_values._scope(address),
    ).fetchone()
    if row is None:
        raise SessionNotFoundError(f"session does not exist: {address.session_id}")
    return cast(sqlite3.Row, row)


def descriptor_sources(
    connection: sqlite3.Connection, addresses: Sequence[SessionAddress]
) -> dict[SessionAddress, tuple[JsonObject, int, ChatMessage | None]]:
    """Load compact descriptor inputs for many Sessions in set-oriented reads."""
    sources: dict[SessionAddress, tuple[JsonObject, int, ChatMessage | None]] = {}
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
                f"SELECT session_key, message_count, {_store_values._SESSION_LIST_COLUMNS} "
                "FROM sessions WHERE project_id = ? AND agent_id = ? "
                "AND status = 'live' "
                f"AND session_id IN ({placeholders})",
                (project_id, agent_id, *chunk),
            ).fetchall()
            if not states:
                continue
            session_keys = [int(state["session_key"]) for state in states]
            key_placeholders = ", ".join("?" for _ in session_keys)
            first_user_rows = connection.execute(
                _store_values._message_records_sql(
                    where=(
                        f"m.session_key IN ({key_placeholders}) AND m.role = 'user' "
                        "AND m.seq = (SELECT MIN(first.seq) FROM messages AS first "
                        "WHERE first.session_key = m.session_key AND first.role = 'user')"
                    ),
                    order_by="ORDER BY m.session_key",
                ),
                session_keys,
            ).fetchall()
            first_users = {
                int(row["session_key"]): _store_codec.message_from_row(row)
                for row in first_user_rows
            }
            for state in states:
                address = _store_values._address(state)
                sources[address] = (
                    _store_values._session_projected_metadata_from_state(state),
                    int(state["message_count"]),
                    first_users.get(int(state["session_key"])),
                )
    return sources


def list_addresses(
    connection: sqlite3.Connection,
    *,
    project_id: str | None = None,
    agent_id: str | None = None,
    include_all_scopes: bool = False,
) -> list[SessionAddress]:
    clauses = ["status = 'live'"]
    params: list[str] = []
    if not include_all_scopes:
        clauses.append("project_id = ?")
        params.append(project_id or "")
    if agent_id is not None:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    rows = connection.execute(
        "SELECT project_id, agent_id, session_id FROM sessions WHERE "
        + " AND ".join(clauses)
        + " ORDER BY session_id",
        params,
    ).fetchall()
    return [_store_values._address(row) for row in rows]


def list_state_rows(
    connection: sqlite3.Connection, project_id: str | None, agent_id: str
) -> list[sqlite3.Row]:
    rows = connection.execute(
        "SELECT * FROM sessions WHERE status = 'live' AND project_id = ? AND agent_id = ? ORDER BY session_id",
        (project_id or "", agent_id),
    ).fetchall()
    return cast(list[sqlite3.Row], rows)


def list_summary_rows_for_scope(
    connection: sqlite3.Connection,
    project_id: str | None,
    agent_id: str,
    *,
    metadata_keys: Sequence[str] = (),
) -> list[sqlite3.Row]:
    selected_keys = tuple(dict.fromkeys(metadata_keys))
    unknown = set(selected_keys) - _store_values._SUMMARY_METADATA_COLUMNS.keys()
    if unknown:
        raise ChatSessionError(
            f"unsupported Session summary metadata: {', '.join(sorted(unknown))}"
        )
    metadata_columns = "".join(
        f", json_extract(metadata_json, '{_store_values._SUMMARY_METADATA_COLUMNS[key]}') "
        f"AS metadata_{key}_json"
        for key in selected_keys
    )
    rows = connection.execute(
        f"SELECT {_store_values._SESSION_LIST_COLUMNS}{metadata_columns} FROM sessions "
        "WHERE status = 'live' AND project_id = ? AND agent_id = ? "
        "ORDER BY active_sort DESC, session_id",
        (project_id or "", agent_id),
    ).fetchall()
    return cast(list[sqlite3.Row], rows)


def list_recall_summary_rows(
    connection: sqlite3.Connection,
    project_id: str | None,
    agent_id: str,
    *,
    include_subagents: bool,
    excluded_session_id: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int | None,
) -> list[sqlite3.Row]:
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
        raise ChatSessionError("Recall Session limit must be a positive integer")
    valid_kinds = (
        f"((list_visibility_mask & {_store_values._LIST_VISIBILITY_VALID_RUN_KINDS}) != 0)"
    )
    reflection = f"((list_visibility_mask & {_store_values._LIST_VISIBILITY_REFLECTION}) != 0)"
    subagent = (
        "((list_visibility_mask & "
        f"{_store_values._LIST_VISIBILITY_SUBAGENT_SESSION | _store_values._LIST_VISIBILITY_SUBAGENT_RUN_KIND}) != 0)"
    )
    user_facing = f"((list_visibility_mask & {_store_values._LIST_VISIBILITY_USER_FACING}) != 0)"
    where = [
        "status = 'live'",
        "project_id = ?",
        "agent_id = ?",
        f"NOT {reflection}",
        f"(({subagent} AND ? = 1) OR (NOT {subagent} AND (NOT {valid_kinds} OR {user_facing})))",
    ]
    params: list[Any] = [
        project_id or "",
        agent_id,
        int(include_subagents),
    ]
    if excluded_session_id is not None:
        where.append("session_id <> ?")
        params.append(excluded_session_id)
    if since is not None or until is not None:
        period_placeholders = ", ".join("?" for _ in _store_values._RECALL_PERIOD_ROLES)
        period = [
            "candidate.session_key = sessions.session_key",
            f"candidate.role IN ({period_placeholders})",
        ]
        params.extend(_store_values._RECALL_PERIOD_ROLES)
        if since is not None:
            period.append("julianday(candidate.timestamp) >= julianday(?)")
            params.append(since.isoformat())
        if until is not None:
            period.append("julianday(candidate.timestamp) <= julianday(?)")
            params.append(until.isoformat())
        where.append(
            "EXISTS (SELECT 1 FROM messages AS candidate WHERE " + " AND ".join(period) + ")"
        )
    sql = (
        f"SELECT {_store_values._SESSION_LIST_COLUMNS} FROM sessions WHERE "
        + " AND ".join(where)
        + " ORDER BY active_sort DESC, session_id"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = connection.execute(sql, params).fetchall()
    return cast(list[sqlite3.Row], rows)


def list_summary_rows(
    connection: sqlite3.Connection,
    scopes: Sequence[tuple[str | None, str]],
    *,
    limit: int,
    cursor: tuple[float, str, str, str] | None,
    include_subagents: bool,
    include_memory_reflections: bool,
    include_skill_reflections: bool,
    include_cron: bool,
    required_address: SessionAddress | None,
) -> tuple[list[sqlite3.Row], sqlite3.Row | None, int, bool]:
    """Read one bounded, globally ordered Session-list page from normalized columns."""
    normalized_scopes = tuple(
        dict.fromkeys((project_id or "", agent_id) for project_id, agent_id in scopes)
    )
    if not normalized_scopes:
        return [], None, 0, False
    scope_sql = (
        "(" + " OR ".join("(project_id = ? AND agent_id = ?)" for _scope in normalized_scopes) + ")"
    )
    scope_params = [value for scope in normalized_scopes for value in scope]
    visibility_sql, visibility_params = _store_values._session_list_visibility_sql(
        include_subagents=include_subagents,
        include_memory_reflections=include_memory_reflections,
        include_skill_reflections=include_skill_reflections,
        include_cron=include_cron,
    )
    base_where = f"status = 'live' AND {scope_sql}"
    page_where = ""
    page_params: list[Any] = []
    if cursor is not None:
        active_sort, project_id, agent_id, session_id = cursor
        page_where = (
            "WHERE active_sort < ? OR (active_sort = ? AND "
            "(project_id, agent_id, session_id) > (?, ?, ?))"
        )
        page_params.extend((active_sort, active_sort, project_id, agent_id, session_id))
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
    return cast(list[sqlite3.Row], rows), required_row, total, has_more


def list_activity_rows(
    connection: sqlite3.Connection, project_id: str | None, agent_id: str
) -> list[sqlite3.Row]:
    rows = connection.execute(
        "SELECT session_id, latest_completion_run_id, latest_completion_status, "
        "latest_completion_at, read_completion_run_id FROM sessions "
        "WHERE status = 'live' AND project_id = ? AND agent_id = ? ORDER BY session_id",
        (project_id or "", agent_id),
    ).fetchall()
    return cast(list[sqlite3.Row], rows)


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
    clauses = [
        "s.status = 'live'",
        "s.project_id = ?",
        "s.agent_id = ?",
        f"m.role IN ({','.join('?' for _ in role_values)})",
    ]
    params: list[str] = [project_id or "", agent_id, *role_values]
    if since is not None:
        clauses.append("julianday(m.timestamp) >= julianday(?)")
        params.append(since.isoformat())
    if until is not None:
        clauses.append("julianday(m.timestamp) <= julianday(?)")
        params.append(until.isoformat())
    rows = connection.execute(
        "SELECT DISTINCT s.session_id FROM sessions AS s "
        "JOIN messages AS m ON m.session_key = s.session_key WHERE " + " AND ".join(clauses),
        params,
    ).fetchall()
    return {str(row["session_id"]) for row in rows}


def list_history_revisions(
    connection: sqlite3.Connection, project_id: str | None, agent_id: str
) -> list[tuple[SessionAddress, str, int]]:
    rows = connection.execute(
        "SELECT project_id, agent_id, session_id, generation_id, history_revision FROM sessions WHERE status = 'live' AND project_id = ? AND agent_id = ? ORDER BY session_id",
        (project_id or "", agent_id),
    ).fetchall()
    return [
        (_store_values._address(row), str(row["generation_id"]), int(row["history_revision"]))
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
