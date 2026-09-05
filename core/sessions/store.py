"""Transactional SQLite persistence hidden behind the Session domain."""
# ruff: noqa: E501

from __future__ import annotations

import builtins
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import contextmanager, suppress
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.chat.errors import ChatSessionError
from core.sessions.errors import (
    FtsHealth,
    QuarantineResult,
    SessionNotFoundError,
    SessionPageCursorError,
    SessionStorageFormatError,
    SessionStoreCorruptError,
    SessionStoreHealth,
    SessionStoreSchemaMismatchError,
    SessionStoreUnavailableError,
)
from core.sessions.format import (
    MARKER_STATE_BOOTSTRAP,
    publish_ready_marker,
    read_session_store_marker,
    validate_session_store_paths,
)
from core.sessions.schema import (
    APPLICATION_ID,
    DATABASE_ID_META_KEY,
    FTS_COMPLETED_HIGH_WATER_KEY,
    FTS_DEGRADED_REASON_KEY,
    FTS_GENERATION_KEY,
    FTS_SQL,
    FTS_SQL_FALLBACK,
    FTS_STALE_KEY,
    FTS_STORAGE_VERSION,
    FTS_STORAGE_VERSION_KEY,
    FTS_TABLE,
    FTS_TARGET_HIGH_WATER_KEY,
    FTS_TRIGGERS,
    FTS_TRIGRAM_TABLE,
    FTS_TRIGRAM_VIEW,
    FTS_VIEW,
    SCHEMA_CONVERSION_FLOOR,
    SCHEMA_VERSION,
    reconcile_schema,
)
from core.sessions.sqlite_runtime import (
    ACTIVITY_WRITE_PATIENCE_S,
    TRANSCRIPT_WRITE_PATIENCE_S,
    WRITE_PATIENCE_S,
    SQLiteRuntime,
    classify_write_error,
)

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions.sessions import SessionAddress, SessionReadCursor

_LOGGER = logging.getLogger("vbot.sessions")
_DESCRIPTOR_SOURCE_BATCH_SIZE = 900

JsonObject = dict[str, Any]

_SESSION_METADATA_PROJECTION_VERSION_KEY = "session_metadata_projection_version"
_SESSION_METADATA_PROJECTION_VERSION = "3"
_SESSION_METADATA_SCALAR_COLUMNS = (
    ("title", "title"),
    ("auto_title", "auto_title"),
    ("source_channel_id", "source_channel_id"),
    ("platform", "platform"),
    ("platform_conv_id", "platform_conv_id"),
)
_SESSION_METADATA_JSON_COLUMNS = (
    ("subagent_parent", "subagent_parent_json", dict),
    ("fork_source", "fork_source_json", dict),
    ("run_kinds", "run_kinds_json", list),
    ("compaction_policy", "compaction_policy_json", dict),
)
_SESSION_METADATA_PROJECTION_COLUMNS = (
    "title",
    "auto_title",
    "source_channel_id",
    "platform",
    "platform_conv_id",
    "is_subagent_session",
    "subagent_parent_json",
    "fork_source_json",
    "run_kinds_json",
    "compaction_policy_json",
    "list_visibility_mask",
)
_SESSION_LIST_COLUMNS = """
    project_id,
    agent_id,
    session_id,
    created_at,
    COALESCE(last_message_at, created_at) AS last_active_at,
    active_sort,
    title,
    auto_title,
    source_channel_id,
    platform,
    platform_conv_id,
    is_subagent_session,
    subagent_parent_json,
    fork_source_json,
    run_kinds_json,
    compaction_policy_json,
    latest_completion_run_id,
    latest_completion_status,
    latest_completion_at,
    read_completion_run_id
"""
_SESSION_LIST_BACKGROUND_KINDS = (
    "cron",
    "reflection",
    "memory_reflection",
    "skill_reflection",
)
_RECALL_VALID_RUN_KINDS = (
    "user",
    "channel",
    "cron",
    "reflection",
    "memory_reflection",
    "skill_reflection",
    "subagent",
    "system",
)
_RECALL_REFLECTION_RUN_KINDS = (
    "reflection",
    "memory_reflection",
    "skill_reflection",
)
_RECALL_USER_FACING_RUN_KINDS = ("user", "channel", "cron")
_RECALL_PERIOD_ROLES = ("user", "assistant", "error", "compaction_checkpoint")
_SUMMARY_METADATA_COLUMNS = {"seen_skills": "$.seen_skills"}
_LIST_VISIBILITY_SUBAGENT_SESSION = 1 << 0
_LIST_VISIBILITY_BACKGROUND = 1 << 1
_LIST_VISIBILITY_CRON = 1 << 2
_LIST_VISIBILITY_MEMORY_REFLECTION = 1 << 3
_LIST_VISIBILITY_SKILL_REFLECTION = 1 << 4
_LIST_VISIBILITY_REFLECTION = 1 << 5
_LIST_VISIBILITY_VALID_RUN_KINDS = 1 << 6
_LIST_VISIBILITY_USER_FACING = 1 << 7
_LIST_VISIBILITY_SUBAGENT_RUN_KIND = 1 << 8
_LIST_VISIBILITY_SUBAGENT_PARENT = 1 << 9


def _session_list_visibility_mask(metadata: JsonObject) -> int:
    mask = 0
    if metadata.get("is_subagent_session") is True:
        mask |= _LIST_VISIBILITY_SUBAGENT_SESSION
    if isinstance(metadata.get("subagent_parent"), dict):
        mask |= _LIST_VISIBILITY_SUBAGENT_PARENT

    run_kinds = metadata.get("run_kinds")
    valid_run_kinds = (
        isinstance(run_kinds, list)
        and bool(run_kinds)
        and all(isinstance(kind, str) and kind in _RECALL_VALID_RUN_KINDS for kind in run_kinds)
    )
    if not valid_run_kinds:
        return mask
    mask |= _LIST_VISIBILITY_VALID_RUN_KINDS
    kinds = set(cast(list[str], run_kinds))
    if kinds & set(_RECALL_REFLECTION_RUN_KINDS):
        mask |= _LIST_VISIBILITY_REFLECTION
    if kinds & set(_RECALL_USER_FACING_RUN_KINDS):
        mask |= _LIST_VISIBILITY_USER_FACING
    if "cron" in kinds:
        mask |= _LIST_VISIBILITY_CRON
    if "memory_reflection" in kinds:
        mask |= _LIST_VISIBILITY_MEMORY_REFLECTION
    if "skill_reflection" in kinds:
        mask |= _LIST_VISIBILITY_SKILL_REFLECTION
    if "reflection" in kinds:
        mask |= _LIST_VISIBILITY_REFLECTION
    if "subagent" in kinds:
        mask |= _LIST_VISIBILITY_SUBAGENT_RUN_KIND

    platform = metadata.get("platform")
    platform_conversation = metadata.get("platform_conv_id")
    is_channel = (
        isinstance(platform, str)
        and bool(platform.strip())
        and isinstance(platform_conversation, str)
        and bool(platform_conversation.strip())
    )
    if not is_channel and kinds <= set(_SESSION_LIST_BACKGROUND_KINDS):
        mask |= _LIST_VISIBILITY_BACKGROUND
    return mask


def _session_metadata_storage(metadata: JsonObject) -> tuple[str, tuple[Any, ...]]:
    """Separate indexed/listable metadata from the open-ended metadata object."""
    residual = dict(metadata)
    columns: dict[str, Any] = dict.fromkeys(_SESSION_METADATA_PROJECTION_COLUMNS)
    columns["list_visibility_mask"] = _session_list_visibility_mask(metadata)
    for key, column in _SESSION_METADATA_SCALAR_COLUMNS:
        value = residual.get(key)
        if isinstance(value, str):
            columns[column] = value
            residual.pop(key)
    subagent_flag = residual.get("is_subagent_session")
    if isinstance(subagent_flag, bool):
        columns["is_subagent_session"] = int(subagent_flag)
        residual.pop("is_subagent_session")
    for key, column, expected_type in _SESSION_METADATA_JSON_COLUMNS:
        value = residual.get(key)
        if isinstance(value, expected_type):
            columns[column] = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            residual.pop(key)
    return _json_object(residual, "session metadata"), tuple(
        columns[column] for column in _SESSION_METADATA_PROJECTION_COLUMNS
    )


def _session_projected_metadata_from_state(state: Any) -> JsonObject:
    metadata: JsonObject = {}
    for key, column in _SESSION_METADATA_SCALAR_COLUMNS:
        value = state[column]
        if value is not None:
            metadata[key] = str(value)
    subagent_flag = state["is_subagent_session"]
    if subagent_flag is not None:
        metadata["is_subagent_session"] = bool(subagent_flag)
    for key, column, expected_type in _SESSION_METADATA_JSON_COLUMNS:
        payload = state[column]
        if payload is None:
            continue
        value = _json_value_from_payload(payload, f"Session {key}", expected_type)
        metadata[key] = value
    return metadata


def _session_metadata_from_state(state: Any) -> JsonObject:
    metadata = _json_from_payload(state["metadata_json"], "session metadata")
    metadata.update(_session_projected_metadata_from_state(state))
    return metadata


def _session_list_visibility_sql(
    *,
    include_subagents: bool,
    include_memory_reflections: bool,
    include_skill_reflections: bool,
    include_cron: bool,
) -> tuple[str, list[Any]]:
    is_subagent = (
        "((list_visibility_mask & "
        f"{_LIST_VISIBILITY_SUBAGENT_SESSION | _LIST_VISIBILITY_SUBAGENT_PARENT}) != 0)"
    )
    is_background = f"((list_visibility_mask & {_LIST_VISIBILITY_BACKGROUND}) != 0)"
    background_enabled = (
        f"((list_visibility_mask & {_LIST_VISIBILITY_CRON}) = 0 OR ? = 1) "
        f"AND ((list_visibility_mask & {_LIST_VISIBILITY_MEMORY_REFLECTION}) = 0 OR ? = 1) "
        f"AND ((list_visibility_mask & {_LIST_VISIBILITY_SKILL_REFLECTION}) = 0 OR ? = 1) "
        f"AND ((list_visibility_mask & {_LIST_VISIBILITY_REFLECTION}) = 0 OR (? = 1 OR ? = 1))"
    )
    visible = (
        f"(({is_subagent} AND ? = 1) OR (NOT {is_subagent} AND "
        f"(NOT {is_background} OR ({background_enabled}))))"
    )
    params: list[Any] = [
        int(include_subagents),
        int(include_cron),
        int(include_memory_reflections),
        int(include_skill_reflections),
        int(include_memory_reflections),
        int(include_skill_reflections),
    ]
    return visible, params


_MESSAGE_INSERT = """
    INSERT INTO messages (
        session_key, seq, message_id, role, timestamp, content,
        content_blocks_json, content_search, model, active, searchable
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_MESSAGE_RECORD_COLUMNS = """
    m.*,
    a.reasoning,
    a.reasoning_meta_json,
    a.reasoning_scope,
    a.reasoning_started_at,
    a.reasoning_completed_at,
    a.reasoning_duration_ms,
    a.reasoning_timing_extra_json,
    a.phase,
    a.input_tokens,
    a.output_tokens,
    a.cache_read_tokens,
    a.cache_write_tokens,
    a.reasoning_tokens,
    a.usage_estimated,
    a.input_tokens_estimated,
    a.output_tokens_estimated,
    a.usage_present,
    a.usage_extra_json,
    a.tool_calls_present,
    a.interrupted,
    a.interruption_cause,
    t.tool_call_key,
    t.tool_call_id,
    t.name,
    t.result_content AS role_content,
    t.started_at AS timing_started_at,
    t.completed_at AS timing_completed_at,
    t.duration_ms AS timing_duration_ms,
    t.timing_extra_json,
    t.display_json AS tool_display_json,
    u.sender_id,
    u.display_name AS sender_display_name,
    u.role AS sender_role,
    e.error_kind,
    c.tail_boundary_id,
    c.projection_json,
    c.policy AS compaction_policy,
    c.strategy AS compaction_strategy,
    c.compacted_token_count,
    c.context_tokens_before,
    c.context_tokens_after,
    c.compaction_duration_ms,
    c.usage_present AS compaction_usage_present,
    c.usage_extra_json AS compaction_usage_extra_json,
    r.run_id,
    r.work_id,
    r.status,
    r.started_at AS run_started_at,
    r.completed_at AS run_completed_at,
    r.duration_ms AS run_duration_ms,
    r.timing_extra_json AS run_timing_extra_json,
    r.iteration_count,
    r.changed_files,
    r.lines_added,
    r.lines_removed,
    r.change_stats_extra_json,
    h.target_message_id,
    CASE WHEN EXISTS (SELECT 1 FROM tool_calls AS tc WHERE tc.message_key = m.message_key)
         THEN (SELECT json_group_array(json_array(
                    ordered.tool_call_id,
                    ordered.name,
                    ordered.arguments_json,
                    ordered.rejection_code,
                    ordered.rejection_message,
                    ordered.rejection_fingerprint,
                    ordered.argument_sequence_index,
                    ordered.argument_sequence_length
                ))
               FROM (SELECT * FROM tool_calls WHERE message_key = m.message_key ORDER BY ordinal) AS ordered)
         ELSE NULL END AS tool_call_rows_json,
    CASE WHEN EXISTS (SELECT 1 FROM assistant_output_files AS f WHERE f.message_key = m.message_key)
         THEN (SELECT json_group_array(json_array(
                    ordered.path,
                    ordered.line_index,
                    ordered.start_index,
                    ordered.end_index
                ))
               FROM (SELECT * FROM assistant_output_files WHERE message_key = m.message_key ORDER BY ordinal) AS ordered)
         ELSE NULL END AS output_file_rows_json,
    CASE WHEN EXISTS (SELECT 1 FROM run_change_paths AS p WHERE p.message_key = m.message_key)
         THEN (SELECT json_group_array(ordered.path)
               FROM (SELECT path FROM run_change_paths WHERE message_key = m.message_key ORDER BY ordinal) AS ordered)
         ELSE NULL END AS change_paths_json
"""

_MESSAGE_RECORD_JOINS = """
    LEFT JOIN assistant_messages AS a ON a.message_key = m.message_key
    LEFT JOIN tool_messages AS t ON t.message_key = m.message_key
    LEFT JOIN user_message_senders AS u ON u.message_key = m.message_key
    LEFT JOIN error_messages AS e ON e.message_key = m.message_key
    LEFT JOIN compaction_checkpoints AS c ON c.message_key = m.message_key
    LEFT JOIN run_summaries AS r ON r.message_key = m.message_key
    LEFT JOIN history_edits AS h ON h.message_key = m.message_key
"""


def _message_records_sql(*, where: str, order_by: str = "") -> str:
    return (
        f"SELECT {_MESSAGE_RECORD_COLUMNS} FROM messages AS m "
        f"{_MESSAGE_RECORD_JOINS} WHERE {where} {order_by}"
    )


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
    sql = _message_records_sql(
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
                _message_records_sql(
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
        _message_records_sql(where=where, order_by="ORDER BY m.seq"),
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
        _message_records_sql(
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


_FTS_BATCH_SIZE = 100
_FTS_REBUILD_THROTTLE_S = 0.01
_OFFLINE_IMPORT_CACHE_KIB = 262_144
_SEARCH_RESULT_LIMIT = 1_000
_CANONICAL_SEARCH_SCAN_LIMIT = 10_000
_FTS_REBUILD_HOOK: Callable[[str, int], None] | None = None
_CONTINUATION_RECORD_VERSION = 1
_CONTINUATION_SYNTHETIC_TIMESTAMP = "1970-01-01T00:00:00+00:00"


class _FtsSearchRows(builtins.list[tuple["SessionAddress", str, str, str, float]]):
    """List-compatible search rows with internal fallback coverage metadata."""

    def __init__(
        self,
        rows: Sequence[tuple[SessionAddress, str, str, str, float]] = (),
        *,
        source: str,
        complete: bool = True,
        fallback_reason: str | None = None,
    ) -> None:
        super().__init__(rows)
        self.source = source
        self.complete = complete
        self.fallback_reason = fallback_reason


def _fts_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _set_fts_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute("INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)", (key, value))


def _search_projection(message: ChatMessage) -> str:
    """Build the Recall-owned text projection from one canonical Message."""
    from core.recall.canonical import (
        SESSION_RECALL_CONVERSATION_ROLES,
        is_recall_artifact_message,
        message_search_text,
    )
    from core.sessions.sessions import is_skill_context_note

    if (
        message.role not in SESSION_RECALL_CONVERSATION_ROLES
        or is_recall_artifact_message(message)
        or is_skill_context_note(message)
    ):
        return ""
    return message_search_text(message)


def _message_is_searchable(message: ChatMessage) -> bool:
    """Test index eligibility without materializing large Message search text."""
    from core.recall.canonical import (
        SESSION_RECALL_CONVERSATION_ROLES,
        is_recall_artifact_message,
    )
    from core.sessions.sessions import is_skill_context_note

    if (
        message.role not in SESSION_RECALL_CONVERSATION_ROLES
        or is_recall_artifact_message(message)
        or is_skill_context_note(message)
    ):
        return False
    return bool(
        message.content
        or message.reasoning
        or message.name
        or message.error_kind
        or message.tool_calls
    )


def _fts_table_exists(connection: sqlite3.Connection, table: str = FTS_TABLE) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _drop_fts_triggers(connection: sqlite3.Connection) -> None:
    for trigger in FTS_TRIGGERS:
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _drop_fts(connection: sqlite3.Connection) -> None:
    _drop_fts_triggers(connection)
    connection.execute(f"DROP TABLE IF EXISTS {FTS_TRIGRAM_TABLE}")
    connection.execute(f"DROP VIEW IF EXISTS {FTS_TRIGRAM_VIEW}")
    connection.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
    connection.execute(f"DROP VIEW IF EXISTS {FTS_VIEW}")


def _fts_coverage_ok(
    connection: sqlite3.Connection, *, verify_internal_index: bool = True
) -> tuple[bool, str | None]:
    if not _fts_table_exists(connection) or not _fts_table_exists(connection, FTS_TRIGRAM_TABLE):
        return False, "FTS tables are missing"
    missing_base = connection.execute(
        """
        SELECT 1
        FROM messages AS m
        LEFT JOIN messages_fts_docsize AS indexed ON indexed.id = m.message_key
        WHERE (m.searchable = 1 AND m.active = 1 AND indexed.id IS NULL)
           OR ((m.searchable = 0 OR m.active = 0) AND indexed.id IS NOT NULL)
        LIMIT 1
        """
    ).fetchone()
    if missing_base is not None:
        return False, "canonical Messages are missing base FTS rows"
    invalid_trigram = connection.execute(
        """
        SELECT 1
        FROM messages AS m
        LEFT JOIN messages_fts_trigram_docsize AS indexed ON indexed.id = m.message_key
        WHERE (m.searchable = 1 AND m.active = 1 AND m.role <> 'tool' AND indexed.id IS NULL)
           OR ((m.searchable = 0 OR m.active = 0 OR m.role = 'tool') AND indexed.id IS NOT NULL)
        LIMIT 1
        """
    ).fetchone()
    if invalid_trigram is not None:
        return False, "trigram FTS coverage does not match non-Tool Messages"
    if verify_internal_index:
        try:
            connection.execute(
                "INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)"
            )
            connection.execute(
                "INSERT INTO messages_fts_trigram(messages_fts_trigram, rank) VALUES('integrity-check', 1)"
            )
        except sqlite3.DatabaseError:
            return False, "FTS index does not match canonical Messages"
    return True, None


def _fts_health_from_connection(
    connection: sqlite3.Connection, *, verify_coverage: bool
) -> FtsHealth:
    """Read FTS lifecycle state, optionally proving canonical row coverage."""
    if not _fts_table_exists(connection) or not _fts_table_exists(connection, FTS_TRIGRAM_TABLE):
        return FtsHealth(state="unavailable", reason="FTS tables are missing")
    storage_version = _fts_meta(connection, FTS_STORAGE_VERSION_KEY)
    generation = _fts_meta(connection, FTS_GENERATION_KEY)
    target = _fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY)
    completed = _fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY)
    stale = _fts_meta(connection, FTS_STALE_KEY)
    reason = _fts_meta(connection, FTS_DEGRADED_REASON_KEY)
    if storage_version != str(FTS_STORAGE_VERSION):
        return FtsHealth(
            state="degraded",
            reason="FTS storage version is missing or unsupported",
            generation=generation,
        )
    if not generation:
        return FtsHealth(state="degraded", reason="FTS rebuild generation is missing")
    try:
        target_value = int(target) if target is not None else -1
        completed_value = int(completed) if completed is not None else -1
    except (TypeError, ValueError):
        return FtsHealth(
            state="degraded",
            reason="FTS high-water metadata is malformed",
            generation=generation,
        )
    if target_value < 0 or completed_value < 0 or completed_value > target_value:
        return FtsHealth(
            state="degraded",
            reason="FTS high-water metadata is invalid",
            generation=generation,
            target_high_water=target_value,
            completed_high_water=completed_value,
        )
    coverage_ok = True
    coverage_reason = None
    if verify_coverage:
        coverage_ok, coverage_reason = _fts_coverage_ok(connection, verify_internal_index=False)
    if stale is not None or completed_value != target_value or not coverage_ok:
        rebuilding = stale == "rebuilding" or completed_value != target_value
        return FtsHealth(
            state="rebuilding" if rebuilding else "degraded",
            reason=reason or stale or coverage_reason or "FTS coverage is incomplete",
            generation=generation,
            target_high_water=target_value,
            completed_high_water=completed_value,
        )
    return FtsHealth(
        state="healthy",
        generation=generation,
        target_high_water=target_value,
        completed_high_water=completed_value,
    )


def _fts_rebuild_boundary(stage: str, high_water: int) -> None:
    if _FTS_REBUILD_HOOK is not None:
        _FTS_REBUILD_HOOK(stage, high_water)


def _ensure_fts_schema(connection: sqlite3.Connection) -> None:
    """Create or repair the derived FTS projection without weakening canonical storage."""
    try:
        health = _fts_health_from_connection(connection, verify_coverage=True)
        if health.available:
            return
        if (
            health.state == "rebuilding"
            and _fts_meta(connection, FTS_STORAGE_VERSION_KEY) == str(FTS_STORAGE_VERSION)
            and _fts_table_exists(connection)
            and _fts_table_exists(connection, FTS_TRIGRAM_TABLE)
        ):
            _backfill_fts(connection)
            _finish_fts_rebuild(connection)
            return
        if _fts_table_exists(connection) or _fts_table_exists(connection, FTS_TRIGRAM_TABLE):
            _drop_fts(connection)
        try:
            connection.executescript("BEGIN IMMEDIATE;\n" + FTS_SQL)
        except sqlite3.Error:
            with suppress(sqlite3.Error):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            _drop_fts(connection)
            connection.executescript("BEGIN IMMEDIATE;\n" + FTS_SQL_FALLBACK)
        _set_fts_meta(connection, FTS_STORAGE_VERSION_KEY, str(FTS_STORAGE_VERSION))
        _set_fts_meta(connection, FTS_GENERATION_KEY, uuid.uuid4().hex)
        _set_fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY, "0")
        _set_fts_meta(connection, FTS_STALE_KEY, "rebuilding")
        _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, "FTS rebuild in progress")
        target = int(
            connection.execute("SELECT COALESCE(MAX(message_key), 0) FROM messages").fetchone()[0]
        )
        _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, str(target))
        if target == 0:
            coverage_ok, coverage_reason = _fts_coverage_ok(connection)
            if not coverage_ok:
                raise sqlite3.DatabaseError(
                    coverage_reason or "empty FTS bootstrap failed its coverage check"
                )
            _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, "")
            connection.execute("DELETE FROM store_meta WHERE key = ?", (FTS_STALE_KEY,))
            connection.execute("COMMIT")
            return
        connection.execute("COMMIT")
        _backfill_fts(connection)
        _finish_fts_rebuild(connection)
    except sqlite3.Error as exc:
        with suppress(sqlite3.Error):
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        try:
            connection.execute("BEGIN IMMEDIATE")
            _set_fts_meta(connection, FTS_STALE_KEY, "1")
            _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, f"FTS unavailable: {exc}")
            connection.execute("COMMIT")
        except sqlite3.Error:
            with suppress(sqlite3.Error):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
        _LOGGER.warning("Session FTS unavailable, using canonical scan: %s", exc)


def _backfill_fts(connection: sqlite3.Connection) -> None:
    """Populate both external-content indexes in committed bounded batches."""
    generation = _fts_meta(connection, FTS_GENERATION_KEY) or uuid.uuid4().hex
    _set_fts_meta(connection, FTS_GENERATION_KEY, generation)
    previous = _fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY)
    try:
        completed = max(0, int(previous)) if previous is not None else 0
    except ValueError:
        completed = 0
    target = int(
        connection.execute("SELECT COALESCE(MAX(message_key), 0) FROM messages").fetchone()[0]
    )
    _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, str(target))
    _set_fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY, str(completed))
    connection.commit()
    while True:
        target = max(
            target,
            int(
                connection.execute("SELECT COALESCE(MAX(message_key), 0) FROM messages").fetchone()[
                    0
                ]
            ),
        )
        _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, str(target))
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute(
                """
                SELECT m.message_key, m.role, m.searchable, m.active,
                       source.content, source.content_search, source.reasoning,
                       source.name, source.error_kind, source.tool_calls
                FROM messages AS m
                LEFT JOIN messages_fts_source AS source
                  ON source.message_key = m.message_key
                WHERE m.message_key > ? AND m.message_key <= ?
                ORDER BY m.message_key
                LIMIT ?
                """,
                (completed, target, _FTS_BATCH_SIZE),
            ).fetchall()
            if not rows:
                connection.execute("COMMIT")
                break
            batch_max = int(rows[-1][0])
            _fts_rebuild_boundary("before_batch_commit", batch_max)
            for row in rows:
                values = (
                    row["content"],
                    row["content_search"],
                    row["reasoning"],
                    row["name"],
                    row["error_kind"],
                    row["tool_calls"],
                )
                if bool(row["searchable"]) and bool(row["active"]):
                    connection.execute(
                        "INSERT INTO messages_fts(rowid, content, content_search, reasoning, name, error_kind, tool_calls) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (int(row["message_key"]), *values),
                    )
                if bool(row["searchable"]) and bool(row["active"]) and row["role"] != "tool":
                    connection.execute(
                        "INSERT INTO messages_fts_trigram(rowid, content, content_search, name, error_kind, tool_calls) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            int(row["message_key"]),
                            row["content"],
                            row["content_search"],
                            row["name"],
                            row["error_kind"],
                            row["tool_calls"],
                        ),
                    )
            _set_fts_meta(
                connection,
                FTS_COMPLETED_HIGH_WATER_KEY,
                str(max(completed, batch_max)),
            )
            connection.execute("COMMIT")
            completed = max(completed, batch_max)
            _fts_rebuild_boundary("after_batch_commit", batch_max)
        except BaseException:
            with suppress(BaseException):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            raise
        time.sleep(_FTS_REBUILD_THROTTLE_S)


def _finish_fts_rebuild(connection: sqlite3.Connection) -> None:
    """Rebuild FTS rows and clear stale only after explicit coverage checks."""
    coverage_ok, coverage_reason = _fts_coverage_ok(connection)
    if not coverage_ok:
        _set_fts_meta(
            connection,
            FTS_DEGRADED_REASON_KEY,
            coverage_reason or "FTS coverage is incomplete",
        )
        connection.commit()
        return
    final_target = int(
        connection.execute("SELECT COALESCE(MAX(message_key), 0) FROM messages").fetchone()[0]
    )
    _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, str(final_target))
    _set_fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY, str(final_target))
    _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, "")
    connection.execute("DELETE FROM store_meta WHERE key = ?", (FTS_STALE_KEY,))
    _fts_rebuild_boundary("after_final_clear", final_target)
    connection.commit()


def _detach_fts(connection: sqlite3.Connection, reason: str = "FTS write failed") -> None:
    """Mark FTS degraded and remove only its virtual table/triggers."""
    with suppress(sqlite3.Error):
        _set_fts_meta(connection, FTS_STALE_KEY, "1")
        _set_fts_meta(connection, FTS_DEGRADED_REASON_KEY, reason)
        _drop_fts(connection)
    _LOGGER.warning("Session FTS detached after corruption; canonical writes continue")


def _mark_fts_write(connection: sqlite3.Connection) -> None:
    if not _fts_table_exists(connection) or _fts_meta(connection, FTS_STALE_KEY) is not None:
        return
    target = int(
        connection.execute("SELECT COALESCE(MAX(message_key), 0) FROM messages").fetchone()[0]
    )
    _set_fts_meta(connection, FTS_TARGET_HIGH_WATER_KEY, str(target))
    _set_fts_meta(connection, FTS_COMPLETED_HIGH_WATER_KEY, str(target))


def _insert_fts_message(connection: sqlite3.Connection, message_key: int) -> None:
    """Project one fully written normalized Message into disposable FTS."""
    if not _fts_table_exists(connection) or _fts_meta(connection, FTS_STALE_KEY) is not None:
        return
    state = connection.execute(
        "SELECT role, searchable, active FROM messages WHERE message_key = ?", (message_key,)
    ).fetchone()
    if state is None or not bool(state["searchable"]) or not bool(state["active"]):
        return
    connection.execute(
        """
        INSERT INTO messages_fts(
            rowid, content, content_search, reasoning, name, error_kind, tool_calls
        )
        SELECT message_key, content, content_search, reasoning, name, error_kind, tool_calls
        FROM messages_fts_source
        WHERE message_key = ?
        """,
        (message_key,),
    )
    if state["role"] != "tool" and _fts_table_exists(connection, FTS_TRIGRAM_TABLE):
        connection.execute(
            """
            INSERT INTO messages_fts_trigram(
                rowid, content, content_search, name, error_kind, tool_calls
            )
            SELECT message_key, content, content_search, name, error_kind, tool_calls
            FROM messages_fts_trigram_source
            WHERE message_key = ?
            """,
            (message_key,),
        )


def _insert_fts_session(connection: sqlite3.Connection, session_key: int) -> None:
    """Project one already-normalized Session into both external-content indexes."""
    if not _fts_table_exists(connection) or _fts_meta(connection, FTS_STALE_KEY) is not None:
        return
    connection.execute(
        """
        INSERT INTO messages_fts(
            rowid, content, content_search, reasoning, name, error_kind, tool_calls
        )
        SELECT source.message_key, source.content, source.content_search, source.reasoning,
               source.name, source.error_kind, source.tool_calls
        FROM messages_fts_source AS source
        JOIN messages AS message ON message.message_key = source.message_key
        WHERE message.session_key = ? AND message.searchable = 1 AND message.active = 1
        """,
        (session_key,),
    )
    if _fts_table_exists(connection, FTS_TRIGRAM_TABLE):
        connection.execute(
            """
            INSERT INTO messages_fts_trigram(
                rowid, content, content_search, name, error_kind, tool_calls
            )
            SELECT source.message_key, source.content, source.content_search, source.name,
                   source.error_kind, source.tool_calls
            FROM messages_fts_trigram_source AS source
            JOIN messages AS message ON message.message_key = source.message_key
            WHERE message.session_key = ? AND message.searchable = 1 AND message.active = 1
              AND message.role <> 'tool'
            """,
            (session_key,),
        )


def _delete_fts_message(connection: sqlite3.Connection, message_key: int) -> None:
    """Delete one still-visible projection before canonical state changes hide it."""
    if not _fts_table_exists(connection) or _fts_meta(connection, FTS_STALE_KEY) is not None:
        return
    connection.execute(
        """
        INSERT INTO messages_fts(
            messages_fts, rowid, content, content_search, reasoning, name, error_kind, tool_calls
        )
        SELECT 'delete', message_key, content, content_search, reasoning, name, error_kind, tool_calls
        FROM messages_fts_source
        WHERE message_key = ?
        """,
        (message_key,),
    )
    if _fts_table_exists(connection, FTS_TRIGRAM_TABLE):
        connection.execute(
            """
            INSERT INTO messages_fts_trigram(
                messages_fts_trigram, rowid, content, content_search, name, error_kind, tool_calls
            )
            SELECT 'delete', message_key, content, content_search, name, error_kind, tool_calls
            FROM messages_fts_trigram_source
            WHERE message_key = ?
            """,
            (message_key,),
        )


def _copy_message_relation(
    connection: sqlite3.Connection,
    *,
    table: str,
    columns: Sequence[str],
    source_session_key: int,
    target_session_key: int,
) -> None:
    column_list = ", ".join(columns)
    selected_columns = ", ".join(f"child.{column}" for column in columns)
    connection.execute(
        f"INSERT INTO {table} (message_key, {column_list}) "
        f"SELECT target.message_key, {selected_columns} FROM {table} AS child "
        "JOIN messages AS source ON source.message_key = child.message_key "
        "JOIN messages AS target ON target.session_key = ? AND target.seq = source.seq "
        "WHERE source.session_key = ?",
        (target_session_key, source_session_key),
    )


def _copy_session_messages(
    connection: sqlite3.Connection,
    *,
    source_session_key: int,
    target_session_key: int,
) -> None:
    """Copy one canonical normalized Message graph without Python reconstruction."""
    connection.execute(
        """
        INSERT INTO messages (
            session_key, seq, message_id, role, timestamp, content, content_blocks_json,
            content_search, model, active, searchable
        )
        SELECT ?, seq, message_id, role, timestamp, content, content_blocks_json,
               content_search, model, active, searchable
        FROM messages
        WHERE session_key = ?
        ORDER BY seq
        """,
        (target_session_key, source_session_key),
    )
    relations = (
        (
            "assistant_messages",
            (
                "reasoning",
                "reasoning_meta_json",
                "reasoning_scope",
                "reasoning_started_at",
                "reasoning_completed_at",
                "reasoning_duration_ms",
                "reasoning_timing_extra_json",
                "phase",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "usage_estimated",
                "input_tokens_estimated",
                "output_tokens_estimated",
                "usage_present",
                "usage_extra_json",
                "tool_calls_present",
                "interrupted",
                "interruption_cause",
            ),
        ),
        (
            "tool_calls",
            (
                "ordinal",
                "tool_call_id",
                "name",
                "arguments_json",
                "rejection_code",
                "rejection_message",
                "rejection_fingerprint",
                "argument_sequence_index",
                "argument_sequence_length",
            ),
        ),
        (
            "assistant_output_files",
            ("ordinal", "path", "line_index", "start_index", "end_index"),
        ),
        ("user_message_senders", ("sender_id", "display_name", "role")),
        ("error_messages", ("error_kind",)),
        (
            "compaction_checkpoints",
            (
                "tail_boundary_id",
                "projection_json",
                "policy",
                "strategy",
                "compacted_token_count",
                "context_tokens_before",
                "context_tokens_after",
                "compaction_duration_ms",
                "usage_present",
                "usage_extra_json",
            ),
        ),
        (
            "run_summaries",
            (
                "run_id",
                "work_id",
                "status",
                "started_at",
                "completed_at",
                "duration_ms",
                "timing_extra_json",
                "iteration_count",
                "changed_files",
                "lines_added",
                "lines_removed",
                "change_stats_extra_json",
            ),
        ),
        ("run_change_paths", ("ordinal", "path")),
        ("history_edits", ("target_message_id",)),
    )
    for table, columns in relations:
        _copy_message_relation(
            connection,
            table=table,
            columns=columns,
            source_session_key=source_session_key,
            target_session_key=target_session_key,
        )

    connection.execute(
        """
        INSERT INTO tool_messages (
            message_key, tool_call_key, tool_call_id, name, result_content,
            started_at, completed_at, duration_ms, timing_extra_json, display_json
        )
        SELECT target.message_key, target_call.tool_call_key, child.tool_call_id, child.name,
               child.result_content, child.started_at, child.completed_at, child.duration_ms,
               child.timing_extra_json, child.display_json
        FROM tool_messages AS child
        JOIN messages AS source ON source.message_key = child.message_key
        JOIN messages AS target ON target.session_key = ? AND target.seq = source.seq
        LEFT JOIN tool_calls AS source_call ON source_call.tool_call_key = child.tool_call_key
        LEFT JOIN messages AS source_call_message
          ON source_call_message.message_key = source_call.message_key
        LEFT JOIN messages AS target_call_message
          ON target_call_message.session_key = ? AND target_call_message.seq = source_call_message.seq
        LEFT JOIN tool_calls AS target_call
          ON target_call.message_key = target_call_message.message_key
         AND target_call.ordinal = source_call.ordinal
        WHERE source.session_key = ?
        """,
        (target_session_key, target_session_key, source_session_key),
    )
    _insert_fts_session(connection, target_session_key)


# Application-level patience is budgeted in seconds; SQLite's own busy handler is
# kept short so contention surfaces for jittered retry.
BUSY_TIMEOUT_MS = 1_000


class SessionStore:
    """One canonical SQLite database with explicit read/write snapshots."""

    def __init__(self, path: Path, *, _offline: bool = False) -> None:
        self.path = Path(path)
        self._runtime = SQLiteRuntime(self.path)
        self._offline = _offline
        self._offline_bulk_import = False
        try:
            self._writer = self._open_runtime(offline=_offline)
        except BaseException:
            self._runtime.close()
            raise

    def _open_runtime(self, *, offline: bool) -> sqlite3.Connection:
        if offline:
            database_id = uuid.uuid4().hex if not self.path.exists() else None
            writer = self._runtime.open_writer(
                create=not self.path.exists(), database_id=database_id
            )
            self._reconcile_open_database(writer, expected_database_id=None)
            return writer

        validate_session_store_paths(self.path.parent, self.path)
        marker = read_session_store_marker(self.path.parent)
        if marker is None:
            raise SessionStorageFormatError(
                f"the data directory does not authorize a current-format Session store: "
                f"{self.path.parent}; initialize the data directory or install a converted "
                "Session database first"
            )
        if int(marker["schema_version"]) != SCHEMA_VERSION:
            raise SessionStoreSchemaMismatchError(
                "Session store marker schema does not match the Runtime: "
                f"schema version {marker['schema_version']}"
            )
        database_id = str(marker["database_id"])
        if marker["state"] == MARKER_STATE_BOOTSTRAP:
            writer = self._runtime.open_writer(create=True, database_id=database_id)
            self._reconcile_open_database(writer, expected_database_id=database_id)
            publish_ready_marker(self.path.parent, database_id)
            return writer
        from core.sessions.snapshots import auto_restore_if_needed, read_recovery_incident

        pending_incident = read_recovery_incident(self.path.parent)
        if pending_incident and pending_incident.get("verification") == "pending":
            auto_restore_if_needed(self.path.parent, self.path)
        if not self.path.exists() and not auto_restore_if_needed(self.path.parent, self.path):
            raise SessionStoreUnavailableError(
                f"the Session database is missing although the store is ready: {self.path}"
            )
        try:
            writer = self._runtime.open_writer(expected_database_id=database_id)
            self._reconcile_open_database(writer, expected_database_id=database_id)
            return writer
        except (SessionStoreCorruptError, sqlite3.DatabaseError, OSError):
            self._runtime.close()
            if auto_restore_if_needed(self.path.parent, self.path):
                self._runtime = SQLiteRuntime(self.path)
                writer = self._runtime.open_writer(expected_database_id=database_id)
                self._reconcile_open_database(writer, expected_database_id=database_id)
                return writer
            raise

    def _reconcile_open_database(
        self, connection: sqlite3.Connection, *, expected_database_id: str | None
    ) -> None:
        if expected_database_id is not None:
            self._verify_database_identity(connection, self.path, expected_database_id)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != 0 and version < SCHEMA_CONVERSION_FLOOR:
            raise SessionStoreSchemaMismatchError(
                f"Session database schema {version} requires the offline converter"
            )
        applied = reconcile_schema(connection)
        if applied:
            _LOGGER.info(
                "Reconciled Session database schema at %s: %s",
                self.path,
                "; ".join(applied),
            )
        self._reconcile_session_metadata_projection(connection)
        _ensure_fts_schema(connection)
        self._verify_connection(connection, self.path)

    def _reconcile_session_metadata_projection(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT value FROM store_meta WHERE key = ?",
            (_SESSION_METADATA_PROJECTION_VERSION_KEY,),
        ).fetchone()
        if row is not None and str(row[0]) == _SESSION_METADATA_PROJECTION_VERSION:
            return
        rows = connection.execute("SELECT * FROM sessions ORDER BY session_key").fetchall()
        connection.execute("BEGIN IMMEDIATE")
        try:
            for state in rows:
                metadata = _session_metadata_from_state(state)
                residual_payload, projection = _session_metadata_storage(metadata)
                connection.execute(
                    "UPDATE sessions SET active_sort = "
                    "COALESCE(julianday(COALESCE(last_message_at, created_at)), 0.0), "
                    "metadata_json = ?, "
                    + ", ".join(f"{column} = ?" for column in _SESSION_METADATA_PROJECTION_COLUMNS)
                    + " WHERE session_key = ?",
                    (residual_payload, *projection, state["session_key"]),
                )
            connection.execute(
                "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                (
                    _SESSION_METADATA_PROJECTION_VERSION_KEY,
                    _SESSION_METADATA_PROJECTION_VERSION,
                ),
            )
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        if rows:
            _LOGGER.info(
                "Reconciled normalized Session metadata projections at %s (sessions=%s)",
                self.path,
                len(rows),
            )

    @staticmethod
    def _verify_connection(connection: sqlite3.Connection, path: Path) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise SessionStoreSchemaMismatchError(
                f"unsupported Session database version {version} at {path}"
            )
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        if application_id != APPLICATION_ID:
            raise SessionStoreCorruptError(f"not a vBot Session database: {path}")
        try:
            for table in (
                "store_meta",
                "sessions",
                "messages",
                "assistant_messages",
                "tool_calls",
                "tool_messages",
                "run_summaries",
                "continuations",
            ):
                connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        except sqlite3.DatabaseError as exc:
            raise SessionStoreCorruptError(
                f"Session database structure is unreadable at {path}"
            ) from exc

    @staticmethod
    def _verify_database_identity(
        connection: sqlite3.Connection, path: Path, database_id: str
    ) -> None:
        row = connection.execute(
            "SELECT value FROM store_meta WHERE key = ?", (DATABASE_ID_META_KEY,)
        ).fetchone()
        if row is None or str(row[0]) != database_id:
            raise SessionStoreCorruptError(
                f"Session database identity does not match the store marker at {path}"
            )

    @staticmethod
    def _scope(address: SessionAddress) -> tuple[str, str, str]:
        return (address.project_id or "", address.agent_id, address.session_id)

    @staticmethod
    def _address(row: sqlite3.Row) -> SessionAddress:
        from core.sessions.sessions import SessionAddress

        return SessionAddress(
            project_id=row["project_id"] or None,
            agent_id=row["agent_id"],
            session_id=row["session_id"],
        )

    # The Session domain uses this compact facade; connection ownership and
    # synchronization live exclusively in SQLiteRuntime.
    @contextmanager
    def _transaction(self, *, write: bool, patience_s: float = WRITE_PATIENCE_S):
        if write:
            raise RuntimeError("SessionStore write transactions use _execute_write")
        with self._runtime.read_ctx() as connection:
            yield connection

    def _execute_write(
        self, func: Callable[[sqlite3.Connection], Any], patience_s: float = WRITE_PATIENCE_S
    ) -> Any:
        fts_retried = False
        while True:
            try:
                return self._runtime.execute_write(func, patience_s=patience_s)
            except sqlite3.Error as exc:
                message = str(exc).lower()
                if not fts_retried and any(
                    marker in message for marker in ("fts", "messages_fts", "message_search")
                ):
                    fts_retried = True
                    with suppress(Exception):
                        self._runtime.execute_write(_detach_fts, patience_s=patience_s)
                    continue
                classification = classify_write_error(exc)
                if classification == "corrupt":
                    raise SessionStoreCorruptError(
                        f"Session database write found corruption: {self.path}"
                    ) from exc
                if classification == "unavailable":
                    raise SessionStoreUnavailableError(
                        f"Session database write failed: {self.path}"
                    ) from exc
                raise

    def _try_wal_checkpoint(self) -> None:
        self._runtime.checkpoint()

    def _try_fts_merge(self) -> None:
        def merge(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('merge', 16)")
            connection.execute(
                "INSERT INTO messages_fts_trigram(messages_fts_trigram, rank) VALUES('merge', 16)"
            )

        with suppress(Exception):
            self._runtime.execute_write(merge, patience_s=ACTIVITY_WRITE_PATIENCE_S)

    @contextmanager
    def _read_transaction(self):
        with self._runtime.read_ctx() as connection:
            yield connection

    def close(self) -> None:
        self._runtime.close()

    def checkpoint(self) -> None:
        self._runtime.checkpoint()

    def prepare_offline_bulk_import(self) -> None:
        """Open one disposable canonical-data transaction without maintaining FTS."""
        if not self._offline:
            raise RuntimeError("bulk import mode is available only to the offline converter")
        if self._offline_bulk_import:
            raise RuntimeError("bulk import mode is already active")
        mode = str(self._writer.execute("PRAGMA journal_mode=MEMORY").fetchone()[0]).lower()
        if mode != "memory":
            raise SessionStoreUnavailableError("staged Session database rejected MEMORY journal")
        self._writer.execute("PRAGMA synchronous=OFF")
        self._writer.execute("PRAGMA temp_store=MEMORY")
        self._writer.execute(f"PRAGMA cache_size=-{_OFFLINE_IMPORT_CACHE_KIB}")
        _drop_fts(self._writer)
        _set_fts_meta(self._writer, FTS_STALE_KEY, "offline-import")
        self._writer.execute("BEGIN IMMEDIATE")
        self._offline_bulk_import = True

    def finish_offline_bulk_import(self) -> None:
        """Commit canonical rows and build both disposable FTS indexes in SQLite."""
        if not self._offline or not self._offline_bulk_import:
            raise RuntimeError("bulk import mode is not active")
        self._writer.execute("COMMIT")
        self._offline_bulk_import = False
        try:
            self._writer.executescript(FTS_SQL)
        except sqlite3.Error:
            _drop_fts(self._writer)
            self._writer.executescript(FTS_SQL_FALLBACK)
        target = int(
            self._writer.execute("SELECT COALESCE(MAX(message_key), 0) FROM messages").fetchone()[0]
        )
        _set_fts_meta(self._writer, FTS_STORAGE_VERSION_KEY, str(FTS_STORAGE_VERSION))
        _set_fts_meta(self._writer, FTS_GENERATION_KEY, uuid.uuid4().hex)
        _set_fts_meta(self._writer, FTS_TARGET_HIGH_WATER_KEY, str(target))
        _set_fts_meta(self._writer, FTS_COMPLETED_HIGH_WATER_KEY, "0")
        _set_fts_meta(self._writer, FTS_STALE_KEY, "rebuilding")
        _set_fts_meta(self._writer, FTS_DEGRADED_REASON_KEY, "FTS rebuild in progress")
        self._writer.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        if _fts_table_exists(self._writer, FTS_TRIGRAM_TABLE):
            self._writer.execute(
                "INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild')"
            )
        coverage_ok, coverage_reason = _fts_coverage_ok(self._writer)
        if not coverage_ok:
            raise SessionStoreCorruptError(
                coverage_reason or "offline FTS rebuild did not cover canonical Messages"
            )
        _set_fts_meta(self._writer, FTS_COMPLETED_HIGH_WATER_KEY, str(target))
        _set_fts_meta(self._writer, FTS_DEGRADED_REASON_KEY, "")
        self._writer.execute("DELETE FROM store_meta WHERE key = ?", (FTS_STALE_KEY,))

    def backup(
        self,
        destination: Path,
        *,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        return self._runtime.backup(destination, cancel_event=cancel_event)

    def verify_read_write(self) -> None:
        """Exercise the opened Runtime's read/write path without changing canonical rows."""

        def verify(connection: sqlite3.Connection) -> None:
            connection.execute("CREATE TEMP TABLE session_store_verify(value INTEGER NOT NULL)")
            try:
                connection.execute("INSERT INTO session_store_verify(value) VALUES (1)")
                row = connection.execute("SELECT value FROM session_store_verify").fetchone()
                if row is None or int(row[0]) != 1:
                    raise SessionStoreUnavailableError(
                        "Session database read/write verification failed"
                    )
            finally:
                connection.execute("DROP TABLE IF EXISTS session_store_verify")

        self._execute_write(verify)

    def status_projection(self) -> JsonObject:
        """Return operator-safe health, snapshot, and incident state."""
        from core.sessions.snapshots import (
            read_recovery_incident,
            read_snapshot_health,
            snapshot_inventory,
        )

        marker = read_session_store_marker(self.path.parent)
        if marker is None:
            raise SessionStorageFormatError("current-format Session marker is missing")
        fts = self.fts_health()
        incident = read_recovery_incident(self.path.parent)
        snapshots = snapshot_inventory(
            self.path.parent, expected_database_id=str(marker["database_id"])
        )
        snapshot_health = read_snapshot_health(self.path.parent)
        active_incident = incident if incident and not incident.get("acknowledged", False) else None
        if active_incident:
            health = SessionStoreHealth("recovered_with_incident")
        elif not fts.available:
            health = SessionStoreHealth("search_degraded", fts.reason)
        elif not snapshots or snapshot_health.get("state") != "healthy":
            health = SessionStoreHealth(
                "snapshot_degraded",
                str(snapshot_health.get("reason") or "no verified Session snapshot is available"),
            )
        else:
            health = SessionStoreHealth("healthy")
        return {
            "state": health.state,
            "reason": health.reason,
            "database_id": str(marker["database_id"]),
            "marker_state": marker["state"],
            "schema_version": int(marker["schema_version"]),
            "fts": {
                "state": fts.state,
                "reason": fts.reason,
                "generation": fts.generation,
                "target_high_water": fts.target_high_water,
                "completed_high_water": fts.completed_high_water,
            },
            "snapshots": snapshots,
            "snapshot_health": snapshot_health,
            "incident": active_incident,
        }

    def create(self, address: SessionAddress, created_at: str | None = None) -> None:
        timestamp = created_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")

        def _fn(connection: sqlite3.Connection) -> None:
            try:
                connection.execute(
                    "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, "
                    "created_at, active_sort) VALUES (?, ?, ?, ?, ?, "
                    "COALESCE(julianday(?), 0.0))",
                    (uuid.uuid4().hex, *self._scope(address), timestamp, timestamp),
                )
            except sqlite3.IntegrityError as exc:
                raise ChatSessionError(f"session already exists: {address.session_id}") from exc

        self._execute_write(_fn)

    def import_generation(
        self,
        address: SessionAddress,
        *,
        generation_id: str,
        messages: Sequence[ChatMessage],
        metadata: JsonObject,
        activity: JsonObject,
        continuation: Sequence[JsonObject],
        archived: bool,
        created_at: str,
    ) -> None:
        """Import one complete generation in one offline-only transaction.

        The converter is the only caller that may supply a generation id. Keeping
        this operation on the canonical transaction owner means imported rows use
        exactly the same validation, projections, and foreign-key behavior as
        normal Session writes, without exposing a migration hook to Runtime.
        """
        if not self._offline:
            raise ChatSessionError("generation import is available only to the offline converter")
        if (
            not isinstance(generation_id, str)
            or len(generation_id) != 32
            or any(character not in "0123456789abcdef" for character in generation_id)
        ):
            raise ChatSessionError("offline generation id is invalid")
        metadata_payload = _json_object(metadata, "session metadata")
        residual_metadata_payload, metadata_projection = _session_metadata_storage(metadata)
        activity_payload = _json_object(activity, "session activity")
        for message in messages:
            _message_base_row(message)
        for record in continuation:
            _json_object(record, "continuation record")

        def _fn(connection: sqlite3.Connection) -> None:
            try:
                cursor = connection.execute(
                    "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, status, "
                    "created_at, last_message_at, active_sort, archived_at, message_count, last_message_id, "
                    "history_revision, state_revision, metadata_json, "
                    + ", ".join(_SESSION_METADATA_PROJECTION_COLUMNS)
                    + ", activity_json) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(julianday(?), 0.0), ?, ?, ?, ?, ?, ?, "
                    + ", ".join("?" for _ in _SESSION_METADATA_PROJECTION_COLUMNS)
                    + ", ?)",
                    (
                        generation_id,
                        *self._scope(address),
                        "archived" if archived else "live",
                        created_at,
                        messages[-1].timestamp if messages else None,
                        messages[-1].timestamp if messages else created_at,
                        created_at if archived else None,
                        len(messages),
                        messages[-1].id if messages else None,
                        1 if messages else 0,
                        1
                        if messages
                        or continuation
                        or metadata_payload != "{}"
                        or activity_payload != "{}"
                        else 0,
                        residual_metadata_payload,
                        *metadata_projection,
                        activity_payload,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ChatSessionError(
                    f"offline Session generation conflicts: {address.session_id}"
                ) from exc
            session_key = cursor.lastrowid
            if session_key is None:
                raise SessionStoreCorruptError("SQLite did not return an offline Session key")
            for sequence, message in enumerate(messages):
                _insert_message(
                    connection,
                    int(session_key),
                    sequence,
                    message,
                    index_fts=not self._offline_bulk_import,
                )
            for record in continuation:
                _apply_continuation_record(connection, int(session_key), record)
            if not self._offline_bulk_import:
                _mark_fts_write(connection)

        if self._offline_bulk_import:
            savepoint = f"generation_{generation_id}"
            self._writer.execute(f"SAVEPOINT {savepoint}")
            try:
                _fn(self._writer)
                self._writer.execute(f"RELEASE {savepoint}")
            except BaseException:
                self._writer.execute(f"ROLLBACK TO {savepoint}")
                self._writer.execute(f"RELEASE {savepoint}")
                raise
            return
        self._execute_write(_fn, patience_s=TRANSCRIPT_WRITE_PATIENCE_S)

    def ensure_live(self, address: SessionAddress) -> None:
        """Atomically return an existing live Session or create a new generation."""
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        def _fn(connection: sqlite3.Connection) -> None:
            if self._find_live(connection, address) is not None:
                return
            connection.execute(
                "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, "
                "created_at, active_sort) VALUES (?, ?, ?, ?, ?, "
                "COALESCE(julianday(?), 0.0))",
                (uuid.uuid4().hex, *self._scope(address), timestamp, timestamp),
            )

        self._execute_write(_fn)

    def exists(self, address: SessionAddress, *, include_archived: bool = False) -> bool:
        clause = "" if include_archived else " AND status = 'live'"
        with self._transaction(write=False) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ?"
                    + clause,
                    self._scope(address),
                ).fetchone()
                is not None
            )

    def state(self, address: SessionAddress, *, include_archived: bool = False) -> sqlite3.Row:
        clause = "" if include_archived else " AND status = 'live'"
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ?"
                + clause
                + " ORDER BY status = 'live' DESC, session_key DESC LIMIT 1",
                self._scope(address),
            ).fetchone()
        if row is None:
            raise SessionNotFoundError(f"session does not exist: {address.session_id}")
        return cast(sqlite3.Row, row)

    def metadata(self, address: SessionAddress) -> JsonObject:
        return _session_metadata_from_state(self.state(address))

    def descriptor_source(
        self, address: SessionAddress
    ) -> tuple[JsonObject, int, ChatMessage | None]:
        """Load compact descriptor inputs without reconstructing Session history."""
        source = self.descriptor_sources((address,)).get(address)
        if source is None:
            raise SessionNotFoundError(f"session does not exist: {address.session_id}")
        return source

    def descriptor_sources(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, tuple[JsonObject, int, ChatMessage | None]]:
        """Load compact descriptor inputs for many Sessions in set-oriented reads."""
        sources: dict[SessionAddress, tuple[JsonObject, int, ChatMessage | None]] = {}
        by_scope: dict[tuple[str, str], list[str]] = {}
        for address in addresses:
            by_scope.setdefault((address.project_id or "", address.agent_id), []).append(
                address.session_id
            )
        with self._transaction(write=False) as connection:
            for (project_id, agent_id), session_ids in by_scope.items():
                for start in range(0, len(session_ids), _DESCRIPTOR_SOURCE_BATCH_SIZE):
                    chunk = session_ids[start : start + _DESCRIPTOR_SOURCE_BATCH_SIZE]
                    placeholders = ", ".join("?" for _ in chunk)
                    states = connection.execute(
                        f"SELECT session_key, message_count, {_SESSION_LIST_COLUMNS} "
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
                        _message_records_sql(
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
                        int(row["session_key"]): message_from_row(row) for row in first_user_rows
                    }
                    for state in states:
                        address = self._address(state)
                        sources[address] = (
                            _session_projected_metadata_from_state(state),
                            int(state["message_count"]),
                            first_users.get(int(state["session_key"])),
                        )
        return sources

    def replace_metadata(self, address: SessionAddress, metadata: JsonObject) -> None:
        _json_object(metadata, "session metadata")
        payload, projection = _session_metadata_storage(metadata)

        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, address)
            connection.execute(
                "UPDATE sessions SET metadata_json = ?, "
                + ", ".join(f"{column} = ?" for column in _SESSION_METADATA_PROJECTION_COLUMNS)
                + ", state_revision = state_revision + 1 WHERE session_key = ?",
                (payload, *projection, state["session_key"]),
            )

        self._execute_write(_fn)

    def mutate_metadata(
        self, address: SessionAddress, mutation: Callable[[JsonObject], None]
    ) -> tuple[JsonObject, JsonObject]:
        """Apply one metadata read-modify-write under the writer transaction."""
        result: tuple[JsonObject, JsonObject] | None = None

        def _fn(connection: sqlite3.Connection) -> None:
            nonlocal result
            state = self._require_live(connection, address)
            previous = _session_metadata_from_state(state)
            updated = deepcopy(previous)
            mutation(updated)
            _json_object(updated, "session metadata")
            payload, projection = _session_metadata_storage(updated)
            connection.execute(
                "UPDATE sessions SET metadata_json = ?, "
                + ", ".join(f"{column} = ?" for column in _SESSION_METADATA_PROJECTION_COLUMNS)
                + ", state_revision = state_revision + 1 WHERE session_key = ?",
                (payload, *projection, state["session_key"]),
            )
            result = (previous, updated)

        self._execute_write(_fn)
        assert result is not None
        return result

    def activity(self, address: SessionAddress) -> JsonObject:
        payload = self.state(address)["activity_json"]
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SessionStoreCorruptError(
                f"invalid Session activity: {address.session_id}"
            ) from exc
        return data if isinstance(data, dict) else {}

    def replace_activity(self, address: SessionAddress, activity: JsonObject) -> None:
        payload = _json_object(activity, "session activity")

        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, address)
            connection.execute(
                "UPDATE sessions SET activity_json = ?, state_revision = state_revision + 1 WHERE session_key = ?",
                (payload, state["session_key"]),
            )

        self._execute_write(_fn, patience_s=ACTIVITY_WRITE_PATIENCE_S)

    def mutate_activity(
        self, address: SessionAddress, mutation: Callable[[JsonObject], None]
    ) -> tuple[JsonObject, JsonObject]:
        """Apply one activity read-modify-write under the writer transaction."""
        result: tuple[JsonObject, JsonObject] | None = None

        def _fn(connection: sqlite3.Connection) -> None:
            nonlocal result
            state = self._require_live(connection, address)
            previous = _json_from_payload(state["activity_json"], "session activity")
            updated = deepcopy(previous)
            mutation(updated)
            payload = _json_object(updated, "session activity")
            connection.execute(
                "UPDATE sessions SET activity_json = ?, state_revision = state_revision + 1 WHERE session_key = ?",
                (payload, state["session_key"]),
            )
            result = (previous, updated)

        self._execute_write(_fn, patience_s=ACTIVITY_WRITE_PATIENCE_S)
        assert result is not None
        return result

    def append_messages(self, address: SessionAddress, messages: Sequence[ChatMessage]) -> None:
        if not messages:
            return
        for message in messages:
            _message_base_row(message)

        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, address)
            session_key = int(state["session_key"])
            next_seq = int(state["message_count"])
            for index, message in enumerate(messages):
                _insert_message(connection, session_key, next_seq + index, message)
            last_message = messages[-1]
            connection.execute(
                "UPDATE sessions SET message_count = message_count + ?, last_message_at = ?, "
                "active_sort = COALESCE(julianday(?), 0.0), last_message_id = ?, "
                "history_revision = history_revision + 1, state_revision = state_revision + 1 "
                "WHERE session_key = ?",
                (
                    len(messages),
                    last_message.timestamp,
                    last_message.timestamp,
                    last_message.id,
                    session_key,
                ),
            )
            _mark_fts_write(connection)

        self._execute_write(_fn, patience_s=TRANSCRIPT_WRITE_PATIENCE_S)

    def messages(self, address: SessionAddress) -> list[ChatMessage]:

        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
            rows = connection.execute(
                _message_records_sql(where="m.session_key = ?", order_by="ORDER BY m.seq"),
                (state["session_key"],),
            ).fetchall()
        return [message_from_row(row) for row in rows]

    def active_messages(self, address: SessionAddress) -> list[ChatMessage]:
        """Load the relationally materialized active lineage only."""
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
            rows = connection.execute(
                _message_records_sql(
                    where="m.session_key = ? AND m.active = 1",
                    order_by="ORDER BY m.seq",
                ),
                (state["session_key"],),
            ).fetchall()
        return [message_from_row(row) for row in rows]

    def active_user_message_count(self, address: SessionAddress, *, limit: int) -> int:
        """Count at most ``limit`` active User Messages without loading their content."""
        if limit <= 0:
            raise ChatSessionError("user Message count limit must be positive")
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
            row = connection.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM messages WHERE session_key = ? "
                "AND active = 1 AND role = 'user' LIMIT ?)",
                (state["session_key"], limit),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def latest_note(self, address: SessionAddress, *, content_prefix: str) -> ChatMessage | None:
        """Load the newest Note matching one canonical content prefix."""
        if not content_prefix:
            raise ChatSessionError("note prefix must be non-empty")
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
            row = connection.execute(
                _message_records_sql(
                    where=("m.session_key = ? AND m.role = 'note' AND substr(m.content, 1, ?) = ?"),
                    order_by="ORDER BY m.seq DESC LIMIT 1",
                ),
                (state["session_key"], len(content_prefix), content_prefix),
            ).fetchone()
        return None if row is None else message_from_row(row)

    def chat_history_snapshot(
        self,
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
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            raise ChatSessionError("message page limit must be a positive integer")
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
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
            [message_from_row(row) for row in page_rows],
            has_more,
            editable_ids,
            usage,
            [message_from_row(row) for row in context_rows],
            [message_from_row(row) for row in background_rows],
            str(state["generation_id"]),
            page_floor,
        )

    def status_snapshot(
        self,
        address: SessionAddress,
    ) -> tuple[str | None, int, JsonObject | None, JsonObject, int]:
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
            session_key = int(state["session_key"])
            facts = connection.execute(
                "SELECT MIN(CASE WHEN seq = 0 THEN timestamp END) AS first_message_at, "
                "SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END) AS user_count "
                "FROM messages WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            latest_row = connection.execute(
                _message_records_sql(
                    where=("m.session_key = ? AND m.role = 'assistant' AND a.usage_present = 1"),
                    order_by="ORDER BY m.seq DESC LIMIT 1",
                ),
                (session_key,),
            ).fetchone()
            latest_usage = None
            if latest_row is not None:
                latest_usage = message_from_row(latest_row).usage
            usage, cache_input_tokens = _session_usage_from_connection(connection, session_key)
        return (
            None if facts["first_message_at"] is None else str(facts["first_message_at"]),
            int(facts["user_count"] or 0),
            latest_usage,
            usage,
            cache_input_tokens,
        )

    def history_snapshot(
        self,
        address: SessionAddress,
        *,
        snapshot_sequence: int | None = None,
    ) -> tuple[str, list[tuple[int, str, str, str]]] | None:
        """Resolve active Compaction checkpoints without reading history records."""
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
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
        self,
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
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
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
                _message_records_sql(
                    where=" AND ".join(clauses),
                    order_by=(
                        "ORDER BY m.seq ASC LIMIT ?"
                        if direction == "start"
                        else "ORDER BY m.seq DESC LIMIT ?"
                    ),
                ),
                params,
            ).fetchall()
        return [(int(row["seq"]), message_from_row(row)) for row in rows]

    def history_section_stats(
        self,
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
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
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
                    f"{_MESSAGE_RECORD_JOINS} WHERE m.session_key = ? AND m.active = 1 "
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
        self,
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
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
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
                _message_records_sql(
                    where=" AND ".join([*base_clauses, "m.message_id = ?"]),
                    order_by="ORDER BY m.seq LIMIT 1",
                ),
                (*base_params, message_id),
            ).fetchone()
            if anchor is None:
                return exists, []
            anchor_sequence = int(anchor["seq"])
            earlier_rows = connection.execute(
                _message_records_sql(
                    where=" AND ".join([*base_clauses, "m.seq < ?"]),
                    order_by="ORDER BY m.seq DESC LIMIT ?",
                ),
                (*base_params, anchor_sequence, before),
            ).fetchall()
            later_rows = connection.execute(
                _message_records_sql(
                    where=" AND ".join([*base_clauses, "m.seq > ?"]),
                    order_by="ORDER BY m.seq LIMIT ?",
                ),
                (*base_params, anchor_sequence, after),
            ).fetchall()
        rows = [*reversed(earlier_rows), anchor, *later_rows]
        return exists, [(int(row["seq"]), message_from_row(row)) for row in rows]

    def reflection_runs(self, address: SessionAddress) -> list[JsonObject]:
        """Read each review fork's own terminal Run, excluding inherited history."""
        with self._transaction(write=False) as connection:
            source = self._require_live(connection, address)
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
        self,
        address: SessionAddress,
        *,
        run_id: str | None = None,
        work_id: str | None = None,
    ) -> ChatMessage | None:
        if (run_id is None) == (work_id is None):
            raise ChatSessionError("exactly one of run_id or work_id is required")
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
            field, value = ("r.run_id", run_id) if run_id is not None else ("r.work_id", work_id)
            row = connection.execute(
                _message_records_sql(
                    where=f"m.session_key = ? AND m.role = 'run_summary' AND {field} = ?",
                    order_by="ORDER BY m.seq DESC LIMIT 1",
                ),
                (state["session_key"], value),
            ).fetchone()
        return None if row is None else message_from_row(row)

    def run_result(
        self,
        address: SessionAddress,
        *,
        run_id: str | None = None,
        work_id: str | None = None,
        require_latest: bool = False,
    ) -> tuple[ChatMessage | None, ChatMessage, str | None] | None:
        """Project one terminal Run without reconstructing its Tool/result payloads."""
        if run_id is not None and work_id is not None:
            raise ChatSessionError("run_id and work_id cannot be combined")
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
            params: list[Any] = [state["session_key"]]
            where = "m.session_key = ? AND m.role = 'run_summary'"
            if run_id is not None:
                where += " AND r.run_id = ?"
                params.append(run_id)
            elif work_id is not None:
                where += " AND r.work_id = ?"
                params.append(work_id)
            summary_row = connection.execute(
                _message_records_sql(
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
                "SELECT MAX(seq) FROM messages WHERE session_key = ? "
                "AND role = 'run_summary' AND seq < ?",
                (state["session_key"], summary_seq),
            ).fetchone()[0]
            previous_seq = -1 if previous is None else int(previous)
            assistant_row = connection.execute(
                _message_records_sql(
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
            None if assistant_row is None else message_from_row(assistant_row),
            message_from_row(summary_row),
            None if latest_tool is None else str(latest_tool["name"]),
        )

    def messages_since(
        self, address: SessionAddress, cursor: SessionReadCursor | None
    ) -> tuple[list[ChatMessage], SessionReadCursor] | None:
        from core.sessions.sessions import SessionReadCursor

        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
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
                _message_records_sql(
                    where="m.session_key = ? AND m.seq >= ?",
                    order_by="ORDER BY m.seq",
                ),
                (state["session_key"], start),
            ).fetchall()
        return (
            [message_from_row(row) for row in rows],
            SessionReadCursor(generation_id, revision, count, count, last_id),
        )

    def continuation(self, address: SessionAddress) -> list[JsonObject]:
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
            return _continuation_records(connection, int(state["session_key"]))

    def append_continuation(self, address: SessionAddress, records: Sequence[JsonObject]) -> None:
        if not records:
            return
        for record in records:
            _json_object(record, "continuation record")

        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, address)
            session_key = int(state["session_key"])
            for record in records:
                _apply_continuation_record(connection, session_key, record)
            self._touch_state(connection, int(state["session_key"]))

        self._execute_write(_fn)

    def clear_continuation(self, address: SessionAddress) -> None:
        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, address)
            connection.execute(
                "DELETE FROM continuations WHERE session_key = ?",
                (state["session_key"],),
            )
            self._touch_state(connection, int(state["session_key"]))

        self._execute_write(_fn)

    def bookend_timestamps(self, address: SessionAddress) -> tuple[str, str] | None:
        with self._transaction(write=False) as connection:
            state = self._require_live(connection, address)
            if int(state["message_count"]) == 0:
                return None
            first = connection.execute(
                "SELECT timestamp FROM messages WHERE session_key = ? AND seq = 0",
                (state["session_key"],),
            ).fetchone()
            if first is None or state["last_message_at"] is None:
                raise SessionStoreCorruptError(
                    f"invalid Session message summary: {address.session_id}"
                )
            return str(first["timestamp"]), str(state["last_message_at"])

    def list_addresses(
        self,
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
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT project_id, agent_id, session_id FROM sessions WHERE "
                + " AND ".join(clauses)
                + " ORDER BY session_id",
                params,
            ).fetchall()
        return [self._address(row) for row in rows]

    def list_state_rows(self, project_id: str | None, agent_id: str) -> list[sqlite3.Row]:
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT * FROM sessions WHERE status = 'live' AND project_id = ? AND agent_id = ? ORDER BY session_id",
                (project_id or "", agent_id),
            ).fetchall()
        return cast(list[sqlite3.Row], rows)

    def list_summary_rows_for_scope(
        self,
        project_id: str | None,
        agent_id: str,
        *,
        metadata_keys: Sequence[str] = (),
    ) -> list[sqlite3.Row]:
        selected_keys = tuple(dict.fromkeys(metadata_keys))
        unknown = set(selected_keys) - _SUMMARY_METADATA_COLUMNS.keys()
        if unknown:
            raise ChatSessionError(
                f"unsupported Session summary metadata: {', '.join(sorted(unknown))}"
            )
        metadata_columns = "".join(
            f", json_extract(metadata_json, '{_SUMMARY_METADATA_COLUMNS[key]}') "
            f"AS metadata_{key}_json"
            for key in selected_keys
        )
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                f"SELECT {_SESSION_LIST_COLUMNS}{metadata_columns} FROM sessions "
                "WHERE status = 'live' AND project_id = ? AND agent_id = ? "
                "ORDER BY active_sort DESC, session_id",
                (project_id or "", agent_id),
            ).fetchall()
        return cast(list[sqlite3.Row], rows)

    def list_recall_summary_rows(
        self,
        project_id: str | None,
        agent_id: str,
        *,
        include_subagents: bool,
        excluded_session_id: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[sqlite3.Row]:
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            raise ChatSessionError("Recall Session limit must be a positive integer")
        valid_kinds = f"((list_visibility_mask & {_LIST_VISIBILITY_VALID_RUN_KINDS}) != 0)"
        reflection = f"((list_visibility_mask & {_LIST_VISIBILITY_REFLECTION}) != 0)"
        subagent = (
            "((list_visibility_mask & "
            f"{_LIST_VISIBILITY_SUBAGENT_SESSION | _LIST_VISIBILITY_SUBAGENT_RUN_KIND}) != 0)"
        )
        user_facing = f"((list_visibility_mask & {_LIST_VISIBILITY_USER_FACING}) != 0)"
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
            period_placeholders = ", ".join("?" for _ in _RECALL_PERIOD_ROLES)
            period = [
                "candidate.session_key = sessions.session_key",
                f"candidate.role IN ({period_placeholders})",
            ]
            params.extend(_RECALL_PERIOD_ROLES)
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
            f"SELECT {_SESSION_LIST_COLUMNS} FROM sessions WHERE "
            + " AND ".join(where)
            + " ORDER BY active_sort DESC, session_id"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._transaction(write=False) as connection:
            rows = connection.execute(sql, params).fetchall()
        return cast(list[sqlite3.Row], rows)

    @staticmethod
    def metadata_from_state(state: Any) -> JsonObject:
        return _session_metadata_from_state(state)

    def list_summary_rows(
        self,
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
            "("
            + " OR ".join("(project_id = ? AND agent_id = ?)" for _scope in normalized_scopes)
            + ")"
        )
        scope_params = [value for scope in normalized_scopes for value in scope]
        visibility_sql, visibility_params = _session_list_visibility_sql(
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
        with self._transaction(write=False) as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM sessions WHERE {base_where} AND {visibility_sql}",
                    (*scope_params, *visibility_params),
                ).fetchone()[0]
            )
            fetched = connection.execute(
                f"WITH candidates AS (SELECT {_SESSION_LIST_COLUMNS} FROM sessions "
                f"WHERE {base_where} AND {visibility_sql}) "
                f"SELECT * FROM candidates {page_where} "
                "ORDER BY active_sort DESC, project_id, agent_id, session_id LIMIT ?",
                (*scope_params, *visibility_params, *page_params, limit + 1),
            ).fetchall()
            has_more = len(fetched) > limit
            rows = fetched[:limit]
            required_row: sqlite3.Row | None = None
            if required_address is not None:
                required_scope = self._scope(required_address)
                if (required_scope[0], required_scope[1]) in normalized_scopes:
                    required_row = connection.execute(
                        f"SELECT {_SESSION_LIST_COLUMNS}, "
                        f"CASE WHEN {visibility_sql} THEN 1 ELSE 0 END AS list_visible "
                        "FROM sessions WHERE status = 'live' AND project_id = ? "
                        "AND agent_id = ? AND session_id = ?",
                        (*visibility_params, *required_scope),
                    ).fetchone()
        if required_row is not None and not bool(required_row["list_visible"]):
            total += 1
        return cast(list[sqlite3.Row], rows), required_row, total, has_more

    def list_activity_rows(self, project_id: str | None, agent_id: str) -> list[sqlite3.Row]:
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT session_id, latest_completion_run_id, latest_completion_status, "
                "latest_completion_at, read_completion_run_id FROM sessions "
                "WHERE status = 'live' AND project_id = ? AND agent_id = ? ORDER BY session_id",
                (project_id or "", agent_id),
            ).fetchall()
        return cast(list[sqlite3.Row], rows)

    def session_ids_with_messages(
        self,
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
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT DISTINCT s.session_id FROM sessions AS s "
                "JOIN messages AS m ON m.session_key = s.session_key WHERE "
                + " AND ".join(clauses),
                params,
            ).fetchall()
        return {str(row["session_id"]) for row in rows}

    def list_history_revisions(
        self, project_id: str | None, agent_id: str
    ) -> list[tuple[SessionAddress, str, int]]:
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT project_id, agent_id, session_id, generation_id, history_revision FROM sessions WHERE status = 'live' AND project_id = ? AND agent_id = ? ORDER BY session_id",
                (project_id or "", agent_id),
            ).fetchall()
        return [
            (self._address(row), str(row["generation_id"]), int(row["history_revision"]))
            for row in rows
        ]

    def list_history_versions(
        self, addresses: Sequence[SessionAddress]
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
        with self._transaction(write=False) as connection:
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
                        address = self._address(row)
                        versions[address] = (
                            str(row["generation_id"]),
                            int(row["history_revision"]),
                        )
        return versions

    def fts_health(self) -> FtsHealth:
        """Return operator-facing FTS state with explicit canonical coverage checks."""
        try:
            with self._transaction(write=False) as connection:
                return _fts_health_from_connection(connection, verify_coverage=True)
        except Exception as exc:
            return FtsHealth(state="unavailable", reason=f"FTS health check failed: {exc}")

    def is_fts_available(self) -> bool:
        """Return cheap marker-backed availability; rebuild verification owns coverage scans."""
        try:
            with self._transaction(write=False) as connection:
                return _fts_health_from_connection(connection, verify_coverage=False).available
        except Exception:
            return False

    def fts_search(
        self,
        query: str,
        *,
        project_id: str | None,
        agent_id: str | None,
        session_id: str | None = None,
        match_mode: str = "all_terms",
        limit: int = _SEARCH_RESULT_LIMIT,
        roles: Sequence[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        excluded_session_ids: Sequence[str] = (),
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
        ) -> _FtsSearchRows:
            sql = (
                f"SELECT s.project_id, s.agent_id, s.session_id, {_MESSAGE_RECORD_COLUMNS} "
                f"FROM messages AS m {_MESSAGE_RECORD_JOINS} "
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
            params.append(_CANONICAL_SEARCH_SCAN_LIMIT + 1)
            result: builtins.list[tuple[SessionAddress, str, str, str, float]] = []
            candidates = connection.execute(sql, params).fetchmany(_CANONICAL_SEARCH_SCAN_LIMIT + 1)
            complete = len(candidates) <= _CANONICAL_SEARCH_SCAN_LIMIT
            for row in candidates[:_CANONICAL_SEARCH_SCAN_LIMIT]:
                message = message_from_row(row)
                if not matches(_search_projection(message)):
                    continue
                payload = _message_payload(row)
                from core.sessions.sessions import SessionAddress as SessionAddr

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
            return _FtsSearchRows(
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

        if not self.is_fts_available():
            with self._transaction(write=False) as connection:
                return canonical_rows(connection, fallback_reason="fts_unavailable")

        try:
            with self._transaction(write=False) as connection:

                def query_fts(
                    fts_table: str,
                ) -> _FtsSearchRows:
                    sql = (
                        f"SELECT s.project_id, s.agent_id, s.session_id, "
                        f"{_MESSAGE_RECORD_COLUMNS}, bm25({fts_table}) AS rank "
                        f"FROM {fts_table} "
                        f"JOIN messages AS m ON m.message_key = {fts_table}.rowid "
                        f"{_MESSAGE_RECORD_JOINS} "
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
                        from core.sessions.sessions import SessionAddress as SessionAddr

                        found.append(
                            (
                                SessionAddr(
                                    project_id=row["project_id"] or None,
                                    agent_id=row["agent_id"],
                                    session_id=row["session_id"],
                                ),
                                str(row["message_id"]),
                                str(row["timestamp"]),
                                _message_payload(row),
                                float(row["rank"]) if row["rank"] is not None else 0.0,
                            )
                        )
                    return _FtsSearchRows(found, source="fts")

                result = query_fts(FTS_TABLE)
                if not result and trigram_supported and not (roles is not None and "tool" in roles):
                    result = query_fts(FTS_TRIGRAM_TABLE)
                if not result and (roles is None or "tool" in roles):
                    result = canonical_rows(connection, fallback_reason="tool_inclusive")
                return result
        except sqlite3.Error as exc:
            if "fts" in str(exc).lower() or "messages_fts" in str(exc).lower():
                error_message = str(exc)
                with suppress(Exception):
                    self._execute_write(lambda connection: _detach_fts(connection, error_message))
            with self._transaction(write=False) as connection:
                return canonical_rows(connection, fallback_reason="fts_error")

    def archive(self, address: SessionAddress) -> None:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, address)
            connection.execute(
                "UPDATE sessions SET status = 'archived', archived_at = ?, state_revision = state_revision + 1 WHERE session_key = ?",
                (timestamp, state["session_key"]),
            )

        self._execute_write(_fn)

    def move(
        self,
        source: SessionAddress,
        target: SessionAddress,
        prepare_metadata: Callable[[JsonObject, int], None],
    ) -> None:
        """Relocate one complete Session row and its dependent rows atomically."""

        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, source)
            collision = connection.execute(
                "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'live'",
                self._scope(target),
            ).fetchone()
            if collision is not None:
                raise ChatSessionError(f"destination session already exists: {target.session_id}")
            metadata = _session_metadata_from_state(state)
            prepare_metadata(metadata, int(state["message_count"]))
            _json_object(metadata, "session metadata")
            payload, projection = _session_metadata_storage(metadata)
            connection.execute(
                "UPDATE sessions SET project_id = ?, agent_id = ?, session_id = ?, metadata_json = ?, "
                + ", ".join(f"{column} = ?" for column in _SESSION_METADATA_PROJECTION_COLUMNS)
                + ", state_revision = state_revision + 1 WHERE session_key = ?",
                (*self._scope(target), payload, *projection, state["session_key"]),
            )

        self._execute_write(_fn)

    def fork(
        self,
        source: SessionAddress,
        target: SessionAddress,
        prepare_metadata: Callable[[JsonObject, int], None],
    ) -> None:
        """Copy canonical history to a new live Session without activity/journal state."""

        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, source)
            metadata = _session_metadata_from_state(state)
            prepare_metadata(metadata, int(state["message_count"]))
            _json_object(metadata, "session metadata")
            payload, projection = _session_metadata_storage(metadata)
            try:
                target_row = connection.execute(
                    "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, "
                    "created_at, last_message_at, active_sort, message_count, last_message_id, "
                    "history_revision, state_revision, metadata_json, "
                    + ", ".join(_SESSION_METADATA_PROJECTION_COLUMNS)
                    + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, "
                    + ", ".join("?" for _ in _SESSION_METADATA_PROJECTION_COLUMNS)
                    + ")",
                    (
                        uuid.uuid4().hex,
                        *self._scope(target),
                        state["created_at"],
                        state["last_message_at"],
                        state["active_sort"],
                        state["message_count"],
                        state["last_message_id"],
                        state["history_revision"],
                        payload,
                        *projection,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ChatSessionError(
                    f"destination session already exists: {target.session_id}"
                ) from exc
            target_session_key = target_row.lastrowid
            if target_session_key is None:
                raise SessionStoreCorruptError("SQLite did not return a forked Session key")
            _copy_session_messages(
                connection,
                source_session_key=int(state["session_key"]),
                target_session_key=int(target_session_key),
            )
            _mark_fts_write(connection)

        self._execute_write(_fn)

    def restore(self, address: SessionAddress) -> None:
        def _fn(connection: sqlite3.Connection) -> None:
            collision = connection.execute(
                "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'live'",
                self._scope(address),
            ).fetchone()
            if collision is not None:
                raise ChatSessionError(f"live session already exists: {address.session_id}")
            row = connection.execute(
                "SELECT session_key FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'archived' ORDER BY session_key DESC LIMIT 1",
                self._scope(address),
            ).fetchone()
            if row is None:
                raise ChatSessionError(f"archived session does not exist: {address.session_id}")
            connection.execute(
                "UPDATE sessions SET status = 'live', archived_at = NULL, state_revision = state_revision + 1 WHERE session_key = ?",
                (row["session_key"],),
            )

        self._execute_write(_fn)

    def delete(self, address: SessionAddress) -> None:
        def _fn(connection: sqlite3.Connection) -> None:
            state = self._require_live(connection, address)
            connection.execute(
                "DELETE FROM sessions WHERE session_key = ?", (state["session_key"],)
            )

        self._execute_write(_fn)

    def retarget_identity_agent(self, old_agent_id: str, new_agent_id: str) -> None:
        """Rename every global Session address for one Identity Agent atomically."""

        def _fn(connection: sqlite3.Connection) -> None:
            collision = connection.execute(
                "SELECT 1 FROM sessions AS source WHERE source.project_id = '' AND source.agent_id = ? "
                "AND EXISTS (SELECT 1 FROM sessions AS target WHERE target.project_id = '' "
                "AND target.agent_id = ? AND target.session_id = source.session_id AND target.status = 'live') "
                "AND source.status = 'live'",
                (old_agent_id, new_agent_id),
            ).fetchone()
            if collision is not None:
                raise ChatSessionError("destination Agent already has a Session with the same id")
            connection.execute(
                "UPDATE sessions SET agent_id = ?, state_revision = state_revision + 1 "
                "WHERE project_id = '' AND agent_id = ? AND status = 'live'",
                (new_agent_id, old_agent_id),
            )

        self._execute_write(_fn)

    def archive_identity_agent_sessions(self, agent_id: str) -> None:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        def _fn(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE sessions SET status = 'archived', archived_at = ?, state_revision = state_revision + 1 "
                "WHERE project_id = '' AND agent_id = ? AND status = 'live'",
                (timestamp, agent_id),
            )

        self._execute_write(_fn)

    def archive_project_sessions(self, project_id: str) -> None:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        def _fn(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE sessions SET status = 'archived', archived_at = ?, state_revision = state_revision + 1 "
                "WHERE project_id = ? AND status = 'live'",
                (timestamp, project_id),
            )

        self._execute_write(_fn)

    def _require_live(self, connection: sqlite3.Connection, address: SessionAddress) -> sqlite3.Row:
        row = self._find_live(connection, address)
        if row is None:
            raise SessionNotFoundError(f"session does not exist: {address.session_id}")
        return row

    def _find_live(
        self, connection: sqlite3.Connection, address: SessionAddress
    ) -> sqlite3.Row | None:
        row = connection.execute(
            "SELECT * FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'live'",
            self._scope(address),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    @staticmethod
    def _touch_state(connection: sqlite3.Connection, session_key: int) -> None:
        connection.execute(
            "UPDATE sessions SET state_revision = state_revision + 1 WHERE session_key = ?",
            (session_key,),
        )


def _json_object(value: JsonObject, name: str) -> str:
    if not isinstance(value, dict):
        raise ChatSessionError(f"{name} must be an object")
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ChatSessionError(f"{name} must be JSON-serializable") from exc


def _json_from_payload(value: str, name: str) -> JsonObject:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SessionStoreCorruptError(f"invalid {name}") from exc
    if not isinstance(decoded, dict):
        raise SessionStoreCorruptError(f"invalid {name}")
    return decoded


def _json_value_from_payload(value: str, name: str, expected_type: type[Any]) -> Any:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SessionStoreCorruptError(f"invalid {name}") from exc
    if not isinstance(decoded, expected_type):
        raise SessionStoreCorruptError(f"invalid {name}")
    return decoded


def _optional_json(value: Any, name: str) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ChatSessionError(f"{name} is not JSON-serializable") from exc


def _continuation_string(record: JsonObject, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ChatSessionError(f"continuation record {key} must be a non-empty string")
    return value


def _continuation_step(record: JsonObject) -> int:
    value = record.get("step")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ChatSessionError("continuation record step must be a non-negative integer")
    return value


def _next_continuation_ordinal(connection: sqlite3.Connection, table: str, session_key: int) -> int:
    if table not in {
        "continuation_requests",
        "continuation_steps",
        "continuation_operations",
    }:
        raise RuntimeError(f"unsupported continuation table: {table}")
    row = connection.execute(
        f"SELECT COALESCE(MAX(ordinal) + 1, 0) FROM {table} WHERE session_key = ?",
        (session_key,),
    ).fetchone()
    return int(row[0])


def _upsert_continuation_operation(
    connection: sqlite3.Connection,
    session_key: int,
    *,
    tool_call_id: str,
    name: str,
    run_id: str,
    status: str,
    ok: bool | None,
    replace_unknown: bool,
) -> None:
    existing = connection.execute(
        "SELECT ordinal FROM continuation_operations WHERE session_key = ? AND tool_call_id = ?",
        (session_key, tool_call_id),
    ).fetchone()
    if existing is None:
        connection.execute(
            """
            INSERT INTO continuation_operations (
                session_key, tool_call_id, ordinal, name, run_id, status, ok
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_key,
                tool_call_id,
                _next_continuation_ordinal(connection, "continuation_operations", session_key),
                name,
                run_id,
                status,
                None if ok is None else int(ok),
            ),
        )
        return
    if replace_unknown or status == "completed":
        connection.execute(
            """
            UPDATE continuation_operations
            SET name = ?, run_id = ?, status = ?, ok = ?
            WHERE session_key = ? AND tool_call_id = ?
            """,
            (name, run_id, status, None if ok is None else int(ok), session_key, tool_call_id),
        )


def _apply_continuation_record(
    connection: sqlite3.Connection, session_key: int, record: JsonObject
) -> None:
    if record.get("version") != _CONTINUATION_RECORD_VERSION:
        raise ChatSessionError("unsupported continuation record version")
    record_type = record.get("type")
    if record_type == "run_started":
        checkpoint_id = _continuation_string(record, "checkpoint_id")
        run_id = _continuation_string(record, "run_id")
        origin_run_id = _continuation_string(record, "origin_run_id")
        existing = connection.execute(
            "SELECT checkpoint_id FROM continuations WHERE session_key = ?", (session_key,)
        ).fetchone()
        if existing is None or str(existing[0]) != checkpoint_id:
            connection.execute("DELETE FROM continuations WHERE session_key = ?", (session_key,))
            connection.execute(
                """
                INSERT INTO continuations (
                    session_key, checkpoint_id, origin_run_id, latest_run_id, cause, active
                ) VALUES (?, ?, ?, ?, NULL, 1)
                """,
                (session_key, checkpoint_id, origin_run_id, run_id),
            )
        else:
            connection.execute(
                """
                UPDATE continuations SET latest_run_id = ?, cause = NULL, active = 1
                WHERE session_key = ?
                """,
                (run_id, session_key),
            )
        if record.get("request") is not None:
            try:
                request_json = json.dumps(
                    record["request"], ensure_ascii=False, separators=(",", ":")
                )
            except (TypeError, ValueError) as exc:
                raise ChatSessionError("continuation request is not JSON-serializable") from exc
            connection.execute(
                """
                INSERT INTO continuation_requests (session_key, ordinal, request_json)
                VALUES (?, ?, ?)
                """,
                (
                    session_key,
                    _next_continuation_ordinal(connection, "continuation_requests", session_key),
                    request_json,
                ),
            )
        return

    continuation = connection.execute(
        "SELECT checkpoint_id FROM continuations WHERE session_key = ?", (session_key,)
    ).fetchone()
    if continuation is None:
        return
    if record_type in {"stream_delta", "stream_attempt_discarded", "assistant_boundary"}:
        run_id = _continuation_string(record, "run_id")
        step = _continuation_step(record)
        existing = connection.execute(
            """
            SELECT reasoning, content, assistant_message_id, interrupted
            FROM continuation_steps WHERE session_key = ? AND run_id = ? AND step = ?
            """,
            (session_key, run_id, step),
        ).fetchone()
        if record_type == "stream_attempt_discarded":
            connection.execute(
                "DELETE FROM continuation_steps WHERE session_key = ? AND run_id = ? AND step = ?",
                (session_key, run_id, step),
            )
            return
        reasoning = "" if existing is None else str(existing["reasoning"])
        content = "" if existing is None else str(existing["content"])
        assistant_message_id = None if existing is None else existing["assistant_message_id"]
        interrupted = False if existing is None else bool(existing["interrupted"])
        if record_type == "stream_delta":
            if isinstance(record.get("reasoning_delta"), str):
                reasoning += str(record["reasoning_delta"])
            if isinstance(record.get("content_delta"), str):
                content += str(record["content_delta"])
        else:
            if isinstance(record.get("reasoning"), str):
                reasoning = str(record["reasoning"])
            if isinstance(record.get("content"), str):
                content = str(record["content"])
            value = record.get("message_id")
            assistant_message_id = value if isinstance(value, str) else None
            interrupted = record.get("interrupted") is True
        if existing is None:
            connection.execute(
                """
                INSERT INTO continuation_steps (
                    session_key, run_id, step, ordinal, reasoning, content,
                    assistant_message_id, interrupted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_key,
                    run_id,
                    step,
                    _next_continuation_ordinal(connection, "continuation_steps", session_key),
                    reasoning,
                    content,
                    assistant_message_id,
                    int(interrupted),
                ),
            )
        else:
            connection.execute(
                """
                UPDATE continuation_steps
                SET reasoning = ?, content = ?, assistant_message_id = ?, interrupted = ?
                WHERE session_key = ? AND run_id = ? AND step = ?
                """,
                (
                    reasoning,
                    content,
                    assistant_message_id,
                    int(interrupted),
                    session_key,
                    run_id,
                    step,
                ),
            )
        if record_type == "assistant_boundary" and isinstance(record.get("tool_calls"), list):
            for tool_call in record["tool_calls"]:
                if not isinstance(tool_call, dict):
                    continue
                tool_call_id = tool_call.get("id")
                name = tool_call.get("name")
                if (
                    isinstance(tool_call_id, str)
                    and tool_call_id
                    and isinstance(name, str)
                    and name
                ):
                    _upsert_continuation_operation(
                        connection,
                        session_key,
                        tool_call_id=tool_call_id,
                        name=name,
                        run_id=run_id,
                        status="unknown",
                        ok=None,
                        replace_unknown=False,
                    )
        return
    if record_type == "tool_started":
        _upsert_continuation_operation(
            connection,
            session_key,
            tool_call_id=_continuation_string(record, "tool_call_id"),
            name=_continuation_string(record, "name"),
            run_id=_continuation_string(record, "run_id"),
            status="unknown",
            ok=None,
            replace_unknown=True,
        )
        return
    if record_type == "tool_result":
        _upsert_continuation_operation(
            connection,
            session_key,
            tool_call_id=_continuation_string(record, "tool_call_id"),
            name=_continuation_string(record, "name"),
            run_id=_continuation_string(record, "run_id"),
            status="completed",
            ok=record.get("ok") is True,
            replace_unknown=True,
        )
        return
    if record_type == "run_interrupted":
        cause = _continuation_string(record, "cause")
        if cause not in {"user", "provider", "network", "timeout", "process_restart", "internal"}:
            raise ChatSessionError(f"unsupported continuation cause: {cause}")
        connection.execute(
            """
            UPDATE continuations SET latest_run_id = ?, cause = ?, active = 0
            WHERE session_key = ?
            """,
            (_continuation_string(record, "run_id"), cause, session_key),
        )
        return
    if record_type == "resolved" and record.get("checkpoint_id") == continuation[0]:
        connection.execute("DELETE FROM continuations WHERE session_key = ?", (session_key,))


def _continuation_records(connection: sqlite3.Connection, session_key: int) -> list[JsonObject]:
    continuation = connection.execute(
        "SELECT * FROM continuations WHERE session_key = ?", (session_key,)
    ).fetchone()
    if continuation is None:
        return []
    requests = connection.execute(
        "SELECT request_json FROM continuation_requests WHERE session_key = ? ORDER BY ordinal",
        (session_key,),
    ).fetchall()
    base: JsonObject = {
        "version": _CONTINUATION_RECORD_VERSION,
        "type": "run_started",
        "run_id": str(continuation["latest_run_id"]),
        "timestamp": _CONTINUATION_SYNTHETIC_TIMESTAMP,
        "checkpoint_id": str(continuation["checkpoint_id"]),
        "origin_run_id": str(continuation["origin_run_id"]),
    }
    records: list[JsonObject] = []
    if requests:
        for request in requests:
            records.append({**base, "request": json.loads(str(request[0]))})
    else:
        records.append(base)
    for step in connection.execute(
        "SELECT * FROM continuation_steps WHERE session_key = ? ORDER BY ordinal",
        (session_key,),
    ):
        records.append(
            {
                "version": _CONTINUATION_RECORD_VERSION,
                "type": "assistant_boundary",
                "run_id": str(step["run_id"]),
                "timestamp": _CONTINUATION_SYNTHETIC_TIMESTAMP,
                "step": int(step["step"]),
                "message_id": step["assistant_message_id"],
                "reasoning": str(step["reasoning"]),
                "content": str(step["content"]),
                "interrupted": bool(step["interrupted"]),
            }
        )
    for operation in connection.execute(
        "SELECT * FROM continuation_operations WHERE session_key = ? ORDER BY ordinal",
        (session_key,),
    ):
        common: JsonObject = {
            "version": _CONTINUATION_RECORD_VERSION,
            "run_id": str(operation["run_id"]),
            "timestamp": _CONTINUATION_SYNTHETIC_TIMESTAMP,
            "tool_call_id": str(operation["tool_call_id"]),
            "name": str(operation["name"]),
        }
        records.append({**common, "type": "tool_started"})
        if operation["status"] == "completed":
            records.append({**common, "type": "tool_result", "ok": bool(operation["ok"])})
    if not bool(continuation["active"]):
        records.append(
            {
                "version": _CONTINUATION_RECORD_VERSION,
                "type": "run_interrupted",
                "run_id": str(continuation["latest_run_id"]),
                "timestamp": _CONTINUATION_SYNTHETIC_TIMESTAMP,
                "cause": str(continuation["cause"]),
            }
        )
    return records


def _message_base_row(message: ChatMessage) -> tuple[Any, ...]:
    message.validate()
    content = (
        message.content if isinstance(message.content, str) and message.role != "tool" else None
    )
    content_blocks = message.content if isinstance(message.content, list) else None
    if content_blocks is None:
        content_search = None
        content_blocks_json = None
    else:
        from core.chat.content_blocks import content_block_to_dict
        from core.recall.canonical import content_to_text

        content_search = content_to_text(content_blocks)
        content_blocks_json = _optional_json(
            [content_block_to_dict(block) for block in content_blocks], "content"
        )
    return (
        message.id,
        message.role,
        message.timestamp,
        content,
        content_blocks_json,
        content_search,
        message.model,
        int(message.role != "history_edit"),
        int(_message_is_searchable(message)),
    )


def _split_structured_fields(
    payload: JsonObject | None,
    validators: dict[str, Callable[[Any], bool]],
) -> tuple[dict[str, Any], str | None, bool]:
    """Promote stable fields while retaining unknown/future fields losslessly."""
    if payload is None:
        return {}, None, False
    remaining = dict(payload)
    promoted: dict[str, Any] = {}
    for key, validator in validators.items():
        if key in remaining and validator(remaining[key]):
            promoted[key] = remaining.pop(key)
    return promoted, _optional_json(remaining or None, "structured extras"), True


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_string(value: Any) -> bool:
    return isinstance(value, str)


def _timing_fields(payload: JsonObject | None) -> tuple[dict[str, Any], str | None, bool]:
    return _split_structured_fields(
        payload,
        {
            "started_at": _is_string,
            "completed_at": _is_string,
            "duration_ms": _is_non_negative_int,
        },
    )


def _deactivate_history_tail(
    connection: sqlite3.Connection, session_key: int, target_message_id: str
) -> None:
    target = connection.execute(
        """
        SELECT message_key, seq
        FROM messages
        WHERE session_key = ? AND message_id = ? AND role = 'user' AND active = 1
        ORDER BY seq
        LIMIT 1
        """,
        (session_key, target_message_id),
    ).fetchone()
    if target is None:
        raise ChatSessionError(f"history edit target is not active: {target_message_id}")
    keys = [
        int(row[0])
        for row in connection.execute(
            "SELECT message_key FROM messages WHERE session_key = ? AND seq >= ? AND active = 1",
            (session_key, int(target["seq"])),
        ).fetchall()
    ]
    for message_key in keys:
        _delete_fts_message(connection, message_key)
    connection.execute(
        "UPDATE messages SET active = 0 WHERE session_key = ? AND seq >= ? AND active = 1",
        (session_key, int(target["seq"])),
    )


def _insert_message(
    connection: sqlite3.Connection,
    session_key: int,
    sequence: int,
    message: ChatMessage,
    *,
    index_fts: bool = True,
) -> int:
    if message.role == "history_edit":
        if message.target_message_id is None:
            raise ChatSessionError("history edit target is missing")
        _deactivate_history_tail(connection, session_key, message.target_message_id)
    cursor = connection.execute(
        _MESSAGE_INSERT,
        (session_key, sequence, *_message_base_row(message)),
    )
    if cursor.lastrowid is None:
        raise SessionStoreCorruptError("SQLite did not return a canonical message key")
    message_key = int(cursor.lastrowid)

    if message.role == "assistant":
        usage, usage_extra, usage_present = _split_structured_fields(
            message.usage,
            {
                "input_tokens": _is_non_negative_int,
                "output_tokens": _is_non_negative_int,
                "cache_read_tokens": _is_non_negative_int,
                "cache_write_tokens": _is_non_negative_int,
                "reasoning_tokens": _is_non_negative_int,
                "estimated": _is_bool,
                "input_tokens_estimated": _is_bool,
                "output_tokens_estimated": _is_bool,
            },
        )
        reasoning_timing, reasoning_timing_extra, _timing_present = _timing_fields(
            message.reasoning_timing
        )
        connection.execute(
            """
            INSERT INTO assistant_messages (
                message_key, reasoning, reasoning_meta_json, reasoning_scope,
                reasoning_started_at, reasoning_completed_at, reasoning_duration_ms,
                reasoning_timing_extra_json, phase, input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens, reasoning_tokens, usage_estimated,
                input_tokens_estimated, output_tokens_estimated, usage_present,
                usage_extra_json, tool_calls_present, interrupted, interruption_cause
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_key,
                message.reasoning,
                _optional_json(message.reasoning_meta, "reasoning_meta"),
                message.reasoning_scope,
                reasoning_timing.get("started_at"),
                reasoning_timing.get("completed_at"),
                reasoning_timing.get("duration_ms"),
                reasoning_timing_extra,
                message.phase,
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                usage.get("cache_read_tokens"),
                usage.get("cache_write_tokens"),
                usage.get("reasoning_tokens"),
                None if "estimated" not in usage else int(bool(usage["estimated"])),
                (
                    None
                    if "input_tokens_estimated" not in usage
                    else int(bool(usage["input_tokens_estimated"]))
                ),
                (
                    None
                    if "output_tokens_estimated" not in usage
                    else int(bool(usage["output_tokens_estimated"]))
                ),
                int(usage_present),
                usage_extra,
                int(message.tool_calls is not None),
                int(message.interrupted),
                message.interruption_cause,
            ),
        )
        for ordinal, tool_call in enumerate(message.tool_calls or ()):
            rejection = tool_call.rejection
            connection.execute(
                """
                INSERT INTO tool_calls (
                    message_key, ordinal, tool_call_id, name, arguments_json,
                    rejection_code, rejection_message, rejection_fingerprint,
                    argument_sequence_index, argument_sequence_length
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_key,
                    ordinal,
                    tool_call.id,
                    tool_call.name,
                    _json_object(tool_call.arguments, "tool call arguments"),
                    None if rejection is None else rejection.code,
                    None if rejection is None else rejection.message,
                    None if rejection is None else rejection.fingerprint,
                    tool_call.argument_sequence_index,
                    tool_call.argument_sequence_length,
                ),
            )
        for ordinal, reference in enumerate(message.output_files or ()):
            connection.execute(
                """
                INSERT INTO assistant_output_files (
                    message_key, ordinal, path, line_index, start_index, end_index
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_key,
                    ordinal,
                    reference.path,
                    reference.line_index,
                    reference.start_index,
                    reference.end_index,
                ),
            )
    elif message.role == "user" and message.sender is not None:
        connection.execute(
            """
            INSERT INTO user_message_senders (
                message_key, sender_id, display_name, role
            ) VALUES (?, ?, ?, ?)
            """,
            (message_key, message.sender.id, message.sender.display_name, message.sender.role),
        )
    elif message.role == "tool":
        timing, timing_extra, _timing_present = _timing_fields(message.timing)
        linked = connection.execute(
            """
            SELECT tc.tool_call_key
            FROM tool_calls AS tc
            JOIN messages AS owner ON owner.message_key = tc.message_key
            WHERE owner.session_key = ? AND owner.seq < ? AND tc.tool_call_id = ?
            ORDER BY owner.seq DESC, tc.ordinal DESC
            LIMIT 1
            """,
            (session_key, sequence, message.tool_call_id),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO tool_messages (
                message_key, tool_call_key, tool_call_id, name, result_content,
                started_at, completed_at, duration_ms, timing_extra_json, display_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_key,
                None if linked is None else int(linked[0]),
                message.tool_call_id,
                message.name,
                message.content,
                timing.get("started_at"),
                timing.get("completed_at"),
                timing.get("duration_ms"),
                timing_extra,
                _optional_json(message.tool_display, "tool_display"),
            ),
        )
    elif message.role == "error":
        connection.execute(
            "INSERT INTO error_messages (message_key, error_kind) VALUES (?, ?)",
            (message_key, message.error_kind),
        )
    elif message.role == "compaction_checkpoint":
        usage, usage_extra, usage_present = _split_structured_fields(
            message.usage,
            {
                "compacted_token_count": _is_non_negative_int,
                "context_tokens_before": _is_non_negative_int,
                "context_tokens_after": _is_non_negative_int,
                "compaction_duration_ms": _is_non_negative_int,
            },
        )
        connection.execute(
            """
            INSERT INTO compaction_checkpoints (
                message_key, tail_boundary_id, projection_json, policy, strategy,
                compacted_token_count, context_tokens_before, context_tokens_after,
                compaction_duration_ms, usage_present, usage_extra_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_key,
                message.tail_boundary_id,
                _optional_json(message.projection, "projection"),
                message.compaction_policy,
                message.compaction_strategy,
                usage.get("compacted_token_count"),
                usage.get("context_tokens_before"),
                usage.get("context_tokens_after"),
                usage.get("compaction_duration_ms"),
                int(usage_present),
                usage_extra,
            ),
        )
    elif message.role == "run_summary":
        timing, timing_extra, _timing_present = _timing_fields(message.timing)
        changes, changes_extra, changes_present = _split_structured_fields(
            message.change_stats,
            {
                "files": _is_non_negative_int,
                "added": _is_non_negative_int,
                "removed": _is_non_negative_int,
                "paths": lambda value: (
                    isinstance(value, list) and all(isinstance(path, str) for path in value)
                ),
            },
        )
        connection.execute(
            """
            INSERT INTO run_summaries (
                message_key, run_id, work_id, status, started_at, completed_at,
                duration_ms, timing_extra_json, iteration_count, changed_files,
                lines_added, lines_removed, change_stats_extra_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_key,
                message.run_id,
                message.work_id,
                message.status,
                timing.get("started_at"),
                timing.get("completed_at"),
                timing.get("duration_ms"),
                timing_extra,
                message.iteration_count,
                changes.get("files") if changes_present else None,
                changes.get("added") if changes_present else None,
                changes.get("removed") if changes_present else None,
                changes_extra,
            ),
        )
        for ordinal, path in enumerate(changes.get("paths", ())):
            connection.execute(
                "INSERT INTO run_change_paths (message_key, ordinal, path) VALUES (?, ?, ?)",
                (message_key, ordinal, path),
            )
    elif message.role == "history_edit":
        connection.execute(
            "INSERT INTO history_edits (message_key, target_message_id) VALUES (?, ?)",
            (message_key, message.target_message_id),
        )

    if index_fts:
        _insert_fts_message(connection, message_key)
    return message_key


def message_from_row(row: sqlite3.Row) -> ChatMessage:
    """Reconstruct one canonical ChatMessage from normalized SQLite columns."""
    from core.chat.messages import ChatMessage

    try:
        data: JsonObject = {
            "id": str(row["message_id"]),
            "role": str(row["role"]),
            "timestamp": str(row["timestamp"]),
        }
        content_blocks = row["content_blocks_json"]
        if content_blocks is not None:
            data["content"] = json.loads(str(content_blocks))
        elif row["content"] is not None or row["role_content"] is not None:
            data["content"] = str(
                row["content"] if row["content"] is not None else row["role_content"]
            )
        scalar_fields = (
            "model",
            "reasoning",
            "reasoning_scope",
            "phase",
            "tool_call_id",
            "name",
            "error_kind",
            "tail_boundary_id",
            "compaction_policy",
            "compaction_strategy",
            "run_id",
            "work_id",
            "status",
            "iteration_count",
            "target_message_id",
            "interruption_cause",
        )
        for field in scalar_fields:
            if row[field] is not None:
                data[field] = row[field]
        json_fields = {
            "reasoning_meta_json": "reasoning_meta",
            "tool_display_json": "tool_display",
            "projection_json": "projection",
        }
        for column, field in json_fields.items():
            if row[column] is not None:
                data[field] = json.loads(str(row[column]))
        if (
            row["reasoning_started_at"] is not None
            or row["reasoning_timing_extra_json"] is not None
        ):
            timing = (
                {}
                if row["reasoning_timing_extra_json"] is None
                else json.loads(str(row["reasoning_timing_extra_json"]))
            )
            for key, column in (
                ("started_at", "reasoning_started_at"),
                ("completed_at", "reasoning_completed_at"),
                ("duration_ms", "reasoning_duration_ms"),
            ):
                if row[column] is not None:
                    timing[key] = row[column]
            data["reasoning_timing"] = timing
        if bool(row["usage_present"] or row["compaction_usage_present"]):
            extra_column = (
                "usage_extra_json" if row["usage_present"] else "compaction_usage_extra_json"
            )
            usage = {} if row[extra_column] is None else json.loads(str(row[extra_column]))
            usage_columns = (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("cache_read_tokens", "cache_read_tokens"),
                ("cache_write_tokens", "cache_write_tokens"),
                ("reasoning_tokens", "reasoning_tokens"),
                ("estimated", "usage_estimated"),
                ("input_tokens_estimated", "input_tokens_estimated"),
                ("output_tokens_estimated", "output_tokens_estimated"),
                ("compacted_token_count", "compacted_token_count"),
                ("context_tokens_before", "context_tokens_before"),
                ("context_tokens_after", "context_tokens_after"),
                ("compaction_duration_ms", "compaction_duration_ms"),
            )
            for key, column in usage_columns:
                if row[column] is not None:
                    usage[key] = (
                        bool(row[column])
                        if column
                        in {
                            "usage_estimated",
                            "input_tokens_estimated",
                            "output_tokens_estimated",
                        }
                        else row[column]
                    )
            data["usage"] = usage
        if row["timing_started_at"] is not None or row["run_started_at"] is not None:
            is_run = row["run_started_at"] is not None
            prefix = "run_" if is_run else "timing_"
            extra_column = "run_timing_extra_json" if is_run else "timing_extra_json"
            timing = {} if row[extra_column] is None else json.loads(str(row[extra_column]))
            for key, suffix in (
                ("started_at", "started_at"),
                ("completed_at", "completed_at"),
                ("duration_ms", "duration_ms"),
            ):
                value = row[f"{prefix}{suffix}"]
                if value is not None:
                    timing[key] = value
            data["timing"] = timing
        if bool(row["tool_calls_present"]):
            calls: list[JsonObject] = []
            for values in json.loads(str(row["tool_call_rows_json"] or "[]")):
                call: JsonObject = {
                    "id": values[0],
                    "name": values[1],
                    "arguments": json.loads(values[2]),
                }
                if values[3] is not None:
                    call["rejection"] = {
                        "code": values[3],
                        "message": values[4],
                        "fingerprint": values[5],
                    }
                if values[6] is not None:
                    call["argument_sequence_index"] = values[6]
                    call["argument_sequence_length"] = values[7]
                calls.append(call)
            data["tool_calls"] = calls
        if row["sender_id"] is not None:
            data["sender"] = {
                "id": row["sender_id"],
                "display_name": row["sender_display_name"],
                "role": row["sender_role"],
            }
        if row["output_file_rows_json"] is not None:
            data["output_files"] = [
                {
                    "path": values[0],
                    "line_index": values[1],
                    **({} if values[2] is None else {"start_index": values[2]}),
                    **({} if values[3] is None else {"end_index": values[3]}),
                }
                for values in json.loads(str(row["output_file_rows_json"]))
            ]
        if row["changed_files"] is not None:
            changes = (
                {}
                if row["change_stats_extra_json"] is None
                else json.loads(str(row["change_stats_extra_json"]))
            )
            changes.update(
                {
                    "files": row["changed_files"],
                    "added": row["lines_added"],
                    "removed": row["lines_removed"],
                    "paths": json.loads(str(row["change_paths_json"] or "[]")),
                }
            )
            data["change_stats"] = changes
        if bool(row["interrupted"]):
            data["interrupted"] = True
        return ChatMessage.from_dict(data)
    except (json.JSONDecodeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise SessionStoreCorruptError("invalid canonical Session message") from exc


def _message_payload(row: sqlite3.Row) -> str:
    try:
        return json.dumps(
            message_from_row(row).to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SessionStoreCorruptError("invalid canonical Session message") from exc


def messages_from_connection(connection: sqlite3.Connection, session_key: int) -> list[ChatMessage]:
    """Decode one generation for offline verification/export callers."""
    rows = connection.execute(
        _message_records_sql(where="m.session_key = ?", order_by="ORDER BY m.seq"),
        (session_key,),
    ).fetchall()
    return [message_from_row(row) for row in rows]


def continuation_from_connection(
    connection: sqlite3.Connection, session_key: int
) -> list[JsonObject]:
    """Project normalized current continuation state through the legacy-shaped facade."""
    return _continuation_records(connection, session_key)


QUARANTINE_DIRECTORY_NAME = "session-quarantine"


def quarantine_database(database_path: Path) -> QuarantineResult:
    """Move a distrusted database bundle through the snapshot owner."""

    from core.sessions.snapshots import quarantine_database as quarantine

    return quarantine(Path(database_path))


def database_sidecar_paths(database_path: Path) -> tuple[Path, ...]:
    """Return the SQLite sidecar files for *database_path*."""

    return (
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
        Path(f"{database_path}-journal"),
    )
