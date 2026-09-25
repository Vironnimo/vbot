"""Cost and Compaction diagnostics survive canonical SQLite and derived indexing."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from core.models.pricing import TokenPricing, price_usage
from core.statistics import StatisticsService
from tests.core.statistics.statistics_test_support import (
    BASE,
    _assistant,
    _compaction,
    _service,
    _write_session,
)


def test_costs_preserve_saved_prices_and_reprice_legacy_calls(tmp_path: Path):
    service, manager = _service(tmp_path, ["main"])
    old = TokenPricing.from_cost({"input": 1, "output": 2}, source="models.dev:test/m")
    current = TokenPricing.from_cost({"input": 10, "output": 20}, source="models.dev:test/m")
    usage = {"input_tokens": 1000, "output_tokens": 100}
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model="test/m::connection/account",
                at=BASE,
                usage={**usage, "cost": price_usage(usage, old)},
            ),
            _assistant(model="test/m", at=BASE + timedelta(seconds=1), usage=usage),
            _assistant(
                model="test/m", at=BASE + timedelta(seconds=2), usage={"reported_cost_usd": 0}
            ),
            _assistant(model="test/missing", at=BASE + timedelta(seconds=3), usage=usage),
        ],
    )
    service = StatisticsService(
        manager,
        service._agents,
        pricing_lookup=lambda model: current if model == "test/m" else None,
    )
    report = service.report()
    totals = report.costs.totals
    assert (totals.calls, totals.reported_calls, totals.estimated_calls, totals.unpriced_calls) == (
        4,
        1,
        2,
        1,
    )
    assert totals.reported_usd == 0
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


def test_compaction_usage_and_context_are_counted_once(tmp_path: Path):
    service, manager = _service(tmp_path, ["main"])
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
                usage={"input_tokens": 460, "output_tokens": 10, "input_tokens_estimated": True},
            ),
        ],
    )
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


def test_recent_calls_use_time_order_and_are_bounded(tmp_path: Path):
    service, manager = _service(tmp_path, ["main"])
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model="test/m", at=BASE + timedelta(seconds=n), usage={"reported_cost_usd": 0.001}
            )
            for n in range(65)
        ],
    )
    costs = service.report().costs
    assert costs.totals.calls == 65
    assert len(costs.recent_calls) == 50
    assert costs.recent_calls_truncated
    assert costs.recent_calls[0].timestamp == "2026-06-01T12:01:04.000000Z"
