"""Activity: the user's days with vBot, streaks, habits, favorites and records.

A day counts as active when the user started a Run on it (origin ``user`` or
``channel``); work Agents do on their own (automation, Sub-Agents,
Extensions, background learning) shows in the day figures but makes no day
active. Days, hours of day and weekdays are local to the report zone; hours
fold into the local date of their start like every day series.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import NamedTuple

from core.statistics._projection import MICROSECONDS_PER_HOUR
from core.statistics._sections.common import (
    JsonObject,
    ReportContext,
    Totals,
    grouped_totals,
)

# Run origins that are the user's own conversations.
YOUR_ORIGINS = ("user", "channel")


class _HourRuns(NamedTuple):
    """In-window Runs that share a start hour, Session and whether they are the user's."""

    hour: int
    session_key: int
    yours: bool
    runs: int
    messages: int
    tool_calls: int
    changed_files: int
    lines_added: int
    lines_removed: int


class _Day:
    __slots__ = ("messages", "runs", "totals", "your_runs")

    def __init__(self) -> None:
        self.runs = 0
        self.your_runs = 0
        self.messages = 0
        self.totals = Totals()


def build(context: ReportContext) -> JsonObject:
    calendar = context.calendar
    origins = ", ".join(f"'{origin}'" for origin in YOUR_ORIGINS)
    groups = [
        _HourRuns(*row)
        for row in context.query(
            f"""
            SELECT r.start_instant / {MICROSECONDS_PER_HOUR} AS hour, r.session_key,
                r.origin IN ({origins}) AS yours, COUNT(*),
                COALESCE(SUM(CASE WHEN r.origin IN ({origins}) THEN r.user_messages END), 0),
                SUM(r.tool_calls), COALESCE(SUM(r.changed_files), 0),
                COALESCE(SUM(r.lines_added), 0), COALESCE(SUM(r.lines_removed), 0)
            FROM agg_runs r WHERE {context.instant_condition("r.start_instant")}
            GROUP BY hour, r.session_key, yours
            """
        )
    ]
    days: dict[str, _Day] = {}
    by_hour = [0] * 24
    by_weekday = [0] * 7
    agents: Counter[str] = Counter()
    sessions: Counter[int] = Counter()
    for group in groups:
        local_date, local_hour = calendar.local(group.hour)
        day = days.setdefault(local_date, _Day())
        day.runs += group.runs
        if not group.yours:
            continue
        day.your_runs += group.runs
        day.messages += group.messages
        by_hour[local_hour] += group.runs
        by_weekday[date.fromisoformat(local_date).weekday()] += group.runs
        sessions[group.session_key] += group.runs
        session = context.by_key.get(group.session_key)
        if session is not None:
            agents[session.display_key] += group.runs
    for hour, totals in grouped_totals(context, "a.hour").items():
        days.setdefault(calendar.date(hour), _Day()).totals.merge(totals)

    usage = Totals()
    for day in days.values():
        usage.merge(day.totals)
    active = sorted(key for key, day in days.items() if day.your_runs)
    your_runs = sum(day.your_runs for day in days.values())
    today = context.window.now.astimezone(context.window.zone).date()
    return {
        "days": [
            {
                "date": key,
                "runs": day.runs,
                "your_runs": day.your_runs,
                "messages": day.messages,
                "tokens": _tokens(day.totals),
                "cost_usd": day.totals.cost_usd,
            }
            for key, day in sorted(days.items())
        ],
        "totals": {
            "active_days": len(active),
            "first_active": active[0] if active else None,
            "runs": sum(day.runs for day in days.values()),
            "your_runs": your_runs,
            "messages": sum(day.messages for day in days.values()),
            "sessions": len(sessions),
            "agents": len(agents),
            "tokens": _tokens(usage),
            "cost_usd": usage.cost_usd,
            "tool_calls": sum(group.tool_calls for group in groups),
            "changed_files": sum(group.changed_files for group in groups),
            "lines_added": sum(group.lines_added for group in groups),
            "lines_removed": sum(group.lines_removed for group in groups),
        },
        "streaks": _streaks(active, today),
        "by_hour": by_hour,
        "by_weekday": by_weekday,
        "favorite_model": _favorite_model(context, usage),
        "favorite_agent": _favorite(agents, your_runs, "agent_id"),
        "records": {
            "busiest_day": _busiest_day(days),
            "top_session": _top_session(context, sessions),
            "longest_run": _longest_run(context, origins),
        },
    }


def _tokens(totals: Totals) -> int:
    return totals["input_tokens"] + totals["output_tokens"]


def _streaks(active: list[str], today: date) -> JsonObject:
    """The longest run of consecutive active days, and the current one.

    The current streak ends today, or yesterday while today has no activity
    yet, so it does not break before the day is over.
    """
    longest: tuple[int, str | None, str | None] = (0, None, None)
    start: date | None = None
    previous: date | None = None
    for key in active:
        day = date.fromisoformat(key)
        if previous is None or day - previous != timedelta(days=1):
            start = day
        previous = day
        assert start is not None
        length = (day - start).days + 1
        if length > longest[0]:
            longest = (length, start.isoformat(), key)
    current = 0
    current_start = None
    if previous is not None and start is not None and today - previous <= timedelta(days=1):
        current = (previous - start).days + 1
        current_start = start.isoformat()
    return {
        "current": current,
        "current_start": current_start,
        "longest": longest[0],
        "longest_start": longest[1],
        "longest_end": longest[2],
    }


def _favorite(counts: Counter[str], total: int, field: str) -> JsonObject | None:
    if not counts:
        return None
    key, runs = min(counts.items(), key=lambda item: (-item[1], item[0]))
    return {field: key, "runs": runs, "share": runs / total if total else None}


def _favorite_model(context: ReportContext, usage: Totals) -> JsonObject | None:
    """The Model with the most tokens in the window."""
    models = [
        (model, _tokens(totals), totals["calls"])
        for model, totals in grouped_totals(context, "a.model_key").items()
        if model
    ]
    if not models:
        return None
    model, tokens, calls = min(models, key=lambda item: (-item[1], -item[2], item[0]))
    total = _tokens(usage)
    return {
        "model": model,
        "tokens": tokens,
        "calls": calls,
        "share": tokens / total if total else None,
    }


def _busiest_day(days: dict[str, _Day]) -> JsonObject | None:
    """The day with the most of the user's Runs; the earlier one on a tie."""
    candidates = [(key, day) for key, day in days.items() if day.your_runs]
    if not candidates:
        return None
    key, day = min(candidates, key=lambda item: (-item[1].your_runs, item[0]))
    return {"date": key, "your_runs": day.your_runs, "tokens": _tokens(day.totals)}


def _top_session(context: ReportContext, sessions: Counter[int]) -> JsonObject | None:
    """The listed Session with the most of the user's Runs."""
    listed = [(key, runs) for key, runs in sessions.items() if key in context.by_key]
    if not listed:
        return None
    key, runs = min(listed, key=lambda item: (-item[1], item[0]))
    session = context.by_key[key]
    return {
        "agent_id": session.display_key,
        "session_id": session.address.session_id,
        "session_title": session.title,
        "your_runs": runs,
    }


def _longest_run(context: ReportContext, origins: str) -> JsonObject | None:
    """The user's longest finished Run, as a RunRow."""
    rows = context.run_rows(
        f"r.origin IN ({origins}) AND r.status <> 'running' AND r.duration_ms IS NOT NULL",
        "r.duration_ms DESC",
        1,
    )
    return rows[0] if rows else None
