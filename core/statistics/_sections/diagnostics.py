"""Diagnostics: Compactions, prompt-cache health, data quality, failed attempts and outliers."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta

from core.statistics._cache import TOP_CACHE_BREAK_INCIDENTS, cache_section, load_cache_facts
from core.statistics._compactions import CompactionAccumulator
from core.statistics._sections.common import (
    RUN_COST_ORDER,
    JsonObject,
    ReportContext,
    failed_calls_sql,
    percentile,
    usd,
)
from core.statistics._sections.overview import RUNAWAY_ITERATIONS
from core.statistics._sections.window import hour_timestamp
from core.statistics._units import ReportUnit, UnitScan

TOP_FAILED_HOURS = 10
# A Run is a cost outlier at this multiple of the median positive Run cost.
RUNAWAY_COST_FACTOR = 20
MAX_RUNAWAY_RUNS = 50

# Chat messages are the visible conversation: every user record, and Assistant
# steps only when they carry non-blank text. Session records count every
# persisted record by role.
SESSION_RECORD_ROLES = (
    "system",
    "user",
    "assistant",
    "tool",
    "note",
    "error",
    "compaction_checkpoint",
    "run_summary",
    "agent_takeover",
    "history_edit",
)


def build(context: ReportContext) -> JsonObject:
    window = context.window
    units = [
        ReportUnit(
            display_key=session.display_key,
            session_key=session.session_key,
            session_id=session.address.session_id,
            title=session.title,
            address=session.address,
        )
        for session in context.by_key.values()
    ]
    # Unit scans select instants inclusively; stop one microsecond before ``until``.
    scan = UnitScan(
        context.connection,
        units,
        since=window.since,
        until=None if window.until is None else window.until - timedelta(microseconds=1),
    )
    compactions = CompactionAccumulator()
    compactions.load(scan, [unit.title for unit in units])
    cache = cache_section(load_cache_facts(scan, top_incidents=TOP_CACHE_BREAK_INCIDENTS))
    return {
        "compactions": asdict(compactions.build()),
        "cache": asdict(cache),
        "data_quality": _data_quality(context),
        "failed_attempts": _failed_attempts(context),
        "runaway_runs": _runaway_runs(context),
        "open_runs": int(
            context.scalar(
                "SELECT COUNT(*) FROM agg_runs r WHERE r.status = 'running' AND "
                + context.instant_condition("r.start_instant")
            )
        ),
        "roles": _roles(context),
    }


def _data_quality(context: ReportContext) -> list[JsonObject]:
    return [
        {
            "model": model,
            "calls": calls,
            "unreported_calls": unreported,
            "estimated_token_calls": estimated_tokens,
            "uncached_calls": uncached_calls,
            "uncached_cost_usd": usd(uncached_nusd) if estimated_calls else None,
            "unpriced_calls": unpriced,
            "retrospective_calls": retrospective,
        }
        for (
            model,
            calls,
            unreported,
            estimated_tokens,
            uncached_calls,
            uncached_nusd,
            estimated_calls,
            unpriced,
            retrospective,
        ) in context.query(
            f"""
            SELECT a.model_key, SUM(a.calls) AS calls, SUM(a.unreported_calls),
                SUM(a.estimated_token_calls), SUM(a.uncached_calls), SUM(a.uncached_nusd),
                SUM(a.estimated_calls), SUM(a.unpriced_calls), SUM(a.retrospective_calls)
            FROM agg_usage a WHERE {context.hour_condition("a")}
            GROUP BY a.model_key ORDER BY calls DESC, a.model_key
            """
        )
    ]


def _failed_attempts(context: ReportContext) -> JsonObject:
    """Failed ledger attempts in the window and the worst hours with their Models and actors."""
    failed = failed_calls_sql()
    hours = context.query(
        f"""
        SELECT a.hour, SUM(a.calls), SUM({failed}) AS failed FROM agg_usage a
        WHERE {context.hour_condition("a")}
        GROUP BY a.hour HAVING failed > 0
        ORDER BY failed DESC, a.hour DESC LIMIT {TOP_FAILED_HOURS}
        """
    )
    total = context.scalar(
        f"SELECT COALESCE(SUM({failed}), 0) FROM agg_usage a WHERE {context.hour_condition('a')}"
    )
    selected = ", ".join(str(row[0]) for row in hours)
    models: dict[int, list[JsonObject]] = {}
    agents: dict[int, list[JsonObject]] = {}
    if hours:
        for target, key_sql, joins in (
            (models, "a.model_key", ""),
            (agents, "u.actor", f"JOIN {context.units_table} u ON u.unit_key = a.unit_key"),
        ):
            for hour, key, count in context.query(
                f"""
                SELECT a.hour, {key_sql} AS key, SUM({failed}) AS failed FROM agg_usage a {joins}
                WHERE a.hour IN ({selected})
                GROUP BY a.hour, key HAVING failed > 0
                ORDER BY a.hour, failed DESC, key
                """
            ):
                target.setdefault(hour, []).append({"key": key, "count": count})
    return {
        "total": int(total),
        "hours": [
            {
                "hour_start": hour_timestamp(hour),
                "calls": calls,
                "failed": failed_count,
                "models": models.get(hour, []),
                "agents": agents.get(hour, []),
            }
            for hour, calls, failed_count in hours
        ],
    }


def _runaway_runs(context: ReportContext) -> list[JsonObject]:
    """Runs with many iterations or a cost far above the median positive Run cost."""
    cost = "r.reported_nusd + r.estimated_nusd"
    costs = [
        row[0]
        for row in context.query(
            f"SELECT {cost} AS cost FROM agg_runs r "
            f"WHERE {context.instant_condition('r.start_instant')} AND cost > 0 ORDER BY cost"
        )
    ]
    median = percentile(costs, 50)
    condition = f"r.iterations >= {RUNAWAY_ITERATIONS}"
    if median is not None:
        condition += f" OR {cost} >= {RUNAWAY_COST_FACTOR * median}"
    return context.run_rows(condition, RUN_COST_ORDER, MAX_RUNAWAY_RUNS)


def _roles(context: ReportContext) -> JsonObject:
    records = dict(
        context.query(
            f"SELECT r.role, COUNT(*) FROM stat_records r "
            f"WHERE {context.instant_condition('r.instant')} GROUP BY r.role"
        )
    )
    visible = context.scalar(
        "SELECT COUNT(*) FROM stat_calls c WHERE c.kind = 0 AND c.visible = 1 AND "
        + context.instant_condition("c.instant")
    )
    return {
        "chat_messages_by_role": {"user": records.get("user", 0), "assistant": int(visible)},
        "session_records_by_role": {role: records.get(role, 0) for role in SESSION_RECORD_ROLES},
    }
