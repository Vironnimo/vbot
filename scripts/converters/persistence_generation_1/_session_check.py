"""Session-level smoke check of a staged ``sessions.db`` against its source.

Before Generation 1 a Session's current history was every record whose
``active`` flag was set: Messages, Tool results, compaction checkpoints and Run
terminals, in seq order. For every Session generation, the check compares the
ids of that history with the ids of the staged Session's current view, read
the way the Generation 1 store reads it (own entries plus inherited lineage).

A difference is acceptable only when the Session area reported a drop or an
approximation for a Session the view reads; any other difference, and any
Session that appears or disappears, fails the check. The longest live Session
and one live fork that shares its source's history are also loaded through the
application's Session manager, which decodes every entry of their views.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.database._connections import readonly_sqlite_uri
from core.sessions import ChatSessionManager, SessionAddress, _store_lineage
from core.sessions.store import SessionStore
from scripts.converters.persistence_generation_1._context import (
    ConversionContext,
    ConversionError,
)
from scripts.converters.persistence_generation_1.sessions import (
    AREA,
    DATABASE,
    _is_current,
    _open_source,
    session_label,
)

_LEGACY_SESSIONS_SQL = (
    "SELECT session_key, generation_id, project_id, agent_id, session_id FROM sessions"
)
_LEGACY_HISTORY_SQL = """
SELECT seq, message_id FROM messages WHERE session_key = :session AND active = 1
UNION ALL
SELECT t.result_sequence, t.result_id
FROM tool_calls AS t JOIN messages AS m ON m.message_key = t.message_key
WHERE m.session_key = :session AND t.result_id IS NOT NULL AND t.result_active = 1
UNION ALL
SELECT seq, message_id FROM compaction_checkpoints WHERE session_key = :session AND active = 1
UNION ALL
SELECT terminal_sequence, terminal_id FROM runs
WHERE session_key = :session AND terminal_sequence IS NOT NULL AND terminal_active = 1
ORDER BY 1
"""
_STAGED_SESSIONS_SQL = (
    "SELECT session_key, generation_id, project_id, agent_id, session_id, state FROM sessions"
)
# How many differing Sessions the report names.
_REPORTED_DIFFERENCES = 20


@dataclass
class SessionCheck:
    """What the check compared; ``explained`` lists the accepted differences."""

    sessions: int = 0
    entries: int = 0
    explained: list[str] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions_compared": self.sessions,
            "view_entries_compared": self.entries,
            "explained_differences": len(self.explained),
            "explained_examples": self.explained[:_REPORTED_DIFFERENCES],
            "loaded_through_the_application": self.loaded,
        }


def check_sessions(context: ConversionContext) -> SessionCheck | None:
    """Compare every staged Session's current view with its source; ``None`` without one."""
    source_path = context.source_path(DATABASE)
    staged_path = context.staging / DATABASE
    if not source_path.is_file() or not staged_path.is_file():
        return None
    with _open_source(source_path) as source:
        if _is_current(source_path, source):
            return None
        legacy = _legacy_histories(source)
    with closing(sqlite3.connect(readonly_sqlite_uri(staged_path), uri=True)) as staged:
        staged.row_factory = sqlite3.Row
        check, samples = _compare(context, legacy, staged)
    _load_samples(context, staged_path, samples, check)
    return check


def _legacy_histories(source: sqlite3.Connection) -> dict[str, tuple[str, list[str]]]:
    """Per generation id: the Session's report label and its active record ids."""
    histories: dict[str, tuple[str, list[str]]] = {}
    for row in source.execute(_LEGACY_SESSIONS_SQL).fetchall():
        ids = [
            str(record[1])
            for record in source.execute(_LEGACY_HISTORY_SQL, {"session": row["session_key"]})
        ]
        histories[str(row["generation_id"])] = (session_label(row), ids)
    return histories


@dataclass(frozen=True)
class _Sample:
    address: SessionAddress
    label: str
    ids: list[str]


def _compare(
    context: ConversionContext,
    legacy: dict[str, tuple[str, list[str]]],
    staged: sqlite3.Connection,
) -> tuple[SessionCheck, list[_Sample]]:
    reported = {item.item for item in context.report.skipped if item.area == AREA}
    rows = {str(row["generation_id"]): row for row in staged.execute(_STAGED_SESSIONS_SQL)}
    labels = {int(row["session_key"]): session_label(row) for row in rows.values()}
    check = SessionCheck()
    failures: list[str] = []
    longest: _Sample | None = None
    fork: _Sample | None = None
    for generation_id in sorted(set(legacy) - set(rows)):
        label = legacy[generation_id][0]
        (check.explained if label in reported else failures).append(f"{label}: not converted")
    for generation_id, row in sorted(rows.items()):
        label = labels[int(row["session_key"])]
        if generation_id not in legacy:
            failures.append(f"{label}: not in the source")
            continue
        ranges = _store_lineage.view_ranges(staged, int(row["session_key"]))
        ids = [
            str(entry["entry_id"])
            for entry in _store_lineage.ordered_rows(staged, ranges, columns="e.entry_id")
        ]
        check.sessions += 1
        check.entries += len(ids)
        expected = legacy[generation_id][1]
        if ids != expected:
            readers = {labels.get(view_range.source_key, "") for view_range in ranges}
            difference = f"{label}: {len(expected)} records before, {len(ids)} entries after"
            (check.explained if readers & reported else failures).append(difference)
            continue
        if row["state"] != "live":
            continue
        sample = _Sample(_address(row), label, ids)
        if longest is None or len(ids) > len(longest.ids):
            longest = sample
        shares = any(view_range.source_key != row["session_key"] for view_range in ranges)
        if shares and (fork is None or len(ids) > len(fork.ids)):
            fork = sample
    if failures:
        shown = "; ".join(failures[:_REPORTED_DIFFERENCES])
        raise ConversionError(
            f"{len(failures)} converted Sessions differ from their source history: {shown}"
        )
    samples = {sample.label: sample for sample in (longest, fork) if sample is not None}
    return check, list(samples.values())


def _address(row: sqlite3.Row) -> SessionAddress:
    return SessionAddress(
        project_id=row["project_id"] or None,
        agent_id=str(row["agent_id"]),
        session_id=str(row["session_id"]),
    )


def _load_samples(
    context: ConversionContext, staged_path: Path, samples: list[_Sample], check: SessionCheck
) -> None:
    """Load the sampled Sessions through the application's Session manager."""
    if not samples:
        return
    store = SessionStore(staged_path, _offline=True)
    manager = ChatSessionManager(context.staging, store=store)
    try:
        for sample in samples:
            messages = manager.get(sample.address).load_active()
            if [message.id for message in messages] != sample.ids:
                raise ConversionError(
                    f"{sample.label}: the application loads another history than the store view"
                )
            check.loaded.append(f"{sample.label}: {len(messages)} entries")
    finally:
        manager.close()
        store.close()
