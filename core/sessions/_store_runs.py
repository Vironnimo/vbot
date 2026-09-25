"""Run admission, completion, restart recovery and completion activity."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from core.chat.errors import ChatSessionError
from core.sessions import _store_codec, _store_fts, _store_mutations, _store_values
from core.sessions._io import _encode_chat_history_cursor
from core.sessions._metadata import _RUN_KIND_VALUES, _completion_activity_from_state
from core.sessions._types import (
    JsonObject,
    SessionAddress,
    SessionRunAdmission,
    SessionRunCompletion,
)
from core.utils.timestamps import utc_now_timestamp

_OWNER_MISMATCH_ERROR = (
    "This Session no longer matches this Run. Ask the user to resume it through its Extension."
)
_CHANGE_STATS_VALIDATORS = {
    "files": _store_codec._is_non_negative_int,
    "added": _store_codec._is_non_negative_int,
    "removed": _store_codec._is_non_negative_int,
    "paths": lambda value: isinstance(value, list) and all(isinstance(path, str) for path in value),
}


def _owner_fields(admission: SessionRunAdmission) -> tuple[str, str, str, str, str] | None:
    owner = admission.owner
    if owner is None:
        if admission.input_id is not None:
            raise ChatSessionError("an owner input requires an execution owner")
        return None
    fields = (
        owner.extension,
        owner.group_id,
        owner.participant_id,
        owner.generation_id,
        owner.epoch,
    )
    if not all(isinstance(value, str) and value for value in fields) or (
        admission.input_id is not None
        and (not isinstance(admission.input_id, str) or not admission.input_id)
    ):
        raise ChatSessionError(_OWNER_MISMATCH_ERROR)
    return fields


def _validate_admission(admission: SessionRunAdmission) -> None:
    if not isinstance(admission.run_id, str) or not admission.run_id:
        raise ChatSessionError("invalid Run start")
    if admission.run_kind not in _RUN_KIND_VALUES:
        raise ChatSessionError(f"unknown run kind: {admission.run_kind}")
    if admission.work_id is not None and (
        not isinstance(admission.work_id, str) or not admission.work_id
    ):
        raise ChatSessionError("invalid Run work id")


def admit_run(
    connection: sqlite3.Connection, address: SessionAddress, admission: SessionRunAdmission
) -> None:
    """Admit one Run: its row, its Run kind and its execution owner together.

    A missing live Session is created. Admitting a Run that is already running
    here is a no-op when it carries the same owner; a settled or inherited Run
    is never admitted again. An owner's participant binding must still be live
    in the generation the owner names.
    """
    _validate_admission(admission)
    owner = _owner_fields(admission)
    started_at = _store_values._timestamp(admission.started_at, "Run start")
    _store_mutations.ensure_live(connection, address)
    state = _store_values._require_live(connection, address)
    session_key = int(state["session_key"])
    existing = connection.execute(
        "SELECT r.run_key, r.status, r.inherited, o.owner_name, o.group_id, o.participant_id, "
        "o.participant_generation_id, o.epoch, o.input_id FROM runs AS r "
        "LEFT JOIN run_execution_owners AS o ON o.run_key = r.run_key "
        "WHERE r.session_key = ? AND r.run_id = ?",
        (session_key, admission.run_id),
    ).fetchone()
    if existing is not None:
        if existing["status"] != "running" or existing["inherited"]:
            raise ChatSessionError("A settled or inherited Run cannot be admitted again")
        recorded = (
            None
            if existing["owner_name"] is None
            else (
                str(existing["owner_name"]),
                str(existing["group_id"]),
                str(existing["participant_id"]),
                str(existing["participant_generation_id"]),
                str(existing["epoch"]),
            )
        )
        if owner is not None and (recorded != owner or existing["input_id"] != admission.input_id):
            raise ChatSessionError(_OWNER_MISMATCH_ERROR)
        return
    if owner is not None:
        # The owner's participant binding must still name the generation the
        # owner holds. The Run may execute in that Session or in a descendant
        # Session the owner continues.
        binding = connection.execute(
            "SELECT 1 FROM temporary_session_bindings AS b "
            "JOIN sessions AS s ON s.session_key = b.session_key "
            "WHERE b.owner_name = ? AND b.group_id = ? AND b.participant_id = ? "
            "AND s.generation_id = ? AND s.state = 'live'",
            owner[:4],
        ).fetchone()
        if binding is None:
            raise ChatSessionError(_OWNER_MISMATCH_ERROR)
    cursor = connection.execute(
        "INSERT INTO runs (session_key, run_id, work_id, run_kind, contributes_to_activity, "
        "status, started_at, start_seq) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)",
        (
            session_key,
            admission.run_id,
            admission.work_id,
            admission.run_kind,
            int(admission.contributes_to_activity),
            started_at,
            int(state["next_seq"]),
        ),
    )
    run_key = int(cursor.lastrowid or 0)
    if owner is not None:
        try:
            connection.execute(
                "INSERT INTO run_execution_owners (run_key, session_key, run_id, input_id, "
                "owner_name, group_id, participant_id, participant_generation_id, epoch) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_key, session_key, admission.run_id, admission.input_id, *owner),
            )
        except sqlite3.IntegrityError as exc:
            raise ChatSessionError(_OWNER_MISMATCH_ERROR) from exc
    _store_mutations.record_run_kind_by_key(connection, session_key, admission.run_kind)
    _store_values._touch_state(connection, session_key)


def finish_run(
    connection: sqlite3.Connection, address: SessionAddress, completion: SessionRunCompletion
) -> JsonObject:
    """Complete one running Run with its summary entry, in one transaction.

    The summary entry closes the Run's history. Unresolved Tool calls of the
    Run end cancelled (for a cancelled Run) or interrupted, a completed Run
    resolves the Continuation it continued, and an activity-contributing Run
    becomes the Session's latest completion. Returns the committed cursor.
    """
    from core.chat.messages import ChatMessage

    state = _store_values._require_live(connection, address)
    session_key = int(state["session_key"])
    run = connection.execute(
        "SELECT run_key, status, inherited FROM runs WHERE session_key = ? AND run_id = ?",
        (session_key, completion.run_id),
    ).fetchone()
    if run is None or run["status"] != "running" or run["inherited"]:
        raise ChatSessionError("Only an admitted running Run can be completed")
    run_key = int(run["run_key"])
    summary = ChatMessage.run_summary(
        run_id=completion.run_id,
        work_id=completion.work_id,
        status=completion.status,
        timing=completion.timing,
        iteration_count=completion.iteration_count,
        change_stats=completion.change_stats,
        timestamp=datetime.fromisoformat(
            _store_values._timestamp(completion.timing.get("completed_at", ""), "Run completion")
        ),
    )
    summary.validate()
    timing, timing_extra, _present = _store_codec._timing_fields(summary.timing)
    changes, changes_extra, changes_present = _store_codec._split_structured_fields(
        summary.change_stats, _CHANGE_STATS_VALIDATORS
    )
    completed_at = _store_values._timestamp(str(timing["completed_at"]), "Run completion")
    seq = int(state["next_seq"])
    entry_key = _store_codec.insert_entry(connection, session_key, seq, summary, run_key=run_key)
    connection.execute(
        "UPDATE runs SET work_id = COALESCE(?, work_id), status = ?, completed_at = ?, "
        "duration_ms = ?, timing_started_at = ?, timing_extra_json = ?, iteration_count = ?, "
        "changed_files = ?, lines_added = ?, lines_removed = ?, change_stats_extra_json = ?, "
        "completion_reason = ?, end_entry_key = ? WHERE run_key = ?",
        (
            completion.work_id,
            completion.status,
            completed_at,
            timing.get("duration_ms"),
            _store_values._optional_timestamp(timing.get("started_at"), "Run timing"),
            timing_extra,
            completion.iteration_count,
            changes.get("files") if changes_present else None,
            changes.get("added") if changes_present else None,
            changes.get("removed") if changes_present else None,
            changes_extra,
            completion.completion_reason,
            entry_key,
            run_key,
        ),
    )
    connection.executemany(
        "INSERT INTO run_change_paths (run_key, ordinal, path) VALUES (?, ?, ?)",
        [(run_key, ordinal, path) for ordinal, path in enumerate(changes.get("paths", ()))],
    )
    connection.execute(
        "UPDATE tool_calls SET status = ?, completed_at = ? WHERE result_entry_key IS NULL "
        "AND status IN ('pending', 'running') AND entry_key IN "
        "(SELECT entry_key FROM entries WHERE run_key = ?)",
        (
            "cancelled" if completion.status == "cancelled" else "interrupted",
            completed_at,
            run_key,
        ),
    )
    if completion.status == "completed":
        connection.execute(
            "DELETE FROM continuations WHERE session_key = ? AND latest_run_key = ?",
            (session_key, run_key),
        )
    latest = (
        (run_key, completion.status, completed_at)
        if completion.contributes_to_activity
        else (
            state["latest_completion_run_key"],
            state["latest_completion_status"],
            state["latest_completion_at"],
        )
    )
    connection.execute(
        "UPDATE sessions SET next_seq = ?, last_activity_at = ?, "
        "last_entry_id = ?, latest_completion_run_key = ?, latest_completion_status = ?, "
        "latest_completion_at = ?, history_revision = history_revision + 1, "
        "state_revision = state_revision + 1 WHERE session_key = ?",
        (seq + 1, completed_at, summary.id, *latest, session_key),
    )
    _store_fts.fts_index_keys(connection, [entry_key])
    generation_id = str(state["generation_id"])
    return {
        "history_persisted": True,
        "history_generation_id": generation_id,
        "history_cursor": _encode_chat_history_cursor(generation_id, seq + 1),
    }


def recover_interrupted_runs(connection: sqlite3.Connection) -> None:
    """Settle Runs a stopped process left running, without retrying any work."""
    now = utc_now_timestamp()
    rows = connection.execute(
        "SELECT r.run_id, r.work_id, r.contributes_to_activity, r.iteration_count, "
        "r.started_at, s.project_id, s.agent_id, s.session_id FROM runs AS r "
        "JOIN sessions AS s ON s.session_key = r.session_key "
        "WHERE r.status = 'running' AND r.inherited = 0 AND s.state = 'live' "
        "ORDER BY r.run_key"
    ).fetchall()
    for row in rows:
        started = datetime.fromisoformat(str(row["started_at"]))
        completed = datetime.fromisoformat(now)
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
                    "started_at": str(row["started_at"]),
                    "completed_at": now,
                    "duration_ms": max(0, round((completed - started).total_seconds() * 1000)),
                },
            ),
        )


def _completion_activity(connection: sqlite3.Connection, session_key: int) -> sqlite3.Row:
    row: sqlite3.Row = connection.execute(
        f"SELECT {_store_values._COMPLETION_ACTIVITY_COLUMNS} FROM sessions AS s "
        "WHERE s.session_key = ?",
        (session_key,),
    ).fetchone()
    return row


def mark_terminal_run_read(
    connection: sqlite3.Connection, address: SessionAddress, run_id: str
) -> tuple[JsonObject, bool]:
    """Mark the latest completion read when it is *run_id*; return the activity."""
    session_key = int(_store_values._require_live(connection, address)["session_key"])
    activity = _completion_activity(connection, session_key)
    marked = (
        activity["latest_completion_run_id"] == run_id and not activity["latest_completion_read"]
    )
    if marked:
        connection.execute(
            "UPDATE sessions SET read_completion_run_key = latest_completion_run_key, "
            "state_revision = state_revision + 1 WHERE session_key = ?",
            (session_key,),
        )
        activity = _completion_activity(connection, session_key)
    return _completion_activity_from_state(activity), marked
