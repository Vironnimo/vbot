"""Incremental, disposable projection of the canonical request-usage ledger."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from core.statistics._projection import CALL_COLUMNS, ProjectedRows, timestamp_instant

if TYPE_CHECKING:
    from core.usage import UsageRecorder


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
CREATE TABLE stat_usage_calls AS
    SELECT {CALL_COLUMNS} FROM stat_calls WHERE 0;
CREATE UNIQUE INDEX stat_usage_calls_key ON stat_usage_calls(session_key, seq);
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


def reconcile_usage(connection: sqlite3.Connection, recorder: UsageRecorder) -> None:
    """Apply changed requests atomically; unchanged ledger snapshots write nothing."""
    previous = connection.execute("SELECT source_id, revision FROM stat_usage_state").fetchone()
    try:
        source_id = f"{recorder.database.database_id}:{recorder.projection_epoch}"
        revision = int(previous[1]) if previous is not None and previous[0] == source_id else 0
        latest, records = recorder.read_since(revision)
        reset = previous is not None and (previous[0] != source_id or latest < revision)
        # A restored canonical snapshot can have the same database identity
        # but a shorter revision history. Its projection must follow that source.
        if latest < revision:
            revision = 0
            latest, records = recorder.read_since(0)
    except Exception as error:
        raise UsageSourceError(error) from error
    if reset:
        for table in (
            "stat_usage_state",
            "stat_usage_records",
            "stat_usage_calls",
            "stat_usage_units",
        ):
            connection.execute(f"DELETE FROM {table}")
    if not reset and previous is not None and previous[0] == source_id and previous[1] == latest:
        return
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
            "SELECT session_key, seq FROM stat_usage_records WHERE call_id = ?", (record.id,)
        ).fetchone()
        if prior_call is not None:
            connection.execute(
                "DELETE FROM stat_usage_calls WHERE session_key = ? AND seq = ?", prior_call
            )
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
    connection.execute(
        "INSERT INTO stat_usage_state (source_id, revision) VALUES (?, ?) "
        "ON CONFLICT(source_id) DO UPDATE SET revision = excluded.revision",
        (source_id, latest),
    )
