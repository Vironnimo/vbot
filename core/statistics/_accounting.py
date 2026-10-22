"""Incremental, disposable projection of the canonical request-usage ledger."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from core.statistics._projection import (
    CALL_COLUMNS,
    CALL_TABLE_DEFINITION,
    ProjectedRows,
    timestamp_instant,
)

if TYPE_CHECKING:
    from core.statistics._rollups import RollupChanges
    from core.usage import UsagePage, UsageRecord, UsageRecorder


ACCOUNTING_SCHEMA = f"""
CREATE TABLE stat_usage_state (
    source_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE stat_usage_units (
    session_key INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    group_id TEXT NOT NULL,
    session_title TEXT,
    session_generation_id TEXT NOT NULL DEFAULT '',
    UNIQUE (project_id, agent_id, session_id, owner_name, group_id, session_generation_id)
);
CREATE INDEX stat_usage_units_generation ON stat_usage_units(session_generation_id);
CREATE TABLE stat_usage_records (
    seq INTEGER PRIMARY KEY,
    session_key INTEGER NOT NULL,
    call_id TEXT NOT NULL UNIQUE,
    timestamp TEXT NOT NULL,
    instant INTEGER NOT NULL,
    run_id TEXT,
    status TEXT NOT NULL,
    duration_ms INTEGER,
    failure_class TEXT,
    http_status INTEGER
);
CREATE INDEX stat_usage_records_run ON stat_usage_records(session_key, run_id);
CREATE INDEX stat_usage_records_instant ON stat_usage_records(instant);
CREATE TABLE stat_usage_calls {CALL_TABLE_DEFINITION};
CREATE INDEX stat_usage_calls_window ON stat_usage_calls(session_key, instant);
CREATE INDEX stat_usage_calls_retrospective
    ON stat_usage_calls(model_key) WHERE retrospective = 1;
CREATE INDEX stat_usage_calls_unpriced
    ON stat_usage_calls(model_key) WHERE retrospective = 1 AND priced = 0;
"""

_USAGE_TABLES = ("stat_usage_state", "stat_usage_records", "stat_usage_calls", "stat_usage_units")
_INSERT_CALL = (
    f"INSERT INTO stat_usage_calls ({CALL_COLUMNS}) "
    f"VALUES ({', '.join('?' for _column in CALL_COLUMNS.split(','))})"
)
_RECORD_COLUMNS = (
    "session_key, call_id, timestamp, instant, run_id, status, duration_ms, failure_class, "
    "http_status"
)
# Insert or replace one call's record; an existing record keeps its sequence.
_UPSERT_RECORD = (
    f"INSERT INTO stat_usage_records ({_RECORD_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(call_id) DO UPDATE SET "
    "session_key = excluded.session_key, timestamp = excluded.timestamp, "
    "instant = excluded.instant, run_id = excluded.run_id, status = excluded.status, "
    "duration_ms = excluded.duration_ms, failure_class = excluded.failure_class, "
    "http_status = excluded.http_status "
    "RETURNING seq"
)
_INSERT_RECORD = (
    f"INSERT INTO stat_usage_records (seq, {_RECORD_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class UsageSourceError(Exception):
    """A canonical read failure must never cause a disposable-index rebuild."""

    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


def _ledger_pages(recorder: UsageRecorder, revision: int) -> Iterator[UsagePage]:
    """The recorder's pages after ``revision``; a failed read is a ``UsageSourceError``."""
    try:
        pages = iter(recorder.read_since(revision))
    except Exception as error:
        raise UsageSourceError(error) from error
    while True:
        try:
            page = next(pages)
        except StopIteration:
            return
        except Exception as error:
            raise UsageSourceError(error) from error
        yield page


def reconcile_usage(
    connection: sqlite3.Connection, recorder: UsageRecorder, changes: RollupChanges
) -> None:
    """Apply changed requests atomically; unchanged ledger snapshots write nothing.

    The projection continues from its revision while the recorder's
    ``ledger_id`` is unchanged, so a restart reads only newer changes. Another
    ledger id (a restore) or a watermark below the projected revision rebuilds
    it from the whole ledger, streamed in bounded pages. ``changes`` learns the
    units and Runs whose requests changed; a rebuild renumbers every unit, so
    it rebuilds all aggregates. A rebuild fills the emptied tables a page at a
    time (:class:`_LedgerRebuild`) with the rows the per-call path would write.
    """
    previous = connection.execute("SELECT source_id, revision FROM stat_usage_state").fetchone()
    source_id = recorder.ledger_id
    same_source = previous is not None and previous[0] == source_id
    revision = int(previous[1]) if same_source else 0
    pages = _ledger_pages(recorder, revision)
    latest, records = next(pages)
    # A ledger replaced outside the kernel's restore can keep its identity with
    # a shorter revision history. Its projection must follow that source too.
    rewound = latest < revision
    if rewound:
        revision = 0
        pages = _ledger_pages(recorder, 0)
        latest, records = next(pages)
    rebuild = previous is None or not same_source or rewound
    if rebuild:
        changes.rebuild = True
        for table in _USAGE_TABLES:
            connection.execute(f"DELETE FROM {table}")
    elif latest == revision:
        return
    ledger_rebuild = _LedgerRebuild(connection) if rebuild else None
    while True:
        if ledger_rebuild is not None:
            ledger_rebuild.project(records)
        else:
            _project_calls(connection, records, changes)
        page = next(pages, None)
        if page is None:
            break
        latest, records = page
    connection.execute(
        "INSERT INTO stat_usage_state (source_id, revision) VALUES (?, ?) "
        "ON CONFLICT(source_id) DO UPDATE SET revision = excluded.revision",
        (source_id, latest),
    )


def _project_calls(
    connection: sqlite3.Connection, records: tuple[UsageRecord, ...], changes: RollupChanges
) -> None:
    """Apply changed calls one at a time, retaining each one's prior contribution."""
    for record in records:
        unit = connection.execute(
            "SELECT session_key, session_title FROM stat_usage_units "
            "WHERE project_id = ? AND agent_id = ? AND session_id = ? "
            "AND owner_name = ? AND group_id = ? AND session_generation_id = ?",
            _unit_address(record),
        ).fetchone()
        if unit is None:
            unit_key = _insert_unit(connection, record)
        else:
            unit_key = int(unit[0])
            if record.session_title and unit[1] != record.session_title:
                _write_unit_title(connection, unit_key, record.session_title)
        prior_call = connection.execute(
            "SELECT session_key, seq, run_id FROM stat_usage_records WHERE call_id = ?",
            (record.id,),
        ).fetchone()
        if prior_call is not None:
            # An updated call may have moved to another unit or Run.
            changes.usage_record(connection, int(prior_call[1]))
            changes.usage_call(int(prior_call[0]), prior_call[2])
            connection.execute(
                "DELETE FROM stat_usage_calls WHERE session_key = ? AND seq = ?",
                (prior_call[0], prior_call[1]),
            )
        changes.usage_call(unit_key, record.run_id)
        instant = timestamp_instant(record.timestamp)
        sequence = int(
            connection.execute(
                _UPSERT_RECORD, _record_values(record, unit_key, instant)
            ).fetchone()[0]
        )
        if prior_call is None:
            changes.usage_record(connection, sequence)
        connection.executemany(_INSERT_CALL, _call_rows(record, unit_key, sequence, instant))


class _LedgerRebuild:
    """Project a whole ledger into the emptied usage tables, one batch per page.

    A rebuild recomputes every aggregate, so it retains no prior contributions
    and marks no Runs. Units and call sequences stay in memory instead of
    being looked up per call, and sequences count up from 1 like the table's
    own row ids. A call that changed while the pages were read comes back on
    a later page and replaces its stored rows, keeping its sequence, like the
    per-call path.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        # Unit address -> [unit key, current title].
        self._units: dict[tuple[str, ...], list[Any]] = {}
        # Call id -> (sequence, unit key) of its stored rows.
        self._calls: dict[str, tuple[int, int]] = {}

    def project(self, records: tuple[UsageRecord, ...]) -> None:
        connection = self._connection
        new_records: list[tuple[Any, ...]] = []
        calls: list[tuple[Any, ...]] = []
        for record in records:
            unit_key = self._unit_key(record)
            instant = timestamp_instant(record.timestamp)
            prior = self._calls.get(record.id)
            if prior is None:
                seq = len(self._calls) + 1
                new_records.append((seq, *_record_values(record, unit_key, instant)))
            else:
                # A page never repeats a call, so its earlier rows are stored.
                seq = prior[0]
                connection.execute(
                    "DELETE FROM stat_usage_calls WHERE session_key = ? AND seq = ?",
                    (prior[1], seq),
                )
                connection.execute(_UPSERT_RECORD, _record_values(record, unit_key, instant))
            self._calls[record.id] = (seq, unit_key)
            calls.extend(_call_rows(record, unit_key, seq, instant))
        connection.executemany(_INSERT_RECORD, new_records)
        connection.executemany(_INSERT_CALL, calls)

    def _unit_key(self, record: UsageRecord) -> int:
        address = _unit_address(record)
        unit = self._units.get(address)
        if unit is None:
            unit = [_insert_unit(self._connection, record), record.session_title]
            self._units[address] = unit
        elif record.session_title and unit[1] != record.session_title:
            _write_unit_title(self._connection, unit[0], record.session_title)
            unit[1] = record.session_title
        return int(unit[0])


def _unit_address(record: UsageRecord) -> tuple[str, str, str, str, str, str]:
    return (
        record.project_id or "",
        record.agent_id or "",
        record.session_id or "",
        record.owner_name or "",
        record.group_id or "",
        record.session_generation_id or "",
    )


def _insert_unit(connection: sqlite3.Connection, record: UsageRecord) -> int:
    return int(
        connection.execute(
            "INSERT INTO stat_usage_units "
            "(project_id, agent_id, session_id, owner_name, group_id, "
            "session_generation_id, session_title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING session_key",
            (*_unit_address(record), record.session_title),
        ).fetchone()[0]
    )


def _write_unit_title(connection: sqlite3.Connection, unit_key: int, title: str) -> None:
    connection.execute(
        "UPDATE stat_usage_units SET session_title = ? WHERE session_key = ?", (title, unit_key)
    )


def _record_values(record: UsageRecord, unit_key: int, instant: int) -> tuple[Any, ...]:
    """A call's ``stat_usage_records`` values in ``_RECORD_COLUMNS`` order."""
    return (
        unit_key,
        record.id,
        record.timestamp,
        instant,
        record.run_id,
        record.status,
        record.duration_ms,
        record.failure_class,
        record.http_status,
    )


def _call_rows(record: UsageRecord, unit_key: int, seq: int, instant: int) -> list[tuple[Any, ...]]:
    rows = ProjectedRows(unit_key)
    rows.add_usage_call(
        seq,
        timestamp=record.timestamp,
        instant=instant,
        model=record.model,
        kind=record.kind,
        usage=record.usage,
    )
    return rows.calls
