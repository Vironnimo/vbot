"""Anthropic Adapter responses: catalog entries, completed Messages, errors and retry."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.models.models import (
    REASONING_CONTROL_BUDGET,
    REASONING_CONTROL_LEVELS,
    ReasoningCapabilities,
)
from core.providers.anthropic import (
    ANTHROPIC_METADATA_KEY,
    SUPPORTS_TEMPERATURE_METADATA_FIELD,
    AnthropicAdapter,
)
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

from .anthropic_test_support import (
    ANTHROPIC_CONFIG,
    ANTHROPIC_URL,
    API_KEY,
    MODEL_ID,
    SAMPLE_MESSAGES,
    SUCCESS_RESPONSE,
    make_adapter,
)

# ---------------------------------------------------------------------------
# Catalog discovery
# ---------------------------------------------------------------------------


def _catalog_entry(
    model_id: str = "claude-x",
    *,
    adaptive: bool,
    enabled: bool,
    efforts: tuple[str, ...] = (),
    thinking: bool = True,
) -> dict:
    """A raw ``/models`` entry mirroring the live Anthropic catalog shape."""

    return {
        "type": "model",
        "id": model_id,
        "display_name": f"Display {model_id}",
        "max_input_tokens": 1000000,
        "max_tokens": 128000,
        "capabilities": {
            "image_input": {"supported": True},
            "pdf_input": {"supported": True},
            "structured_outputs": {"supported": True},
            "thinking": {
                "supported": thinking,
                "types": {"enabled": {"supported": enabled}, "adaptive": {"supported": adaptive}},
            },
            "effort": {
                "supported": bool(efforts),
                **{level: {"supported": True} for level in efforts},
            },
        },
    }


def test_catalog_entry_maps_listing_facts_onto_a_model() -> None:
    model = AnthropicAdapter.normalize_catalog_entry(
        _catalog_entry("claude-opus-4-8", adaptive=True, enabled=False)
    )

    assert model.model_id == "claude-opus-4-8"
    assert model.name == "Display claude-opus-4-8"
    assert model.context_window == 1000000
    assert model.max_output_tokens == 128000
    assert model.capabilities.vision is True
    assert model.capabilities.tools is True
    assert model.capabilities.json_mode is True
    assert model.capabilities.input_modalities == ("text", "image", "pdf")

    unnamed = _catalog_entry(adaptive=True, enabled=False)
    del unnamed["display_name"]
    assert AnthropicAdapter.normalize_catalog_entry(unnamed).name == "claude-x"


@pytest.mark.parametrize(
    ("entry", "reasoning", "supports_temperature"),
    [
        pytest.param(
            _catalog_entry(adaptive=True, enabled=False, efforts=("low", "high", "max")),
            ReasoningCapabilities(
                supported=True, control=REASONING_CONTROL_LEVELS, levels=("low", "high", "max")
            ),
            False,
            id="adaptive-only-levels",
        ),
        pytest.param(
            _catalog_entry(adaptive=False, enabled=True),
            ReasoningCapabilities(supported=True, control=REASONING_CONTROL_BUDGET),
            True,
            id="native-budget",
        ),
        pytest.param(
            _catalog_entry(adaptive=False, enabled=True, efforts=("low", "medium", "high")),
            ReasoningCapabilities(supported=True, control=REASONING_CONTROL_BUDGET),
            True,
            id="effort-ladder-without-adaptive-is-budget",
        ),
        pytest.param(
            _catalog_entry(adaptive=False, enabled=False),
            ReasoningCapabilities(supported=True),
            True,
            id="thinking-without-control",
        ),
        pytest.param(
            _catalog_entry(adaptive=False, enabled=False, thinking=False),
            ReasoningCapabilities(supported=False),
            True,
            id="non-reasoning",
        ),
    ],
)
def test_catalog_reasoning_control_and_sampling_follow_live_thinking_caps(
    entry, reasoning, supports_temperature
) -> None:
    model = AnthropicAdapter.normalize_catalog_entry(entry)

    assert model.capabilities.reasoning == reasoning
    assert (
        model.metadata[ANTHROPIC_METADATA_KEY][SUPPORTS_TEMPERATURE_METADATA_FIELD]
        is supports_temperature
    )


def test_catalog_discovery_pages_the_listing_with_the_version_header() -> None:
    headers = AnthropicAdapter.discovery_headers(ANTHROPIC_CONFIG, API_KEY, {"x-api-key": "secret"})

    assert headers == {"x-api-key": "secret", "anthropic-version": "2023-06-01"}
    assert AnthropicAdapter.discovery_params() == {"limit": "1000"}


# ---------------------------------------------------------------------------
# Completed response normalization
# ---------------------------------------------------------------------------


def test_normalize_response_maps_blocks_to_canonical_assistant_fields() -> None:
    thinking = {"type": "thinking", "thinking": "Need weather.", "signature": "opaque"}
    redacted = {"type": "redacted_thinking", "data": "opaque-redacted"}
    response = {
        "content": [
            thinking,
            redacted,
            {"type": "text", "text": "Checking"},
            {"type": "text", "text": " now."},
            {
                "type": "tool_use",
                "id": "toolu_abc",
                "name": "get_weather",
                "input": {"city": "Berlin"},
            },
        ]
    }

    assert make_adapter().normalize_response(response) == {
        "role": "assistant",
        "content": "Checking now.",
        "reasoning": "Need weather.",
        "reasoning_meta": {"content_blocks": [thinking, redacted]},
        "tool_calls": [{"id": "toolu_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}],
        "terminal_outcome": "unknown",
    }


def test_normalize_response_accepts_a_collapsed_single_tool_use_block() -> None:
    normalized = make_adapter().normalize_response(
        {
            "content": {"type": "tool_use", "id": "toolu_one", "name": "get_weather", "input": {}},
            "stop_reason": "tool_use",
        }
    )

    assert normalized["tool_calls"] == [{"id": "toolu_one", "name": "get_weather", "arguments": {}}]
    assert normalized["terminal_outcome"] == "tool_calls"


@pytest.mark.parametrize(
    ("stop_reason", "expected_outcome"),
    [
        ("end_turn", "stop"),
        ("tool_use", "tool_calls"),
        ("max_tokens", "output_truncated"),
        ("refusal", "content_filtered"),
        ("pause_turn", "error"),
        ("provider_added_reason", "unknown"),
    ],
)
def test_normalize_response_preserves_terminal_outcome(stop_reason, expected_outcome) -> None:
    normalized = make_adapter().normalize_response(
        {"content": [{"type": "text", "text": "partial"}], "stop_reason": stop_reason}
    )

    assert normalized["terminal_outcome"] == expected_outcome


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        pytest.param(
            {"input_tokens": 25, "output_tokens": 87},
            {"input_tokens": 25, "output_tokens": 87},
            id="primary",
        ),
        pytest.param(
            {"input_tokens": 2589, "output_tokens": 0},
            {"input_tokens": 2589, "output_tokens": 0},
            id="zero-output",
        ),
        pytest.param(
            {
                "input_tokens": 25,
                "output_tokens": 87,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 200,
                "output_tokens_details": {"thinking_tokens": 55},
            },
            {
                "input_tokens": 1225,
                "output_tokens": 87,
                "cache_read_tokens": 1000,
                "cache_write_tokens": 200,
                "reasoning_tokens": 55,
            },
            id="cache-folded-into-input",
        ),
        pytest.param(
            {"input_tokens": True, "output_tokens": 8}, {"output_tokens": 8}, id="boolean-input"
        ),
        pytest.param(
            {"input_tokens": 17, "output_tokens": -1}, {"input_tokens": 17}, id="negative-output"
        ),
        pytest.param(
            {"input_tokens": "17", "output_tokens": 8}, {"output_tokens": 8}, id="string-input"
        ),
        pytest.param(
            {
                "input_tokens": 17,
                "output_tokens": 8,
                "cache_read_input_tokens": True,
                "cache_creation_input_tokens": -20,
            },
            {"input_tokens": 17, "output_tokens": 8},
            id="unusable-cache-counters",
        ),
        pytest.param(
            {"input_tokens": 17, "output_tokens": 8, "cache_read_input_tokens": None},
            {"input_tokens": 17, "output_tokens": 8},
            id="null-cache-counter",
        ),
        pytest.param(None, None, id="null"),
    ],
)
def test_normalize_response_keeps_only_usable_usage_counters(usage, expected) -> None:
    response = {"content": [{"type": "text", "text": "Hello!"}], "usage": usage}

    assert make_adapter().normalize_response(response).get("usage") == expected
    assert "usage" not in make_adapter().normalize_response({"content": []})


# ---------------------------------------------------------------------------
# send() errors and retry
# ---------------------------------------------------------------------------


def _anthropic_error(status: int, error_type: str, message: str) -> httpx.Response:
    return httpx.Response(
        status, json={"type": "error", "error": {"type": error_type, "message": message}}
    )


@pytest.mark.parametrize(
    ("response", "expected_type", "retryable", "attempts"),
    [
        pytest.param(
            _anthropic_error(401, "authentication_error", "invalid x-api-key"),
            ProviderAuthError,
            False,
            1,
            id="401",
        ),
        pytest.param(
            _anthropic_error(403, "permission_error", "Forbidden"),
            ProviderAuthError,
            False,
            1,
            id="403",
        ),
        pytest.param(
            _anthropic_error(400, "invalid_request_error", "max_tokens is required"),
            ProviderError,
            False,
            1,
            id="400",
        ),
        pytest.param(
            _anthropic_error(500, "api_error", "Internal server error"),
            ProviderError,
            False,
            1,
            id="500",
        ),
        pytest.param(
            httpx.Response(200, text="not-valid-json{"),
            ProviderError,
            False,
            1,
            id="malformed-json",
        ),
        pytest.param(
            _anthropic_error(429, "rate_limit_error", "Too many requests"),
            ProviderRateLimitError,
            True,
            4,
            id="429",
        ),
        pytest.param(httpx.Response(502, text="Bad Gateway"), ProviderError, True, 4, id="502"),
        pytest.param(
            _anthropic_error(529, "overloaded_error", "Overloaded"),
            ProviderError,
            True,
            4,
            id="529",
        ),
    ],
)
@pytest.mark.asyncio
async def test_send_classifies_error_responses_and_retries_only_transient_ones(
    response, expected_type, retryable, attempts
) -> None:
    with respx.mock, patch("core.utils.retry._sleep", new_callable=AsyncMock):
        route = respx.post(ANTHROPIC_URL).mock(return_value=response)
        with pytest.raises(ProviderError) as exc_info:
            await make_adapter().send(SAMPLE_MESSAGES, model_id=MODEL_ID)

    assert type(exc_info.value) is expected_type
    assert exc_info.value.retryable is retryable
    assert route.call_count == attempts


@pytest.mark.asyncio
async def test_send_error_detail_carries_the_provider_error_message() -> None:
    with respx.mock:
        respx.post(ANTHROPIC_URL).mock(
            return_value=_anthropic_error(400, "invalid_request_error", "max_tokens is required")
        )
        with pytest.raises(ProviderError, match="max_tokens is required"):
            await make_adapter().send(SAMPLE_MESSAGES, model_id=MODEL_ID)


@pytest.mark.parametrize(
    ("failure", "expected_type"),
    [
        (httpx.TimeoutException("timed out"), ProviderTimeoutError),
        (httpx.ConnectError("connection failed"), NetworkError),
    ],
)
@pytest.mark.asyncio
async def test_send_wraps_transport_failures_after_retries(failure, expected_type) -> None:
    with respx.mock, patch("core.utils.retry._sleep", new_callable=AsyncMock):
        respx.post(ANTHROPIC_URL).mock(side_effect=failure)
        with pytest.raises(expected_type):
            await make_adapter().send(SAMPLE_MESSAGES, model_id=MODEL_ID)


@pytest.mark.parametrize(
    "first_attempt",
    [
        pytest.param(httpx.Response(429, text="Rate limited"), id="429"),
        pytest.param(httpx.Response(503, text="Service Unavailable"), id="503"),
        pytest.param(httpx.Response(529, text="Overloaded"), id="529"),
        pytest.param(httpx.TimeoutException("Connection timed out"), id="timeout"),
        pytest.param(httpx.ReadError("connection reset"), id="read-error"),
    ],
)
@pytest.mark.asyncio
async def test_send_retries_a_transient_failure_and_returns_the_response(first_attempt) -> None:
    with respx.mock, patch("core.utils.retry._sleep", new_callable=AsyncMock):
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[first_attempt, httpx.Response(200, json=SUCCESS_RESPONSE)]
        )
        result = await make_adapter().send(SAMPLE_MESSAGES, model_id=MODEL_ID)

    assert result == SUCCESS_RESPONSE
    assert route.call_count == 2
