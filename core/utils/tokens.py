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
from collections.abc import Mapping, Sequence
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
    "tool_result_content",
)

# Native media reaches providers as large base64/data-URL strings, but Models
# account for decoded image/audio/document content rather than one text token per
# few encoded characters. Request budgeting therefore replaces each encoded
# payload with a conservative fixed reservation instead of letting transport
# bytes consume the whole estimated context window.
# One payload must still leave usable output capacity under the conservative
# 8192-token unknown-Model context floor; the separate 25% request reserve then
# absorbs Provider-specific media accounting variance.
NATIVE_MEDIA_TOKEN_RESERVE = 4096


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


def estimate_json_tokens(value: Any) -> tuple[int, bool]:
    """Estimate tokens for a JSON-serializable value via its compact JSON size.

    Used for payloads that reach the provider as structured data rather than
    prose — e.g. the tool-definition array sent alongside the system prompt.
    Providers render such payloads into model context in provider-specific
    formats, so the compact JSON size is the provider-neutral approximation.
    """
    return estimate_tokens(_render_token_estimate_value(value))


def estimate_request_input_tokens(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[int, bool]:
    """Estimate one Provider request's input footprint, including Tools.

    This is the request-limit estimator, not the persisted Usage estimator. It
    counts the same Provider-visible message fields as
    :func:`estimate_message_tokens`, adds the structured Tool-definition array,
    and normalizes native base64/data-URL media so transport encoding does not
    masquerade as prose tokens. Each removed media payload contributes the named
    :data:`NATIVE_MEDIA_TOKEN_RESERVE` instead.
    """

    total_tokens = 0
    media_payloads = 0
    for message in messages:
        normalized, message_media_payloads = _normalize_native_media(message)
        estimated_tokens, _ = estimate_message_tokens(normalized)
        total_tokens += estimated_tokens
        media_payloads += message_media_payloads
    if tools:
        tool_tokens, _ = estimate_json_tokens(tools)
        total_tokens += tool_tokens
    total_tokens += media_payloads * NATIVE_MEDIA_TOKEN_RESERVE
    return total_tokens, True


def _render_token_estimate_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _normalize_native_media(value: Any) -> tuple[Any, int]:
    """Return a token-estimation copy with encoded native media replaced."""

    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        media_payloads = 0
        mapping_type = value.get("type")
        for key, item in value.items():
            if _is_native_media_payload(value, key, item, mapping_type):
                normalized[str(key)] = "<native-media>"
                media_payloads += 1
                continue
            normalized_item, nested_media_payloads = _normalize_native_media(item)
            normalized[str(key)] = normalized_item
            media_payloads += nested_media_payloads
        return normalized, media_payloads
    if isinstance(value, list):
        normalized_items: list[Any] = []
        media_payloads = 0
        for item in value:
            normalized_item, nested_media_payloads = _normalize_native_media(item)
            normalized_items.append(normalized_item)
            media_payloads += nested_media_payloads
        return normalized_items, media_payloads
    if isinstance(value, tuple):
        normalized_items, media_payloads = _normalize_native_media(list(value))
        return normalized_items, media_payloads
    return value, 0


def _is_native_media_payload(
    container: Mapping[str, Any],
    key: Any,
    value: Any,
    container_type: Any,
) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value.startswith("data:") and ";base64," in value[:128]:
        return True
    if key == "base64" and isinstance(container.get("media_type"), str):
        return True
    if key == "data" and container_type == "base64":
        return True
    return key == "data" and isinstance(container.get("format"), str)
