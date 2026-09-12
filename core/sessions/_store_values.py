"""Relational Session metadata, scope and value validation."""
# ruff: noqa: E501

from __future__ import annotations

import builtins
import json
import logging
import sqlite3
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from core.chat.errors import ChatSessionError
from core.sessions.errors import (
    SessionNotFoundError,
    SessionStoreCorruptError,
)
from core.utils.ids import new_id

if TYPE_CHECKING:
    from core.sessions._types import SessionAddress


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
    "calendar",
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
_RECALL_USER_FACING_RUN_KINDS = ("user", "channel", "cron", "calendar")
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
_LIST_VISIBILITY_OWNER_MANAGED = 1 << 10


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
        "NOT EXISTS (SELECT 1 FROM temporary_session_bindings AS owner_binding "
        "WHERE owner_binding.session_key = sessions.session_key) AND "
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


_OFFLINE_IMPORT_CACHE_KIB = 262_144
_SEARCH_RESULT_LIMIT = 1_000
_CANONICAL_SEARCH_SCAN_LIMIT = 10_000


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


def _json_object(value: JsonObject, name: str) -> str:
    if not isinstance(value, dict):
        raise ChatSessionError(f"{name} must be an object")
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ChatSessionError(f"{name} must be JSON-serializable") from exc


def _canonical_json_payload(payload: str) -> str:
    """Normalize a stored JSON object before comparing protected configurations."""

    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SessionStoreCorruptError("invalid temporary Session config") from exc
    if not isinstance(value, dict):
        raise SessionStoreCorruptError("invalid temporary Session config")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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


def _scope(address: SessionAddress) -> tuple[str, str, str]:
    return (address.project_id or "", address.agent_id, address.session_id)


def _address(row: sqlite3.Row) -> SessionAddress:
    from core.sessions._types import SessionAddress

    return SessionAddress(
        project_id=row["project_id"] or None,
        agent_id=row["agent_id"],
        session_id=row["session_id"],
    )


def _allocate_address(connection: sqlite3.Connection, scope: SessionAddress) -> SessionAddress:
    from core.sessions._types import SessionAddress

    def available(candidate: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? LIMIT 1",
                _scope(SessionAddress(scope.project_id, scope.agent_id, candidate)),
            ).fetchone()
            is None
        )

    return SessionAddress(scope.project_id, scope.agent_id, new_id("ses", claim=available))


def _reject_owner_managed_mutation(connection: sqlite3.Connection, state: sqlite3.Row) -> None:
    binding = connection.execute(
        "SELECT 1 FROM temporary_session_bindings WHERE session_key = ?",
        (state["session_key"],),
    ).fetchone()
    if binding is not None:
        raise ChatSessionError(
            "This Session is managed by an Extension. Use that Extension to resume it."
        )


def _reject_owner_managed_scope_mutation(
    connection: sqlite3.Connection, where: str, params: tuple[Any, ...]
) -> None:
    binding = connection.execute(
        "SELECT 1 FROM temporary_session_bindings AS b "
        "JOIN sessions AS s ON s.session_key = b.session_key WHERE " + where,
        params,
    ).fetchone()
    if binding is not None:
        raise ChatSessionError(
            "This Session is managed by an Extension. Use that Extension to resume it."
        )


def _require_live(connection: sqlite3.Connection, address: SessionAddress) -> sqlite3.Row:
    row = _find_live(connection, address)
    if row is None:
        raise SessionNotFoundError(f"session does not exist: {address.session_id}")
    return row


def _find_live(connection: sqlite3.Connection, address: SessionAddress) -> sqlite3.Row | None:
    row = connection.execute(
        "SELECT * FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'live'",
        _scope(address),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def _touch_state(connection: sqlite3.Connection, session_key: int) -> None:
    connection.execute(
        "UPDATE sessions SET state_revision = state_revision + 1 WHERE session_key = ?",
        (session_key,),
    )
