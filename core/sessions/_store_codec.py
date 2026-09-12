"""Canonical Message records, structured payloads and graph copying."""
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from core.chat.errors import ChatSessionError
from core.sessions import _store_fts, _store_values
from core.sessions._types import JsonObject
from core.sessions.errors import SessionStoreCorruptError

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage


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
    _store_fts._insert_fts_session(connection, target_session_key)


BUSY_TIMEOUT_MS = 1_000


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
    return promoted, _store_values._optional_json(remaining or None, "structured extras"), True


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_string(value: Any) -> bool:
    return isinstance(value, str)


def _timing_fields(
    payload: JsonObject | None,
) -> tuple[dict[str, Any], str | None, bool]:
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
        _store_fts._delete_fts_message(connection, message_key)
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
        _store_values._MESSAGE_INSERT,
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
                _store_values._optional_json(message.reasoning_meta, "reasoning_meta"),
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
                    _store_values._json_object(tool_call.arguments, "tool call arguments"),
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
                _store_values._optional_json(message.tool_display, "tool_display"),
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
                _store_values._optional_json(message.projection, "projection"),
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
        _store_fts._insert_fts_message(connection, message_key)
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
        _store_values._message_records_sql(where="m.session_key = ?", order_by="ORDER BY m.seq"),
        (session_key,),
    ).fetchall()
    return [message_from_row(row) for row in rows]


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
        content_blocks_json = _store_values._optional_json(
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
        int(_store_fts._message_is_searchable(message)),
    )
