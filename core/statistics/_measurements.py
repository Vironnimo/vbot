"""Pure normalization of Statistics message, usage, timing and count measurements."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

from core.chat.messages import ChatMessage, usage_token_is_estimated
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


def _distinct_run_models(group: list[ChatMessage]) -> set[str]:
    """Return the distinct bare models that produced output in a run group."""
    return {
        _provider_model_key(message.model)
        for message in group
        if message.role == "assistant" and message.model
    }


def _group_is_open(group: list[ChatMessage]) -> bool:
    """Best-effort: a trailing group with conversational activity is unterminated."""
    return any(message.role in ("user", "assistant") for message in group)


def _is_visible_assistant_message(message: ChatMessage) -> bool:
    """Return whether an Assistant record contains user-visible Agent text."""
    return (
        message.role == "assistant"
        and isinstance(message.content, str)
        and bool(message.content.strip())
    )


def _is_visible_chat_message(message: ChatMessage) -> bool:
    """Count visible User messages and Assistant text, not internal Model steps."""
    return message.role == "user" or _is_visible_assistant_message(message)


@dataclass(frozen=True)
class _UsageFacts:
    """Normalized view of one assistant turn's ``usage`` payload."""

    input_tokens: int
    output_tokens: int
    reasoning: int
    input_estimated: bool
    output_estimated: bool
    estimated: bool
    cache_read: int
    cache_write: int
    # Field *presence*, not value: distinguishes "provider reported zero cache"
    # from "provider does not report caching at all".
    has_cache_data: bool
    # Like cache data, field presence distinguishes a reported zero from a
    # Provider that supplied no reasoning-token breakdown.
    has_reasoning_data: bool


def _read_usage(usage: JsonObject | None) -> _UsageFacts:
    """Normalize an assistant turn's usage payload.

    Canonical ``input_tokens`` already includes cached tokens, so cache figures
    are surfaced only as separate informational totals — never added on top.
    """
    if not isinstance(usage, dict):
        return _UsageFacts(0, 0, 0, False, False, False, 0, 0, False, False)
    input_estimated = usage_token_is_estimated(usage, "input_tokens")
    output_estimated = usage_token_is_estimated(usage, "output_tokens")
    return _UsageFacts(
        input_tokens=_non_negative_int(usage.get("input_tokens")),
        output_tokens=_non_negative_int(usage.get("output_tokens")),
        reasoning=_non_negative_int(usage.get("reasoning_tokens")),
        input_estimated=input_estimated,
        output_estimated=output_estimated,
        estimated=input_estimated or output_estimated,
        cache_read=_non_negative_int(usage.get("cache_read_tokens")),
        cache_write=_non_negative_int(usage.get("cache_write_tokens")),
        has_cache_data="cache_read_tokens" in usage or "cache_write_tokens" in usage,
        has_reasoning_data=(
            "reasoning_tokens" in usage
            and _optional_non_negative_int(usage.get("reasoning_tokens")) is not None
        ),
    )


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


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _optional_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _date_key(timestamp: str) -> str | None:
    parsed = parse_timestamp(timestamp)
    return parsed.date().isoformat() if parsed is not None else None


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
    count = len(sorted_values)
    rank = math.ceil((percentile / 100) * count)
    index = min(max(rank - 1, 0), count - 1)
    return float(sorted_values[index])


def _ratio(part: int, total: int) -> float:
    return part / total if total else 0.0


def _count_entries(counter: Counter[str]) -> list[CountEntry]:
    return [
        CountEntry(key=key, count=count)
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]
