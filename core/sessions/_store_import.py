"""Offline canonical-generation insertion within the Store-owned transaction."""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import TYPE_CHECKING

from core.chat.errors import ChatSessionError
from core.sessions import (
    _store_codec,
    _store_continuation,
    _store_fts,
    _store_values,
)
from core.sessions._types import JsonObject
from core.sessions.errors import SessionStoreCorruptError

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions._types import SessionAddress


def import_generation(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    generation_id: str,
    messages: Sequence[ChatMessage],
    metadata: JsonObject,
    activity: JsonObject,
    continuation: Sequence[JsonObject],
    archived: bool,
    created_at: str,
    index_fts: bool,
) -> None:
    if (
        not isinstance(generation_id, str)
        or len(generation_id) != 32
        or any(character not in "0123456789abcdef" for character in generation_id)
    ):
        raise ChatSessionError("offline generation id is invalid")
    metadata_payload = _store_values._json_object(metadata, "session metadata")
    residual_metadata_payload, metadata_projection = _store_values._session_metadata_storage(
        metadata
    )
    activity_payload = _store_values._json_object(activity, "session activity")
    for message in messages:
        _store_codec._message_base_row(message)
    for record in continuation:
        _store_values._json_object(record, "continuation record")

    def _fn(connection: sqlite3.Connection) -> None:
        try:
            cursor = connection.execute(
                "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, status, "
                "created_at, last_message_at, active_sort, archived_at, message_count, last_message_id, "
                "history_revision, state_revision, metadata_json, "
                + ", ".join(_store_values._SESSION_METADATA_PROJECTION_COLUMNS)
                + ", activity_json) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(julianday(?), 0.0), ?, ?, ?, ?, ?, ?, "
                + ", ".join("?" for _ in _store_values._SESSION_METADATA_PROJECTION_COLUMNS)
                + ", ?)",
                (
                    generation_id,
                    *_store_values._scope(address),
                    "archived" if archived else "live",
                    created_at,
                    messages[-1].timestamp if messages else None,
                    messages[-1].timestamp if messages else created_at,
                    created_at if archived else None,
                    len(messages),
                    messages[-1].id if messages else None,
                    1 if messages else 0,
                    1
                    if messages
                    or continuation
                    or metadata_payload != "{}"
                    or activity_payload != "{}"
                    else 0,
                    residual_metadata_payload,
                    *metadata_projection,
                    activity_payload,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ChatSessionError(
                f"offline Session generation conflicts: {address.session_id}"
            ) from exc
        session_key = cursor.lastrowid
        if session_key is None:
            raise SessionStoreCorruptError("SQLite did not return an offline Session key")
        for sequence, message in enumerate(messages):
            _store_codec._insert_message(
                connection,
                int(session_key),
                sequence,
                message,
                index_fts=index_fts,
            )
        for record in continuation:
            _store_continuation._apply_continuation_record(connection, int(session_key), record)
        if index_fts:
            _store_fts._mark_fts_write(connection)

    _fn(connection)
