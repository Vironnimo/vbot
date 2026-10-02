"""Costs and tokens: Totals by dimension, the day series, top Runs and Sessions, recent calls.

Every figure comes from the ledger's usage cube and Run rows; ``runs`` per
breakdown row counts the in-window Runs (by start) that touch the key, and
``sessions`` the Session addresses with in-window requests.
"""

from __future__ import annotations

import json
from collections import Counter

from core.statistics._projection import UNKNOWN_COST
from core.statistics._rollups import USAGE_ORIGIN_SQL
from core.statistics._sections.common import (
    RUN_COST_ORDER,
    JsonObject,
    ReportContext,
    Totals,
    cost_order,
    grouped_totals,
    provider_sql,
    totals_sql,
    usage_totals,
)

TOP_RUNS = 50
TOP_SESSIONS = 50
RECENT_CALLS = 50


def build(context: ReportContext) -> JsonObject:
    units = context.units_table
    sessions = context.sessions_table
    in_window = context.instant_condition("r.start_instant")
    run_models = f"FROM agg_runs r, json_each(r.models) m WHERE {in_window}"
    run_sessions = (
        f"FROM agg_runs r JOIN {sessions} s ON s.session_key = r.session_key WHERE {in_window}"
    )
    dimensions = {
        "agent": ("u.actor", f"SELECT s.actor, COUNT(*) {run_sessions} GROUP BY s.actor"),
        "model": ("a.model_key", f"SELECT m.value, COUNT(*) {run_models} GROUP BY m.value"),
        "provider": (
            provider_sql("a.model_key"),
            f"SELECT provider, COUNT(*) FROM (SELECT DISTINCT r.session_key, r.run_id, "
            f"{provider_sql('m.value')} AS provider {run_models}) GROUP BY provider",
        ),
        "project": ("u.project", f"SELECT s.project, COUNT(*) {run_sessions} GROUP BY s.project"),
        "origin": (
            "a.origin",
            f"SELECT r.origin, COUNT(*) FROM agg_runs r WHERE {in_window} GROUP BY r.origin",
        ),
        "kind": (
            "a.kind",
            f"SELECT k.value, COUNT(*) FROM agg_runs r, json_each(r.kinds) k "
            f"WHERE {in_window} GROUP BY k.value",
        ),
    }
    return {
        "totals": usage_totals(context).json(),
        "breakdowns": {
            name: _breakdown(context, key_sql, runs_sql)
            for name, (key_sql, runs_sql) in dimensions.items()
        },
        "series": _series(context),
        "top_runs": context.run_rows("1", RUN_COST_ORDER, TOP_RUNS),
        "top_sessions": _top_sessions(context, units, run_sessions),
        "recent_calls": _recent_calls(context, units),
    }


def _breakdown(context: ReportContext, key_sql: str, runs_sql: str) -> list[JsonObject]:
    usage: dict[str, tuple[int, Totals]] = {
        row[0]: (row[1], Totals(row[2:]))
        for row in context.query(
            f"""
            SELECT {key_sql} AS key, COUNT(DISTINCT u.address), {totals_sql()}
            FROM agg_usage a JOIN {context.units_table} u ON u.unit_key = a.unit_key
            WHERE {context.hour_condition("a")}
            GROUP BY key
            """
        )
    }
    runs = Counter({str(key): count for key, count in context.query(runs_sql)})
    rows = []
    for key in {*usage, *runs}:
        sessions, totals = usage.get(key, (0, Totals()))
        rows.append({"key": key, **totals.json(), "runs": runs[key], "sessions": sessions})
    rows.sort(key=lambda row: cost_order(row["cost_usd"], row["calls"], row["key"]))
    return rows


def _series(context: ReportContext) -> list[JsonObject]:
    hourly = grouped_totals(context, "a.hour")
    hours = context.window.series_hours(min(hourly, default=None), max(hourly, default=None))
    days = {day: Totals() for day in context.calendar.days(hours)}
    for hour, totals in hourly.items():
        days[context.calendar.date(hour)].merge(totals)
    return [{"date": day, **totals.json()} for day, totals in days.items()]


def _top_sessions(context: ReportContext, units: str, run_sessions: str) -> list[JsonObject]:
    usage = grouped_totals(
        context,
        "u.address",
        f"JOIN {units} u ON u.unit_key = a.unit_key AND u.address IS NOT NULL",
    )
    runs = dict(context.query(f"SELECT s.address, COUNT(*) {run_sessions} GROUP BY s.address"))
    ranked = sorted(
        usage.items(),
        key=lambda item: cost_order(
            item[1].cost_usd, item[1]["calls"], context.addresses[item[0]][1]
        ),
    )[:TOP_SESSIONS]
    rows = []
    for address, totals in ranked:
        actor, session_id, title = context.addresses[address]
        rows.append(
            {
                "agent_id": actor,
                "session_id": session_id,
                "session_title": title,
                "runs": runs.get(address, 0),
                **totals.json(),
            }
        )
    return rows


def _recent_calls(context: ReportContext, units: str) -> list[JsonObject]:
    rows = context.query(
        f"""
        SELECT r.timestamp, c.model_key, c.purpose, r.status, {USAGE_ORIGIN_SQL}, ru.actor,
            ru.address, c.input_tokens, c.output_tokens, c.cache_read_tokens,
            c.input_estimated OR c.output_estimated,
            c.retrospective = 1 AND c.cost_source = 2, c.cost_json
        FROM stat_usage_records r
        CROSS JOIN stat_usage_calls c ON c.session_key = r.session_key AND c.seq = r.seq
        CROSS JOIN stat_usage_units u ON u.session_key = r.session_key
        CROSS JOIN {units} ru ON ru.unit_key = r.session_key
        LEFT JOIN stat_sessions s ON s.project_id = u.project_id AND s.agent_id = u.agent_id
            AND s.session_id = u.session_id
        LEFT JOIN stat_run_records rr ON rr.session_key = s.session_key AND rr.run_id = r.run_id
        WHERE {context.instant_condition("r.instant")}
        ORDER BY r.instant DESC, r.seq DESC
        LIMIT {RECENT_CALLS}
        """
    )
    calls = []
    for (
        timestamp,
        model,
        kind,
        status,
        origin,
        actor,
        address,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        estimated,
        retrospective,
        cost,
    ) in rows:
        session_id, title = (None, None) if address is None else context.addresses[address][1:]
        calls.append(
            {
                "timestamp": timestamp,
                "model": model,
                "kind": kind,
                "status": status,
                "origin": origin,
                "agent_id": actor,
                "session_id": session_id,
                "session_title": title,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "estimated_tokens": bool(estimated),
                "retrospective": bool(retrospective),
                "cost": json.loads(cost) if cost else dict(UNKNOWN_COST),
            }
        )
    return calls
