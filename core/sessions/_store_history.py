"""Bounded Message and history projections in a supplied snapshot."""

from __future__ import annotations

import bisect
import json
import sqlite3
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from core.chat.errors import ChatSessionError
from core.sessions import _store_codec, _store_timeline, _store_values
from core.sessions._io import _encode_chat_history_cursor
from core.sessions._types import (
    SKILL_CONTEXT_NOTE_PREFIX,
    SKILL_TOOL_MESSAGE_NAME,
    JsonObject,
    SessionChatHistorySnapshot,
    SessionMessagePage,
    SessionReadBatch,
)
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
        FROM history_records AS m
        JOIN assistant_messages AS a ON a.message_key = m.source_key
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


def _records_by_key(connection: sqlite3.Connection, keys: Sequence[int]) -> list[sqlite3.Row]:
    """Read full records for keys already selected from one Session, by sequence."""
    if not keys:
        return []
    return connection.execute(
        _store_values._message_records_sql(
            where=_store_values._KEYED_RECORDS, order_by="ORDER BY m.seq"
        ),
        (json.dumps(list(keys)),),
    ).fetchall()


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
            "SELECT m.seq FROM history_records AS m WHERE "
            + " AND ".join(clauses)
            + " AND m.seq = ?",
            (*params, before_sequence),
        ).fetchone()
        if before_row is None:
            raise SessionPageCursorError("before cursor is invalid")
        cutoff = int(before_row["seq"])
    elif before_message_id is not None:
        before_row = connection.execute(
            "SELECT m.seq FROM history_records AS m WHERE "
            + " AND ".join(clauses)
            + " AND m.message_id = ? ORDER BY m.seq LIMIT 1",
            (*params, before_message_id),
        ).fetchone()
        if before_row is None:
            raise SessionPageCursorError("before must reference an active message id")
        cutoff = int(before_row["seq"])

    if limit is None:
        rows = connection.execute(
            _store_values._message_records_sql(
                where=" AND ".join([*clauses, "m.seq < ?"]),
                order_by="ORDER BY m.seq",
            ),
            (*params, cutoff),
        ).fetchall()
    else:
        # Order and limit narrow keys first; only the page reads full records.
        keys = connection.execute(
            "SELECT m.seq, m.message_key, m.owner_run_id FROM history_records AS m WHERE "
            + " AND ".join([*clauses, "m.seq < ?"])
            + " ORDER BY m.seq DESC LIMIT ?",
            (*params, cutoff, limit),
        ).fetchall()
        if keys and complete_run_segment and keys[-1]["owner_run_id"] is not None:
            boundary = connection.execute(
                "SELECT start_sequence FROM runs WHERE session_key=? AND run_id=?",
                (session_key, keys[-1]["owner_run_id"]),
            ).fetchone()
            assert boundary is not None
            if int(boundary[0]) < int(keys[-1]["seq"]):
                keys = connection.execute(
                    "SELECT m.message_key FROM history_records AS m WHERE "
                    + " AND ".join([*clauses, "m.seq >= ?", "m.seq < ?"]),
                    (*params, int(boundary[0]), cutoff),
                ).fetchall()
        rows = _records_by_key(connection, [int(row["message_key"]) for row in keys])
    if not rows:
        return [], False, frozenset(), None

    page_floor = int(rows[0]["seq"])

    has_more = (
        connection.execute(
            "SELECT 1 FROM history_records AS m WHERE "
            + " AND ".join(clauses)
            + " AND m.seq < ? LIMIT 1",
            (*params, page_floor),
        ).fetchone()
        is not None
    )
    latest_takeover = connection.execute(
        "SELECT MAX(seq) FROM history_records WHERE session_key = ? AND active = 1 "
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
        FROM history_records AS m
        LEFT JOIN assistant_messages AS a ON a.message_key = m.source_key
        LEFT JOIN compaction_checkpoints AS c ON c.snapshot_key = m.message_key
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


def current_skill_activation_messages(
    connection: sqlite3.Connection, address: SessionAddress
) -> Callable[[], list[ChatMessage]]:
    """Load the active Skill activation candidates the current context can still see.

    Candidates are ``[skill-context]`` Notes and ``skill`` Tool Results after the
    newest active Compaction checkpoint; the caller decides which of them carry
    a valid activation. Rows a history edit deactivated never qualify.
    """
    state = _store_values._require_live(connection, address)
    checkpoint = connection.execute(
        "SELECT MAX(seq) FROM compaction_checkpoints WHERE session_key = ? AND active = 1",
        (state["session_key"],),
    ).fetchone()
    floor = -1 if checkpoint is None or checkpoint[0] is None else int(checkpoint[0])
    rows = connection.execute(
        _store_values._message_records_sql(
            where=(
                "m.session_key = ? AND m.active = 1 AND m.seq > ? "
                "AND m.role IN ('note', 'tool') "
                "AND (m.role = 'tool' OR substr(m.content, 1, ?) = ?) "
                "AND (m.role = 'note' OR t.name = ?)"
            ),
            order_by="ORDER BY m.seq",
        ),
        (
            state["session_key"],
            floor,
            len(SKILL_CONTEXT_NOTE_PREFIX),
            SKILL_CONTEXT_NOTE_PREFIX,
            SKILL_TOOL_MESSAGE_NAME,
        ),
    ).fetchall()
    return lambda: [_store_codec.message_from_row(row) for row in rows]


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
          WHERE visible_call.message_key = m.source_key
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
        "SELECT 1 FROM history_records WHERE session_key = ? AND active = 1 "
        "AND role = 'compaction_checkpoint' AND seq = ?",
        (state["session_key"], snapshot_sequence),
    ).fetchone()
    return checkpoint is not None


def active_messages(
    connection: sqlite3.Connection, address: SessionAddress
) -> Callable[[], list[ChatMessage]]:
    """Load the relationally materialized active lineage only."""
    state = _store_values._require_live(connection, address)
    rows = connection.execute(
        _store_values._message_records_sql(
            where="m.session_key = ? AND m.active = 1",
            order_by="ORDER BY m.seq",
        ),
        (state["session_key"],),
    ).fetchall()
    return lambda: [_store_codec.message_from_row(row) for row in rows]


def active_user_message_count(
    connection: sqlite3.Connection, address: SessionAddress, *, limit: int
) -> int:
    """Count at most ``limit`` active User Messages without loading their content."""
    if limit <= 0:
        raise ChatSessionError("user Message count limit must be positive")
    state = _store_values._require_live(connection, address)
    row = connection.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM history_records WHERE session_key = ? "
        "AND active = 1 AND role = 'user' LIMIT ?)",
        (state["session_key"], limit),
    ).fetchone()
    assert row is not None
    return int(row[0])


def latest_note(
    connection: sqlite3.Connection, address: SessionAddress, *, content_prefix: str
) -> Callable[[], ChatMessage | None]:
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
    return lambda: None if row is None else _store_codec.message_from_row(row)


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
    after: tuple[str, int] | None = None,
) -> Callable[[], SessionChatHistorySnapshot]:
    """Read one WebUI history projection from a single SQLite snapshot."""
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
        raise ChatSessionError("message page limit must be a positive integer")
    state = _store_values._require_live(connection, address)
    incremental = _store_timeline.can_append(connection, state, after)
    through = int(state["message_count"])
    if incremental:
        assert after is not None
        page_rows, through = _store_timeline.appended_rows(
            connection,
            state,
            sequence=after[1],
            limit=limit or 500,
            excluded_roles=excluded_roles,
        )
        has_more, page_floor = False, None
        editable_ids = frozenset(
            str(row["message_id"])
            for row in page_rows
            if row["role"] == "user" and row["content"] is not None and row["sender_id"] is None
        )
    else:
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
    generation = str(state["generation_id"])
    has_newer = through < int(state["message_count"])
    runs = _store_timeline.page_runs(
        connection, state, page_rows, through=through, incremental=incremental
    )

    def decode() -> SessionChatHistorySnapshot:
        return SessionChatHistorySnapshot(
            page=SessionMessagePage(
                messages=tuple(_store_codec.message_from_row(row) for row in page_rows),
                has_more=has_more,
                editable_message_ids=editable_ids,
                before_cursor=(
                    _encode_chat_history_cursor(generation, page_floor)
                    if has_more and page_floor is not None
                    else None
                ),
                record_sequences=tuple(int(row["seq"]) for row in page_rows),
                record_run_ids=_store_timeline.record_run_ids(page_rows),
            ),
            session_usage=usage,
            context_messages=tuple(_store_codec.message_from_row(row) for row in context_rows),
            background_messages=tuple(
                _store_codec.message_from_row(row) for row in background_rows
            ),
            generation_id=generation,
            after_cursor=_encode_chat_history_cursor(generation, through),
            incremental=incremental,
            has_newer=has_newer,
            runs=runs,
        )

    return decode


def status_snapshot(
    connection: sqlite3.Connection, address: SessionAddress
) -> Callable[[], tuple[str | None, int, JsonObject | None, JsonObject, int]]:
    state = _store_values._require_live(connection, address)
    session_key = int(state["session_key"])
    facts = connection.execute(
        "SELECT MIN(CASE WHEN seq = 0 THEN timestamp END) AS first_message_at, "
        "SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END) AS user_count "
        "FROM history_records WHERE session_key = ?",
        (session_key,),
    ).fetchone()
    latest_row = connection.execute(
        _store_values._message_records_sql(
            where=("m.session_key = ? AND m.role = 'assistant' AND a.usage_present = 1"),
            order_by="ORDER BY m.seq DESC LIMIT 1",
        ),
        (session_key,),
    ).fetchone()
    usage, cache_input_tokens = _session_usage_from_connection(connection, session_key)
    return lambda: (
        None if facts["first_message_at"] is None else str(facts["first_message_at"]),
        int(facts["user_count"] or 0),
        None if latest_row is None else _store_codec.message_from_row(latest_row).usage,
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
            "SELECT MAX(seq) FROM history_records WHERE session_key = ? AND active = 1 "
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
        "FROM history_records WHERE session_key = ? AND active = 1 "
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
) -> Callable[[], list[tuple[int, ChatMessage]] | None]:
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
        return lambda: None
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
    return lambda: [(int(row["seq"]), _store_codec.message_from_row(row)) for row in rows]


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
    if not sections:
        return result
    # One pass over the covering range; each section is a slice of its sequences.
    rows = connection.execute(
        "SELECT m.seq, m.timestamp FROM history_records AS m "
        f"{_store_values._MESSAGE_RECORD_JOINS} WHERE m.session_key = ? AND m.active = 1 "
        "AND m.seq > ? AND m.seq < ? AND " + record_filter + " ORDER BY m.seq",
        (
            state["session_key"],
            min(lower for lower, _upper in sections),
            max(upper for _lower, upper in sections),
            *filter_params,
        ),
    ).fetchall()
    sequences = [int(row["seq"]) for row in rows]
    for lower_sequence, upper_sequence in sections:
        start = bisect.bisect_right(sequences, lower_sequence)
        end = bisect.bisect_left(sequences, upper_sequence)
        if start >= end:
            result[upper_sequence] = (0, None, None)
            continue
        first, last = rows[start]["timestamp"], rows[end - 1]["timestamp"]
        result[upper_sequence] = (
            end - start,
            None if first is None else str(first),
            None if last is None else str(last),
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
) -> Callable[[], tuple[bool, list[tuple[int, ChatMessage]]] | None]:
    """Read a bounded eligible neighborhood around the earliest matching public id."""
    record_filter, filter_params = _history_record_filter(roles, excluded_tool_name)
    state = _store_values._require_live(connection, address)
    if not _history_snapshot_is_current(
        connection,
        state,
        expected_generation_id=expected_generation_id,
        snapshot_sequence=snapshot_sequence,
    ):
        return lambda: None
    exists = (
        connection.execute(
            "SELECT 1 FROM history_records WHERE session_key = ? AND active = 1 "
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
        return lambda: (exists, [])
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
    return lambda: (exists, [(int(row["seq"]), _store_codec.message_from_row(row)) for row in rows])


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
        JOIN runs AS r ON r.run_key = (
          SELECT run_key FROM runs
          WHERE session_key=s.session_key AND status<>'running' AND origin_generation_id IS NULL
          ORDER BY start_sequence,run_key LIMIT 1
        )
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


def run_messages(
    connection: sqlite3.Connection, address: SessionAddress, run_id: str
) -> Callable[[], list[ChatMessage]]:
    state = _store_values._require_live(connection, address)
    rows = connection.execute(
        _store_values._message_records_sql(
            where="m.session_key=? AND m.owner_run_id=? AND m.active=1",
            order_by="ORDER BY m.seq",
        ),
        (state["session_key"], run_id),
    ).fetchall()
    return lambda: [_store_codec.message_from_row(row) for row in rows]


def run_summary(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    run_id: str | None = None,
    work_id: str | None = None,
) -> Callable[[], ChatMessage | None]:
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
    return lambda: None if row is None else _store_codec.message_from_row(row)


def run_result(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    run_id: str | None = None,
    work_id: str | None = None,
    require_latest: bool = False,
) -> Callable[[], tuple[ChatMessage | None, ChatMessage, str | None] | None]:
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
        return lambda: None
    if require_latest:
        latest = connection.execute(
            "SELECT run_id FROM runs WHERE session_key=? ORDER BY run_key DESC LIMIT 1",
            (state["session_key"],),
        ).fetchone()
        if latest is None or latest["run_id"] != summary_row["run_id"]:
            return lambda: None
    assistant_row = connection.execute(
        _store_values._message_records_sql(
            where=(
                "m.session_key = ? AND m.owner_run_id = ? "
                "AND m.role = 'assistant' AND (NULLIF(m.content, '') IS NOT NULL "
                "OR (m.content_blocks_json IS NOT NULL "
                "AND json_array_length(m.content_blocks_json) > 0))"
            ),
            order_by="ORDER BY m.seq DESC LIMIT 1",
        ),
        (state["session_key"], summary_row["run_id"]),
    ).fetchone()
    latest_tool = connection.execute(
        "SELECT tc.name FROM history_records AS m "
        "JOIN tool_calls AS tc ON tc.message_key = m.source_key "
        "WHERE m.session_key = ? AND m.owner_run_id = ? "
        "ORDER BY m.seq DESC, tc.ordinal DESC LIMIT 1",
        (state["session_key"], summary_row["run_id"]),
    ).fetchone()
    return lambda: (
        None if assistant_row is None else _store_codec.message_from_row(assistant_row),
        _store_codec.message_from_row(summary_row),
        None if latest_tool is None else str(latest_tool["name"]),
    )


def message_rows_since(
    connection: sqlite3.Connection, address: SessionAddress, cursor: SessionReadCursor | None
) -> tuple[list[sqlite3.Row], SessionReadCursor] | None:
    """Select the records after *cursor* without decoding them.

    A writer can select inside its transaction and decode after commit, so
    Message reconstruction never extends the write lock.
    """
    from core.sessions._types import SessionReadCursor

    state = _store_values._require_live(connection, address)
    count = int(state["message_count"])
    revision = int(state["history_revision"])
    generation_id = str(state["generation_id"])
    last_id = state["last_message_id"]
    current = SessionReadCursor(generation_id, revision, count, count, last_id)
    if cursor is None:
        start = 0
    else:
        if cursor.generation_id != generation_id or not 0 <= cursor.next_seq <= count:
            return None
        if cursor.next_seq < int(state["history_reset_sequence"]):
            return None
        if cursor.next_seq == count and cursor.last_message_id == last_id:
            # The Session row names its newest record, so a current cursor needs no read.
            return [], current
        start = cursor.next_seq
    # One read from the anchor record, which is the one before the cursor.
    rows = connection.execute(
        _store_values._message_records_sql(
            where="m.session_key = ? AND m.seq >= ?",
            order_by="ORDER BY m.seq",
        ),
        (state["session_key"], max(start - 1, 0)),
    ).fetchall()
    if cursor is not None:
        anchor_id = None
        if start > 0 and rows and int(rows[0]["seq"]) == start - 1:
            anchor_id = rows.pop(0)["message_id"]
        if anchor_id != cursor.last_message_id:
            return None
    return rows, current


def read_batch(delta: tuple[list[sqlite3.Row], SessionReadCursor]) -> SessionReadBatch:
    rows, cursor = delta
    messages = tuple(_store_codec.message_from_row(row) for row in rows)
    return SessionReadBatch(
        messages,
        cursor,
        tuple(message for row, message in zip(rows, messages, strict=True) if row["active"]),
    )


def bookend_timestamps(
    connection: sqlite3.Connection, address: SessionAddress
) -> tuple[str, str] | None:
    state = _store_values._require_live(connection, address)
    if int(state["message_count"]) == 0:
        return None
    first = connection.execute(
        "SELECT timestamp FROM history_records WHERE session_key = ? AND seq = 0",
        (state["session_key"],),
    ).fetchone()
    if first is None or state["last_message_at"] is None:
        raise SessionStoreCorruptError(f"invalid Session message summary: {address.session_id}")
    return str(first["timestamp"]), str(state["last_message_at"])


def messages(
    connection: sqlite3.Connection, address: SessionAddress
) -> Callable[[], list[ChatMessage]]:
    state = _store_values._require_live(connection, address)
    rows = connection.execute(
        _store_values._message_records_sql(where="m.session_key = ?", order_by="ORDER BY m.seq"),
        (state["session_key"],),
    ).fetchall()
    return lambda: [_store_codec.message_from_row(row) for row in rows]


def recall_context(
    connection: sqlite3.Connection, address: SessionAddress, message_id: str
) -> list[JsonObject]:
    """Read the enclosing question and final answer without loading a transcript.

    Only active conversation text is projected. The caller already has the hit;
    do not repeat it or hydrate Tool graphs. A deleted/edited-away anchor returns
    no context rather than borrowing a different conversation block.
    """
    # The scalar Session key reaches every view branch; a join would not.
    anchor = connection.execute(
        "SELECT m.session_key, m.seq, m.role FROM history_records AS m "
        "WHERE m.session_key = (SELECT session_key FROM sessions WHERE project_id = ? "
        "AND agent_id = ? AND session_id = ? AND status = 'live') AND m.active = 1 "
        "AND m.message_id = ? ORDER BY m.seq DESC LIMIT 1",
        (*_store_values._scope(address), message_id),
    ).fetchone()
    if anchor is None or anchor["role"] not in {"user", "assistant"}:
        return []
    key, seq = int(anchor["session_key"]), int(anchor["seq"])
    bounds = connection.execute(
        "SELECT (SELECT MAX(seq) FROM history_records WHERE session_key = ? AND active = 1 "
        "AND role = 'user' AND seq <= ?) AS first, "
        "(SELECT MIN(seq) FROM history_records WHERE session_key = ? AND active = 1 "
        "AND role = 'user' AND seq > ?) AS following",
        (key, seq, key, seq),
    ).fetchone()
    first = bounds["first"]
    if first is None:
        return []
    following = bounds["following"]
    answer = connection.execute(
        "SELECT MAX(seq) FROM history_records WHERE session_key = ? AND active = 1 "
        "AND role = 'assistant' AND seq > ? AND (? IS NULL OR seq < ?) "
        "AND length(COALESCE(content, content_search, '')) > 0",
        (key, first, following, following),
    ).fetchone()[0]
    # Two narrow row lookups; substr bounds the text before it leaves SQLite.
    rows = connection.execute(
        "SELECT seq, message_id, role, timestamp, "
        "substr(COALESCE(content, content_search, ''), 1, 801) AS text "
        "FROM history_records WHERE session_key = ? AND active = 1 AND seq != ? "
        "AND seq IN (?, ?) ORDER BY seq",
        (key, seq, first, answer),
    ).fetchall()
    return [
        {
            "message_index": int(row["seq"]),
            "message_id": str(row["message_id"]),
            "role": str(row["role"]),
            "timestamp": str(row["timestamp"]),
            "text": str(row["text"])[:800],
            "truncated": len(str(row["text"])) > 800,
        }
        for row in rows
        if row["text"]
    ]
