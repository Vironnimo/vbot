"""Tests for shared, explicitly approximate token estimation."""

import base64
import io
import math
from typing import Any

import pytest
from PIL import Image

from core.utils import tokens as token_utils
from core.utils.tokens import (
    FALLBACK_CHARS_PER_TOKEN,
    NATIVE_MEDIA_TOKEN_RESERVE,
    OPAQUE_REASONING_BLOB_TOKEN_RESERVE,
    TOKEN_ESTIMATE_ENCODING,
    estimate_json_tokens,
    estimate_message_tokens,
    estimate_request_input_tokens,
    estimate_structured_tokens,
    estimate_tokens,
)


class _DeterministicEncoding:
    """Small test double whose UTF-8 chunks expose multilingual differences."""

    @staticmethod
    def encode_ordinary(text: str) -> list[int]:
        token_count = math.ceil(len(text.encode("utf-8")) / 4)
        return list(range(token_count))


@pytest.fixture(autouse=True)
def _stub_tiktoken_encoding(monkeypatch: pytest.MonkeyPatch):
    encoding = _DeterministicEncoding()
    monkeypatch.setattr(token_utils.tiktoken, "get_encoding", lambda _name: encoding)
    token_utils._load_estimation_encoding.cache_clear()
    token_utils._COUNT_CACHE.clear()
    token_utils._IMAGE_CACHE.clear()
    yield
    token_utils._load_estimation_encoding.cache_clear()
    token_utils._COUNT_CACHE.clear()
    token_utils._IMAGE_CACHE.clear()


# ----- Empty input -----


def test_estimate_tokens_returns_zero_for_empty_string():
    """An empty string produces a token estimate of 0."""
    # Arrange
    text = ""

    # Act
    count, is_estimate = estimate_tokens(text)

    # Assert
    assert count == 0
    assert is_estimate is True


# ----- Simple ASCII text -----


def test_estimate_tokens_simple_ascii_text():
    """Plain ASCII text is delegated to the shared estimation encoding."""
    # Arrange
    text = "Hello, world!"  # 13 characters

    # Act
    count, is_estimate = estimate_tokens(text)

    # Assert
    assert count == 4  # ceil(13 / 4) = 4
    assert is_estimate is True


def test_estimate_tokens_always_returns_estimate_flag():
    """The boolean return value is always True, signalling an estimate."""
    # Arrange
    text = "abc"

    # Act
    _, is_estimate = estimate_tokens(text)

    # Assert
    assert is_estimate is True


# ----- Tokenizer delegation -----


def test_estimate_tokens_rounds_up_on_remainder():
    """The deterministic encoding controls the returned token count."""
    # Arrange
    text = "a" * 5  # 5 chars → ceil(5/4) = 2 tokens

    # Act
    count, _ = estimate_tokens(text)

    # Assert
    assert count == 2


def test_estimate_tokens_exact_division():
    """The tokenizer result is returned without an additional adjustment."""
    # Arrange
    text = "a" * 8  # 8 chars → 8/4 = 2 tokens

    # Act
    count, _ = estimate_tokens(text)

    # Assert
    assert count == 2


def test_estimate_tokens_one_char_rounds_up():
    """A non-empty tokenizer result remains non-zero."""
    # Arrange
    text = "x"  # 1 char → ceil(1/4) = 1 token

    # Act
    count, _ = estimate_tokens(text)

    # Assert
    assert count == 1


# ----- Unicode text (CJK characters) -----


def test_estimate_tokens_cjk_characters():
    """Multibyte CJK text no longer follows Python character count divided by four."""
    # Arrange
    text = "你好世界"

    # Act
    count, is_estimate = estimate_tokens(text)

    # Assert
    assert count == 3
    assert is_estimate is True


def test_estimate_tokens_mixed_unicode_and_ascii():
    """Mixed Unicode and ASCII text is delegated unchanged to the tokenizer."""
    # Arrange
    text = "Hello世界!"

    # Act
    count, _ = estimate_tokens(text)

    # Assert
    assert count == 3


def test_estimate_tokens_emoji():
    """Emoji no longer collapse to one token solely because Python sees two characters."""
    # Arrange
    text = "🎉🎊"

    # Act
    count, _ = estimate_tokens(text)

    # Assert
    assert count == 2


def test_estimation_encoding_is_fixed_and_cached(monkeypatch: pytest.MonkeyPatch):
    """Every estimate shares one cached o200k_base encoding instance."""
    calls: list[str] = []
    encoding = _DeterministicEncoding()

    def load_encoding(name: str) -> _DeterministicEncoding:
        calls.append(name)
        return encoding

    monkeypatch.setattr(token_utils.tiktoken, "get_encoding", load_encoding)
    token_utils._load_estimation_encoding.cache_clear()

    first, _ = estimate_tokens("first")
    second, _ = estimate_tokens("second")

    assert first > 0
    assert second > 0
    assert calls == [TOKEN_ESTIMATE_ENCODING]


def test_estimate_tokens_uses_character_fallback_when_encoding_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """A tokenizer-data failure retains the previous fail-soft estimate."""

    def fail_to_load(_name: str) -> Any:
        raise OSError("offline")

    monkeypatch.setattr(token_utils.tiktoken, "get_encoding", fail_to_load)
    token_utils._load_estimation_encoding.cache_clear()

    count, is_estimate = estimate_tokens("x" * 5)

    assert count == math.ceil(5 / FALLBACK_CHARS_PER_TOKEN)
    assert is_estimate is True
    assert "Token estimation encoding unavailable" in caplog.text


def test_estimate_message_tokens_counts_structured_tool_call_payloads():
    """Structured tool calls are counted by payload size, not by content=None."""
    # Arrange
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_large",
                "name": "write_file",
                "arguments": {"payload": "x" * 8_000},
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "timestamp": "2026-01-01T00:00:00+00:00",
    }

    # Act
    count, is_estimate = estimate_message_tokens(message)

    # Assert
    assert count > 2_000
    assert is_estimate is True


def test_estimate_json_tokens_counts_compact_json_size():
    """A JSON-serializable value sends its compact serialization to the tokenizer."""
    # Arrange
    tool_definitions = [
        {"name": "read", "description": "Read a file", "parameters": {"type": "object"}}
    ]
    compact_length = len(
        '[{"description":"Read a file","name":"read","parameters":{"type":"object"}}]'
    )

    # Act
    count, is_estimate = estimate_json_tokens(tool_definitions)

    # Assert
    assert count == -(-compact_length // 4)
    assert is_estimate is True


def test_estimate_json_tokens_plain_string_counts_verbatim():
    """A bare string is counted as-is, without JSON quoting."""
    # Act
    count, _ = estimate_json_tokens("abcd")

    # Assert
    assert count == 1


def test_estimate_structured_tokens_counts_items_with_array_framing():
    """Separate item counts allow reuse as the request grows."""
    # Arrange
    value = [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}]
    compact_length = len('{"encrypted_content":"opaque","id":"rs_1","type":"reasoning"}')

    # Act
    count, is_estimate = estimate_structured_tokens(value)

    # Assert
    assert count == -(-compact_length // 4) + 2
    assert is_estimate is True


def test_estimate_structured_tokens_reserves_native_media_without_counting_base64():
    """Encoded media uses the fixed semantic reserve, not transport-byte size."""
    # Arrange
    value = [
        {
            "type": "input_image",
            "image_url": f"data:image/png;base64,{'A' * 100_000}",
        }
    ]

    # Act
    count, _ = estimate_structured_tokens(value)

    # Assert
    assert count >= NATIVE_MEDIA_TOKEN_RESERVE
    assert count < NATIVE_MEDIA_TOKEN_RESERVE + 100


def test_estimate_message_tokens_ignores_storage_metadata():
    """Storage fields should not affect provider-message estimates."""
    # Arrange
    base_message = {"role": "user", "content": "hello"}
    with_storage_metadata = {
        **base_message,
        "id": "message-id-that-should-not-count",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "usage": {"input_tokens": 9_999},
        "timing": {"duration_ms": 123},
    }

    # Act
    base_count, _ = estimate_message_tokens(base_message)
    metadata_count, _ = estimate_message_tokens(with_storage_metadata)

    # Assert
    assert metadata_count == base_count


def test_estimate_request_input_tokens_includes_messages_and_tools():
    """The request estimate covers both conversation and Tool definitions."""
    messages = [{"role": "user", "content": "hello"}]
    tools = [
        {
            "name": "read",
            "description": "Read a file",
            "parameters": {"type": "object"},
        }
    ]
    message_tokens, _ = estimate_message_tokens(messages[0])
    tool_tokens, _ = estimate_json_tokens(tools)

    request_tokens, is_estimate = estimate_request_input_tokens(messages, tools)

    assert request_tokens == message_tokens + tool_tokens
    assert is_estimate is True


def test_estimate_request_input_tokens_reserves_native_media_without_counting_base64():
    """Large encoded media uses fixed semantic reserves, not transport-byte size."""
    encoded = "A" * 100_000
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                },
                {
                    "type": "media",
                    "media_type": "image/png",
                    "base64": encoded,
                },
                {
                    "type": "document",
                    "media_type": "application/pdf",
                    "filename": "report.pdf",
                    "base64": encoded,
                },
            ],
        },
        {
            "role": "tool",
            "content": '{"ok":true}',
            "tool_call_id": "call_image",
            "tool_result_content": [
                {
                    "type": "media",
                    "media_type": "image/png",
                    "base64": encoded,
                }
            ],
        },
    ]

    request_tokens, _ = estimate_request_input_tokens(messages)

    assert request_tokens >= 4 * NATIVE_MEDIA_TOKEN_RESERVE
    assert request_tokens < (4 * NATIVE_MEDIA_TOKEN_RESERVE) + 100


# ----- Opaque reasoning blobs -----


def _reasoning_details_message(details: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "reasoning_meta": {"reasoning_details": details},
    }


def test_estimate_message_tokens_reserves_opaque_reasoning_blobs_without_counting_encoded_size():
    """An oversized non-text blob counts as one fixed reservation, not as prose."""

    # Arrange — a 5000-character encrypted blob would serialize to >1250
    # prose tokens; providers bill it by decoded content instead.
    message = _reasoning_details_message(
        [
            {
                "type": "reasoning.encrypted",
                "format": "unknown",
                "index": 0,
                "encrypted_content": "S" * 5_000,
                "text": "ok",
            }
        ]
    )

    # Act
    count, is_estimate = estimate_message_tokens(message)

    # Assert
    assert count >= OPAQUE_REASONING_BLOB_TOKEN_RESERVE
    assert count < OPAQUE_REASONING_BLOB_TOKEN_RESERVE + 100
    assert is_estimate is True


def test_estimate_message_tokens_deduplicates_responses_reasoning_meta_carriers():
    """Derived Responses metadata views do not multiply one continuity item."""

    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "S" * 5_000,
    }
    complete_meta = {
        "response_output": [reasoning_item],
        "reasoning_items": [reasoning_item],
        "encrypted_content": [reasoning_item["encrypted_content"]],
    }
    response_output_only = {"response_output": [reasoning_item]}

    complete_count, _ = estimate_message_tokens(
        {"role": "assistant", "content": None, "reasoning_meta": complete_meta}
    )
    response_output_count, _ = estimate_message_tokens(
        {"role": "assistant", "content": None, "reasoning_meta": response_output_only}
    )

    assert complete_count == response_output_count


def test_estimate_message_tokens_reserves_opaque_reasoning_blob_lists():
    """A scalar-list continuity carrier receives one reserve per opaque blob."""

    count, _ = estimate_message_tokens(
        {
            "role": "assistant",
            "content": None,
            "reasoning_meta": {"encrypted_content": ["S" * 5_000]},
        }
    )

    assert count >= OPAQUE_REASONING_BLOB_TOKEN_RESERVE
    assert count < OPAQUE_REASONING_BLOB_TOKEN_RESERVE + 100


def test_estimate_message_tokens_counts_visible_reasoning_text_as_prose():
    """Visible reasoning text inside details keeps counting at text size."""

    # Arrange
    message = _reasoning_details_message(
        [{"type": "reasoning.text", "format": "unknown", "index": 0, "text": "a" * 4_000}]
    )

    # Act
    count, _ = estimate_message_tokens(message)

    # Assert
    assert count >= 1_000


def test_estimate_message_tokens_counts_short_reasoning_identifiers_verbatim():
    """Compact identifier strings stay below the blob threshold and add no reserve."""

    # Arrange
    message = _reasoning_details_message(
        [
            {
                "type": "reasoning.text",
                "format": "unknown",
                "id": "rs_1",
                "index": 0,
                "text": "ok",
            }
        ]
    )

    # Act
    count, _ = estimate_message_tokens(message)

    # Assert — well below one blob reservation, so nothing was treated as opaque.
    assert 0 < count < OPAQUE_REASONING_BLOB_TOKEN_RESERVE


def test_estimate_structured_tokens_reserves_opaque_reasoning_blobs():
    """Structured Responses-style payloads reserve oversized continuity blobs."""

    # Arrange
    value = [{"type": "reasoning", "id": "rs_1", "encrypted_content": "X" * 5_000}]

    # Act
    count, _ = estimate_structured_tokens(value)

    # Assert
    assert count >= OPAQUE_REASONING_BLOB_TOKEN_RESERVE
    assert count < OPAQUE_REASONING_BLOB_TOKEN_RESERVE + 100


def test_estimate_message_tokens_compacts_legacy_delta_fragments():
    """Sessions persisted before fragment merging are budgeted by content size."""

    # Arrange — one ~4-character fragment per streamed delta, as persisted by
    # the pre-merge accumulator; 1000 fragments serialize to ~80 KB of framing.
    fragments = [
        {"type": "reasoning.text", "format": "unknown", "index": 0, "text": "abc "}
        for _ in range(1_000)
    ]
    message = _reasoning_details_message(fragments)

    # Act
    count, _ = estimate_message_tokens(message)

    # Assert — merged into one item whose text is ~4000 chars (~1000 tokens).
    assert count >= 900
    assert count < 2_000


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("gpt-4", "cl100k_base"),
        ("openai/gpt-4-0613", "cl100k_base"),
        ("gpt-3.5-turbo", "cl100k_base"),
        ("gpt-4o", "o200k_base"),
        ("openai/gpt-4.1", "o200k_base"),
        ("gpt-5.6-sol", "o200k_base"),
        ("anthropic/claude-opus-4-7", "o200k_base"),
        ("opencode-go/deepseek-v4.1-flash", "o200k_base"),
        ("my-custom-model", "o200k_base"),
        (None, "o200k_base"),
    ],
)
def test_estimation_encoding_selection(model_id, expected):
    assert token_utils._estimation_encoding_name(model_id) == expected


def test_encodings_shared_across_models_and_count_cache_is_encoding_specific(monkeypatch):
    loaded = []
    encoded = []

    class Encoding:
        def __init__(self, name):
            self.name = name

        def encode_ordinary(self, text):
            encoded.append((self.name, text))
            return [0] * (3 if self.name == "cl100k_base" else 7)

    def load(name):
        loaded.append(name)
        return Encoding(name)

    monkeypatch.setattr(token_utils.tiktoken, "get_encoding", load)
    for model, expected in [("gpt-4", 3), ("gpt-4o", 7), ("gpt-3.5-turbo", 3), ("gpt-4.1", 7)]:
        assert estimate_tokens("same text", model_id=model) == (expected, True)
    assert loaded == ["cl100k_base", "o200k_base"]
    assert len(encoded) == 2


def test_growing_wire_reuses_unchanged_message_counts(monkeypatch):
    encoded = []

    class Encoding:
        def encode_ordinary(self, text):
            encoded.append(text)
            return [0] * len(text)

    monkeypatch.setattr(token_utils.tiktoken, "get_encoding", lambda _: Encoding())
    first = {"role": "user", "content": "unchanged"}
    estimate_structured_tokens([first], model_id="gpt-4o")
    estimate_structured_tokens([first, {"role": "assistant", "content": "new"}], model_id="gpt-4o")
    assert len(encoded) == 2
    first["content"] = "edited"
    estimate_structured_tokens([first], model_id="gpt-4o")
    assert len(encoded) == 3


def test_count_cache_is_bounded_and_does_not_retain_text(monkeypatch):
    monkeypatch.setattr(token_utils, "_COUNT_CACHE_SIZE", 3)
    for index in range(10):
        estimate_tokens(f"private message {index}")
    assert len(token_utils._COUNT_CACHE) == 3
    assert all(
        len(digest) == 32 and isinstance(count, int)
        for (_, digest), count in token_utils._COUNT_CACHE.items()
    )


def _image_payload(width=1024, height=1024, image_format="PNG"):
    output = io.BytesIO()
    with Image.new("RGB", (width, height)) as image:
        image.save(output, format=image_format)
    return base64.b64encode(output.getvalue()).decode("ascii")


@pytest.mark.parametrize(
    ("model", "width", "height", "detail", "expected"),
    [
        ("gpt-4o", 1024, 1024, "low", 85),
        ("gpt-4o", 1024, 1024, "high", 765),
        ("openai/gpt-4o-2024-08-06", 2048, 4096, "auto", 1105),
        ("gpt-4o-mini", 512, 512, "low", 2833),
        ("gpt-4o-mini", 512, 512, "high", 8500),
        ("gpt-5.1", 512, 512, "auto", 210),
        ("o3", 512, 512, "high", 225),
        ("gpt-6-astra", 1024, 1024, "high", 1229),
        ("gpt-6-astra", 2048, 2048, "high", 3000),
        ("gpt-6-astra", 4096, 512, "high", 2458),
        ("gpt-5.6-sol", 2048, 2048, "auto", 4916),
        ("gpt-5.6-sol", 2048, 2048, "low", 308),
        ("gpt-5.4", 2048, 2048, "auto", 3000),
        ("gpt-5.4", 2048, 2048, "low", 4916),
        ("gpt-4.1-mini", 1024, 1024, "auto", 1659),
        ("claude-sonnet-4-6", 200, 200, "auto", 64),
        ("claude-sonnet-4-6", 1000, 1000, "auto", 1296),
        ("claude-sonnet-4-6", 1920, 1080, "auto", 1560),
        ("claude-opus-4-7", 1920, 1080, "auto", 2691),
        ("anthropic/claude-opus-4-7-20260801", 3840, 2160, "auto", 4784),
        ("claude-3-5-sonnet-latest", 2000, 1500, "auto", 1564),
        # Golden results from DeepSeek's official V4.1 browser calculator.
        ("deepseek-flash", 200, 200, "auto", 184),
        ("opencode-go/deepseek-v4.1-flash", 1024, 1024, "auto", 652),
        ("deepseek-v4.1-flash", 1920, 1080, "auto", 968),
        ("deepseek-v4.1-flash", 3840, 2160, "auto", 968),
        ("deepseek-v4.1-flash", 100, 5000, "auto", 482),
        ("deepseek-v4.1-flash", 5000, 100, "auto", 365),
        ("deepseek-v4.1-flash", 1024, 1024, "low", 184),
    ],
)
def test_documented_image_token_examples(model, width, height, detail, expected):
    assert (
        token_utils._image_tokens(_image_payload(width, height), model_id=model, detail=detail)
        == expected
    )


@pytest.mark.parametrize("shape", ["chat", "responses", "messages", "canonical"])
def test_image_wrappers_preserve_size_detail_and_count_once(shape):
    payload = _image_payload(512, 512)
    url = f"data:image/png;base64,{payload}"
    block = {
        "chat": {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
        "responses": {"type": "input_image", "image_url": url, "detail": "high"},
        "messages": {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": payload},
        },
        "canonical": {"type": "media", "media_type": "image/png", "base64": payload},
    }[shape]
    import copy

    original = copy.deepcopy(block)
    normalized, image_tokens = token_utils._normalize_native_media(block, model_id="gpt-4o")
    assert image_tokens == 255
    assert block == original
    assert payload not in str(normalized)


def test_nested_chat_detail_overrides_auto():
    block = {
        "type": "image_url",
        "image_url": {"url": "https://example.test/image.png", "detail": "low"},
    }
    assert token_utils._normalize_native_media(block, model_id="gpt-4o")[1] == 85


@pytest.mark.parametrize(
    "payload", ["not-base64!", "https://example.test/image.png", "data:image/png,raw", "file_123"]
)
def test_unknown_dimensions_use_reserve(payload):
    assert (
        token_utils._image_tokens(payload, model_id="gpt-4o", detail="high")
        == NATIVE_MEDIA_TOKEN_RESERVE
    )


def test_unknown_model_and_low_detail_do_not_open_images(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Image inspection is unnecessary")

    monkeypatch.setattr(token_utils.Image, "open", unexpected)
    assert (
        token_utils._image_tokens("unused", model_id="unknown", detail="auto")
        == NATIVE_MEDIA_TOKEN_RESERVE
    )
    assert token_utils._image_tokens("unused", model_id="gpt-4o", detail="low") == 85


def test_image_header_cache_is_shared_across_models_and_bounded(monkeypatch):
    payload = _image_payload(256, 128, "JPEG")
    original_open = token_utils.Image.open
    calls = []

    def inspect_header(stream):
        calls.append(len(stream.getvalue()))
        return original_open(stream)

    monkeypatch.setattr(token_utils.Image, "open", inspect_header)
    for model in ["gpt-4o", "claude-opus-4-7", "deepseek-flash"]:
        token_utils._image_tokens(payload, model_id=model, detail="auto")
    assert len(calls) == 1
    assert calls[0] <= token_utils._IMAGE_HEADER_BYTES
    monkeypatch.setattr(token_utils, "_IMAGE_CACHE_SIZE", 2)
    for index in range(5):
        token_utils._image_dimensions(f"invalid-{index}")
    assert len(token_utils._IMAGE_CACHE) == 2
    assert all(isinstance(key, bytes) for key in token_utils._IMAGE_CACHE)


def test_image_dimensions_never_load_pixels(monkeypatch):
    payload = _image_payload(640, 480)

    def unexpected(*args, **kwargs):
        raise AssertionError("Pixel decoding must not run")

    monkeypatch.setattr(Image.Image, "load", unexpected)
    assert token_utils._image_dimensions(payload) == (640, 480)


def test_generic_request_estimate_uses_model_for_image_and_tools():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "media", "media_type": "image/png", "base64": _image_payload(512, 512)}
            ],
        }
    ]
    tools = [{"name": "inspect", "parameters": {"type": ["object", "null"]}}]
    known = estimate_request_input_tokens(messages, tools, model_id="gpt-4o")[0]
    unknown = estimate_request_input_tokens(messages, tools)[0]
    assert unknown - known == NATIVE_MEDIA_TOKEN_RESERVE - 255


def test_image_header_inspection_has_a_hard_byte_limit(monkeypatch):
    raw = base64.b64decode(_image_payload(640, 480)) + bytes(1024 * 1024)
    original_open = token_utils.Image.open
    inspected = []

    def inspect_header(stream):
        inspected.append(len(stream.getvalue()))
        return original_open(stream)

    monkeypatch.setattr(token_utils.Image, "open", inspect_header)
    assert token_utils._image_dimensions(base64.b64encode(raw).decode("ascii")) == (640, 480)
    assert inspected == [(token_utils._IMAGE_HEADER_BYTES // 3) * 3]


@pytest.mark.parametrize("failure", [Image.DecompressionBombWarning, Image.DecompressionBombError])
def test_oversized_image_header_falls_back(monkeypatch, failure):
    def reject_header(*args, **kwargs):
        raise failure("oversized image")

    monkeypatch.setattr(token_utils.Image, "open", reject_header)
    assert (
        token_utils._image_tokens("AAAA", model_id="gpt-4o", detail="high")
        == NATIVE_MEDIA_TOKEN_RESERVE
    )
