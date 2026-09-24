"""Thread-safe log-scale duration histograms and last-value gauges.

Durations are milliseconds. Buckets grow by a fixed factor (about 10% relative
width) from ``MIN_BUCKET_MS`` to ``MAX_BUCKET_MS``, with one underflow and one
overflow bucket, so percentiles cost no per-observation storage. A registry
caps its distinct names; callers decide how a rejected new name is reported.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

BUCKET_FACTOR = 1.1
MIN_BUCKET_MS = 0.01
MAX_BUCKET_MS = 3_600_000.0
_LOG_FACTOR = math.log(BUCKET_FACTOR)
BUCKET_COUNT = math.ceil(math.log(MAX_BUCKET_MS / MIN_BUCKET_MS) / _LOG_FACTOR)
UNDERFLOW_BUCKET = 0
OVERFLOW_BUCKET = BUCKET_COUNT + 1
_PERCENTILES = (("p50_ms", 0.50), ("p90_ms", 0.90), ("p99_ms", 0.99))
_MIDPOINT_FACTOR = math.sqrt(BUCKET_FACTOR)


def bucket_index(ms: float) -> int:
    """Return the histogram bucket for one non-negative duration."""
    if ms < MIN_BUCKET_MS:
        return UNDERFLOW_BUCKET
    if ms >= MAX_BUCKET_MS:
        return OVERFLOW_BUCKET
    return min(int(math.log(ms / MIN_BUCKET_MS) / _LOG_FACTOR) + 1, BUCKET_COUNT)


def bucket_bounds(index: int) -> tuple[float, float]:
    """Return the ``[lower, upper)`` millisecond range covered by one bucket."""
    if index <= UNDERFLOW_BUCKET:
        return 0.0, MIN_BUCKET_MS
    if index >= OVERFLOW_BUCKET:
        return MAX_BUCKET_MS, math.inf
    lower = MIN_BUCKET_MS * BUCKET_FACTOR ** (index - 1)
    return lower, lower * BUCKET_FACTOR


@dataclass(slots=True)
class HistogramData:
    """Raw counts of one histogram; summaries are derived outside any lock."""

    count: int = 0
    total_ms: float = 0.0
    min_ms: float = math.inf
    max_ms: float = 0.0
    buckets: dict[int, int] = field(default_factory=dict)

    def add(self, ms: float, index: int) -> None:
        self.count += 1
        self.total_ms += ms
        if ms < self.min_ms:
            self.min_ms = ms
        if ms > self.max_ms:
            self.max_ms = ms
        self.buckets[index] = self.buckets.get(index, 0) + 1

    def copy(self) -> HistogramData:
        return HistogramData(
            self.count, self.total_ms, self.min_ms, self.max_ms, dict(self.buckets)
        )

    def summary(self) -> dict[str, float | int]:
        """Return count, sum, extremes and bucket-estimated percentiles."""
        result: dict[str, float | int] = {
            "count": self.count,
            "sum_ms": _rounded(self.total_ms),
            "min_ms": _rounded(self.min_ms if self.count else 0.0),
            "max_ms": _rounded(self.max_ms),
        }
        ordered = sorted(self.buckets.items())
        for key, quantile in _PERCENTILES:
            result[key] = _rounded(self._percentile(ordered, quantile))
        return result

    def _percentile(self, ordered: list[tuple[int, int]], quantile: float) -> float:
        if self.count == 0:
            return 0.0
        rank = max(1, math.ceil(quantile * self.count))
        seen = 0
        for index, bucket_count in ordered:
            seen += bucket_count
            if seen >= rank:
                return self._estimate(index)
        return self.max_ms

    def _estimate(self, index: int) -> float:
        if index <= UNDERFLOW_BUCKET:
            return self.min_ms
        if index >= OVERFLOW_BUCKET:
            return self.max_ms
        lower, _upper = bucket_bounds(index)
        return min(max(lower * _MIDPOINT_FACTOR, self.min_ms), self.max_ms)


def _rounded(value: float) -> float:
    return round(value, 3)


class MetricRegistry:
    """Named histograms and gauges guarded by one lock, with a distinct-name cap."""

    def __init__(self, *, max_names: int) -> None:
        self._max_names = max_names
        self._lock = threading.Lock()
        self._histograms: dict[str, HistogramData] = {}
        self._gauges: dict[str, float] = {}

    def observe(self, name: str, ms: float) -> bool:
        """Add one duration; return False when a new name exceeds the cap."""
        if not ms >= 0.0:
            # Negative (clock skew) and NaN durations carry no usable timing.
            ms = 0.0
        index = bucket_index(ms)
        with self._lock:
            histogram = self._histograms.get(name)
            if histogram is None:
                if len(self._histograms) >= self._max_names:
                    return False
                histogram = self._histograms[name] = HistogramData()
            histogram.add(ms, index)
        return True

    def set_gauge(self, name: str, value: float, *, capped: bool = True) -> bool:
        """Store the latest gauge value; return False when a new name exceeds the cap."""
        with self._lock:
            if capped and name not in self._gauges and len(self._gauges) >= self._max_names:
                return False
            self._gauges[name] = value
        return True

    def raise_gauge(self, name: str, value: float) -> bool:
        """Keep the maximum value seen for one gauge name."""
        with self._lock:
            current = self._gauges.get(name)
            if current is None:
                if len(self._gauges) >= self._max_names:
                    return False
                self._gauges[name] = value
            elif value > current:
                self._gauges[name] = value
        return True

    def histogram_summaries(self) -> dict[str, dict[str, float | int]]:
        """Return per-name summaries sorted by name, computed outside the lock."""
        with self._lock:
            copies = {name: histogram.copy() for name, histogram in self._histograms.items()}
        return {name: copies[name].summary() for name in sorted(copies)}

    def gauges(self) -> dict[str, float]:
        """Return the current gauge values sorted by name."""
        with self._lock:
            values = dict(self._gauges)
        return {name: values[name] for name in sorted(values)}
