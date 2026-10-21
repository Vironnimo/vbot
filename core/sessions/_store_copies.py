"""Materialize inherited entries, Runs, Tool calls and payloads on detachment."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from core.sessions import _store_lineage, _store_values
from core.utils.timestamps import utc_now_timestamp

if TYPE_CHECKING:
    from core.sessions._store_lineage import ViewRange

# ruff: noqa: E501


_KEYS = "IN (SELECT value FROM json_each(?))"


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]


def _copy_side_rows(
    connection: sqlite3.Connection, table: str, pairs: Sequence[tuple[int, int]]
) -> None:
    """Copy the ``entry_key``-keyed rows of *table* for ``(new, old)`` key pairs."""
    columns = [column for column in _table_columns(connection, table) if column != "entry_key"]
    connection.executemany(
        f"INSERT INTO {table} (entry_key, {', '.join(columns)}) "
        f"SELECT ?, {', '.join(columns)} FROM {table} WHERE entry_key = ?",
        pairs,
    )


_ENTRY_SIDE_TABLES = (
    "session_attachment_refs",
    "entry_text",
    "assistant_entries",
    "assistant_reasoning",
    "assistant_output_files",
    "user_entry_senders",
    "user_entry_origins",
    "error_entries",
    "history_edit_entries",
    "note_model_fallbacks",
    "note_goal_events",
    "note_subagent_messages",
    "note_widget_states",
    "checkpoint_entries",
    "checkpoint_projections",
)


def copy_entries(
    connection: sqlite3.Connection, *, target_key: int, view_range: ViewRange
) -> list[int]:
    """Give *target_key* its own copy of the entries *view_range* admits from its source.

    Copies keep their seq and id, get new keys and are current. Their side
    rows, Tool calls with their payloads and referenced Runs come along; copied
    Runs are inherited and never admitted again, and unfinished calls or Runs
    end interrupted. Returns the new entry keys.
    """
    now = utc_now_timestamp()
    columns = _table_columns(connection, "entries")
    column_sql = {column: '"' + column.replace('"', '""') + '"' for column in columns}
    rows = connection.execute(
        f"SELECT {', '.join(f'e.{column_sql[column]}' for column in columns)} FROM entries AS e "
        f"WHERE {_store_lineage.range_predicate()} ORDER BY e.seq",
        _store_lineage.range_params(view_range),
    ).fetchall()
    run_map = {
        run_key: _copy_run(connection, run_key, target_key, now)
        for run_key in sorted({int(row["run_key"]) for row in rows if row["run_key"] is not None})
    }
    entry_map: dict[int, int] = {}
    copied_columns = [column for column in columns if column != "entry_key"]
    for row in rows:
        replaced = {
            "session_key": target_key,
            "run_key": None if row["run_key"] is None else run_map[int(row["run_key"])],
            "superseded_at_seq": None,
        }
        cursor = connection.execute(
            f"INSERT INTO entries ({', '.join(column_sql[column] for column in copied_columns)}) "
            f"VALUES ({', '.join('?' for _ in copied_columns)})",
            tuple(replaced.get(column, row[column]) for column in copied_columns),
        )
        assert cursor.lastrowid is not None
        entry_map[int(row["entry_key"])] = int(cursor.lastrowid)
    pairs = [(new, old) for old, new in entry_map.items()]
    for table in _ENTRY_SIDE_TABLES:
        _copy_side_rows(connection, table, pairs)
    _copy_tool_calls(connection, entry_map, now)
    for row in rows:
        if row["role"] == "run_summary" and row["run_key"] is not None:
            connection.execute(
                "UPDATE runs SET end_entry_key = ? WHERE run_key = ? AND end_entry_key IS NULL",
                (entry_map[int(row["entry_key"])], run_map[int(row["run_key"])]),
            )
    return list(entry_map.values())


def _copy_run(connection: sqlite3.Connection, run_key: int, target_key: int, now: str) -> int:
    existing = connection.execute(
        "SELECT target.run_key FROM runs AS source JOIN runs AS target "
        "ON target.session_key = ? AND target.run_id = source.run_id WHERE source.run_key = ?",
        (target_key, run_key),
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    replaced = {
        "session_key": "?",
        "inherited": "1",
        "contributes_to_activity": "0",
        "end_entry_key": "NULL",
        "status": "CASE WHEN status = 'running' THEN 'interrupted' ELSE status END",
        "completed_at": "CASE WHEN status = 'running' THEN ? ELSE completed_at END",
    }
    columns = [column for column in _table_columns(connection, "runs") if column != "run_key"]
    expressions = [replaced.get(column, column) for column in columns]
    params: list[Any] = []
    for column in columns:
        if column == "session_key":
            params.append(target_key)
        elif column == "completed_at":
            params.append(now)
    params.append(run_key)
    cursor = connection.execute(
        f"INSERT INTO runs ({', '.join(columns)}) SELECT {', '.join(expressions)} "
        "FROM runs WHERE run_key = ?",
        params,
    )
    assert cursor.lastrowid is not None
    copied = int(cursor.lastrowid)
    path_columns = ", ".join(
        '"' + column.replace('"', '""') + '"'
        for column in _table_columns(connection, "run_change_paths")
        if column != "run_key"
    )
    connection.execute(
        f"INSERT INTO run_change_paths (run_key, {path_columns}) "
        f"SELECT ?, {path_columns} FROM run_change_paths WHERE run_key = ?",
        (copied, run_key),
    )
    return copied


def _copy_tool_calls(
    connection: sqlite3.Connection, entry_map: Mapping[int, int], now: str
) -> None:
    if not entry_map:
        return
    columns = [
        column
        for column in _table_columns(connection, "tool_calls")
        if column not in {"call_key", "entry_key", "result_entry_key", "status", "completed_at"}
    ]
    calls = connection.execute(
        f"SELECT call_key, entry_key, result_entry_key, status, completed_at, {', '.join(columns)} "
        f"FROM tool_calls WHERE entry_key {_KEYS} ORDER BY call_key",
        (_store_values._key_list(list(entry_map)),),
    ).fetchall()
    payload_columns = [
        column
        for column in _table_columns(connection, "tool_call_payloads")
        if column != "call_key"
    ]
    result_payload_columns = [
        column
        for column in _table_columns(connection, "tool_result_payloads")
        if column not in {"payload_key", "call_key"}
    ]
    for call in calls:
        unfinished = call["status"] in {"pending", "running"}
        result_key = call["result_entry_key"]
        cursor = connection.execute(
            f"INSERT INTO tool_calls (entry_key, result_entry_key, status, completed_at, "
            f"{', '.join(columns)}) VALUES ({', '.join('?' for _ in range(4 + len(columns)))})",
            (
                entry_map[int(call["entry_key"])],
                None if result_key is None else entry_map.get(int(result_key)),
                "interrupted" if unfinished else call["status"],
                now if unfinished else call["completed_at"],
                *(call[column] for column in columns),
            ),
        )
        connection.execute(
            f"INSERT INTO tool_call_payloads (call_key, {', '.join(payload_columns)}) "
            f"SELECT ?, {', '.join(payload_columns)} FROM tool_call_payloads WHERE call_key = ?",
            (cursor.lastrowid, call["call_key"]),
        )
        connection.execute(
            f"INSERT INTO tool_result_payloads (call_key, {', '.join(result_payload_columns)}) "
            f"SELECT ?, {', '.join(result_payload_columns)} FROM tool_result_payloads "
            "WHERE call_key = ? ORDER BY payload_key",
            (cursor.lastrowid, call["call_key"]),
        )
