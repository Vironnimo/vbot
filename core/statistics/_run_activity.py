"""Runs overlapping a time interval, from the per-Run aggregates."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime

from core.statistics._projection import datetime_instant
from core.statistics._sections.common import LiveSession
from core.statistics._sections.window import instant_timestamp
from core.statistics.report import RunActivity


def load_run_activity(
    connection: sqlite3.Connection,
    sessions: Mapping[int, LiveSession],
    *,
    since: datetime,
    until: datetime,
    limit: int,
) -> tuple[int, list[RunActivity]]:
    """Return the overlap count and the latest-started listed Runs overlapping the interval.

    A Run executes from its start to its end; a Run still running has no end and
    overlaps every interval ending after its start. Requests are the Run's own
    (``agg_runs``), across the whole Run rather than only the interval.
    """
    low, high = datetime_instant(since), datetime_instant(until)
    connection.execute("DROP TABLE IF EXISTS temp.activity_sessions")
    connection.execute("CREATE TEMP TABLE activity_sessions (session_key INTEGER PRIMARY KEY)")
    try:
        connection.executemany(
            "INSERT INTO temp.activity_sessions VALUES (?)", [(key,) for key in sessions]
        )
        overlapping = """
            FROM agg_runs r JOIN temp.activity_sessions l ON l.session_key = r.session_key
            WHERE r.start_instant <= :high AND (r.end_instant IS NULL OR r.end_instant >= :low)
        """
        params = {"low": low, "high": high, "limit": limit}
        total = int(connection.execute(f"SELECT COUNT(*) {overlapping}", params).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT r.session_key, r.run_id, r.status, r.start_instant, r.end_instant,
                r.duration_ms, r.models, r.tool_calls, r.input_tokens,
                r.estimated_input_tokens, r.output_tokens, r.estimated_output_tokens
            {overlapping}
            ORDER BY r.start_instant DESC, r.session_key, r.run_id
            LIMIT :limit
            """,
            params,
        ).fetchall()
    finally:
        connection.execute("DROP TABLE temp.activity_sessions")
    runs = []
    for (
        session_key,
        run_id,
        status,
        start,
        end,
        duration,
        models,
        tool_calls,
        input_tokens,
        estimated_input,
        output_tokens,
        estimated_output,
    ) in rows:
        session = sessions[session_key]
        runs.append(
            RunActivity(
                agent_id=session.display_key,
                session_id=session.address.session_id,
                session_title=session.title,
                run_id=run_id,
                status=status,
                started_at=instant_timestamp(start),
                completed_at=None if end is None else instant_timestamp(end),
                duration_ms=duration,
                models=json.loads(models),
                tool_calls=tool_calls,
                measured_input_tokens=input_tokens - estimated_input,
                measured_output_tokens=output_tokens - estimated_output,
                estimated_input_tokens=estimated_input,
                estimated_output_tokens=estimated_output,
            )
        )
    return total, runs
