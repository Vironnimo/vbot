"""Token estimation utilities.

Estimates selected-wire requests with shared tiktoken encodings and local image
headers. Counts remain approximate: private tokenizers, wire framing, opaque
reasoning, and non-image media cannot be reproduced locally.

Usage::

    count, is_estimate = estimate_tokens("Hello, world!")
"""

import base64
import binascii
import hashlib
import io
import json
import logging
import math
import re
import warnings
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from functools import lru_cache
from threading import RLock
from typing import Any

import tiktoken
from PIL import Image

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

# Fallback for unknown image inputs and audio/documents. Known images use their
# Model profile and header dimensions instead of counting base64 as text.
# One payload must still leave usable output capacity under the conservative
# 8192-token unknown-Model context floor; the separate 25% request reserve then
# absorbs Provider-specific media accounting variance.
NATIVE_MEDIA_TOKEN_RESERVE = 4096

# Cache only digests and scalar results, never transcripts, base64 or pixels.
_COUNT_CACHE_SIZE = 4096
_IMAGE_CACHE_SIZE = 256
_IMAGE_HEADER_BYTES = 256 * 1024
_COUNT_CACHE: OrderedDict[tuple[str, bytes], int] = OrderedDict()
_IMAGE_CACHE: OrderedDict[bytes, tuple[int, int] | None] = OrderedDict()
_CACHE_LOCK = RLock()

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


def estimate_tokens(text: str, *, model_id: str | None = None) -> tuple[int, bool]:
    """Count text with an available Model encoding; always mark it estimated."""
    if not text:
        return 0, True
    encoding_name = _estimation_encoding_name(model_id)
    key = (encoding_name, _content_digest(text))
    with _CACHE_LOCK:
        if key in _COUNT_CACHE:
            _COUNT_CACHE.move_to_end(key)
            return _COUNT_CACHE[key], True
    encoding = _load_estimation_encoding(encoding_name)
    count = (
        len(encoding.encode_ordinary(text))
        if encoding is not None
        else math.ceil(len(text) / FALLBACK_CHARS_PER_TOKEN)
    )
    with _CACHE_LOCK:
        _COUNT_CACHE[key] = count
        _COUNT_CACHE.move_to_end(key)
        if len(_COUNT_CACHE) > _COUNT_CACHE_SIZE:
            _COUNT_CACHE.popitem(last=False)
    return count, True


def _content_digest(text: str) -> bytes:
    """Hash large media/text without another full-size UTF-8 allocation."""
    digest = hashlib.sha256()
    for start in range(0, len(text), 65536):
        digest.update(text[start : start + 65536].encode("utf-8", errors="surrogatepass"))
    return digest.digest()


def _model_name(model_id: str | None) -> str:
    # Gateways use vendor/model IDs; dated snapshots and route suffixes retain
    # their identity. Do not fuzzy-match arbitrary custom model names.
    return (model_id or "").lower().rsplit("/", 1)[-1].split(":", 1)[0]


@lru_cache(maxsize=256)
def _estimation_encoding_name(model_id: str | None) -> str:
    try:
        name = tiktoken.model.encoding_name_for_model(_model_name(model_id))
    except KeyError:
        return TOKEN_ESTIMATE_ENCODING
    # This estimator supports modern chat encodings only. Unknown/private and
    # legacy vocabularies use the shared default instead of loading more tables.
    return name if name in {"o200k_base", "cl100k_base"} else TOKEN_ESTIMATE_ENCODING


@lru_cache(maxsize=2)
def _load_estimation_encoding(name: str = TOKEN_ESTIMATE_ENCODING) -> tiktoken.Encoding | None:
    """One instance per encoding and process, shared across Agents and Sessions.

    tiktoken's registry serializes first construction, including concurrent
    misses in this wrapper's LRU cache.
    """

    try:
        return tiktoken.get_encoding(name)
    except (OSError, ValueError) as exc:
        _LOGGER.warning(
            "Token estimation encoding unavailable; using character fallback (encoding=%s): %s",
            name,
            exc,
        )
        return None


def estimate_message_tokens(
    message: Mapping[str, Any], *, model_id: str | None = None
) -> tuple[int, bool]:
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
    estimated_tokens, _ = estimate_tokens("\n".join(chunks), model_id=model_id)
    return estimated_tokens + blob_count * OPAQUE_REASONING_BLOB_TOKEN_RESERVE, True


def estimate_json_tokens(value: Any, *, model_id: str | None = None) -> tuple[int, bool]:
    """Estimate tokens for a JSON-serializable value via its compact JSON size.

    Used for payloads that reach the provider as structured data rather than
    prose — e.g. the tool-definition array sent alongside the system prompt.
    Providers render such payloads into model context in provider-specific
    formats, so the compact JSON size is the provider-neutral approximation.
    """
    return estimate_tokens(_render_token_estimate_value(value), model_id=model_id)


def estimate_structured_tokens(value: Any, *, model_id: str | None = None) -> tuple[int, bool]:
    """Estimate tokens for a structured value, normalizing native media.

    Images use known Model rules and header dimensions; unavailable dimensions,
    unknown Models and other media use a fixed reserve. Top-level arrays count
    items separately so growing requests reuse cached counts for unchanged
    messages/Tools. JSON framing remains an approximation of Provider framing.
    """

    if isinstance(value, (list, tuple)):
        return (
            sum(estimate_structured_tokens(item, model_id=model_id)[0] for item in value)
            + (2 + max(0, len(value) - 1)),
            True,
        )
    normalized, media_tokens = _normalize_native_media(value, model_id=model_id)
    normalized, blob_count = _normalize_opaque_reasoning_blobs(normalized)
    estimated, _ = estimate_json_tokens(normalized, model_id=model_id)
    return (
        estimated + media_tokens + blob_count * OPAQUE_REASONING_BLOB_TOKEN_RESERVE,
        True,
    )


def estimate_request_input_tokens(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
    *,
    model_id: str | None = None,
) -> tuple[int, bool]:
    """Estimate one Provider request's input footprint, including Tools.

    This is the request-limit estimator, not the persisted Usage estimator. It
    counts the same Provider-visible message fields as
    :func:`estimate_message_tokens`, adds the structured Tool-definition array,
    and normalizes native base64/data-URL media so transport encoding does not
    masquerade as prose tokens. Images use Model-specific estimates where known;
    other media and unsupported image inputs retain the fixed reserve.
    """

    total_tokens = 0
    media_tokens = 0
    for message in messages:
        normalized, message_media_tokens = _normalize_native_media(message, model_id=model_id)
        estimated_tokens, _ = estimate_message_tokens(normalized, model_id=model_id)
        total_tokens += estimated_tokens
        media_tokens += message_media_tokens
    if tools:
        tool_tokens, _ = estimate_json_tokens(tools, model_id=model_id)
        total_tokens += tool_tokens
    total_tokens += media_tokens
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


def _normalize_native_media(
    value: Any, *, model_id: str | None = None, image: bool = False, detail: str = "auto"
) -> tuple[Any, int]:
    """Return a copy with native payloads replaced, plus their semantic tokens.

    Carry image/detail context through Chat, Responses, Messages and canonical
    Content Block wrappers. External images are reserved without fetching them.
    """

    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        media_tokens = 0
        mapping_type = value.get("type")
        image = (
            image
            or mapping_type in ("image", "image_url", "input_image")
            or str(value.get("media_type", "")).startswith("image/")
        )
        if isinstance(value.get("detail"), str):
            detail = value["detail"]
        for key, item in value.items():
            if _is_native_media_payload(value, key, item, mapping_type) or (
                image and key in {"url", "image_url", "file_id"} and isinstance(item, str)
            ):
                normalized[str(key)] = "<native-media>"
                is_image = image or (isinstance(item, str) and item.startswith("data:image/"))
                media_tokens += (
                    _image_tokens(item, model_id=model_id, detail=detail)
                    if is_image
                    else NATIVE_MEDIA_TOKEN_RESERVE
                )
                continue
            normalized_item, nested_media_tokens = _normalize_native_media(
                item, model_id=model_id, image=image, detail=detail
            )
            normalized[str(key)] = normalized_item
            media_tokens += nested_media_tokens
        return normalized, media_tokens
    if isinstance(value, (list, tuple)):
        normalized_items: list[Any] = []
        media_tokens = 0
        for item in value:
            normalized_item, nested_media_tokens = _normalize_native_media(
                item, model_id=model_id, image=image, detail=detail
            )
            normalized_items.append(normalized_item)
            media_tokens += nested_media_tokens
        return normalized_items, media_tokens
    return value, 0


def _image_dimensions(payload: str) -> tuple[int, int] | None:
    """Inspect at most 256 KiB of decoded header; never load pixels or URLs."""
    key = _content_digest(payload)
    with _CACHE_LOCK:
        if key in _IMAGE_CACHE:
            _IMAGE_CACHE.move_to_end(key)
            return _IMAGE_CACHE[key]
    dimensions = None
    start = 0
    if payload.startswith("data:"):
        marker = payload.find(";base64,", 0, 128)
        start = marker + len(";base64,") if marker >= 0 else len(payload)
    try:
        header = base64.b64decode(
            payload[start : start + (_IMAGE_HEADER_BYTES // 3) * 4], validate=True
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(header)) as source:
                if source.width > 0 and source.height > 0:
                    dimensions = source.size
    except (
        ValueError,
        OSError,
        SyntaxError,
        binascii.Error,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ):
        pass
    with _CACHE_LOCK:
        _IMAGE_CACHE[key] = dimensions
        _IMAGE_CACHE.move_to_end(key)
        if len(_IMAGE_CACHE) > _IMAGE_CACHE_SIZE:
            _IMAGE_CACHE.popitem(last=False)
    return dimensions


def _image_tokens(payload: str, *, model_id: str | None, detail: str) -> int:
    """Apply documented image rules, with a fixed reserve for unknown inputs.

    Sources (2026-09-11): developers.openai.com/api/docs/guides/images-vision,
    platform.claude.com/docs/en/build-with-claude/{vision,vision-coordinates},
    api-docs.deepseek.com/{guides/vision,quick_start/token_usage}.
    """
    model = _model_name(model_id)
    if detail not in {"low", "high", "original", "auto"}:
        return NATIVE_MEDIA_TOKEN_RESERVE
    tile = re.fullmatch(
        r"(gpt-4o-mini|gpt-4o|gpt-4\.1|gpt-5\.1|gpt-5|o1-pro|o1|o3)(?:-\d{4}-\d{2}-\d{2})?",
        model,
    )
    patch = re.fullmatch(
        r"(gpt-6-astra|gpt-5\.6-(?:sol|terra|luna)|gpt-5\.5|gpt-5\.4(?:-mini|-nano)?|gpt-5\.2|gpt-4\.1-mini)(?:-\d{4}-\d{2}-\d{2})?",
        model,
    )
    claude = re.fullmatch(
        r"claude-(?:(?:opus|sonnet|haiku)-(\d+)(?:[.-](\d+))?|(\d+)[.-](\d+)-(?:opus|sonnet|haiku))(?:-\d{8}|-latest)?",
        model,
    )
    deepseek = model in {"deepseek-flash", "deepseek-v4.1-flash", "deepseek-v4-flash-vision-exp"}
    if tile:
        family = tile[1]
        base, per_tile = (
            (2833, 5667)
            if family == "gpt-4o-mini"
            else (70, 140)
            if family.startswith("gpt-5")
            else (75, 150)
            if family.startswith("o")
            else (85, 170)
        )
        if detail == "low":
            return base
        if detail == "original":
            return NATIVE_MEDIA_TOKEN_RESERVE
    if not (tile or patch or claude or deepseek):
        return NATIVE_MEDIA_TOKEN_RESERVE
    dimensions = _image_dimensions(payload)
    if dimensions is None:
        return NATIVE_MEDIA_TOKEN_RESERVE
    width, height = dimensions
    if tile:
        scale = min(1, 2048 / max(width, height), 768 / min(width, height))
        width, height = max(1, math.floor(width * scale)), max(1, math.floor(height * scale))
        return base + per_tile * math.ceil(width / 512) * math.ceil(height / 512)
    if patch:
        family = patch[1]
        multiplier = 1.62 if family == "gpt-4.1-mini" else 1.2
        budget: int | None
        if family in {"gpt-5.2", "gpt-4.1-mini"}:
            if detail == "original":
                return NATIVE_MEDIA_TOKEN_RESERVE
            edge, budget = 2048, 6144
        else:
            modern = family in {"gpt-6-astra", "gpt-5.5"} or family.startswith("gpt-5.6-")
            if detail == "auto":
                detail = "original" if modern else "high"
            if detail == "low":
                edge, budget = (512, None) if modern else (2048, 6144)
            elif detail == "original":
                edge, budget = (65535, None) if family != "gpt-5.5" and modern else (6000, 10000)
            else:
                edge, budget = (65535 if family == "gpt-6-astra" else 2048), 2500
        return math.ceil(_patch_count(width, height, edge=edge, budget=budget) * multiplier)
    if claude:
        version = (int(claude[1] or claude[3]), int(claude[2] or claude[4] or 0))
        return _claude_image_tokens(width, height, high_resolution=version >= (4, 7))
    if detail == "low":
        scale = min(1, 512 / max(width, height))
        width, height = max(1, int(width * scale)), max(1, int(height * scale))
    return _deepseek_image_tokens(width, height)


def _patch_count(width: int, height: int, *, edge: int, budget: int | None) -> int:
    scale = min(1, edge / max(width, height))
    width, height = max(1, round(width * scale)), max(1, round(height * scale))
    if budget is not None and math.ceil(width / 32) * math.ceil(height / 32) > budget:
        scale = math.sqrt(32 * 32 * budget / (width * height))
        scaled_w, scaled_h = width * scale / 32, height * scale / 32
        # Extreme aspect ratios need at least one patch on the short edge.
        if min(scaled_w, scaled_h) < 1:
            return budget
        scale *= min(math.floor(scaled_w) / scaled_w, math.floor(scaled_h) / scaled_h)
        width, height = max(1, math.floor(width * scale)), max(1, math.floor(height * scale))
    return math.ceil(width / 32) * math.ceil(height / 32)


def _claude_image_tokens(width: int, height: int, *, high_resolution: bool) -> int:
    edge, budget = (2576, 4784) if high_resolution else (1568, 1568)
    width, height = max(width, height), min(width, height)

    def fits(w: int, h: int) -> bool:
        columns, rows = math.ceil(w / 28), math.ceil(h / 28)
        return columns * 28 <= edge and rows * 28 <= edge and columns * rows <= budget

    if not fits(width, height):
        aspect = width / height
        low, high = 1, width
        while low + 1 < high:
            middle = (low + high) // 2
            if fits(middle, max(round(middle / aspect), 1)):
                low = middle
            else:
                high = middle
        width, height = low, max(round(low / aspect), 1)
    return math.ceil(width / 28) * math.ceil(height / 28)


def _deepseek_image_tokens(width: int, height: int) -> int:
    """V4.1/Flash calculator: 14px patches, 3x downsampling, row separators.

    Repeat preprocessing until dimensions stabilize, as in the official local
    calculator. An unexpected non-convergence keeps the documented 1024 ceiling.
    """
    previous = None
    for _ in range(10):
        if width * height < 544 * 544:
            scale = math.sqrt(544 * 544 / (width * height))
            width, height = max(1, int(width * scale)), max(1, int(height * scale))
        width, height = math.ceil(width / 14) * 14, math.ceil(height / 14) * 14
        rows, columns = math.ceil(height / 42), math.ceil(width / 42)
        count = rows * (columns + 1) + 2
        if count > 1024:
            aspect = height / width
            columns_f = math.sqrt(1022 / aspect + 0.25) - 0.5
            rows_f = columns_f * aspect
            if columns_f < 1:
                width, height = 42, 511 * 42
            elif rows_f < 1:
                width, height = 1021 * 42, 42
            else:
                scale = min(int(columns_f) * 42 / width, int(rows_f) * 42 / height)
                width, height = (
                    max(14, int(width * scale / 14) * 14),
                    max(14, int(height * scale / 14) * 14),
                )
            rows, columns = math.ceil(height / 42), math.ceil(width / 42)
            count = rows * (columns + 1) + 2
        current = (width, height, count)
        if current == previous:
            return min(1024, count)
        previous = current
    return 1024


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
