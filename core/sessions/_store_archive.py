"""Archive entry ledger statements in a supplied transaction.

Every statement that touches ``archive_entries``, ``archive_entry_sessions`` or
``archive_entry_trees`` lives here. State transitions are compare-and-set: each
names the state it leaves, so two operations on one entry can never both pass.
An entry's row, its Session membership and the archived state of its Sessions
change in one transaction.
"""
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from core.chat.errors import ChatSessionError
from core.sessions import _store_mutations, _store_values
from core.sessions._archive_types import (
    ARCHIVE_KIND_OWNER_GROUP,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_KIND_SESSION,
    ARCHIVE_ORIGIN_OPERATION,
    ARCHIVE_ORIGIN_RECOVERED,
    ARCHIVE_STATE_ARCHIVED,
    ARCHIVE_STATE_ARCHIVING,
    ARCHIVE_STATE_PURGING,
    ARCHIVE_STATE_RESTORED,
    ARCHIVE_STATE_RESTORING,
    ArchiveEntry,
    ArchiveEntryCursor,
    ArchiveEntryFilter,
    ArchiveEntryPage,
    ArchiveEntryRef,
    ArchiveMember,
    ArchiveScope,
    ArchiveTree,
)
from core.sessions._types import SessionAddress
from core.sessions.errors import (
    ArchiveAddressTakenError,
    ArchiveEntryBusyError,
    ArchiveEntryNotFoundError,
    ArchiveMembersManagedError,
    SessionStoreCorruptError,
)
from core.utils.ids import new_id
from core.utils.timestamps import utc_now_timestamp

# Metadata facade keys a restore under a new id drops: the routing members of
# ``SESSION_FORK_ALWAYS_STRIP_META_KEYS``. A copy under a new id must not claim
# the Channel conversation the original address still routes.
_CHANNEL_ROUTING_KEYS = ("source_channel_id", "platform", "platform_conv_id", "last_reply_target")

# Whether the member ``s`` of the entry row ``m`` is a review fork of another member:
# it followed that member into the entry and keeps its own Session id on restore.
_MEMBER_REVIEW_FORK = (
    "EXISTS (SELECT 1 FROM sessions AS p JOIN archive_entry_sessions AS pm "
    "ON pm.session_key = p.session_key AND pm.entry_key = m.entry_key "
    f"WHERE {_store_values._review_fork_of('s', 'p')})"
)
_ENTRY_COLUMNS = (
    "e.entry_key, e.entry_id, e.kind, e.state, e.subject_id, e.project_id, e.agent_id, "
    "e.owner_name, e.archived_at, e.retention_start, e.origin, e.cleanup_pending, e.facts_json, "
    "(SELECT COUNT(*) FROM archive_entry_sessions AS m WHERE m.entry_key = e.entry_key) "
    "AS session_count, "
    "(SELECT COUNT(*) FROM archive_entry_sessions AS m "
    "JOIN sessions AS s ON s.session_key = m.session_key "
    f"WHERE m.entry_key = e.entry_key AND {_MEMBER_REVIEW_FORK}) AS review_fork_count"
)

_NoTrees: Callable[[str], Sequence[ArchiveTree]] = lambda _entry_id: ()  # noqa: E731


# -- Entry rows ----------------------------------------------------------------


def _facts_json(facts: Mapping[str, Any]) -> str:
    return _store_values._json_object(dict(facts), "archive entry facts")


def _facts(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(str(row["facts_json"]))
    except json.JSONDecodeError as exc:
        raise SessionStoreCorruptError(f"invalid facts of archive entry {row['entry_id']}") from exc
    if not isinstance(value, dict):
        raise SessionStoreCorruptError(f"invalid facts of archive entry {row['entry_id']}")
    return value


def insert_entry(
    connection: sqlite3.Connection,
    *,
    kind: str,
    state: str,
    subject_id: str,
    archived_at: str,
    retention_start: str,
    project_id: str = "",
    agent_id: str = "",
    owner_name: str | None = None,
    origin: str = ARCHIVE_ORIGIN_OPERATION,
    cleanup_pending: bool = False,
    facts: Mapping[str, Any] | None = None,
    trees: Callable[[str], Sequence[ArchiveTree]] = _NoTrees,
    entry_id: str | None = None,
) -> ArchiveEntryRef:
    """Insert one entry under a freshly claimed ``arc_`` id, with its payload trees.

    ``trees`` receives the claimed id, because new payloads live under
    ``archive/entries/<entry_id>/``. A tree path another entry already records
    raises: no two entries ever share a payload. A given ``entry_id`` is kept
    when it is free.
    """
    facts_json = _facts_json(facts or {})
    claimed: list[int] = []

    def claim(candidate: str) -> bool:
        cursor = connection.execute(
            "INSERT INTO archive_entries (entry_id, kind, state, subject_id, project_id, agent_id, "
            "owner_name, archived_at, retention_start, origin, cleanup_pending, facts_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (entry_id) DO NOTHING",
            (
                candidate,
                kind,
                state,
                subject_id,
                project_id,
                agent_id,
                owner_name,
                archived_at,
                retention_start,
                origin,
                int(cleanup_pending),
                facts_json,
            ),
        )
        if cursor.rowcount != 1 or cursor.lastrowid is None:
            return False
        claimed.append(int(cursor.lastrowid))
        return True

    if entry_id is None or not claim(entry_id):
        entry_id = new_id("arc", claim=claim)
    entry_key = claimed[-1]
    for tree in trees(entry_id):
        connection.execute(
            "INSERT INTO archive_entry_trees (path, entry_key, role, source_path) VALUES (?, ?, ?, ?)",
            (tree.path, entry_key, tree.role, tree.source_path),
        )
    return ArchiveEntryRef(entry_key, entry_id)


def _entries(
    connection: sqlite3.Connection, rows: Sequence[sqlite3.Row]
) -> tuple[ArchiveEntry, ...]:
    """Decode entry rows, reading the trees of all of them in one indexed statement."""
    if not rows:
        return ()
    keys = [int(row["entry_key"]) for row in rows]
    trees: dict[int, list[ArchiveTree]] = {key: [] for key in keys}
    for tree in connection.execute(
        "SELECT t.entry_key, t.path, t.role, t.source_path FROM archive_entry_trees AS t "
        "WHERE t.entry_key IN (SELECT value FROM json_each(?)) ORDER BY t.entry_key, t.path",
        (_store_values._key_list(keys),),
    ):
        trees[int(tree["entry_key"])].append(
            ArchiveTree(str(tree["path"]), str(tree["role"]), tree["source_path"])
        )
    return tuple(
        ArchiveEntry(
            entry_key=int(row["entry_key"]),
            entry_id=str(row["entry_id"]),
            kind=str(row["kind"]),
            state=str(row["state"]),
            subject_id=str(row["subject_id"]),
            project_id=str(row["project_id"]),
            agent_id=str(row["agent_id"]),
            owner_name=None if row["owner_name"] is None else str(row["owner_name"]),
            archived_at=str(row["archived_at"]),
            retention_start=str(row["retention_start"]),
            origin=str(row["origin"]),
            cleanup_pending=bool(row["cleanup_pending"]),
            facts=_facts(row),
            trees=tuple(trees[int(row["entry_key"])]),
            session_count=int(row["session_count"]),
            review_fork_count=int(row["review_fork_count"]),
        )
        for row in rows
    )


def entry_by_id(connection: sqlite3.Connection, entry_id: str) -> ArchiveEntry | None:
    row = connection.execute(
        f"SELECT {_ENTRY_COLUMNS} FROM archive_entries AS e WHERE e.entry_id = ?", (entry_id,)
    ).fetchone()
    return None if row is None else _entries(connection, [row])[0]


def entry_by_key(connection: sqlite3.Connection, entry_key: int) -> ArchiveEntry | None:
    row = connection.execute(
        f"SELECT {_ENTRY_COLUMNS} FROM archive_entries AS e WHERE e.entry_key = ?", (entry_key,)
    ).fetchone()
    return None if row is None else _entries(connection, [row])[0]


def _require_state(connection: sqlite3.Connection, entry_key: int, state: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT entry_key, entry_id, kind, state, facts_json FROM archive_entries WHERE entry_key = ?",
        (entry_key,),
    ).fetchone()
    if row is None:
        raise ArchiveEntryNotFoundError(f"#{entry_key}")
    if str(row["state"]) != state:
        raise ArchiveEntryBusyError(str(row["entry_id"]), str(row["state"]))
    return cast(sqlite3.Row, row)


def _merged_facts(row: sqlite3.Row, patch: Mapping[str, Any] | None) -> str:
    facts = _facts(row)
    if patch:
        facts.update(patch)
    return _facts_json(facts)


def update_facts(connection: sqlite3.Connection, entry_key: int, patch: Mapping[str, Any]) -> None:
    """Merge ``patch`` into an entry's facts, keeping every other key."""
    row = connection.execute(
        "SELECT entry_id, facts_json FROM archive_entries WHERE entry_key = ?", (entry_key,)
    ).fetchone()
    if row is None:
        raise ArchiveEntryNotFoundError(f"#{entry_key}")
    connection.execute(
        "UPDATE archive_entries SET facts_json = ? WHERE entry_key = ?",
        (_merged_facts(row, patch), entry_key),
    )


# -- Archive ---------------------------------------------------------------------


def begin(
    connection: sqlite3.Connection,
    kind: str,
    *,
    subject_id: str,
    project_id: str = "",
    agent_id: str = "",
    facts: Mapping[str, Any] | None = None,
    trees: Callable[[str], Sequence[ArchiveTree]] = _NoTrees,
) -> ArchiveEntryRef:
    """Record an archive about to move files: state ``archiving``, Sessions still live."""
    now = utc_now_timestamp()
    return insert_entry(
        connection,
        kind=kind,
        state=ARCHIVE_STATE_ARCHIVING,
        subject_id=subject_id,
        project_id=project_id,
        agent_id=agent_id,
        archived_at=now,
        retention_start=now,
        facts=facts,
        trees=trees,
    )


def _scope_predicate(scope: ArchiveScope) -> tuple[str, tuple[Any, ...]]:
    if scope.project_id is None and scope.agent_id:
        return "s.project_id = '' AND s.agent_id = ? AND s.state = 'live'", (scope.agent_id,)
    if scope.project_id and scope.agent_id is None:
        return "s.project_id = ? AND s.state = 'live'", (scope.project_id,)
    raise ValueError("an archive scope names an Identity Agent or a Project")


def _archive_members(connection: sqlite3.Connection, entry_key: int, now: str) -> None:
    connection.execute(
        "UPDATE sessions SET state = 'archived', archived_at = ?, state_revision = state_revision + 1 "
        "WHERE state = 'live' AND session_key IN "
        "(SELECT session_key FROM archive_entry_sessions WHERE entry_key = ?)",
        (now, entry_key),
    )


def _scope_members(
    connection: sqlite3.Connection, scope: ArchiveScope, *, idle: bool = False
) -> list[sqlite3.Row]:
    """Live scope members and their review followers, in one canonical read."""
    where, params = _scope_predicate(scope)
    running = (
        " AND NOT EXISTS (SELECT 1 FROM runs AS r "
        "WHERE r.session_key = f.session_key AND r.status = 'running')"
        if idle
        else ""
    )
    return list(
        connection.execute(
            "WITH RECURSIVE members (session_key) AS ("
            f"SELECT s.session_key FROM sessions AS s WHERE {where} UNION "
            "SELECT f.session_key FROM members AS m "
            "JOIN sessions AS p ON p.session_key = m.session_key "
            f"JOIN sessions AS f ON {_store_values._review_fork_of('f', 'p')} "
            f"WHERE f.state = 'live'{running}) "
            "SELECT s.session_key, s.project_id, s.agent_id, s.session_id "
            "FROM members AS m JOIN sessions AS s ON s.session_key = m.session_key "
            "ORDER BY s.session_key",
            params,
        )
    )


def scope_addresses(
    connection: sqlite3.Connection, scope: ArchiveScope
) -> tuple[SessionAddress, ...]:
    """Sessions whose work must be guarded before archiving an Agent or Project."""
    return tuple(_store_values._address(row) for row in _scope_members(connection, scope))


def commit_scope(
    connection: sqlite3.Connection,
    entry_key: int,
    scope: ArchiveScope,
    *,
    facts: Mapping[str, Any] | None = None,
    cleanup_pending: bool = False,
) -> int:
    """Archive every live Session of ``scope`` into an ``archiving`` entry; return how many.

    The membership rows, the archived Sessions and the entry's move to
    ``archived`` commit together. Owner-managed Sessions in the scope refuse the
    whole commit. A Project also freezes its Identity Sessions' Working Project
    associations, including archived Sessions but not an older incarnation's users.
    """
    row = _require_state(connection, entry_key, ARCHIVE_STATE_ARCHIVING)
    keys = [int(member["session_key"]) for member in _scope_members(connection, scope, idle=True)]
    encoded_keys = _store_values._key_list(keys)
    _store_values._reject_owner_managed_scope_mutation(
        connection, "s.session_key IN (SELECT value FROM json_each(?))", (encoded_keys,)
    )
    now = utc_now_timestamp()
    count = connection.execute(
        "INSERT INTO archive_entry_sessions (session_key, entry_key) "
        "SELECT value, ? FROM json_each(?)",
        (entry_key, encoded_keys),
    ).rowcount
    if scope.project_id is not None:
        connection.execute(
            "INSERT OR IGNORE INTO session_working_project_archives (session_key, archive_id) "
            "SELECT session_key, ? FROM sessions WHERE project_id = '' AND working_project_id = ?",
            (row["entry_id"], scope.project_id),
        )
        facts = {**(facts or {}), "working_project_provenance": True}
    _archive_members(connection, entry_key, now)
    connection.execute(
        "UPDATE archive_entries SET state = ?, archived_at = ?, retention_start = ?, "
        "cleanup_pending = ?, facts_json = ? WHERE entry_key = ?",
        (
            ARCHIVE_STATE_ARCHIVED,
            now,
            now,
            int(cleanup_pending),
            _merged_facts(row, facts),
            entry_key,
        ),
    )
    return int(count)


def abandon(connection: sqlite3.Connection, entry_key: int) -> None:
    """Delete an entry whose archive was rolled back; its Sessions never left ``live``."""
    connection.execute(
        "DELETE FROM archive_entries WHERE entry_key = ? AND state = ?",
        (entry_key, ARCHIVE_STATE_ARCHIVING),
    )


def finish_cleanup(connection: sqlite3.Connection, entry_key: int) -> None:
    connection.execute(
        "UPDATE archive_entries SET cleanup_pending = 0 WHERE entry_key = ?", (entry_key,)
    )


def archive_session(
    connection: sqlite3.Connection, address: SessionAddress, facts: Mapping[str, Any] | None
) -> ArchiveEntryRef:
    """Archive one live Session generation as its own ``session`` entry.

    Its idle live review forks join the entry (``_store_mutations.review_forks``),
    so they leave, return and are purged with it.
    """
    state = _store_values._require_live(connection, address)
    _store_values._reject_owner_managed_mutation(connection, state)
    now = utc_now_timestamp()
    project_id, agent_id, session_id = _store_values._scope(address)
    ref = insert_entry(
        connection,
        kind=ARCHIVE_KIND_SESSION,
        state=ARCHIVE_STATE_ARCHIVED,
        subject_id=session_id,
        project_id=project_id,
        agent_id=agent_id,
        archived_at=now,
        retention_start=now,
        facts=facts,
    )
    session_key = int(state["session_key"])
    keys = [
        session_key,
        *(
            int(row["session_key"])
            for row in _store_mutations.review_forks(connection, session_key, idle=True)
        ),
    ]
    connection.execute(
        "INSERT INTO archive_entry_sessions (session_key, entry_key) "
        "SELECT value, ? FROM json_each(?)",
        (ref.entry_key, _store_values._key_list(keys)),
    )
    _archive_members(connection, ref.entry_key, now)
    return ref


def archive_owner_group(
    connection: sqlite3.Connection, *, owner_name: str, group_id: str, reason: str
) -> tuple[int, ArchiveEntryRef | None]:
    """Archive one owner group's live bound Sessions as an ``owner_group`` entry.

    Returns how many Sessions it archived and the entry, or ``(0, None)`` when the
    group has no live Session. Bindings, titles, receipts and Run records stay.
    """
    keys = [
        int(row[0])
        for row in connection.execute(
            "SELECT s.session_key FROM temporary_session_bindings AS b "
            "JOIN sessions AS s ON s.session_key = b.session_key "
            "WHERE b.owner_name = ? AND b.group_id = ? AND s.state = 'live' ORDER BY s.session_key",
            (owner_name, group_id),
        )
    ]
    if not keys:
        return 0, None
    now = utc_now_timestamp()
    ref = insert_entry(
        connection,
        kind=ARCHIVE_KIND_OWNER_GROUP,
        state=ARCHIVE_STATE_ARCHIVED,
        subject_id=group_id,
        owner_name=owner_name,
        archived_at=now,
        retention_start=now,
        facts={"reason": reason},
    )
    connection.execute(
        "INSERT INTO archive_entry_sessions (session_key, entry_key) "
        "SELECT value, ? FROM json_each(?)",
        (ref.entry_key, _store_values._key_list(keys)),
    )
    _archive_members(connection, ref.entry_key, now)
    return len(keys), ref


def delete_empty_owner_group_entries(
    connection: sqlite3.Connection, *, owner_name: str, group_id: str
) -> int:
    """Delete this group's ``owner_group`` entries that no longer hold a Session."""
    return int(
        connection.execute(
            "DELETE FROM archive_entries WHERE kind = ? AND subject_id = ? AND owner_name = ? "
            "AND NOT EXISTS (SELECT 1 FROM archive_entry_sessions AS m "
            "WHERE m.entry_key = archive_entries.entry_key)",
            (ARCHIVE_KIND_OWNER_GROUP, group_id, owner_name),
        ).rowcount
    )


def adopt_payload(
    connection: sqlite3.Connection,
    entry_id: str,
    kind: str,
    *,
    subject_id: str,
    archived_at: str,
    trees: Sequence[ArchiveTree],
    facts: Mapping[str, Any] | None = None,
) -> ArchiveEntryRef:
    """Record a payload without an entry as a ``recovered`` entry that holds no Session.

    Its retention clock starts now.
    """
    return insert_entry(
        connection,
        kind=kind,
        state=ARCHIVE_STATE_ARCHIVED,
        subject_id=subject_id,
        archived_at=archived_at,
        retention_start=utc_now_timestamp(),
        origin=ARCHIVE_ORIGIN_RECOVERED,
        facts=facts,
        trees=lambda _entry_id: trees,
        entry_id=entry_id,
    )


# -- Restore ---------------------------------------------------------------------


def begin_restore(
    connection: sqlite3.Connection, entry_id: str, plan: Mapping[str, Any]
) -> ArchiveEntry:
    """Claim an ``archived`` entry for a restore and record its plan for recovery."""
    entry = entry_by_id(connection, entry_id)
    if entry is None:
        raise ArchiveEntryNotFoundError(entry_id)
    row = _require_state(connection, entry.entry_key, ARCHIVE_STATE_ARCHIVED)
    connection.execute(
        "UPDATE archive_entries SET state = ?, facts_json = ? WHERE entry_key = ?",
        (
            ARCHIVE_STATE_RESTORING,
            _merged_facts(row, {"restore_plan": dict(plan)}),
            entry.entry_key,
        ),
    )
    restored = entry_by_key(connection, entry.entry_key)
    assert restored is not None
    return restored


def _member_rows(connection: sqlite3.Connection, entry_key: int) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT s.session_key, s.project_id, s.agent_id, s.session_id, "
            f"{_MEMBER_REVIEW_FORK} AS review_fork FROM archive_entry_sessions AS m "
            "JOIN sessions AS s ON s.session_key = m.session_key WHERE m.entry_key = ? "
            "ORDER BY m.session_key",
            (entry_key,),
        )
    )


def _strip_channel_routing(connection: sqlite3.Connection, session_key: int) -> None:
    row = _store_values._metadata_row(connection, session_key)
    metadata = _store_values._session_metadata_from_state(row)
    if not any(key in metadata for key in _CHANNEL_ROUTING_KEYS):
        return
    for key in _CHANNEL_ROUTING_KEYS:
        metadata.pop(key, None)
    storage = _store_values._session_metadata_storage(
        metadata, _store_values._derived_metadata_from_state(row)
    )
    _store_values._write_metadata_storage(connection, session_key, storage)


def _restore_targets(
    connection: sqlite3.Connection,
    entry_key: int,
    *,
    project_id: str | None,
    agent_id: str | None,
    session_id: str | None,
) -> tuple[list[tuple[int, SessionAddress]], tuple[SessionAddress, ...]]:
    """Each member's key with its restored address, and the addresses a live Session holds.

    A new ``session_id`` names the entry's one Session; its review forks keep theirs.
    """
    members = _member_rows(connection, entry_key)
    if session_id is not None and sum(not member["review_fork"] for member in members) != 1:
        raise ChatSessionError(
            "only an entry of one Session can be restored under a new Session id"
        )
    entry = entry_by_key(connection, entry_key)
    assert entry is not None
    targets: list[tuple[int, SessionAddress]] = []
    for member in members:
        # A review kept its learning owner when its source moved. Restoring
        # that source under another name must not move the review's owner.
        original_owner = not member["review_fork"] or (
            str(member["project_id"]) == (entry.project_id or "")
            and (entry.kind == ARCHIVE_KIND_PROJECT or str(member["agent_id"]) == entry.agent_id)
        )
        target = SessionAddress(
            project_id=(str(member["project_id"]) or None)
            if project_id is None or not original_owner
            else project_id or None,
            agent_id=str(member["agent_id"])
            if agent_id is None or not original_owner
            else agent_id,
            session_id=str(member["session_id"])
            if session_id is None or member["review_fork"]
            else session_id,
        )
        targets.append((int(member["session_key"]), target))
    seen: set[SessionAddress] = set()
    taken: list[SessionAddress] = []
    for _key, target in targets:
        if target in seen or _store_values._find_live(connection, target) is not None:
            taken.append(target)
        seen.add(target)
    return targets, tuple(taken)


def taken_addresses(
    connection: sqlite3.Connection,
    entry_key: int,
    *,
    project_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> tuple[SessionAddress, ...]:
    """The addresses a restore with these replacements finds held by a live Session."""
    return _restore_targets(
        connection, entry_key, project_id=project_id, agent_id=agent_id, session_id=session_id
    )[1]


def unproven_parent_link_count(connection: sqlite3.Connection, entry_key: int) -> int:
    """Count children a renamed restore cannot safely reconnect to its members."""
    return int(
        connection.execute(
            "SELECT COUNT(DISTINCT child.session_key) FROM archive_entry_sessions AS m "
            "JOIN sessions AS parent ON parent.session_key = m.session_key "
            "JOIN sessions AS child ON child.subagent_parent_agent_id = parent.agent_id "
            "AND child.subagent_parent_session_id = parent.session_id "
            "AND child.subagent_parent_project_id IS NULLIF(parent.project_id, '') "
            "WHERE m.entry_key = ? AND child.subagent_parent_session_id IS NOT NULL "
            "AND (child.subagent_parent_generation_id IS NULL OR child.subagent_parent_generation_id = '')",
            (entry_key,),
        ).fetchone()[0]
    )


def commit_restore(
    connection: sqlite3.Connection,
    entry_key: int,
    *,
    project_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    strip_channel_keys: bool = False,
) -> tuple[SessionAddress, ...]:
    """Make every member of a ``restoring`` entry live again; return their addresses.

    A given ``project_id``, ``agent_id`` or ``session_id`` replaces that part of
    every member's address (a new ``session_id`` only that of the entry's one
    Session; its review forks keep theirs). A live Session at any resulting
    address refuses the whole restore with :class:`ArchiveAddressTakenError`.
    ``strip_channel_keys`` drops the Channel routing of the members. The
    membership goes and the entry becomes ``restored``. Members an Extension
    manages refuse it with :class:`ArchiveMembersManagedError`. Proven Parent links
    and Working Project associations follow in this same transaction; reused
    public addresses and history timestamps never establish ownership.
    """
    row = _require_state(connection, entry_key, ARCHIVE_STATE_RESTORING)
    owned = connection.execute(
        "SELECT 1 FROM temporary_session_bindings WHERE session_key IN "
        "(SELECT session_key FROM archive_entry_sessions WHERE entry_key = ?) LIMIT 1",
        (entry_key,),
    ).fetchone()
    if owned is not None:
        raise ArchiveMembersManagedError(str(row["entry_id"]))
    targets, taken = _restore_targets(
        connection, entry_key, project_id=project_id, agent_id=agent_id, session_id=session_id
    )
    if taken:
        raise ArchiveAddressTakenError(str(row["entry_id"]), taken)
    for session_key, target in targets:
        original = _store_values._state_by_key(connection, session_key)
        if _store_values._address(original) != target:
            # Address reuse never establishes lineage. Original children follow
            # their exact Parent generation, including children archived elsewhere.
            connection.execute(
                "UPDATE sessions SET subagent_parent_project_id = ?, subagent_parent_agent_id = ?, "
                "subagent_parent_session_id = ?, state_revision = state_revision + 1 "
                "WHERE subagent_parent_generation_id = ?",
                (
                    target.project_id,
                    target.agent_id,
                    target.session_id,
                    original["generation_id"],
                ),
            )
        connection.execute(
            "UPDATE sessions SET state = 'live', archived_at = NULL, project_id = ?, agent_id = ?, "
            "session_id = ?, state_revision = state_revision + 1 WHERE session_key = ?",
            (*_store_values._scope(target), session_key),
        )
        if strip_channel_keys:
            _strip_channel_routing(connection, session_key)
    if row["kind"] == ARCHIVE_KIND_PROJECT:
        if project_id is not None:
            connection.execute(
                "UPDATE sessions SET working_project_id = ?, state_revision = state_revision + 1 "
                "WHERE session_key IN (SELECT session_key FROM session_working_project_archives "
                "WHERE archive_id = ?)",
                (project_id, row["entry_id"]),
            )
        connection.execute(
            "DELETE FROM session_working_project_archives WHERE archive_id = ?", (row["entry_id"],)
        )
    connection.execute("DELETE FROM archive_entry_sessions WHERE entry_key = ?", (entry_key,))
    connection.execute(
        "UPDATE archive_entries SET state = ? WHERE entry_key = ?",
        (ARCHIVE_STATE_RESTORED, entry_key),
    )
    return tuple(target for _key, target in targets)


def abort_restore(connection: sqlite3.Connection, entry_key: int) -> None:
    """Return a ``restoring`` entry to ``archived``; its Sessions never left the archive."""
    row = connection.execute(
        "SELECT entry_id, facts_json FROM archive_entries WHERE entry_key = ? AND state = ?",
        (entry_key, ARCHIVE_STATE_RESTORING),
    ).fetchone()
    if row is None:
        return
    facts = _facts(row)
    facts.pop("restore_plan", None)
    connection.execute(
        "UPDATE archive_entries SET state = ?, facts_json = ? WHERE entry_key = ?",
        (ARCHIVE_STATE_ARCHIVED, _facts_json(facts), entry_key),
    )


def finish_restore(connection: sqlite3.Connection, entry_key: int) -> None:
    """Delete a ``restored`` entry once its follow-up is done."""
    connection.execute(
        "DELETE FROM archive_entries WHERE entry_key = ? AND state = ?",
        (entry_key, ARCHIVE_STATE_RESTORED),
    )


# -- Purge -----------------------------------------------------------------------


def begin_purge(connection: sqlite3.Connection, entry_id: str) -> ArchiveEntry:
    """Claim an ``archived`` entry for deletion; a ``purging`` one is resumed."""
    entry = entry_by_id(connection, entry_id)
    if entry is None:
        raise ArchiveEntryNotFoundError(entry_id)
    if entry.state == ARCHIVE_STATE_PURGING:
        return entry
    _require_state(connection, entry.entry_key, ARCHIVE_STATE_ARCHIVED)
    connection.execute(
        "UPDATE archive_entries SET state = ? WHERE entry_key = ?",
        (ARCHIVE_STATE_PURGING, entry.entry_key),
    )
    claimed = entry_by_key(connection, entry.entry_key)
    assert claimed is not None
    return claimed


def purge_next_session(connection: sqlite3.Connection, entry_key: int) -> bool:
    """Delete the member with the highest key of a ``purging`` entry; ``False`` when none remain.

    Forks always have larger keys than their sources, so a member's fork inside
    the same entry goes first and is never materialized only to be deleted.
    """
    _require_state(connection, entry_key, ARCHIVE_STATE_PURGING)
    row = connection.execute(
        "SELECT session_key FROM archive_entry_sessions WHERE entry_key = ? "
        "ORDER BY session_key DESC LIMIT 1",
        (entry_key,),
    ).fetchone()
    if row is None:
        return False
    _store_mutations.delete_session(connection, int(row[0]))
    return True


def finish_purge(connection: sqlite3.Connection, entry_key: int) -> None:
    """Delete a ``purging`` entry whose Sessions are gone, with its tree records.

    An Extension group's title goes with it once no Session of that group remains.
    """
    row = connection.execute(
        "SELECT entry_id, kind, state, subject_id, owner_name FROM archive_entries WHERE entry_key = ?",
        (entry_key,),
    ).fetchone()
    if row is None:
        return
    if str(row["state"]) != ARCHIVE_STATE_PURGING:
        raise ArchiveEntryBusyError(str(row["entry_id"]), str(row["state"]))
    if connection.execute(
        "SELECT 1 FROM archive_entry_sessions WHERE entry_key = ? LIMIT 1", (entry_key,)
    ).fetchone():
        raise ArchiveEntryBusyError(str(row["entry_id"]), ARCHIVE_STATE_PURGING)
    if str(row["kind"]) == ARCHIVE_KIND_OWNER_GROUP and row["owner_name"] is not None:
        connection.execute(
            "DELETE FROM temporary_group_titles WHERE owner_name = ? AND group_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM temporary_session_bindings "
            "WHERE owner_name = ? AND group_id = ?)",
            (row["owner_name"], row["subject_id"], row["owner_name"], row["subject_id"]),
        )
    connection.execute("DELETE FROM archive_entries WHERE entry_key = ?", (entry_key,))


# -- Reads -----------------------------------------------------------------------


def page(
    connection: sqlite3.Connection,
    filters: ArchiveEntryFilter,
    cursor: ArchiveEntryCursor | None,
    limit: int,
) -> ArchiveEntryPage:
    """One page of entries, newest first, after ``cursor``.

    A Project or Agent filter walks ``archive_entries_by_scope``, every other
    page ``archive_entries_by_archived``.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("invalid archive page limit")
    clauses: list[str] = []
    params: list[Any] = []
    if filters.project_id is not None or filters.agent_id is not None:
        clauses.append("e.project_id = ?")
        params.append(filters.project_id or "")
        if filters.agent_id is not None:
            clauses.append("e.agent_id = ?")
            params.append(filters.agent_id)
    if filters.kind is not None:
        clauses.append("e.kind = ?")
        params.append(filters.kind)
    if cursor is not None:
        clauses.append("(e.archived_at, e.entry_id) < (?, ?)")
        params.extend((cursor.archived_at, cursor.entry_id))
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    rows = connection.execute(
        f"SELECT {_ENTRY_COLUMNS} FROM archive_entries AS e {where}"
        "ORDER BY e.archived_at DESC, e.entry_id DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    entries = _entries(connection, rows[:limit])
    next_cursor = (
        ArchiveEntryCursor(entries[-1].archived_at, entries[-1].entry_id)
        if len(rows) > limit
        else None
    )
    return ArchiveEntryPage(entries, next_cursor)


def members(
    connection: sqlite3.Connection, entry_key: int, limit: int | None
) -> tuple[ArchiveMember, ...]:
    """The first ``limit`` member Sessions of an entry (all for ``None``), in archive order."""
    rows = connection.execute(
        "SELECT s.project_id, s.agent_id, s.session_id, s.generation_id, s.title, s.auto_title, "
        f"s.created_at, s.last_activity_at, {_MEMBER_REVIEW_FORK} AS review_fork "
        "FROM archive_entry_sessions AS m "
        "JOIN sessions AS s ON s.session_key = m.session_key WHERE m.entry_key = ? "
        "ORDER BY m.session_key LIMIT ?",
        (entry_key, -1 if limit is None else limit),
    ).fetchall()
    return tuple(
        ArchiveMember(
            address=_store_values._address(row),
            generation_id=str(row["generation_id"]),
            title=row["title"] or row["auto_title"],
            created_at=str(row["created_at"]),
            last_activity_at=str(row["last_activity_at"]),
            review_fork=bool(row["review_fork"]),
        )
        for row in rows
    )


def holds_one_session(connection: sqlite3.Connection, entry_key: int) -> bool:
    """Whether the entry holds exactly one Session besides its review forks."""
    rows = connection.execute(
        "SELECT 1 FROM archive_entry_sessions AS m "
        "JOIN sessions AS s ON s.session_key = m.session_key "
        f"WHERE m.entry_key = ? AND NOT {_MEMBER_REVIEW_FORK} LIMIT 2",
        (entry_key,),
    ).fetchall()
    return len(rows) == 1


def is_recorded(connection: sqlite3.Connection, directory: str) -> bool:
    """Whether an entry records ``directory`` or a tree inside it, or a tree that holds it."""
    if (
        connection.execute(
            "SELECT 1 FROM archive_entry_trees WHERE path = ? OR (path > ? AND path < ?) LIMIT 1",
            (directory, f"{directory}/", f"{directory}0"),
        ).fetchone()
        is not None
    ):
        return True
    parts = directory.split("/")
    ancestors = ["/".join(parts[:index]) for index in range(1, len(parts))]
    return bool(ancestors) and (
        connection.execute(
            "SELECT 1 FROM archive_entry_trees WHERE path IN (SELECT value FROM json_each(?)) LIMIT 1",
            (_store_values._json_list(ancestors),),
        ).fetchone()
        is not None
    )


def unsettled(connection: sqlite3.Connection) -> tuple[ArchiveEntry, ...]:
    """Entries an interrupted operation left behind, oldest first."""
    rows = connection.execute(
        f"SELECT {_ENTRY_COLUMNS} FROM archive_entries AS e "
        "WHERE e.state <> 'archived' OR e.cleanup_pending = 1 ORDER BY e.state, e.entry_key"
    ).fetchall()
    return _entries(connection, sorted(rows, key=lambda row: int(row["entry_key"])))


def pending_purges(connection: sqlite3.Connection) -> tuple[ArchiveEntry, ...]:
    """Started purges and expired Ephemeral Session entries awaiting their purge."""
    rows = connection.execute(
        f"SELECT {_ENTRY_COLUMNS} FROM archive_entries AS e "
        "WHERE e.state = 'purging' OR (e.state = 'archived' AND e.kind = 'session' "
        "AND json_extract(e.facts_json, '$.ephemeral_expiry') = 1) ORDER BY e.entry_key"
    ).fetchall()
    return _entries(connection, rows)


def due(
    connection: sqlite3.Connection, before: str, after: ArchiveEntry | None, limit: int
) -> tuple[ArchiveEntry, ...]:
    """``archived`` entries whose retention started at or before ``before``, oldest first.

    With ``after``, the walk continues behind that entry.
    """
    clauses = ["e.state = 'archived'", "e.retention_start <= ?"]
    params: list[Any] = [before]
    if after is not None:
        clauses.append("(e.retention_start, e.entry_key) > (?, ?)")
        params.extend((after.retention_start, after.entry_key))
    rows = connection.execute(
        f"SELECT {_ENTRY_COLUMNS} FROM archive_entries AS e WHERE {' AND '.join(clauses)} "
        "ORDER BY e.retention_start, e.entry_key LIMIT ?",
        (*params, limit),
    ).fetchall()
    return _entries(connection, rows)


def identity_agent_entry_ids(connection: sqlite3.Connection, agent_id: str) -> tuple[str, ...]:
    """The entries an Identity Agent rename from or to ``agent_id`` changes, oldest first.

    Those that hold archived Sessions following a rename of the Agent
    (``_store_mutations.archived_identity_session``) and its ``session`` entries.
    """
    rows = connection.execute(
        "SELECT e.entry_id FROM archive_entries AS e "
        "WHERE (e.kind = ? AND e.project_id = '' AND e.agent_id = ?) "
        "OR e.entry_key IN (SELECT m.entry_key FROM archive_entry_sessions AS m "
        "JOIN sessions AS s ON s.session_key = m.session_key "
        f"WHERE {_store_mutations.archived_identity_session('s')}) "
        "ORDER BY e.entry_key",
        (ARCHIVE_KIND_SESSION, agent_id, agent_id),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def newest_entry_id(connection: sqlite3.Connection, kind: str, subject_id: str) -> str | None:
    """The newest entry archiving ``subject_id`` as ``kind``, or ``None``."""
    row = connection.execute(
        "SELECT entry_id FROM archive_entries WHERE kind = ? AND subject_id = ? "
        "ORDER BY archived_at DESC, entry_id DESC LIMIT 1",
        (kind, subject_id),
    ).fetchone()
    return None if row is None else str(row[0])
