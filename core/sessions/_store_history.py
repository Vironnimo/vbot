"""Bounded Message and history projections in a supplied snapshot."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from core.chat.errors import ChatSessionError
from core.sessions import _store_codec, _store_values
from core.sessions._types import JsonObject
from core.sessions.errors import (
    SessionPageCursorError,
    SessionStoreCorruptError,
)

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions._types import SessionAddress, SessionReadCursor


def _session_usage_from_connection(
    connection: sqlite3.Connection,
    session_key: int,
) -> tuple[JsonObject, int]:
    input_estimated = (
        "CASE WHEN a.input_tokens_estimated IS NOT NULL THEN a.input_tokens_estimated "
        "WHEN a.output_tokens_estimated IS NOT NULL THEN 0 "
        "ELSE COALESCE(a.usage_estimated, 0) END"
    )
    output_estimated = (
        "CASE WHEN a.output_tokens_estimated IS NOT NULL THEN a.output_tokens_estimated "
        "WHEN a.input_tokens_estimated IS NOT NULL THEN 0 "
        "ELSE COALESCE(a.usage_estimated, 0) END"
    )
    row = connection.execute(
        f"""
        SELECT
          COALESCE(SUM(CASE WHEN a.usage_present = 1
            AND ({input_estimated}) = 0 AND ({output_estimated}) = 0 THEN 1 ELSE 0 END), 0)
            AS measured_turns,
          COALESCE(SUM(CASE WHEN a.usage_present = 1
            AND (({input_estimated}) = 1 OR ({output_estimated}) = 1) THEN 1 ELSE 0 END), 0)
            AS estimated_turns,
          COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({input_estimated}) = 0
            AND (a.cache_read_tokens IS NOT NULL OR a.cache_write_tokens IS NOT NULL)
            THEN 1 ELSE 0 END), 0) AS cache_turns,
          COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({output_estimated}) = 0
            AND a.reasoning_tokens IS NOT NULL THEN 1 ELSE 0 END), 0) AS reasoning_turns,
          COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({input_estimated}) = 0
            THEN COALESCE(a.input_tokens, 0) ELSE 0 END), 0) AS input_tokens,
          COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({output_estimated}) = 0
            THEN COALESCE(a.output_tokens, 0) ELSE 0 END), 0) AS output_tokens,
          COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({input_estimated}) = 0
            THEN COALESCE(a.cache_read_tokens, 0) ELSE 0 END), 0) AS cache_read_tokens,
          COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({input_estimated}) = 0
            THEN COALESCE(a.cache_write_tokens, 0) ELSE 0 END), 0) AS cache_write_tokens,
          COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({output_estimated}) = 0
            THEN COALESCE(a.reasoning_tokens, 0) ELSE 0 END), 0) AS reasoning_tokens,
          COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({input_estimated}) = 0
            AND (a.cache_read_tokens IS NOT NULL OR a.cache_write_tokens IS NOT NULL)
            THEN COALESCE(a.input_tokens, 0) ELSE 0 END), 0) AS cache_input_tokens
        FROM messages AS m
        JOIN assistant_messages AS a ON a.message_key = m.message_key
        WHERE m.session_key = ?
        """,
        (session_key,),
    ).fetchone()
    assert row is not None
    usage: JsonObject = {
        "measured_turns": int(row["measured_turns"]),
        "estimated_turns": int(row["estimated_turns"]),
        "cache_turns": int(row["cache_turns"]),
        "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]),
        "cache_read_tokens": int(row["cache_read_tokens"]),
        "cache_write_tokens": int(row["cache_write_tokens"]),
    }
    if int(row["reasoning_turns"]) > 0:
        usage["reasoning_turns"] = int(row["reasoning_turns"])
        usage["reasoning_tokens"] = int(row["reasoning_tokens"])
    return usage, int(row["cache_input_tokens"])


def _active_message_page_from_connection(
    connection: sqlite3.Connection,
    state: sqlite3.Row,
    *,
    limit: int | None,
    before_message_id: str | None,
    before_sequence: int | None,
    expected_generation_id: str | None,
    excluded_roles: Sequence[str],
    complete_run_segment: bool,
) -> tuple[list[sqlite3.Row], bool, frozenset[str], int | None]:
    session_key = int(state["session_key"])
    excluded = tuple(dict.fromkeys(excluded_roles))
    clauses = ["m.session_key = ?", "m.active = 1"]
    params: list[Any] = [session_key]
    if excluded:
        placeholders = ", ".join("?" for _ in excluded)
        clauses.append(f"m.role NOT IN ({placeholders})")
        params.extend(excluded)
    cutoff = int(state["message_count"])
    if before_sequence is not None:
        if str(state["generation_id"]) != expected_generation_id:
            raise SessionPageCursorError("before cursor is invalid")
        before_row = connection.execute(
            "SELECT m.seq FROM messages AS m WHERE " + " AND ".join(clauses) + " AND m.seq = ?",
            (*params, before_sequence),
        ).fetchone()
        if before_row is None:
            raise SessionPageCursorError("before cursor is invalid")
        cutoff = int(before_row["seq"])
    elif before_message_id is not None:
        before_row = connection.execute(
            "SELECT m.seq FROM messages AS m WHERE "
            + " AND ".join(clauses)
            + " AND m.message_id = ? ORDER BY m.seq LIMIT 1",
            (*params, before_message_id),
        ).fetchone()
        if before_row is None:
            raise SessionPageCursorError("before must reference an active message id")
        cutoff = int(before_row["seq"])

    page_clauses = [*clauses, "m.seq < ?"]
    page_params = [*params, cutoff]
    sql = _store_values._message_records_sql(
        where=" AND ".join(page_clauses),
        order_by="ORDER BY m.seq DESC",
    )
    if limit is not None:
        sql += " LIMIT ?"
        page_params.append(limit)
    rows = list(reversed(connection.execute(sql, page_params).fetchall()))
    if not rows:
        return [], False, frozenset(), None

    page_floor = int(rows[0]["seq"])
    earlier = connection.execute(
        "SELECT 1 FROM messages AS m WHERE " + " AND ".join(clauses) + " AND m.seq < ? LIMIT 1",
        (*params, page_floor),
    ).fetchone()
    if limit is not None and complete_run_segment and earlier is not None:
        previous_summary = connection.execute(
            "SELECT m.seq FROM messages AS m WHERE "
            + " AND ".join(clauses)
            + " AND m.role = 'run_summary' AND m.seq < ? ORDER BY m.seq DESC LIMIT 1",
            (*params, page_floor),
        ).fetchone()
        should_expand = previous_summary is not None or any(
            str(row["role"]) == "run_summary" for row in rows
        )
        if should_expand:
            segment_floor = 0 if previous_summary is None else int(previous_summary["seq"]) + 1
            rows = connection.execute(
                _store_values._message_records_sql(
                    where=" AND ".join([*clauses, "m.seq >= ?", "m.seq < ?"]),
                    order_by="ORDER BY m.seq",
                ),
                (*params, segment_floor, cutoff),
            ).fetchall()
            # The Run boundary may land on an excluded Note or inactive Message.
            # Cursors must anchor the first row accepted by the same visibility
            # predicate so the next page can validate and resume from it.
            page_floor = int(rows[0]["seq"])

    has_more = (
        connection.execute(
            "SELECT 1 FROM messages AS m WHERE " + " AND ".join(clauses) + " AND m.seq < ? LIMIT 1",
            (*params, page_floor),
        ).fetchone()
        is not None
    )
    latest_takeover = connection.execute(
        "SELECT MAX(seq) FROM messages WHERE session_key = ? AND active = 1 "
        "AND role = 'agent_takeover'",
        (session_key,),
    ).fetchone()[0]
    takeover_seq = -1 if latest_takeover is None else int(latest_takeover)
    editable_ids = frozenset(
        str(row["message_id"])
        for row in rows
        if str(row["role"]) == "user"
        and row["content"] is not None
        and row["sender_id"] is None
        and int(row["seq"]) > takeover_seq
    )
    return rows, has_more, editable_ids, page_floor


def _active_message_subset_from_connection(
    connection: sqlite3.Connection,
    state: sqlite3.Row,
    *,
    roles: Sequence[str],
    tool_names: Sequence[str] = (),
) -> list[sqlite3.Row]:
    selected_roles = tuple(dict.fromkeys(roles))
    if not selected_roles:
        return []
    role_placeholders = ", ".join("?" for _ in selected_roles)
    where = f"m.session_key = ? AND m.active = 1 AND m.role IN ({role_placeholders})"
    params: list[Any] = [state["session_key"], *selected_roles]
    selected_tool_names = tuple(dict.fromkeys(tool_names))
    if selected_tool_names:
        name_placeholders = ", ".join("?" for _ in selected_tool_names)
        where += f" AND (m.role <> 'tool' OR t.name IN ({name_placeholders}))"
        params.extend(selected_tool_names)
    return connection.execute(
        _store_values._message_records_sql(where=where, order_by="ORDER BY m.seq"),
        params,
    ).fetchall()


def _context_usage_rows_from_connection(
    connection: sqlite3.Connection,
    state: sqlite3.Row,
) -> list[sqlite3.Row]:
    anchor = connection.execute(
        """
        SELECT m.seq
        FROM messages AS m
        LEFT JOIN assistant_messages AS a ON a.message_key = m.message_key
        LEFT JOIN compaction_checkpoints AS c ON c.message_key = m.message_key
        WHERE m.session_key = ? AND m.active = 1
          AND ((m.role = 'assistant' AND a.usage_present = 1)
            OR (m.role = 'compaction_checkpoint' AND c.context_tokens_after IS NOT NULL))
        ORDER BY m.seq DESC
        LIMIT 1
        """,
        (state["session_key"],),
    ).fetchone()
    if anchor is None:
        return []
    return connection.execute(
        _store_values._message_records_sql(
            where="m.session_key = ? AND m.active = 1 AND m.seq >= ?",
            order_by="ORDER BY m.seq",
        ),
        (state["session_key"], anchor["seq"]),
    ).fetchall()


def _history_record_filter(
    roles: Sequence[str],
    excluded_tool_name: str,
) -> tuple[str, list[Any]]:
    selected_roles = tuple(dict.fromkeys(roles))
    if not selected_roles:
        return "0", []
    placeholders = ", ".join("?" for _ in selected_roles)
    where = f"m.role IN ({placeholders})"
    where += """
      AND NOT (
        m.role = 'tool'
        AND (
          t.name = ?
          OR EXISTS (
            SELECT 1 FROM tool_calls AS history_call
            WHERE history_call.tool_call_key = t.tool_call_key
              AND history_call.name = ?
          )
        )
      )
      AND NOT (
        m.role = 'assistant'
        AND NULLIF(m.content, '') IS NULL
        AND (m.content_blocks_json IS NULL OR json_array_length(m.content_blocks_json) = 0)
        AND NULLIF(a.reasoning, '') IS NULL
        AND (
          a.reasoning_meta_json IS NULL
          OR NOT EXISTS (SELECT 1 FROM json_each(a.reasoning_meta_json))
        )
        AND NOT EXISTS (
          SELECT 1 FROM tool_calls AS visible_call
          WHERE visible_call.message_key = m.message_key
            AND visible_call.name <> ?
        )
      )
    """
    return where, [*selected_roles, excluded_tool_name, excluded_tool_name, excluded_tool_name]


def _history_snapshot_is_current(
    connection: sqlite3.Connection,
    state: sqlite3.Row,
    *,
    expected_generation_id: str,
    snapshot_sequence: int,
) -> bool:
    if str(state["generation_id"]) != expected_generation_id:
        return False
    checkpoint = connection.execute(
        "SELECT 1 FROM messages WHERE session_key = ? AND active = 1 "
        "AND role = 'compaction_checkpoint' AND seq = ?",
        (state["session_key"], snapshot_sequence),
    ).fetchone()
    return checkpoint is not None


def active_messages(connection: sqlite3.Connection, address: SessionAddress) -> list[ChatMessage]:
    """Load the relationally materialized active lineage only."""
    state = _store_values._require_live(connection, address)
    rows = connection.execute(
        _store_values._message_records_sql(
            where="m.session_key = ? AND m.active = 1",
            order_by="ORDER BY m.seq",
        ),
        (state["session_key"],),
    ).fetchall()
    return [_store_codec.message_from_row(row) for row in rows]


def active_user_message_count(
    connection: sqlite3.Connection, address: SessionAddress, *, limit: int
) -> int:
    """Count at most ``limit`` active User Messages without loading their content."""
    if limit <= 0:
        raise ChatSessionError("user Message count limit must be positive")
    state = _store_values._require_live(connection, address)
    row = connection.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM messages WHERE session_key = ? "
        "AND active = 1 AND role = 'user' LIMIT ?)",
        (state["session_key"], limit),
    ).fetchone()
    assert row is not None
    return int(row[0])


def latest_note(
    connection: sqlite3.Connection, address: SessionAddress, *, content_prefix: str
) -> ChatMessage | None:
    """Load the newest Note matching one canonical content prefix."""
    if not content_prefix:
        raise ChatSessionError("note prefix must be non-empty")
    state = _store_values._require_live(connection, address)
    row = connection.execute(
        _store_values._message_records_sql(
            where=("m.session_key = ? AND m.role = 'note' AND substr(m.content, 1, ?) = ?"),
            order_by="ORDER BY m.seq DESC LIMIT 1",
        ),
        (state["session_key"], len(content_prefix), content_prefix),
    ).fetchone()
    return None if row is None else _store_codec.message_from_row(row)


def chat_history_snapshot(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    limit: int | None,
    before_message_id: str | None,
    before_sequence: int | None,
    expected_generation_id: str | None,
    excluded_roles: Sequence[str],
    complete_run_segment: bool,
    background_roles: Sequence[str],
    background_tool_names: Sequence[str],
) -> tuple[
    list[ChatMessage],
    bool,
    frozenset[str],
    JsonObject,
    list[ChatMessage],
    list[ChatMessage],
    str,
    int | None,
]:
    """Read one WebUI history projection from a single SQLite snapshot."""
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
        raise ChatSessionError("message page limit must be a positive integer")
    state = _store_values._require_live(connection, address)
    page_rows, has_more, editable_ids, page_floor = _active_message_page_from_connection(
        connection,
        state,
        limit=limit,
        before_message_id=before_message_id,
        before_sequence=before_sequence,
        expected_generation_id=expected_generation_id,
        excluded_roles=excluded_roles,
        complete_run_segment=complete_run_segment,
    )
    usage, _cache_input_tokens = _session_usage_from_connection(
        connection, int(state["session_key"])
    )
    context_rows = _context_usage_rows_from_connection(connection, state)
    background_rows = _active_message_subset_from_connection(
        connection,
        state,
        roles=background_roles,
        tool_names=background_tool_names,
    )
    return (
        [_store_codec.message_from_row(row) for row in page_rows],
        has_more,
        editable_ids,
        usage,
        [_store_codec.message_from_row(row) for row in context_rows],
        [_store_codec.message_from_row(row) for row in background_rows],
        str(state["generation_id"]),
        page_floor,
    )


def status_snapshot(
    connection: sqlite3.Connection, address: SessionAddress
) -> tuple[str | None, int, JsonObject | None, JsonObject, int]:
    state = _store_values._require_live(connection, address)
    session_key = int(state["session_key"])
    facts = connection.execute(
        "SELECT MIN(CASE WHEN seq = 0 THEN timestamp END) AS first_message_at, "
        "SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END) AS user_count "
        "FROM messages WHERE session_key = ?",
        (session_key,),
    ).fetchone()
    latest_row = connection.execute(
        _store_values._message_records_sql(
            where=("m.session_key = ? AND m.role = 'assistant' AND a.usage_present = 1"),
            order_by="ORDER BY m.seq DESC LIMIT 1",
        ),
        (session_key,),
    ).fetchone()
    latest_usage = None
    if latest_row is not None:
        latest_usage = _store_codec.message_from_row(latest_row).usage
    usage, cache_input_tokens = _session_usage_from_connection(connection, session_key)
    return (
        None if facts["first_message_at"] is None else str(facts["first_message_at"]),
        int(facts["user_count"] or 0),
        latest_usage,
        usage,
        cache_input_tokens,
    )


def history_snapshot(
    connection: sqlite3.Connection, address: SessionAddress, *, snapshot_sequence: int | None = None
) -> tuple[str, list[tuple[int, str, str, str]]] | None:
    """Resolve active Compaction checkpoints without reading history records."""
    state = _store_values._require_live(connection, address)
    session_key = int(state["session_key"])
    if snapshot_sequence is None:
        upper = connection.execute(
            "SELECT MAX(seq) FROM messages WHERE session_key = ? AND active = 1 "
            "AND role = 'compaction_checkpoint'",
            (session_key,),
        ).fetchone()[0]
        if upper is None:
            return None
        snapshot_sequence = int(upper)
    elif not _history_snapshot_is_current(
        connection,
        state,
        expected_generation_id=str(state["generation_id"]),
        snapshot_sequence=snapshot_sequence,
    ):
        return None
    rows = connection.execute(
        "SELECT seq, message_id, timestamp, COALESCE(content, '') AS summary "
        "FROM messages WHERE session_key = ? AND active = 1 "
        "AND role = 'compaction_checkpoint' AND seq <= ? ORDER BY seq",
        (session_key, snapshot_sequence),
    ).fetchall()
    if not rows:
        return None
    return str(state["generation_id"]), [
        (int(row["seq"]), str(row["message_id"]), str(row["timestamp"]), str(row["summary"]))
        for row in rows
    ]


def history_records(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    expected_generation_id: str,
    snapshot_sequence: int,
    lower_sequence: int,
    upper_sequence: int,
    roles: Sequence[str],
    direction: str,
    cursor_sequence: int | None,
    limit: int,
    excluded_tool_name: str,
) -> list[tuple[int, ChatMessage]] | None:
    """Read one bounded canonical history batch in sequence order."""
    if direction not in {"start", "end"}:
        raise ChatSessionError("history direction must be start or end")
    if limit <= 0:
        raise ChatSessionError("history record limit must be positive")
    record_filter, filter_params = _history_record_filter(roles, excluded_tool_name)
    state = _store_values._require_live(connection, address)
    if not _history_snapshot_is_current(
        connection,
        state,
        expected_generation_id=expected_generation_id,
        snapshot_sequence=snapshot_sequence,
    ):
        return None
    clauses = [
        "m.session_key = ?",
        "m.active = 1",
        "m.seq > ?",
        "m.seq < ?",
        record_filter,
    ]
    params: list[Any] = [
        state["session_key"],
        lower_sequence,
        upper_sequence,
        *filter_params,
    ]
    if cursor_sequence is not None:
        clauses.append("m.seq >= ?" if direction == "start" else "m.seq <= ?")
        params.append(cursor_sequence)
    params.append(limit)
    rows = connection.execute(
        _store_values._message_records_sql(
            where=" AND ".join(clauses),
            order_by=(
                "ORDER BY m.seq ASC LIMIT ?"
                if direction == "start"
                else "ORDER BY m.seq DESC LIMIT ?"
            ),
        ),
        params,
    ).fetchall()
    return [(int(row["seq"]), _store_codec.message_from_row(row)) for row in rows]


def history_section_stats(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    expected_generation_id: str,
    snapshot_sequence: int,
    sections: Sequence[tuple[int, int]],
    excluded_tool_name: str,
) -> dict[int, tuple[int, str | None, str | None]] | None:
    """Aggregate default-role History overview facts for selected sections."""
    record_filter, filter_params = _history_record_filter(
        ("user", "assistant", "error"), excluded_tool_name
    )
    result: dict[int, tuple[int, str | None, str | None]] = {}
    state = _store_values._require_live(connection, address)
    if not _history_snapshot_is_current(
        connection,
        state,
        expected_generation_id=expected_generation_id,
        snapshot_sequence=snapshot_sequence,
    ):
        return None
    for lower_sequence, upper_sequence in sections:
        row = connection.execute(
            "WITH eligible AS ("
            "SELECT m.seq, m.timestamp FROM messages AS m "
            f"{_store_values._MESSAGE_RECORD_JOINS} WHERE m.session_key = ? AND m.active = 1 "
            "AND m.seq > ? AND m.seq < ? AND " + record_filter + "), bounds AS ("
            "SELECT COUNT(*) AS eligible_count, MIN(seq) AS first_seq, "
            "MAX(seq) AS last_seq FROM eligible) "
            "SELECT bounds.eligible_count, first.timestamp AS start_timestamp, "
            "last.timestamp AS end_timestamp FROM bounds "
            "LEFT JOIN eligible AS first ON first.seq = bounds.first_seq "
            "LEFT JOIN eligible AS last ON last.seq = bounds.last_seq",
            (state["session_key"], lower_sequence, upper_sequence, *filter_params),
        ).fetchone()
        assert row is not None
        result[upper_sequence] = (
            int(row["eligible_count"]),
            None if row["start_timestamp"] is None else str(row["start_timestamp"]),
            None if row["end_timestamp"] is None else str(row["end_timestamp"]),
        )
    return result


def history_around(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    expected_generation_id: str,
    snapshot_sequence: int,
    lower_sequence: int,
    upper_sequence: int,
    roles: Sequence[str],
    message_id: str,
    before: int,
    after: int,
    excluded_tool_name: str,
) -> tuple[bool, list[tuple[int, ChatMessage]]] | None:
    """Read a bounded eligible neighborhood around the earliest matching public id."""
    record_filter, filter_params = _history_record_filter(roles, excluded_tool_name)
    state = _store_values._require_live(connection, address)
    if not _history_snapshot_is_current(
        connection,
        state,
        expected_generation_id=expected_generation_id,
        snapshot_sequence=snapshot_sequence,
    ):
        return None
    exists = (
        connection.execute(
            "SELECT 1 FROM messages WHERE session_key = ? AND active = 1 "
            "AND message_id = ? LIMIT 1",
            (state["session_key"], message_id),
        ).fetchone()
        is not None
    )
    base_clauses = [
        "m.session_key = ?",
        "m.active = 1",
        "m.seq > ?",
        "m.seq < ?",
        record_filter,
    ]
    base_params: list[Any] = [
        state["session_key"],
        lower_sequence,
        upper_sequence,
        *filter_params,
    ]
    anchor = connection.execute(
        _store_values._message_records_sql(
            where=" AND ".join([*base_clauses, "m.message_id = ?"]),
            order_by="ORDER BY m.seq LIMIT 1",
        ),
        (*base_params, message_id),
    ).fetchone()
    if anchor is None:
        return exists, []
    anchor_sequence = int(anchor["seq"])
    earlier_rows = connection.execute(
        _store_values._message_records_sql(
            where=" AND ".join([*base_clauses, "m.seq < ?"]),
            order_by="ORDER BY m.seq DESC LIMIT ?",
        ),
        (*base_params, anchor_sequence, before),
    ).fetchall()
    later_rows = connection.execute(
        _store_values._message_records_sql(
            where=" AND ".join([*base_clauses, "m.seq > ?"]),
            order_by="ORDER BY m.seq LIMIT ?",
        ),
        (*base_params, anchor_sequence, after),
    ).fetchall()
    rows = [*reversed(earlier_rows), anchor, *later_rows]
    return exists, [(int(row["seq"]), _store_codec.message_from_row(row)) for row in rows]


def reflection_runs(connection: sqlite3.Connection, address: SessionAddress) -> list[JsonObject]:
    """Read each review fork's own terminal Run, excluding inherited history."""
    source = _store_values._require_live(connection, address)
    rows = connection.execute(
        """
        SELECT s.session_id, r.run_id, r.status, r.started_at,
          (SELECT value FROM json_each(s.run_kinds_json)
           WHERE value IN ('reflection', 'memory_reflection', 'skill_reflection')
           ORDER BY key LIMIT 1) AS run_kind
        FROM sessions AS s
        JOIN messages AS m ON m.message_key = (
          SELECT message_key FROM messages
          WHERE session_key = s.session_key AND role = 'run_summary'
            AND seq >= json_extract(s.fork_source_json, '$.message_count')
          ORDER BY seq LIMIT 1
        )
        JOIN run_summaries AS r ON r.message_key = m.message_key
        WHERE s.status = 'live' AND s.project_id = ? AND s.agent_id = ?
          AND json_extract(s.fork_source_json, '$.session_id') = ?
          AND json_extract(s.fork_source_json, '$.agent_id') = ?
          AND json_extract(s.fork_source_json, '$.project_id') IS ?
          AND julianday(json_extract(s.fork_source_json, '$.forked_at'))
            >= julianday(?)
          AND EXISTS (SELECT 1 FROM json_each(s.run_kinds_json)
            WHERE value IN ('reflection', 'memory_reflection', 'skill_reflection'))
        ORDER BY r.started_at, r.run_id
        """,
        (
            source["project_id"],
            address.agent_id,
            address.session_id,
            address.agent_id,
            address.project_id,
            source["created_at"],
        ),
    ).fetchall()
    return [dict(row) for row in rows]


def run_summary(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    run_id: str | None = None,
    work_id: str | None = None,
) -> ChatMessage | None:
    if (run_id is None) == (work_id is None):
        raise ChatSessionError("exactly one of run_id or work_id is required")
    state = _store_values._require_live(connection, address)
    field, value = ("r.run_id", run_id) if run_id is not None else ("r.work_id", work_id)
    row = connection.execute(
        _store_values._message_records_sql(
            where=f"m.session_key = ? AND m.role = 'run_summary' AND {field} = ?",
            order_by="ORDER BY m.seq DESC LIMIT 1",
        ),
        (state["session_key"], value),
    ).fetchone()
    return None if row is None else _store_codec.message_from_row(row)


def run_result(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    run_id: str | None = None,
    work_id: str | None = None,
    require_latest: bool = False,
) -> tuple[ChatMessage | None, ChatMessage, str | None] | None:
    """Project one terminal Run without reconstructing its Tool/result payloads."""
    if run_id is not None and work_id is not None:
        raise ChatSessionError("run_id and work_id cannot be combined")
    state = _store_values._require_live(connection, address)
    params: list[Any] = [state["session_key"]]
    where = "m.session_key = ? AND m.role = 'run_summary'"
    if run_id is not None:
        where += " AND r.run_id = ?"
        params.append(run_id)
    elif work_id is not None:
        where += " AND r.work_id = ?"
        params.append(work_id)
    summary_row = connection.execute(
        _store_values._message_records_sql(
            where=where,
            order_by="ORDER BY m.seq DESC LIMIT 1",
        ),
        params,
    ).fetchone()
    if summary_row is None:
        return None
    summary_seq = int(summary_row["seq"])
    if require_latest:
        later = connection.execute(
            "SELECT 1 FROM messages WHERE session_key = ? AND seq > ? "
            "AND role IN ('user', 'assistant', 'tool', 'error') LIMIT 1",
            (state["session_key"], summary_seq),
        ).fetchone()
        if later is not None:
            return None
    previous = connection.execute(
        "SELECT MAX(seq) FROM messages WHERE session_key = ? AND role = 'run_summary' AND seq < ?",
        (state["session_key"], summary_seq),
    ).fetchone()[0]
    previous_seq = -1 if previous is None else int(previous)
    assistant_row = connection.execute(
        _store_values._message_records_sql(
            where=(
                "m.session_key = ? AND m.seq > ? AND m.seq < ? "
                "AND m.role = 'assistant' AND (NULLIF(m.content, '') IS NOT NULL "
                "OR (m.content_blocks_json IS NOT NULL "
                "AND json_array_length(m.content_blocks_json) > 0))"
            ),
            order_by="ORDER BY m.seq DESC LIMIT 1",
        ),
        (state["session_key"], previous_seq, summary_seq),
    ).fetchone()
    latest_tool = connection.execute(
        "SELECT tc.name FROM messages AS m "
        "JOIN tool_calls AS tc ON tc.message_key = m.message_key "
        "WHERE m.session_key = ? AND m.seq > ? AND m.seq < ? "
        "ORDER BY m.seq DESC, tc.ordinal DESC LIMIT 1",
        (state["session_key"], previous_seq, summary_seq),
    ).fetchone()
    return (
        None if assistant_row is None else _store_codec.message_from_row(assistant_row),
        _store_codec.message_from_row(summary_row),
        None if latest_tool is None else str(latest_tool["name"]),
    )


def messages_since(
    connection: sqlite3.Connection, address: SessionAddress, cursor: SessionReadCursor | None
) -> tuple[list[ChatMessage], SessionReadCursor] | None:
    from core.sessions._types import SessionReadCursor

    state = _store_values._require_live(connection, address)
    count = int(state["message_count"])
    revision = int(state["history_revision"])
    generation_id = str(state["generation_id"])
    last_id = state["last_message_id"]
    if cursor is not None:
        if cursor.generation_id != generation_id or not 0 <= cursor.next_seq <= count:
            return None
        if cursor.next_seq == 0:
            anchor_id = None
        else:
            anchor = connection.execute(
                "SELECT message_id FROM messages WHERE session_key = ? AND seq = ?",
                (state["session_key"], cursor.next_seq - 1),
            ).fetchone()
            anchor_id = None if anchor is None else anchor["message_id"]
        if anchor_id != cursor.last_message_id:
            return None
    start = 0 if cursor is None else cursor.next_seq
    rows = connection.execute(
        _store_values._message_records_sql(
            where="m.session_key = ? AND m.seq >= ?",
            order_by="ORDER BY m.seq",
        ),
        (state["session_key"], start),
    ).fetchall()
    return (
        [_store_codec.message_from_row(row) for row in rows],
        SessionReadCursor(generation_id, revision, count, count, last_id),
    )


def bookend_timestamps(
    connection: sqlite3.Connection, address: SessionAddress
) -> tuple[str, str] | None:
    state = _store_values._require_live(connection, address)
    if int(state["message_count"]) == 0:
        return None
    first = connection.execute(
        "SELECT timestamp FROM messages WHERE session_key = ? AND seq = 0",
        (state["session_key"],),
    ).fetchone()
    if first is None or state["last_message_at"] is None:
        raise SessionStoreCorruptError(f"invalid Session message summary: {address.session_id}")
    return str(first["timestamp"]), str(state["last_message_at"])


def messages(connection: sqlite3.Connection, address: SessionAddress) -> list[ChatMessage]:
    state = _store_values._require_live(connection, address)
    rows = connection.execute(
        _store_values._message_records_sql(where="m.session_key = ?", order_by="ORDER BY m.seq"),
        (state["session_key"],),
    ).fetchall()
    return [_store_codec.message_from_row(row) for row in rows]
