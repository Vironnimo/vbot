"""Continuation state: fold Chat's records transactionally, read one typed state."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from core.chat.errors import ChatSessionError
from core.sessions import _store_values
from core.sessions._types import (
    JsonObject,
    SessionAddress,
    SessionContinuationOperation,
    SessionContinuationState,
    SessionContinuationStep,
)

_CONTINUATION_RECORD_VERSION = 1


def _continuation_step(record: JsonObject) -> int:
    """Return the Run-local Model step; Chat numbers a Run's first step 1."""
    value = record.get("step")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChatSessionError("continuation record step must be a positive integer")
    return value


def _optional_text(record: JsonObject, key: str) -> str | None:
    value = record.get(key)
    return value if isinstance(value, str) else None


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
        if record_type == "stream_attempt_discarded":
            connection.execute(
                "DELETE FROM continuation_steps WHERE session_key = ? AND run_id = ? AND step = ?",
                (session_key, run_id, step),
            )
            return
        if record_type == "stream_delta":
            # Append in SQL: a long step never round-trips its accumulated text.
            connection.execute(
                """
                INSERT INTO continuation_steps (
                    session_key, run_id, step, ordinal, reasoning, content
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (session_key, run_id, step) DO UPDATE SET
                    reasoning = reasoning || excluded.reasoning,
                    content = content || excluded.content
                """,
                (
                    session_key,
                    run_id,
                    step,
                    _next_continuation_ordinal(connection, "continuation_steps", session_key),
                    _optional_text(record, "reasoning_delta") or "",
                    _optional_text(record, "content_delta") or "",
                ),
            )
            return
        reasoning = _optional_text(record, "reasoning")
        content = _optional_text(record, "content")
        connection.execute(
            """
            INSERT INTO continuation_steps (
                session_key, run_id, step, ordinal, reasoning, content,
                assistant_message_id, interrupted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (session_key, run_id, step) DO UPDATE SET
                reasoning = COALESCE(?, reasoning),
                content = COALESCE(?, content),
                assistant_message_id = excluded.assistant_message_id,
                interrupted = excluded.interrupted
            """,
            (
                session_key,
                run_id,
                step,
                _next_continuation_ordinal(connection, "continuation_steps", session_key),
                reasoning or "",
                content or "",
                _optional_text(record, "message_id"),
                int(record.get("interrupted") is True),
                reasoning,
                content,
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


def _continuation_state(
    connection: sqlite3.Connection, session_key: int
) -> SessionContinuationState | None:
    continuation = connection.execute(
        "SELECT * FROM continuations WHERE session_key = ?", (session_key,)
    ).fetchone()
    if continuation is None:
        return None
    requests = tuple(
        json.loads(str(row[0]))
        for row in connection.execute(
            "SELECT request_json FROM continuation_requests WHERE session_key = ? ORDER BY ordinal",
            (session_key,),
        )
    )
    steps = tuple(
        SessionContinuationStep(
            run_id=str(row["run_id"]),
            step=int(row["step"]),
            reasoning=str(row["reasoning"]),
            content=str(row["content"]),
            assistant_message_id=row["assistant_message_id"],
            interrupted=bool(row["interrupted"]),
        )
        for row in connection.execute(
            "SELECT * FROM continuation_steps WHERE session_key = ? ORDER BY ordinal",
            (session_key,),
        )
    )
    operations = tuple(
        SessionContinuationOperation(
            tool_call_id=str(row["tool_call_id"]),
            name=str(row["name"]),
            run_id=str(row["run_id"]),
            completed=row["status"] == "completed",
            ok=None if row["status"] != "completed" else bool(row["ok"]),
        )
        for row in connection.execute(
            "SELECT * FROM continuation_operations WHERE session_key = ? ORDER BY ordinal",
            (session_key,),
        )
    )
    return SessionContinuationState(
        checkpoint_id=str(continuation["checkpoint_id"]),
        origin_run_id=str(continuation["origin_run_id"]),
        latest_run_id=str(continuation["latest_run_id"]),
        cause=None if continuation["cause"] is None else str(continuation["cause"]),
        active=bool(continuation["active"]),
        requests=requests,
        steps=steps,
        operations=operations,
    )


def _continuation_string(record: JsonObject, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ChatSessionError(f"continuation record {key} must be a non-empty string")
    return value


def continuation(
    connection: sqlite3.Connection, address: SessionAddress
) -> SessionContinuationState | None:
    """Return the Session's current Continuation state, or ``None`` without one."""
    state = _store_values._require_live(connection, address)
    return _continuation_state(connection, int(state["session_key"]))


def append_continuation(
    connection: sqlite3.Connection,
    address: SessionAddress,
    records: Sequence[JsonObject],
) -> None:
    if not records:
        return
    for record in records:
        _store_values._json_object(record, "continuation record")

    def _fn(connection: sqlite3.Connection) -> None:
        state = _store_values._require_live(connection, address)
        session_key = int(state["session_key"])
        for record in records:
            _apply_continuation_record(connection, session_key, record)
        _store_values._touch_state(connection, int(state["session_key"]))

    _fn(connection)


def clear_continuation(connection: sqlite3.Connection, address: SessionAddress) -> None:
    def _fn(connection: sqlite3.Connection) -> None:
        state = _store_values._require_live(connection, address)
        connection.execute(
            "DELETE FROM continuations WHERE session_key = ?",
            (state["session_key"],),
        )
        _store_values._touch_state(connection, int(state["session_key"]))

    _fn(connection)
