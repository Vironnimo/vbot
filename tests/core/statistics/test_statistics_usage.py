"""Tests for statistics usage."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.chat.messages import ChatMessage
from tests.core.statistics.statistics_test_support import (
    BASE,
    _assistant,
    _run_summary,
    _service,
    _write_session,
)


def test_measured_and_estimated_tokens_stay_separate(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
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
                    "estimated": True,
                },
            ),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=2), duration_ms=1000, run_id="r1"
            ),
        ],
    )

    report = service.report()
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
    assert model_usage.model == "openrouter/anthropic/claude-sonnet-4"
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


def test_partial_usage_keeps_provider_output_measured(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
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

    report = service.report()
    totals = report.usage.totals

    assert totals.measured_input_tokens == 0
    assert totals.measured_output_tokens == 2572
    assert totals.estimated_input_tokens == 134_547
    assert totals.estimated_output_tokens == 0
    assert totals.measured_turns == 0
    assert totals.estimated_turns == 1
    assert report.usage.models[0].total_tokens == 137_119


def test_cache_totals_split_per_provider_model_and_day(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    cached_model = "anthropic/claude-sonnet-4"
    plain_model = "ollama/llama3"
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model=cached_model,
                at=BASE,
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 20,
                    "cache_read_tokens": 700,
                    "cache_write_tokens": 100,
                },
            ),
            _assistant(
                model=cached_model,
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
                model=plain_model,
                at=BASE + timedelta(seconds=20),
                usage={"input_tokens": 500, "output_tokens": 10},
            ),
            _assistant(
                model=cached_model,
                at=BASE + timedelta(seconds=30),
                usage={"input_tokens": 9, "output_tokens": 1, "estimated": True},
            ),
        ],
    )

    report = service.report()
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

    cached_model_usage = next(m for m in report.usage.models if m.model == cached_model)
    assert cached_model_usage.cache_turns == 2
    assert cached_model_usage.cache_read_tokens == 2500

    day = report.usage.daily[0]
    assert day.cache_input_tokens == 3000
    assert day.cache_read_tokens == 2500
    assert day.cache_write_tokens == 150


def test_session_cache_records_sorted_worst_hit_rate_first(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    model = "anthropic/claude-sonnet-4"
    good_session = _write_session(
        manager,
        "main",
        [
            _assistant(
                model=model,
                at=BASE,
                usage={"input_tokens": 1000, "output_tokens": 10, "cache_read_tokens": 900},
            ),
            _assistant(
                model=model,
                at=BASE + timedelta(seconds=5),
                usage={"input_tokens": 1000, "output_tokens": 10, "cache_read_tokens": 900},
            ),
        ],
    )
    bad_session = _write_session(
        manager,
        "main",
        [
            _assistant(
                model=model,
                at=BASE,
                usage={"input_tokens": 1000, "output_tokens": 10, "cache_read_tokens": 100},
            ),
            _assistant(
                model=model,
                at=BASE + timedelta(seconds=5),
                usage={"input_tokens": 1000, "output_tokens": 10, "cache_read_tokens": 100},
            ),
        ],
    )
    # A single cache-reporting turn is not enough for a meaningful hit rate.
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model=model,
                at=BASE,
                usage={"input_tokens": 1000, "output_tokens": 10, "cache_read_tokens": 0},
            ),
        ],
    )

    report = service.report()
    records = report.usage.cache.lowest_hit_rate_sessions

    assert [record.session_id for record in records] == [bad_session, good_session]
    assert records[0].hit_rate == pytest.approx(0.1)
    assert records[0].cache_turns == 2
    assert records[0].input_tokens == 2000
    assert records[0].cache_read_tokens == 200
    assert records[1].hit_rate == pytest.approx(0.9)
    assert records[0].last_activity is not None


def test_suspected_cache_break_detected_for_prefix_collapse(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    model = "anthropic/claude-sonnet-4"
    session_id = _write_session(
        manager,
        "main",
        [
            _assistant(
                model=model,
                at=BASE,
                usage={"input_tokens": 10000, "output_tokens": 50, "cache_read_tokens": 9000},
            ),
            _assistant(
                model=model,
                at=BASE + timedelta(seconds=30),
                usage={"input_tokens": 11000, "output_tokens": 60, "cache_read_tokens": 500},
            ),
        ],
    )

    report = service.report()
    breaks = report.usage.cache.suspected_breaks

    assert breaks.evaluated_turns == 1
    assert breaks.suspected_turns == 1
    incident = breaks.incidents[0]
    assert incident.agent_id == "main"
    assert incident.session_id == session_id
    assert incident.model == model
    assert incident.previous_input_tokens == 10000
    assert incident.cache_read_tokens == 500


def test_cache_break_heuristic_skips_legitimate_prefix_changes(tmp_path: Path) -> None:
    service, manager = _service(tmp_path, ["main"])
    model = "anthropic/claude-sonnet-4"
    other_model = "anthropic/claude-haiku-4"

    def cached_turn(at: datetime, *, model_id: str = model, cache_read: int = 0) -> ChatMessage:
        return _assistant(
            model=model_id,
            at=at,
            usage={"input_tokens": 10000, "output_tokens": 10, "cache_read_tokens": cache_read},
        )

    # Model switch between turns: not evaluated.
    _write_session(
        manager,
        "main",
        [
            cached_turn(BASE),
            cached_turn(BASE + timedelta(seconds=10), model_id=other_model),
        ],
    )
    # Idle gap beyond the provider cache TTL: not evaluated.
    _write_session(
        manager,
        "main",
        [
            cached_turn(BASE),
            cached_turn(BASE + timedelta(seconds=301)),
        ],
    )
    # Compaction checkpoint between turns rebuilds the prefix: not evaluated.
    _write_session(
        manager,
        "main",
        [
            cached_turn(BASE),
            ChatMessage.compaction_checkpoint(
                summary="compacted",
                projection=[ChatMessage.user("tail")],
                compacted_token_count=100,
                timestamp=BASE + timedelta(seconds=5),
            ),
            cached_turn(BASE + timedelta(seconds=10)),
        ],
    )
    # Previous prompt below the minimum cacheable size: not evaluated.
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model=model,
                at=BASE,
                usage={"input_tokens": 500, "output_tokens": 5, "cache_read_tokens": 0},
            ),
            cached_turn(BASE + timedelta(seconds=10)),
        ],
    )
    # Healthy continuation: evaluated, not suspected.
    _write_session(
        manager,
        "main",
        [
            cached_turn(BASE, cache_read=9000),
            cached_turn(BASE + timedelta(seconds=10), cache_read=9800),
        ],
    )

    report = service.report()
    breaks = report.usage.cache.suspected_breaks

    assert breaks.evaluated_turns == 1
    assert breaks.suspected_turns == 0
    assert breaks.incidents == []
