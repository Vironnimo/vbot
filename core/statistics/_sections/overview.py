"""Overview: usage and Run totals against the previous window, series, leaders, insights."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, NamedTuple

from core.statistics._extensions import EXTENSION_ACTOR_PREFIX
from core.statistics._projection import MICROSECONDS_PER_HOUR
from core.statistics._rollups import ORIGINS
from core.statistics._sections.common import (
    RUN_STATUSES,
    JsonObject,
    ReportContext,
    Totals,
    cost_order,
    failed_calls_sql,
    grouped_totals,
    percentile,
    usage_totals,
    usd,
)
from core.statistics._sections.window import ReportWindow, hour_timestamp

TOP_ROWS = 5

# Insight thresholds: an insight is emitted only when its condition holds.
UNCACHED_SHARE = 0.25
CANCELLED_COST_SHARE = 0.05
TOP_RUNS_FRACTION = 0.1
TOP_RUNS_SHARE = 0.6
TOP_RUNS_MIN_RUNS = 10
FAILED_ATTEMPT_BURST = 20
RUNAWAY_ITERATIONS = 100
TOOL_FAILURE_MIN_CALLS = 20
TOOL_FAILURE_RATE = 0.25


# A Run's known cost in nano-USD; NULL when every one of its requests is unpriced.
_KNOWN_COST = (
    "CASE WHEN r.calls > 0 AND r.unpriced_calls >= r.calls "
    "THEN NULL ELSE r.reported_nusd + r.estimated_nusd END"
)


class RunGroup(NamedTuple):
    """In-window Runs that share a start hour, origin, status and Session."""

    hour: int
    origin: str
    status: str
    session_key: int
    runs: int
    runaway: int
    priced_runs: int
    cost: int


def _run_groups(context: ReportContext, window: ReportWindow | None = None) -> list[RunGroup]:
    """The window's Runs aggregated in SQL; Python only folds the small groups."""
    return [
        RunGroup(*row)
        for row in context.query(
            f"""
            SELECT r.start_instant / {MICROSECONDS_PER_HOUR} AS hour, r.origin, r.status,
                r.session_key, COUNT(*), SUM(COALESCE(r.iterations, 0) >= {RUNAWAY_ITERATIONS}),
                COUNT({_KNOWN_COST}), COALESCE(SUM({_KNOWN_COST}), 0)
            FROM agg_runs r WHERE {context.instant_condition("r.start_instant", window)}
            GROUP BY hour, r.origin, r.status, r.session_key
            """
        )
    ]


def _status_counts(groups: list[RunGroup]) -> JsonObject:
    counts = dict.fromkeys(RUN_STATUSES, 0)
    total = 0
    for group in groups:
        total += group.runs
        if group.status in counts:
            counts[group.status] += group.runs
    return {"total": total, **counts}


def build(context: ReportContext) -> JsonObject:
    window = context.window
    previous = window.previous()
    totals = usage_totals(context)
    runs = _run_groups(context)
    previous_runs = _run_groups(context, previous) if previous is not None else None
    models = grouped_totals(context, "a.model_key")
    return {
        "totals": totals.json(),
        "previous": usage_totals(context, previous).json() if previous is not None else None,
        "runs": _status_counts(runs),
        "previous_runs": _status_counts(previous_runs) if previous_runs is not None else None,
        "user_runs": _user_runs(context, runs),
        "previous_user_runs": (
            _user_runs(context, previous_runs, previous) if previous_runs is not None else None
        ),
        "active_agents": _active_agents(context),
        "active_sessions": _active_sessions(context),
        "series": _series(context, runs),
        "by_origin": _by_origin(context, runs),
        "top_agents": _top_agents(context, runs),
        "top_models": [
            {
                "model": model,
                "calls": row["calls"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cost_usd": row.cost_usd,
                "cache_read_tokens": row["cache_read_tokens"],
                "cache_input_tokens": row["cache_input_tokens"],
                "cache_calls": row["cache_calls"],
            }
            for model, row in sorted(
                models.items(),
                key=lambda item: cost_order(item[1].cost_usd, item[1]["calls"], item[0]),
            )[:TOP_ROWS]
        ],
        "insights": _insights(context, totals, runs, models),
    }


def _user_runs(
    context: ReportContext, groups: list[RunGroup], window: ReportWindow | None = None
) -> JsonObject:
    """Percentiles of user Runs; durations and costs of finished Runs only."""

    def values(value_sql: str, finished: bool) -> list[int]:
        condition = " AND r.status <> 'running'" if finished else ""
        return [
            row[0]
            for row in context.query(
                f"SELECT {value_sql} AS value FROM agg_runs r "
                f"WHERE r.origin = 'user' AND "
                f"{context.instant_condition('r.start_instant', window)}{condition} "
                "AND value IS NOT NULL ORDER BY value"
            )
        ]

    count = sum(group.runs for group in groups if group.origin == "user")
    if not count:
        durations: list[int] = []
        costs: list[int] = []
        first_visible: list[int] = []
    else:
        durations = values("r.duration_ms", finished=True)
        costs = values(_KNOWN_COST, finished=True)
        first_visible = values("r.first_visible_ms", finished=False)
    cost_p50, cost_p90 = percentile(costs, 50), percentile(costs, 90)
    return {
        "count": count,
        "duration_p50_ms": percentile(durations, 50),
        "duration_p90_ms": percentile(durations, 90),
        "cost_p50_usd": None if cost_p50 is None else usd(cost_p50),
        "cost_p90_usd": None if cost_p90 is None else usd(cost_p90),
        "first_visible_p50_ms": percentile(first_visible, 50),
    }


def _active_agents(context: ReportContext) -> int:
    """Agents with an in-window Run or request; Extensions and standalone work excluded."""
    return int(
        context.scalar(
            f"""
            SELECT COUNT(*) FROM (
                SELECT s.actor FROM agg_runs r
                JOIN {context.sessions_table} s ON s.session_key = r.session_key
                WHERE {context.instant_condition("r.start_instant")}
                UNION
                SELECT u.actor FROM agg_usage a
                JOIN {context.units_table} u ON u.unit_key = a.unit_key
                WHERE {context.hour_condition("a")}
            ) WHERE actor <> '' AND actor NOT LIKE ?
            """,
            (EXTENSION_ACTOR_PREFIX + "%",),
        )
    )


def _active_sessions(context: ReportContext) -> int:
    """Session addresses with an in-window Run or request."""
    return int(
        context.scalar(
            f"""
            SELECT COUNT(*) FROM (
                SELECT s.address FROM agg_runs r
                JOIN {context.sessions_table} s ON s.session_key = r.session_key
                WHERE {context.instant_condition("r.start_instant")}
                UNION
                SELECT u.address FROM agg_usage a
                JOIN {context.units_table} u ON u.unit_key = a.unit_key
                WHERE {context.hour_condition("a")} AND u.address IS NOT NULL
            )
            """
        )
    )


def _series(context: ReportContext, runs: list[RunGroup]) -> list[JsonObject]:
    buckets = context.buckets
    hourly = grouped_totals(context, "a.hour")
    active = {*hourly, *(group.hour for group in runs)}
    points = {key: (Totals(), Counter[str]()) for key in buckets.keys(active)}
    for hour, totals in hourly.items():
        points[buckets.key(hour)][0].merge(totals)
    for group in runs:
        counts = points[buckets.key(group.hour)][1]
        counts["runs"] += group.runs
        if group.status == "failed":
            counts["failed_runs"] += group.runs
    series = []
    for key, (totals, counts) in points.items():
        values = totals.json()
        series.append(
            {
                buckets.field: key,
                **{field: values[field] for field in _SERIES_FIELDS},
                "runs": counts["runs"],
                "failed_runs": counts["failed_runs"],
            }
        )
    return series


_SERIES_FIELDS = (
    "calls",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "reported_cost_usd",
    "estimated_cost_usd",
    "cache_read_tokens",
    "cache_input_tokens",
)


def _by_origin(context: ReportContext, runs: list[RunGroup]) -> list[JsonObject]:
    usage = grouped_totals(context, "a.origin")
    run_counts: Counter[str] = Counter()
    for group in runs:
        run_counts[group.origin] += group.runs
    return [
        {
            "origin": origin,
            "calls": totals["calls"],
            "runs": run_counts[origin],
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "cost_usd": totals.cost_usd,
        }
        for origin in ORIGINS
        for totals in (usage.get(origin, Totals()),)
        if totals["calls"] or run_counts[origin]
    ]


def _top_agents(context: ReportContext, runs: list[RunGroup]) -> list[JsonObject]:
    usage = grouped_totals(
        context, "u.actor", f"JOIN {context.units_table} u ON u.unit_key = a.unit_key"
    )
    run_counts: Counter[str] = Counter()
    for group in runs:
        session = context.by_key.get(group.session_key)
        if session is not None:
            run_counts[session.display_key] += group.runs
    rows = [
        {
            "agent_id": actor,
            "calls": totals["calls"],
            "runs": run_counts[actor],
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "cost_usd": totals.cost_usd,
        }
        for actor in {*usage, *run_counts}
        if actor
        for totals in (usage.get(actor, Totals()),)
    ]
    rows.sort(key=lambda row: cost_order(row["cost_usd"], row["calls"], row["agent_id"]))
    return rows[:TOP_ROWS]


def _insights(
    context: ReportContext, totals: Totals, runs: list[RunGroup], models: dict[str, Totals]
) -> list[JsonObject]:
    insights: list[JsonObject] = []

    def emit(insight_id: str, values: dict[str, Any], severity: str = "warn") -> None:
        insights.append({"id": insight_id, "severity": severity, "values": values})

    estimated = totals["estimated_nusd"]
    uncached = totals["uncached_nusd"]
    if estimated > 0 and uncached / estimated >= UNCACHED_SHARE:
        top_model = min(models, key=lambda model: (-models[model]["uncached_nusd"], model))
        emit(
            "uncached_value",
            {"share": uncached / estimated, "cost_usd": usd(uncached), "top_model": top_model},
        )
    cancelled = [group for group in runs if group.status == "cancelled"]
    cancelled_cost = sum(group.cost for group in cancelled)
    if (
        totals.cost_nusd > 0
        and cancelled_cost > 0
        and cancelled_cost / totals.cost_nusd >= CANCELLED_COST_SHARE
    ):
        emit(
            "cancelled_cost",
            {
                "share": cancelled_cost / totals.cost_nusd,
                "cost_usd": usd(cancelled_cost),
                "runs": sum(group.runs for group in cancelled),
            },
        )
    priced_runs = sum(group.priced_runs for group in runs)
    run_cost = sum(group.cost for group in runs)
    if priced_runs >= TOP_RUNS_MIN_RUNS and run_cost > 0:
        top = math.ceil(priced_runs * TOP_RUNS_FRACTION)
        top_cost = context.scalar(
            f"""
            SELECT SUM(cost) FROM (
                SELECT {_KNOWN_COST} AS cost FROM agg_runs r
                WHERE {context.instant_condition("r.start_instant")} AND cost IS NOT NULL
                ORDER BY cost DESC LIMIT {top}
            )
            """
        )
        share = top_cost / run_cost
        if share >= TOP_RUNS_SHARE:
            emit("top_runs_share", {"share": share, "runs": top}, severity="info")
    burst = context.query(
        f"""
        SELECT a.hour, SUM({failed_calls_sql()}) AS failed FROM agg_usage a
        WHERE {context.hour_condition("a")}
        GROUP BY a.hour HAVING failed >= {FAILED_ATTEMPT_BURST}
        ORDER BY failed DESC, a.hour DESC LIMIT 1
        """
    )
    if burst:
        hour, failed = burst[0]
        model = context.query(
            f"""
            SELECT a.model_key, SUM({failed_calls_sql()}) AS failed FROM agg_usage a
            WHERE a.hour = ? GROUP BY a.model_key ORDER BY failed DESC, a.model_key LIMIT 1
            """,
            (hour,),
        )[0][0]
        emit(
            "failed_attempt_burst",
            {"hour_start": hour_timestamp(hour), "failed": failed, "model": model},
        )
    runaway = sum(group.runaway for group in runs)
    if runaway:
        emit("runaway_runs", {"runs": runaway})
    failing = [
        (rejected / calls, calls, name)
        for name, calls, rejected in context.query(
            f"""
            SELECT t.name, SUM(t.calls) AS calls, SUM(t.rejected) FROM agg_tools t
            WHERE {context.hour_condition("t")}
            GROUP BY t.name HAVING calls >= {TOOL_FAILURE_MIN_CALLS}
            """
        )
        if rejected / calls >= TOOL_FAILURE_RATE
    ]
    if failing:
        rate, calls, name = min(failing, key=lambda item: (-item[0], -item[1], item[2]))
        emit("tool_failure", {"tool": name, "rate": rate, "calls": calls})
    return insights
