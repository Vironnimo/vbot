"""Token estimation utilities.

Provides heuristic-based token counting for cases where a provider does
not report actual usage.  The estimate is deliberately conservative —
it uses a simple characters-per-token ratio and signals to consumers
that the number is approximate, not exact.

Usage::

    count, is_estimate = estimate_tokens("Hello, world!")
"""

import json
import math
from collections.abc import Mapping
from typing import Any

CHARS_PER_TOKEN = 4
MESSAGE_TOKEN_ESTIMATE_FIELDS = (
    "role",
    "content",
    "reasoning",
    "reasoning_meta",
    "tool_calls",
    "tool_call_id",
    "name",
    "error_kind",
)


def estimate_tokens(text: str) -> tuple[int, bool]:
    """Estimate the number of tokens in *text* using a character heuristic.

    Divides the character count by ``CHARS_PER_TOKEN`` (4 chars/token) and
    rounds up so that any remainder counts as a full token.

    Args:
        text: The string to estimate token count for.

    Returns:
        A ``(estimated_count, True)`` tuple where the boolean always
        signals that the count is an estimate, not a precise measurement.
    """
    if not text:
        return 0, True
    return math.ceil(len(text) / CHARS_PER_TOKEN), True


def estimate_message_tokens(message: Mapping[str, Any]) -> tuple[int, bool]:
    """Estimate tokens for provider-relevant message fields.

    Storage-only metadata such as message ids, timestamps, usage, and timing is
    intentionally ignored. Structured content, tool calls, and reasoning fields
    are serialized as compact JSON so they are counted by their payload size
    instead of by Python's object representation.
    """
    chunks: list[str] = []
    for field_name in MESSAGE_TOKEN_ESTIMATE_FIELDS:
        if field_name not in message:
            continue
        rendered = _render_token_estimate_value(message[field_name])
        if rendered:
            chunks.append(rendered)
    return estimate_tokens("\n".join(chunks))


def _render_token_estimate_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return str(value)
