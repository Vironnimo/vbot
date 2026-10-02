"""Runs: outcomes, durations and costs by origin and Agent, top Runs, cancellations, errors.

A Run belongs to the window it started in. Duration and cost percentiles are
nearest-rank over finished Runs (running ones have no final duration or
cost); averages cover every in-window Run. ``previous`` holds the outcome
counts and user-Run duration percentiles of the window of equal length
before, or ``None`` without one.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence

from core.statistics._measurements import UNKNOWN_MODEL_KEY, _provider_of
from core.statistics._projection import MICROSECONDS_PER_HOUR
from core.statistics._rollups import ORIGINS
from core.statistics._sections.common import (
    RUN_COST_ORDER,
    RUN_STATUSES,
    JsonObject,
    ReportContext,
    RunFact,
    count_entries,
    failed_calls_sql,
    load_runs,
    percentile,
    sorted_values,
    status_counts,
    usd,
)

TOP_RUNS = 25
# Upper bounds (inclusive, ms) of the duration histogram; ``None`` holds longer Runs.
DURATION_EDGES = (10_000, 30_000, 60_000, 120_000, 300_000, 600_000, 1_800_000, 3_600_000, None)
_OUTCOMES = RUN_STATUSES[:4]


def build(context: ReportContext) -> JsonObject:
    runs = load_runs(context)
    by_origin: dict[str, list[RunFact]] = {origin: [] for origin in ORIGINS}
    by_agent: dict[str, list[RunFact]] = {}
    for run in runs:
        by_origin.setdefault(run.origin, []).append(run)
        session = context.by_key.get(run.session_key)
        by_agent.setdefault(session.display_key if session is not None else "", []).append(run)
    origins = {origin: group for origin, group in by_origin.items() if group}
    cancelled = [run for run in runs if run.status == "cancelled"]
    return {
        "totals": status_counts(runs),
        "by_origin": [
            {
                "origin": origin,
                **_outcomes(group),
                **_durations(group),
                **_costs(group),
                "avg_tool_calls": _mean(run.tool_calls for run in group),
                "avg_model_steps": _mean(run.model_steps for run in group),
            }
            for origin, group in origins.items()
        ],
        "duration_buckets": _duration_buckets(runs, list(origins)),
        "agents": [
            _agent_row(agent_id, group)
            for agent_id, group in sorted(
                by_agent.items(), key=lambda item: (-len(item[1]), item[0])
            )
        ],
        "daily": _daily(context, runs),
        "longest": context.run_rows("r.duration_ms IS NOT NULL", "r.duration_ms DESC", TOP_RUNS),
        "costliest": context.run_rows("1", RUN_COST_ORDER, TOP_RUNS),
        "most_steps": context.run_rows("1", "r.model_steps DESC", TOP_RUNS),
        "cancelled": {
            "runs": len(cancelled),
            "cost_usd": _costs(cancelled)["cost_usd"],
            "wait_p50_ms": percentile(sorted_values(run.duration_ms for run in cancelled), 50),
        },
        "errors": _errors(context),
        "previous": _previous(context),
    }


def _previous(context: ReportContext) -> JsonObject | None:
    """Outcome counts and user-Run duration percentiles of the previous window."""
    window = context.window.previous()
    if window is None:
        return None
    runs = load_runs(context, window)
    return {
        "totals": status_counts(runs),
        "user": _durations([run for run in runs if run.origin == "user"]),
    }


def _agent_row(agent_id: str, runs: Sequence[RunFact]) -> JsonObject:
    costs = _costs(runs)
    return {
        "agent_id": agent_id,
        **_outcomes(runs),
        **_durations(runs),
        "cost_usd": costs["cost_usd"],
        "cost_p50_usd": costs["cost_p50_usd"],
        "avg_tool_calls": _mean(run.tool_calls for run in runs),
        "avg_model_steps": _mean(run.model_steps for run in runs),
        "tool_ms": sum(run.tool_ms for run in runs),
        "changed_files": sum(run.changed_files or 0 for run in runs),
        "lines_added": sum(run.lines_added or 0 for run in runs),
        "lines_removed": sum(run.lines_removed or 0 for run in runs),
    }


def _outcomes(runs: Sequence[RunFact]) -> JsonObject:
    counts = Counter(run.status for run in runs)
    return {"runs": len(runs), **{status: counts[status] for status in _OUTCOMES}}


def _durations(runs: Sequence[RunFact]) -> JsonObject:
    durations = sorted_values(run.duration_ms for run in runs if run.status != "running")
    return {
        "duration_p50_ms": percentile(durations, 50),
        "duration_p90_ms": percentile(durations, 90),
    }


def _costs(runs: Sequence[RunFact]) -> JsonObject:
    """Summed known Run costs and percentiles of finished Runs' known costs.

    The sum is ``None`` when Runs made requests and none of them is priced.
    """
    known = [run.cost for run in runs if run.cost is not None]
    unknown = not known and any(run.calls for run in runs)
    costs = sorted_values(run.cost for run in runs if run.status != "running")
    p50, p90 = percentile(costs, 50), percentile(costs, 90)
    return {
        "cost_usd": None if unknown else usd(sum(known)),
        "cost_p50_usd": None if p50 is None else usd(p50),
        "cost_p90_usd": None if p90 is None else usd(p90),
    }


def _mean(values: Iterable[int]) -> float | None:
    numbers = list(values)
    return sum(numbers) / len(numbers) if numbers else None


def _duration_buckets(runs: Sequence[RunFact], origins: list[str]) -> list[JsonObject]:
    counts = [dict.fromkeys(origins, 0) for _edge in DURATION_EDGES]
    for run in runs:
        if run.duration_ms is None or run.status == "running":
            continue
        for position, edge in enumerate(DURATION_EDGES):
            if edge is None or run.duration_ms <= edge:
                counts[position][run.origin] += 1
                break
    return [
        {"upper_ms": edge, "by_origin": bucket}
        for edge, bucket in zip(DURATION_EDGES, counts, strict=True)
    ]


def _daily(context: ReportContext, runs: Sequence[RunFact]) -> list[JsonObject]:
    hours = [run.start_hour for run in runs]
    span = context.window.series_hours(min(hours, default=None), max(hours, default=None))
    days = {day: Counter[str]() for day in context.calendar.days(span)}
    for run in runs:
        days[context.calendar.date(run.start_hour)][run.status] += 1
    return [
        {"date": day, "runs": counts.total(), **{status: counts[status] for status in _OUTCOMES}}
        for day, counts in days.items()
    ]


def _errors(context: ReportContext) -> JsonObject:
    """Error records by kind, Provider, Model, Agent, local day and local hour.

    An error is attributed to the Model of the latest Assistant step before it
    in its Session.
    """
    by_kind: Counter[str] = Counter()
    by_model: Counter[str] = Counter()
    by_provider: Counter[str] = Counter()
    by_agent: Counter[str] = Counter()
    by_hour: Counter[int] = Counter()
    hourly: Counter[int] = Counter()
    for hour, kind, actor, model, count in context.query(
        f"""
        SELECT e.instant / {MICROSECONDS_PER_HOUR} AS hour, e.kind, s.actor, (
                SELECT c.model_key FROM stat_calls c
                WHERE c.session_key = e.session_key AND c.seq < e.seq AND c.kind = 0
                ORDER BY c.seq DESC LIMIT 1
            ) AS model, COUNT(*)
        FROM stat_errors e JOIN {context.sessions_table} s ON s.session_key = e.session_key
        WHERE {context.instant_condition("e.instant")}
        GROUP BY hour, e.kind, s.actor, model
        """
    ):
        model_key = model or UNKNOWN_MODEL_KEY
        by_kind[kind] += count
        by_model[model_key] += count
        by_provider[_provider_of(model_key)] += count
        by_agent[actor] += count
        hourly[hour] += count
        by_hour[context.calendar.local(hour)[1]] += count
    span = context.window.series_hours(min(hourly, default=None), max(hourly, default=None))
    daily = dict.fromkeys(context.calendar.days(span), 0)
    for hour, count in hourly.items():
        daily[context.calendar.date(hour)] += count
    failed = context.scalar(
        f"SELECT COALESCE(SUM({failed_calls_sql()}), 0) FROM agg_usage a "
        f"WHERE {context.hour_condition('a')}"
    )
    return {
        "total": sum(hourly.values()),
        "failed_attempts": int(failed),
        "by_kind": count_entries(by_kind),
        "by_provider": count_entries(by_provider),
        "by_model": count_entries(by_model),
        "by_agent": count_entries(by_agent),
        "daily": [{"date": day, "count": count} for day, count in daily.items()],
        "by_hour": [{"hour": hour, "count": by_hour[hour]} for hour in range(24)],
    }
