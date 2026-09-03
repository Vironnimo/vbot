"""Token estimation utilities.

Provides model-neutral token counting for cases where a Provider does not
report actual Usage. The estimate uses one fixed ``o200k_base`` tokenizer and
signals to consumers that the number remains approximate rather than claiming
Provider- or Model-specific precision. A character heuristic remains available
only when the tokenizer data cannot be loaded.

Usage::

    count, is_estimate = estimate_tokens("Hello, world!")
"""

import json
import logging
import math
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

import tiktoken

_LOGGER = logging.getLogger("vbot.utils.tokens")

TOKEN_ESTIMATE_ENCODING = "o200k_base"
FALLBACK_CHARS_PER_TOKEN = 4
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

# Replayable reasoning metadata mixes compact identifiers with large opaque
# continuity blobs (signatures, encrypted thinking state). Providers bill such
# blobs by their decoded content rather than by encoded character count — live
# evidence 2026-08-25: a session whose replayed details serialized to ~3.4 MB of
# JSON reported only ~220k real prompt tokens. Request budgeting therefore
# replaces each oversized non-text blob string with a fixed reservation instead
# of letting transport encoding masquerade as prose tokens. Visible ``text``
# fields keep counting as prose.
OPAQUE_REASONING_BLOB_MIN_CHARS = 128
OPAQUE_REASONING_BLOB_TOKEN_RESERVE = 512
REASONING_TEXT_FIELD = "text"
# Gateways such as OpenRouter stream ``reasoning.text`` details as one tiny
# fragment per delta (observed 2026-08-25 on stealth/ox-alpha: ~4-character
# texts, no stable id, identical index). Merging is limited to delta-sized
# texts so a gateway repeating a complete snapshot per sibling Tool Call keeps
# its copies as separate items.
REASONING_TEXT_DELTA_MERGE_MAX_CHARS = 256
# Well-known opaque continuity keys outside ``reasoning_details`` containers —
# stateless Responses reasoning items carry ``encrypted_content`` directly, and
# Anthropic-family thinking blocks carry signatures.
OPAQUE_REASONING_BLOB_KEYS = frozenset({"encrypted_content", "signature", "redacted_thinking"})
REASONING_META_RESPONSE_OUTPUT_KEY = "response_output"
REASONING_META_REASONING_ITEMS_KEY = "reasoning_items"
REASONING_META_ENCRYPTED_CONTENT_KEY = "encrypted_content"


def estimate_tokens(text: str) -> tuple[int, bool]:
    """Estimate the number of tokens in *text* with one fixed tokenizer.

    ``o200k_base`` is deliberately used for every estimate: this path exists
    only when Provider Usage is unavailable, so model-neutral consistency and
    materially better multilingual/code estimates matter more than pretending
    to reproduce each Provider's private tokenizer. If the encoding cannot be
    loaded, the prior four-characters-per-token heuristic is retained as a
    fail-soft fallback.

    Args:
        text: The string to estimate token count for.

    Returns:
        A ``(estimated_count, True)`` tuple where the boolean always
        signals that the count is an estimate, not a precise measurement.
    """
    if not text:
        return 0, True
    encoding = _load_estimation_encoding()
    if encoding is not None:
        return len(encoding.encode_ordinary(text)), True
    return math.ceil(len(text) / FALLBACK_CHARS_PER_TOKEN), True


@lru_cache(maxsize=1)
def _load_estimation_encoding() -> tiktoken.Encoding | None:
    """Load and cache the single model-neutral estimation encoding."""

    try:
        return tiktoken.get_encoding(TOKEN_ESTIMATE_ENCODING)
    except (OSError, ValueError) as exc:
        _LOGGER.warning(
            "Token estimation encoding unavailable; using character fallback (encoding=%s): %s",
            TOKEN_ESTIMATE_ENCODING,
            exc,
        )
        return None


def estimate_message_tokens(message: Mapping[str, Any]) -> tuple[int, bool]:
    """Estimate tokens for provider-relevant message fields.

    Storage-only metadata such as message ids, timestamps, usage, and timing is
    intentionally ignored. Structured content, tool calls, and reasoning fields
    are serialized as compact JSON so they are counted by their payload size
    instead of by Python's object representation. Opaque reasoning blobs inside
    ``reasoning_details`` are replaced by fixed reservations (see
    :func:`_normalize_opaque_reasoning_blobs`).
    """
    chunks: list[str] = []
    blob_count = 0
    for field_name in MESSAGE_TOKEN_ESTIMATE_FIELDS:
        if field_name not in message:
            continue
        field_value = message[field_name]
        if field_name == "reasoning_meta":
            field_value = _deduplicate_reasoning_meta_carriers(field_value)
        normalized_value, field_blob_count = _normalize_opaque_reasoning_blobs(field_value)
        blob_count += field_blob_count
        rendered = _render_token_estimate_value(normalized_value)
        if rendered:
            chunks.append(rendered)
    estimated_tokens, _ = estimate_tokens("\n".join(chunks))
    return estimated_tokens + blob_count * OPAQUE_REASONING_BLOB_TOKEN_RESERVE, True


def estimate_json_tokens(value: Any) -> tuple[int, bool]:
    """Estimate tokens for a JSON-serializable value via its compact JSON size.

    Used for payloads that reach the provider as structured data rather than
    prose — e.g. the tool-definition array sent alongside the system prompt.
    Providers render such payloads into model context in provider-specific
    formats, so the compact JSON size is the provider-neutral approximation.
    """
    return estimate_tokens(_render_token_estimate_value(value))


def estimate_structured_tokens(value: Any) -> tuple[int, bool]:
    """Estimate tokens for a structured value, normalizing native media.

    Like :func:`estimate_json_tokens`, but replaces encoded base64/data-URL
    media payloads with the fixed :data:`NATIVE_MEDIA_TOKEN_RESERVE` first, so
    transport encoding does not masquerade as prose tokens. Used for provider
    payloads that reach the model as structured data rather than chat messages
    — e.g. stateless Responses input items, which carry provider-owned
    reasoning items with large encrypted continuity blobs. Opaque reasoning
    blobs get the same treatment (see :func:`_normalize_opaque_reasoning_blobs`).
    """

    normalized, media_payloads = _normalize_native_media(value)
    normalized, blob_count = _normalize_opaque_reasoning_blobs(normalized)
    estimated, _ = estimate_json_tokens(normalized)
    return (
        estimated
        + media_payloads * NATIVE_MEDIA_TOKEN_RESERVE
        + blob_count * OPAQUE_REASONING_BLOB_TOKEN_RESERVE,
        True,
    )


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


def _normalize_opaque_reasoning_blobs(value: Any) -> tuple[Any, int]:
    """Return a token-estimation copy with oversized opaque reasoning blobs replaced.

    Replayable reasoning metadata mixes compact identifiers with large opaque
    continuity blobs — signatures, encrypted thinking state. Inside
    ``reasoning_details`` items any oversized non-``text`` string counts as a
    blob; outside them only the well-known :data:`OPAQUE_REASONING_BLOB_KEYS`
    continuity keys do. Providers bill such blobs by their decoded content
    rather than by encoded character count, so budgeting replaces each one with
    a fixed reservation per blob. Returns the normalized copy and the number of
    replaced blobs.
    """

    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        blob_count = 0
        for key, item in value.items():
            if key == "reasoning_details" and isinstance(item, list):
                compacted_items, detail_blob_count = _compact_reasoning_details(item)
                normalized[str(key)] = compacted_items
                blob_count += detail_blob_count
            elif (
                key in OPAQUE_REASONING_BLOB_KEYS
                and isinstance(item, str)
                and len(item) >= OPAQUE_REASONING_BLOB_MIN_CHARS
            ):
                normalized[str(key)] = "<opaque-reasoning>"
                blob_count += 1
            elif key in OPAQUE_REASONING_BLOB_KEYS and isinstance(item, (list, tuple)):
                normalized_items: list[Any] = []
                for nested_item in item:
                    if (
                        isinstance(nested_item, str)
                        and len(nested_item) >= OPAQUE_REASONING_BLOB_MIN_CHARS
                    ):
                        normalized_items.append("<opaque-reasoning>")
                        blob_count += 1
                    else:
                        normalized_item, nested_blob_count = _normalize_opaque_reasoning_blobs(
                            nested_item
                        )
                        normalized_items.append(normalized_item)
                        blob_count += nested_blob_count
                normalized[str(key)] = normalized_items
            else:
                normalized_item, nested_blob_count = _normalize_opaque_reasoning_blobs(item)
                normalized[str(key)] = normalized_item
                blob_count += nested_blob_count
        return normalized, blob_count
    if isinstance(value, list):
        normalized_list: list[Any] = []
        blob_count = 0
        for item in value:
            normalized_item, nested_blob_count = _normalize_opaque_reasoning_blobs(item)
            normalized_list.append(normalized_item)
            blob_count += nested_blob_count
        return normalized_list, blob_count
    if isinstance(value, tuple):
        normalized_tuple, blob_count = _normalize_opaque_reasoning_blobs(list(value))
        return tuple(normalized_tuple), blob_count
    return value, 0


def _deduplicate_reasoning_meta_carriers(value: Any) -> Any:
    """Keep only the strongest copy of redundant Responses reasoning state.

    Responses normalization retains the complete ordered ``response_output``
    alongside two derived compatibility views: its reasoning-only item subset
    and the encrypted-content scalar list. No Provider sends all three views as
    separate Context, so the generic fallback estimator must not count them as
    independent prompt material.
    """

    if not isinstance(value, Mapping):
        return value
    normalized = dict(value)
    if isinstance(normalized.get(REASONING_META_RESPONSE_OUTPUT_KEY), list):
        normalized.pop(REASONING_META_REASONING_ITEMS_KEY, None)
        normalized.pop(REASONING_META_ENCRYPTED_CONTENT_KEY, None)
    elif isinstance(normalized.get(REASONING_META_REASONING_ITEMS_KEY), list):
        normalized.pop(REASONING_META_ENCRYPTED_CONTENT_KEY, None)
    return normalized


def _normalize_reasoning_detail(detail: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    """Replace oversized non-text strings inside one reasoning-details item."""

    normalized: dict[str, Any] = {}
    blob_count = 0
    for key, item in detail.items():
        if (
            key != REASONING_TEXT_FIELD
            and isinstance(item, str)
            and len(item) >= OPAQUE_REASONING_BLOB_MIN_CHARS
        ):
            normalized[str(key)] = "<opaque-reasoning>"
            blob_count += 1
        else:
            normalized[str(key)] = item
    return normalized, blob_count


def _compact_reasoning_details(details: list[Any]) -> tuple[list[Any], int]:
    """Normalize one ``reasoning_details`` list for estimation.

    Replaces oversized non-text blob strings with fixed reservations and merges
    consecutive id-less same-shape text fragments into single items — mirroring
    the adapter's stream accumulation, so sessions persisted before that fix
    (one item per streamed delta) are budgeted by their real content size.
    """

    compacted: list[Any] = []
    blob_count = 0
    for detail in details:
        if not isinstance(detail, Mapping):
            compacted.append(detail)
            continue
        normalized_detail, detail_blob_count = _normalize_reasoning_detail(detail)
        blob_count += detail_blob_count
        previous = compacted[-1] if compacted else None
        previous_text = previous.get("text") if isinstance(previous, Mapping) else None
        if (
            isinstance(previous, Mapping)
            and isinstance(previous_text, str)
            and continues_reasoning_text_block(previous, normalized_detail)
        ):
            compacted[len(compacted) - 1] = {
                **previous,
                "text": previous_text + normalized_detail["text"],
            }
            continue
        compacted.append(normalized_detail)
    return compacted, blob_count


def continues_reasoning_text_block(previous: Any, incoming: Any) -> bool:
    """Whether a reasoning-details fragment continues the previous block.

    Both must be id-less mappings sharing ``type``, ``format``, and ``index``
    with string ``text`` on each side, and the incoming text must be delta-sized
    so a provider repeating a complete snapshot per sibling Tool Call keeps its
    copies as separate items. Shared by the adapter's stream accumulator and the
    estimator's list compaction so both see the same logical blocks.
    """

    if not isinstance(previous, Mapping) or not isinstance(incoming, Mapping):
        return False
    incoming_text = incoming.get("text")
    if (
        not isinstance(incoming_text, str)
        or not 0 < len(incoming_text) <= REASONING_TEXT_DELTA_MERGE_MAX_CHARS
    ):
        return False
    if not isinstance(previous.get("text"), str):
        return False
    return (
        previous.get("id") is None
        and incoming.get("id") is None
        and previous.get("type") == incoming.get("type")
        and previous.get("format") == incoming.get("format")
        and previous.get("index") == incoming.get("index")
    )


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
