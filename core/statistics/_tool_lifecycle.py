"""Project canonical Tool invocation revisions, independently of history entries."""

from __future__ import annotations

import sqlite3

from core.sessions import SessionToolInvocationBatch
from core.statistics._projection import latency_bucket, timestamp_instant
from core.statistics._rollups import RollupChanges

_COLUMNS = (
    "session_key, invocation_key, instant, name, status, outcome, error_code, "
    "duration_ms, run_id, latency_bucket"
)


def project_tool_invocations(
    connection: sqlite3.Connection,
    session_key: int,
    batch: SessionToolInvocationBatch,
    changes: RollupChanges,
) -> tuple[set[str], int | None, int | None]:
    """Upsert only changed invocations; retain the old and new aggregate groups.

    A pending, cancelled or interrupted invocation without a saved Result has
    no success/failure evidence. Its Result bucket stays unknown while the
    explicit lifecycle status remains available in the fact table.
    """
    runs: set[str] = set()
    instants: list[int] = []
    for invocation in batch.invocations:
        instant = timestamp_instant(invocation.started_at)
        values = (
            session_key,
            invocation.invocation_key,
            instant,
            invocation.name,
            invocation.status,
            None if invocation.result_ok is None else int(invocation.result_ok),
            invocation.error_code,
            invocation.duration_ms,
            invocation.run_id,
            latency_bucket(invocation.duration_ms),
        )
        previous = connection.execute(
            f"SELECT {_COLUMNS} FROM stat_tools WHERE session_key = ? AND invocation_key = ?",
            (session_key, invocation.invocation_key),
        ).fetchone()
        if previous is not None and tuple(previous) == values:
            continue
        changes.tool_call(connection, session_key, invocation.invocation_key)
        connection.execute(
            f"INSERT OR REPLACE INTO stat_tools ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        changes.tool_call(connection, session_key, invocation.invocation_key)
        if previous is not None and previous[8]:
            runs.add(str(previous[8]))
        if invocation.run_id:
            runs.add(invocation.run_id)
        instants.append(instant)
    return runs, min(instants, default=None), max(instants, default=None)
