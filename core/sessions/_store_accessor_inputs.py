"""Ordinary Accessor acceptance receipts and recoverable queued input state.

The Session store owns every transaction. Chat owns the opaque execution payload;
this module only guards exact-generation disposition and never executes work.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import TYPE_CHECKING

from core.chat.errors import ChatMessageValidationError, ChatSessionError
from core.sessions import _store_queries, _store_values
from core.sessions._types import AccessorInputRecord, JsonObject, SessionAddress
from core.sessions.errors import (
    AccessorInputConflictError,
    SessionNotFoundError,
    SessionStoreCorruptError,
)
from core.utils.ids import is_safe_id
from core.utils.surrogates import without_lone_surrogates
from core.utils.timestamps import utc_now_timestamp

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage

_COLUMNS = (
    "i.input_key, i.input_id, i.request_hash, i.queue_id, i.state AS input_state, "
    "i.created_at, i.run_key, p.payload_json, s.session_key, s.project_id, "
    "s.agent_id, s.session_id, s.generation_id, s.state AS session_state, "
    "r.run_id, r.status AS run_status, c.notes_json AS fallback_notes_json, "
    "(SELECT json_group_array(a.attachment_id) FROM session_accessor_input_attachments AS a "
    "WHERE a.input_key = i.input_key) AS attachment_ids_json "
)
_FROM = (
    "FROM session_accessor_inputs AS i "
    "JOIN session_accessor_input_payloads AS p ON p.input_key = i.input_key "
    "JOIN sessions AS s ON s.session_key = i.session_key "
    "LEFT JOIN runs AS r ON r.run_key = i.run_key "
    "LEFT JOIN session_accessor_input_carriers AS c ON c.input_key = i.input_key "
)


def _identifier(value: str, subject: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise ChatSessionError(f"Invalid {subject}")


def _payload_json(payload: JsonObject) -> str:
    if not isinstance(payload, dict):
        raise ChatSessionError("Accessor input payload must be an object")
    try:
        return json.dumps(
            without_lone_surrogates(payload), ensure_ascii=False, allow_nan=False, sort_keys=True
        )
    except (TypeError, ValueError) as exc:
        raise ChatSessionError("Accessor input payload must be JSON-serializable") from exc


def lookup(
    connection: sqlite3.Connection, input_id: str, *, request_hash: str | None = None
) -> sqlite3.Row | None:
    _identifier(input_id, "Accessor input id")
    row: sqlite3.Row | None = connection.execute(
        f"SELECT {_COLUMNS}{_FROM}WHERE i.input_id = ?", (input_id,)
    ).fetchone()
    if row is not None and request_hash is not None and row["request_hash"] != request_hash:
        raise AccessorInputConflictError("Accessor input id conflicts with its original request")
    return row


def decode(row: sqlite3.Row) -> AccessorInputRecord:
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError) as exc:
        raise SessionStoreCorruptError("Stored Accessor input payload is invalid") from exc
    if not isinstance(payload, dict):
        raise SessionStoreCorruptError("Stored Accessor input payload is not an object")
    return AccessorInputRecord(
        input_id=str(row["input_id"]),
        address=_store_values._address(row),
        generation_id=str(row["generation_id"]),
        request_hash=str(row["request_hash"]),
        payload=payload,
        queue_id=str(row["queue_id"]),
        state=str(row["input_state"]),
        run_id=None if row["run_id"] is None else str(row["run_id"]),
        created_at=str(row["created_at"]),
        live=row["session_state"] == "live",
        run_status=None if row["run_status"] is None else str(row["run_status"]),
        attachment_ids=tuple(sorted(json.loads(str(row["attachment_ids_json"])))),
        fallback_notes=_decode_notes(row["fallback_notes_json"]),
    )


def _decode_notes(value: str | None) -> tuple[ChatMessage, ...]:
    from core.chat.messages import ChatMessage

    if value is None:
        return ()
    try:
        records = json.loads(value)
        if not isinstance(records, list):
            raise ValueError("not a Note batch")
        notes = tuple(ChatMessage.from_dict(record) for record in records)
        if any(note.role != "note" for note in notes):
            raise ValueError("not a Note carrier")
    except (TypeError, ValueError, KeyError, ChatMessageValidationError) as exc:
        raise SessionStoreCorruptError("Stored Accessor fallback Notes are invalid") from exc
    return notes


def accept(
    connection: sqlite3.Connection,
    address: SessionAddress,
    input_id: str,
    *,
    request_hash: str,
    payload: JsonObject,
    queue_id: str,
    expected_generation_id: str | None,
    attachment_ids: Sequence[str],
    fallback_input: ChatMessage | None,
    fallback_notes: Sequence[ChatMessage],
    source: SessionAddress | None = None,
    expected_source_generation_id: str | None = None,
    expected_working_project_id: str | None = None,
) -> tuple[sqlite3.Row, bool]:
    _identifier(input_id, "Accessor input id")
    _identifier(request_hash, "Accessor request hash")
    _identifier(queue_id, "Accessor queue id")
    encoded = _payload_json(payload)
    existing = lookup(connection, input_id, request_hash=request_hash)
    if existing is not None:
        return existing, False
    state = _store_values._require_live(connection, address)
    if expected_generation_id is not None and state["generation_id"] != expected_generation_id:
        raise ChatSessionError("Session generation changed before Accessor input acceptance")
    if source is not None:
        # Both addresses are checked in the acceptance transaction. A Tool can
        # already hold its caller's write lease, so taking the peer's async lock
        # here would deadlock simultaneous opposite-direction sends.
        source_state = _store_values._require_live(connection, source)
        if source_state["generation_id"] != expected_source_generation_id:
            raise SessionNotFoundError(
                "The sending Session generation changed before input acceptance"
            )
        if not expected_working_project_id:
            raise ChatSessionError("A Session message requires a Working Project")
        for peer in (source, address):
            working = _store_queries.working_project(connection, peer)
            if working.archived or working.project_id != expected_working_project_id:
                raise ChatSessionError("The Sessions are not in the same live Working Project")
    cursor = connection.execute(
        "INSERT INTO session_accessor_inputs "
        "(input_id, session_key, queue_id, request_hash, state, created_at) "
        "VALUES (?, ?, ?, ?, 'queued', ?)",
        (input_id, state["session_key"], queue_id, request_hash, utc_now_timestamp()),
    )
    connection.execute(
        "INSERT INTO session_accessor_input_payloads (input_key, payload_json) VALUES (?, ?)",
        (cursor.lastrowid, encoded),
    )
    _replace_attachments(connection, int(cursor.lastrowid or 0), attachment_ids)
    _replace_carrier(connection, int(cursor.lastrowid or 0), fallback_input, fallback_notes)
    row = lookup(connection, input_id)
    assert row is not None
    return row, True


def queued(connection: sqlite3.Connection) -> tuple[sqlite3.Row, ...]:
    return tuple(
        connection.execute(
            f"SELECT {_COLUMNS}{_FROM}"
            "WHERE i.state = 'queued' AND s.state = 'live' ORDER BY i.input_key"
        ).fetchall()
    )


def _queued_row(
    connection: sqlite3.Connection, address: SessionAddress, input_id: str
) -> sqlite3.Row:
    state = _store_values._require_live(connection, address)
    row = lookup(connection, input_id)
    if row is None or row["session_key"] != state["session_key"]:
        raise ChatSessionError("Accessor input does not belong to this Session generation")
    if row["input_state"] != "queued":
        raise ChatSessionError("Accessor input is no longer queued")
    return row


def update(
    connection: sqlite3.Connection,
    address: SessionAddress,
    input_id: str,
    *,
    payload_patch: JsonObject,
    attachment_ids: Sequence[str] | None,
    fallback_input: ChatMessage | None,
    fallback_notes: Sequence[ChatMessage] | None,
) -> sqlite3.Row:
    row = _queued_row(connection, address, input_id)
    payload = decode(row).payload
    payload.update(json.loads(_payload_json(payload_patch)))
    connection.execute(
        "UPDATE session_accessor_input_payloads SET payload_json = ? WHERE input_key = ?",
        (_payload_json(payload), row["input_key"]),
    )
    if attachment_ids is not None:
        _replace_attachments(connection, int(row["input_key"]), attachment_ids)
    if fallback_input is not None or fallback_notes is not None:
        _replace_carrier(connection, int(row["input_key"]), fallback_input, fallback_notes)
    updated = lookup(connection, input_id)
    assert updated is not None
    return updated


def remove(connection: sqlite3.Connection, address: SessionAddress, input_id: str) -> bool:
    state = _store_values._require_live(connection, address)
    row = lookup(connection, input_id)
    if row is None or row["session_key"] != state["session_key"]:
        return False
    if row["input_state"] != "queued":
        return False
    connection.execute(
        "UPDATE session_accessor_inputs SET state = 'removed' WHERE input_key = ?",
        (row["input_key"],),
    )
    connection.execute(
        "UPDATE session_accessor_input_payloads SET payload_json = '{}' WHERE input_key = ?",
        (row["input_key"],),
    )
    _replace_attachments(connection, int(row["input_key"]), ())
    connection.execute(
        "DELETE FROM session_accessor_input_carriers WHERE input_key = ?", (row["input_key"],)
    )
    return True


def claim(
    connection: sqlite3.Connection,
    address: SessionAddress,
    input_id: str,
    run_key: int,
) -> None:
    state = _store_values._require_live(connection, address)
    row = lookup(connection, input_id)
    if row is None or row["session_key"] != state["session_key"]:
        raise ChatSessionError("Accessor input does not belong to this Session generation")
    prior = connection.execute(
        "SELECT input_id FROM session_accessor_inputs WHERE run_key = ? AND state = 'started'",
        (run_key,),
    ).fetchone()
    if prior is not None and prior["input_id"] != input_id:
        raise ChatSessionError("This Run already answers another Accessor input")
    if row["input_state"] == "started" and row["run_key"] == run_key:
        return
    if row["input_state"] != "queued":
        raise ChatSessionError("Accessor input was already consumed or removed")
    connection.execute(
        "UPDATE session_accessor_inputs SET state = 'started', run_key = ? WHERE input_key = ?",
        (run_key, row["input_key"]),
    )


def prepare_steering(
    connection: sqlite3.Connection,
    address: SessionAddress,
    input_ids: Sequence[str],
    run_id: str | None,
) -> tuple[int, tuple[int, ...]] | None:
    """Validate the whole batch; a complete exact replay appends no carriers."""
    state = _store_values._require_live(connection, address)
    run = connection.execute(
        "SELECT run_key, status, inherited FROM runs WHERE session_key = ? AND run_id = ?",
        (state["session_key"], run_id),
    ).fetchone()
    if run is None or run["status"] != "running" or run["inherited"]:
        raise ChatSessionError("Steering requires an admitted, running Run in this Session")
    run_key = int(run["run_key"])
    rows = [lookup(connection, input_id) for input_id in dict.fromkeys(input_ids)]
    if any(row is None or row["session_key"] != state["session_key"] for row in rows):
        raise ChatSessionError("Accessor input does not belong to this Session generation")
    actual = [row for row in rows if row is not None]
    if all(row["input_state"] == "steered" and row["run_key"] == run_key for row in actual):
        return None
    if any(row["input_state"] != "queued" for row in actual):
        raise ChatSessionError("Accessor steering batch was already consumed or removed")
    return run_key, tuple(int(row["input_key"]) for row in actual)


def commit_steering(connection: sqlite3.Connection, prepared: tuple[int, tuple[int, ...]]) -> None:
    run_key, input_keys = prepared
    connection.executemany(
        "UPDATE session_accessor_inputs SET state = 'steered', run_key = ? WHERE input_key = ?",
        [(run_key, input_key) for input_key in input_keys],
    )
    connection.executemany(
        "DELETE FROM session_accessor_input_attachments WHERE input_key = ?",
        [(input_key,) for input_key in input_keys],
    )
    connection.executemany(
        "DELETE FROM session_accessor_input_carriers WHERE input_key = ?",
        [(input_key,) for input_key in input_keys],
    )


def _replace_carrier(
    connection: sqlite3.Connection,
    input_key: int,
    fallback_input: ChatMessage | None,
    fallback_notes: Sequence[ChatMessage] | None,
) -> None:
    from core.sessions import _store_codec

    previous = connection.execute(
        "SELECT message_json, notes_json FROM session_accessor_input_carriers WHERE input_key = ?",
        (input_key,),
    ).fetchone()
    if fallback_input is None:
        if previous is None:
            if fallback_notes:
                raise ChatSessionError("Accessor fallback Notes require a User carrier")
            return
        message_json = str(previous["message_json"])
    else:
        fallback_input.validate()
        if fallback_input.role != "user":
            raise ChatSessionError("An Accessor fallback carrier must be a User message")
        message_json = _payload_json(_store_codec.storable(fallback_input).to_dict())
    if fallback_notes is None:
        notes_json = str(previous["notes_json"]) if previous is not None else "[]"
    else:
        for note in fallback_notes:
            note.validate()
            if note.role != "note":
                raise ChatSessionError("Accessor fallback Notes must have the Note role")
        notes_json = json.dumps(
            [_store_codec.storable(note).to_dict() for note in fallback_notes],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
    connection.execute(
        "INSERT INTO session_accessor_input_carriers (input_key, message_json, notes_json) "
        "VALUES (?, ?, ?) ON CONFLICT (input_key) DO UPDATE SET "
        "message_json = excluded.message_json, notes_json = excluded.notes_json",
        (input_key, message_json, notes_json),
    )


def materialize_unwritten_input(
    connection: sqlite3.Connection, address: SessionAddress, run_id: str
) -> bool:
    """Preserve admitted input missing its User carrier without executing it again."""
    from core.chat.messages import ChatMessage
    from core.sessions import _store_mutations

    state = _store_values._require_live(connection, address)
    row = connection.execute(
        "SELECT c.message_json, c.notes_json FROM session_accessor_inputs AS i "
        "JOIN session_accessor_input_carriers AS c ON c.input_key = i.input_key "
        "JOIN runs AS r ON r.run_key = i.run_key "
        "WHERE r.session_key = ? AND r.run_id = ? AND i.session_key = r.session_key "
        "AND i.state = 'started' "
        "AND NOT EXISTS (SELECT 1 FROM entries AS e WHERE e.run_key = r.run_key "
        "AND e.role = 'user')",
        (state["session_key"], run_id),
    ).fetchone()
    if row is None:
        return False
    try:
        message = ChatMessage.from_dict(json.loads(str(row["message_json"])))
        if message.role != "user":
            raise ValueError("not a User carrier")
        saved_notes = _decode_notes(str(row["notes_json"]))
    except (TypeError, ValueError, KeyError) as exc:
        raise SessionStoreCorruptError("Stored Accessor fallback carrier is invalid") from exc
    # Input preparation may have flushed its deferred Notes before failing to
    # append the User carrier. Preserve them once when settling or recovering
    # the Run, scoped to its actual membership rather than a public id alone.
    notes = [
        note
        for note in saved_notes
        if connection.execute(
            "SELECT 1 FROM entries AS e JOIN runs AS r ON r.run_key = e.run_key "
            "WHERE r.session_key = ? AND r.run_id = ? AND e.role = 'note' "
            "AND e.entry_id = ? LIMIT 1",
            (state["session_key"], run_id, note.id),
        ).fetchone()
        is None
    ]
    _store_mutations.append_messages(connection, address, [*notes, message], run_id=run_id)
    return True


def _replace_attachments(
    connection: sqlite3.Connection, input_key: int, attachment_ids: Sequence[str]
) -> None:
    if isinstance(attachment_ids, str | bytes):
        raise ChatSessionError("Queued Accessor attachment ids must be a sequence of ids")
    normalized: set[str] = set()
    for attachment_id in attachment_ids:
        if not isinstance(attachment_id, str) or not is_safe_id(attachment_id.lower()):
            raise ChatSessionError("Invalid queued Accessor attachment id")
        normalized.add(attachment_id.lower())
    connection.execute(
        "DELETE FROM session_accessor_input_attachments WHERE input_key = ?", (input_key,)
    )
    connection.executemany(
        "INSERT OR IGNORE INTO session_attachment_ids (attachment_id) VALUES (?)",
        [(attachment_id,) for attachment_id in normalized],
    )
    connection.executemany(
        "INSERT INTO session_accessor_input_attachments (input_key, attachment_id) VALUES (?, ?)",
        [(input_key, attachment_id) for attachment_id in normalized],
    )
