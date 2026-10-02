"""Usage and costs in the Statistics report: tokens, cache figures, prices and Compaction calls.

Saved Session Usage reaches the report through the durable ledger, which
imports it once; the cache diagnostics read the saved Chat steps.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core.chat.messages import ChatMessage
from core.models.pricing import TokenPricing, price_usage
from core.sessions import ChatSessionManager, SessionAddress
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
    _write_session,
)

CLAUDE = "anthropic/claude-sonnet-4"
NOW = datetime(2026, 6, 2, 0, 30, tzinfo=UTC)
Report = dict[str, Any]


def _clock() -> datetime:
    return NOW


def _usage(service: Any, **request: Any) -> Report:
    usage: Report = service.report(sections=["usage"], **request)["usage"]
    return usage


def _rows(usage: Report, dimension: str) -> dict[str, Report]:
    return {row["key"]: row for row in usage["breakdowns"][dimension]}


def test_measured_and_estimated_tokens_stay_separate(
    manager: ChatSessionManager, statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    model = "openrouter/anthropic/claude-sonnet-4"
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model=model,
                at=BASE,
                usage={
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_tokens": 30,
                    "reasoning_tokens": 12,
                },
            ),
            _assistant(
                model=model,
                at=BASE + timedelta(seconds=1),
                usage={
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "reasoning_tokens": 2,
                    "input_tokens_estimated": True,
                    "output_tokens_estimated": True,
                    "estimated": True,
                },
            ),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=2), duration_ms=1000, run_id="r1"
            ),
        ],
    )

    usage = _usage(statistics(usage_recorder=ledger, clock=_clock))

    totals = usage["totals"]
    # Token counts include their estimated part, which is reported alongside.
    assert (totals["input_tokens"], totals["estimated_input_tokens"]) == (107, 7)
    assert (totals["output_tokens"], totals["estimated_output_tokens"]) == (23, 3)
    # Reasoning is part of measured output only; cache figures need measured input.
    assert totals["reasoning_tokens"] == 12
    assert (totals["cache_calls"], totals["cache_input_tokens"]) == (1, 100)
    assert totals["cache_read_tokens"] == 30
    model_row = _rows(usage, "model")[model]
    assert (model_row["input_tokens"], model_row["runs"], model_row["sessions"]) == (107, 1, 1)
    assert _rows(usage, "provider")["openrouter"]["reasoning_tokens"] == 12
    assert usage["series"][0]["reasoning_tokens"] == 12


def test_partial_usage_keeps_provider_output_measured(
    manager: ChatSessionManager, statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model="ollama-cloud/minimax-m3",
                at=BASE,
                usage={
                    "input_tokens": 134_547,
                    "input_tokens_estimated": True,
                    "output_tokens": 2572,
                    "estimated": True,
                },
            ),
            _run_summary(status="completed", at=BASE, duration_ms=1000, run_id="r1"),
        ],
    )

    totals = _usage(statistics(usage_recorder=ledger, clock=_clock))["totals"]

    assert (totals["input_tokens"], totals["estimated_input_tokens"]) == (134_547, 134_547)
    assert (totals["output_tokens"], totals["estimated_output_tokens"]) == (2572, 0)
    assert totals["unreported_calls"] == 0


# -- Breakdowns -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_breakdowns_rank_every_dimension_by_cost_with_runs_and_sessions(
    manager: ChatSessionManager, statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    for project_id, agent_id, run_id, model, cost in [
        (None, "main", "r1", "a/x", 0.3),
        ("vbot", "builder", "p1", "b/y", 0.5),
    ]:
        session = manager.create(agent_id, session_id=f"{run_id}-session", project_id=project_id)
        manager.set_title(session.address, f"Title {run_id}")
        _admit(manager, session.address, run_id)
        seed_history(
            manager.get(session.address),
            [_run_summary(status="completed", at=BASE, duration_ms=100, run_id=run_id)],
        )
        await _call(
            ledger,
            {"input_tokens": 10, "output_tokens": 1, "reported_cost_usd": cost},
            model=model,
            address=session.address,
            run_id=run_id,
        )
    # A standalone Task request belongs to no Agent, Session, Project or Run.
    await _call(
        ledger,
        {"input_tokens": 4, "output_tokens": 0, "reported_cost_usd": 0.1},
        kind="text_embedding",
        model="c/z",
        at=BASE + timedelta(minutes=1),
    )

    usage = _usage(
        statistics(["main"], projects={"vbot": ["builder"]}, clock=_clock, usage_recorder=ledger)
    )

    def ranked(dimension: str) -> list[tuple[str, float | None, int, int]]:
        return [
            (row["key"], row["cost_usd"], row["runs"], row["sessions"])
            for row in usage["breakdowns"][dimension]
        ]

    assert ranked("agent") == [("builder@vbot", 0.5, 1, 1), ("main", 0.3, 1, 1), ("", 0.1, 0, 0)]
    assert ranked("model") == [("b/y", 0.5, 1, 1), ("a/x", 0.3, 1, 1), ("c/z", 0.1, 0, 0)]
    assert ranked("provider") == [("b", 0.5, 1, 1), ("a", 0.3, 1, 1), ("c", 0.1, 0, 0)]
    # Identity (non-Project) usage has the empty Project key.
    assert ranked("project") == [("vbot", 0.5, 1, 1), ("", 0.4, 1, 1)]
    assert ranked("origin") == [("user", 0.8, 2, 2), ("background", 0.1, 0, 0)]
    assert ranked("kind") == [("chat", 0.8, 2, 2), ("text_embedding", 0.1, 0, 0)]
    assert [(row["run_id"], row["session_title"]) for row in usage["top_runs"]] == [
        ("p1", "Title p1"),
        ("r1", "Title r1"),
    ]
    assert [
        (row["agent_id"], row["session_id"], row["session_title"], row["runs"], row["cost_usd"])
        for row in usage["top_sessions"]
    ] == [
        ("builder@vbot", "p1-session", "Title p1", 1, 0.5),
        ("main", "r1-session", "Title r1", 1, 0.3),
    ]
    newest = usage["recent_calls"][0]
    assert newest == {
        "timestamp": "2026-06-01T12:01:00.000000Z",
        "model": "c/z",
        "kind": "text_embedding",
        "status": "completed",
        "origin": "background",
        "agent_id": "",
        "session_id": None,
        "session_title": None,
        "input_tokens": 4,
        "output_tokens": 0,
        "cache_read_tokens": None,
        "estimated_tokens": False,
        "retrospective": False,
        "cost": {"amount_usd": 0.1, "source": "provider"},
    }
    assert usage["recent_calls"][1]["session_title"] in {"Title p1", "Title r1"}


# -- Cache figures ------------------------------------------------------------


def test_cache_totals_split_per_provider_model_and_day(
    manager: ChatSessionManager, statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model=CLAUDE,
                at=BASE,
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 20,
                    "cache_read_tokens": 700,
                    "cache_write_tokens": 100,
                },
            ),
            _assistant(
                model=CLAUDE,
                at=BASE + timedelta(seconds=10),
                usage={
                    "input_tokens": 2000,
                    "output_tokens": 30,
                    "cache_read_tokens": 1800,
                    "cache_write_tokens": 50,
                },
            ),
            # Measured turn without any cache fields: counts into measured
            # totals but never into cache-call denominators.
            _assistant(
                model="ollama/llama3",
                at=BASE + timedelta(seconds=20),
                usage={"input_tokens": 500, "output_tokens": 10},
            ),
            _assistant(
                model=CLAUDE,
                at=BASE + timedelta(seconds=30),
                usage={
                    "input_tokens": 9,
                    "output_tokens": 1,
                    "input_tokens_estimated": True,
                    "output_tokens_estimated": True,
                    "estimated": True,
                },
            ),
        ],
    )

    usage = _usage(statistics(usage_recorder=ledger, clock=_clock))

    def cache(row: Report) -> tuple[int, int, int, int]:
        return (
            row["cache_calls"],
            row["cache_input_tokens"],
            row["cache_read_tokens"],
            row["cache_write_tokens"],
        )

    assert cache(usage["totals"]) == (2, 3000, 2500, 150)
    assert cache(_rows(usage, "provider")["anthropic"]) == (2, 3000, 2500, 150)
    assert cache(_rows(usage, "provider")["ollama"]) == (0, 0, 0, 0)
    assert cache(_rows(usage, "model")[CLAUDE]) == (2, 3000, 2500, 150)
    assert cache(usage["series"][0]) == (2, 3000, 2500, 150)


def test_session_cache_records_sorted_worst_hit_rate_first(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    def cached_session(*cache_reads: int) -> str:
        return _write_session(
            manager,
            "main",
            [
                _assistant(
                    model=CLAUDE,
                    at=BASE + timedelta(seconds=5 * index),
                    usage={"input_tokens": 1000, "output_tokens": 10, "cache_read_tokens": read},
                )
                for index, read in enumerate(cache_reads)
            ],
        )

    good_session = cached_session(900, 900)
    bad_session = cached_session(100, 100)
    # A single cache-reporting turn is not enough for a meaningful hit rate.
    cached_session(0)

    report = statistics(clock=_clock).report(sections=["diagnostics"])
    records = report["diagnostics"]["cache"]["lowest_hit_rate_sessions"]

    assert [record["session_id"] for record in records] == [bad_session, good_session]
    assert records[0]["hit_rate"] == pytest.approx(0.1)
    assert (records[0]["cache_turns"], records[0]["input_tokens"]) == (2, 2000)
    assert records[0]["cache_read_tokens"] == 200
    assert records[1]["hit_rate"] == pytest.approx(0.9)
    assert records[0]["last_activity"] is not None


def test_suspected_cache_breaks_flag_prefix_collapse_and_skip_explained_misses(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    def cached_turn(at: datetime, *, model: str = CLAUDE, cache_read: int = 0) -> ChatMessage:
        return _assistant(
            model=model,
            at=at,
            usage={"input_tokens": 10000, "output_tokens": 10, "cache_read_tokens": cache_read},
        )

    later = BASE + timedelta(seconds=10)
    # The cached prefix collapses between two comparable turns: suspected.
    collapsed = _write_session(
        manager, "main", [cached_turn(BASE, cache_read=9000), cached_turn(later, cache_read=500)]
    )
    # A healthy continuation is evaluated, not suspected.
    _write_session(
        manager, "main", [cached_turn(BASE, cache_read=9000), cached_turn(later, cache_read=9800)]
    )
    explained_misses = [
        # A Model switch between turns.
        [cached_turn(BASE), cached_turn(later, model="anthropic/claude-haiku-4")],
        # An idle gap beyond the Provider cache TTL.
        [cached_turn(BASE), cached_turn(BASE + timedelta(seconds=301))],
        # A Compaction checkpoint rebuilds the prefix.
        [
            cached_turn(BASE),
            ChatMessage.compaction_checkpoint(
                summary="compacted",
                projection=[ChatMessage.user("tail")],
                compacted_token_count=100,
                timestamp=BASE + timedelta(seconds=5),
            ),
            cached_turn(later),
        ],
        # A previous prompt below the minimum cacheable size.
        [
            _assistant(
                model=CLAUDE,
                at=BASE,
                usage={"input_tokens": 500, "output_tokens": 5, "cache_read_tokens": 0},
            ),
            cached_turn(later),
        ],
    ]
    for messages in explained_misses:
        _write_session(manager, "main", messages)

    report = statistics(clock=_clock).report(sections=["diagnostics"])
    breaks = report["diagnostics"]["cache"]["suspected_breaks"]

    assert (breaks["evaluated_turns"], breaks["suspected_turns"]) == (2, 1)
    [incident] = breaks["incidents"]
    assert (incident["agent_id"], incident["session_id"]) == ("main", collapsed)
    assert incident["model"] == CLAUDE
    assert (incident["previous_input_tokens"], incident["cache_read_tokens"]) == (10000, 500)


# -- Costs --------------------------------------------------------------------


def test_costs_preserve_saved_prices_and_reprice_retrospective_calls(
    manager: ChatSessionManager, statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    saved = TokenPricing.from_cost({"input": 1, "output": 2}, source="models.dev:test/m")
    usage = {"input_tokens": 1000, "output_tokens": 100}
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model="test/m::connection/account",
                at=BASE,
                usage={**usage, "cost": price_usage(usage, saved)},
            ),
            _assistant(model="test/m", at=BASE + timedelta(hours=1), usage=usage),
            _assistant(
                model="test/m", at=BASE + timedelta(hours=2), usage={"reported_cost_usd": 0}
            ),
            _assistant(model="test/missing", at=BASE + timedelta(hours=3), usage=usage),
        ],
    )
    source = "models.dev:test/m"
    prices = {"test/m": TokenPricing.from_cost({"input": 10, "output": 20}, source=source)}
    service = statistics(usage_recorder=ledger, pricing_lookup=prices.get, clock=_clock)

    usage_section = _usage(service)

    totals = usage_section["totals"]
    assert (
        totals["calls"],
        totals["reported_calls"],
        totals["estimated_calls"],
        totals["unpriced_calls"],
    ) == (4, 1, 2, 1)
    assert totals["reported_cost_usd"] == 0.0
    # The saved snapshot (0.0012) plus the call priced at the current rate (0.012).
    assert totals["estimated_cost_usd"] == 0.0132
    assert totals["retrospective_calls"] == 1
    recent = usage_section["recent_calls"]
    assert [row["retrospective"] for row in recent] == [False, False, True, False]
    assert recent[-1]["cost"]["pricing"]["rates"]["input"] == 1
    assert recent[-1]["model"] == "test/m"
    assert recent[1]["input_tokens"] is None
    # Reading the persisted index again must not change the calculation.
    assert _usage(service) == usage_section
    narrowed = _usage(service, since=BASE + timedelta(hours=2))["totals"]
    assert narrowed["calls"] == 2
    assert narrowed["estimated_cost_usd"] is None
    assert narrowed["unreported_calls"] == 1

    # A changed catalog price reprices only the retrospective call.
    prices["test/m"] = TokenPricing.from_cost({"input": 100, "output": 200}, source=source)
    repriced = _usage(service)["totals"]
    assert repriced["retrospective_calls"] == 1
    assert repriced["estimated_cost_usd"] == 0.1212


def test_compaction_usage_and_context_are_counted_once(
    manager: ChatSessionManager, statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    first = _compaction(at=BASE, before=1000, after=300)
    first = replace(
        first,
        usage={
            **(first.usage or {}),
            "compaction_duration_ms": 1200,
            "model_call": {
                "model": "summary/m",
                "usage": {
                    "input_tokens": 800,
                    "output_tokens": 50,
                    "cost": {"amount_usd": 0.004, "source": "provider"},
                },
            },
        },
    )
    second = _compaction(at=BASE + timedelta(hours=2), before=400, after=400)
    second = replace(second, usage={**(second.usage or {}), "context_tokens_after": 450})
    _write_session(
        manager,
        "main",
        [
            first,
            _assistant(
                model="test/m",
                at=BASE + timedelta(hours=1),
                usage={"input_tokens": 330, "output_tokens": 10},
            ),
            second,
            _assistant(
                model="test/m",
                at=BASE + timedelta(hours=3),
                usage={
                    "input_tokens": 460,
                    "output_tokens": 10,
                    "input_tokens_estimated": True,
                    "estimated": True,
                },
            ),
        ],
    )
    service = statistics(usage_recorder=ledger, clock=_clock)

    report = service.report(sections=["usage", "diagnostics"])

    totals = report["usage"]["totals"]
    assert totals["calls"] == 3
    assert (totals["input_tokens"], totals["estimated_input_tokens"]) == (1590, 460)
    kinds = _rows(report["usage"], "kind")
    assert (kinds["chat"]["calls"], kinds["compaction"]["calls"]) == (2, 1)
    assert kinds["compaction"]["reported_cost_usd"] == 0.004
    context = report["diagnostics"]["compactions"]["context"]
    assert context["average_after_tokens"] == 375
    assert context["reduction_ratio"] == pytest.approx(650 / 1400)
    assert context["non_shrinking"] == 1
    assert context["average_steps_between"] == 1
    assert context["rapid_recompactions"] == 1
    assert (context["duration_observations"], context["average_duration_ms"]) == (1, 1200)
    assert context["average_next_input_tokens"] == 330
    assert context["next_input_observations"] == 1
    # The previous checkpoint outside the range still establishes the interval.
    narrowed = service.report(since=BASE + timedelta(hours=2), sections=["usage", "diagnostics"])
    assert narrowed["diagnostics"]["compactions"]["context"]["average_steps_between"] == 1
    narrowed_kinds = _rows(narrowed["usage"], "kind")
    assert narrowed_kinds["chat"]["calls"] == 1
    # The Run that started in the window still counts under every kind it used.
    assert narrowed_kinds["compaction"]["calls"] == 0


@pytest.mark.asyncio
async def test_recent_calls_are_the_newest_fifty(
    statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    address = SessionAddress(None, "main", "busy")
    for second in range(65):
        await _call(
            ledger,
            {"reported_cost_usd": 0.001},
            address=address,
            at=BASE + timedelta(seconds=second),
        )

    usage = _usage(statistics(usage_recorder=ledger, clock=_clock))

    assert usage["totals"]["calls"] == 65
    assert len(usage["recent_calls"]) == 50
    assert usage["recent_calls"][0]["timestamp"] == "2026-06-01T12:01:04.000000Z"
    assert usage["recent_calls"][-1]["timestamp"] == "2026-06-01T12:00:15.000000Z"
    # A Session no longer listed keeps its requests under its Agent address.
    assert (usage["recent_calls"][0]["agent_id"], usage["recent_calls"][0]["session_id"]) == (
        "main",
        "busy",
    )
