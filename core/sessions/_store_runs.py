"""Transactional Run lifecycle and restart recovery, owned by Sessions."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from core.chat.errors import ChatSessionError
from core.runs import RunKind
from core.sessions import _store_codec, _store_mutations, _store_owned, _store_values
from core.sessions._io import _encode_chat_history_cursor
from core.sessions._types import (
    SESSION_RUN_KINDS_META_KEY,
    JsonObject,
    SessionAddress,
    SessionRunCompletion,
)

_RUN_KIND_VALUES = frozenset(kind.value for kind in RunKind)


def start_run(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    run_id: str,
    work_id: str | None,
    run_kind: str,
    contributes_to_activity: bool,
    started_at: str,
) -> None:
    _store_mutations.ensure_live(connection, address)
    _store_owned.record_run_start(connection, address, run_id=run_id)
    state = _store_values._require_live(connection, address)
    connection.execute(
        "UPDATE runs SET work_id=?,run_kind=?,contributes_to_activity=?,started_at=? "
        "WHERE session_key=? AND run_id=? AND status='running'",
        (work_id, run_kind, int(contributes_to_activity), started_at, state["session_key"], run_id),
    )
    record_run_kind(connection, address, run_kind)


def record_run_kind(connection: sqlite3.Connection, address: SessionAddress, run_kind: str) -> None:
    """Add *run_kind* to the Session's listed Run kinds; a known kind is a no-op."""
    if run_kind not in _RUN_KIND_VALUES:
        raise ChatSessionError(f"unknown run kind: {run_kind}")

    def update(metadata: JsonObject) -> None:
        values = metadata.get(SESSION_RUN_KINDS_META_KEY, [])
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value in _RUN_KIND_VALUES for value in values
        ):
            raise ChatSessionError("session run_kinds metadata is invalid")
        if run_kind not in values:
            metadata[SESSION_RUN_KINDS_META_KEY] = [*values, run_kind]

    _store_mutations.mutate_metadata(connection, address, update)


def start_tool(
    connection: sqlite3.Connection,
    address: SessionAddress,
    run_id: str,
    assistant_id: str,
    call_id: str,
    started_at: str,
) -> None:
    state = _store_values._require_live(connection, address)
    updated = connection.execute(
        "UPDATE tool_calls SET status='running',started_at=? WHERE tool_call_id=? "
        "AND status='pending' AND message_key IN (SELECT message_key FROM messages "
        "WHERE session_key=? AND run_id=? AND message_id=?)",
        (started_at, call_id, state["session_key"], run_id, assistant_id),
    )
    if updated.rowcount != 1:
        raise ChatSessionError("Tool start must identify one pending invocation")


def finish_run(
    connection: sqlite3.Connection, address: SessionAddress, completion: SessionRunCompletion
) -> JsonObject:
    state = _store_values._require_live(connection, address)
    session_key = int(state["session_key"])
    run = connection.execute(
        "SELECT * FROM runs WHERE session_key=? AND run_id=?", (session_key, completion.run_id)
    ).fetchone()
    if run is None or run["status"] != "running":
        raise ChatSessionError("Only an admitted running Run can be completed")
    sequence = int(state["message_count"])
    _store_codec._finish_run(connection, session_key, sequence, completion)
    if completion.status == "completed":
        connection.execute(
            "DELETE FROM continuations WHERE session_key=? AND latest_run_id=?",
            (session_key, completion.run_id),
        )
    timestamp = completion.timing["completed_at"]
    connection.execute(
        "UPDATE tool_calls SET status=?,completed_at=? WHERE result_id IS NULL "
        "AND message_key IN (SELECT message_key FROM messages WHERE session_key=? AND run_id=?)",
        (
            "cancelled" if completion.status == "cancelled" else "interrupted",
            timestamp,
            session_key,
            completion.run_id,
        ),
    )
    activity = _store_values._json_from_payload(state["activity_json"], "session activity")
    if completion.contributes_to_activity:
        activity["latest_completion"] = {
            "run_id": completion.run_id,
            "status": completion.status,
            "timestamp": timestamp,
        }
    connection.execute(
        "UPDATE sessions SET message_count=message_count+1, last_message_at=?, "
        "last_message_id=(SELECT terminal_id FROM runs WHERE run_key=?), "
        "active_sort=COALESCE(julianday(?),0), history_revision=history_revision+1, "
        "state_revision=state_revision+1, activity_json=? WHERE session_key=?",
        (
            timestamp,
            run["run_key"],
            timestamp,
            _store_values._json_object(activity, "session activity"),
            session_key,
        ),
    )
    return {
        "history_persisted": True,
        "history_generation_id": str(state["generation_id"]),
        "history_cursor": _encode_chat_history_cursor(str(state["generation_id"]), sequence + 1),
    }


def recover_interrupted_runs(connection: sqlite3.Connection) -> None:
    """Settle abandoned executions once, without retrying any Model or Tool work."""
    now = datetime.now(UTC).isoformat()
    rows = connection.execute(
        "SELECT r.*, s.project_id, s.agent_id, s.session_id FROM runs r "
        "JOIN sessions s ON s.session_key=r.session_key "
        "WHERE r.status='running' AND s.status='live'"
    ).fetchall()
    for row in rows:
        finish_run(
            connection,
            _store_values._address(row),
            SessionRunCompletion(
                run_id=str(row["run_id"]),
                status="interrupted",
                contributes_to_activity=bool(row["contributes_to_activity"]),
                work_id=row["work_id"],
                iteration_count=int(row["iteration_count"] or 0),
                completion_reason="process_restart",
                timing={
                    "started_at": row["started_at"],
                    "completed_at": now,
                    "duration_ms": max(
                        0,
                        round(
                            (
                                datetime.fromisoformat(now)
                                - datetime.fromisoformat(row["started_at"])
                            ).total_seconds()
                            * 1000
                        ),
                    ),
                },
            ),
        )
