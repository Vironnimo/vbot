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


# Each breakdown's key over usage cube rows ``a`` and their report units ``u``.
_DIMENSIONS = {
    "agent": "u.actor",
    "model": "a.model_key",
    "provider": provider_sql("a.model_key"),
    "project": "u.project",
    "origin": "a.origin",
    "kind": "a.kind",
}


def build(context: ReportContext) -> JsonObject:
    units = context.units_table
    runs, session_runs = _run_counts(context)
    return {
        "totals": usage_totals(context).json(),
        "breakdowns": {
            name: _breakdown(context, key_sql, runs[name]) for name, key_sql in _DIMENSIONS.items()
        },
        "series": _series(context),
        "top_runs": context.run_rows("1", RUN_COST_ORDER, TOP_RUNS),
        "top_sessions": _top_sessions(context, units, session_runs),
        "recent_calls": _recent_calls(context, units),
    }


def _run_counts(context: ReportContext) -> tuple[dict[str, Counter[str]], Counter[int]]:
    """In-window Runs per breakdown key and per listed Session address, from one scan.

    A Run counts once under each Model, Provider and request kind it used;
    Agent, Project and Session counts cover listed Sessions only.
    """
    counts: dict[str, Counter[str]] = {name: Counter() for name in _DIMENSIONS}
    by_address: Counter[int] = Counter()
    context.sessions_table  # noqa: B018 - resolves the Session address ids.
    parsed: dict[str, list[str]] = {}

    def values(text: str) -> list[str]:
        cached = parsed.get(text)
        if cached is None:
            cached = parsed[text] = json.loads(text)
        return cached

    for session_key, origin, models, kinds, runs in context.query(
        f"""
        SELECT r.session_key, r.origin, r.models, r.kinds, COUNT(*) FROM agg_runs r
        WHERE {context.instant_condition("r.start_instant")}
        GROUP BY r.session_key, r.origin, r.models, r.kinds
        """
    ):
        counts["origin"][origin] += runs
        run_models = values(models)
        for model in run_models:
            counts["model"][model] += runs
        for provider in {model.split("/", 1)[0] for model in run_models}:
            counts["provider"][provider] += runs
        for kind in values(kinds):
            counts["kind"][kind] += runs
        session = context.by_key.get(session_key)
        if session is not None:
            counts["agent"][session.display_key] += runs
            counts["project"][session.address.project_id or ""] += runs
            by_address[context.session_addresses[session_key]] += runs
    return counts, by_address


def _breakdown(context: ReportContext, key_sql: str, runs: Counter[str]) -> list[JsonObject]:
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
    rows = []
    for key in {*usage, *runs}:
        sessions, totals = usage.get(key, (0, Totals()))
        rows.append({"key": key, **totals.json(), "runs": runs[key], "sessions": sessions})
    rows.sort(key=lambda row: cost_order(row["cost_usd"], row["calls"], row["key"]))
    return rows


def _series(context: ReportContext) -> list[JsonObject]:
    buckets = context.buckets
    hourly = grouped_totals(context, "a.hour")
    points = {key: Totals() for key in buckets.keys(hourly)}
    for hour, totals in hourly.items():
        points[buckets.key(hour)].merge(totals)
    return [{buckets.field: key, **totals.json()} for key, totals in points.items()]


def _top_sessions(context: ReportContext, units: str, runs: Counter[int]) -> list[JsonObject]:
    usage = grouped_totals(
        context,
        "u.address",
        f"JOIN {units} u ON u.unit_key = a.unit_key AND u.address IS NOT NULL",
    )
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
