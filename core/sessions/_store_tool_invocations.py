"""Canonical dispatch admissions and their outcomes, independent of Tool Results."""

from __future__ import annotations

import json
import sqlite3

from core.chat.errors import ChatSessionError
from core.sessions import _store_values
from core.sessions._types import (
    SessionAddress,
    SessionToolInvocation,
    SessionToolInvocationBatch,
)
from core.sessions.errors import SessionNotFoundError


def _revision(connection: sqlite3.Connection, session_key: int) -> int:
    return int(
        connection.execute(
            "UPDATE sessions SET history_revision = history_revision + 1, "
            "state_revision = state_revision + 1 WHERE session_key = ? RETURNING history_revision",
            (session_key,),
        ).fetchone()[0]
    )


def start(
    connection: sqlite3.Connection,
    address: SessionAddress,
    run_id: str,
    *,
    call_id: str,
    name: str,
    started_at: str,
) -> int:
    state = _store_values._require_live(connection, address)
    session_key = int(state["session_key"])
    run = connection.execute(
        "SELECT run_key FROM runs WHERE session_key = ? AND run_id = ? "
        "AND status = 'running' AND inherited = 0",
        (session_key, run_id),
    ).fetchone()
    if run is None:
        raise ChatSessionError("A Tool invocation requires an admitted running Run")
    revision = _revision(connection, session_key)
    cursor = connection.execute(
        "INSERT INTO tool_invocations (session_key, run_key, call_id, name, started_at, "
        "status, source_revision) VALUES (?, ?, ?, ?, ?, 'running', ?)",
        (
            session_key,
            int(run[0]),
            call_id,
            name,
            _store_values._timestamp(started_at, "Tool start"),
            revision,
        ),
    )
    return int(cursor.lastrowid or 0)


def settle_run(connection: sqlite3.Connection, session_key: int, run_key: int, status: str) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM tool_invocations WHERE run_key = ? AND status = 'running' LIMIT 1",
            (run_key,),
        ).fetchone()
        is None
    ):
        return
    revision = _revision(connection, session_key)
    connection.execute(
        "UPDATE tool_invocations SET status = ?, source_revision = ? "
        "WHERE run_key = ? AND status = 'running'",
        ("cancelled" if status == "cancelled" else "interrupted", revision, run_key),
    )


def read_since(
    connection: sqlite3.Connection,
    address: SessionAddress,
    after_revision: int,
    expected_generation_id: str | None,
) -> SessionToolInvocationBatch:
    state = _store_values._require_live(connection, address)
    if expected_generation_id is not None and state["generation_id"] != expected_generation_id:
        raise SessionNotFoundError(f"session generation changed: {address.session_id}")
    rows = connection.execute(
        "SELECT i.invocation_key, r.run_id, i.name, i.started_at, i.status, i.result_ok, "
        "i.error_code, i.duration_ms, i.source_revision FROM tool_invocations AS i "
        "LEFT JOIN runs AS r ON r.run_key = i.run_key "
        "WHERE i.session_key = ? AND i.source_revision > ? "
        "ORDER BY i.source_revision, i.invocation_key",
        (int(state["session_key"]), after_revision),
    )
    return SessionToolInvocationBatch(
        revision=int(state["history_revision"]),
        invocations=tuple(
            SessionToolInvocation(
                invocation_key=int(row[0]),
                run_id=row[1],
                name=str(row[2]),
                started_at=str(row[3]),
                status=str(row[4]),
                result_ok=None if row[5] is None else bool(row[5]),
                error_code=row[6],
                duration_ms=row[7],
                source_revision=int(row[8]),
            )
            for row in rows
        ),
    )


def link_result(
    connection: sqlite3.Connection,
    *,
    session_key: int,
    run_key: int | None,
    result_entry_key: int,
    call_id: str,
    name: str,
    timestamp: str,
    started_at: str | None,
    status: str,
    result_ok: bool | None,
    error_code: str | None,
    duration_ms: int | None,
    invocation_key: int | None,
    content: str | None = None,
    own_call: bool = True,
) -> None:
    if invocation_key is not None:
        changed = connection.execute(
            "UPDATE tool_invocations SET result_entry_key = ?, status = ?, result_ok = ?, "
            "error_code = ?, duration_ms = ?, source_revision = ? WHERE invocation_key = ? "
            "AND session_key = ? AND run_key IS ? AND call_id = ? AND name = ? "
            "AND result_entry_key IS NULL",
            (
                result_entry_key,
                status,
                None if result_ok is None else int(result_ok),
                error_code,
                duration_ms,
                _revision(connection, session_key),
                invocation_key,
                session_key,
                run_key,
                call_id,
                name,
            ),
        ).rowcount
        if changed != 1:
            raise ChatSessionError("Tool Result does not match its started invocation")
        return
    if not own_call:
        return
    if result_ok is None or (result_ok is False and error_code is None):
        observed_ok, observed_code = _outcome_evidence(content)
        result_ok = observed_ok if result_ok is None else result_ok
        error_code = error_code or observed_code
        if result_ok is False and status == "completed":
            status = "failed"
    # Synthetic stop carriers say only that work may have stopped; they are
    # not independent evidence that a dispatch actually started.
    if error_code == "tool_stopped":
        return
    revision = _revision(connection, session_key)
    connection.execute(
        "INSERT INTO tool_invocations (session_key, run_key, call_id, name, started_at, "
        "status, result_ok, error_code, duration_ms, result_entry_key, source_revision) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_key,
            run_key,
            call_id,
            name,
            started_at or timestamp,
            status,
            None if result_ok is None else int(result_ok),
            error_code,
            duration_ms,
            result_entry_key,
            revision,
        ),
    )


def _outcome_evidence(content: str | None) -> tuple[bool | None, str | None]:
    """Decode historical/direct-import outcomes once into canonical scalar facts."""
    from core.tools import is_tool_result_envelope

    try:
        result = json.loads(content) if isinstance(content, str) else None
    except ValueError, TypeError:
        return None, None
    if not isinstance(result, dict) or not is_tool_result_envelope(result):
        return None, None
    if result["ok"]:
        return True, None
    return False, result["error"]["code"]


def backfill(connection: sqlite3.Connection) -> None:
    """Retain outcome evidence, never infer starts from Assistant declarations."""
    rows = connection.execute(
        "SELECT e.session_key, e.run_key, c.call_id, c.name, c.started_at, c.status, "
        "c.result_ok, c.error_code, c.duration_ms, c.result_entry_key, "
        "result.created_at, CASE WHEN c.result_ok IS NULL OR "
        "(c.result_ok = 0 AND c.error_code IS NULL) THEN text.content END "
        "FROM tool_calls AS c JOIN entries AS e ON e.entry_key = c.entry_key "
        "JOIN sessions AS s ON s.session_key = e.session_key "
        "JOIN entries AS result ON result.entry_key = c.result_entry_key "
        "LEFT JOIN entry_text AS text ON text.entry_key = result.entry_key "
        "LEFT JOIN runs AS r ON r.run_key = e.run_key "
        "WHERE e.seq >= COALESCE(s.fork_point_seq, 0) AND (r.run_key IS NULL OR r.inherited = 0) "
        "AND (c.error_code IS NULL OR c.error_code <> 'tool_stopped') "
        "AND NOT EXISTS (SELECT 1 FROM tool_invocations AS i "
        "WHERE i.result_entry_key = c.result_entry_key) ORDER BY e.session_key, e.seq, c.ordinal"
    )
    current_session = None
    revision = 0
    while batch := rows.fetchmany(256):
        values = []
        for row in batch:
            session_key = int(row[0])
            result_ok = None if row[6] is None else bool(row[6])
            error_code = row[7]
            status = str(row[5])
            if result_ok is None or (result_ok is False and error_code is None):
                observed_ok, observed_code = _outcome_evidence(row[11])
                result_ok = observed_ok if result_ok is None else result_ok
                error_code = error_code or observed_code
                if result_ok is False and status == "completed":
                    status = "failed"
            if error_code == "tool_stopped":
                continue
            if session_key != current_session:
                current_session = session_key
                revision = _revision(connection, session_key)
            values.append(
                (
                    session_key,
                    row[1],
                    str(row[2]),
                    str(row[3]),
                    row[4] or str(row[10]),
                    status,
                    None if result_ok is None else int(result_ok),
                    error_code,
                    row[8],
                    int(row[9]),
                    revision,
                )
            )
        connection.executemany(
            "INSERT INTO tool_invocations (session_key, run_key, call_id, name, started_at, "
            "status, result_ok, error_code, duration_ms, result_entry_key, source_revision) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
