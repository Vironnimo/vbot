"""Usage and costs in the Statistics report: tokens, cache figures, prices and Compaction calls."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from core.chat.messages import ChatMessage
from core.models.pricing import TokenPricing, price_usage
from core.sessions import ChatSessionManager
from tests.core.statistics.statistics_test_support import (
    BASE,
    StatisticsFactory,
    _assistant,
    _compaction,
    _run_summary,
    _write_session,
)

CLAUDE = "anthropic/claude-sonnet-4"


def test_measured_and_estimated_tokens_stay_separate(
    manager: ChatSessionManager, statistics: StatisticsFactory
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

    report = statistics().report()
    totals = report.usage.totals

    assert totals.measured_input_tokens == 100
    assert totals.measured_output_tokens == 20
    assert totals.estimated_input_tokens == 7
    assert totals.estimated_output_tokens == 3
    assert totals.measured_turns == 1
    assert totals.estimated_turns == 1
    assert totals.cache_read_tokens == 30
    assert totals.reasoning_tokens == 12
    assert totals.reasoning_turns == 1

    model_usage = report.usage.models[0]
    assert model_usage.provider == "openrouter"
    assert model_usage.model == model
    assert model_usage.measured_input_tokens == 100
    assert model_usage.estimated_input_tokens == 7
    assert model_usage.reasoning_tokens == 12
    assert model_usage.reasoning_turns == 1
    # Reasoning is already included in measured output, so it is not added.
    assert model_usage.total_tokens == 130
    assert model_usage.runs == 1

    provider_usage = report.usage.providers[0]
    assert provider_usage.reasoning_tokens == 12
    assert provider_usage.reasoning_turns == 1
    assert provider_usage.total_tokens == 130

    day = report.usage.daily[0]
    assert day.reasoning_tokens == 12
    assert day.reasoning_turns == 1


def test_partial_usage_keeps_provider_output_measured(
    manager: ChatSessionManager, statistics: StatisticsFactory
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

    report = statistics().report()
    totals = report.usage.totals

    assert totals.measured_input_tokens == 0
    assert totals.measured_output_tokens == 2572
    assert totals.estimated_input_tokens == 134_547
    assert totals.estimated_output_tokens == 0
    assert totals.measured_turns == 0
    assert totals.estimated_turns == 1
    assert report.usage.models[0].total_tokens == 137_119


# -- Cache figures ------------------------------------------------------------


def test_cache_totals_split_per_provider_model_and_day(
    manager: ChatSessionManager, statistics: StatisticsFactory
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
            # totals but never into cache-turn denominators.
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

    report = statistics().report()
    totals = report.usage.totals

    assert totals.cache_turns == 2
    assert totals.cache_input_tokens == 3000
    assert totals.cache_read_tokens == 2500
    assert totals.cache_write_tokens == 150

    cached_provider = next(p for p in report.usage.providers if p.provider == "anthropic")
    assert cached_provider.cache_turns == 2
    assert cached_provider.cache_input_tokens == 3000
    assert cached_provider.cache_read_tokens == 2500
    assert cached_provider.cache_write_tokens == 150

    plain_provider = next(p for p in report.usage.providers if p.provider == "ollama")
    assert plain_provider.cache_turns == 0
    assert plain_provider.cache_input_tokens == 0
    assert plain_provider.cache_read_tokens == 0

    cached_model_usage = next(m for m in report.usage.models if m.model == CLAUDE)
    assert cached_model_usage.cache_turns == 2
    assert cached_model_usage.cache_read_tokens == 2500

    day = report.usage.daily[0]
    assert day.cache_input_tokens == 3000
    assert day.cache_read_tokens == 2500
    assert day.cache_write_tokens == 150


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

    records = statistics().report().usage.cache.lowest_hit_rate_sessions

    assert [record.session_id for record in records] == [bad_session, good_session]
    assert records[0].hit_rate == pytest.approx(0.1)
    assert records[0].cache_turns == 2
    assert records[0].input_tokens == 2000
    assert records[0].cache_read_tokens == 200
    assert records[1].hit_rate == pytest.approx(0.9)
    assert records[0].last_activity is not None


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

    breaks = statistics().report().usage.cache.suspected_breaks

    assert breaks.evaluated_turns == 2
    assert breaks.suspected_turns == 1
    [incident] = breaks.incidents
    assert incident.agent_id == "main"
    assert incident.session_id == collapsed
    assert incident.model == CLAUDE
    assert incident.previous_input_tokens == 10000
    assert incident.cache_read_tokens == 500


# -- Costs --------------------------------------------------------------------


def test_costs_preserve_saved_prices_and_reprice_retrospective_calls(
    manager: ChatSessionManager, statistics: StatisticsFactory
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
            _assistant(model="test/m", at=BASE + timedelta(seconds=1), usage=usage),
            _assistant(
                model="test/m", at=BASE + timedelta(seconds=2), usage={"reported_cost_usd": 0}
            ),
            _assistant(model="test/missing", at=BASE + timedelta(seconds=3), usage=usage),
        ],
    )
    source = "models.dev:test/m"
    prices = {"test/m": TokenPricing.from_cost({"input": 10, "output": 20}, source=source)}
    service = statistics(pricing_lookup=prices.get)

    report = service.report()

    totals = report.costs.totals
    assert (totals.calls, totals.reported_calls, totals.estimated_calls, totals.unpriced_calls) == (
        4,
        1,
        2,
        1,
    )
    assert totals.reported_usd == 0
    # The saved snapshot (0.0012) plus the call priced at the current rate (0.012).
    assert totals.estimated_usd == pytest.approx(0.0132)
    assert totals.retrospective_calls == 1
    assert report.costs.recent_calls[-1].cost["pricing"]["rates"]["input"] == 1
    assert report.costs.recent_calls[1].input_tokens is None
    assert report.costs.recent_calls[-1].model == "test/m"
    # Reading the persisted index again must not change the calculation.
    assert service.report().costs == report.costs
    narrowed = service.report(since=BASE + timedelta(seconds=2))
    assert narrowed.costs.totals.calls == 2
    assert narrowed.costs.totals.estimated_usd is None
    assert narrowed.usage.totals.unreported_calls == 1

    # A changed catalog price reprices only the retrospective call.
    prices["test/m"] = TokenPricing.from_cost({"input": 100, "output": 200}, source=source)
    repriced = service.report().costs.totals
    assert repriced.retrospective_calls == 1
    assert repriced.estimated_usd == pytest.approx(0.0012 + 0.12)


def test_compaction_usage_and_context_are_counted_once(
    manager: ChatSessionManager, statistics: StatisticsFactory
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
    second = _compaction(at=BASE + timedelta(seconds=2), before=400, after=400)
    second = replace(second, usage={**(second.usage or {}), "context_tokens_after": 450})
    _write_session(
        manager,
        "main",
        [
            first,
            _assistant(
                model="test/m",
                at=BASE + timedelta(seconds=1),
                usage={"input_tokens": 330, "output_tokens": 10},
            ),
            second,
            _assistant(
                model="test/m",
                at=BASE + timedelta(seconds=3),
                usage={
                    "input_tokens": 460,
                    "output_tokens": 10,
                    "input_tokens_estimated": True,
                    "estimated": True,
                },
            ),
        ],
    )
    service = statistics()

    report = service.report()

    assert report.costs.totals.calls == 3
    assert report.costs.compactions.reported_usd == 0.004
    assert report.usage.totals.assistant_messages == 2
    assert report.usage.totals.model_calls == 3
    assert report.usage.totals.compaction_calls == 1
    assert report.usage.totals.measured_input_tokens == 1130
    context = report.compactions.context
    assert context.average_after_tokens == 375
    assert context.reduction_ratio == pytest.approx(650 / 1400)
    assert context.non_shrinking == 1
    assert context.average_steps_between == 1
    assert context.rapid_recompactions == 1
    assert context.duration_observations == 1
    assert context.average_duration_ms == 1200
    assert context.average_next_input_tokens == 330
    assert context.next_input_observations == 1
    # The previous checkpoint outside the range still establishes the interval.
    narrowed = service.report(since=BASE + timedelta(seconds=2))
    assert narrowed.compactions.context.average_steps_between == 1
    assert narrowed.costs.compactions.calls == 0


def test_recent_calls_use_time_order_and_are_bounded(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    # 65 calls: the report keeps the newest 50.
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model="test/m",
                at=BASE + timedelta(seconds=second),
                usage={"reported_cost_usd": 0.001},
            )
            for second in range(65)
        ],
    )

    costs = statistics().report().costs

    assert costs.totals.calls == 65
    assert len(costs.recent_calls) == 50
    assert costs.recent_calls_truncated
    assert costs.recent_calls[0].timestamp == "2026-06-01T12:01:04.000000Z"
