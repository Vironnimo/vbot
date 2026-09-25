"""Session lifecycle, metadata and plain history appends in a supplied transaction."""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from core.chat.errors import ChatSessionError
from core.sessions import (
    _store_codec,
    _store_fts,
    _store_lineage,
    _store_prompts,
    _store_values,
)
from core.sessions._metadata import _RUN_KIND_VALUES
from core.sessions._types import (
    SESSION_FORK_ALWAYS_STRIP_META_KEYS,
    SESSION_TITLE_KEY,
    JsonObject,
    SessionIdentityReferenceUpdate,
    ToolResultFacts,
)
from core.sessions.errors import SessionStoreCorruptError
from core.utils.timestamps import utc_now_timestamp

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions._types import SessionAddress


def _insert_session(
    connection: sqlite3.Connection, address: SessionAddress, created_at: str
) -> int:
    cursor = connection.execute(
        "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, state, "
        "created_at, last_activity_at) VALUES (?, ?, ?, ?, 'live', ?, ?)",
        (uuid.uuid4().hex, *_store_values._scope(address), created_at, created_at),
    )
    if cursor.lastrowid is None:
        raise SessionStoreCorruptError("SQLite did not return a Session key")
    return int(cursor.lastrowid)


def create(
    connection: sqlite3.Connection,
    address: SessionAddress,
    created_at: str | None = None,
    *,
    generate_id: bool = False,
) -> SessionAddress:
    """Create a live Session; with *generate_id*, allocate a fresh id in its scope."""
    timestamp = (
        utc_now_timestamp()
        if created_at is None
        else _store_values._timestamp(created_at, "Session creation")
    )
    if generate_id:
        address = _store_values._allocate_address(connection, address)
    try:
        _insert_session(connection, address, timestamp)
    except sqlite3.IntegrityError as exc:
        raise ChatSessionError(f"session already exists: {address.session_id}") from exc
    return address


def ensure_live(connection: sqlite3.Connection, address: SessionAddress) -> None:
    """Keep an existing live Session or create a new generation at *address*."""
    if _store_values._find_live(connection, address) is None:
        _insert_session(connection, address, utc_now_timestamp())


# -- Metadata facade ---------------------------------------------------------


def _storage_change(
    state: sqlite3.Row, metadata: JsonObject
) -> _store_values._MetadataStorage | None:
    """Return the storage *metadata* needs, or ``None`` when the row already holds it."""
    storage = _store_values._session_metadata_storage(
        metadata, _store_values._derived_metadata_from_state(state)
    )
    if storage == _store_values._metadata_storage_of(state):
        return None
    return storage


def replace_metadata(
    connection: sqlite3.Connection, address: SessionAddress, metadata: JsonObject
) -> None:
    state = _store_values._live_metadata_row(connection, address)
    storage = _storage_change(state, metadata)
    if storage is not None:
        _store_values._write_metadata_storage(connection, int(state["session_key"]), storage)


def metadata_change(
    state: sqlite3.Row, mutation: Callable[[JsonObject], None]
) -> tuple[JsonObject, JsonObject, _store_values._MetadataStorage | None]:
    """Apply *mutation* to one metadata row's facade without writing it.

    Returns the previous and updated metadata plus the storage to persist, or
    ``None`` when the persisted form is unchanged and no write is needed.
    """
    previous = _store_values._session_metadata_from_state(state)
    updated = deepcopy(previous)
    mutation(updated)
    return previous, updated, _storage_change(state, updated)


def mutate_metadata(
    connection: sqlite3.Connection,
    address: SessionAddress,
    mutation: Callable[[JsonObject], None],
) -> tuple[JsonObject, JsonObject]:
    """Apply one metadata read-modify-write under the writer transaction."""
    state = _store_values._live_metadata_row(connection, address)
    previous, updated, storage = metadata_change(state, mutation)
    if storage is not None:
        _store_values._write_metadata_storage(connection, int(state["session_key"]), storage)
    return previous, updated


def ensure_metadata(
    connection: sqlite3.Connection,
    address: SessionAddress,
    mutation: Callable[[JsonObject], None],
    *,
    create_missing: bool,
) -> tuple[JsonObject, JsonObject]:
    """Optionally create the live Session, then apply one metadata mutation."""
    if create_missing:
        ensure_live(connection, address)
    return mutate_metadata(connection, address, mutation)


def record_run_kind_by_key(connection: sqlite3.Connection, session_key: int, run_kind: str) -> None:
    """Add *run_kind* to one Session's Run kinds; a known kind is a no-op."""
    if run_kind not in _RUN_KIND_VALUES:
        raise ChatSessionError(f"unknown run kind: {run_kind}")
    inserted = connection.execute(
        "INSERT OR IGNORE INTO session_run_kinds (session_key, run_kind) VALUES (?, ?)",
        (session_key, run_kind),
    ).rowcount
    if inserted:
        _store_values._refresh_visibility(connection, session_key)
        _store_values._touch_state(connection, session_key)


def record_run_kind(connection: sqlite3.Connection, address: SessionAddress, run_kind: str) -> None:
    """Classify a live Session by *run_kind* before its first Run of that kind."""
    state = _store_values._require_live(connection, address)
    record_run_kind_by_key(connection, int(state["session_key"]), run_kind)


# -- History appends -----------------------------------------------------------


def append_messages(
    connection: sqlite3.Connection,
    address: SessionAddress,
    messages: Sequence[ChatMessage],
    *,
    run_id: str | None = None,
    assistant_message_id: str | None = None,
    tool_results: Mapping[str, ToolResultFacts] | None = None,
) -> list[int]:
    """Append Messages to one live Session's history; return their entry keys.

    Messages of a Run require that Run to be admitted and running here. A Tool
    result links to the one stored call it answers; its *tool_results* facts
    record how the call ended, and a result without facts completed. An Agent
    takeover moves the cursor floor past itself, so no earlier read cursor
    continues across it.
    """
    if not messages:
        return []
    for message in messages:
        _store_codec.validate_appendable(message)
    facts = dict(tool_results or {})
    _store_codec.validate_tool_results(messages, facts)
    state = _store_values._require_live(connection, address)
    session_key = int(state["session_key"])
    run_key = None
    if run_id is not None:
        run = connection.execute(
            "SELECT run_key, status, inherited FROM runs WHERE session_key = ? AND run_id = ?",
            (session_key, run_id),
        ).fetchone()
        if run is None or run["status"] != "running" or run["inherited"]:
            raise ChatSessionError("Messages require an admitted, running Run in this Session")
        run_key = int(run["run_key"])
    seq = int(state["next_seq"])
    floor = int(state["cursor_floor_seq"])
    keys: list[int] = []
    for message in messages:
        entry_key = _store_codec.insert_entry(
            connection, session_key, seq, message, run_key=run_key
        )
        if message.role == "tool":
            _store_codec.link_tool_result(
                connection,
                session_key,
                entry_key,
                message,
                run_key=run_key,
                assistant_message_id=assistant_message_id,
                facts=facts.get(str(message.tool_call_id)),
            )
        elif message.role == "agent_takeover":
            floor = seq + 1
        keys.append(entry_key)
        seq += 1
    last = messages[-1]
    connection.execute(
        "UPDATE sessions SET next_seq = ?, last_activity_at = ?, last_entry_id = ?, "
        "cursor_floor_seq = ?, history_revision = history_revision + 1, "
        "state_revision = state_revision + 1 WHERE session_key = ?",
        (
            seq,
            _store_values._timestamp(last.timestamp, "Message timestamp"),
            last.id,
            floor,
            session_key,
        ),
    )
    _store_fts.fts_index_keys(connection, keys)
    return keys


# -- Lifecycle -------------------------------------------------------------------


def archive(connection: sqlite3.Connection, address: SessionAddress) -> None:
    state = _store_values._require_live(connection, address)
    _store_values._reject_owner_managed_mutation(connection, state)
    connection.execute(
        "UPDATE sessions SET state = 'archived', archived_at = ?, "
        "state_revision = state_revision + 1 WHERE session_key = ?",
        (utc_now_timestamp(), state["session_key"]),
    )


def _same_scope(source: SessionAddress, target: SessionAddress) -> bool:
    return (source.project_id or None, source.agent_id) == (
        target.project_id or None,
        target.agent_id,
    )


def move(connection: sqlite3.Connection, source: SessionAddress, target: SessionAddress) -> None:
    """Give one live Session a new address, history and relations unchanged.

    A move into another scope leaves the Agent-bound prompt state behind and
    starts a new prompt-cache lineage.
    """
    state = _store_values._require_live(connection, source)
    _store_values._reject_owner_managed_mutation(connection, state)
    if _store_values._find_live(connection, target) is not None:
        raise ChatSessionError(f"destination session already exists: {target.session_id}")
    session_key = int(state["session_key"])
    connection.execute(
        "UPDATE sessions SET project_id = ?, agent_id = ?, session_id = ?, "
        "state_revision = state_revision + 1 WHERE session_key = ?",
        (*_store_values._scope(target), session_key),
    )
    _store_prompts.carry_prompt_state(
        connection, source=state, target_key=session_key, same_scope=_same_scope(source, target)
    )


def fork_point(connection: sqlite3.Connection, state: sqlite3.Row) -> int:
    """Return where a fork of *state* ends: before its running Run, else at its end."""
    row = connection.execute(
        "SELECT MIN(start_seq) FROM runs WHERE session_key = ? AND status = 'running' "
        "AND inherited = 0",
        (state["session_key"],),
    ).fetchone()
    return int(state["next_seq"]) if row[0] is None else int(row[0])


def fork(
    connection: sqlite3.Connection,
    source: SessionAddress,
    target_scope: SessionAddress,
    *,
    title: str | None = None,
    run_kind: str | None = None,
    allow_owner_managed_source: bool = False,
) -> SessionAddress:
    """Create a fork of *source* under a fresh id in *target_scope*.

    The fork inherits the source's current view up to the fork point through
    lineage; no entry is copied. Channel, Sub-Agent and reflection bindings and
    the Run kinds stay behind; *run_kind* classifies the fork instead. A given
    *title* replaces the source's title, and an empty one clears it. Prompt
    state carries over by the scope rules of a move.
    """
    if run_kind is not None and run_kind not in _RUN_KIND_VALUES:
        raise ChatSessionError(f"unknown run kind: {run_kind}")
    state = _store_values._live_metadata_row(connection, source)
    if not allow_owner_managed_source:
        _store_values._reject_owner_managed_mutation(connection, state)
    target = _store_values._allocate_address(connection, target_scope)
    source_key = int(state["session_key"])
    point = fork_point(connection, state)
    metadata = _store_values._session_metadata_from_state(state)
    for key in (*SESSION_FORK_ALWAYS_STRIP_META_KEYS, _store_values._FORK_SOURCE_KEY):
        metadata.pop(key, None)
    if title is not None:
        if title:
            metadata[SESSION_TITLE_KEY] = title
        else:
            metadata.pop(SESSION_TITLE_KEY, None)
    storage = _store_values._session_metadata_storage(metadata, {})
    ranges = _store_lineage.view_ranges(connection, source_key)
    last_entry_id = (
        None if point == 0 else _store_lineage.entry_id_at(connection, ranges, point - 1)
    )
    cursor = connection.execute(
        "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, state, "
        "created_at, next_seq, last_activity_at, last_entry_id, fork_parent_key, forked_at, "
        "fork_point_seq, "
        + ", ".join(_store_values._METADATA_WRITE_COLUMNS)
        + ") VALUES (?, ?, ?, ?, 'live', ?, ?, ?, ?, ?, ?, ?, "
        + ", ".join("?" for _ in _store_values._METADATA_WRITE_COLUMNS)
        + ")",
        (
            uuid.uuid4().hex,
            *_store_values._scope(target),
            state["created_at"],
            point,
            state["last_activity_at"],
            last_entry_id,
            source_key,
            utc_now_timestamp(),
            point,
            *storage.columns,
        ),
    )
    if cursor.lastrowid is None:
        raise SessionStoreCorruptError("SQLite did not return a forked Session key")
    fork_key = int(cursor.lastrowid)
    _store_lineage.create_fork_lineage(
        connection,
        source_key=source_key,
        fork_key=fork_key,
        fork_point=point,
        source_next_seq=int(state["next_seq"]),
    )
    _store_prompts.carry_prompt_state(
        connection, source=state, target_key=fork_key, same_scope=_same_scope(source, target)
    )
    if run_kind is not None:
        record_run_kind_by_key(connection, fork_key, run_kind)
    return target


def restore(connection: sqlite3.Connection, address: SessionAddress) -> None:
    """Make the newest archived generation at *address* live again."""
    if _store_values._find_live(connection, address) is not None:
        raise ChatSessionError(f"live session already exists: {address.session_id}")
    row = connection.execute(
        "SELECT session_key FROM sessions WHERE project_id = ? AND agent_id = ? "
        "AND session_id = ? AND state = 'archived' ORDER BY session_key DESC LIMIT 1",
        _store_values._scope(address),
    ).fetchone()
    if row is None:
        raise ChatSessionError(f"archived session does not exist: {address.session_id}")
    connection.execute(
        "UPDATE sessions SET state = 'live', archived_at = NULL, "
        "state_revision = state_revision + 1 WHERE session_key = ?",
        (row["session_key"],),
    )


def delete_session(connection: sqlite3.Connection, session_key: int) -> None:
    """Delete one Session generation with everything it owns.

    Forks that inherit from it first receive their own copies, then deleting
    the row cascades, and prompt blobs no other Session pins go with it.
    """
    blob_keys = _store_prompts.session_blob_keys(connection, session_key)
    _store_lineage.detach_session(connection, session_key)
    connection.execute("DELETE FROM sessions WHERE session_key = ?", (session_key,))
    _store_prompts.delete_unreferenced_blobs(connection, blob_keys)


def delete(connection: sqlite3.Connection, address: SessionAddress) -> None:
    state = _store_values._require_live(connection, address)
    _store_values._reject_owner_managed_mutation(connection, state)
    delete_session(connection, int(state["session_key"]))


# -- Identity Agent scope changes ----------------------------------------------

_GLOBAL_AGENT_SCOPE = "s.project_id = '' AND s.agent_id = ? AND s.state = 'live'"


def retarget_identity_agent(
    connection: sqlite3.Connection, old_agent_id: str, new_agent_id: str
) -> None:
    """Rename every live global Session address of one Identity Agent."""
    _store_values._reject_owner_managed_scope_mutation(
        connection, _GLOBAL_AGENT_SCOPE, (old_agent_id,)
    )
    collision = connection.execute(
        "SELECT 1 FROM sessions AS s WHERE s.project_id = '' AND s.agent_id = ? "
        "AND s.state = 'live' AND EXISTS (SELECT 1 FROM sessions AS target "
        "WHERE target.project_id = '' AND target.agent_id = ? "
        "AND target.session_id = s.session_id AND target.state = 'live')",
        (old_agent_id, new_agent_id),
    ).fetchone()
    if collision is not None:
        raise ChatSessionError("destination Agent already has a Session with the same id")
    connection.execute(
        "UPDATE sessions SET agent_id = ?, state_revision = state_revision + 1 "
        "WHERE project_id = '' AND agent_id = ? AND state = 'live'",
        (new_agent_id, old_agent_id),
    )


def _write_subagent_parent(
    connection: sqlite3.Connection, session_key: int, parent: JsonObject | None
) -> None:
    columns = [column for _key, column in _store_values._SUBAGENT_PARENT_FIELDS]
    connection.execute(
        "UPDATE sessions SET "
        + ", ".join(f"{column} = ?" for column in columns)
        + ", state_revision = state_revision + 1 WHERE session_key = ?",
        (*_store_values._subagent_parent_columns(parent), session_key),
    )


def retarget_identity_agent_references(
    connection: sqlite3.Connection, old_agent_id: str, new_agent_id: str
) -> tuple[SessionIdentityReferenceUpdate, ...]:
    """Point every live Sub-Agent parent reference to the renamed Identity Agent."""
    rows = connection.execute(
        f"SELECT {_store_values._SESSION_STATE_COLUMNS} FROM sessions AS s "
        "WHERE s.state = 'live' AND s.subagent_parent_project_id IS NULL "
        "AND s.subagent_parent_agent_id = ? ORDER BY s.session_key",
        (old_agent_id,),
    ).fetchall()
    updates = []
    for row in rows:
        previous = _store_values._subagent_parent_from_state(row)
        assert previous is not None
        updated = {**previous, "agent_id": new_agent_id}
        _write_subagent_parent(connection, int(row["session_key"]), updated)
        updates.append(
            SessionIdentityReferenceUpdate(_store_values._address(row), previous, updated)
        )
    return tuple(updates)


def restore_identity_agent_references(
    connection: sqlite3.Connection, updates: tuple[SessionIdentityReferenceUpdate, ...]
) -> None:
    """Undo retargeted parent references that nothing changed since."""
    for update in reversed(updates):
        state = _store_values._require_live(connection, update.address)
        if _store_values._subagent_parent_from_state(state) != update.updated_parent:
            continue
        _write_subagent_parent(connection, int(state["session_key"]), update.previous_parent)


def _archive_scope(connection: sqlite3.Connection, where: str, params: tuple[Any, ...]) -> None:
    _store_values._reject_owner_managed_scope_mutation(connection, where, params)
    connection.execute(
        "UPDATE sessions AS s SET state = 'archived', archived_at = ?, "
        f"state_revision = state_revision + 1 WHERE {where}",
        (utc_now_timestamp(), *params),
    )


def archive_identity_agent_sessions(connection: sqlite3.Connection, agent_id: str) -> None:
    _archive_scope(connection, _GLOBAL_AGENT_SCOPE, (agent_id,))


def archive_project_sessions(connection: sqlite3.Connection, project_id: str) -> None:
    _archive_scope(connection, "s.project_id = ? AND s.state = 'live'", (project_id,))
