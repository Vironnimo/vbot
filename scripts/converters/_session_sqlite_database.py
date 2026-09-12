"""Offline Session import and canonical database and Continuation verification."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.sessions._store_codec import messages_from_connection
from core.sessions.schema import APPLICATION_ID, SCHEMA_VERSION
from core.sessions.store import SessionStore
from scripts.converters._session_sqlite_values import _address_payload, _fsync_file, _sha256
from scripts.converters.jsonl_sessions import LegacySession

_CONTINUATION_RECORD_VERSION = 1


def _created_at(session: LegacySession) -> str:
    if session.messages:
        return session.messages[0].timestamp
    transcript = session.captured_artifacts[0]
    timestamp = transcript.mtime_ns or 0
    return datetime.fromtimestamp(timestamp / 1_000_000_000, UTC).isoformat().replace("+00:00", "Z")


def _import_to_db(path: Path, sessions: tuple[LegacySession, ...]) -> None:
    store = SessionStore(path, _offline=True)
    try:
        store.prepare_offline_bulk_import()
        for session in sessions:
            store.import_generation(
                session.address,
                generation_id=session.generation_id,
                messages=session.messages,
                metadata=session.metadata,
                activity=session.activity,
                continuation=session.continuation,
                archived=session.archived,
                created_at=_created_at(session),
            )
        store.finish_offline_bulk_import()
        store.checkpoint()
    finally:
        store.close()
    _force_delete_journal(path)


def _force_delete_journal(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        if mode != "delete":
            raise RuntimeError("staged database did not enter DELETE journal mode")
        connection.commit()
    _fsync_file(path)


def _verify_db(path: Path, sessions: tuple[LegacySession, ...]) -> dict[str, Any]:
    result = _verify_db_expected(
        path, {session.generation_id: len(session.messages) for session in sessions}
    )
    expected = {session.generation_id: session for session in sessions}
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM sessions ORDER BY session_key").fetchall()
        for row in rows:
            generation_id = str(row["generation_id"])
            session = expected.get(generation_id)
            if session is None:
                raise RuntimeError(f"unexpected converted Session generation: {generation_id}")
            actual_address = [row["project_id"] or None, row["agent_id"], row["session_id"]]
            if actual_address != _address_payload(session.address):
                raise RuntimeError(f"converted Session address mismatch: {generation_id}")
            expected_status = "archived" if session.archived else "live"
            if row["status"] != expected_status:
                raise RuntimeError(f"converted Session lifecycle mismatch: {generation_id}")
            actual_messages = [
                message.to_dict()
                for message in messages_from_connection(connection, int(row["session_key"]))
            ]
            expected_messages = [message.to_dict() for message in session.messages]
            if actual_messages != expected_messages:
                raise RuntimeError(f"converted Session Messages mismatch: {generation_id}")
            if SessionStore.metadata_from_state(row) != session.metadata:
                raise RuntimeError(f"converted Session metadata mismatch: {generation_id}")
            try:
                activity = json.loads(str(row["activity_json"]))
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"converted Session activity is invalid: {generation_id}"
                ) from exc
            if activity != session.activity:
                raise RuntimeError(f"converted Session activity mismatch: {generation_id}")
            actual_continuation = _continuation_state_from_connection(
                connection, int(row["session_key"])
            )
            if actual_continuation != _fold_continuation(session.continuation):
                raise RuntimeError(f"converted Session Continuation mismatch: {generation_id}")
    return result


def _continuation_string(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"continuation record {key} must be a non-empty string")
    return value


def _continuation_step(record: dict[str, Any]) -> int:
    value = record.get("step")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("continuation record step must be a non-negative integer")
    return value


def _upsert_folded_operation(
    operations: dict[str, dict[str, Any]],
    *,
    tool_call_id: str,
    name: str,
    run_id: str,
    status: str,
    ok: bool | None,
    replace_unknown: bool,
) -> None:
    existing = operations.get(tool_call_id)
    if existing is None:
        operations[tool_call_id] = {
            "tool_call_id": tool_call_id,
            "name": name,
            "run_id": run_id,
            "status": status,
            "ok": ok,
        }
    elif replace_unknown or status == "completed":
        existing.update({"name": name, "run_id": run_id, "status": status, "ok": ok})


def _fold_continuation(records: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    state: dict[str, Any] | None = None
    requests: list[Any] = []
    steps: dict[tuple[str, int], dict[str, Any]] = {}
    operations: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("version") != _CONTINUATION_RECORD_VERSION:
            raise RuntimeError("unsupported continuation record version")
        record_type = record.get("type")
        if record_type == "run_started":
            checkpoint_id = _continuation_string(record, "checkpoint_id")
            run_id = _continuation_string(record, "run_id")
            origin_run_id = _continuation_string(record, "origin_run_id")
            if state is None or state["checkpoint_id"] != checkpoint_id:
                state = {
                    "checkpoint_id": checkpoint_id,
                    "origin_run_id": origin_run_id,
                    "latest_run_id": run_id,
                    "cause": None,
                    "active": True,
                }
                requests = []
                steps = {}
                operations = {}
            else:
                state.update({"latest_run_id": run_id, "cause": None, "active": True})
            if record.get("request") is not None:
                requests.append(record["request"])
            continue
        if state is None:
            continue
        if record_type in {"stream_delta", "stream_attempt_discarded", "assistant_boundary"}:
            run_id = _continuation_string(record, "run_id")
            step_number = _continuation_step(record)
            key = (run_id, step_number)
            if record_type == "stream_attempt_discarded":
                steps.pop(key, None)
                continue
            step = steps.get(key)
            if step is None:
                step = {
                    "run_id": run_id,
                    "step": step_number,
                    "reasoning": "",
                    "content": "",
                    "assistant_message_id": None,
                    "interrupted": False,
                }
                steps[key] = step
            if record_type == "stream_delta":
                if isinstance(record.get("reasoning_delta"), str):
                    step["reasoning"] += record["reasoning_delta"]
                if isinstance(record.get("content_delta"), str):
                    step["content"] += record["content_delta"]
            else:
                if isinstance(record.get("reasoning"), str):
                    step["reasoning"] = record["reasoning"]
                if isinstance(record.get("content"), str):
                    step["content"] = record["content"]
                message_id = record.get("message_id")
                step["assistant_message_id"] = message_id if isinstance(message_id, str) else None
                step["interrupted"] = record.get("interrupted") is True
                tool_calls = record.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tool_call in tool_calls:
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
                            _upsert_folded_operation(
                                operations,
                                tool_call_id=tool_call_id,
                                name=name,
                                run_id=run_id,
                                status="unknown",
                                ok=None,
                                replace_unknown=False,
                            )
            continue
        if record_type == "tool_started":
            _upsert_folded_operation(
                operations,
                tool_call_id=_continuation_string(record, "tool_call_id"),
                name=_continuation_string(record, "name"),
                run_id=_continuation_string(record, "run_id"),
                status="unknown",
                ok=None,
                replace_unknown=True,
            )
            continue
        if record_type == "tool_result":
            _upsert_folded_operation(
                operations,
                tool_call_id=_continuation_string(record, "tool_call_id"),
                name=_continuation_string(record, "name"),
                run_id=_continuation_string(record, "run_id"),
                status="completed",
                ok=record.get("ok") is True,
                replace_unknown=True,
            )
            continue
        if record_type == "run_interrupted":
            state.update(
                {
                    "latest_run_id": _continuation_string(record, "run_id"),
                    "cause": _continuation_string(record, "cause"),
                    "active": False,
                }
            )
            continue
        if record_type == "resolved" and record.get("checkpoint_id") == state["checkpoint_id"]:
            state = None
            requests = []
            steps = {}
            operations = {}
    if state is None:
        return None
    return {
        **state,
        "requests": requests,
        "steps": list(steps.values()),
        "operations": list(operations.values()),
    }


def _continuation_state_from_connection(
    connection: sqlite3.Connection, session_key: int
) -> dict[str, Any] | None:
    state = connection.execute(
        "SELECT * FROM continuations WHERE session_key = ?", (session_key,)
    ).fetchone()
    if state is None:
        return None
    requests = [
        json.loads(str(row["request_json"]))
        for row in connection.execute(
            "SELECT request_json FROM continuation_requests WHERE session_key = ? ORDER BY ordinal",
            (session_key,),
        )
    ]
    steps = [
        {
            "run_id": str(row["run_id"]),
            "step": int(row["step"]),
            "reasoning": str(row["reasoning"]),
            "content": str(row["content"]),
            "assistant_message_id": row["assistant_message_id"],
            "interrupted": bool(row["interrupted"]),
        }
        for row in connection.execute(
            "SELECT * FROM continuation_steps WHERE session_key = ? ORDER BY ordinal",
            (session_key,),
        )
    ]
    operations = [
        {
            "tool_call_id": str(row["tool_call_id"]),
            "name": str(row["name"]),
            "run_id": str(row["run_id"]),
            "status": str(row["status"]),
            "ok": None if row["ok"] is None else bool(row["ok"]),
        }
        for row in connection.execute(
            "SELECT * FROM continuation_operations WHERE session_key = ? ORDER BY ordinal",
            (session_key,),
        )
    ]
    return {
        "checkpoint_id": str(state["checkpoint_id"]),
        "origin_run_id": str(state["origin_run_id"]),
        "latest_run_id": str(state["latest_run_id"]),
        "cause": state["cause"],
        "active": bool(state["active"]),
        "requests": requests,
        "steps": steps,
        "operations": operations,
    }


def _verify_db_expected(path: Path, expected: dict[str, int]) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"staged database is missing: {path}")
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
            raise RuntimeError("staged database schema version mismatch")
        if int(connection.execute("PRAGMA application_id").fetchone()[0]) != APPLICATION_ID:
            raise RuntimeError("staged database application identity mismatch")
        if str(connection.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
            raise RuntimeError("staged database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("staged database foreign-key check failed")
        rows = connection.execute(
            "SELECT s.generation_id, s.message_count, COUNT(m.message_key) AS actual_count "
            "FROM sessions AS s LEFT JOIN messages AS m ON m.session_key = s.session_key "
            "GROUP BY s.session_key ORDER BY s.session_key"
        ).fetchall()
        if len(rows) != len(expected):
            raise RuntimeError("converted Session count does not match immutable capture")
        actual: dict[str, int] = {}
        for row in rows:
            count = int(row["actual_count"])
            if int(row["message_count"]) != count:
                raise RuntimeError(
                    "converted Session message counter does not match canonical rows"
                )
            actual[str(row["generation_id"])] = count
        if actual != expected:
            raise RuntimeError("converted Session message coverage does not match capture")
        return {
            "session_count": len(rows),
            "message_count": int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]),
            "sha256": _sha256(path),
        }
