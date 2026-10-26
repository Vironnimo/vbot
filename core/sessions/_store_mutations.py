"""Session lifecycle, metadata and plain history appends in a supplied transaction."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING

from core.chat.errors import ChatSessionError
from core.sessions import (
    _store_codec,
    _store_copies,
    _store_fts,
    _store_lineage,
    _store_prompts,
    _store_values,
)
from core.sessions._archive_types import ARCHIVE_KIND_AGENT, ARCHIVE_KIND_SESSION
from core.sessions._metadata import _RUN_KIND_VALUES
from core.sessions._types import (
    SESSION_FORK_ALWAYS_STRIP_META_KEYS,
    SESSION_TITLE_KEY,
    SUBAGENT_PARENT_META_KEY,
    SUBAGENT_REPLY_TARGET_META_KEY,
    JsonObject,
    SessionIdentityReferenceUpdate,
    ToolResultFacts,
)
from core.sessions.errors import SessionNotFoundError, SessionStoreCorruptError
from core.utils.timestamps import utc_now_timestamp

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions._types import SessionAddress

# ruff: noqa: E501


def _new_working_project(address: SessionAddress, working_project_id: str | None) -> str | None:
    """Return the working Project column of a new Session at *address*.

    A Project Session works in its address Project, so only a Session of an
    Identity Agent stores one.
    """
    if working_project_id is not None and address.project_id is not None:
        raise ChatSessionError("A Project Session works in its own Project")
    return working_project_id


def _insert_session(
    connection: sqlite3.Connection,
    address: SessionAddress,
    created_at: str,
    working_project_id: str | None = None,
) -> int:
    cursor = connection.execute(
        "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, state, "
        "created_at, last_activity_at, working_project_id) VALUES (?, ?, ?, ?, 'live', ?, ?, ?)",
        (
            uuid.uuid4().hex,
            *_store_values._scope(address),
            created_at,
            created_at,
            _new_working_project(address, working_project_id),
        ),
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
    run_kind: str | None = None,
    metadata: JsonObject | None = None,
    working_project_id: str | None = None,
) -> SessionAddress:
    """Create a live Session; with *generate_id*, allocate a fresh id in its scope.

    A *run_kind* labels the Session and *metadata* sets facade values from its
    first write, so no reader sees the Session without them. A Session of an
    Identity Agent works in *working_project_id* (``None``: its Workspace) for
    its whole life.
    """
    timestamp = (
        utc_now_timestamp()
        if created_at is None
        else _store_values._timestamp(created_at, "Session creation")
    )
    if generate_id:
        address = _store_values._allocate_address(connection, address)
    try:
        session_key = _insert_session(connection, address, timestamp, working_project_id)
    except sqlite3.IntegrityError as exc:
        raise ChatSessionError(f"session already exists: {address.session_id}") from exc
    if run_kind is not None:
        record_run_kind_by_key(connection, session_key, run_kind)
    if metadata:
        initial = dict(metadata)
        mutate_metadata(connection, address, lambda current: current.update(initial))
    return address


def ensure_live(
    connection: sqlite3.Connection,
    address: SessionAddress,
    working_project_id: str | None = None,
) -> None:
    """Keep an existing live Session or create a new generation at *address*.

    A created Session of an Identity Agent works in *working_project_id*; a
    Project Session ignores it and works in its address Project.
    """
    if _store_values._find_live(connection, address) is None:
        _insert_session(
            connection,
            address,
            utc_now_timestamp(),
            None if address.project_id is not None else working_project_id,
        )


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
    *,
    expected_generation_id: str | None = None,
    expected_first_user_sequence: int | None = None,
) -> tuple[JsonObject, JsonObject]:
    """Apply one metadata read-modify-write under the writer transaction.

    With *expected_generation_id*, another live generation counts as missing.
    """
    state = _store_values._live_metadata_row(connection, address)
    if expected_generation_id is not None and str(state["generation_id"]) != expected_generation_id:
        raise SessionNotFoundError(f"session generation changed: {address.session_id}")
    if expected_first_user_sequence is not None:
        first_user = _store_lineage.ordered_rows(
            connection,
            _store_lineage.view_ranges(connection, int(state["session_key"])),
            columns="e.seq",
            where="e.role = 'user'",
            limit=1,
        )
        if not first_user or int(first_user[0][0]) != expected_first_user_sequence:
            raise SessionNotFoundError(f"session title source changed: {address.session_id}")
    previous, updated, storage = metadata_change(state, mutation)
    if storage is not None:
        _store_values._write_metadata_storage(connection, int(state["session_key"]), storage)
    return previous, updated


def record_subagent_reply_target(
    connection: sqlite3.Connection, address: SessionAddress, target: str
) -> None:
    """Change only a linked Session's answer destination, in the caller's transaction."""
    if target not in {"parent", "user"}:
        raise ChatSessionError("Sub-Agent reply target must be parent or user")

    def update(metadata: JsonObject) -> None:
        if isinstance(metadata.get(SUBAGENT_PARENT_META_KEY), dict):
            metadata[SUBAGENT_REPLY_TARGET_META_KEY] = target

    mutate_metadata(connection, address, update)


def ensure_metadata(
    connection: sqlite3.Connection,
    address: SessionAddress,
    mutation: Callable[[JsonObject], None],
    *,
    create_missing: bool,
    working_project_id: str | None = None,
) -> tuple[JsonObject, JsonObject]:
    """Optionally create the live Session, then apply one metadata mutation.

    A Session this creates works in *working_project_id* (see :func:`ensure_live`).
    """
    if create_missing:
        ensure_live(connection, address, working_project_id)
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
        connection.execute(
            "INSERT OR IGNORE INTO session_review_forks (session_key, source_key) "
            "SELECT s.session_key, p.session_key FROM sessions s "
            "JOIN sessions p ON p.session_key = s.fork_parent_key "
            "AND p.project_id = s.project_id AND p.agent_id = s.agent_id "
            f"WHERE s.session_key = ? AND ? IN {_store_values._REFLECTION_RUN_KINDS_SQL}",
            (session_key, run_kind),
        )
        _store_values._refresh_visibility(connection, session_key)
        _store_values._touch_state(connection, session_key)


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
    record how the call ended, and a result without facts completed. An
    Assistant entry of a Run holds the output streamed so far, so it deletes
    that Run's stream draft. An Agent takeover moves the cursor floor past
    itself, so no earlier read cursor continues across it.
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
    reply_target = _store_values.subagent_reply_target(state)
    for message in messages:
        message = _store_codec.storable(replace(message, subagent_reply_target=reply_target))
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
    if run_key is not None and any(
        message.role == "assistant" and not message.programmatic for message in messages
    ):
        connection.execute("DELETE FROM run_stream_drafts WHERE run_key = ?", (run_key,))
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


def _same_scope(source: SessionAddress, target: SessionAddress) -> bool:
    return (source.project_id or None, source.agent_id) == (
        target.project_id or None,
        target.agent_id,
    )


def _carried_working_project(state: sqlite3.Row, target: SessionAddress) -> str | None:
    """Return the working Project column a Session at *target* takes over from *state*.

    A Project target works in its address Project; an Identity target keeps
    working where the source Session worked, in its address Project or its own.
    """
    if target.project_id is not None:
        return None
    return _store_values._working_project_from_state(state)


def move(connection: sqlite3.Connection, source: SessionAddress, target: SessionAddress) -> None:
    """Give one live Session a new address, history and relations unchanged.

    A move into another scope leaves the Agent-bound prompt state behind and
    starts a new prompt-cache lineage. The Session keeps working where it
    worked: moved into an Identity scope it keeps its working Project (a
    Project Session's address Project becomes its own), and moved into a
    Project scope it works in that Project.
    """
    state = _store_values._require_live(connection, source)
    _store_values._reject_owner_managed_mutation(connection, state)
    if _store_values._find_live(connection, target) is not None:
        raise ChatSessionError(f"destination session already exists: {target.session_id}")
    session_key = int(state["session_key"])
    connection.execute(
        "UPDATE sessions SET project_id = ?, agent_id = ?, session_id = ?, "
        "working_project_id = ?, state_revision = state_revision + 1 WHERE session_key = ?",
        (*_store_values._scope(target), _carried_working_project(state, target), session_key),
    )
    if target.project_id is not None:
        # Entering a live Project selects that incarnation, even when an old
        # Working Project association happened to use the same public id.
        connection.execute(
            "DELETE FROM session_working_project_archives WHERE session_key = ?", (session_key,)
        )
    _store_prompts.carry_prompt_state(
        connection, source=state, target_key=session_key, same_scope=_same_scope(source, target)
    )
    # Parent links store the public address, unlike history and fork relations.
    # Move the live children's links in the same transaction as their Parent.
    connection.execute(
        "UPDATE sessions SET subagent_parent_project_id = ?, subagent_parent_agent_id = ?, "
        "subagent_parent_session_id = ?, state_revision = state_revision + 1 "
        "WHERE state = 'live' AND subagent_parent_project_id IS ? "
        "AND subagent_parent_agent_id = ? AND subagent_parent_session_id = ?",
        (
            target.project_id,
            target.agent_id,
            target.session_id,
            source.project_id,
            source.agent_id,
            source.session_id,
        ),
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
    state and the working Project carry over by the scope rules of a move.
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
    for key in (
        *SESSION_FORK_ALWAYS_STRIP_META_KEYS,
        _store_values._FORK_SOURCE_KEY,
        _store_values._WORKING_PROJECT_KEY,
    ):
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
        "fork_point_seq, working_project_id, "
        + ", ".join(_store_values._METADATA_WRITE_COLUMNS)
        + ") VALUES (?, ?, ?, ?, 'live', ?, ?, ?, ?, ?, ?, ?, ?, "
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
            _carried_working_project(state, target),
            *storage.columns,
        ),
    )
    if cursor.lastrowid is None:
        raise SessionStoreCorruptError("SQLite did not return a forked Session key")
    fork_key = int(cursor.lastrowid)
    if target.project_id is None:
        connection.execute(
            "INSERT INTO session_working_project_archives (session_key, archive_id) "
            "SELECT ?, archive_id FROM session_working_project_archives WHERE session_key = ?",
            (fork_key, source_key),
        )
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


def _detach_session(connection: sqlite3.Connection, session_key: int) -> None:
    """Prepare deleting one Session: descendants stop depending on it.

    Every descendant receives its own copy of what it inherits from the
    Session, and the Session's search rows go before its entries do. Search
    membership that only this Session's lineage kept alive is recomputed.
    Deleting the ``sessions`` row afterwards cascades the rest.
    """
    # Search rows are forgotten while their membership is still readable.
    _store_fts.fts_forget(
        connection, "SELECT entry_key FROM entries WHERE session_key = ?", (session_key,)
    )
    inherited = tuple(
        view_range
        for view_range in _store_lineage.view_ranges(connection, session_key)
        if view_range.source_key != session_key
    )
    candidates: list[int] = []
    if inherited:
        sql, params = _store_lineage.superseded_candidates(inherited)
        candidates = [int(row[0]) for row in connection.execute(sql, params)]
        _store_fts.fts_forget_keys(connection, candidates)
    for segment in connection.execute(
        "SELECT session_key, from_seq, upto_seq, as_of_seq FROM session_lineage "
        "WHERE ancestor_key = ? ORDER BY session_key, from_seq",
        (session_key,),
    ).fetchall():
        descendant_key, from_seq, upto_seq, as_of_seq = (int(value) for value in segment)
        copies = _store_copies.copy_entries(
            connection,
            target_key=descendant_key,
            view_range=_store_lineage.ViewRange(session_key, from_seq, upto_seq, as_of_seq),
        )
        connection.execute(
            "DELETE FROM session_lineage WHERE session_key = ? AND from_seq = ?",
            (descendant_key, from_seq),
        )
        _store_fts.fts_index_keys(connection, copies)
        connection.execute(
            "UPDATE sessions SET history_revision = history_revision + 1, "
            "state_revision = state_revision + 1 WHERE session_key = ?",
            (descendant_key,),
        )
    connection.execute("DELETE FROM session_lineage WHERE session_key = ?", (session_key,))
    _store_fts.fts_index_keys(connection, candidates)


def delete_session(connection: sqlite3.Connection, session_key: int) -> None:
    """Delete one Session generation with everything it owns.

    Forks that inherit from it first receive their own copies, then deleting
    the row cascades, and prompt blobs no other Session pins go with it.
    """
    blob_keys = _store_prompts.session_blob_keys(connection, session_key)
    _detach_session(connection, session_key)
    connection.execute("DELETE FROM sessions WHERE session_key = ?", (session_key,))
    _store_prompts.delete_unreferenced_blobs(connection, blob_keys)


def review_forks(
    connection: sqlite3.Connection, session_key: int, *, idle: bool = False, recursive: bool = True
) -> list[sqlite3.Row]:
    """The live review forks that follow one Session, oldest first.

    A review fork of a review fork follows too. Rows carry ``session_key`` and
    the address columns. With ``idle``, a fork with a running Run is left out
    with what follows it: archive and delete leave a review in flight live and
    whole, and only an admission guard that missed it lets one through.
    """
    running = (
        " AND NOT EXISTS (SELECT 1 FROM runs AS r "
        "WHERE r.session_key = f.session_key AND r.status = 'running')"
        if idle
        else ""
    )
    direct = "" if recursive else " AND p.session_key = ?"
    return list(
        connection.execute(
            "WITH RECURSIVE followers (session_key) AS (SELECT ? UNION "
            "SELECT f.session_key FROM followers AS t "
            "JOIN sessions AS p ON p.session_key = t.session_key "
            f"JOIN sessions AS f ON {_store_values._review_fork_of('f', 'p')} "
            f"WHERE f.state = 'live'{running}{direct}) "
            "SELECT s.session_key, s.project_id, s.agent_id, s.session_id "
            "FROM followers AS t JOIN sessions AS s ON s.session_key = t.session_key "
            "WHERE s.session_key <> ? ORDER BY s.session_key",
            (session_key, session_key) if recursive else (session_key, session_key, session_key),
        )
    )


def review_fork_addresses(
    connection: sqlite3.Connection, address: SessionAddress, *, recursive: bool = True
) -> tuple[SessionAddress, ...]:
    """The addresses of one live Session's live review forks, oldest first."""
    state = _store_values._require_live(connection, address)
    return tuple(
        _store_values._address(row)
        for row in review_forks(connection, int(state["session_key"]), recursive=recursive)
    )


def discard_unused(
    connection: sqlite3.Connection, address: SessionAddress, generation_id: str
) -> bool:
    """Atomically compensate a creation, never a replacement or a used Session."""
    state = _store_values._find_live(connection, address)
    if state is None or state["generation_id"] != generation_id or state["next_seq"] != 0:
        return False
    session_key = int(state["session_key"])
    if (
        connection.execute(
            "SELECT 1 FROM runs WHERE session_key = ? LIMIT 1", (session_key,)
        ).fetchone()
        is not None
    ):
        return False
    _store_values._reject_owner_managed_mutation(connection, state)
    delete_session(connection, session_key)
    return True


def delete(connection: sqlite3.Connection, address: SessionAddress) -> None:
    """Delete one live Session after its idle live review forks, newest first."""
    state = _store_values._require_live(connection, address)
    _store_values._reject_owner_managed_mutation(connection, state)
    session_key = int(state["session_key"])
    for row in reversed(review_forks(connection, session_key, idle=True)):
        delete_session(connection, int(row["session_key"]))
    delete_session(connection, session_key)


# -- Identity Agent scope changes ----------------------------------------------

_GLOBAL_AGENT_SCOPE = "s.project_id = '' AND s.agent_id = ? AND s.state = 'live'"


def _stays_archived_with_its_owner(alias: str) -> str:
    """SQL: the archived Session ``alias`` belongs to an owner that restores or deletes it.

    An archived Agent of the same id holds it in its own entry, or an Extension
    manages it.
    """
    return (
        "(EXISTS (SELECT 1 FROM archive_entry_sessions AS held "
        "JOIN archive_entries AS holder ON holder.entry_key = held.entry_key "
        f"WHERE held.session_key = {alias}.session_key AND holder.kind = '{ARCHIVE_KIND_AGENT}' "
        f"AND holder.subject_id = {alias}.agent_id) "
        "OR EXISTS (SELECT 1 FROM temporary_session_bindings AS managed "
        f"WHERE managed.session_key = {alias}.session_key))"
    )


def archived_identity_session(alias: str) -> str:
    """SQL: the Session ``alias`` is archived and follows a rename of its Identity Agent.

    The next parameter names the Agent. Every archived global Session of it
    follows, so its archive entry restores it to the renamed Agent, except one
    an archived Agent of the same id holds in its own entry, which that entry
    restores with that Agent, and one an Extension manages, which only the
    Extension resumes or deletes.
    """
    return (
        f"{alias}.project_id = '' AND {alias}.agent_id = ? AND {alias}.state = 'archived' "
        f"AND NOT {_stays_archived_with_its_owner(alias)}"
    )


def _renamed_session(alias: str) -> str:
    """SQL: the Session ``alias`` moves with a rename of the Identity Agent the next parameter names.

    Its live global Sessions move, and its archived ones that follow it
    (:func:`archived_identity_session`).
    """
    return (
        f"{alias}.project_id = '' AND {alias}.agent_id = ? AND ({alias}.state = 'live' "
        f"OR ({alias}.state = 'archived' AND NOT {_stays_archived_with_its_owner(alias)}))"
    )


def retarget_identity_agent(
    connection: sqlite3.Connection, old_agent_id: str, new_agent_id: str
) -> int:
    """Rename every global Session address of one Identity Agent, live and archived.

    Archived Sessions follow (:func:`archived_identity_session`), and so does the
    scope of every ``session`` archive entry of the Agent, so a restore brings
    them back to the renamed Agent. Archived generations may share an address,
    so only live addresses can collide. Returns how many archived Sessions and
    archive entries changed.
    """
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
    archived = connection.execute(
        "UPDATE sessions SET agent_id = ?, state_revision = state_revision + 1 "
        "WHERE session_key IN (SELECT s.session_key FROM sessions AS s "
        f"WHERE {archived_identity_session('s')})",
        (new_agent_id, old_agent_id),
    ).rowcount
    entries = connection.execute(
        "UPDATE archive_entries SET agent_id = ? "
        "WHERE kind = ? AND project_id = '' AND agent_id = ?",
        (new_agent_id, ARCHIVE_KIND_SESSION, old_agent_id),
    ).rowcount
    # Package owners follow an Identity Agent rename, including use in other
    # Agents' Sessions. This changes old entries rather than appending new ones,
    # so invalidate existing read cursors for their own and inherited views.
    connection.execute(
        "WITH affected AS (SELECT DISTINCT session_key FROM entries "
        "WHERE skill_package_source = 'agent' AND skill_package_location = ?) "
        "UPDATE sessions SET history_revision = history_revision + 1, "
        "cursor_floor_revision = history_revision + 1, state_revision = state_revision + 1 "
        "WHERE session_key IN (SELECT session_key FROM affected UNION "
        "SELECT l.session_key FROM session_lineage AS l JOIN affected AS a "
        "ON l.ancestor_key = a.session_key)",
        (old_agent_id,),
    )
    connection.execute(
        "UPDATE entries SET skill_package_location = ? "
        "WHERE skill_package_source = 'agent' AND skill_package_location = ?",
        (new_agent_id, old_agent_id),
    )
    return int(archived) + int(entries)


def retarget_metadata_value(
    connection: sqlite3.Connection,
    agent_id: str,
    key: str,
    old_value: str,
    new_value: str,
    *,
    matching: Mapping[str, str] | None = None,
) -> int:
    """Give one open metadata value of an Identity Agent's global Sessions a new value.

    Live and archived Sessions change, so an archived one keeps the value through
    a restore, but only those whose ``key`` still holds ``old_value`` and whose
    ``matching`` keys hold the given values; returns their count.
    """
    conditions = {key: old_value, **(matching or {})}
    for name in conditions:
        if not name.isidentifier() or _store_values._is_reserved_prompt_key(name):
            raise ChatSessionError(f"Session metadata {name} cannot be retargeted")
    cursor = connection.execute(
        "UPDATE sessions SET metadata_json = json_set(metadata_json, ?, ?), "
        "state_revision = state_revision + 1 WHERE project_id = '' AND agent_id = ? "
        "AND state IN ('live', 'archived')"
        + "".join(" AND json_extract(metadata_json, ?) = ?" for _ in conditions),
        (
            f"$.{key}",
            new_value,
            agent_id,
            *(part for name, value in conditions.items() for part in (f"$.{name}", value)),
        ),
    )
    return int(cursor.rowcount)


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
    """Point Sub-Agent parent links at the renamed Identity Agent's moved Sessions.

    A link of a live or archived child follows its parent Session's address: it
    moves only while that Session, live or archived, is under ``new_agent_id``
    the way :func:`retarget_identity_agent` moves Sessions there, and a link
    that recorded its parent's generation moves only with that generation. Links
    to a parent that stayed behind, such as a Session an archived Agent of the
    old id holds, or that no longer exists keep their Agent id.
    """
    rows = connection.execute(
        f"SELECT {_store_values._SESSION_STATE_COLUMNS} FROM sessions AS s "
        "WHERE s.subagent_parent_project_id IS NULL "
        "AND s.subagent_parent_agent_id = ? AND EXISTS (SELECT 1 FROM sessions AS parent "
        f"WHERE {_renamed_session('parent')} "
        "AND parent.session_id = s.subagent_parent_session_id "
        "AND COALESCE(s.subagent_parent_generation_id, '') IN ('', parent.generation_id)) "
        "ORDER BY s.session_key",
        (old_agent_id, new_agent_id),
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
