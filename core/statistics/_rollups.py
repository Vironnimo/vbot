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
- A Session's per-hour cubes over its own records: Tool calls (``agg_tools``
  and ``agg_tool_latency``, a duration histogram with
  ``bucket = floor(4 * log2(duration_ms + 1))``), records by role with their
  visible chat steps (``agg_records``), and prompt-cache turns (``agg_cache``
  per hour, ``agg_cache_breaks`` per suspected break). Each cache-reporting
  turn is judged against its predecessor in the whole Session, so the
  judgement does not depend on a report window.
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
    kinds TEXT NOT NULL,
    changed_files INTEGER,
    lines_added INTEGER,
    lines_removed INTEGER,
    PRIMARY KEY (session_key, run_id)
) WITHOUT ROWID;
CREATE INDEX agg_runs_start ON agg_runs(start_instant);
CREATE INDEX agg_runs_origin ON agg_runs(origin, start_instant);
CREATE TABLE agg_run_models (
    session_key INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    model_key TEXT NOT NULL,
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
    PRIMARY KEY (session_key, run_id, model_key)
) WITHOUT ROWID;
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
CREATE TABLE agg_records (
    hour INTEGER NOT NULL,
    session_key INTEGER NOT NULL,
    role TEXT NOT NULL,
    records INTEGER NOT NULL,
    visible_steps INTEGER NOT NULL,
    PRIMARY KEY (hour, session_key, role)
) WITHOUT ROWID;
CREATE INDEX agg_records_session ON agg_records(session_key);
CREATE TABLE agg_cache (
    hour INTEGER NOT NULL,
    session_key INTEGER NOT NULL,
    turns INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    evaluated_turns INTEGER NOT NULL,
    suspected_turns INTEGER NOT NULL,
    last_instant INTEGER NOT NULL,
    PRIMARY KEY (hour, session_key)
) WITHOUT ROWID;
CREATE INDEX agg_cache_session ON agg_cache(session_key);
CREATE TABLE agg_cache_breaks (
    session_key INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    instant INTEGER NOT NULL,
    model_key TEXT NOT NULL,
    previous_input_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    PRIMARY KEY (session_key, seq)
) WITHOUT ROWID;
"""

ROLLUP_TABLES = (
    "agg_runs",
    "agg_run_models",
    "agg_usage",
    "agg_tools",
    "agg_tool_latency",
    "agg_records",
    "agg_cache",
    "agg_cache_breaks",
)
# Aggregates recomputed whole per Session from its own records.
_SESSION_CUBES = ("agg_tools", "agg_tool_latency", "agg_records", "agg_cache", "agg_cache_breaks")

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
    Runs and per-Session cubes whole (also after its removal);
    ``fact_sessions`` recompute only the cubes of a Session whose records grew
    or whose Run identities changed; ``addresses`` recompute the
    ledger units at a Session address, whose requests take their origin from
    that Session. ``runs`` are ``(session_key, run_id)``; ``units`` are ledger
    unit keys; ``unit_runs`` are ``(unit_key, run_id)`` of changed requests,
    which recompute the Run at that unit's address.
    """

    rebuild: bool = False
    sessions: set[int] = field(default_factory=set)
    addresses: set[SessionKey] = field(default_factory=set)
    runs: set[tuple[int, str]] = field(default_factory=set)
    fact_sessions: set[int] = field(default_factory=set)
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
                self.fact_sessions,
                self.units,
                self.unit_runs,
            )
        )


# Work tables of one maintenance pass, each keyed by its columns.
_WORK_TABLES = {
    "rollup_sessions": "session_key INTEGER",
    "rollup_runs": "session_key INTEGER, run_id TEXT",
    "rollup_fact_sessions": "session_key INTEGER",
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
            "INSERT INTO temp.rollup_fact_sessions SELECT session_key FROM stat_sessions"
        )
        connection.execute("INSERT INTO temp.rollup_units SELECT session_key FROM stat_usage_units")
    else:
        _collect(connection, changes)
    _recompute_runs(connection)
    _recompute_tools(connection)
    _recompute_records(connection)
    _recompute_cache(connection)
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
    insert("rollup_fact_sessions", ((key,) for key in changes.sessions | changes.fact_sessions))
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
    for table in ("agg_runs", "agg_run_models"):
        connection.execute(
            f"DELETE FROM {table} WHERE session_key IN "
            "(SELECT session_key FROM temp.rollup_sessions)"
        )
        connection.execute(
            f"DELETE FROM {table} WHERE (session_key, run_id) IN "
            "(SELECT session_key, run_id FROM temp.rollup_runs)"
        )
    for table in _SESSION_CUBES:
        connection.execute(
            f"DELETE FROM {table} WHERE session_key IN "
            "(SELECT session_key FROM temp.rollup_fact_sessions)"
        )
    connection.execute(
        "DELETE FROM agg_usage WHERE unit_key IN (SELECT unit_key FROM temp.rollup_units)"
    )


def run_origin_sql(session: str, run_kind: str) -> str:
    """The origin of a Run of ``run_kind`` in the ``stat_sessions`` row ``session``."""
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


def session_origin_sql(session: str) -> str:
    """The origin of Session-level work in the ``stat_sessions`` row ``session``."""
    return (
        f"CASE WHEN {session}.owner_name <> '' THEN 'extension' "
        f"WHEN {session}.is_subagent = 1 THEN 'subagent' ELSE 'user' END"
    )


# Prompt-cache-break heuristic (best-effort, derived). A measured turn is
# evaluated against its predecessor only when no legitimate prefix change
# explains a cache miss; the thresholds keep false positives low rather than
# catching every break.
CACHE_BREAK_READ_RATIO = 0.5
"""A cache read below this share of the previous turn's prompt is a suspected break."""
CACHE_BREAK_MAX_GAP_SECONDS = 300
"""Provider prompt caches expire after ~5 idle minutes; longer gaps are expected misses."""
CACHE_BREAK_MIN_PREVIOUS_INPUT_TOKENS = 2048
"""Below provider minimum cacheable prompt sizes an empty cache read is legitimate."""
_MICROSECONDS_PER_SECOND = 1_000_000


def judged_cache_turns_sql(source: str, where: str, unit: str) -> str:
    """Select every measured, cache-reporting Assistant turn of ``source`` with its judgement.

    ``source`` is a FROM clause yielding ``stat_records r`` rows, ``where``
    filters them and ``unit`` names the partition the turns are ordered in.
    Only measured Assistant turns set an expectation baseline; a Compaction
    checkpoint, an Agent takeover, a turn without Usage or an estimated turn
    clears it. A cache-reporting turn is evaluated against the immediately
    preceding measured turn when that turn also reported cache fields, used
    the same Model, sent a prompt of at least the minimum cacheable size and
    ran within the cache lifetime; a read below the break ratio of the
    previous prompt is a suspected break (``incident``). Columns: ``unit, seq,
    timestamp, instant, model_key, input_tokens, cache_read_tokens,
    cache_write_tokens, previous_input_tokens, evaluated, incident``.
    """
    return f"""
        WITH stream AS (
            SELECT {unit} AS unit, r.seq, r.timestamp, r.instant, c.model_key, c.has_cache,
                COALESCE(c.input_tokens, 0) AS input_tokens,
                COALESCE(c.cache_read_tokens, 0) AS cache_read_tokens,
                COALESCE(c.cache_write_tokens, 0) AS cache_write_tokens,
                (r.role = 'assistant' AND c.has_usage = 1
                    AND c.input_estimated = 0 AND c.output_estimated = 0) AS measured
            FROM {source}
            LEFT JOIN stat_calls c
                ON c.session_key = r.session_key AND c.seq = r.seq AND c.kind = 0
            WHERE {where}
                AND r.role IN ('assistant', 'compaction_checkpoint', 'agent_takeover')
        ),
        turns AS (
            SELECT unit, seq, timestamp, instant, model_key, has_cache, input_tokens,
                cache_read_tokens, cache_write_tokens, measured,
                LAG(measured) OVER turn_order AS previous_measured,
                LAG(has_cache) OVER turn_order AS previous_has_cache,
                LAG(model_key) OVER turn_order AS previous_model_key,
                LAG(input_tokens) OVER turn_order AS previous_input_tokens,
                LAG(instant) OVER turn_order AS previous_instant
            FROM stream
            WINDOW turn_order AS (PARTITION BY unit ORDER BY seq)
        ),
        judged AS (
            SELECT unit, seq, timestamp, instant, model_key, input_tokens,
                cache_read_tokens, cache_write_tokens, previous_input_tokens, COALESCE(
                previous_measured = 1
                AND previous_has_cache = 1
                AND previous_model_key = model_key
                AND previous_input_tokens >= {CACHE_BREAK_MIN_PREVIOUS_INPUT_TOKENS}
                AND instant - previous_instant
                    BETWEEN 0 AND {CACHE_BREAK_MAX_GAP_SECONDS * _MICROSECONDS_PER_SECOND},
                0
            ) AS evaluated
            FROM turns
            WHERE measured = 1 AND has_cache = 1
        )
        SELECT unit, seq, timestamp, instant, model_key, input_tokens, cache_read_tokens,
            cache_write_tokens, previous_input_tokens, evaluated,
            (evaluated = 1
                AND cache_read_tokens < previous_input_tokens * {CACHE_BREAK_READ_RATIO}
            ) AS incident
        FROM judged
    """


# The origin of a ledger request: ``r`` its record, ``c`` its call, ``u`` its
# unit, and the LEFT JOINed ``s`` (the Session at the unit's address) and
# ``rr`` (that Session's Run record of the request's Run id).
USAGE_ORIGIN_SQL = f"""CASE WHEN u.owner_name <> '' THEN 'extension'
    WHEN rr.run_id IS NOT NULL THEN {run_origin_sql("s", "rr.run_kind")}
    WHEN s.owner_name <> '' OR s.is_subagent = 1
        OR c.purpose IN ('chat', 'compaction') THEN {session_origin_sql("s")}
    ELSE 'background' END"""
# A failed attempt: a finished ledger request that did not complete.
FAILED_STATUS_SQL = "r.status NOT IN ('completed', 'started')"
_HOUR = f"r.instant / {MICROSECONDS_PER_HOUR}"
_NUSD = "CAST(ROUND(c.cost_usd * 1000000000) AS INTEGER)"
# Catalog-priced calls that report usage but no cache counter.
_UNCACHED = "(c.cost_source = 2 AND c.input_tokens IS NOT NULL AND c.has_cache = 0)"
# The usage measures of ``agg_usage`` and ``agg_run_models`` rows, in report
# order. ``failed_calls`` is a column of ``agg_run_models`` only: usage rows
# keep the request status instead.
USAGE_MEASURES = (
    "calls",
    "failed_calls",
    "input_tokens",
    "estimated_input_tokens",
    "output_tokens",
    "estimated_output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_input_tokens",
    "cache_calls",
    "unreported_calls",
    "reported_nusd",
    "reported_calls",
    "estimated_nusd",
    "estimated_calls",
    "unpriced_calls",
    "retrospective_calls",
    "uncached_nusd",
    "uncached_calls",
    "estimated_token_calls",
)
# Each measure's sum over calls ``c`` of requests ``r``.
_MEASURE_SQL = {
    "calls": "COUNT(*)",
    "failed_calls": f"SUM({FAILED_STATUS_SQL})",
    "input_tokens": "SUM(COALESCE(c.input_tokens, 0))",
    "estimated_input_tokens": (
        "SUM(CASE WHEN c.input_estimated = 1 THEN COALESCE(c.input_tokens, 0) ELSE 0 END)"
    ),
    "output_tokens": "SUM(COALESCE(c.output_tokens, 0))",
    "estimated_output_tokens": (
        "SUM(CASE WHEN c.output_estimated = 1 THEN COALESCE(c.output_tokens, 0) ELSE 0 END)"
    ),
    "reasoning_tokens": f"SUM(CASE WHEN {REASONING_SQL} THEN c.reasoning_tokens ELSE 0 END)",
    "cache_read_tokens": (
        f"SUM(CASE WHEN {CACHE_SQL} THEN COALESCE(c.cache_read_tokens, 0) ELSE 0 END)"
    ),
    "cache_write_tokens": (
        f"SUM(CASE WHEN {CACHE_SQL} THEN COALESCE(c.cache_write_tokens, 0) ELSE 0 END)"
    ),
    "cache_input_tokens": f"SUM(CASE WHEN {CACHE_SQL} THEN c.input_tokens ELSE 0 END)",
    "cache_calls": f"SUM({CACHE_SQL})",
    "unreported_calls": "SUM(c.input_tokens IS NULL OR c.output_tokens IS NULL)",
    "reported_nusd": f"SUM(CASE WHEN c.cost_source = 1 THEN {_NUSD} ELSE 0 END)",
    "reported_calls": "SUM(c.cost_source = 1)",
    "estimated_nusd": f"SUM(CASE WHEN c.cost_source = 2 THEN {_NUSD} ELSE 0 END)",
    "estimated_calls": "SUM(c.cost_source = 2)",
    "unpriced_calls": "SUM(c.cost_source = 0)",
    "retrospective_calls": "SUM(c.cost_source = 2 AND c.retrospective = 1)",
    "uncached_nusd": f"SUM(CASE WHEN {_UNCACHED} THEN {_NUSD} ELSE 0 END)",
    "uncached_calls": f"SUM({_UNCACHED})",
    "estimated_token_calls": (
        "SUM((c.input_estimated = 1 OR c.output_estimated = 1) "
        "AND c.input_tokens IS NOT NULL AND c.output_tokens IS NOT NULL)"
    ),
    "reasoning_calls": f"SUM({REASONING_SQL})",
}
_USAGE_CUBE_MEASURES = (
    *(name for name in USAGE_MEASURES if name != "failed_calls"),
    "reasoning_calls",
)
# A per-Model and purpose row of a Run: key, run id, Model, has Model, purpose,
# first use, then ``USAGE_MEASURES``.
_MODEL_KEY, _HAS_MODEL, _PURPOSE, _FIRST_USE, _MEASURES = 2, 3, 4, 5, 6
_MEASURE = {name: _MEASURES + position for position, name in enumerate(USAGE_MEASURES)}
_RUN_MODEL_SQL = ", ".join(_MEASURE_SQL[name] for name in USAGE_MEASURES)
# Saved usage has no request status, so it records no failed attempts.
_SAVED_RUN_MODEL_SQL = ", ".join(
    "0" if name == "failed_calls" else _MEASURE_SQL[name] for name in USAGE_MEASURES
)
# The request measures ``agg_runs`` keeps, in its column order.
_RUN_MEASURES = (
    "calls",
    "failed_calls",
    "input_tokens",
    "estimated_input_tokens",
    "output_tokens",
    "estimated_output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_input_tokens",
    "cache_calls",
    "reported_nusd",
    "estimated_nusd",
    "unpriced_calls",
)

_RUN_COLUMNS = (
    "session_key, run_id, origin, run_kind, status, completion_reason, start_instant, "
    "end_instant, duration_ms, iterations, model_steps, visible_messages, user_messages, "
    "first_visible_ms, tool_calls, tool_rejected, tool_ms, compactions, errors, calls, "
    "failed_calls, input_tokens, estimated_input_tokens, output_tokens, "
    "estimated_output_tokens, reasoning_tokens, cache_read_tokens, cache_write_tokens, "
    "cache_input_tokens, cache_calls, reported_nusd, estimated_nusd, unpriced_calls, "
    "primary_model, models, kinds, changed_files, lines_added, lines_removed"
)


def _recompute_runs(connection: sqlite3.Connection) -> None:
    runs = connection.execute(
        f"""
        SELECT d.session_key, d.run_id, {run_origin_sql("s", "rr.run_kind")}, rr.run_kind,
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
        SELECT d.session_key, d.run_id, c.model_key, c.has_model, c.purpose, MIN(c.instant),
            {_RUN_MODEL_SQL}
        FROM temp.rollup_runs d
        CROSS JOIN stat_sessions s ON s.session_key = d.session_key
        CROSS JOIN stat_usage_units u ON u.project_id = s.project_id
            AND u.agent_id = s.agent_id AND u.session_id = s.session_id
        CROSS JOIN stat_usage_records r ON r.session_key = u.session_key AND r.run_id = d.run_id
        CROSS JOIN stat_usage_calls c ON c.session_key = r.session_key AND c.seq = r.seq
        GROUP BY d.session_key, d.run_id, c.model_key, c.has_model, c.purpose
        """,
    )
    saved = _model_rows(
        connection,
        f"""
        SELECT d.session_key, d.run_id, c.model_key, c.has_model, c.purpose, MIN(c.instant),
            {_SAVED_RUN_MODEL_SQL}
        FROM temp.rollup_runs d
        CROSS JOIN stat_records r ON r.session_key = d.session_key AND r.run_id = d.run_id
        CROSS JOIN stat_calls c ON c.session_key = r.session_key AND c.seq = r.seq
        GROUP BY d.session_key, d.run_id, c.model_key, c.has_model, c.purpose
        """,
    )
    rows = []
    model_rows: list[tuple[Any, ...]] = []
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
        model_rows.extend(_run_model_rows(key, models))
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
    connection.executemany(
        f"INSERT INTO agg_run_models (session_key, run_id, model_key, "
        f"{', '.join(USAGE_MEASURES)}) VALUES ({', '.join('?' * (3 + len(USAGE_MEASURES)))})",
        model_rows,
    )


def _model_rows(
    connection: sqlite3.Connection, sql: str
) -> dict[tuple[int, str], list[tuple[Any, ...]]]:
    grouped: dict[tuple[int, str], list[tuple[Any, ...]]] = {}
    for row in connection.execute(sql):
        grouped.setdefault((row[0], row[1]), []).append(tuple(row))
    return grouped


def _run_usage(models: list[tuple[Any, ...]]) -> tuple[Any, ...]:
    """Sum a Run's per-Model and purpose request rows; name its Models by first use.

    The primary Model is the one with the most input tokens (ties by name);
    ``kinds`` lists the distinct request purposes by name.
    """
    sums = [sum(row[_MEASURE[name]] or 0 for row in models) for name in _RUN_MEASURES]
    first_use: dict[str, int] = {}
    inputs: dict[str, int] = {}
    for row in models:
        if row[_HAS_MODEL]:
            key = row[_MODEL_KEY]
            first_use[key] = min(first_use.get(key, row[_FIRST_USE]), row[_FIRST_USE])
            inputs[key] = inputs.get(key, 0) + row[_MEASURE["input_tokens"]]
    primary = min(inputs, key=lambda key: (-inputs[key], key), default=None)
    ordered = sorted(first_use, key=lambda key: (first_use[key], key))
    return (
        *sums,
        primary,
        json.dumps(ordered, separators=(",", ":")),
        json.dumps(sorted({row[_PURPOSE] for row in models}), separators=(",", ":")),
    )


def _run_model_rows(key: tuple[int, str], models: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """The Run's ``agg_run_models`` rows: its request measures summed per Model key."""
    by_model: dict[str, list[int]] = {}
    for row in models:
        sums = by_model.setdefault(row[_MODEL_KEY], [0] * len(USAGE_MEASURES))
        for position in range(len(USAGE_MEASURES)):
            sums[position] += row[_MEASURES + position] or 0
    return [(*key, model, *sums) for model, sums in sorted(by_model.items())]


def _recompute_tools(connection: sqlite3.Connection) -> None:
    origin = (
        f"CASE WHEN rr.run_id IS NOT NULL THEN {run_origin_sql('s', 'rr.run_kind')} "
        f"ELSE {session_origin_sql('s')} END"
    )
    source = """
        FROM temp.rollup_fact_sessions d
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


def _recompute_records(connection: sqlite3.Connection) -> None:
    """Records by hour and role; ``visible_steps`` are visible chat Model steps."""
    connection.execute(
        f"""
        INSERT INTO agg_records (hour, session_key, role, records, visible_steps)
        SELECT {_HOUR} AS hour, r.session_key, r.role, COUNT(*), COUNT(c.seq)
        FROM temp.rollup_fact_sessions d
        CROSS JOIN stat_records r ON r.session_key = d.session_key
        LEFT JOIN stat_calls c ON c.session_key = r.session_key AND c.seq = r.seq
            AND c.kind = 0 AND c.visible = 1
        GROUP BY hour, r.session_key, r.role
        """
    )


def _recompute_cache(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS temp.rollup_cache_turns")
    connection.execute(
        "CREATE TEMP TABLE rollup_cache_turns AS "
        + judged_cache_turns_sql(
            "temp.rollup_fact_sessions d "
            "CROSS JOIN stat_records r ON r.session_key = d.session_key",
            "1",
            "r.session_key",
        )
    )
    connection.execute(
        f"""
        INSERT INTO agg_cache (
            hour, session_key, turns, input_tokens, cache_read_tokens, cache_write_tokens,
            evaluated_turns, suspected_turns, last_instant
        )
        SELECT t.instant / {MICROSECONDS_PER_HOUR} AS hour, t.unit, COUNT(*),
            SUM(t.input_tokens), SUM(t.cache_read_tokens), SUM(t.cache_write_tokens),
            SUM(t.evaluated), SUM(t.incident), MAX(t.instant)
        FROM temp.rollup_cache_turns t
        GROUP BY hour, t.unit
        """
    )
    connection.execute(
        """
        INSERT INTO agg_cache_breaks (
            session_key, seq, instant, model_key, previous_input_tokens, cache_read_tokens
        )
        SELECT t.unit, t.seq, t.instant, t.model_key, t.previous_input_tokens,
            t.cache_read_tokens
        FROM temp.rollup_cache_turns t WHERE t.incident = 1
        """
    )
    connection.execute("DROP TABLE temp.rollup_cache_turns")


def _recompute_usage(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        INSERT INTO agg_usage (
            hour, unit_key, origin, model_key, kind, status, {", ".join(_USAGE_CUBE_MEASURES)}
        )
        SELECT {_HOUR} AS hour, r.session_key,
            {USAGE_ORIGIN_SQL} AS origin,
            c.model_key, c.purpose, r.status,
            {", ".join(_MEASURE_SQL[name] for name in _USAGE_CUBE_MEASURES)}
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
