"""Pure normalization of Statistics usage, timing, envelope and count measurements."""

from __future__ import annotations

import json
import math
from typing import Any

from core.chat.model_resolution import parse_bare_model
from core.statistics.report import JsonObject
from core.tools import is_tool_result_envelope

UNKNOWN_MODEL_KEY = "unknown"
SPEED_BASES = ("output", "answer", "provider")


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
    except ValueError, TypeError:
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


def _mean(values: list[int]) -> float | None:
    return sum(values) / len(values) if values else None


def _usage_nonnegative_int(usage: JsonObject | None, field_name: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _speed_facts(usage: JsonObject) -> tuple[int | None, int | None, bool, int | None, str]:
    """A call's measured output-speed window and its time to first token.

    Returns ``(tokens, window_ms, estimated, first_token_ms, basis)`` from Chat's
    ``usage.speed`` (``chat/usage.md`` -> Output speed). A window counts only
    with positive whole tokens and duration, so every measured call weighs its
    tokens alike in a token-weighted speed; ``estimated`` marks a counted
    window whose tokens are a local estimate.
    """
    speed = usage.get("speed")
    if not isinstance(speed, dict):
        return None, None, False, None, "unknown"
    tokens = _usage_nonnegative_int(speed, "tokens")
    window_ms = _usage_nonnegative_int(speed, "window_ms")
    if not tokens or not window_ms:
        tokens = window_ms = None
    estimated = tokens is not None and speed.get("estimated") is True
    basis = speed.get("basis")
    return (
        tokens,
        window_ms,
        estimated,
        _usage_nonnegative_int(speed, "first_token_ms"),
        basis if basis in SPEED_BASES else "unknown",
    )


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
