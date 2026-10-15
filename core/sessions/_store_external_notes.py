"""Exactly-once context delivery from a durable accessor inbox."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence

from core.chat.errors import ChatSessionError
from core.sessions import _store_mutations, _store_values
from core.sessions._types import SessionAddress
from core.sessions.errors import SessionNotFoundError


def generation_address(connection: sqlite3.Connection, generation_id: str) -> SessionAddress | None:
    """Resolve a live incarnation after an Agent rename or Session move."""
    row = connection.execute(
        "SELECT project_id, agent_id, session_id FROM sessions "
        "WHERE generation_id = ? AND state = 'live'",
        (generation_id,),
    ).fetchone()
    if row is None:
        return None
    return SessionAddress(
        project_id=row["project_id"] or None,
        agent_id=str(row["agent_id"]),
        session_id=str(row["session_id"]),
    )


def append_notes_once(
    connection: sqlite3.Connection,
    address: SessionAddress,
    delivery_id: str,
    notes: Sequence[str],
    *,
    generation_id: str,
) -> None:
    """Commit a whole external Note batch with its receipt, or do nothing on replay."""
    from core.chat.messages import ChatMessage

    if not delivery_id or not notes or any(not isinstance(note, str) for note in notes):
        raise ChatSessionError("External Note delivery is invalid")
    state = _store_values._require_live(connection, address)
    if state["generation_id"] != generation_id:
        raise SessionNotFoundError("External Note delivery belongs to another Session generation")
    digest = hashlib.sha256(json.dumps(list(notes), ensure_ascii=False).encode()).hexdigest()
    existing = connection.execute(
        "SELECT content_hash FROM session_external_note_receipts "
        "WHERE session_key = ? AND delivery_id = ?",
        (state["session_key"], delivery_id),
    ).fetchone()
    if existing is not None:
        if existing["content_hash"] != digest:
            raise ChatSessionError("External Note delivery conflicts with its prior content")
        return
    _store_mutations.append_messages(
        connection, address, [ChatMessage.note(note) for note in notes]
    )
    connection.execute(
        "INSERT INTO session_external_note_receipts (session_key, delivery_id, content_hash) "
        "VALUES (?, ?, ?)",
        (state["session_key"], delivery_id, digest),
    )
