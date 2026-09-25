"""Continuation state: fold Chat's records transactionally, read one typed state.

Streamed step text is appended as chunks, one row per flush, so a long step
never rewrites its accumulated text. The Assistant boundary folds a step's
chunks into one chunk holding its final text.
"""

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
CONTINUATION_CAUSES = frozenset(
    {"user", "provider", "network", "timeout", "process_restart", "internal"}
)


def _continuation_step(record: JsonObject) -> int:
    """Return the Run-local Model step; Chat numbers a Run's first step 1."""
    value = record.get("step")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChatSessionError("continuation record step must be a positive integer")
    return value


def _optional_text(record: JsonObject, key: str) -> str | None:
    value = record.get(key)
    return value if isinstance(value, str) else None


def _continuation_string(record: JsonObject, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ChatSessionError(f"continuation record {key} must be a non-empty string")
    return value


def _run_key(connection: sqlite3.Connection, session_key: int, record: JsonObject, key: str) -> int:
    run_id = _continuation_string(record, key)
    row = connection.execute(
        "SELECT run_key FROM runs WHERE session_key = ? AND run_id = ?", (session_key, run_id)
    ).fetchone()
    if row is None:
        raise ChatSessionError(f"continuation record names an unknown Run: {run_id}")
    return int(row[0])


def _upsert_operation(
    connection: sqlite3.Connection,
    session_key: int,
    *,
    tool_call_id: str,
    name: str,
    run_key: int,
    completed: bool,
    ok: bool | None,
    replace_unknown: bool,
) -> None:
    status = "completed" if completed else "unknown"
    existing = connection.execute(
        "SELECT 1 FROM continuation_operations WHERE session_key = ? AND tool_call_id = ?",
        (session_key, tool_call_id),
    ).fetchone()
    if existing is None:
        connection.execute(
            "INSERT INTO continuation_operations (session_key, tool_call_id, ordinal, name, "
            "run_key, status, ok) VALUES (?, ?, (SELECT COALESCE(MAX(ordinal) + 1, 0) "
            "FROM continuation_operations WHERE session_key = ?), ?, ?, ?, ?)",
            (
                session_key,
                tool_call_id,
                session_key,
                name,
                run_key,
                status,
                None if ok is None else int(ok),
            ),
        )
    elif replace_unknown or completed:
        connection.execute(
            "UPDATE continuation_operations SET name = ?, run_key = ?, status = ?, ok = ? "
            "WHERE session_key = ? AND tool_call_id = ?",
            (name, run_key, status, None if ok is None else int(ok), session_key, tool_call_id),
        )


def _ensure_step(connection: sqlite3.Connection, session_key: int, run_key: int, step: int) -> None:
    connection.execute(
        "INSERT INTO continuation_steps (session_key, run_key, step, ordinal) "
        "VALUES (?, ?, ?, (SELECT COALESCE(MAX(ordinal) + 1, 0) FROM continuation_steps "
        "WHERE session_key = ?)) ON CONFLICT (session_key, run_key, step) DO NOTHING",
        (session_key, run_key, step, session_key),
    )


def _fold_step_text(
    connection: sqlite3.Connection,
    session_key: int,
    run_key: int,
    step: int,
    *,
    reasoning: str | None,
    content: str | None,
) -> None:
    """Replace a step's chunks with one chunk; a missing final text keeps the streamed one."""
    if reasoning is None or content is None:
        chunks = connection.execute(
            "SELECT reasoning_delta, content_delta FROM continuation_step_chunks "
            "WHERE session_key = ? AND run_key = ? AND step = ? ORDER BY chunk_key",
            (session_key, run_key, step),
        ).fetchall()
        if reasoning is None:
            reasoning = "".join(str(row[0]) for row in chunks)
        if content is None:
            content = "".join(str(row[1]) for row in chunks)
    connection.execute(
        "DELETE FROM continuation_step_chunks WHERE session_key = ? AND run_key = ? AND step = ?",
        (session_key, run_key, step),
    )
    if reasoning or content:
        connection.execute(
            "INSERT INTO continuation_step_chunks (session_key, run_key, step, reasoning_delta, "
            "content_delta) VALUES (?, ?, ?, ?, ?)",
            (session_key, run_key, step, reasoning, content),
        )


def _start_run(connection: sqlite3.Connection, session_key: int, record: JsonObject) -> None:
    checkpoint_id = _continuation_string(record, "checkpoint_id")
    run_key = _run_key(connection, session_key, record, "run_id")
    origin_run_key = _run_key(connection, session_key, record, "origin_run_id")
    existing = connection.execute(
        "SELECT checkpoint_id FROM continuations WHERE session_key = ?", (session_key,)
    ).fetchone()
    if existing is None or str(existing[0]) != checkpoint_id:
        connection.execute("DELETE FROM continuations WHERE session_key = ?", (session_key,))
        connection.execute(
            "INSERT INTO continuations (session_key, checkpoint_id, origin_run_key, "
            "latest_run_key, cause, active) VALUES (?, ?, ?, ?, NULL, 1)",
            (session_key, checkpoint_id, origin_run_key, run_key),
        )
    else:
        connection.execute(
            "UPDATE continuations SET latest_run_key = ?, cause = NULL, active = 1 "
            "WHERE session_key = ?",
            (run_key, session_key),
        )
    if record.get("request") is not None:
        try:
            request_json = json.dumps(record["request"], ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ChatSessionError("continuation request is not JSON-serializable") from exc
        connection.execute(
            "INSERT INTO continuation_requests (session_key, ordinal, request_json) "
            "VALUES (?, (SELECT COALESCE(MAX(ordinal) + 1, 0) FROM continuation_requests "
            "WHERE session_key = ?), ?)",
            (session_key, session_key, request_json),
        )


def _apply_record(connection: sqlite3.Connection, session_key: int, record: JsonObject) -> None:
    if record.get("version") != _CONTINUATION_RECORD_VERSION:
        raise ChatSessionError("unsupported continuation record version")
    record_type = record.get("type")
    if record_type == "run_started":
        _start_run(connection, session_key, record)
        return
    continuation = connection.execute(
        "SELECT checkpoint_id FROM continuations WHERE session_key = ?", (session_key,)
    ).fetchone()
    if continuation is None:
        return
    if record_type in {"stream_delta", "stream_attempt_discarded", "assistant_boundary"}:
        run_key = _run_key(connection, session_key, record, "run_id")
        step = _continuation_step(record)
        if record_type == "stream_attempt_discarded":
            connection.execute(
                "DELETE FROM continuation_steps WHERE session_key = ? AND run_key = ? AND step = ?",
                (session_key, run_key, step),
            )
            return
        _ensure_step(connection, session_key, run_key, step)
        if record_type == "stream_delta":
            reasoning = _optional_text(record, "reasoning_delta") or ""
            content = _optional_text(record, "content_delta") or ""
            if reasoning or content:
                connection.execute(
                    "INSERT INTO continuation_step_chunks (session_key, run_key, step, "
                    "reasoning_delta, content_delta) VALUES (?, ?, ?, ?, ?)",
                    (session_key, run_key, step, reasoning, content),
                )
            return
        connection.execute(
            "UPDATE continuation_steps SET assistant_message_id = ?, interrupted = ? "
            "WHERE session_key = ? AND run_key = ? AND step = ?",
            (
                _optional_text(record, "message_id"),
                int(record.get("interrupted") is True),
                session_key,
                run_key,
                step,
            ),
        )
        _fold_step_text(
            connection,
            session_key,
            run_key,
            step,
            reasoning=_optional_text(record, "reasoning"),
            content=_optional_text(record, "content"),
        )
        for tool_call in record.get("tool_calls") or ():
            if not isinstance(tool_call, dict):
                continue
            tool_call_id, name = tool_call.get("id"), tool_call.get("name")
            if isinstance(tool_call_id, str) and tool_call_id and isinstance(name, str) and name:
                _upsert_operation(
                    connection,
                    session_key,
                    tool_call_id=tool_call_id,
                    name=name,
                    run_key=run_key,
                    completed=False,
                    ok=None,
                    replace_unknown=False,
                )
        return
    if record_type in {"tool_started", "tool_result"}:
        completed = record_type == "tool_result"
        _upsert_operation(
            connection,
            session_key,
            tool_call_id=_continuation_string(record, "tool_call_id"),
            name=_continuation_string(record, "name"),
            run_key=_run_key(connection, session_key, record, "run_id"),
            completed=completed,
            ok=record.get("ok") is True if completed else None,
            replace_unknown=True,
        )
        return
    if record_type == "run_interrupted":
        cause = _continuation_string(record, "cause")
        if cause not in CONTINUATION_CAUSES:
            raise ChatSessionError(f"unsupported continuation cause: {cause}")
        connection.execute(
            "UPDATE continuations SET latest_run_key = ?, cause = ?, active = 0 "
            "WHERE session_key = ?",
            (_run_key(connection, session_key, record, "run_id"), cause, session_key),
        )
        return
    if record_type == "resolved" and record.get("checkpoint_id") == continuation[0]:
        connection.execute("DELETE FROM continuations WHERE session_key = ?", (session_key,))


def _continuation_state(
    connection: sqlite3.Connection, session_key: int
) -> SessionContinuationState | None:
    continuation = connection.execute(
        "SELECT c.checkpoint_id, origin.run_id AS origin_run_id, latest.run_id AS latest_run_id, "
        "c.cause, c.active FROM continuations AS c "
        "JOIN runs AS origin ON origin.run_key = c.origin_run_key "
        "JOIN runs AS latest ON latest.run_key = c.latest_run_key WHERE c.session_key = ?",
        (session_key,),
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
    texts: dict[tuple[int, int], tuple[list[str], list[str]]] = {}
    for row in connection.execute(
        "SELECT run_key, step, reasoning_delta, content_delta FROM continuation_step_chunks "
        "WHERE session_key = ? ORDER BY chunk_key",
        (session_key,),
    ):
        reasoning, content = texts.setdefault((int(row[0]), int(row[1])), ([], []))
        reasoning.append(str(row[2]))
        content.append(str(row[3]))
    steps = []
    for row in connection.execute(
        "SELECT s.run_key, s.step, r.run_id, s.assistant_message_id, s.interrupted "
        "FROM continuation_steps AS s JOIN runs AS r ON r.run_key = s.run_key "
        "WHERE s.session_key = ? ORDER BY s.ordinal",
        (session_key,),
    ):
        reasoning, content = texts.get((int(row["run_key"]), int(row["step"])), ([], []))
        steps.append(
            SessionContinuationStep(
                run_id=str(row["run_id"]),
                step=int(row["step"]),
                reasoning="".join(reasoning),
                content="".join(content),
                assistant_message_id=row["assistant_message_id"],
                interrupted=bool(row["interrupted"]),
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
            "SELECT o.tool_call_id, o.name, r.run_id, o.status, o.ok "
            "FROM continuation_operations AS o JOIN runs AS r ON r.run_key = o.run_key "
            "WHERE o.session_key = ? ORDER BY o.ordinal",
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
        steps=tuple(steps),
        operations=operations,
    )


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
    """Fold Chat's Continuation records into the Session's current state."""
    if not records:
        return
    for record in records:
        _store_values._json_object(record, "continuation record")
    state = _store_values._require_live(connection, address)
    session_key = int(state["session_key"])
    for record in records:
        _apply_record(connection, session_key, record)
    _store_values._touch_state(connection, session_key)


def clear_continuation(connection: sqlite3.Connection, address: SessionAddress) -> None:
    state = _store_values._require_live(connection, address)
    connection.execute("DELETE FROM continuations WHERE session_key = ?", (state["session_key"],))
    _store_values._touch_state(connection, int(state["session_key"]))
