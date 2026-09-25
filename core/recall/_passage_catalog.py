"""The Passage catalog both disposable Recall indexes share.

The Passage FTS index and the vector index each hold this catalog in their own
kernel disposable database. It records which Passages every indexed Session's
current view contains, with a freshness stamp ``(generation_id,
history_revision)`` per Session.

A Passage is stored once per Recall scope (project and Agent), identified by
its text and exact boundaries, however many Sessions show it. A fork and its
origin build identical Passages over the history they share, so the shared
history is one set of rows. ``passage_views`` names every Session that shows a
Passage and whether the Session *owns* it: its last Message is one of the
Session's own entries rather than history that precedes its fork point.

Search reports each Passage for one candidate Session, by the rule store search
applies to entries: the owner when it is a candidate, else the newest candidate
that shows the Passage (the largest creation order). A fork and its origin
therefore never both return the Passages they share, and history an origin no
longer shows (it edited it away, or was deleted) is reported for a descendant
that still shows it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from core.database import (
    DatabaseError,
    DatabaseSpec,
    DisposableDatabase,
    projection_failure,
)
from core.recall.canonical import RecallScope
from core.recall.passages import PASSAGE_POLICY_VERSION, Passage, build_session_passages
from core.sessions import (
    ChatSessionManager,
    SessionAddress,
    SessionNotFoundError,
    SessionReadBatch,
)

_T = TypeVar("_T")

SessionVersion = tuple[str, int]

# Stored for the identity/global scope (``project_id is None``): an empty string
# keeps the UNIQUE constraints reliable, because SQLite treats NULLs as distinct.
GLOBAL_SCOPE = ""
# A Recall write waits this long for the database before failing as busy.
WRITE_PATIENCE_S = 5.0

CATALOG_SCHEMA = """
CREATE TABLE indexed_sessions (
  session_ref INTEGER PRIMARY KEY,
  project_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  generation_id TEXT NOT NULL,
  history_revision INTEGER NOT NULL,
  UNIQUE (project_id, agent_id, session_id)
) STRICT;

CREATE TABLE passages (
  passage_ref INTEGER PRIMARY KEY,
  project_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  passage_key TEXT NOT NULL,
  passage_id TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  text TEXT NOT NULL,
  start_message_id TEXT NOT NULL,
  end_message_id TEXT NOT NULL,
  start_timestamp TEXT NOT NULL,
  end_timestamp TEXT NOT NULL,
  start_role TEXT NOT NULL,
  end_role TEXT NOT NULL,
  UNIQUE (project_id, agent_id, passage_key)
) STRICT;

CREATE INDEX passages_by_text_hash ON passages (text_hash);

CREATE TABLE passage_views (
  session_ref INTEGER NOT NULL,
  passage_ref INTEGER NOT NULL,
  owned INTEGER NOT NULL CHECK (owned IN (0, 1)),
  PRIMARY KEY (session_ref, passage_ref)
) STRICT, WITHOUT ROWID;

CREATE INDEX passage_views_by_passage ON passage_views (passage_ref, session_ref, owned);
"""

# The Passages a set of indexed Sessions shows; the parameter is a JSON array of
# ``session_ref`` values.
VIEWED_BY_SESSIONS = (
    "SELECT passage_ref FROM passage_views WHERE session_ref IN (SELECT value FROM json_each(?))"
)

_PASSAGE_COLUMNS = (
    "passage_ref, passage_id, text, start_message_id, end_message_id, "
    "start_timestamp, end_timestamp, start_role, end_role"
)


def projection_version(layout_version: int) -> int:
    """The kernel projection version of a database that holds the catalog.

    It changes with the owner's layout version and with the Passage policy, so
    the kernel rebuilds the database when either changes. Both counters start at
    1 and only ever grow, so their sum starts at 1 and grows whenever either does.
    """
    return layout_version + PASSAGE_POLICY_VERSION - 1


def stored_scope(project_id: str | None) -> str:
    """Map a Recall project scope to the stored scope value."""
    return project_id if project_id is not None else GLOBAL_SCOPE


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def passage_key(passage: Passage) -> str:
    """Identity of a stored Passage: its id, text and exact boundaries.

    The id alone is insufficient because a growing tail window keeps its id.
    """
    fields = (
        passage.passage_id,
        text_hash(passage.text),
        passage.start_message_id,
        passage.end_message_id,
        passage.start_timestamp,
        passage.end_timestamp,
        passage.start_role,
        passage.end_role,
    )
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredPassage:
    """One indexed Passage with its exact canonical boundaries and text."""

    passage_ref: int
    passage_id: str
    text: str
    start_message_id: str
    end_message_id: str
    start_timestamp: str
    end_timestamp: str
    start_role: str
    end_role: str


def stored_passage(row: sqlite3.Row) -> StoredPassage:
    """Read a row that selected the catalog's Passage columns by name."""
    return StoredPassage(
        passage_ref=int(row["passage_ref"]),
        passage_id=str(row["passage_id"]),
        text=str(row["text"]),
        start_message_id=str(row["start_message_id"]),
        end_message_id=str(row["end_message_id"]),
        start_timestamp=str(row["start_timestamp"]),
        end_timestamp=str(row["end_timestamp"]),
        start_role=str(row["start_role"]),
        end_role=str(row["end_role"]),
    )


def passage_columns(alias: str) -> str:
    """The catalog's Passage columns for :func:`stored_passage`, qualified by *alias*."""
    return ", ".join(f"{alias}.{column.strip()}" for column in _PASSAGE_COLUMNS.split(","))


# -- Planning and applying a refresh ----------------------------------------------


@dataclass(frozen=True)
class SessionPassages:
    """The complete Passage set of one changed Session at one canonical version.

    ``owned`` holds, per Passage, whether its last Message is one of the
    Session's own entries.
    """

    session_id: str
    previous: SessionVersion | None
    version: SessionVersion
    passages: tuple[Passage, ...]
    owned: tuple[bool, ...]


@dataclass(frozen=True)
class CatalogPlan:
    """Catalog changes that bring one scope's candidates up to date."""

    agent_id: str
    project: str
    pruned: tuple[str, ...]
    changes: tuple[SessionPassages, ...]

    @property
    def is_empty(self) -> bool:
        return not self.pruned and not self.changes


class SourceReadError(Exception):
    """A canonical Session read failed while planning a refresh.

    It never marks the index as damaged; :meth:`PassageCatalog.recovering`
    re-raises the original error.
    """

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


def read_stamps(
    connection: sqlite3.Connection, *, agent_id: str, project: str
) -> dict[str, SessionVersion]:
    """Return ``{session_id: (generation_id, history_revision)}`` for one scope."""
    return {
        str(row[0]): (str(row[1]), int(row[2]))
        for row in connection.execute(
            "SELECT session_id, generation_id, history_revision FROM indexed_sessions "
            "WHERE project_id = ? AND agent_id = ?",
            (project, agent_id),
        )
    }


def plan_refresh(
    sessions: ChatSessionManager,
    agent_id: str,
    project_id: str | None,
    scope: RecallScope,
    stamps: Mapping[str, SessionVersion],
) -> CatalogPlan:
    """Reread the candidates whose stamp changed and build their Passages.

    Runs off the Event Loop. Indexed Sessions that left the complete live scope
    are pruned; only the request's candidates are reconciled.
    """
    pruned = {session_id for session_id in stamps if session_id not in scope.live_session_ids}
    changes: list[SessionPassages] = []
    for session_id, version in sorted(scope.candidates.items()):
        previous = stamps.get(session_id)
        if previous == version:
            continue
        address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        try:
            # One read transaction returns the current view together with the
            # exact version it belongs to.
            batch = sessions.get(address).load_since(None)
        except SessionNotFoundError:
            # Vanished since the scope read: its rows describe no live Session.
            if previous is not None:
                pruned.add(session_id)
            continue
        if batch is None:  # Only a stale cursor yields no batch; a full read has none.
            continue
        changes.append(_session_passages(session_id, previous, batch))
    return CatalogPlan(agent_id, stored_scope(project_id), tuple(sorted(pruned)), tuple(changes))


def _session_passages(
    session_id: str, previous: SessionVersion | None, batch: SessionReadBatch
) -> SessionPassages:
    passages = tuple(build_session_passages(batch.active_messages))
    position = {str(message.id): index for index, message in enumerate(batch.active_messages)}
    return SessionPassages(
        session_id=session_id,
        previous=previous,
        version=(batch.cursor.generation_id, batch.cursor.history_revision),
        passages=passages,
        owned=tuple(
            position.get(passage.end_message_id, 0) >= batch.inherited_count for passage in passages
        ),
    )


def apply_plan(connection: sqlite3.Connection, plan: CatalogPlan) -> list[int]:
    """Write *plan* in the caller's write transaction; return the Passages it added.

    A Session whose stamp moved since planning was refreshed by another writer
    and is skipped. A Passage no Session shows any longer is deleted.
    """
    released: set[int] = set()
    if plan.pruned:
        released |= _delete_views(connection, plan.agent_id, plan.project, plan.pruned)
    added: list[int] = []
    for change in plan.changes:
        added.extend(_apply_change(connection, plan, change, released))
    _delete_unviewed(connection, released)
    return added


def _apply_change(
    connection: sqlite3.Connection,
    plan: CatalogPlan,
    change: SessionPassages,
    released: set[int],
) -> list[int]:
    stamp = connection.execute(
        "SELECT session_ref, generation_id, history_revision FROM indexed_sessions "
        "WHERE project_id = ? AND agent_id = ? AND session_id = ?",
        (plan.project, plan.agent_id, change.session_id),
    ).fetchone()
    current = None if stamp is None else (str(stamp[1]), int(stamp[2]))
    if current != change.previous:
        return []
    if stamp is None:
        # RETURNING rows are drained so the statement completes at once.
        (inserted_session,) = connection.execute(
            "INSERT INTO indexed_sessions "
            "(project_id, agent_id, session_id, generation_id, history_revision) "
            "VALUES (?, ?, ?, ?, ?) RETURNING session_ref",
            (plan.project, plan.agent_id, change.session_id, *change.version),
        ).fetchall()
        session_ref = int(inserted_session[0])
    else:
        session_ref = int(stamp[0])
        connection.execute(
            "UPDATE indexed_sessions SET generation_id = ?, history_revision = ? "
            "WHERE session_ref = ?",
            (*change.version, session_ref),
        )
    existing = {
        str(row[0]): (int(row[1]), bool(row[2]))
        for row in connection.execute(
            "SELECT p.passage_key, v.passage_ref, v.owned FROM passage_views AS v "
            "JOIN passages AS p ON p.passage_ref = v.passage_ref WHERE v.session_ref = ?",
            (session_ref,),
        )
    }
    target: dict[str, tuple[Passage, bool]] = {}
    for passage, owned in zip(change.passages, change.owned, strict=True):
        key = passage_key(passage)
        previous = target.get(key)
        target[key] = (passage, owned or (previous is not None and previous[1]))
    for key, (passage_ref, _owned) in existing.items():
        if key not in target:
            connection.execute(
                "DELETE FROM passage_views WHERE session_ref = ? AND passage_ref = ?",
                (session_ref, passage_ref),
            )
            released.add(passage_ref)
    added: list[int] = []
    for key, (passage, owned) in target.items():
        stored = existing.get(key)
        if stored is not None:
            if stored[1] != owned:
                connection.execute(
                    "UPDATE passage_views SET owned = ? WHERE session_ref = ? AND passage_ref = ?",
                    (int(owned), session_ref, stored[0]),
                )
            continue
        passage_ref = _insert_passage(connection, plan, key, passage, added)
        connection.execute(
            "INSERT INTO passage_views (session_ref, passage_ref, owned) VALUES (?, ?, ?)",
            (session_ref, passage_ref, int(owned)),
        )
    return added


def _insert_passage(
    connection: sqlite3.Connection,
    plan: CatalogPlan,
    key: str,
    passage: Passage,
    added: list[int],
) -> int:
    """Return the stored Passage for *key*, inserting it when the scope has none."""
    inserted = connection.execute(
        """
        INSERT INTO passages (
          project_id, agent_id, passage_key, passage_id, text_hash, text,
          start_message_id, end_message_id, start_timestamp, end_timestamp,
          start_role, end_role
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (project_id, agent_id, passage_key) DO NOTHING
        RETURNING passage_ref
        """,
        (
            plan.project,
            plan.agent_id,
            key,
            passage.passage_id,
            text_hash(passage.text),
            passage.text,
            passage.start_message_id,
            passage.end_message_id,
            passage.start_timestamp,
            passage.end_timestamp,
            passage.start_role,
            passage.end_role,
        ),
    ).fetchall()
    if inserted:
        added.append(int(inserted[0][0]))
        return int(inserted[0][0])
    row = connection.execute(
        "SELECT passage_ref FROM passages "
        "WHERE project_id = ? AND agent_id = ? AND passage_key = ?",
        (plan.project, plan.agent_id, key),
    ).fetchone()
    return int(row[0])


def delete_sessions(
    connection: sqlite3.Connection,
    *,
    agent_id: str,
    project: str,
    session_ids: Sequence[str],
) -> None:
    """Remove Sessions with their views, and the Passages no Session shows any more."""
    _delete_unviewed(connection, _delete_views(connection, agent_id, project, session_ids))


def _delete_views(
    connection: sqlite3.Connection, agent_id: str, project: str, session_ids: Sequence[str]
) -> set[int]:
    refs = [
        int(row[0])
        for row in connection.execute(
            "SELECT session_ref FROM indexed_sessions WHERE project_id = ? AND agent_id = ? "
            "AND session_id IN (SELECT value FROM json_each(?))",
            (project, agent_id, json.dumps(sorted(set(session_ids)))),
        )
    ]
    if not refs:
        return set()
    selected = json.dumps(refs)
    released = {
        int(row[0])
        for row in connection.execute(
            "DELETE FROM passage_views WHERE session_ref IN (SELECT value FROM json_each(?)) "
            "RETURNING passage_ref",
            (selected,),
        ).fetchall()
    }
    connection.execute(
        "DELETE FROM indexed_sessions WHERE session_ref IN (SELECT value FROM json_each(?))",
        (selected,),
    )
    return released


def _delete_unviewed(connection: sqlite3.Connection, passage_refs: set[int]) -> None:
    # Triggers of the owning database remove whatever it keeps per Passage.
    connection.executemany(
        "DELETE FROM passages WHERE passage_ref = ? "
        "AND NOT EXISTS (SELECT 1 FROM passage_views WHERE passage_ref = ?)",
        [(passage_ref, passage_ref) for passage_ref in sorted(passage_refs)],
    )


# -- Candidates and attribution ----------------------------------------------------


@dataclass(frozen=True)
class Candidates:
    """The request's candidate Sessions of one scope with their creation order."""

    agent_id: str
    project: str
    creation_orders: Mapping[str, int]

    @classmethod
    def of(cls, agent_id: str, project_id: str | None, scope: RecallScope) -> Candidates:
        return cls(
            agent_id,
            stored_scope(project_id),
            {session_id: scope.creation_orders[session_id] for session_id in scope.candidates},
        )


def candidate_refs(
    connection: sqlite3.Connection, candidates: Candidates
) -> dict[int, tuple[str, int]]:
    """Map each indexed candidate's ``session_ref`` to its id and creation order."""
    rows = connection.execute(
        "SELECT session_ref, session_id FROM indexed_sessions WHERE project_id = ? "
        "AND agent_id = ? AND session_id IN (SELECT value FROM json_each(?))",
        (candidates.project, candidates.agent_id, json.dumps(sorted(candidates.creation_orders))),
    ).fetchall()
    return {int(row[0]): (str(row[1]), candidates.creation_orders[str(row[1])]) for row in rows}


def refs_parameter(refs: Mapping[int, Any]) -> str:
    """The JSON parameter of :data:`VIEWED_BY_SESSIONS` for *refs*."""
    return json.dumps(sorted(refs))


def reported_sessions(
    connection: sqlite3.Connection,
    passage_refs: Sequence[int],
    refs: Mapping[int, tuple[str, int]],
) -> dict[int, str]:
    """Map each Passage to the candidate Session its hit is reported for.

    The owner wins when it is a candidate, else the newest candidate that shows
    the Passage. A Passage no candidate shows is absent.
    """
    best: dict[int, tuple[int, int, str]] = {}
    for passage_ref, session_ref, owned in connection.execute(
        "SELECT passage_ref, session_ref, owned FROM passage_views "
        "WHERE passage_ref IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(set(passage_refs))),),
    ):
        candidate = refs.get(int(session_ref))
        if candidate is None:
            continue
        session_id, creation_order = candidate
        rank = (int(owned), creation_order, session_id)
        current = best.get(int(passage_ref))
        if current is None or rank > current:
            best[int(passage_ref)] = rank
    return {passage_ref: rank[2] for passage_ref, rank in best.items()}


def time_bounds(
    alias: str, since: datetime | None, until: datetime | None, *, lenient: bool
) -> tuple[list[str], list[str]]:
    """Conditions keeping Passages that overlap the requested period.

    Stored timestamps compare as instants, so equivalent encodings agree. With
    ``lenient`` a timestamp that is not a valid instant keeps the Passage.
    """
    conditions: list[str] = []
    parameters: list[str] = []
    for column, operator, bound in (
        ("end_timestamp", ">=", since),
        ("start_timestamp", "<=", until),
    ):
        if bound is None:
            continue
        condition = f"julianday({alias}.{column}) {operator} julianday(?)"
        if lenient:
            condition = f"(julianday({alias}.{column}) IS NULL OR {condition})"
        conditions.append(condition)
        parameters.append(bound.isoformat())
    return conditions, parameters


# -- The database ------------------------------------------------------------------


class PassageCatalog:
    """One disposable Recall database that holds the Passage catalog.

    Operations run through the kernel's asynchronous variants, so no SQLite
    work blocks the Event Loop. Subclasses add their own tables and keep them in
    step through :meth:`_after_add` and triggers on ``passages``.
    """

    def __init__(self, spec: DatabaseSpec) -> None:
        self._database = DisposableDatabase(spec)

    @property
    def path(self) -> Path:
        return self._database.path

    async def read(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        database = await self._database.get_async()
        return await database.read_async(operation)

    async def write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        database = await self._database.get_async()
        return await database.write_async(operation, patience_s=WRITE_PATIENCE_S)

    async def list_indexed_sessions(
        self, agent_id: str, project_id: str | None = None
    ) -> dict[str, SessionVersion]:
        """Return the freshness stamp of every indexed Session of one scope."""
        project = stored_scope(project_id)
        return await self.read(
            lambda connection: read_stamps(connection, agent_id=agent_id, project=project)
        )

    async def refresh(
        self,
        sessions: ChatSessionManager,
        agent_id: str,
        project_id: str | None,
        scope: RecallScope,
    ) -> None:
        """Bring the scope's candidates up to date in one write transaction.

        One stamp read finds indexed Sessions that left the scope and
        candidates whose ``(generation_id, history_revision)`` changed; only
        those are reread, and only their changed Passages and views are
        written. A canonical read failure raises :class:`SourceReadError`.
        """
        project = stored_scope(project_id)
        stamps = await self.read(
            lambda connection: read_stamps(connection, agent_id=agent_id, project=project)
        )
        try:
            plan = await asyncio.to_thread(
                plan_refresh, sessions, agent_id, project_id, scope, stamps
            )
        except Exception as error:
            raise SourceReadError(error) from error
        await self.apply(plan)

    async def apply(self, plan: CatalogPlan) -> None:
        """Write *plan* in one transaction; an empty plan writes nothing."""
        if not plan.is_empty:
            await self.write(lambda connection: self._apply(connection, plan))

    def _apply(self, connection: sqlite3.Connection, plan: CatalogPlan) -> None:
        added = apply_plan(connection, plan)
        if added:
            self._after_add(connection, added)

    def _after_add(self, connection: sqlite3.Connection, passage_refs: list[int]) -> None:
        """Complete state a subclass keeps for newly stored Passages."""

    async def remove_session(self, agent_id: str, project_id: str | None, session_id: str) -> None:
        """Evict one Session and the Passages only it showed."""
        project = stored_scope(project_id)
        await self.write(
            lambda connection: delete_sessions(
                connection, agent_id=agent_id, project=project, session_ids=(session_id,)
            )
        )

    async def recovering(
        self,
        operation: Callable[[], Awaitable[_T]],
        *,
        warning: Callable[[BaseException], None],
    ) -> _T:
        """Run *operation*; a damaged database is discarded and it runs once more.

        ``warning`` reports each damage before the discard. Contention and
        unavailability never discard. A canonical read failure propagates
        unchanged.
        """
        for attempt in range(2):
            try:
                return await operation()
            except SourceReadError as failure:
                raise failure.error from failure.error.__cause__
            except Exception as error:
                if projection_failure(error) != "rebuild":
                    raise
                warning(error)
                await self.discard_quietly()
                if attempt:
                    raise
        raise AssertionError("unreachable")

    async def discard_if_damaged(self, error: BaseException) -> None:
        """Discard the database when *error* shows it is damaged, so it is rebuilt."""
        if projection_failure(error) == "rebuild":
            await self.discard_quietly()

    async def discard_quietly(self) -> None:
        """Discard the database; one still open elsewhere is left for later."""
        with contextlib.suppress(DatabaseError):
            await self._database.discard_async()

    def close(self) -> None:
        """Release the database; later operations fail as unavailable."""
        self._database.close()
