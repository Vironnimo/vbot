"""Overview: usage and Run totals against the previous window, day series, leaders, insights."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from core.statistics._extensions import EXTENSION_ACTOR_PREFIX
from core.statistics._rollups import ORIGINS
from core.statistics._sections.common import (
    JsonObject,
    ReportContext,
    RunFact,
    Totals,
    cost_order,
    failed_calls_sql,
    grouped_totals,
    load_runs,
    percentile,
    sorted_values,
    status_counts,
    usage_totals,
    usd,
)
from core.statistics._sections.window import hour_timestamp

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


def build(context: ReportContext) -> JsonObject:
    window = context.window
    previous = window.previous()
    totals = usage_totals(context)
    runs = load_runs(context)
    previous_runs = load_runs(context, previous) if previous is not None else None
    models = grouped_totals(context, "a.model_key")
    return {
        "totals": totals.json(),
        "previous": usage_totals(context, previous).json() if previous is not None else None,
        "runs": status_counts(runs),
        "previous_runs": status_counts(previous_runs) if previous_runs is not None else None,
        "user_runs": _user_runs(runs),
        "previous_user_runs": _user_runs(previous_runs) if previous_runs is not None else None,
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


def _user_runs(runs: list[RunFact]) -> JsonObject:
    """Percentiles of user Runs; durations and costs of finished Runs only."""
    user = [run for run in runs if run.origin == "user"]
    finished = [run for run in user if run.status != "running"]
    durations = sorted_values(run.duration_ms for run in finished)
    costs = sorted_values(run.cost for run in finished)
    cost_p50, cost_p90 = percentile(costs, 50), percentile(costs, 90)
    return {
        "count": len(user),
        "duration_p50_ms": percentile(durations, 50),
        "duration_p90_ms": percentile(durations, 90),
        "cost_p50_usd": None if cost_p50 is None else usd(cost_p50),
        "cost_p90_usd": None if cost_p90 is None else usd(cost_p90),
        "first_visible_p50_ms": percentile(sorted_values(run.first_visible_ms for run in user), 50),
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


def _series(context: ReportContext, runs: list[RunFact]) -> list[JsonObject]:
    hourly = grouped_totals(context, "a.hour")
    run_hours = [run.start_hour for run in runs]
    active = [*hourly, *run_hours]
    hours = context.window.series_hours(min(active, default=None), max(active, default=None))
    days = {day: (Totals(), Counter[str]()) for day in context.calendar.days(hours)}
    for hour, totals in hourly.items():
        days[context.calendar.date(hour)][0].merge(totals)
    for run in runs:
        counts = days[context.calendar.date(run.start_hour)][1]
        counts["runs"] += 1
        counts["failed_runs"] += run.status == "failed"
    points = []
    for day, (totals, counts) in days.items():
        values = totals.json()
        points.append(
            {
                "date": day,
                **{field: values[field] for field in _SERIES_FIELDS},
                "runs": counts["runs"],
                "failed_runs": counts["failed_runs"],
            }
        )
    return points


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


def _by_origin(context: ReportContext, runs: list[RunFact]) -> list[JsonObject]:
    usage = grouped_totals(context, "a.origin")
    run_counts = Counter(run.origin for run in runs)
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


def _top_agents(context: ReportContext, runs: list[RunFact]) -> list[JsonObject]:
    usage = grouped_totals(
        context, "u.actor", f"JOIN {context.units_table} u ON u.unit_key = a.unit_key"
    )
    run_counts: Counter[str] = Counter()
    for run in runs:
        session = context.by_key.get(run.session_key)
        if session is not None:
            run_counts[session.display_key] += 1
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
    context: ReportContext, totals: Totals, runs: list[RunFact], models: dict[str, Totals]
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
    cancelled = [run for run in runs if run.status == "cancelled"]
    cancelled_cost = sum(run.cost or 0 for run in cancelled)
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
                "runs": len(cancelled),
            },
        )
    costs = sorted((run.cost for run in runs if run.cost is not None), reverse=True)
    run_cost = sum(costs)
    if len(costs) >= TOP_RUNS_MIN_RUNS and run_cost > 0:
        top = math.ceil(len(costs) * TOP_RUNS_FRACTION)
        share = sum(costs[:top]) / run_cost
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
    runaway = sum(
        run.iterations is not None and run.iterations >= RUNAWAY_ITERATIONS for run in runs
    )
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
