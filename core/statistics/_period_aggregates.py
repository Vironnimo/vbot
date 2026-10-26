"""Exact first-hour aggregates for a measurement period.

Whole hours still read the maintained cubes. Only the partial first hour is
aggregated from indexed facts, lazily per cube; temporary tables never shadow
the index and the index reader owns their cleanup.
"""

from __future__ import annotations

import sqlite3

from core.statistics._projection import MICROSECONDS_PER_HOUR
from core.statistics._rollups import (
    _usage_select,
    judged_cache_turns_sql,
    run_origin_sql,
    session_origin_sql,
)


def period_aggregate(connection: sqlite3.Connection, table: str, since: int) -> str:
    hour = since // MICROSECONDS_PER_HOUR
    end = (hour + 1) * MICROSECONDS_PER_HOUR
    target = "report_period_" + table.removeprefix("agg_")
    connection.execute(f"CREATE TEMP TABLE {target} AS SELECT * FROM {table} WHERE 0")
    connection.execute(f"INSERT INTO temp.{target} " + _edge_select(table, since, end))
    return f"(SELECT * FROM {table} WHERE hour > {hour} UNION ALL SELECT * FROM temp.{target})"


def _edge_select(table: str, since: int, end: int) -> str:
    where = f"r.instant >= {since} AND r.instant < {end}"
    hour = f"r.instant / {MICROSECONDS_PER_HOUR}"
    if table in {"agg_usage", "agg_first_token"}:
        return _usage_select("stat_usage_records r", where, first_token=table == "agg_first_token")
    if table in {"agg_tools", "agg_tool_latency"}:
        origin = (
            f"CASE WHEN rr.run_id IS NOT NULL THEN {run_origin_sql('s', 'rr.run_kind')} "
            f"ELSE {session_origin_sql('s')} END"
        )
        latency = table == "agg_tool_latency"
        measures = (
            "r.latency_bucket, COUNT(*)"
            if latency
            else "COUNT(*), SUM(r.outcome IS 1), SUM(r.outcome IS 0), "
            "SUM(r.outcome IS NULL), COUNT(r.duration_ms), "
            "COALESCE(SUM(r.duration_ms), 0), MAX(r.duration_ms)"
        )
        return f"""
            SELECT {hour}, r.session_key, {origin}, r.name, {measures}
            FROM stat_tools r
            JOIN stat_sessions s ON s.session_key = r.session_key
            LEFT JOIN stat_run_records rr
                ON rr.session_key = r.session_key AND rr.run_id = r.run_id
            WHERE {where} {"AND r.latency_bucket IS NOT NULL" if latency else ""}
            GROUP BY {hour}, r.session_key, {origin}, r.name
                {", r.latency_bucket" if latency else ""}
        """
    if table == "agg_records":
        return f"""
            SELECT {hour}, r.session_key, r.role, COUNT(*), COUNT(c.seq)
            FROM stat_records r
            LEFT JOIN stat_calls c ON c.session_key = r.session_key AND c.seq = r.seq
                AND c.kind = 0 AND c.visible = 1
            WHERE {where}
            GROUP BY {hour}, r.session_key, r.role
        """
    if table == "agg_cache":
        return _cache_select(since, end)
    raise ValueError(f"Unknown hourly aggregate: {table}")


def _cache_select(since: int, end: int) -> str:
    # A predecessor before the start still explains the first in-window turn.
    # Bound the sequence walk to each affected Session's first/last edge row
    # plus its immediate relevant predecessor, preserving out-of-order times.
    judged = judged_cache_turns_sql(
        """edge_sessions d CROSS JOIN stat_records r
        ON r.session_key = d.session_key AND r.seq <= d.last_seq
        AND r.seq >= COALESCE((SELECT MAX(p.seq) FROM stat_records p
            WHERE p.session_key = d.session_key AND p.seq < d.first_seq
            AND p.role IN ('assistant', 'compaction_checkpoint', 'agent_takeover')),
            d.first_seq)""",
        "1",
        "r.session_key",
    )
    return f"""
        WITH edge_sessions AS (
            SELECT session_key, MIN(seq) AS first_seq, MAX(seq) AS last_seq
            FROM stat_records WHERE instant >= {since} AND instant < {end}
            AND role IN ('assistant', 'compaction_checkpoint', 'agent_takeover')
            GROUP BY session_key
        )
        SELECT t.instant / {MICROSECONDS_PER_HOUR} AS hour, t.unit, COUNT(*),
            SUM(t.input_tokens), SUM(t.cache_read_tokens), SUM(t.cache_write_tokens),
            SUM(t.evaluated), SUM(t.incident), MAX(t.instant)
        FROM ({judged}) t WHERE t.instant >= {since} AND t.instant < {end}
        GROUP BY hour, t.unit
    """
