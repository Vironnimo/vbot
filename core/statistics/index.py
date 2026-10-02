"""Disposable typed SQLite read model for Statistics-relevant Session facts.

One :class:`StatisticsIndex` owns the index database of a data directory, a
disposable projection opened through the shared kernel (``core/database``),
which also owns its identity, projection version, journal policy and rebuild
on open. Every read reconciles canonical Sessions into typed tables and then
hands one SQL connection to a consumer that aggregates in SQL; no projection
is hydrated or kept in memory between reads. Canonical history is only touched
for Sessions whose generation or revision changed, and an unchanged index is
read without any write. Each Session contributes its own audit only: a fork's
inherited history belongs to the Session that wrote it, so shared history
counts once however many forks show it.

Failure policy: a busy or locked index raises :class:`StatisticsUnavailableError`
so the caller can retry; a damaged or inconsistent index is discarded and
rebuilt once; an index that cannot be used otherwise (or fails again after the
rebuild) computes the read from a transient in-memory projection with the same
code, so Statistics stay available. After :meth:`StatisticsIndex.close` reads
raise :class:`~core.database.DatabaseUnavailableError`.

Reconcile also prices retrospective calls and maintains the aggregate tables
(``core/statistics/_rollups.py``) in the same write transaction, recomputing
only the units whose facts changed, so a consumer always reads aggregates that
match the facts.

Asynchronous callers run a read, with everything it touches, on the index
database's bounded worker pool through :meth:`StatisticsIndex.run_async`.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from core.database import (
    APPLICATION_IDS,
    DISPOSABLE,
    DatabaseError,
    DatabaseSpec,
    DatabaseUnavailableError,
    DisposableDatabase,
    projection_failure,
)
from core.sessions import (
    ChatSession,
    SessionAddress,
    SessionNotFoundError,
    SessionReadBatch,
    SessionReadCursor,
    SessionRunRecord,
)
from core.statistics._accounting import ACCOUNTING_SCHEMA, UsageSourceError, reconcile_usage
from core.statistics._costs import PricingLookup, prune_pricing, refresh_retrospective_costs
from core.statistics._projection import (
    CALL_COLUMNS,
    CALL_TABLE_DEFINITION,
    ProjectedRows,
    timestamp_instant,
)
from core.statistics._rollups import ROLLUP_SCHEMA, RollupChanges, maintain_rollups
from core.utils.errors import VBotError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.usage import UsageRecorder

JsonObject = dict[str, Any]

_LOGGER = get_logger("statistics")

_INDEX_DIRECTORY = "statistics"
_INDEX_FILENAME = "session-statistics.sqlite"
_GLOBAL_SCOPE = ""
# The kernel discards and rebuilds an index built for another projection
# version. Bump it when the fact tables or the meaning of their rows change.
_PROJECTION_VERSION = 3
# A busy index fails the read quickly as retryable instead of queueing it.
_WRITE_PATIENCE_S = 1.0

SESSION_FACT_TABLES = (
    "stat_records",
    "stat_calls",
    "stat_tools",
    "stat_errors",
    "stat_checkpoints",
    "stat_runs",
    "stat_skills",
)

_SCHEMA = (
    f"""
CREATE TABLE stat_sessions (
    session_key INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    history_revision INTEGER NOT NULL,
    next_seq INTEGER NOT NULL,
    last_message_id TEXT,
    min_instant INTEGER,
    max_instant INTEGER,
    owner_name TEXT NOT NULL,
    is_subagent INTEGER NOT NULL,
    UNIQUE (project_id, agent_id, session_id)
);
CREATE TABLE stat_records (
    session_key INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    instant INTEGER NOT NULL,
    run_id TEXT,
    PRIMARY KEY (session_key, seq)
) WITHOUT ROWID;
CREATE INDEX stat_records_run
    ON stat_records(session_key, run_id, seq, role, instant)
    WHERE run_id IS NOT NULL;
CREATE TABLE stat_calls {CALL_TABLE_DEFINITION};
CREATE INDEX stat_calls_retrospective
    ON stat_calls(model_key) WHERE retrospective = 1;
CREATE INDEX stat_calls_unpriced
    ON stat_calls(model_key) WHERE retrospective = 1 AND priced = 0;
CREATE TABLE stat_tools (
    session_key INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    instant INTEGER NOT NULL,
    name TEXT NOT NULL,
    outcome INTEGER,
    error_code TEXT,
    duration_ms INTEGER,
    run_id TEXT,
    latency_bucket INTEGER,
    PRIMARY KEY (session_key, seq)
) WITHOUT ROWID;
CREATE TABLE stat_errors (
    session_key INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    instant INTEGER NOT NULL,
    day INTEGER NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (session_key, seq)
) WITHOUT ROWID;
CREATE TABLE stat_checkpoints (
    session_key INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    instant INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    context_before INTEGER,
    context_after INTEGER,
    duration_ms INTEGER,
    PRIMARY KEY (session_key, seq)
) WITHOUT ROWID;
CREATE TABLE stat_runs (
    session_key INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    instant INTEGER NOT NULL,
    day INTEGER NOT NULL,
    run_id TEXT,
    status TEXT NOT NULL,
    duration_ms INTEGER,
    timing_started_at TEXT,
    timing_completed_at TEXT,
    activity_start INTEGER,
    activity_end INTEGER,
    PRIMARY KEY (session_key, seq)
) WITHOUT ROWID;
CREATE INDEX stat_runs_run ON stat_runs(session_key, run_id);
CREATE INDEX stat_runs_activity
    ON stat_runs(activity_end, activity_start)
    WHERE activity_start IS NOT NULL AND activity_end IS NOT NULL;
CREATE TABLE stat_skills (
    session_key INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (session_key, seq)
) WITHOUT ROWID;
-- The Session's own canonical Run records, replaced from Sessions on refresh.
CREATE TABLE stat_run_records (
    session_key INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    run_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    start_instant INTEGER NOT NULL,
    end_instant INTEGER,
    duration_ms INTEGER,
    completion_reason TEXT,
    iteration_count INTEGER,
    changed_files INTEGER,
    lines_added INTEGER,
    lines_removed INTEGER,
    PRIMARY KEY (session_key, run_id)
) WITHOUT ROWID;
CREATE TABLE stat_pricing (
    model_key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL
) WITHOUT ROWID;
"""
    + ACCOUNTING_SCHEMA
    + ROLLUP_SCHEMA
)
_RUN_RECORD_COLUMNS = (
    "session_key, run_id, run_kind, status, start_instant, end_instant, duration_ms, "
    "completion_reason, iteration_count, changed_files, lines_added, lines_removed"
)

_INSERT_COLUMNS = {
    "records": ("stat_records", "session_key, seq, role, timestamp, instant, run_id"),
    "calls": (
        "stat_calls",
        CALL_COLUMNS,
    ),
    "tools": (
        "stat_tools",
        "session_key, seq, instant, name, outcome, error_code, duration_ms, run_id, latency_bucket",
    ),
    "errors": ("stat_errors", "session_key, seq, instant, day, kind"),
    "checkpoints": (
        "stat_checkpoints",
        "session_key, seq, instant, strategy, context_before, context_after, duration_ms",
    ),
    "runs": (
        "stat_runs",
        "session_key, seq, instant, day, run_id, status, duration_ms, timing_started_at, "
        "timing_completed_at, activity_start, activity_end",
    ),
    "skills": ("stat_skills", "session_key, seq, name"),
}
_INSERT_ROWS = {
    name: (
        f"INSERT INTO {table} ({columns}) "
        f"VALUES ({', '.join('?' for _column in columns.split(','))})"
    )
    for name, (table, columns) in _INSERT_COLUMNS.items()
}


def _prepare_connection(connection: sqlite3.Connection) -> None:
    # Aggregation builds temporary tables; keep them off the disk.
    connection.execute("PRAGMA temp_store = MEMORY")


def statistics_database_spec(path: Path) -> DatabaseSpec:
    """Declare the disposable Statistics index at ``path``."""
    return DatabaseSpec(
        name="statistics",
        path=path,
        profile=DISPOSABLE,
        application_id=APPLICATION_IDS["statistics"],
        format_generation=1,
        schema_sql=_SCHEMA,
        projection_version=_PROJECTION_VERSION,
        connection_setup=_prepare_connection,
    )


class StatisticsUnavailableError(VBotError):
    """The Statistics index is busy; the read can be retried shortly."""


class StatisticsSessionSource(Protocol):
    """Session surface required by the derived index."""

    data_dir: Path

    def get(self, address: SessionAddress) -> ChatSession: ...

    def list_history_versions(
        self, addresses: Sequence[SessionAddress]
    ) -> dict[SessionAddress, tuple[str, int]]: ...


@dataclass(frozen=True)
class StatisticsScope:
    """One identity, Project or Extension-owned Session scope in report order.

    ``owner_name`` names the Extension that owns the scope's Sessions; the
    aggregates attribute their Runs and requests to that Extension.
    """

    project_id: str | None
    agent_id: str
    display_key: str
    summaries: tuple[JsonObject, ...]
    owner_name: str | None = None


@dataclass(frozen=True)
class IndexedSession:
    """One reconciled Session: its index key, canonical generation and summary.

    ``summary`` is the last listed summary of the Session across all scopes;
    reports read its title, creation and last activity times, and offered
    Skills from it.
    """

    session_key: int
    generation_id: str
    summary: JsonObject


@dataclass(frozen=True)
class IndexView:
    """A reconciled index read: one connection and the Sessions it covers."""

    connection: sqlite3.Connection
    sessions: Mapping[tuple[str, str, str], IndexedSession]

    def session(
        self, project_id: str | None, agent_id: str, session_id: str
    ) -> IndexedSession | None:
        return self.sessions.get(statistics_session_key(project_id, agent_id, session_id))


@dataclass(frozen=True)
class _StoredSession:
    session_key: int
    generation_id: str
    history_revision: int
    next_seq: int
    last_message_id: str | None
    min_instant: int | None
    max_instant: int | None
    flags: tuple[str, int]


@dataclass(frozen=True)
class _Listed:
    """One listed Session: its address, last listed summary and stored flags.

    ``flags`` are the owning Extension (``""`` for none) and whether it is a
    Sub-Agent Session; they decide the origin of its Runs and requests.
    """

    address: SessionAddress
    summary: JsonObject
    flags: tuple[str, int]


class _SourceFailureError(Exception):
    """Carries a canonical Session failure through index error handling."""

    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


class StatisticsIndex:
    """Own one disposable typed Statistics index and its reconciliation."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.index_path = self.data_dir / _INDEX_DIRECTORY / _INDEX_FILENAME
        self._database = DisposableDatabase(statistics_database_spec(self.index_path))
        self._lock = threading.RLock()

    def read[Result](
        self,
        sessions: StatisticsSessionSource,
        scopes: Sequence[StatisticsScope],
        consume: Callable[[IndexView], Result],
        *,
        prune: bool = True,
        usage_recorder: UsageRecorder | None = None,
        pricing_lookup: PricingLookup | None = None,
    ) -> Result:
        """Reconcile ``scopes`` and run ``consume`` on one consistent index view.

        ``prune`` removes indexed Sessions outside ``scopes``; partial readers
        such as one Extension group pass ``False`` so they never shrink the
        shared index. ``pricing_lookup`` prices calls without a cost snapshot;
        readers of one index pass the same lookup, since a different one
        reprices. Recovery may retry ``consume`` after partial aggregation;
        mutable result state must be local to each call. After :meth:`close` it raises
        :class:`~core.database.DatabaseUnavailableError`.
        """
        sources = _Sources(sessions, scopes, prune, usage_recorder, pricing_lookup)
        with self._lock:
            if self._database.is_closed():
                raise DatabaseUnavailableError(f"{self._database.spec.name} is closed")
            try:
                for attempt in range(2):
                    try:
                        return self._read_file(sources, consume)
                    except Exception as error:
                        failure = projection_failure(error)
                        if failure is None:
                            raise
                        if failure == "busy":
                            raise StatisticsUnavailableError(
                                "Statistics are busy; retry shortly"
                            ) from error
                        if failure == "unavailable" or attempt:
                            _LOGGER.warning(
                                "Statistics index unavailable; using a transient projection: %s",
                                error,
                            )
                            break
                        _LOGGER.warning("Statistics index is inconsistent; rebuilding: %s", error)
                        try:
                            self._database.discard()
                        except DatabaseError as discard_error:
                            _LOGGER.warning(
                                "Could not discard the Statistics index: %s", discard_error
                            )
                            break
                return self._read_memory(sources, consume)
            except (_SourceFailureError, UsageSourceError) as failure:
                raise failure.error from failure.error.__cause__

    def discard(self) -> None:
        """Close and delete the disposable database with its SQLite sidecars."""
        with self._lock:
            self._database.discard()

    async def run_async[Result](
        self, function: Callable[..., Result], *arguments: Any, **keyword_arguments: Any
    ) -> Result:
        """Run blocking Statistics work on the index database's bounded worker pool.

        After :meth:`close` it raises :class:`~core.database.DatabaseUnavailableError`.
        """
        return await self._database.run_async(function, *arguments, **keyword_arguments)

    def close(self) -> None:
        """Release the index database and its worker pool; later work is unavailable."""
        with self._lock:
            self._database.close()

    async def aclose(self) -> None:
        """``close`` for the Event Loop: it waits on the worker pool for a running read."""
        with contextlib.suppress(DatabaseUnavailableError):
            await self._database.run_async(self.close)

    def _read_file[Result](
        self, sources: _Sources, consume: Callable[[IndexView], Result]
    ) -> Result:
        database = self._database.get()
        indexed = database.write(
            lambda connection: _reconcile_sources(connection, sources),
            patience_s=_WRITE_PATIENCE_S,
        )
        # Aggregation builds temporary tables, which read-only pooled readers
        # refuse, so the consumer runs on the writer too; its transaction
        # changes no index row.
        return database.write(
            lambda connection: _consume(connection, indexed, consume),
            patience_s=_WRITE_PATIENCE_S,
        )

    def _read_memory[Result](
        self, sources: _Sources, consume: Callable[[IndexView], Result]
    ) -> Result:
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            _prepare_connection(connection)
            connection.executescript(_SCHEMA)
            with _transaction(connection, immediate=True):
                indexed = _reconcile_sources(connection, sources)
            with _transaction(connection):
                return _consume(connection, indexed, consume)


def _consume[Result](
    connection: sqlite3.Connection,
    indexed: Mapping[tuple[str, str, str], IndexedSession],
    consume: Callable[[IndexView], Result],
) -> Result:
    """Run ``consume`` and drop its temporary tables before the transaction ends.

    Temporary tables outlive a commit on the long-lived writer, and some shadow
    the fact tables by name, so none may survive into the next read. A failed
    consumer's rollback removes the ones it created.
    """
    _drop_temporary_tables(connection)
    result = consume(IndexView(connection, indexed))
    _drop_temporary_tables(connection)
    return result


def _drop_temporary_tables(connection: sqlite3.Connection) -> None:
    names = [
        str(row[0])
        for row in connection.execute("SELECT name FROM temp.sqlite_master WHERE type = 'table'")
    ]
    for name in names:
        connection.execute(f'DROP TABLE temp."{name}"')


@contextmanager
def _transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


@dataclass(frozen=True)
class _Sources:
    """The canonical sources and options of one index read."""

    sessions: StatisticsSessionSource
    scopes: Sequence[StatisticsScope]
    prune: bool
    usage_recorder: UsageRecorder | None
    pricing_lookup: PricingLookup | None


def _reconcile_sources(
    connection: sqlite3.Connection, sources: _Sources
) -> dict[tuple[str, str, str], IndexedSession]:
    """Reconcile facts, price new calls, then maintain the aggregates they changed."""
    changes = RollupChanges()
    indexed = _reconcile(
        connection, sources.sessions, sources.scopes, prune=sources.prune, changes=changes
    )
    if sources.usage_recorder is not None:
        reconcile_usage(connection, sources.usage_recorder, changes)
    repriced = refresh_retrospective_costs(connection, sources.pricing_lookup)
    changes.repriced(repriced["stat_calls"], repriced["stat_usage_calls"])
    maintain_rollups(connection, changes)
    return indexed


def _reconcile(
    connection: sqlite3.Connection,
    sessions: StatisticsSessionSource,
    scopes: Sequence[StatisticsScope],
    *,
    prune: bool,
    changes: RollupChanges,
) -> dict[tuple[str, str, str], IndexedSession]:
    """Bring the index up to date, touching canonical history only for changes.

    Runs inside the caller's write transaction; an unchanged index writes
    nothing. ``changes`` learns every Session whose facts changed.
    """
    # A Session listed by several scopes keeps its first position and its last
    # listed summary; an Extension scope's owner sticks.
    listed: dict[tuple[str, str, str], _Listed] = {}
    for scope in scopes:
        for summary in scope.summaries:
            session_id = str(summary["id"])
            key = statistics_session_key(scope.project_id, scope.agent_id, session_id)
            prior = listed.get(key)
            owner_name = scope.owner_name or (prior.flags[0] if prior is not None else "")
            listed[key] = _Listed(
                SessionAddress(
                    project_id=scope.project_id,
                    agent_id=scope.agent_id,
                    session_id=session_id,
                ),
                summary,
                (owner_name, int(summary.get("is_subagent_session") is True)),
            )
    versions = _source(sessions.list_history_versions, [entry.address for entry in listed.values()])
    stored = _stored_sessions(connection)
    current: dict[tuple[str, str, str], IndexedSession] = {}
    changed: list[tuple[tuple[str, str, str], _Listed, tuple[str, int], _StoredSession | None]] = []
    for key, entry in listed.items():
        version = versions.get(entry.address)
        if version is None:
            continue
        row = stored.get(key)
        if (
            row is not None
            and row.generation_id == version[0]
            and row.history_revision == version[1]
        ):
            current[key] = IndexedSession(row.session_key, row.generation_id, entry.summary)
            if row.flags != entry.flags:
                _write_flags(connection, row.session_key, entry.flags)
                changes.session(row.session_key, key)
        else:
            changed.append((key, entry, version, row))
    stale = [row.session_key for key, row in stored.items() if key not in current] if prune else []
    if not changed and not stale:
        return current

    removed = False
    for key, entry, version, row in changed:
        try:
            handle = _source(sessions.get, entry.address)
            indexed = _refresh(connection, handle, key, entry, version, row, changes)
        except SessionNotFoundError:
            # The live generation vanished after the batched version read;
            # a stale derived row is pruned below.
            continue
        # Replacing or extending an existing Session may retire priced Models.
        removed = removed or row is not None
        current[key] = indexed
    if prune:
        stale_rows = [(key, row) for key, row in stored.items() if key not in current]
        if stale_rows:
            _delete_facts(connection, [row.session_key for _key, row in stale_rows])
            connection.executemany(
                "DELETE FROM stat_sessions WHERE session_key = ?",
                [(row.session_key,) for _key, row in stale_rows],
            )
            for key, row in stale_rows:
                changes.session(row.session_key, key)
            removed = True
    if removed:
        prune_pricing(connection)
    return current


def _stored_sessions(connection: sqlite3.Connection) -> dict[tuple[str, str, str], _StoredSession]:
    return {
        (str(row[0]), str(row[1]), str(row[2])): _StoredSession(
            session_key=int(row[3]),
            generation_id=str(row[4]),
            history_revision=int(row[5]),
            next_seq=int(row[6]),
            last_message_id=row[7],
            min_instant=row[8],
            max_instant=row[9],
            flags=(str(row[10]), int(row[11])),
        )
        for row in connection.execute(
            """
            SELECT project_id, agent_id, session_id, session_key, generation_id,
                history_revision, next_seq, last_message_id, min_instant, max_instant,
                owner_name, is_subagent
            FROM stat_sessions
            """
        )
    }


def _refresh(
    connection: sqlite3.Connection,
    session: ChatSession,
    key: tuple[str, str, str],
    entry: _Listed,
    version: tuple[str, int],
    row: _StoredSession | None,
    changes: RollupChanges,
) -> IndexedSession:
    # Run records are read after the history they describe: a Run that
    # finishes in between changes the revision, so the next read refreshes it.
    if row is not None and row.generation_id == version[0]:
        batch = _source(
            session.load_since,
            SessionReadCursor(
                generation_id=row.generation_id,
                history_revision=row.history_revision,
                next_seq=row.next_seq,
                last_message_id=row.last_message_id,
            ),
        )
        if batch is not None:
            runs = _source(session.run_records, batch.cursor.generation_id)
            _append(connection, key, entry, row, batch, runs, changes)
            return IndexedSession(row.session_key, batch.cursor.generation_id, entry.summary)
    batch = _source(session.load_since)
    if batch is None:
        raise SessionNotFoundError(f"Session {key[2]} has no readable history")
    runs = _source(session.run_records, batch.cursor.generation_id)
    return _replace(connection, key, entry, batch, runs, row, changes)


def _replace(
    connection: sqlite3.Connection,
    key: tuple[str, str, str],
    entry: _Listed,
    batch: SessionReadBatch,
    runs: Sequence[SessionRunRecord],
    row: _StoredSession | None,
    changes: RollupChanges,
) -> IndexedSession:
    if row is None:
        cursor = connection.execute(
            """
            INSERT INTO stat_sessions (
                project_id, agent_id, session_id, generation_id, history_revision,
                next_seq, last_message_id, min_instant, max_instant, owner_name, is_subagent
            ) VALUES (?, ?, ?, '', 0, 0, NULL, NULL, NULL, '', 0)
            """,
            key,
        )
        session_key = int(cursor.lastrowid or 0)
    else:
        session_key = row.session_key
        _delete_facts(connection, [session_key])
    rows = _project_batch(session_key, batch)
    _insert_rows(connection, rows)
    _write_run_records(connection, session_key, runs, {})
    _write_session_state(
        connection,
        session_key,
        batch.cursor,
        min_instant=rows.min_instant,
        max_instant=rows.max_instant,
    )
    _write_flags(connection, session_key, entry.flags)
    changes.session(session_key, key)
    return IndexedSession(session_key, batch.cursor.generation_id, entry.summary)


def _append(
    connection: sqlite3.Connection,
    key: tuple[str, str, str],
    entry: _Listed,
    row: _StoredSession,
    batch: SessionReadBatch,
    runs: Sequence[SessionRunRecord],
    changes: RollupChanges,
) -> None:
    session_key = row.session_key
    rows = _project_batch(session_key, batch)
    _insert_rows(connection, rows)
    stored = {
        str(values[1]): tuple(values)
        for values in connection.execute(
            f"SELECT {_RUN_RECORD_COLUMNS} FROM stat_run_records WHERE session_key = ?",
            (session_key,),
        )
    }
    changed_runs = _write_run_records(connection, session_key, runs, stored)
    _write_session_state(
        connection,
        session_key,
        batch.cursor,
        min_instant=_bound(min, row.min_instant, rows.min_instant),
        max_instant=_bound(max, row.max_instant, rows.max_instant),
    )
    if row.flags != entry.flags:
        _write_flags(connection, session_key, entry.flags)
        changes.session(session_key, key)
        return
    changes.session_runs(
        session_key,
        {str(record[5]) for record in rows.records if record[5]} | changed_runs,
    )
    if rows.tools:
        changes.tool_sessions.add(session_key)
    if {record.run_id: record.run_kind for record in runs} != {
        run_id: str(values[2]) for run_id, values in stored.items()
    }:
        # Run identities decide the origin of this Session's Tools and requests.
        changes.tool_sessions.add(session_key)
        changes.addresses.add(key)


def _write_run_records(
    connection: sqlite3.Connection,
    session_key: int,
    runs: Sequence[SessionRunRecord],
    stored: Mapping[str, tuple[Any, ...]],
) -> set[str]:
    """Store the Session's Run records over ``stored``; return the changed Run ids."""
    current = {
        record.run_id: (
            session_key,
            record.run_id,
            record.run_kind,
            record.status,
            timestamp_instant(record.timing_started_at or record.started_at),
            None if record.completed_at is None else timestamp_instant(record.completed_at),
            record.duration_ms,
            record.completion_reason,
            record.iteration_count,
            record.changed_files,
            record.lines_added,
            record.lines_removed,
        )
        for record in runs
    }
    removed = [run_id for run_id in stored if run_id not in current]
    written = [values for run_id, values in current.items() if stored.get(run_id) != values]
    if removed:
        connection.executemany(
            "DELETE FROM stat_run_records WHERE session_key = ? AND run_id = ?",
            [(session_key, run_id) for run_id in removed],
        )
    if written:
        connection.executemany(
            f"INSERT OR REPLACE INTO stat_run_records ({_RUN_RECORD_COLUMNS}) "
            f"VALUES ({', '.join('?' for _column in _RUN_RECORD_COLUMNS.split(','))})",
            written,
        )
    return {*removed, *(str(values[1]) for values in written)}


def _bound(pick: Callable[[int, int], int], stored: int | None, added: int | None) -> int | None:
    if stored is None:
        return added
    if added is None:
        return stored
    return pick(stored, added)


def _project_batch(session_key: int, batch: SessionReadBatch) -> ProjectedRows:
    """Project a batch of the Session's own audit.

    Own audit entries occupy consecutive sequence numbers up to the batch's
    cursor, so the first entry's sequence follows from the batch length.
    """
    rows = ProjectedRows(session_key)
    first_seq = batch.cursor.next_seq - len(batch.messages)
    for offset, message in enumerate(batch.messages):
        rows.add(first_seq + offset, message)
    return rows


def _insert_rows(connection: sqlite3.Connection, rows: ProjectedRows) -> None:
    for name, statement in _INSERT_ROWS.items():
        values = getattr(rows, name)
        if values:
            connection.executemany(statement, values)


def _write_flags(connection: sqlite3.Connection, session_key: int, flags: tuple[str, int]) -> None:
    connection.execute(
        "UPDATE stat_sessions SET owner_name = ?, is_subagent = ? WHERE session_key = ?",
        (*flags, session_key),
    )


def _write_session_state(
    connection: sqlite3.Connection,
    session_key: int,
    cursor: SessionReadCursor,
    *,
    min_instant: int | None,
    max_instant: int | None,
) -> None:
    connection.execute(
        """
        UPDATE stat_sessions SET
            generation_id = ?,
            history_revision = ?,
            next_seq = ?,
            last_message_id = ?,
            min_instant = ?,
            max_instant = ?
        WHERE session_key = ?
        """,
        (
            cursor.generation_id,
            cursor.history_revision,
            cursor.next_seq,
            cursor.last_message_id,
            min_instant,
            max_instant,
            session_key,
        ),
    )


def _delete_facts(connection: sqlite3.Connection, session_keys: Sequence[int]) -> None:
    parameters = [(session_key,) for session_key in session_keys]
    for table in (*SESSION_FACT_TABLES, "stat_run_records"):
        connection.executemany(f"DELETE FROM {table} WHERE session_key = ?", parameters)


def _source[Result](call: Callable[..., Result], *args: Any) -> Result:
    """Run one canonical Session read, keeping its failures apart from index failures."""
    try:
        return call(*args)
    except SessionNotFoundError:
        raise
    except Exception as error:
        raise _SourceFailureError(error) from error


def _scope_key(project_id: str | None) -> str:
    return project_id if project_id is not None else _GLOBAL_SCOPE


def statistics_session_key(
    project_id: str | None,
    agent_id: str,
    session_id: str,
) -> tuple[str, str, str]:
    """Return the persisted composite key for one scoped Session."""
    return (_scope_key(project_id), agent_id, session_id)
