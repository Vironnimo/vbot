"""Aggregate tier: per-Run rows and hourly usage and Tool cubes over the facts.

The fact tables stay the source of truth. Reconcile records the units its
changes touch in :class:`RollupChanges`; :func:`maintain_rollups` then deletes
the touched units' aggregate rows and recomputes them from the facts in the
same write transaction, so a reader never sees aggregates that disagree with
the facts. A full rebuild is the same code with every unit touched, and
incremental maintenance yields exactly the rows a full rebuild yields.

Units and their tables:

- A Run (``agg_runs``, one row): its canonical Run record, its own records
  (Model steps, Tool calls, Compactions, errors) and its requests. Requests
  are the ledger requests recorded for its Session address and Run id; a Run
  without any uses the usage saved in its own records instead.
- A Session's Tool calls (``agg_tools`` per hour and ``agg_tool_latency``, a
  duration histogram with ``bucket = floor(4 * log2(duration_ms + 1))``).
- A ledger unit's requests (``agg_usage``, per hour).

Every Run, Tool call and request has one origin; the first matching rule
wins: ``extension`` (an Extension owns the Session, or the request names an
owner), ``subagent`` (a Sub-Agent Run or Session), ``automation`` (cron and
calendar Runs), ``channel``, ``reflection`` (all reflection kinds),
``system``, ``user`` (user Runs and unknown kinds) and ``background`` (a
request without a Run that is not a chat or Compaction request). A request of
an indexed Run takes the Run's origin; any other request in an Extension or
Sub-Agent Session, and a chat or Compaction request in any Session, takes its
Session's (``extension``, ``subagent``, else ``user``); a Tool call takes its
Run's origin, else its Session's.

Money is integer nano-USD: each call's ``amount_usd * 1e9`` rounded half away
from zero, so sums are exact and independent of order.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from core.statistics._projection import CACHE_SQL, MICROSECONDS_PER_HOUR, REASONING_SQL

ROLLUP_SCHEMA = """
CREATE TABLE agg_runs (
    session_key INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    run_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    completion_reason TEXT,
    start_instant INTEGER NOT NULL,
    end_instant INTEGER,
    duration_ms INTEGER,
    iterations INTEGER,
    model_steps INTEGER NOT NULL,
    visible_messages INTEGER NOT NULL,
    user_messages INTEGER NOT NULL,
    first_visible_ms INTEGER,
    tool_calls INTEGER NOT NULL,
    tool_rejected INTEGER NOT NULL,
    tool_ms INTEGER NOT NULL,
    compactions INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    calls INTEGER NOT NULL,
    failed_calls INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    estimated_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    estimated_output_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_input_tokens INTEGER NOT NULL,
    cache_calls INTEGER NOT NULL,
    reported_nusd INTEGER NOT NULL,
    estimated_nusd INTEGER NOT NULL,
    unpriced_calls INTEGER NOT NULL,
    primary_model TEXT,
    models TEXT NOT NULL,
    changed_files INTEGER,
    lines_added INTEGER,
    lines_removed INTEGER,
    PRIMARY KEY (session_key, run_id)
) WITHOUT ROWID;
CREATE INDEX agg_runs_start ON agg_runs(start_instant);
CREATE INDEX agg_runs_origin ON agg_runs(origin, start_instant);
CREATE TABLE agg_usage (
    hour INTEGER NOT NULL,
    unit_key INTEGER NOT NULL,
    origin TEXT NOT NULL,
    model_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    calls INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    estimated_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    estimated_output_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    reasoning_calls INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_input_tokens INTEGER NOT NULL,
    cache_calls INTEGER NOT NULL,
    unreported_calls INTEGER NOT NULL,
    reported_nusd INTEGER NOT NULL,
    reported_calls INTEGER NOT NULL,
    estimated_nusd INTEGER NOT NULL,
    estimated_calls INTEGER NOT NULL,
    unpriced_calls INTEGER NOT NULL,
    retrospective_calls INTEGER NOT NULL,
    uncached_nusd INTEGER NOT NULL,
    uncached_calls INTEGER NOT NULL,
    estimated_token_calls INTEGER NOT NULL,
    PRIMARY KEY (hour, unit_key, origin, model_key, kind, status)
) WITHOUT ROWID;
CREATE INDEX agg_usage_unit ON agg_usage(unit_key);
CREATE TABLE agg_tools (
    hour INTEGER NOT NULL,
    session_key INTEGER NOT NULL,
    origin TEXT NOT NULL,
    name TEXT NOT NULL,
    calls INTEGER NOT NULL,
    accepted INTEGER NOT NULL,
    rejected INTEGER NOT NULL,
    unknown INTEGER NOT NULL,
    duration_calls INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    max_ms INTEGER,
    PRIMARY KEY (hour, session_key, origin, name)
) WITHOUT ROWID;
CREATE INDEX agg_tools_session ON agg_tools(session_key);
CREATE TABLE agg_tool_latency (
    hour INTEGER NOT NULL,
    session_key INTEGER NOT NULL,
    origin TEXT NOT NULL,
    name TEXT NOT NULL,
    bucket INTEGER NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (hour, session_key, origin, name, bucket)
) WITHOUT ROWID;
CREATE INDEX agg_tool_latency_session ON agg_tool_latency(session_key);
"""

ROLLUP_TABLES = ("agg_runs", "agg_usage", "agg_tools", "agg_tool_latency")

ORIGINS = (
    "extension",
    "subagent",
    "automation",
    "channel",
    "reflection",
    "system",
    "user",
    "background",
)

# One Session address as the index stores it: project id ("" for none), Agent, Session.
SessionKey = tuple[str, str, str]


@dataclass
class RollupChanges:
    """The aggregate units one reconcile touched.

    ``rebuild`` recomputes every aggregate. ``sessions`` recompute a Session's
    Runs and Tools whole (also after its removal); ``addresses`` recompute the
    ledger units at a Session address, whose requests take their origin from
    that Session. ``runs`` are ``(session_key, run_id)``; ``units`` are ledger
    unit keys; ``unit_runs`` are ``(unit_key, run_id)`` of changed requests,
    which recompute the Run at that unit's address.
    """

    rebuild: bool = False
    sessions: set[int] = field(default_factory=set)
    addresses: set[SessionKey] = field(default_factory=set)
    runs: set[tuple[int, str]] = field(default_factory=set)
    tool_sessions: set[int] = field(default_factory=set)
    units: set[int] = field(default_factory=set)
    unit_runs: set[tuple[int, str]] = field(default_factory=set)

    def session(self, session_key: int, address: SessionKey) -> None:
        """A Session's facts were replaced, added or removed, or its flags changed."""
        self.sessions.add(session_key)
        self.addresses.add(address)

    def session_runs(self, session_key: int, run_ids: Iterable[str]) -> None:
        self.runs.update((session_key, run_id) for run_id in run_ids)

    def usage_call(self, unit_key: int, run_id: str | None) -> None:
        """A ledger request of ``unit_key`` and ``run_id`` was added, changed or moved away."""
        self.units.add(unit_key)
        if run_id:
            self.unit_runs.add((unit_key, run_id))

    def repriced(
        self,
        session_calls: Iterable[tuple[int, str | None]],
        usage_calls: Iterable[tuple[int, str | None]],
    ) -> None:
        """Calls whose projected cost changed, by Session or unit key and Run id."""
        self.runs.update((key, run_id) for key, run_id in session_calls if run_id)
        for unit_key, run_id in usage_calls:
            self.usage_call(unit_key, run_id)

    def touched(self) -> bool:
        return self.rebuild or any(
            (
                self.sessions,
                self.addresses,
                self.runs,
                self.tool_sessions,
                self.units,
                self.unit_runs,
            )
        )


# Work tables of one maintenance pass, each keyed by its columns.
_WORK_TABLES = {
    "rollup_sessions": "session_key INTEGER",
    "rollup_runs": "session_key INTEGER, run_id TEXT",
    "rollup_tool_sessions": "session_key INTEGER",
    "rollup_units": "unit_key INTEGER",
    "rollup_addresses": "project_id TEXT, agent_id TEXT, session_id TEXT",
    "rollup_unit_runs": "unit_key INTEGER, run_id TEXT",
}


def maintain_rollups(connection: sqlite3.Connection, changes: RollupChanges) -> None:
    """Recompute the aggregates of every unit ``changes`` touched; else write nothing."""
    if not changes.touched():
        return
    for name, columns in _WORK_TABLES.items():
        connection.execute(f"DROP TABLE IF EXISTS temp.{name}")
        connection.execute(
            f"CREATE TEMP TABLE {name} ({columns}, PRIMARY KEY ({_names(columns)})) WITHOUT ROWID"
        )
    if changes.rebuild:
        for table in ROLLUP_TABLES:
            connection.execute(f"DELETE FROM {table}")
        connection.execute(
            "INSERT INTO temp.rollup_runs SELECT session_key, run_id FROM stat_run_records"
        )
        connection.execute(
            "INSERT INTO temp.rollup_tool_sessions SELECT session_key FROM stat_sessions"
        )
        connection.execute("INSERT INTO temp.rollup_units SELECT session_key FROM stat_usage_units")
    else:
        _collect(connection, changes)
    _recompute_runs(connection)
    _recompute_tools(connection)
    _recompute_usage(connection)
    for name in _WORK_TABLES:
        connection.execute(f"DROP TABLE temp.{name}")


def _names(columns: str) -> str:
    return ", ".join(column.split()[0] for column in columns.split(","))


def _collect(connection: sqlite3.Connection, changes: RollupChanges) -> None:
    """Resolve the touched units and delete their current aggregate rows."""

    def insert(name: str, rows: Iterable[tuple[Any, ...]]) -> None:
        count = len(_WORK_TABLES[name].split(","))
        connection.executemany(
            f"INSERT OR IGNORE INTO temp.{name} VALUES ({', '.join('?' * count)})", rows
        )

    insert("rollup_sessions", ((key,) for key in changes.sessions))
    insert("rollup_runs", changes.runs)
    insert("rollup_tool_sessions", ((key,) for key in changes.sessions | changes.tool_sessions))
    insert("rollup_units", ((key,) for key in changes.units))
    insert("rollup_addresses", changes.addresses)
    insert("rollup_unit_runs", changes.unit_runs)
    # Whole Sessions recompute every current Run; changed requests recompute
    # the Run of their unit's Session; a changed Session recomputes the
    # ledger units at its address.
    connection.execute(
        "INSERT OR IGNORE INTO temp.rollup_runs SELECT rr.session_key, rr.run_id "
        "FROM temp.rollup_sessions d CROSS JOIN stat_run_records rr "
        "ON rr.session_key = d.session_key"
    )
    connection.execute(
        "INSERT OR IGNORE INTO temp.rollup_runs SELECT s.session_key, d.run_id "
        "FROM temp.rollup_unit_runs d CROSS JOIN stat_usage_units u ON u.session_key = d.unit_key "
        "CROSS JOIN stat_sessions s ON s.project_id = u.project_id "
        "AND s.agent_id = u.agent_id AND s.session_id = u.session_id"
    )
    connection.execute(
        "INSERT OR IGNORE INTO temp.rollup_units SELECT u.session_key "
        "FROM temp.rollup_addresses a CROSS JOIN stat_usage_units u "
        "ON u.project_id = a.project_id AND u.agent_id = a.agent_id "
        "AND u.session_id = a.session_id"
    )
    connection.execute(
        "DELETE FROM agg_runs WHERE session_key IN (SELECT session_key FROM temp.rollup_sessions)"
    )
    connection.execute(
        "DELETE FROM agg_runs WHERE (session_key, run_id) IN "
        "(SELECT session_key, run_id FROM temp.rollup_runs)"
    )
    for table in ("agg_tools", "agg_tool_latency"):
        connection.execute(
            f"DELETE FROM {table} WHERE session_key IN "
            "(SELECT session_key FROM temp.rollup_tool_sessions)"
        )
    connection.execute(
        "DELETE FROM agg_usage WHERE unit_key IN (SELECT unit_key FROM temp.rollup_units)"
    )


def _run_origin(session: str, run_kind: str) -> str:
    return (
        f"CASE WHEN {session}.owner_name <> '' THEN 'extension' "
        f"WHEN {run_kind} = 'subagent' OR {session}.is_subagent = 1 THEN 'subagent' "
        f"WHEN {run_kind} IN ('cron', 'calendar') THEN 'automation' "
        f"WHEN {run_kind} = 'channel' THEN 'channel' "
        f"WHEN {run_kind} IN ('reflection', 'memory_reflection', 'skill_reflection') "
        "THEN 'reflection' "
        f"WHEN {run_kind} = 'system' THEN 'system' "
        "ELSE 'user' END"
    )


def _session_origin(session: str) -> str:
    return (
        f"CASE WHEN {session}.owner_name <> '' THEN 'extension' "
        f"WHEN {session}.is_subagent = 1 THEN 'subagent' ELSE 'user' END"
    )


_HOUR = f"r.instant / {MICROSECONDS_PER_HOUR}"
_NUSD = "CAST(ROUND(c.cost_usd * 1000000000) AS INTEGER)"
# Catalog-priced calls that report usage but no cache counter.
_UNCACHED = "(c.cost_source = 2 AND c.input_tokens IS NOT NULL AND c.has_cache = 0)"
# Per-call measures shared by Run and usage aggregation, over calls ``c``.
_CALL_MEASURES = f"""
    COUNT(*),
    SUM(COALESCE(c.input_tokens, 0)),
    SUM(CASE WHEN c.input_estimated = 1 THEN COALESCE(c.input_tokens, 0) ELSE 0 END),
    SUM(COALESCE(c.output_tokens, 0)),
    SUM(CASE WHEN c.output_estimated = 1 THEN COALESCE(c.output_tokens, 0) ELSE 0 END),
    SUM(CASE WHEN {REASONING_SQL} THEN c.reasoning_tokens ELSE 0 END),
    SUM(CASE WHEN {CACHE_SQL} THEN COALESCE(c.cache_read_tokens, 0) ELSE 0 END),
    SUM(CASE WHEN {CACHE_SQL} THEN COALESCE(c.cache_write_tokens, 0) ELSE 0 END),
    SUM(CASE WHEN {CACHE_SQL} THEN c.input_tokens ELSE 0 END),
    SUM({CACHE_SQL}),
    SUM(CASE WHEN c.cost_source = 1 THEN {_NUSD} ELSE 0 END),
    SUM(CASE WHEN c.cost_source = 2 THEN {_NUSD} ELSE 0 END),
    SUM(c.cost_source = 0)
"""
# Positions in a per-Model row: key, run id, Model, has Model, first use, failed calls.
_MODEL_KEY, _HAS_MODEL, _FIRST_USE, _FAILED, _MEASURES = 2, 3, 4, 5, 6
_INPUT = _MEASURES + 1

_RUN_COLUMNS = (
    "session_key, run_id, origin, run_kind, status, completion_reason, start_instant, "
    "end_instant, duration_ms, iterations, model_steps, visible_messages, user_messages, "
    "first_visible_ms, tool_calls, tool_rejected, tool_ms, compactions, errors, calls, "
    "failed_calls, input_tokens, estimated_input_tokens, output_tokens, "
    "estimated_output_tokens, reasoning_tokens, cache_read_tokens, cache_write_tokens, "
    "cache_input_tokens, cache_calls, reported_nusd, estimated_nusd, unpriced_calls, "
    "primary_model, models, changed_files, lines_added, lines_removed"
)
_NO_CALLS = (0,) * 14


def _recompute_runs(connection: sqlite3.Connection) -> None:
    runs = connection.execute(
        f"""
        SELECT d.session_key, d.run_id, {_run_origin("s", "rr.run_kind")}, rr.run_kind,
            rr.status, rr.completion_reason, rr.start_instant, rr.end_instant, rr.duration_ms,
            rr.iteration_count, rr.changed_files, rr.lines_added, rr.lines_removed
        FROM temp.rollup_runs d
        CROSS JOIN stat_run_records rr ON rr.session_key = d.session_key AND rr.run_id = d.run_id
        CROSS JOIN stat_sessions s ON s.session_key = d.session_key
        """
    ).fetchall()
    if not runs:
        return
    steps = {
        (row[0], row[1]): row[2:]
        for row in connection.execute(
            """
            SELECT d.session_key, d.run_id,
                SUM(r.role = 'assistant'), COUNT(v.seq), MIN(v.instant),
                SUM(r.role = 'user'), COUNT(t.seq), SUM(t.outcome IS 0),
                COALESCE(SUM(t.duration_ms), 0), SUM(r.role = 'compaction_checkpoint'),
                SUM(r.role = 'error')
            FROM temp.rollup_runs d
            CROSS JOIN stat_records r ON r.session_key = d.session_key AND r.run_id = d.run_id
            LEFT JOIN stat_calls v ON v.session_key = r.session_key AND v.seq = r.seq
                AND v.kind = 0 AND v.visible = 1
            LEFT JOIN stat_tools t ON t.session_key = r.session_key AND t.seq = r.seq
            GROUP BY d.session_key, d.run_id
            """
        )
    }
    ledger = _model_rows(
        connection,
        f"""
        SELECT d.session_key, d.run_id, c.model_key, c.has_model, MIN(c.instant),
            SUM(r.status <> 'completed'), {_CALL_MEASURES}
        FROM temp.rollup_runs d
        CROSS JOIN stat_sessions s ON s.session_key = d.session_key
        CROSS JOIN stat_usage_units u ON u.project_id = s.project_id
            AND u.agent_id = s.agent_id AND u.session_id = s.session_id
        CROSS JOIN stat_usage_records r ON r.session_key = u.session_key AND r.run_id = d.run_id
        CROSS JOIN stat_usage_calls c ON c.session_key = r.session_key AND c.seq = r.seq
        GROUP BY d.session_key, d.run_id, c.model_key, c.has_model
        """,
    )
    saved = _model_rows(
        connection,
        f"""
        SELECT d.session_key, d.run_id, c.model_key, c.has_model, MIN(c.instant), 0,
            {_CALL_MEASURES}
        FROM temp.rollup_runs d
        CROSS JOIN stat_records r ON r.session_key = d.session_key AND r.run_id = d.run_id
        CROSS JOIN stat_calls c ON c.session_key = r.session_key AND c.seq = r.seq
        GROUP BY d.session_key, d.run_id, c.model_key, c.has_model
        """,
    )
    rows = []
    for run in runs:
        key = (run[0], run[1])
        start_instant = run[6]
        (
            model_steps,
            visible_messages,
            first_visible,
            user_messages,
            tool_calls,
            tool_rejected,
            tool_ms,
            compactions,
            errors,
        ) = steps.get(key, (0, 0, None, 0, 0, 0, 0, 0, 0))
        # Ledger requests are authoritative; saved usage covers Runs without any.
        models = ledger.get(key) or saved.get(key, [])
        rows.append(
            (
                *run[:9],
                run[9],
                model_steps,
                visible_messages,
                user_messages,
                None if first_visible is None else max(0, (first_visible - start_instant) // 1000),
                tool_calls,
                tool_rejected,
                tool_ms,
                compactions,
                errors,
                *_run_usage(models),
                *run[10:],
            )
        )
    connection.executemany(
        f"INSERT INTO agg_runs ({_RUN_COLUMNS}) "
        f"VALUES ({', '.join('?' for _column in _RUN_COLUMNS.split(','))})",
        rows,
    )


def _model_rows(
    connection: sqlite3.Connection, sql: str
) -> dict[tuple[int, str], list[tuple[Any, ...]]]:
    grouped: dict[tuple[int, str], list[tuple[Any, ...]]] = {}
    for row in connection.execute(sql):
        grouped.setdefault((row[0], row[1]), []).append(tuple(row))
    return grouped


def _run_usage(models: list[tuple[Any, ...]]) -> tuple[Any, ...]:
    """Sum a Run's per-Model request rows; name its Models by first use.

    The primary Model is the one with the most input tokens (ties by name).
    """
    if not models:
        return (*_NO_CALLS, None, "[]")
    sums = [sum(row[index] for row in models) for index in range(_MEASURES, len(models[0]))]
    failed = sum(row[_FAILED] for row in models)
    named = [row for row in models if row[_HAS_MODEL]]
    ordered = sorted(named, key=lambda row: (row[_FIRST_USE], row[_MODEL_KEY]))
    primary = min(named, key=lambda row: (-row[_INPUT], row[_MODEL_KEY]), default=None)
    return (
        sums[0],
        failed,
        *sums[1:],
        None if primary is None else primary[_MODEL_KEY],
        json.dumps(list(dict.fromkeys(row[_MODEL_KEY] for row in ordered)), separators=(",", ":")),
    )


def _recompute_tools(connection: sqlite3.Connection) -> None:
    origin = (
        f"CASE WHEN rr.run_id IS NOT NULL THEN {_run_origin('s', 'rr.run_kind')} "
        f"ELSE {_session_origin('s')} END"
    )
    source = """
        FROM temp.rollup_tool_sessions d
        CROSS JOIN stat_sessions s ON s.session_key = d.session_key
        CROSS JOIN stat_tools r ON r.session_key = d.session_key
        LEFT JOIN stat_run_records rr ON rr.session_key = r.session_key AND rr.run_id = r.run_id
    """
    connection.execute(
        f"""
        INSERT INTO agg_tools (
            hour, session_key, origin, name, calls, accepted, rejected, unknown,
            duration_calls, duration_ms, max_ms
        )
        SELECT {_HOUR} AS hour, r.session_key, {origin} AS origin, r.name, COUNT(*),
            SUM(r.outcome IS 1), SUM(r.outcome IS 0), SUM(r.outcome IS NULL),
            COUNT(r.duration_ms), COALESCE(SUM(r.duration_ms), 0), MAX(r.duration_ms)
        {source}
        GROUP BY hour, r.session_key, origin, r.name
        """
    )
    connection.execute(
        f"""
        INSERT INTO agg_tool_latency (hour, session_key, origin, name, bucket, count)
        SELECT {_HOUR} AS hour, r.session_key, {origin} AS origin, r.name, r.latency_bucket,
            COUNT(*)
        {source}
        WHERE r.latency_bucket IS NOT NULL
        GROUP BY hour, r.session_key, origin, r.name, r.latency_bucket
        """
    )


def _recompute_usage(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        INSERT INTO agg_usage (
            hour, unit_key, origin, model_key, kind, status, calls, input_tokens,
            estimated_input_tokens, output_tokens, estimated_output_tokens, reasoning_tokens,
            cache_read_tokens, cache_write_tokens, cache_input_tokens, cache_calls,
            reported_nusd, estimated_nusd, unpriced_calls, reasoning_calls, unreported_calls,
            reported_calls, estimated_calls, retrospective_calls, uncached_nusd, uncached_calls,
            estimated_token_calls
        )
        SELECT {_HOUR} AS hour, r.session_key,
            CASE WHEN u.owner_name <> '' THEN 'extension'
                WHEN rr.run_id IS NOT NULL THEN {_run_origin("s", "rr.run_kind")}
                WHEN s.owner_name <> '' OR s.is_subagent = 1
                    OR c.purpose IN ('chat', 'compaction') THEN {_session_origin("s")}
                ELSE 'background' END AS origin,
            c.model_key, c.purpose, r.status,
            {_CALL_MEASURES},
            SUM({REASONING_SQL}),
            SUM(c.input_tokens IS NULL OR c.output_tokens IS NULL),
            SUM(c.cost_source = 1),
            SUM(c.cost_source = 2),
            SUM(c.cost_source = 2 AND c.retrospective = 1),
            SUM(CASE WHEN {_UNCACHED} THEN {_NUSD} ELSE 0 END),
            SUM({_UNCACHED}),
            SUM((c.input_estimated = 1 OR c.output_estimated = 1)
                AND c.input_tokens IS NOT NULL AND c.output_tokens IS NOT NULL)
        FROM temp.rollup_units d
        CROSS JOIN stat_usage_units u ON u.session_key = d.unit_key
        CROSS JOIN stat_usage_records r ON r.session_key = d.unit_key
        CROSS JOIN stat_usage_calls c ON c.session_key = r.session_key AND c.seq = r.seq
        LEFT JOIN stat_sessions s ON s.project_id = u.project_id AND s.agent_id = u.agent_id
            AND s.session_id = u.session_id
        LEFT JOIN stat_run_records rr ON rr.session_key = s.session_key AND rr.run_id = r.run_id
        GROUP BY hour, r.session_key, origin, c.model_key, c.purpose, r.status
        """
    )
