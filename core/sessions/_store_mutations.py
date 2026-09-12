"""Atomic Session metadata and history mutations in a supplied transaction."""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.chat.errors import ChatSessionError
from core.sessions import _store_codec, _store_fts, _store_values
from core.sessions._types import JsonObject
from core.sessions.errors import SessionStoreCorruptError

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions._types import SessionAddress


def create(
    connection: sqlite3.Connection,
    address: SessionAddress,
    created_at: str | None = None,
    *,
    generate_id: bool = False,
) -> SessionAddress:
    timestamp = created_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def _fn(connection: sqlite3.Connection) -> None:
        nonlocal address
        if generate_id:
            address = _store_values._allocate_address(connection, address)
        try:
            connection.execute(
                "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, "
                "created_at, active_sort) VALUES (?, ?, ?, ?, ?, "
                "COALESCE(julianday(?), 0.0))",
                (uuid.uuid4().hex, *_store_values._scope(address), timestamp, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ChatSessionError(f"session already exists: {address.session_id}") from exc

    _fn(connection)
    return address


def ensure_live(connection: sqlite3.Connection, address: SessionAddress) -> None:
    """Atomically return an existing live Session or create a new generation."""
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def _fn(connection: sqlite3.Connection) -> None:
        if _store_values._find_live(connection, address) is not None:
            return
        connection.execute(
            "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, "
            "created_at, active_sort) VALUES (?, ?, ?, ?, ?, "
            "COALESCE(julianday(?), 0.0))",
            (uuid.uuid4().hex, *_store_values._scope(address), timestamp, timestamp),
        )

    _fn(connection)


def replace_metadata(
    connection: sqlite3.Connection, address: SessionAddress, metadata: JsonObject
) -> None:
    _store_values._json_object(metadata, "session metadata")
    payload, projection = _store_values._session_metadata_storage(metadata)

    def _fn(connection: sqlite3.Connection) -> None:
        state = _store_values._require_live(connection, address)
        connection.execute(
            "UPDATE sessions SET metadata_json = ?, "
            + ", ".join(
                f"{column} = ?" for column in _store_values._SESSION_METADATA_PROJECTION_COLUMNS
            )
            + ", state_revision = state_revision + 1 WHERE session_key = ?",
            (payload, *projection, state["session_key"]),
        )

    _fn(connection)


def mutate_metadata(
    connection: sqlite3.Connection,
    address: SessionAddress,
    mutation: Callable[[JsonObject], None],
) -> tuple[JsonObject, JsonObject]:
    """Apply one metadata read-modify-write under the writer transaction."""
    result: tuple[JsonObject, JsonObject] | None = None

    def _fn(connection: sqlite3.Connection) -> None:
        nonlocal result
        state = _store_values._require_live(connection, address)
        previous = _store_values._session_metadata_from_state(state)
        updated = deepcopy(previous)
        mutation(updated)
        _store_values._json_object(updated, "session metadata")
        payload, projection = _store_values._session_metadata_storage(updated)
        connection.execute(
            "UPDATE sessions SET metadata_json = ?, "
            + ", ".join(
                f"{column} = ?" for column in _store_values._SESSION_METADATA_PROJECTION_COLUMNS
            )
            + ", state_revision = state_revision + 1 WHERE session_key = ?",
            (payload, *projection, state["session_key"]),
        )
        result = (previous, updated)

    _fn(connection)
    assert result is not None
    return result


def replace_activity(
    connection: sqlite3.Connection, address: SessionAddress, activity: JsonObject
) -> None:
    payload = _store_values._json_object(activity, "session activity")

    def _fn(connection: sqlite3.Connection) -> None:
        state = _store_values._require_live(connection, address)
        connection.execute(
            "UPDATE sessions SET activity_json = ?, state_revision = state_revision + 1 WHERE session_key = ?",
            (payload, state["session_key"]),
        )

    _fn(connection)


def mutate_activity(
    connection: sqlite3.Connection,
    address: SessionAddress,
    mutation: Callable[[JsonObject], None],
) -> tuple[JsonObject, JsonObject]:
    """Apply one activity read-modify-write under the writer transaction."""
    result: tuple[JsonObject, JsonObject] | None = None

    def _fn(connection: sqlite3.Connection) -> None:
        nonlocal result
        state = _store_values._require_live(connection, address)
        previous = _store_values._json_from_payload(state["activity_json"], "session activity")
        updated = deepcopy(previous)
        mutation(updated)
        payload = _store_values._json_object(updated, "session activity")
        connection.execute(
            "UPDATE sessions SET activity_json = ?, state_revision = state_revision + 1 WHERE session_key = ?",
            (payload, state["session_key"]),
        )
        result = (previous, updated)

    _fn(connection)
    assert result is not None
    return result


def append_messages(
    connection: sqlite3.Connection, address: SessionAddress, messages: Sequence[ChatMessage]
) -> None:
    if not messages:
        return
    for message in messages:
        _store_codec._message_base_row(message)

    def _fn(connection: sqlite3.Connection) -> None:
        state = _store_values._require_live(connection, address)
        session_key = int(state["session_key"])
        next_seq = int(state["message_count"])
        for index, message in enumerate(messages):
            _store_codec._insert_message(connection, session_key, next_seq + index, message)
        last_message = messages[-1]
        connection.execute(
            "UPDATE sessions SET message_count = message_count + ?, last_message_at = ?, "
            "active_sort = COALESCE(julianday(?), 0.0), last_message_id = ?, "
            "history_revision = history_revision + 1, state_revision = state_revision + 1 "
            "WHERE session_key = ?",
            (
                len(messages),
                last_message.timestamp,
                last_message.timestamp,
                last_message.id,
                session_key,
            ),
        )
        _store_fts._mark_fts_write(connection)

    _fn(connection)


def archive(connection: sqlite3.Connection, address: SessionAddress) -> None:
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def _fn(connection: sqlite3.Connection) -> None:
        state = _store_values._require_live(connection, address)
        _store_values._reject_owner_managed_mutation(connection, state)
        connection.execute(
            "UPDATE sessions SET status = 'archived', archived_at = ?, state_revision = state_revision + 1 WHERE session_key = ?",
            (timestamp, state["session_key"]),
        )

    _fn(connection)


def move(
    connection: sqlite3.Connection,
    source: SessionAddress,
    target: SessionAddress,
    prepare_metadata: Callable[[JsonObject, int], None],
) -> None:
    """Relocate one complete Session row and its dependent rows atomically."""

    def _fn(connection: sqlite3.Connection) -> None:
        state = _store_values._require_live(connection, source)
        _store_values._reject_owner_managed_mutation(connection, state)
        collision = connection.execute(
            "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'live'",
            _store_values._scope(target),
        ).fetchone()
        if collision is not None:
            raise ChatSessionError(f"destination session already exists: {target.session_id}")
        metadata = _store_values._session_metadata_from_state(state)
        prepare_metadata(metadata, int(state["message_count"]))
        _store_values._json_object(metadata, "session metadata")
        payload, projection = _store_values._session_metadata_storage(metadata)
        connection.execute(
            "UPDATE sessions SET project_id = ?, agent_id = ?, session_id = ?, metadata_json = ?, "
            + ", ".join(
                f"{column} = ?" for column in _store_values._SESSION_METADATA_PROJECTION_COLUMNS
            )
            + ", state_revision = state_revision + 1 WHERE session_key = ?",
            (*_store_values._scope(target), payload, *projection, state["session_key"]),
        )

    _fn(connection)


def fork(
    connection: sqlite3.Connection,
    source: SessionAddress,
    target: SessionAddress,
    prepare_metadata: Callable[[JsonObject, int], None],
    *,
    generate_id: bool = False,
    allow_owner_managed_source: bool = False,
) -> SessionAddress:
    """Copy canonical history to a new live Session without activity/journal state."""

    def _fn(connection: sqlite3.Connection) -> None:
        nonlocal target
        if generate_id:
            target = _store_values._allocate_address(connection, target)
        state = _store_values._require_live(connection, source)
        if not allow_owner_managed_source:
            _store_values._reject_owner_managed_mutation(connection, state)
        metadata = _store_values._session_metadata_from_state(state)
        prepare_metadata(metadata, int(state["message_count"]))
        _store_values._json_object(metadata, "session metadata")
        payload, projection = _store_values._session_metadata_storage(metadata)
        try:
            target_row = connection.execute(
                "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, "
                "created_at, last_message_at, active_sort, message_count, last_message_id, "
                "history_revision, state_revision, metadata_json, "
                + ", ".join(_store_values._SESSION_METADATA_PROJECTION_COLUMNS)
                + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, "
                + ", ".join("?" for _ in _store_values._SESSION_METADATA_PROJECTION_COLUMNS)
                + ")",
                (
                    uuid.uuid4().hex,
                    *_store_values._scope(target),
                    state["created_at"],
                    state["last_message_at"],
                    state["active_sort"],
                    state["message_count"],
                    state["last_message_id"],
                    state["history_revision"],
                    payload,
                    *projection,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ChatSessionError(
                f"destination session already exists: {target.session_id}"
            ) from exc
        target_session_key = target_row.lastrowid
        if target_session_key is None:
            raise SessionStoreCorruptError("SQLite did not return a forked Session key")
        _store_codec._copy_session_messages(
            connection,
            source_session_key=int(state["session_key"]),
            target_session_key=int(target_session_key),
        )
        _store_fts._mark_fts_write(connection)

    _fn(connection)
    return target


def restore(connection: sqlite3.Connection, address: SessionAddress) -> None:
    def _fn(connection: sqlite3.Connection) -> None:
        collision = connection.execute(
            "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'live'",
            _store_values._scope(address),
        ).fetchone()
        if collision is not None:
            raise ChatSessionError(f"live session already exists: {address.session_id}")
        row = connection.execute(
            "SELECT session_key FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ? AND status = 'archived' ORDER BY session_key DESC LIMIT 1",
            _store_values._scope(address),
        ).fetchone()
        if row is None:
            raise ChatSessionError(f"archived session does not exist: {address.session_id}")
        connection.execute(
            "UPDATE sessions SET status = 'live', archived_at = NULL, state_revision = state_revision + 1 WHERE session_key = ?",
            (row["session_key"],),
        )

    _fn(connection)


def delete(connection: sqlite3.Connection, address: SessionAddress) -> None:
    def _fn(connection: sqlite3.Connection) -> None:
        state = _store_values._require_live(connection, address)
        _store_values._reject_owner_managed_mutation(connection, state)
        connection.execute("DELETE FROM sessions WHERE session_key = ?", (state["session_key"],))

    _fn(connection)


def retarget_identity_agent(
    connection: sqlite3.Connection, old_agent_id: str, new_agent_id: str
) -> None:
    """Rename every global Session address for one Identity Agent atomically."""

    def _fn(connection: sqlite3.Connection) -> None:
        _store_values._reject_owner_managed_scope_mutation(
            connection,
            "s.project_id = '' AND s.agent_id = ? AND s.status = 'live'",
            (old_agent_id,),
        )
        collision = connection.execute(
            "SELECT 1 FROM sessions AS source WHERE source.project_id = '' AND source.agent_id = ? "
            "AND EXISTS (SELECT 1 FROM sessions AS target WHERE target.project_id = '' "
            "AND target.agent_id = ? AND target.session_id = source.session_id AND target.status = 'live') "
            "AND source.status = 'live'",
            (old_agent_id, new_agent_id),
        ).fetchone()
        if collision is not None:
            raise ChatSessionError("destination Agent already has a Session with the same id")
        connection.execute(
            "UPDATE sessions SET agent_id = ?, state_revision = state_revision + 1 "
            "WHERE project_id = '' AND agent_id = ? AND status = 'live'",
            (new_agent_id, old_agent_id),
        )

    _fn(connection)


def archive_identity_agent_sessions(connection: sqlite3.Connection, agent_id: str) -> None:
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def _fn(connection: sqlite3.Connection) -> None:
        _store_values._reject_owner_managed_scope_mutation(
            connection,
            "s.project_id = '' AND s.agent_id = ? AND s.status = 'live'",
            (agent_id,),
        )
        connection.execute(
            "UPDATE sessions SET status = 'archived', archived_at = ?, state_revision = state_revision + 1 "
            "WHERE project_id = '' AND agent_id = ? AND status = 'live'",
            (timestamp, agent_id),
        )

    _fn(connection)


def archive_project_sessions(connection: sqlite3.Connection, project_id: str) -> None:
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def _fn(connection: sqlite3.Connection) -> None:
        _store_values._reject_owner_managed_scope_mutation(
            connection,
            "s.project_id = ? AND s.status = 'live'",
            (project_id,),
        )
        connection.execute(
            "UPDATE sessions SET status = 'archived', archived_at = ?, state_revision = state_revision + 1 "
            "WHERE project_id = ? AND status = 'live'",
            (timestamp, project_id),
        )

    _fn(connection)
