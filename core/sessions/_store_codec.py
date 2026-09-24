"""Canonical Message records, structured payloads and graph copying."""
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from core.chat.errors import ChatSessionError
from core.sessions import _store_fts, _store_values
from core.sessions._types import JsonObject, SessionRunCompletion
from core.sessions.errors import SessionStoreCorruptError
from core.utils.ids import new_id

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage


def _copy_session_messages(
    connection: sqlite3.Connection,
    *,
    source_session_key: int,
    target_session_key: int,
) -> None:
    """Fork a relational snapshot with new storage identities and exact provenance."""
    offset = _store_values._allocate_history_key(connection)
    maximum = connection.execute(
        "SELECT COALESCE(MAX(message_key),0) FROM history_records WHERE session_key=?",
        (source_session_key,),
    ).fetchone()[0]
    connection.execute(
        "UPDATE store_meta SET value=? WHERE key='history_identity'", (str(offset + maximum),)
    )

    def copy(table, replacements, where, params):
        columns = [
            str(row[1])
            for row in connection.execute(f"PRAGMA table_xinfo({table})")
            if not row[6] and replacements.get(str(row[1]), "") is not None
        ]
        expressions = [replacements.get(column, "source." + column) for column in columns]
        connection.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) SELECT {', '.join(expressions)} "
            f"FROM {table} source WHERE {where}",
            params,
        )

    scope = "source.session_key=?"
    replacement = {"session_key": str(target_session_key)}
    copy(
        "runs",
        {
            **replacement,
            "run_key": None,
            "status": "CASE WHEN source.status='running' THEN 'interrupted' ELSE source.status END",
            "completed_at": "CASE WHEN source.status='running' THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE source.completed_at END",
            "completion_reason": "CASE WHEN source.status='running' THEN 'fork_snapshot' ELSE source.completion_reason END",
            "contributes_to_activity": "0",
            "terminal_key": f"source.terminal_key + {offset}",
            "origin_generation_id": f"COALESCE(source.origin_generation_id, (SELECT generation_id FROM sessions WHERE session_key={source_session_key}))",
        },
        scope,
        (source_session_key,),
    )
    copy(
        "messages",
        {**replacement, "message_key": f"source.message_key + {offset}"},
        scope,
        (source_session_key,),
    )
    for table in (
        "assistant_messages",
        "assistant_output_files",
        "user_message_senders",
        "error_messages",
        "tool_calls",
    ):
        replacements: dict[str, str | None] = {"message_key": f"source.message_key + {offset}"}
        if table == "tool_calls":
            replacements.update(
                tool_call_key=None,
                result_key=f"source.result_key + {offset}",
                status="CASE WHEN source.status IN ('pending','running') THEN 'interrupted' ELSE source.status END",
                completed_at="CASE WHEN source.status IN ('pending','running') THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE source.completed_at END",
            )
        copy(
            table,
            replacements,
            "source.message_key IN (SELECT message_key FROM messages WHERE session_key=?)",
            (source_session_key,),
        )
    copy(
        "compaction_checkpoints",
        {**replacement, "snapshot_key": f"source.snapshot_key + {offset}"},
        scope,
        (source_session_key,),
    )
    copy(
        "history_edits",
        {**replacement, "edit_key": f"source.edit_key + {offset}"},
        scope,
        (source_session_key,),
    )
    connection.execute(
        "INSERT INTO run_change_paths(run_key, ordinal, path) "
        "SELECT target.run_key, p.ordinal, p.path FROM run_change_paths p "
        "JOIN runs source ON source.run_key=p.run_key JOIN runs target "
        "ON target.session_key=? AND target.run_id=source.run_id WHERE source.session_key=?",
        (target_session_key, source_session_key),
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
        FROM history_records
        WHERE session_key = ? AND message_id = ? AND role = 'user' AND active = 1
        ORDER BY seq
        LIMIT 1
        """,
        (session_key, target_message_id),
    ).fetchone()
    if target is None:
        raise ChatSessionError(f"history edit target is not active: {target_message_id}")
    floor = int(target["seq"])
    _store_fts._delete_fts_session(connection, session_key, from_sequence=floor)
    connection.execute(
        "UPDATE messages SET active=0 WHERE session_key=? AND seq>=?", (session_key, floor)
    )
    connection.execute(
        "UPDATE compaction_checkpoints SET active=0 WHERE session_key=? AND seq>=?",
        (session_key, floor),
    )
    connection.execute(
        "UPDATE runs SET terminal_active=0 WHERE session_key=? AND terminal_sequence>=?",
        (session_key, floor),
    )
    connection.execute(
        "UPDATE tool_calls SET result_active=0 WHERE result_sequence>=? AND message_key IN (SELECT message_key FROM messages WHERE session_key=?)",
        (floor, session_key),
    )


def _insert_message(
    connection: sqlite3.Connection,
    session_key: int,
    sequence: int,
    message: ChatMessage,
    *,
    index_fts: bool = True,
    run_id: str | None = None,
    assistant_message_id: str | None = None,
) -> int:
    if run_id is not None:
        run = connection.execute(
            "SELECT status,origin_generation_id FROM runs WHERE session_key=? AND run_id=?",
            (session_key, run_id),
        ).fetchone()
        if run is None or run["status"] != "running" or run["origin_generation_id"] is not None:
            raise ChatSessionError("Messages require an admitted, running Run in this Session")
    if message.role in {"history_edit", "agent_takeover"}:
        connection.execute(
            "UPDATE sessions SET history_reset_sequence=? WHERE session_key=?",
            (sequence + 1, session_key),
        )
    if message.role == "tool":
        return _complete_tool(
            connection,
            session_key,
            sequence,
            message,
            run_id=run_id,
            assistant_message_id=assistant_message_id,
            index_fts=index_fts,
        )
    if message.role == "run_summary":
        return _finish_run(connection, session_key, sequence, message)
    if message.role == "compaction_checkpoint":
        key = _save_context(connection, session_key, sequence, message, run_id=run_id)
        if index_fts:
            _store_fts._insert_fts_message(connection, key)
        return key
    if message.role == "history_edit":
        if message.target_message_id is None:
            raise ChatSessionError("history edit target is missing")
        _deactivate_history_tail(connection, session_key, message.target_message_id)
        cursor = connection.execute(
            "INSERT INTO history_edits(edit_key, session_key, seq, message_id, timestamp, target_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                _store_values._allocate_history_key(connection),
                session_key,
                sequence,
                message.id,
                message.timestamp,
                message.target_message_id,
            ),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)
    cursor = connection.execute(
        _store_values._MESSAGE_INSERT,
        (
            _store_values._allocate_history_key(connection),
            session_key,
            sequence,
            *_message_base_row(message),
            run_id,
        ),
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
                reasoning_timing_extra_json, phase, input_tokens, output_tokens, reasoning_summary_json,
                cache_read_tokens, cache_write_tokens, reasoning_tokens, usage_estimated,
                input_tokens_estimated, output_tokens_estimated, usage_present,
                usage_extra_json, tool_calls_present, interrupted, interruption_cause
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(message.reasoning_summary, ensure_ascii=False)
                if message.reasoning_summary is not None
                else None,
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
    elif message.role == "error":
        connection.execute(
            "INSERT INTO error_messages (message_key, error_kind) VALUES (?, ?)",
            (message_key, message.error_kind),
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
            "reasoning_summary_json": "reasoning_summary",
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
    return _message_json(message_from_row(row))


def _message_json(message: ChatMessage) -> str:
    try:
        return json.dumps(message.to_dict(), ensure_ascii=False, separators=(",", ":"))
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


def _save_context(
    connection: sqlite3.Connection,
    session_key: int,
    sequence: int,
    message: ChatMessage,
    *,
    run_id: str | None,
) -> int:
    usage, usage_extra, usage_present = _split_structured_fields(
        message.usage,
        {
            "compacted_token_count": _is_non_negative_int,
            "context_tokens_before": _is_non_negative_int,
            "context_tokens_after": _is_non_negative_int,
            "compaction_duration_ms": _is_non_negative_int,
        },
    )
    cursor = connection.execute(
        """
        INSERT INTO compaction_checkpoints (
            snapshot_key, session_key, seq, run_id, message_id, timestamp, content, tail_boundary_id, projection_json, policy, strategy,
            compacted_token_count, context_tokens_before, context_tokens_after,
            compaction_duration_ms, usage_present, usage_extra_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _store_values._allocate_history_key(connection),
            session_key,
            sequence,
            run_id,
            message.id,
            message.timestamp,
            message.content,
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
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _finish_run(
    connection: sqlite3.Connection,
    session_key: int,
    sequence: int,
    message: ChatMessage | SessionRunCompletion,
) -> int:
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
    row = connection.execute(
        "SELECT run_key FROM runs WHERE session_key=? AND run_id=? AND status='running'",
        (session_key, message.run_id),
    ).fetchone()
    if row is None:
        raise ChatSessionError("Only an admitted running Run can be completed")
    run_key = int(row[0])
    terminal_key = _store_values._allocate_history_key(connection)
    connection.execute(
        "UPDATE runs SET work_id=?, status=?, started_at=?, completed_at=?, duration_ms=?, "
        "timing_extra_json=?, iteration_count=?, changed_files=?, lines_added=?, lines_removed=?, "
        "change_stats_extra_json=?, terminal_sequence=?, terminal_id=?, terminal_key=?, completion_reason=? WHERE run_key=?",
        (
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
            sequence,
            new_id("msg") if isinstance(message, SessionRunCompletion) else message.id,
            terminal_key,
            message.completion_reason if isinstance(message, SessionRunCompletion) else None,
            run_key,
        ),
    )
    for ordinal, path in enumerate(changes.get("paths", ())):
        connection.execute(
            "INSERT INTO run_change_paths (run_key, ordinal, path) VALUES (?, ?, ?)",
            (run_key, ordinal, path),
        )
    return terminal_key


def _complete_tool(
    connection: sqlite3.Connection,
    session_key: int,
    sequence: int,
    message: ChatMessage,
    *,
    run_id: str | None,
    assistant_message_id: str | None,
    index_fts: bool,
) -> int:
    clauses = ["m.session_key=?", "tc.tool_call_id=?"]
    values: list[Any] = [session_key, message.tool_call_id]
    if run_id is not None:
        clauses.append("m.run_id=?")
        values.append(run_id)
    if assistant_message_id is not None:
        clauses.append("m.message_id=?")
        values.append(assistant_message_id)
    rows = connection.execute(
        "SELECT tc.tool_call_key, tc.result_id FROM tool_calls tc JOIN messages m "
        "ON m.message_key=tc.message_key WHERE " + " AND ".join(clauses),
        values,
    ).fetchall()
    if len(rows) != 1:
        raise ChatSessionError("Tool result must identify exactly one stored invocation")
    if rows[0]["result_id"] is not None:
        raise ChatSessionError("Tool invocation already has a result")
    timing, timing_extra, _ = _timing_fields(message.timing)
    key = int(rows[0]["tool_call_key"])
    try:
        result = json.loads(message.content) if isinstance(message.content, str) else None
    except json.JSONDecodeError:
        result = None
    status = "completed"
    if isinstance(result, dict) and result.get("ok") is False:
        error = result.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        status = (
            "cancelled" if code in {"cancelled", "tool_cancelled", "user_cancelled"} else "failed"
        )
    result_key = _store_values._allocate_history_key(connection)
    connection.execute(
        "UPDATE tool_calls SET result_id=?, result_sequence=?, result_timestamp=?, "
        "result_content=?, status=?, started_at=?, completed_at=?, duration_ms=?, "
        "timing_extra_json=?, display_json=?, result_key=? WHERE tool_call_key=?",
        (
            message.id,
            sequence,
            message.timestamp,
            message.content,
            status,
            timing.get("started_at"),
            timing.get("completed_at"),
            timing.get("duration_ms"),
            timing_extra,
            _store_values._optional_json(message.tool_display, "tool_display"),
            result_key,
            key,
        ),
    )
    if index_fts:
        _store_fts._insert_fts_message(connection, result_key)
    return result_key
