"""Extensions: in-window activity of every listed Extension participant Session, by group.

Runs, Tool calls and errors are the Session's own; requests are the ledger's
at the Session's address. ``last_activity`` is the latest in-window record or
request.
"""

from __future__ import annotations

from dataclasses import asdict

from core.statistics._extensions import (
    EXTENSION_ACTOR_PREFIX,
    ExtensionSlice,
    ExtensionUsageAccumulator,
)
from core.statistics._sections.common import JsonObject, ReportContext
from core.statistics._sections.window import instant_timestamp


def build(context: ReportContext) -> JsonObject:
    accumulator = ExtensionUsageAccumulator(windowed=context.window.windowed)
    slices: dict[int, ExtensionSlice] = {
        session.session_key: accumulator.slice(session.extension)
        for session in context.by_key.values()
        if session.extension is not None
    }
    if slices:
        _fill(context, slices)
    return asdict(accumulator.build())


def _fill(context: ReportContext, slices: dict[int, ExtensionSlice]) -> None:
    context.sessions_table  # noqa: B018 - loads the Session address ids.
    by_address = {context.session_addresses[key]: key for key in slices}
    in_window = context.instant_condition
    for session_key, status, count in context.query(
        f"""
        SELECT r.session_key, r.status, COUNT(*) FROM agg_runs r
        WHERE r.origin = 'extension' AND {in_window("r.start_instant")}
        GROUP BY r.session_key, r.status
        """
    ):
        target = slices.get(session_key)
        if target is not None:
            target.runs += count
            target.status[status] += count
    for session_key, calls in context.query(
        f"""
        SELECT t.session_key, SUM(t.calls) FROM agg_tools t
        WHERE t.origin = 'extension' AND {context.hour_condition("t")}
        GROUP BY t.session_key
        """
    ):
        if session_key in slices:
            slices[session_key].tool_calls += calls
    for session_key, count in context.query(
        f"SELECT e.session_key, COUNT(*) FROM stat_errors e WHERE {in_window('e.instant')} "
        "GROUP BY e.session_key"
    ):
        if session_key in slices:
            slices[session_key].errors += count
    for (
        address,
        calls,
        input_tokens,
        estimated_input,
        output_tokens,
        estimated_output,
        reported_calls,
        estimated_calls,
        unpriced_calls,
        retrospective_calls,
        reported_nusd,
        estimated_nusd,
    ) in context.query(
        f"""
        SELECT u.address, SUM(a.calls), SUM(a.input_tokens), SUM(a.estimated_input_tokens),
            SUM(a.output_tokens), SUM(a.estimated_output_tokens), SUM(a.reported_calls),
            SUM(a.estimated_calls), SUM(a.unpriced_calls), SUM(a.retrospective_calls),
            SUM(a.reported_nusd), SUM(a.estimated_nusd)
        FROM agg_usage a JOIN {context.units_table} u ON u.unit_key = a.unit_key
        WHERE {context.hour_condition("a")} AND u.actor LIKE ?
        GROUP BY u.address
        """,
        (EXTENSION_ACTOR_PREFIX + "%",),
    ):
        if address not in by_address:
            continue
        target = slices[by_address[address]]
        target.model_calls += calls
        target.measured_input_tokens += input_tokens - estimated_input
        target.estimated_input_tokens += estimated_input
        target.measured_output_tokens += output_tokens - estimated_output
        target.estimated_output_tokens += estimated_output
        coverage = target.calls
        coverage.calls += calls
        coverage.reported_calls += reported_calls
        coverage.estimated_calls += estimated_calls
        coverage.unpriced_calls += unpriced_calls
        coverage.retrospective_calls += retrospective_calls
        target.reported_nusd += reported_nusd
        target.estimated_nusd += estimated_nusd
    _last_activity(context, slices, by_address)


def _last_activity(
    context: ReportContext, slices: dict[int, ExtensionSlice], by_address: dict[int, int]
) -> None:
    low, high = context.window.instants
    latest: dict[int, int] = {}
    for session_key, max_instant in context.query(
        "SELECT session_key, max_instant FROM stat_sessions WHERE max_instant IS NOT NULL"
    ):
        if session_key not in slices or max_instant < low:
            continue
        if max_instant >= high:
            # Later records exist; find the latest one inside the window.
            max_instant = context.scalar(
                "SELECT MAX(instant) FROM stat_records "
                "WHERE session_key = ? AND instant >= ? AND instant < ?",
                (session_key, low, high),
            )
        if max_instant is not None:
            latest[session_key] = max_instant
    for address, max_instant in context.query(
        f"""
        SELECT u.address, MAX((
            SELECT MAX(c.instant) FROM stat_usage_calls c
            WHERE c.session_key = u.unit_key AND {context.instant_condition("c.instant")}
        ))
        FROM {context.units_table} u WHERE u.actor LIKE ? GROUP BY u.address
        """,
        (EXTENSION_ACTOR_PREFIX + "%",),
    ):
        session_key = by_address.get(address)
        if session_key is not None and max_instant is not None:
            latest[session_key] = max(latest.get(session_key, max_instant), max_instant)
    for session_key, instant in latest.items():
        slices[session_key].last_activity = instant_timestamp(instant)
