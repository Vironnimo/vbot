"""Incremental, disposable projection of the canonical request-usage ledger."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import TYPE_CHECKING

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
    UNIQUE (project_id, agent_id, session_id, owner_name, group_id)
);
CREATE TABLE stat_usage_records (
    seq INTEGER PRIMARY KEY,
    session_key INTEGER NOT NULL,
    call_id TEXT NOT NULL UNIQUE,
    timestamp TEXT NOT NULL,
    instant INTEGER NOT NULL,
    run_id TEXT,
    status TEXT NOT NULL
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
    it rebuilds all aggregates.
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
    if previous is None or not same_source or rewound:
        changes.rebuild = True
    if previous is not None and (not same_source or rewound):
        for table in (
            "stat_usage_state",
            "stat_usage_records",
            "stat_usage_calls",
            "stat_usage_units",
        ):
            connection.execute(f"DELETE FROM {table}")
    elif same_source and latest == revision:
        return
    _project_calls(connection, records, changes)
    for page in pages:
        _project_calls(connection, page.records, changes)
        latest = page.revision
    connection.execute(
        "INSERT INTO stat_usage_state (source_id, revision) VALUES (?, ?) "
        "ON CONFLICT(source_id) DO UPDATE SET revision = excluded.revision",
        (source_id, latest),
    )


def _project_calls(
    connection: sqlite3.Connection, records: tuple[UsageRecord, ...], changes: RollupChanges
) -> None:
    for record in records:
        address = (
            record.project_id or "",
            record.agent_id or "",
            record.session_id or "",
            record.owner_name or "",
            record.group_id or "",
        )
        unit = connection.execute(
            "SELECT session_key, session_title FROM stat_usage_units "
            "WHERE project_id = ? AND agent_id = ? AND session_id = ? "
            "AND owner_name = ? AND group_id = ?",
            address,
        ).fetchone()
        if unit is None:
            unit_key = int(
                connection.execute(
                    "INSERT INTO stat_usage_units "
                    "(project_id, agent_id, session_id, owner_name, group_id, session_title) "
                    "VALUES (?, ?, ?, ?, ?, ?) RETURNING session_key",
                    (*address, record.session_title),
                ).fetchone()[0]
            )
        else:
            unit_key = int(unit[0])
            if record.session_title and unit[1] != record.session_title:
                connection.execute(
                    "UPDATE stat_usage_units SET session_title = ? WHERE session_key = ?",
                    (record.session_title, unit_key),
                )
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
        sequence = int(
            connection.execute(
                "INSERT INTO stat_usage_records "
                "(session_key, call_id, timestamp, instant, run_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(call_id) DO UPDATE SET "
                "session_key = excluded.session_key, timestamp = excluded.timestamp, "
                "instant = excluded.instant, run_id = excluded.run_id, status = excluded.status "
                "RETURNING seq",
                (
                    unit_key,
                    record.id,
                    record.timestamp,
                    timestamp_instant(record.timestamp),
                    record.run_id,
                    record.status,
                ),
            ).fetchone()[0]
        )
        if prior_call is None:
            changes.usage_record(connection, sequence)
        rows = ProjectedRows(unit_key)
        rows.add_usage_call(
            sequence,
            timestamp=record.timestamp,
            model=record.model,
            kind=record.kind,
            usage=record.usage,
        )
        connection.executemany(
            f"INSERT INTO stat_usage_calls ({CALL_COLUMNS}) "
            f"VALUES ({', '.join('?' for _ in CALL_COLUMNS.split(','))})",
            rows.calls,
        )
