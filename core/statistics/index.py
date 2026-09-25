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
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

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
)
from core.statistics._accounting import ACCOUNTING_SCHEMA, UsageSourceError, reconcile_usage
from core.statistics._projection import CALL_COLUMNS, ProjectedRows
from core.utils.errors import VBotError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.usage import UsageRecorder

JsonObject = dict[str, Any]
_Result = TypeVar("_Result")

_LOGGER = get_logger("statistics")

_INDEX_DIRECTORY = "statistics"
_INDEX_FILENAME = "session-statistics.sqlite"
_GLOBAL_SCOPE = ""
# The kernel discards and rebuilds an index built for another projection
# version. Bump it when the fact tables or the meaning of their rows change.
_PROJECTION_VERSION = 2
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
    """
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
CREATE TABLE stat_calls (
    session_key INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    kind INTEGER NOT NULL,
    instant INTEGER NOT NULL,
    day INTEGER NOT NULL,
    model_key TEXT NOT NULL,
    has_model INTEGER NOT NULL,
    visible INTEGER NOT NULL,
    has_usage INTEGER NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    input_estimated INTEGER NOT NULL,
    output_estimated INTEGER NOT NULL,
    has_cache INTEGER NOT NULL,
    reasoning_present INTEGER NOT NULL,
    cache_read_present INTEGER NOT NULL,
    cache_write_present INTEGER NOT NULL,
    price_estimated INTEGER NOT NULL,
    reported_cost_usd REAL,
    retrospective INTEGER NOT NULL,
    priced INTEGER NOT NULL,
    cost_usd REAL,
    cost_source INTEGER NOT NULL,
    cost_json TEXT,
    purpose TEXT NOT NULL,
    PRIMARY KEY (session_key, seq)
) WITHOUT ROWID;
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
CREATE TABLE stat_pricing (
    model_key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL
) WITHOUT ROWID;
"""
    + ACCOUNTING_SCHEMA
)

_INSERT_COLUMNS = {
    "records": ("stat_records", "session_key, seq, role, timestamp, instant, run_id"),
    "calls": (
        "stat_calls",
        CALL_COLUMNS,
    ),
    "tools": ("stat_tools", "session_key, seq, instant, name, outcome, error_code, duration_ms"),
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
    """One identity, Project or Extension-owned Session scope in report order."""

    project_id: str | None
    agent_id: str
    display_key: str
    summaries: tuple[JsonObject, ...]


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

    def read(
        self,
        sessions: StatisticsSessionSource,
        scopes: Sequence[StatisticsScope],
        consume: Callable[[IndexView], _Result],
        *,
        prune: bool = True,
        usage_recorder: UsageRecorder | None = None,
    ) -> _Result:
        """Reconcile ``scopes`` and run ``consume`` on one consistent index view.

        ``prune`` removes indexed Sessions outside ``scopes``; partial readers
        such as one Extension group pass ``False`` so they never shrink the
        shared index. Recovery may retry ``consume`` after partial aggregation;
        mutable result state must be local to each call. After :meth:`close` it raises
        :class:`~core.database.DatabaseUnavailableError`.
        """
        with self._lock:
            if self._database.is_closed():
                raise DatabaseUnavailableError(f"{self._database.spec.name} is closed")
            try:
                for attempt in range(2):
                    try:
                        return self._read_file(
                            sessions, scopes, consume, prune=prune, usage_recorder=usage_recorder
                        )
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
                return self._read_memory(
                    sessions, scopes, consume, prune=prune, usage_recorder=usage_recorder
                )
            except (_SourceFailureError, UsageSourceError) as failure:
                raise failure.error from failure.error.__cause__

    def discard(self) -> None:
        """Close and delete the disposable database with its SQLite sidecars."""
        with self._lock:
            self._database.discard()

    async def run_async(
        self, function: Callable[..., _Result], *arguments: Any, **keyword_arguments: Any
    ) -> _Result:
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

    def _read_file(
        self,
        sessions: StatisticsSessionSource,
        scopes: Sequence[StatisticsScope],
        consume: Callable[[IndexView], _Result],
        *,
        prune: bool,
        usage_recorder: UsageRecorder | None,
    ) -> _Result:
        database = self._database.get()
        indexed = database.write(
            lambda connection: _reconcile_sources(
                connection, sessions, scopes, prune=prune, usage_recorder=usage_recorder
            ),
            patience_s=_WRITE_PATIENCE_S,
        )
        # Aggregation builds temporary tables, which read-only pooled readers
        # refuse, so the consumer runs on the writer too; its transaction
        # changes no index row.
        return database.write(
            lambda connection: _consume(connection, indexed, consume),
            patience_s=_WRITE_PATIENCE_S,
        )

    def _read_memory(
        self,
        sessions: StatisticsSessionSource,
        scopes: Sequence[StatisticsScope],
        consume: Callable[[IndexView], _Result],
        *,
        prune: bool,
        usage_recorder: UsageRecorder | None,
    ) -> _Result:
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            _prepare_connection(connection)
            connection.executescript(_SCHEMA)
            with _transaction(connection, immediate=True):
                indexed = _reconcile_sources(
                    connection, sessions, scopes, prune=prune, usage_recorder=usage_recorder
                )
            with _transaction(connection):
                return _consume(connection, indexed, consume)


def _consume(
    connection: sqlite3.Connection,
    indexed: Mapping[tuple[str, str, str], IndexedSession],
    consume: Callable[[IndexView], _Result],
) -> _Result:
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


def _reconcile_sources(
    connection: sqlite3.Connection,
    sessions: StatisticsSessionSource,
    scopes: Sequence[StatisticsScope],
    *,
    prune: bool,
    usage_recorder: UsageRecorder | None,
) -> dict[tuple[str, str, str], IndexedSession]:
    indexed = _reconcile(connection, sessions, scopes, prune=prune)
    if usage_recorder is not None:
        reconcile_usage(connection, usage_recorder)
    return indexed


def _reconcile(
    connection: sqlite3.Connection,
    sessions: StatisticsSessionSource,
    scopes: Sequence[StatisticsScope],
    *,
    prune: bool,
) -> dict[tuple[str, str, str], IndexedSession]:
    """Bring the index up to date, touching canonical history only for changes.

    Runs inside the caller's write transaction; an unchanged index writes nothing.
    """
    # A Session listed by several scopes keeps its first position and its last
    # listed summary.
    listed: dict[tuple[str, str, str], tuple[SessionAddress, JsonObject]] = {}
    for scope in scopes:
        for summary in scope.summaries:
            session_id = str(summary["id"])
            key = statistics_session_key(scope.project_id, scope.agent_id, session_id)
            listed[key] = (
                SessionAddress(
                    project_id=scope.project_id,
                    agent_id=scope.agent_id,
                    session_id=session_id,
                ),
                summary,
            )
    versions = _source(sessions.list_history_versions, [address for address, _ in listed.values()])
    stored = _stored_sessions(connection)
    current: dict[tuple[str, str, str], IndexedSession] = {}
    changed: list[
        tuple[
            tuple[str, str, str], SessionAddress, JsonObject, tuple[str, int], _StoredSession | None
        ]
    ] = []
    for key, (address, summary) in listed.items():
        version = versions.get(address)
        if version is None:
            continue
        row = stored.get(key)
        if (
            row is not None
            and row.generation_id == version[0]
            and row.history_revision == version[1]
        ):
            current[key] = IndexedSession(row.session_key, row.generation_id, summary)
        else:
            changed.append((key, address, summary, version, row))
    stale = [row.session_key for key, row in stored.items() if key not in current] if prune else []
    if not changed and not stale:
        return current

    removed = False
    for key, address, summary, version, row in changed:
        try:
            handle = _source(sessions.get, address)
            indexed = _refresh(connection, handle, key, summary, version, row)
        except SessionNotFoundError:
            # The live generation vanished after the batched version read;
            # a stale derived row is pruned below.
            continue
        # Replacing or extending an existing Session may retire priced Models.
        removed = removed or row is not None
        current[key] = indexed
    if prune:
        stale_keys = [row.session_key for key, row in stored.items() if key not in current]
        if stale_keys:
            _delete_facts(connection, stale_keys)
            connection.executemany(
                "DELETE FROM stat_sessions WHERE session_key = ?",
                [(session_key,) for session_key in stale_keys],
            )
            removed = True
    if removed:
        connection.execute(
            """
            DELETE FROM stat_pricing
            WHERE NOT EXISTS (
                SELECT 1 FROM stat_calls
                WHERE retrospective = 1 AND model_key = stat_pricing.model_key
            )
            """
        )
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
        )
        for row in connection.execute(
            """
            SELECT project_id, agent_id, session_id, session_key, generation_id,
                history_revision, next_seq, last_message_id, min_instant, max_instant
            FROM stat_sessions
            """
        )
    }


def _refresh(
    connection: sqlite3.Connection,
    session: ChatSession,
    key: tuple[str, str, str],
    summary: JsonObject,
    version: tuple[str, int],
    row: _StoredSession | None,
) -> IndexedSession:
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
            _append(connection, row, batch)
            return IndexedSession(row.session_key, batch.cursor.generation_id, summary)
    batch = _source(session.load_since)
    if batch is None:
        raise SessionNotFoundError(f"Session {key[2]} has no readable history")
    return _replace(connection, key, summary, batch, row)


def _replace(
    connection: sqlite3.Connection,
    key: tuple[str, str, str],
    summary: JsonObject,
    batch: SessionReadBatch,
    row: _StoredSession | None,
) -> IndexedSession:
    if row is None:
        cursor = connection.execute(
            """
            INSERT INTO stat_sessions (
                project_id, agent_id, session_id, generation_id, history_revision,
                next_seq, last_message_id, min_instant, max_instant
            ) VALUES (?, ?, ?, '', 0, 0, NULL, NULL, NULL)
            """,
            key,
        )
        session_key = int(cursor.lastrowid or 0)
    else:
        session_key = row.session_key
        _delete_facts(connection, [session_key])
    rows = _project_batch(session_key, batch)
    _insert_rows(connection, rows)
    _write_session_state(
        connection,
        session_key,
        batch.cursor,
        min_instant=rows.min_instant,
        max_instant=rows.max_instant,
    )
    return IndexedSession(session_key, batch.cursor.generation_id, summary)


def _append(connection: sqlite3.Connection, row: _StoredSession, batch: SessionReadBatch) -> None:
    rows = _project_batch(row.session_key, batch)
    _insert_rows(connection, rows)
    _write_session_state(
        connection,
        row.session_key,
        batch.cursor,
        min_instant=_bound(min, row.min_instant, rows.min_instant),
        max_instant=_bound(max, row.max_instant, rows.max_instant),
    )


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
    for table in SESSION_FACT_TABLES:
        connection.executemany(f"DELETE FROM {table} WHERE session_key = ?", parameters)


def _source(call: Callable[..., _Result], *args: Any) -> _Result:
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
