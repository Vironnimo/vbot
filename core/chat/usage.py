"""Session-level token usage aggregation shared by chat.history and Run events."""

from __future__ import annotations

from typing import Any

from core.chat.messages import ChatMessage

JsonObject = dict[str, Any]


def aggregate_session_usage(messages: list[ChatMessage]) -> JsonObject:
    """Sum provider-reported usage across a session's assistant turns.

    Returns the canonical ``session_usage`` payload carried by the
    ``chat.history`` response and the terminal Run events. Token totals cover
    only measured (provider-reported) turns; estimated turns are counted but
    their approximated token figures never merge into the measured totals.
    Canonical ``input_tokens`` already includes cached tokens, so
    ``cache_read_tokens``/``cache_write_tokens`` are informational subsets of
    the input total, never added on top.
    """
    measured_turns = 0
    estimated_turns = 0
    cache_turns = 0
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    for message in messages:
        if message.role != "assistant" or not isinstance(message.usage, dict):
            continue
        if message.usage.get("estimated") is True:
            estimated_turns += 1
            continue
        measured_turns += 1
        # Field *presence* distinguishes "provider reported zero cache" from
        # "provider does not report caching" — consumers need that to avoid
        # painting a non-caching provider as a 0% hit rate.
        if "cache_read_tokens" in message.usage or "cache_write_tokens" in message.usage:
            cache_turns += 1
        input_tokens += _non_negative_int(message.usage.get("input_tokens"))
        output_tokens += _non_negative_int(message.usage.get("output_tokens"))
        cache_read_tokens += _non_negative_int(message.usage.get("cache_read_tokens"))
        cache_write_tokens += _non_negative_int(message.usage.get("cache_write_tokens"))
    return {
        "measured_turns": measured_turns,
        "estimated_turns": estimated_turns,
        "cache_turns": cache_turns,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
    }


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value
