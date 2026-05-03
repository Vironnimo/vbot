"""Tests for OpenAICompatibleAdapter.

Uses ``respx`` to mock httpx calls.  Verifies request building, header
and defaults injection, SSE streaming, retry on retryable errors, and
immediate failure on auth errors.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ProviderConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OPENAI_CONFIG = ProviderConfig(
    id="openai",
    name="OpenAI",
    adapter="openai_compatible",
    base_url="https://api.openai.com/v1",
    auth=AuthConfig(header="Authorization", prefix="Bearer ", env_key="OPENAI_API_KEY"),
    defaults={"max_tokens": 4096, "temperature": 0.7},
)

OPENROUTER_CONFIG = ProviderConfig(
    id="openrouter",
    name="OpenRouter",
    adapter="openai_compatible",
    base_url="https://openrouter.ai/api/v1",
    auth=AuthConfig(header="Authorization", prefix="Bearer ", env_key="OPENROUTER_API_KEY"),
    defaults={"max_tokens": 4096},
    extra_headers={"HTTP-Referer": "https://vbot.app", "X-Title": "vBot"},
)

NO_DEFAULTS_CONFIG = ProviderConfig(
    id="minimal",
    name="Minimal Provider",
    adapter="openai_compatible",
    base_url="https://api.minimal.example/v1",
    auth=AuthConfig(header="x-api-key", prefix="", env_key="MINIMAL_API_KEY"),
)

API_KEY = "test-api-key-12345"

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MINIMAL_URL = "https://api.minimal.example/v1/chat/completions"

SUCCESS_RESPONSE = {
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

SAMPLE_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello"},
]

CANONICAL_MESSAGES_WITH_TOOL_LOOP = [
    {"role": "system", "model": "openai/gpt-5.2", "content": "You are helpful."},
    {"role": "user", "content": "Weather?"},
    {
        "role": "assistant",
        "model": "openai/gpt-5.2",
        "content": None,
        "reasoning_meta": {"encrypted_content": "opaque-current-turn"},
        "tool_calls": [
            {
                "id": "call_abc",
                "name": "get_weather",
                "arguments": {"city": "Berlin"},
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_abc",
        "name": "get_weather",
        "content": '{"temp":22}',
    },
]

SAMPLE_TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]


@pytest.fixture()
def openai_adapter():
    """OpenAI-compatible adapter with default OpenAI config."""
    return OpenAICompatibleAdapter(OPENAI_CONFIG, API_KEY)


@pytest.fixture()
def openrouter_adapter():
    """OpenAI-compatible adapter with OpenRouter config (extra headers)."""
    return OpenAICompatibleAdapter(OPENROUTER_CONFIG, API_KEY)


# ---------------------------------------------------------------------------
# send() — request format
# ---------------------------------------------------------------------------


class TestSendRequestFormat:
    """Verify that send() translates messages to the correct OpenAI format."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_includes_model_and_messages(self, openai_adapter):
        """The request payload contains the model ID and messages."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert route.called
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["model"] == "gpt-5.2"
        assert request_body["messages"] == SAMPLE_MESSAGES

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_applies_defaults_from_config(self, openai_adapter):
        """Defaults from ProviderConfig are included when not overridden."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["max_tokens"] == 4096
        assert request_body["temperature"] == 0.7

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_kwargs_override_defaults(self, openai_adapter):
        """Caller kwargs take precedence over provider defaults."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", temperature=1.2)

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["temperature"] == 1.2  # overridden
        assert request_body["max_tokens"] == 4096  # from defaults

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_without_defaults(self):
        """When config has no defaults, only model and messages are sent."""
        # Arrange
        adapter = OpenAICompatibleAdapter(NO_DEFAULTS_CONFIG, API_KEY)
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="minimal-model")

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert "model" in request_body
        assert "messages" in request_body
        assert "max_tokens" not in request_body
        assert "temperature" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_canonical_messages_tools_and_reasoning(self, openai_adapter):
        """Canonical messages, tool definitions, and effort map to OpenAI wire format."""
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        await openai_adapter.send(
            CANONICAL_MESSAGES_WITH_TOOL_LOOP,
            model_id="gpt-5.2",
            tools=SAMPLE_TOOLS,
            thinking_effort="high",
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["messages"] == [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Weather?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city":"Berlin"}',
                        },
                    }
                ],
                "encrypted_content": "opaque-current-turn",
            },
            {"role": "tool", "tool_call_id": "call_abc", "content": '{"temp":22}'},
        ]
        assert request_body["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather",
                    "parameters": SAMPLE_TOOLS[0]["parameters"],
                },
            }
        ]
        assert request_body["reasoning_effort"] == "high"

    @respx.mock
    @pytest.mark.asyncio
    async def test_openrouter_reasoning_uses_openrouter_wire_format(self, openrouter_adapter):
        """OpenRouter gets reasoning object and include_reasoning instead of OpenAI string."""
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await openrouter_adapter.send(
            SAMPLE_MESSAGES, model_id="openai/gpt-5.2", thinking_effort="xhigh"
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["reasoning"] == {"effort": "xhigh"}
        assert request_body["include_reasoning"] is True
        assert "reasoning_effort" not in request_body


# ---------------------------------------------------------------------------
# send() — headers and auth
# ---------------------------------------------------------------------------


class TestSendHeaders:
    """Verify that send() sends the correct auth and extra headers."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_bearer_auth_header(self, openai_adapter):
        """OpenAI config sends Authorization: Bearer <key>."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert route.called
        auth_header = route.calls.last.request.headers.get("authorization")
        assert auth_header == f"Bearer {API_KEY}"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_custom_auth_header(self):
        """Config with x-api-key header sends the key without Bearer prefix."""
        # Arrange
        adapter = OpenAICompatibleAdapter(NO_DEFAULTS_CONFIG, API_KEY)
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="minimal-model")

        # Assert
        api_key_header = route.calls.last.request.headers.get("x-api-key")
        assert api_key_header == API_KEY

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_extra_headers(self, openrouter_adapter):
        """OpenRouter config includes extra HTTP-Referer and X-Title headers."""
        # Arrange
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await openrouter_adapter.send(SAMPLE_MESSAGES, model_id="openai/gpt-5.2")

        # Assert
        request = route.calls.last.request
        assert request.headers.get("http-referer") == "https://vbot.app"
        assert request.headers.get("x-title") == "vBot"


# ---------------------------------------------------------------------------
# send() — success response
# ---------------------------------------------------------------------------


class TestSendSuccess:
    """Verify that send() returns the parsed response dict on success."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_returns_parsed_response(self, openai_adapter):
        """send() returns the full response body as a dict."""
        # Arrange
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        result = await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert result == SUCCESS_RESPONSE
        assert result["id"] == "chatcmpl-abc123"
        assert result["choices"][0]["message"]["content"] == "Hello!"

    def test_normalize_response_extracts_text_tool_calls_and_reasoning(self, openai_adapter):
        """Provider response is normalized to canonical assistant fields."""
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning": "Need weather.",
                        "encrypted_content": "opaque",
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":"Berlin"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }

        normalized = openai_adapter.normalize_response(response)

        assert normalized == {
            "role": "assistant",
            "content": None,
            "reasoning": "Need weather.",
            "reasoning_meta": {"encrypted_content": "opaque"},
            "tool_calls": [
                {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
            ],
        }

    def test_normalize_response_uses_empty_arguments_for_malformed_tool_json(self, openai_adapter):
        """Malformed provider tool-call JSON does not leak JSONDecodeError."""
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": "{not-json}",
                                },
                            }
                        ],
                    }
                }
            ]
        }

        normalized = openai_adapter.normalize_response(response)

        assert normalized["tool_calls"] == [
            {"id": "call_abc", "name": "get_weather", "arguments": {}}
        ]

    def test_normalize_response_preserves_openrouter_reasoning_details(self, openrouter_adapter):
        """OpenRouter opaque reasoning_details are preserved unchanged."""
        reasoning_details = [{"type": "reasoning.text", "text": "opaque"}]
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Done",
                        "reasoning_content": "Visible reasoning",
                        "reasoning_details": reasoning_details,
                    }
                }
            ]
        }

        normalized = openrouter_adapter.normalize_response(response)

        assert normalized["content"] == "Done"
        assert normalized["reasoning"] == "Visible reasoning"
        assert normalized["reasoning_meta"] == {"reasoning_details": reasoning_details}


# ---------------------------------------------------------------------------
# send() — error classification
# ---------------------------------------------------------------------------


class TestSendErrorClassification:
    """Verify that send() raises the correct error type for each HTTP status."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_401_raises_provider_auth_error(self, openai_adapter):
        """HTTP 401 raises ProviderAuthError (not retryable)."""
        # Arrange
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(401, text="Invalid API key"))

        # Act / Assert
        with pytest.raises(ProviderAuthError, match="401"):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_403_raises_provider_auth_error(self, openai_adapter):
        """HTTP 403 raises ProviderAuthError (not retryable)."""
        # Arrange
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(403, text="Forbidden"))

        # Act / Assert
        with pytest.raises(ProviderAuthError, match="403"):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_429_raises_provider_rate_limit_error(self, openai_adapter):
        """HTTP 429 raises ProviderRateLimitError (retryable), retried then raised."""
        # Arrange — 4 requests: 3 retries + 1 final that also fails
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(429, text="Rate limited"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderRateLimitError, match="429"),
        ):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_timeout_raises_provider_timeout_error(self, openai_adapter):
        """Connection timeout raises ProviderTimeoutError."""
        # Arrange
        respx.post(OPENAI_URL).mock(side_effect=httpx.TimeoutException("timed out"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderTimeoutError, match="timed out"),
        ):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_500_raises_non_retryable_provider_error(self, openai_adapter):
        """HTTP 500 raises ProviderError with retryable=False (not in retryable set)."""
        # Arrange
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(500, text="Internal Server Error"))

        # Act / Assert
        with pytest.raises(ProviderError) as exc_info:
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        assert exc_info.value.retryable is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_502_raises_retryable_provider_error(self, openai_adapter):
        """HTTP 502 raises ProviderError with retryable=True."""
        # Arrange — all retries fail
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(502, text="Bad Gateway"))

        # Act / Assert
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ProviderError) as exc_info:
                await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

            assert exc_info.value.retryable is True


# ---------------------------------------------------------------------------
# send() — retry behaviour
# ---------------------------------------------------------------------------


class TestSendRetry:
    """Verify that send() retries on retryable errors and not on auth errors."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_429_then_succeeds(self, openai_adapter):
        """send() retries on 429 and succeeds when the next attempt returns 200."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(429, text="Rate limited"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_502_then_succeeds(self, openai_adapter):
        """send() retries on 502 and succeeds when the next attempt returns 200."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(502, text="Bad Gateway"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_503_then_succeeds(self, openai_adapter):
        """send() retries on 503 and succeeds when the next attempt returns 200."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(503, text="Service Unavailable"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_no_retry_on_401(self, openai_adapter):
        """send() raises ProviderAuthError immediately on 401 — no retry."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(401, text="Unauthorized"))

        # Act / Assert
        with pytest.raises(ProviderAuthError):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_no_retry_on_403(self, openai_adapter):
        """send() raises ProviderAuthError immediately on 403 — no retry."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(403, text="Forbidden"))

        # Act / Assert
        with pytest.raises(ProviderAuthError):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retry_on_timeout_then_succeeds(self, openai_adapter):
        """send() retries on timeout and succeeds on the next attempt."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(
            side_effect=[
                httpx.TimeoutException("Connection timed out"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_multiple_retries_then_success(self, openai_adapter):
        """send() retries up to 3 times on consecutive 429s before success."""
        # Arrange — 3 rate-limited responses, then success on 4th attempt
        route = respx.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(429, text="Rate limited"),
                httpx.Response(429, text="Rate limited"),
                httpx.Response(429, text="Rate limited"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 4  # 3 retries + 1 initial = 4 total


# ---------------------------------------------------------------------------
# send() — provider config integration
# ---------------------------------------------------------------------------


class TestSendProviderConfig:
    """Verify that provider config values are correctly used."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_uses_base_url_from_config(self, openrouter_adapter):
        """The request goes to the base_url specified in ProviderConfig."""
        # Arrange
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await openrouter_adapter.send(SAMPLE_MESSAGES, model_id="openai/gpt-5.2")

        # Assert
        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_uses_auth_from_config(self):
        """Config with prefix='' sends the key directly in the auth header."""
        # Arrange
        adapter = OpenAICompatibleAdapter(NO_DEFAULTS_CONFIG, API_KEY)
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="minimal-model")

        # Assert
        api_key_header = route.calls.last.request.headers.get("x-api-key")
        assert api_key_header == API_KEY  # No "Bearer " prefix


# ---------------------------------------------------------------------------
# stream() — SSE parsing
# ---------------------------------------------------------------------------


class TestStreamSSE:
    """Verify that stream() correctly parses SSE event chunks."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_sse_chunks(self, openai_adapter):
        """stream() parses SSE data lines and yields parsed JSON chunks."""
        # Arrange
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"}}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":" world"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        route = respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        # Act
        chunks = []
        async for chunk in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
            chunks.append(chunk)

        # Assert
        assert len(chunks) == 2
        assert chunks[0]["choices"][0]["delta"]["content"] == "Hello"
        assert chunks[1]["choices"][0]["delta"]["content"] == " world"
        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_includes_stream_true_in_payload(self, openai_adapter):
        """stream() sends stream=true in the request payload."""
        # Arrange
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
        )
        route = respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        # Act
        async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
            pass

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["stream"] is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_ignores_non_data_lines(self, openai_adapter):
        """stream() skips lines that don't start with 'data: '."""
        # Arrange — includes comment lines and empty lines
        sse_body = (
            ": this is a comment\n"
            "\n"
            'data: {"id":"1","choices":[{"delta":{"content":"A"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        # Act
        chunks = []
        async for chunk in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
            chunks.append(chunk)

        # Assert
        assert len(chunks) == 1
        assert chunks[0]["id"] == "1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_401_raises_provider_auth_error(self, openai_adapter):
        """stream() raises ProviderAuthError on 401 — no retry."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(401, text="Unauthorized"))

        # Act / Assert
        with pytest.raises(ProviderAuthError):
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass

        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_applies_extra_headers(self, openrouter_adapter):
        """stream() includes extra_headers from provider config."""
        # Arrange
        sse_body = 'data: {"id":"1","choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        # Act
        async for _ in openrouter_adapter.stream(SAMPLE_MESSAGES, model_id="openai/gpt-5.2"):
            pass

        # Assert
        request = route.calls.last.request
        assert request.headers.get("http-referer") == "https://vbot.app"
        assert request.headers.get("x-title") == "vBot"

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_timeout_raises_provider_timeout_error(self, openai_adapter):
        """stream() raises ProviderTimeoutError on connection timeout."""
        # Arrange
        respx.post(OPENAI_URL).mock(side_effect=httpx.TimeoutException("timed out"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderTimeoutError, match="timed out"),
        ):
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass


# ---------------------------------------------------------------------------
# Lifecycle: aclose() and async context manager
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Verify that aclose() and async context manager work correctly."""

    @pytest.mark.asyncio
    async def test_aclose_closes_http_client(self):
        """aclose() closes the underlying httpx.AsyncClient."""
        adapter = OpenAICompatibleAdapter(OPENAI_CONFIG, API_KEY)
        assert not adapter._client.is_closed
        await adapter.aclose()
        assert adapter._client.is_closed

    @pytest.mark.asyncio
    async def test_context_manager_closes_client(self):
        """Using 'async with' closes the client on exit."""
        async with OpenAICompatibleAdapter(OPENAI_CONFIG, API_KEY) as adapter:
            assert not adapter._client.is_closed
        assert adapter._client.is_closed

    @pytest.mark.asyncio
    async def test_context_manager_yields_adapter(self):
        """The context manager yields the adapter instance."""
        async with OpenAICompatibleAdapter(OPENAI_CONFIG, API_KEY) as adapter:
            assert isinstance(adapter, OpenAICompatibleAdapter)
