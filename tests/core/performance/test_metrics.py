"""Log-bucket histograms, gauges and the distinct-name cap."""

from __future__ import annotations

import math
import threading

import pytest

from core.performance._metrics import (
    BUCKET_FACTOR,
    MAX_BUCKET_MS,
    MIN_BUCKET_MS,
    OVERFLOW_BUCKET,
    UNDERFLOW_BUCKET,
    MetricRegistry,
    bucket_bounds,
    bucket_index,
)


def test_buckets_cover_the_range_with_ten_percent_width_and_edge_buckets() -> None:
    assert bucket_index(0.0) == UNDERFLOW_BUCKET
    assert bucket_index(MIN_BUCKET_MS / 2) == UNDERFLOW_BUCKET
    assert bucket_index(MAX_BUCKET_MS) == OVERFLOW_BUCKET
    assert bucket_index(MAX_BUCKET_MS * 10) == OVERFLOW_BUCKET
    for value in (MIN_BUCKET_MS, 0.5, 1.0, 17.3, 250.0, 60_000.0, MAX_BUCKET_MS * 0.999):
        index = bucket_index(value)
        lower, upper = bucket_bounds(index)
        assert UNDERFLOW_BUCKET < index < OVERFLOW_BUCKET
        assert lower * (1 - 1e-9) <= value < upper * (1 + 1e-9)
        assert upper / lower == pytest.approx(BUCKET_FACTOR)
    assert bucket_bounds(OVERFLOW_BUCKET) == (MAX_BUCKET_MS, math.inf)


def test_summary_reports_totals_and_bucket_estimated_percentiles() -> None:
    registry = MetricRegistry(max_names=10)
    for value in range(1, 101):
        registry.observe("chat.persist", float(value))

    summary = registry.histogram_summaries()["chat.persist"]

    assert summary["count"] == 100
    assert summary["sum_ms"] == 5050.0
    assert summary["min_ms"] == 1.0
    assert summary["max_ms"] == 100.0
    # Bucket estimates stay within one bucket width of the exact rank value.
    assert summary["p50_ms"] == pytest.approx(50.0, rel=BUCKET_FACTOR - 1)
    assert summary["p90_ms"] == pytest.approx(90.0, rel=BUCKET_FACTOR - 1)
    assert summary["p99_ms"] == pytest.approx(99.0, rel=BUCKET_FACTOR - 1)


def test_percentiles_are_clamped_to_observed_extremes() -> None:
    registry = MetricRegistry(max_names=10)
    registry.observe("single", 3.0)
    registry.observe("tiny", 0.001)
    registry.observe("huge", MAX_BUCKET_MS * 2)

    summaries = registry.histogram_summaries()

    assert summaries["single"]["p50_ms"] == summaries["single"]["p99_ms"] == 3.0
    assert summaries["tiny"]["p99_ms"] == 0.001
    assert summaries["huge"]["p50_ms"] == MAX_BUCKET_MS * 2


def test_negative_and_nan_durations_count_as_zero() -> None:
    registry = MetricRegistry(max_names=10)
    registry.observe("clock", -5.0)
    registry.observe("clock", math.nan)

    summary = registry.histogram_summaries()["clock"]

    assert summary["count"] == 2
    assert summary["sum_ms"] == 0.0
    assert summary["max_ms"] == 0.0


def test_distinct_name_cap_rejects_new_names_but_keeps_existing_ones() -> None:
    registry = MetricRegistry(max_names=2)

    assert registry.observe("a", 1.0)
    assert registry.observe("b", 1.0)
    assert not registry.observe("c", 1.0)
    assert registry.observe("a", 2.0)
    assert registry.set_gauge("g1", 1)
    assert registry.set_gauge("g2", 2)
    assert not registry.set_gauge("g3", 3)
    assert registry.set_gauge("g3", 3, capped=False)

    assert set(registry.histogram_summaries()) == {"a", "b"}
    assert registry.histogram_summaries()["a"]["count"] == 2
    assert registry.gauges() == {"g1": 1, "g2": 2, "g3": 3}


def test_gauges_keep_last_value_and_raise_gauge_keeps_the_maximum() -> None:
    registry = MetricRegistry(max_names=10)
    registry.set_gauge("runs.active", 3)
    registry.set_gauge("runs.active", 1)
    registry.raise_gauge("peak", 2)
    registry.raise_gauge("peak", 7)
    registry.raise_gauge("peak", 4)

    assert registry.gauges() == {"peak": 7, "runs.active": 1}


def test_concurrent_observations_are_all_counted() -> None:
    registry = MetricRegistry(max_names=10)

    def observe() -> None:
        for _ in range(2000):
            registry.observe("sqlite.write", 1.5)

    threads = [threading.Thread(target=observe) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert registry.histogram_summaries()["sqlite.write"]["count"] == 16_000
