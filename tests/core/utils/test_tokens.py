"""Tests for model-neutral token estimation utilities."""

import math
from typing import Any

import pytest

from core.utils import tokens as token_utils
from core.utils.tokens import (
    FALLBACK_CHARS_PER_TOKEN,
    NATIVE_MEDIA_TOKEN_RESERVE,
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
    yield
    token_utils._load_estimation_encoding.cache_clear()


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


def test_estimate_structured_tokens_counts_compact_json_size():
    """A structured value sends its compact serialization to the tokenizer."""
    # Arrange
    value = [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}]
    compact_length = len('[{"encrypted_content":"opaque","id":"rs_1","type":"reasoning"}]')

    # Act
    count, is_estimate = estimate_structured_tokens(value)

    # Assert
    assert count == -(-compact_length // 4)
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
