"""The Statistics report sections and Run activity, at the service interface.

Usage figures come from the durable ledger; Runs, Tools, errors and Compactions
from the Session read model. Money is exact in nano-USD and ``null`` when no
request of a figure is priced.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core.chat.messages import ChatMessage, ToolCall
from core.models.pricing import TokenPricing, price_usage
from core.sessions import ChatSessionManager, SessionAddress
from core.statistics import REPORT_SECTIONS
from core.tools import tool_failure, tool_success
from core.usage import UsageRecorder
from tests.core.sessions.history_fixtures import seed_history
from tests.core.statistics.statistics_test_support import (
    BASE,
    StatisticsFactory,
    _admit,
    _assistant,
    _call,
    _compaction,
    _run_summary,
    _timing,
    _tool,
    _write_session,
)

SONNET = "openrouter/anthropic/claude-sonnet-4"
NOW = datetime(2026, 6, 2, 0, 30, tzinfo=UTC)
Report = dict[str, Any]


def _clock() -> datetime:
    return NOW


def _priced(usage: dict[str, Any]) -> dict[str, Any]:
    """``usage`` with a catalog cost snapshot at $1 per million tokens."""
    return {**usage, "cost": price_usage(usage, _PRICING)}


_PRICING = TokenPricing.from_cost({"input": 1, "output": 1}, source="test")


def _summary(
    run_id: str,
    *,
    status: str = "completed",
    start: datetime = BASE,
    duration_ms: int = 1000,
    iterations: int = 1,
) -> ChatMessage:
    return ChatMessage.run_summary(
        run_id=run_id,
        status=status,
        timing=_timing(start, duration_ms),
        iteration_count=iterations,
        timestamp=start + timedelta(milliseconds=duration_ms),
    )


def _runs(
    manager: ChatSessionManager,
    runs: list[tuple[str, str, int, int]],
    *,
    agent_id: str = "main",
    session_id: str = "work",
    kind: str = "user",
) -> SessionAddress:
    """Write finished Runs ``(run_id, status, duration_ms, iterations)`` started at BASE."""
    session = manager.create(agent_id, session_id=session_id)
    for run_id, status, duration_ms, iterations in runs:
        _admit(manager, session.address, run_id, kind)
        seed_history(
            manager.get(session.address),
            [_summary(run_id, status=status, duration_ms=duration_ms, iterations=iterations)],
        )
    return session.address


# -- Report shape -------------------------------------------------------------


def test_a_report_without_activity_has_every_section_in_its_zero_form(
    statistics: StatisticsFactory,
) -> None:
    service = statistics(["main"], clock=_clock)

    report = service.report()

    assert set(report) == {"generated_at", "window", *REPORT_SECTIONS}
    assert report["generated_at"] == "2026-06-02T00:30:00.000000Z"
    assert report["window"] == {"since": None, "until": None, "timezone": "UTC", "bucket": "day"}
    overview = report["overview"]
    # Zero requests cost nothing; a split without requests is unknown.
    assert (overview["totals"]["calls"], overview["totals"]["cost_usd"]) == (0, 0.0)
    assert overview["totals"]["reported_cost_usd"] is None
    assert overview["previous"] is None
    assert overview["user_runs"] == {
        "count": 0,
        "duration_p50_ms": None,
        "duration_p90_ms": None,
        "cost_p50_usd": None,
        "cost_p90_usd": None,
        "first_visible_p50_ms": None,
    }
    assert overview["series"] == overview["insights"] == overview["top_models"] == []
    assert report["runs"]["totals"]["total"] == 0
    assert [row["count"] for row in report["runs"]["errors"]["by_hour"]] == [0] * 24
    assert report["tools"]["totals"]["calls"] == 0
    assert report["extensions"] == {"extensions": []}
    diagnostics = report["diagnostics"]
    assert diagnostics["roles"]["chat_messages_by_role"] == {"user": 0, "assistant": 0}
    assert diagnostics["roles"]["session_records_by_role"]["agent_takeover"] == 0
    assert diagnostics["compactions"]["total_compactions"] == 0
    assert json.loads(json.dumps(report)) == report


def test_a_report_builds_only_the_requested_sections_and_rejects_bad_requests(
    statistics: StatisticsFactory,
) -> None:
    service = statistics(["main"], clock=_clock)

    report = service.report(sections=["tools", "runs", "tools"])

    assert set(report) == {"generated_at", "window", "runs", "tools"}
    requests: list[tuple[dict[str, Any], str]] = [
        ({"sections": ["bogus"]}, "unknown statistics sections: bogus"),
        ({"sections": []}, "at least one statistics section"),
        ({"timezone": "Mars/Olympus"}, "timezone"),
        ({"since": BASE, "until": BASE - timedelta(hours=2)}, "since"),
    ]
    for request, message in requests:
        with pytest.raises(ValueError, match=message):
            service.report(**request)


# -- Windows and time ---------------------------------------------------------


@pytest.mark.asyncio
async def test_window_is_hour_aligned_half_open_and_compared_with_the_span_before(
    manager: ChatSessionManager, statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    def at(hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 6, 1, hour, minute, tzinfo=UTC)

    # The window [10:00, 13:00) and its previous span [07:00, 10:00).
    for moment, cost in [
        (at(6, 59), 9.0),
        (at(7), 1.0),
        (at(9, 59), 2.0),
        (at(10), 4.0),
        (at(12, 59), 8.0),
        (at(13), 16.0),
    ]:
        await _call(ledger, {"reported_cost_usd": cost}, at=moment)
    service = statistics(usage_recorder=ledger, clock=_clock)

    report = service.report(since=at(10, 30), until=at(12, 10), sections=["overview"])
    open_ended = service.report(since=at(10, 30), sections=["overview"])

    assert report["window"] == {
        "since": "2026-06-01T10:00:00.000000Z",
        "until": "2026-06-01T13:00:00.000000Z",
        "timezone": "UTC",
        "bucket": "hour",
    }
    overview = report["overview"]
    assert (overview["totals"]["calls"], overview["totals"]["cost_usd"]) == (2, 12.0)
    assert (overview["previous"]["calls"], overview["previous"]["cost_usd"]) == (2, 3.0)
    assert overview["previous_runs"]["total"] == 0
    # Without ``until`` the window ends at the hour after now: [10:00, 01:00 next day)
    # and its previous span is the 15 hours before 10:00.
    assert open_ended["window"]["until"] is None
    assert open_ended["overview"]["totals"]["cost_usd"] == 28.0
    assert open_ended["overview"]["previous"]["cost_usd"] == 12.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timezone", "calls_by_day"),
    [
        # 2026-03-29 has 23 hours in Berlin: clocks jump from 02:00 to 03:00 local.
        (
            "Europe/Berlin",
            {"2026-03-28": 0, "2026-03-29": 2, "2026-03-30": 1, "2026-03-31": 0},
        ),
        ("UTC", {"2026-03-28": 1, "2026-03-29": 2, "2026-03-30": 0}),
    ],
)
async def test_day_series_group_hours_into_local_calendar_days(
    statistics: StatisticsFactory,
    ledger: UsageRecorder,
    timezone: str,
    calls_by_day: dict[str, int],
) -> None:
    for moment in ("2026-03-28T23:30:00", "2026-03-29T21:30:00", "2026-03-29T22:30:00"):
        await _call(
            ledger, {"reported_cost_usd": 0.5}, at=datetime.fromisoformat(moment + "+00:00")
        )
    service = statistics(usage_recorder=ledger, clock=_clock)

    report = service.report(
        since=datetime(2026, 3, 28, tzinfo=UTC),
        until=datetime(2026, 3, 31, tzinfo=UTC),
        timezone=timezone,
        sections=["overview", "usage"],
    )

    overview_series = report["overview"]["series"]
    assert {point["date"]: point["calls"] for point in overview_series} == calls_by_day
    assert [point["date"] for point in report["usage"]["series"]] == list(calls_by_day)
    assert sum(point["cost_usd"] for point in report["usage"]["series"]) == 1.5


@pytest.mark.asyncio
async def test_a_window_of_at_most_two_days_has_hour_buckets_in_every_series(
    manager: ChatSessionManager, statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    # A failed Run with one Model call and an error at 12:00 (BASE).
    address = _runs(manager, [("r1", "failed", 1000, 1)])
    await _call(ledger, {"reported_cost_usd": 0.5}, address=address, run_id="r1", at=BASE)
    _write_session(
        manager,
        "main",
        [ChatMessage.error("timeout", "slow", timestamp=BASE + timedelta(seconds=2))],
    )
    service = statistics(usage_recorder=ledger, clock=_clock)
    until = BASE + timedelta(minutes=30)

    report = service.report(since=BASE - timedelta(hours=2), until=until)

    # [10:00, 13:00): one point per UTC hour, empty hours filled with zeros.
    hours = [f"2026-06-01T{hour}:00:00.000000Z" for hour in ("10", "11", "12")]
    assert report["window"]["bucket"] == "hour"
    overview, usage = report["overview"]["series"], report["usage"]["series"]
    runs = report["runs"]
    assert [(p["hour_start"], p["calls"], p["runs"], p["failed_runs"]) for p in overview] == [
        (hours[0], 0, 0, 0),
        (hours[1], 0, 0, 0),
        (hours[2], 1, 1, 1),
    ]
    assert [(p["hour_start"], p["calls"]) for p in usage] == list(
        zip(hours, [0, 0, 1], strict=True)
    )
    assert [(p["hour_start"], p["runs"], p["failed"]) for p in runs["daily"]] == [
        (hours[0], 0, 0),
        (hours[1], 0, 0),
        (hours[2], 1, 1),
    ]
    assert runs["errors"]["daily"] == [
        {"hour_start": hour, "count": count} for hour, count in zip(hours, [0, 0, 1], strict=True)
    ]
    assert not any("date" in point for point in [*overview, *usage, *runs["daily"]])
    # 48 hours still count by the hour, 49 by the local day; an open end runs
    # to the current hour.
    two_days = service.report(since=BASE - timedelta(hours=47), until=until, sections=["usage"])
    assert (two_days["window"]["bucket"], len(two_days["usage"]["series"])) == ("hour", 48)
    longer = service.report(since=BASE - timedelta(hours=48), until=until, sections=["usage"])
    assert longer["window"]["bucket"] == "day"
    assert [p["date"] for p in longer["usage"]["series"]] == [
        "2026-05-30",
        "2026-05-31",
        "2026-06-01",
    ]
    open_end = service.report(since=NOW - timedelta(hours=24), sections=["usage"])["usage"]
    assert open_end["series"][-1]["hour_start"] == "2026-06-02T00:00:00.000000Z"
    assert len(open_end["series"]) == 25


# -- Runs ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runs_report_origins_nearest_rank_percentiles_and_run_rows(
    manager: ChatSessionManager, statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    # Ten user Runs of 100..1000 ms costing 0.1..1.0, one automation Run, one running.
    address = _runs(manager, [(f"u{i}", "completed", (i + 1) * 100, 1) for i in range(10)])
    for i in range(10):
        await _call(ledger, {"reported_cost_usd": (i + 1) / 10}, address=address, run_id=f"u{i}")
    _admit(manager, address, "cron", "cron")
    session = manager.get(address)
    seed_history(
        session,
        [
            # Saved Usage without a ledger id is imported into the ledger once.
            _assistant(
                model=SONNET,
                at=BASE,
                usage={"input_tokens": 10, "output_tokens": 2, "reported_cost_usd": 0.25},
            ),
            _tool(name="read", at=BASE, envelope=tool_success({}), duration_ms=5),
            _summary("cron", duration_ms=45_000, iterations=3),
        ],
    )
    manager.set_title(address, "Parser refactor")
    _admit(manager, address, "open")
    service = statistics(usage_recorder=ledger, clock=_clock)

    report = service.report(sections=["overview", "runs"])

    runs = report["runs"]
    assert runs["totals"] == {
        "total": 12,
        "completed": 11,
        "failed": 0,
        "cancelled": 0,
        "interrupted": 0,
        "running": 1,
    }
    automation, user = runs["by_origin"]
    assert (automation["origin"], automation["runs"], automation["cost_usd"]) == (
        "automation",
        1,
        0.25,
    )
    # Nearest rank over finished Runs; the running Run has no duration or cost yet.
    assert user["runs"] == 11
    assert (user["duration_p50_ms"], user["duration_p90_ms"]) == (500, 900)
    assert (user["cost_p50_usd"], user["cost_p90_usd"], user["cost_usd"]) == (0.5, 0.9, 5.5)
    assert report["overview"]["user_runs"]["count"] == 11
    assert report["overview"]["user_runs"]["duration_p90_ms"] == 900
    assert runs["duration_buckets"][0] == {
        "upper_ms": 10_000,
        "by_origin": {"automation": 0, "user": 10},
    }
    assert runs["duration_buckets"][2]["by_origin"] == {"automation": 1, "user": 0}
    assert runs["agents"][0]["agent_id"] == "main"
    assert runs["agents"][0]["runs"] == 12
    assert runs["longest"][0] == {
        "agent_id": "main",
        "session_id": "work",
        "session_title": "Parser refactor",
        "run_id": "cron",
        "origin": "automation",
        "status": "completed",
        "started_at": "2026-06-01T12:00:00.000000Z",
        "duration_ms": 45_000,
        "cost_usd": 0.25,
        "input_tokens": 10,
        "output_tokens": 2,
        "calls": 1,
        "model_steps": 1,
        "tool_calls": 1,
        "iterations": 3,
        "primary_model": SONNET,
        "models": [SONNET],
    }
    assert [row["run_id"] for row in runs["costliest"][:3]] == ["u9", "u8", "u7"]
    assert runs["most_steps"][0]["run_id"] == "cron"
    # An all-time report has nothing before it; a window starting an hour after
    # these Runs compares with the span of equal length that holds them.
    assert runs["previous"] is None
    later = service.report(since=BASE + timedelta(hours=1), sections=["runs"])["runs"]
    assert later["totals"]["total"] == 0
    assert later["previous"] == {
        "totals": runs["totals"],
        "user": {"duration_p50_ms": 500, "duration_p90_ms": 900},
    }


@pytest.mark.asyncio
async def test_errors_attribute_the_preceding_model_and_count_failed_attempts(
    manager: ChatSessionManager, statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    _write_session(
        manager,
        "main",
        [
            _assistant(model=SONNET, at=BASE),
            ChatMessage.error("rate_limit", "slow down", timestamp=BASE + timedelta(seconds=1)),
            ChatMessage.error("timeout", "too slow", timestamp=BASE + timedelta(seconds=2)),
            _run_summary(
                status="failed", at=BASE + timedelta(seconds=3), duration_ms=100, run_id="r1"
            ),
        ],
    )
    # An error before any Model call has no Model to attribute it to.
    _write_session(
        manager,
        "main",
        [ChatMessage.error("config_error", "bad", timestamp=BASE + timedelta(days=1))],
    )
    await _call(ledger, None, status="failed")
    service = statistics(usage_recorder=ledger, clock=lambda: BASE + timedelta(days=2))

    errors = service.report(timezone="Europe/Berlin", sections=["runs"])["runs"]["errors"]

    assert errors["total"] == 3
    assert errors["failed_attempts"] == 1
    assert errors["by_kind"] == [
        {"key": "config_error", "count": 1},
        {"key": "rate_limit", "count": 1},
        {"key": "timeout", "count": 1},
    ]
    assert errors["by_provider"] == [
        {"key": "openrouter", "count": 2},
        {"key": "unknown", "count": 1},
    ]
    assert errors["by_model"] == [{"key": SONNET, "count": 2}, {"key": "unknown", "count": 1}]
    assert errors["by_agent"] == [{"key": "main", "count": 3}]
    # 12:00 UTC is 14:00 in Berlin summer time; days are gap-filled to today.
    assert errors["by_hour"][14] == {"hour": 14, "count": 3}
    assert errors["daily"] == [
        {"date": "2026-06-01", "count": 2},
        {"date": "2026-06-02", "count": 1},
        {"date": "2026-06-03", "count": 0},
    ]


# -- Money and insights -------------------------------------------------------


@pytest.mark.asyncio
async def test_totals_sum_money_exactly_and_keep_unknown_cost_null(
    statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    for _ in range(3):
        await _call(ledger, {"input_tokens": 1, "output_tokens": 1, "reported_cost_usd": 0.1})
    # A catalog price snapshot estimates the cost; a call without a price stays unpriced.
    await _call(ledger, _priced({"input_tokens": 1000, "output_tokens": 0}), model="priced/m")
    await _call(ledger, {"input_tokens": 5, "output_tokens": 1}, model="free/m")
    service = statistics(usage_recorder=ledger, clock=_clock)

    usage = service.report(sections=["usage"])["usage"]

    totals = usage["totals"]
    # Three times 0.1 is exactly 0.3; the unpriced call adds nothing and is counted.
    assert totals["reported_cost_usd"] == 0.3
    assert totals["estimated_cost_usd"] == 0.001
    assert totals["cost_usd"] == 0.301
    assert (totals["reported_calls"], totals["estimated_calls"], totals["unpriced_calls"]) == (
        3,
        1,
        1,
    )
    assert totals["retrospective_calls"] == 0
    # A Model none of whose requests is priced has an unknown cost and ranks last.
    models = usage["breakdowns"]["model"]
    assert [(row["key"], row["cost_usd"]) for row in models] == [
        ("chat/m", 0.3),
        ("priced/m", 0.001),
        ("free/m", None),
    ]


async def _uncached_value(
    manager: ChatSessionManager, ledger: UsageRecorder, at: bool
) -> dict[str, Any]:
    # Catalog-priced input without cache counters is a quarter of the estimated cost.
    await _call(ledger, _priced({"input_tokens": 1000, "output_tokens": 0}), model="est/m")
    cached = {"input_tokens": 3000 if at else 3001, "output_tokens": 0, "cache_read_tokens": 0}
    await _call(ledger, _priced(cached), model="est/m")
    return {"share": 0.25, "cost_usd": 0.001, "top_model": "est/m"}


async def _cancelled_cost(
    manager: ChatSessionManager, ledger: UsageRecorder, at: bool
) -> dict[str, Any]:
    address = _runs(manager, [("done", "completed", 100, 1), ("stop", "cancelled", 100, 1)])
    await _call(ledger, {"reported_cost_usd": 1.9 if at else 1.91}, address=address, run_id="done")
    await _call(ledger, {"reported_cost_usd": 0.1}, address=address, run_id="stop")
    return {"share": 0.05, "cost_usd": 0.1, "runs": 1}


async def _top_runs_share(
    manager: ChatSessionManager, ledger: UsageRecorder, at: bool
) -> dict[str, Any]:
    address = _runs(manager, [(f"r{i}", "completed", 100, 1) for i in range(10)])
    for i in range(10):
        cost = (1.35 if at else 1.34) if i == 0 else 0.1
        await _call(ledger, {"reported_cost_usd": cost}, address=address, run_id=f"r{i}")
    return {"share": 0.6, "runs": 1}


async def _failed_attempt_burst(
    manager: ChatSessionManager, ledger: UsageRecorder, at: bool
) -> dict[str, Any]:
    for _ in range(20 if at else 19):
        await _call(ledger, None, status="failed")
    return {"hour_start": "2026-06-01T12:00:00.000000Z", "failed": 20, "model": "chat/m"}


async def _runaway_runs(
    manager: ChatSessionManager, ledger: UsageRecorder, at: bool
) -> dict[str, Any]:
    _runs(manager, [("long", "completed", 100, 100 if at else 99)])
    return {"runs": 1}


async def _tool_failure(
    manager: ChatSessionManager, ledger: UsageRecorder, at: bool
) -> dict[str, Any]:
    rejected = 5 if at else 4
    _write_session(
        manager,
        "main",
        [
            _tool(
                name="edit",
                at=BASE + timedelta(seconds=index),
                envelope=tool_failure("ambiguous_match", "choose")
                if index < rejected
                else tool_success({}),
                duration_ms=5,
            )
            for index in range(20)
        ],
    )
    return {"tool": "edit", "rate": 0.25, "calls": 20}


Scenario = Callable[[ChatSessionManager, UsageRecorder, bool], Awaitable[dict[str, Any]]]


@pytest.mark.asyncio
@pytest.mark.parametrize("at_threshold", [True, False], ids=["at", "below"])
@pytest.mark.parametrize(
    ("insight", "scenario"),
    [
        ("uncached_value", _uncached_value),
        ("cancelled_cost", _cancelled_cost),
        ("top_runs_share", _top_runs_share),
        ("failed_attempt_burst", _failed_attempt_burst),
        ("runaway_runs", _runaway_runs),
        ("tool_failure", _tool_failure),
    ],
)
async def test_insights_appear_from_their_threshold_on(
    manager: ChatSessionManager,
    statistics: StatisticsFactory,
    ledger: UsageRecorder,
    insight: str,
    scenario: Scenario,
    at_threshold: bool,
) -> None:
    values = await scenario(manager, ledger, at_threshold)
    service = statistics(usage_recorder=ledger, clock=_clock)

    insights = service.report(sections=["overview"])["overview"]["insights"]

    if not at_threshold:
        assert insights == []
        return
    [emitted] = insights
    assert emitted["id"] == insight
    assert emitted["severity"] == ("info" if insight == "top_runs_share" else "warn")
    assert emitted["values"] == pytest.approx(values)


# -- Tools ----------------------------------------------------------------------


def test_tools_report_outcomes_codes_and_approximate_latency(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    # Nine fast acceptances and one slow rejection with an error code.
    messages = [
        _tool(
            name="read",
            at=BASE + timedelta(seconds=index),
            envelope=tool_success({"text": "x"}),
            duration_ms=10,
        )
        for index in range(9)
    ]
    messages.append(
        _tool(
            name="read",
            at=BASE + timedelta(seconds=9),
            envelope=tool_failure("not_found", "missing"),
            duration_ms=1000,
        )
    )
    _write_session(manager, "main", messages)

    tools = statistics(clock=_clock).report(sections=["tools"])["tools"]

    assert tools["totals"] == {
        "calls": 10,
        "accepted": 9,
        "rejected": 1,
        "unknown": 0,
        "tool_ms": 1090,
        "tools": 1,
    }
    [read] = tools["tools"]
    # Percentiles are interpolated inside the histogram bucket of the
    # nearest-rank sample: 10 ms lies in [9, 10], 1000 ms in [861, 1022].
    assert (read["p50_ms"], read["p95_ms"], read["max_ms"]) == (9, 938, 1000)
    assert read["rejection_rate"] == 0.1
    assert read["time_share"] == 1.0
    assert read["top_codes"] == [{"code": "not_found", "count": 1}]
    assert tools["rejection_codes"] == [{"code": "not_found", "count": 1, "tools": ["read"]}]
    assert tools["by_agent"] == [{"agent_id": "main", "calls": 10, "rejected": 1, "tool_ms": 1090}]


# -- Diagnostics ----------------------------------------------------------------


def test_chat_messages_count_only_visible_steps_while_records_count_every_role(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    tool_call = ToolCall(id="call-read", name="read", arguments={"path": "README.md"})
    _write_session(
        manager,
        "main",
        [
            ChatMessage.user("inspect", timestamp=BASE),
            _assistant(
                model=SONNET,
                at=BASE + timedelta(seconds=1),
                content=None,
                reasoning="I should inspect the file.",
                tool_calls=[tool_call],
            ),
            _assistant(model=SONNET, at=BASE + timedelta(seconds=2), content="   "),
            _assistant(
                model=SONNET,
                at=BASE + timedelta(seconds=3),
                content="I found the cause.",
                tool_calls=[tool_call],
            ),
            ChatMessage.note("background", timestamp=BASE + timedelta(seconds=4)),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=5), duration_ms=5000, run_id="r1"
            ),
        ],
    )

    roles = statistics(clock=_clock).report(sections=["diagnostics"])["diagnostics"]["roles"]

    assert roles["chat_messages_by_role"] == {"user": 1, "assistant": 1}
    records = roles["session_records_by_role"]
    assert (records["user"], records["assistant"], records["note"], records["run_summary"]) == (
        1,
        3,
        1,
        1,
    )
    assert records["history_edit"] == 0


@pytest.mark.asyncio
async def test_diagnostics_list_data_quality_failed_hours_runaway_and_open_runs(
    manager: ChatSessionManager, statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    address = _runs(
        manager,
        [("loop", "completed", 100, 150)] + [(f"r{i}", "completed", 100, 1) for i in range(3)],
    )
    for run_id in ("r0", "r1", "r2"):
        await _call(ledger, {"reported_cost_usd": 0.01}, address=address, run_id=run_id)
    # Twenty times the median Run cost makes a Run a runaway even with few iterations.
    await _call(ledger, {"reported_cost_usd": 0.2}, address=address, run_id="loop")
    _admit(manager, address, "open")
    await _call(ledger, {"input_tokens": 9, "input_tokens_estimated": True}, model="partial/m")
    for _ in range(2):
        await _call(ledger, None, model="flaky/m", status="failed")
    service = statistics(usage_recorder=ledger, clock=_clock)

    diagnostics = service.report(sections=["diagnostics"])["diagnostics"]

    assert diagnostics["open_runs"] == 1
    assert [row["run_id"] for row in diagnostics["runaway_runs"]] == ["loop"]
    quality = {row["model"]: row for row in diagnostics["data_quality"]}
    assert quality["partial/m"]["unreported_calls"] == 1
    assert quality["partial/m"]["unpriced_calls"] == 1
    assert diagnostics["failed_attempts"] == {
        "total": 2,
        "hours": [
            {
                "hour_start": "2026-06-01T12:00:00.000000Z",
                "calls": 7,
                "failed": 2,
                "models": [{"key": "flaky/m", "count": 2}],
                "agents": [{"key": "", "count": 2}],
            }
        ],
    }


def test_compactions_report_distribution_reclaim_strategy_window_and_forks(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    day_one = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    day_two = datetime(2026, 6, 2, 9, 0, tzinfo=UTC)
    day_three = datetime(2026, 6, 3, 9, 0, tzinfo=UTC)

    source = manager.create("main")
    source.append(_compaction(at=day_one, before=100_000, after=70_000))
    source.append(_compaction(at=day_two, before=90_000, after=30_000))

    fork = asyncio.run(
        manager.fork(SessionAddress(project_id=None, agent_id="main", session_id=source.id))
    )
    fork.append(_compaction(at=day_three, before=95_000, after=25_000))

    second_session_id = _write_session(
        manager,
        "main",
        [
            _compaction(
                at=day_two + timedelta(hours=1),
                before=80_000,
                after=40_000,
                strategy="continuation",
            ),
            _compaction(at=day_three + timedelta(hours=1), before=75_000, after=25_000),
        ],
    )

    report = statistics(clock=_clock).report(
        since=day_two, until=day_three + timedelta(hours=2), sections=["diagnostics"]
    )
    compactions = report["diagnostics"]["compactions"]

    # The fork's inherited source checkpoints are history, not the fork's own activity.
    assert compactions["total_compactions"] == 4
    assert compactions["sessions_with_compactions"] == 3
    assert compactions["average_per_compacted_session"] == pytest.approx(4 / 3)
    assert compactions["p50_per_compacted_session"] == 1.0
    assert compactions["p95_per_compacted_session"] == 2.0
    assert compactions["max_per_session"] == 2
    assert [(row["strategy"], row["compactions"]) for row in compactions["by_strategy"]] == [
        ("summary_tail", 3),
        ("continuation", 1),
    ]
    reclaim = compactions["reclaim"]
    assert (reclaim["observations"], reclaim["total_tokens"]) == (4, 220_000)
    assert (reclaim["average_tokens"], reclaim["p50_tokens"], reclaim["p95_tokens"]) == (
        55_000,
        50_000,
        70_000,
    )
    top = compactions["top_sessions"]
    assert (top[0]["session_id"], top[0]["compactions"]) == (second_session_id, 2)
    assert top[0]["estimated_reclaimed_tokens"] == 90_000
    assert {row["session_id"] for row in top[1:]} == {source.id, fork.id}


# -- Session scopes -------------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_counts_only_its_own_activity_never_inherited_history(
    manager: ChatSessionManager, statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    source = manager.create("main")
    source_messages = [
        ChatMessage.user("source work", timestamp=BASE),
        _assistant(
            model=SONNET,
            at=BASE + timedelta(seconds=1),
            usage={"input_tokens": 100, "output_tokens": 20},
        ),
        _tool(
            name="edit",
            at=BASE + timedelta(seconds=2),
            envelope=tool_success({"changed": True}),
            duration_ms=4,
        ),
        ChatMessage.error("source_error", "source failure", timestamp=BASE + timedelta(seconds=3)),
        _run_summary(
            status="completed",
            at=BASE + timedelta(seconds=4),
            duration_ms=1000,
            run_id="source-run",
        ),
    ]
    seed_history(source, source_messages)

    fork = await manager.fork(SessionAddress(None, "main", source.id))
    # The fork inherits the source's history into its current view without
    # writing any of it into its own audit.
    assert fork.load() == []
    fork_messages = [
        ChatMessage.user("fork work", timestamp=BASE + timedelta(minutes=1)),
        _assistant(
            model=SONNET,
            at=BASE + timedelta(minutes=1, seconds=1),
            usage={"input_tokens": 50, "output_tokens": 10},
        ),
        _tool(
            name="edit",
            at=BASE + timedelta(minutes=1, seconds=2),
            envelope=tool_failure("ambiguous_match", "choose one"),
            duration_ms=2,
        ),
        ChatMessage.error(
            "fork_error", "fork failure", timestamp=BASE + timedelta(minutes=1, seconds=3)
        ),
        _run_summary(
            status="failed",
            at=BASE + timedelta(minutes=1, seconds=4),
            duration_ms=500,
            run_id="fork-run",
        ),
    ]
    seed_history(fork, fork_messages)

    report = statistics(usage_recorder=ledger, clock=_clock).report()

    assert report["overview"]["active_sessions"] == 2
    assert report["runs"]["totals"]["completed"] == 1
    assert report["runs"]["totals"]["failed"] == 1
    assert report["usage"]["totals"]["input_tokens"] == 150
    assert report["usage"]["totals"]["output_tokens"] == 30
    assert report["runs"]["errors"]["total"] == 2
    assert report["tools"]["tools"] == [
        {
            "name": "edit",
            "calls": 2,
            "accepted": 1,
            "rejected": 1,
            "rejection_rate": 0.5,
            "p50_ms": 2,
            "p95_ms": 4,
            "max_ms": 4,
            "total_ms": 6,
            "time_share": 1.0,
            "top_codes": [{"code": "ambiguous_match", "count": 1}],
        }
    ]
    assert report["diagnostics"]["roles"]["session_records_by_role"]["user"] == 2


def test_identity_and_project_sessions_count_once_under_distinct_agent_keys(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    # The same bare Agent id and Session id exist as an identity Session and
    # inside a project; they are different Sessions and each counts once.
    for project_id, run_id in ((None, "identity-run"), ("vbot", "project-run")):
        _write_session(
            manager,
            "builder",
            [
                _assistant(model="openai/gpt-5", at=BASE),
                _run_summary(status="completed", at=BASE, duration_ms=100, run_id=run_id),
            ],
            project_id=project_id,
            session_id="shared-session",
        )

    report = statistics(["builder"], projects={"vbot": ["builder"]}, clock=_clock).report()

    assert report["overview"]["active_agents"] == 2
    assert report["overview"]["active_sessions"] == 2
    assert {row["agent_id"]: row["runs"] for row in report["runs"]["agents"]} == {
        "builder": 1,
        "builder@vbot": 1,
    }


def test_an_empty_project_directory_changes_no_figure(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model="openai/gpt-5", at=BASE, usage={"input_tokens": 10, "output_tokens": 2}
            ),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=1), duration_ms=300, run_id="r1"
            ),
        ],
    )

    identity_only = statistics(["main"], clock=_clock).report()
    with_projects = statistics(["main"], projects={}, clock=_clock).report()

    assert with_projects == identity_only


# -- Run activity ---------------------------------------------------------------


def test_run_activity_returns_overlapping_runs_with_local_usage(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    checkpoint = _compaction(at=BASE + timedelta(minutes=3), before=2000, after=400)
    checkpoint = replace(
        checkpoint,
        usage={
            **(checkpoint.usage or {}),
            "model_call": {
                "model": "summary/m",
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 30,
                    "output_tokens_estimated": True,
                    "estimated": True,
                },
            },
        },
    )
    session_id = _write_session(
        manager,
        "main",
        [
            _assistant(
                model="openai/gpt-5", at=BASE, usage={"input_tokens": 100, "output_tokens": 20}
            ),
            _assistant(
                model="openai/gpt-5",
                at=BASE + timedelta(minutes=1),
                usage={
                    "input_tokens": 10,
                    "output_tokens": 3,
                    "input_tokens_estimated": True,
                    "output_tokens_estimated": True,
                    "estimated": True,
                },
            ),
            _tool(
                name="read",
                at=BASE + timedelta(minutes=2),
                envelope=tool_success({"text": "ok"}),
                duration_ms=20,
            ),
            checkpoint,
            _run_summary(status="completed", at=BASE, duration_ms=10 * 60 * 1000, run_id="r1"),
            _assistant(
                model="openai/gpt-5",
                at=BASE + timedelta(hours=2),
                usage={"input_tokens": 200, "output_tokens": 30},
            ),
            _run_summary(
                status="completed", at=BASE + timedelta(hours=2), duration_ms=100, run_id="r2"
            ),
        ],
    )

    report = statistics().run_activity(
        since=BASE + timedelta(minutes=5), until=BASE + timedelta(minutes=6)
    )

    assert report.total_runs == 1
    assert report.truncated is False
    run = report.runs[0]
    assert run.session_id == session_id
    assert run.run_id == "r1"
    assert run.models == ["openai/gpt-5", "summary/m"]
    assert run.tool_calls == 1
    assert run.measured_input_tokens == 1100
    assert run.measured_output_tokens == 20
    assert run.estimated_input_tokens == 10
    assert run.estimated_output_tokens == 33


@pytest.mark.asyncio
async def test_run_activity_counts_compaction_without_chat_steps(
    manager: ChatSessionManager, statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    checkpoint = _compaction(at=BASE, before=1000, after=200)
    checkpoint = replace(
        checkpoint,
        usage={
            **(checkpoint.usage or {}),
            "model_call": {
                "model": "summary/m",
                "usage": {"input_tokens": 900, "output_tokens": 100},
            },
        },
    )
    _write_session(
        manager,
        "main",
        [
            checkpoint,
            _run_summary(status="completed", at=BASE, duration_ms=1000, run_id="compact"),
        ],
    )
    service = statistics(usage_recorder=ledger, clock=_clock)

    activity = service.run_activity(since=BASE, until=BASE + timedelta(seconds=1))
    [run] = activity.runs
    usage = service.report(sections=["usage"])["usage"]
    assert activity.total_runs == 1
    assert run.models == ["summary/m"]
    assert run.measured_input_tokens == usage["totals"]["input_tokens"] == 900
    assert run.measured_output_tokens == usage["totals"]["output_tokens"] == 100
    assert run.tool_calls == 0
    assert [(row["key"], row["calls"]) for row in usage["breakdowns"]["kind"]] == [
        ("compaction", 1)
    ]


@pytest.mark.parametrize(("runs", "truncated"), [(3, False), (4, True)])
def test_run_activity_returns_the_newest_bounded_runs_with_the_total(
    manager: ChatSessionManager,
    statistics: StatisticsFactory,
    monkeypatch: pytest.MonkeyPatch,
    runs: int,
    truncated: bool,
) -> None:
    monkeypatch.setattr("core.statistics.statistics.MAX_RUN_ACTIVITY", 3)
    _write_session(
        manager,
        "main",
        [
            _run_summary(
                status="completed",
                at=BASE + timedelta(minutes=index),
                duration_ms=1000,
                run_id=f"run-{index}",
            )
            for index in range(runs)
        ],
    )

    activity = statistics().run_activity(since=BASE, until=BASE + timedelta(days=1))

    assert activity.total_runs == runs
    assert activity.truncated is truncated
    assert [run.run_id for run in activity.runs] == [
        f"run-{index}" for index in range(runs - 1, runs - 4, -1)
    ]
