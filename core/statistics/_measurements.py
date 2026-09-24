"""Pure normalization of Statistics usage, timing, envelope and count measurements."""

from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any

from core.chat.model_resolution import parse_bare_model
from core.statistics.report import (
    CountEntry,
    JsonObject,
)
from core.statistics.timestamps import parse_timestamp
from core.tools import is_tool_result_envelope

UNKNOWN_MODEL_KEY = "unknown"


def _provider_model_key(model: str | None) -> str:
    """Return the bare ``<provider>/<model-id>`` key, ``unknown`` when absent."""
    if not model:
        return UNKNOWN_MODEL_KEY
    return parse_bare_model(model)


def _provider_of(model_key: str) -> str:
    """Return the Provider part of a bare Model key, the key itself without one."""
    return model_key.split("/", 1)[0] if "/" in model_key else model_key


def _parse_envelope(content: Any) -> JsonObject | None:
    """Parse a tool result envelope; return ``None`` when it is not one.

    Only ``ok`` and ``error.code`` are consumed — tool arguments and result data
    never enter the report.
    """
    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict) or not is_tool_result_envelope(parsed):
        return None
    return parsed


def _duration_ms(timing: JsonObject | None) -> int | None:
    if not isinstance(timing, dict):
        return None
    value = timing.get("duration_ms")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _timing_field(timing: JsonObject | None, key: str) -> str | None:
    if not isinstance(timing, dict):
        return None
    value = timing.get(key)
    return value if isinstance(value, str) else None


def _max_timestamp(current: str | None, candidate: str) -> str:
    if current is None:
        return candidate
    current_parsed = parse_timestamp(current)
    candidate_parsed = parse_timestamp(candidate)
    if current_parsed is None:
        return candidate
    if candidate_parsed is None:
        return current
    return candidate if candidate_parsed > current_parsed else current


def _mean(values: list[int]) -> float | None:
    return sum(values) / len(values) if values else None


def _usage_nonnegative_int(usage: JsonObject | None, field_name: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nearest_rank_percentile(sorted_values: list[int], percentile: float) -> float | None:
    """Nearest-rank percentile of an ascending list (``None`` when empty).

    Rank = ``ceil(p/100 * n)`` (1-indexed), so P50 of ten values is the fifth,
    P90 the ninth, and P95 the tenth — easy to reason about and to test.
    """
    if not sorted_values:
        return None
    return float(sorted_values[_nearest_rank_index(len(sorted_values), percentile)])


def _nearest_rank_index(count: int, percentile: float) -> int:
    """Zero-based position of the nearest-rank percentile among ``count`` values."""
    rank = math.ceil((percentile / 100) * count)
    return min(max(rank - 1, 0), count - 1)


def _ratio(part: int, total: int) -> float:
    return part / total if total else 0.0


def _count_entries(counter: Counter[str]) -> list[CountEntry]:
    return [
        CountEntry(key=key, count=count)
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]
