"""Session catalog, list summaries, metadata and revision queries in a supplied snapshot."""
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from itertools import batched
from typing import TYPE_CHECKING, Any, cast

from core.chat.errors import ChatSessionError
from core.sessions import _store_values
from core.sessions._metadata import _completion_activity_from_state
from core.sessions._types import (
    JsonObject,
    SessionHistoryRevision,
    SessionListCursor,
    SessionListPage,
)
from core.sessions.errors import SessionNotFoundError

if TYPE_CHECKING:
    from core.sessions._types import SessionAddress, SessionListFilters, SessionRecallVisibility

_LIST_FROM = f"FROM sessions AS s {_store_values._DERIVED_METADATA_JOIN}"


def exists(connection: sqlite3.Connection, address: SessionAddress) -> bool:
    """Probe the live address index for one Session."""
    return (
        connection.execute(
            "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? "
            "AND state = 'live'",
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
    for batch in batched(wanted, _EXISTING_ADDRESS_BATCH_SIZE, strict=False):
        rows = connection.execute(
            "SELECT project_id, agent_id, session_id FROM sessions WHERE state = 'live' "
            "AND (project_id, agent_id, session_id) IN (VALUES "
            + ", ".join("(?, ?, ?)" for _ in batch)
            + ")",
            [value for address in batch for value in _store_values._scope(address)],
        ).fetchall()
        found.update(_store_values._address(row) for row in rows)
    return found


def subagent_children(
    connection: sqlite3.Connection, parent: SessionAddress
) -> list[SessionAddress]:
    """Return the live Sub-Agent Sessions whose Parent link names *parent*, oldest first."""
    rows = connection.execute(
        "SELECT project_id, agent_id, session_id FROM sessions WHERE state = 'live' "
        "AND subagent_parent_agent_id = ? AND subagent_parent_session_id = ? "
        "AND subagent_parent_project_id IS ? ORDER BY created_at, session_key",
        (parent.agent_id, parent.session_id, parent.project_id),
    ).fetchall()
    return [_store_values._address(row) for row in rows]


def subagent_session(connection: sqlite3.Connection, subagent_id: str) -> SessionAddress | None:
    """Return the live Sub-Agent Session whose Parent link carries *subagent_id*."""
    row = connection.execute(
        "SELECT project_id, agent_id, session_id FROM sessions WHERE state = 'live' "
        "AND subagent_parent_id = ? ORDER BY session_key DESC LIMIT 1",
        (subagent_id,),
    ).fetchone()
    return _store_values._address(row) if row is not None else None


def _by_scope(addresses: Sequence[SessionAddress]) -> dict[tuple[str, str], list[str]]:
    by_scope: dict[tuple[str, str], list[str]] = {}
    for address in addresses:
        by_scope.setdefault((address.project_id or "", address.agent_id), []).append(
            address.session_id
        )
    return by_scope


def descriptor_sources(
    connection: sqlite3.Connection, addresses: Sequence[SessionAddress]
) -> Callable[[], dict[SessionAddress, tuple[JsonObject, SessionRecallVisibility]]]:
    """Load each live Session's metadata and Recall visibility in set-oriented reads."""
    selected: list[sqlite3.Row] = []
    for (project_id, agent_id), session_ids in _by_scope(addresses).items():
        for chunk in batched(
            session_ids, _store_values._DESCRIPTOR_SOURCE_BATCH_SIZE, strict=False
        ):
            selected.extend(
                connection.execute(
                    f"SELECT {_store_values._SESSION_STATE_COLUMNS}, "
                    f"{_store_values._DERIVED_METADATA_COLUMNS}, "
                    f"{_store_values._RECALL_VISIBILITY_SQL} AS recall_visibility "
                    f"{_LIST_FROM} WHERE s.project_id = ? AND s.agent_id = ? "
                    "AND s.state = 'live' AND s.session_id IN (SELECT value FROM json_each(?))",
                    (project_id, agent_id, _store_values._json_list(chunk)),
                )
            )
    return lambda: {
        _store_values._address(row): (
            _store_values._session_metadata_from_state(row),
            cast("SessionRecallVisibility", str(row["recall_visibility"])),
        )
        for row in selected
    }


def _live_scope_filter(
    *,
    project_id: str | None,
    agent_id: str | None,
    include_all_scopes: bool,
    exclude_owner_managed: bool,
) -> tuple[str, list[str]]:
    clauses = ["s.state = 'live'"]
    params: list[str] = []
    if not include_all_scopes:
        clauses.append("s.project_id = ?")
        params.append(project_id or "")
    if agent_id is not None:
        clauses.append("s.agent_id = ?")
        params.append(agent_id)
    if exclude_owner_managed:
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM temporary_session_bindings AS owner_binding "
            "WHERE owner_binding.session_key = s.session_key)"
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
        "SELECT s.project_id, s.agent_id, s.session_id FROM sessions AS s "
        f"WHERE {where} ORDER BY s.session_id",
        params,
    ).fetchall()
    return [_store_values._address(row) for row in rows]


def list_live_scopes(connection: sqlite3.Connection) -> list[tuple[str | None, str]]:
    """Return every ``(project_id, agent_id)`` scope with a live Session, sorted."""
    rows = connection.execute(
        "SELECT DISTINCT s.project_id, s.agent_id FROM sessions AS s "
        "WHERE s.state = 'live' ORDER BY s.project_id, s.agent_id"
    ).fetchall()
    return [(str(row["project_id"]) or None, str(row["agent_id"])) for row in rows]


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
        f"SELECT DISTINCT s.agent_id FROM sessions AS s WHERE {where} ORDER BY s.agent_id",
        params,
    ).fetchall()
    return [str(row["agent_id"]) for row in rows]


def metadata_value(connection: sqlite3.Connection, address: SessionAddress, key: str) -> Any:
    """Read one live Session's metadata facade value; a missing key reads as ``None``."""
    row = _store_values._find_live_metadata_row(connection, address)
    if row is None:
        raise SessionNotFoundError(f"session does not exist: {address.session_id}")
    return _store_values._session_metadata_from_state(row).get(key)


def _summary_metadata_columns(metadata_keys: Sequence[str]) -> tuple[tuple[str, ...], str]:
    """Validate requested summary values and select each key as ``metadata_<key>_json``."""
    selected_keys = tuple(dict.fromkeys(metadata_keys))
    unknown = set(selected_keys) - _store_values._SUMMARY_METADATA_COLUMNS.keys()
    if unknown:
        raise ChatSessionError(
            f"unsupported Session summary metadata: {', '.join(sorted(unknown))}"
        )
    return selected_keys, "".join(
        f", {_store_values._SUMMARY_METADATA_COLUMNS[key]} AS metadata_{key}_json"
        for key in selected_keys
    )


def _summary(state: sqlite3.Row, metadata_keys: Sequence[str] = ()) -> JsonObject:
    """Decode one ``_SESSION_LIST_COLUMNS`` row into its Session-list summary."""
    summary: JsonObject = {
        "id": str(state["session_id"]),
        "project_id": str(state["project_id"]) or None,
        "agent_id": str(state["agent_id"]),
        "created_at": str(state["created_at"]),
        "last_active_at": str(state["last_activity_at"]),
    }
    for key in ("title", "auto_title", "source_channel_id", "platform", "platform_conv_id"):
        if state[key] is not None:
            summary[key] = str(state[key])
    if state["is_subagent"]:
        summary["is_subagent_session"] = True
    parent = _store_values._subagent_parent_from_state(state)
    if parent is not None:
        summary["subagent_parent"] = parent
    summary.update(_store_values._derived_metadata_from_state(state))
    if state["compaction_policy_json"] is not None:
        summary["compaction_policy"] = _store_values._json_from_payload(
            str(state["compaction_policy_json"]), "Session compaction policy"
        )
    summary.update(_completion_activity_from_state(state))
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
        f"SELECT {_store_values._SESSION_LIST_COLUMNS}{metadata_columns} {_LIST_FROM} "
        "WHERE s.state = 'live' AND s.project_id = ? AND s.agent_id = ? "
        "ORDER BY s.last_activity_at DESC, s.session_id",
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
    """Read one bounded, globally ordered Session-list page.

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
        "(" + " OR ".join("(s.project_id = ? AND s.agent_id = ?)" for _ in normalized_scopes) + ")"
    )
    scope_params = [value for scope in normalized_scopes for value in scope]
    visibility_sql, visibility_params = _store_values._session_list_visibility_sql(
        include_subagents=filters.include_subagents,
        include_memory_reflections=filters.include_memory_reflections,
        include_skill_reflections=filters.include_skill_reflections,
        include_cron=filters.include_cron,
        include_channels=filters.include_channels,
    )
    base_where = f"s.state = 'live' AND {scope_sql} AND {visibility_sql}"
    page_where = ""
    page_params: list[Any] = []
    if cursor is not None:
        page_where = (
            " AND (s.last_activity_at < ? OR (s.last_activity_at = ? AND "
            "(s.project_id, s.agent_id, s.session_id) > (?, ?, ?)))"
        )
        page_params.extend(
            (
                cursor.last_activity_at,
                cursor.last_activity_at,
                cursor.project_id or "",
                cursor.agent_id,
                cursor.session_id,
            )
        )
    total = int(
        connection.execute(
            f"SELECT COUNT(*) FROM sessions AS s WHERE {base_where}",
            (*scope_params, *visibility_params),
        ).fetchone()[0]
    )
    fetched = connection.execute(
        f"SELECT {_store_values._SESSION_LIST_COLUMNS} {_LIST_FROM} "
        f"WHERE {base_where}{page_where} "
        "ORDER BY s.last_activity_at DESC, s.project_id, s.agent_id, s.session_id LIMIT ?",
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
                f"{_LIST_FROM} WHERE s.state = 'live' AND s.project_id = ? "
                "AND s.agent_id = ? AND s.session_id = ?",
                (*visibility_params, *required_scope),
            ).fetchone()
    if required_row is not None and not bool(required_row["list_visible"]):
        total += 1
    listed_rows = [*rows, *([] if required_row is None else [required_row])]
    changes = change_totals(connection, [int(row["session_key"]) for row in listed_rows])
    return lambda: _summaries_page(rows, required_row, total, changes, has_more=has_more)


def _summaries_page(
    rows: Sequence[sqlite3.Row],
    required_row: sqlite3.Row | None,
    total_count: int,
    changes: dict[int, JsonObject],
    *,
    has_more: bool,
) -> SessionListPage:
    summaries = [_with_changes(_summary(row), row, changes) for row in rows]
    if required_row is not None and _store_values._address(required_row) not in {
        _store_values._address(row) for row in rows
    }:
        summaries.append(_with_changes(_summary(required_row), required_row, changes))
    last = rows[-1] if has_more and rows else None
    return SessionListPage(
        sessions=tuple(summaries),
        next_cursor=None
        if last is None
        else SessionListCursor(
            last_activity_at=str(last["last_activity_at"]),
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
        f"SELECT {_store_values._SESSION_LIST_COLUMNS} {_LIST_FROM} "
        "WHERE s.state = 'live' AND s.project_id = ? AND s.agent_id = ? AND s.session_id = ?",
        _store_values._scope(address),
    ).fetchone()
    if row is None:
        return lambda: None
    changes = change_totals(connection, [int(row["session_key"])])
    return lambda: _with_changes(_summary(row), row, changes)


def _with_changes(
    summary: JsonObject, row: sqlite3.Row, changes: dict[int, JsonObject]
) -> JsonObject:
    totals = changes.get(int(row["session_key"]))
    if totals is not None:
        summary["change_stats"] = totals
    return summary


# Session keys per change-totals statement, far below SQLite's variable limit.
_CHANGE_TOTALS_BATCH_SIZE = 500

# Changed files listed by one Session's change statistics, in path order.
MAX_SESSION_CHANGE_PATHS = 500


def change_totals(
    connection: sqlite3.Connection, session_keys: Sequence[int]
) -> dict[int, JsonObject]:
    """Return ``{files, added, removed}`` per Session that changed files.

    Totals sum the Session's own Runs (inherited fork history excluded),
    running ones with the statistics recorded so far. ``files`` counts each
    changed file once, and at least as many as any one Run reported.
    """
    totals: dict[int, JsonObject] = {}
    for batch in batched(dict.fromkeys(session_keys), _CHANGE_TOTALS_BATCH_SIZE, strict=False):
        placeholders = ", ".join("?" for _key in batch)
        listed = {
            int(row["session_key"]): int(row["listed"])
            for row in connection.execute(
                "SELECT r.session_key, COUNT(DISTINCT p.path) AS listed "
                "FROM runs AS r JOIN run_change_paths AS p ON p.run_key = r.run_key "
                f"WHERE r.session_key IN ({placeholders}) AND r.inherited = 0 "
                "GROUP BY r.session_key",
                batch,
            )
        }
        for row in connection.execute(
            "SELECT session_key, MAX(changed_files) AS most_files, "
            "SUM(lines_added) AS added, SUM(lines_removed) AS removed FROM runs "
            f"WHERE session_key IN ({placeholders}) AND inherited = 0 "
            "AND changed_files IS NOT NULL GROUP BY session_key",
            batch,
        ):
            session_key = int(row["session_key"])
            files = max(int(row["most_files"]), listed.get(session_key, 0))
            if files:
                totals[session_key] = {
                    "files": files,
                    "added": int(row["added"] or 0),
                    "removed": int(row["removed"] or 0),
                }
    return totals


def session_change_stats(
    connection: sqlite3.Connection, address: SessionAddress
) -> JsonObject | None:
    """Return one Session's change statistics with its changed files, or ``None``.

    Adds to ``change_totals`` the ``file_stats`` of up to
    ``MAX_SESSION_CHANGE_PATHS`` files in path order, each with its lines summed
    over the Runs; a count is ``None`` when a Run recorded the file without one.
    """
    session_key = int(_store_values._require_live(connection, address)["session_key"])
    totals = change_totals(connection, [session_key]).get(session_key)
    if totals is None:
        return None
    rows = connection.execute(
        "SELECT p.path, SUM(p.lines_added) AS added, SUM(p.lines_removed) AS removed, "
        "COUNT(*) = COUNT(p.lines_added) AND COUNT(*) = COUNT(p.lines_removed) AS known "
        "FROM runs AS r JOIN run_change_paths AS p ON p.run_key = r.run_key "
        "WHERE r.session_key = ? AND r.inherited = 0 GROUP BY p.path ORDER BY p.path LIMIT ?",
        (session_key, MAX_SESSION_CHANGE_PATHS),
    ).fetchall()
    return {
        **totals,
        "file_stats": [
            {
                "path": str(row["path"]),
                "added": int(row["added"]) if row["known"] else None,
                "removed": int(row["removed"]) if row["known"] else None,
            }
            for row in rows
        ],
    }


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
    for chunk in batched(normalized, _COMPLETION_ACTIVITY_SCOPE_BATCH_SIZE, strict=False):
        values = ", ".join("(?, ?)" for _scope in chunk)
        for state in connection.execute(
            f"WITH scopes(project_id, agent_id) AS (VALUES {values}) "
            "SELECT s.project_id, s.agent_id, s.session_id, "
            f"{_store_values._COMPLETION_ACTIVITY_COLUMNS} "
            "FROM scopes JOIN sessions AS s "
            "ON s.project_id = scopes.project_id AND s.agent_id = scopes.agent_id "
            "WHERE s.state = 'live' AND s.latest_completion_run_key IS NOT NULL "
            "ORDER BY s.project_id, s.agent_id, s.session_id",
            [value for scope in chunk for value in scope],
        ).fetchall():
            result[(state["project_id"] or None, state["agent_id"])].append(
                {"id": state["session_id"], **_completion_activity_from_state(state)}
            )
    return result


def list_history_revisions(
    connection: sqlite3.Connection, project_id: str | None, agent_id: str
) -> list[SessionHistoryRevision]:
    """Return every live Session version of one scope with its Recall visibility."""
    rows = connection.execute(
        "SELECT s.project_id, s.agent_id, s.session_id, s.generation_id, s.history_revision, "
        f"s.session_key, {_store_values._RECALL_VISIBILITY_SQL} AS recall_visibility "
        "FROM sessions AS s "
        "WHERE s.state = 'live' AND s.project_id = ? AND s.agent_id = ? ORDER BY s.session_id",
        (project_id or "", agent_id),
    ).fetchall()
    return [
        SessionHistoryRevision(
            _store_values._address(row),
            str(row["generation_id"]),
            int(row["history_revision"]),
            cast("SessionRecallVisibility", str(row["recall_visibility"])),
            int(row["session_key"]),
        )
        for row in rows
    ]


def list_history_versions(
    connection: sqlite3.Connection, addresses: Sequence[SessionAddress]
) -> dict[SessionAddress, tuple[str, int]]:
    """Return the generation id and history revision for many addresses at once.

    Addresses without a live row are absent. Derived projections (Statistics,
    Recall indexes) refresh their freshness stamps with one query per scope
    chunk instead of one query per Session.
    """
    versions: dict[SessionAddress, tuple[str, int]] = {}
    for (project_id, agent_id), session_ids in _by_scope(addresses).items():
        for chunk in batched(
            session_ids, _store_values._DESCRIPTOR_SOURCE_BATCH_SIZE, strict=False
        ):
            for row in connection.execute(
                "SELECT project_id, agent_id, session_id, generation_id, history_revision "
                "FROM sessions WHERE project_id = ? AND agent_id = ? AND state = 'live' "
                "AND session_id IN (SELECT value FROM json_each(?))",
                (project_id, agent_id, _store_values._json_list(chunk)),
            ):
                versions[_store_values._address(row)] = (
                    str(row["generation_id"]),
                    int(row["history_revision"]),
                )
    return versions
