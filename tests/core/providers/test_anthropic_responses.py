"""Anthropic response, error, retry, and header tests."""

from __future__ import annotations

from .anthropic_test_support import (
    ANTHROPIC_CONFIG,
    ANTHROPIC_MULTI_AUTH_CONFIG,
    ANTHROPIC_URL,
    API_KEY,
    CUSTOM_URL,
    IMAGE_WIRE_MEDIA_TYPES,
    REASONING_REPLAY_FULL_HISTORY,
    SAMPLE_MESSAGES,
    SUCCESS_RESPONSE,
    AnthropicAdapter,
    AsyncMock,
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    _anthropic_test_model,
    _strip_cache_control,
    httpx,
    json,
    patch,
    pytest,
    respx,
)
from .anthropic_test_support import anthropic_adapter as anthropic_adapter
from .anthropic_test_support import custom_adapter as custom_adapter

PRIOR_RUN_THINKING_BLOCK = {
    "type": "thinking",
    "thinking": "Prior-run reasoning.",
    "signature": "opaque-prior-run-signature",
}
PRIOR_RUN_REDACTED_BLOCK = {"type": "redacted_thinking", "data": "opaque-prior-run-redacted"}

TWO_RUN_HISTORY = [
    {"role": "user", "content": "Q1"},
    {
        "role": "assistant",
        "model": "anthropic/claude-sonnet-4-20250219",
        "content": "A1",
        "reasoning": "Prior-run reasoning.",
        "reasoning_meta": {"content_blocks": [PRIOR_RUN_THINKING_BLOCK, PRIOR_RUN_REDACTED_BLOCK]},
    },
    {"role": "user", "content": "Q2"},
]


def test_wire_media_support_is_images_plus_pdf(anthropic_adapter):
    """The Anthropic Messages wire carries images plus native ``application/pdf``."""
    assert anthropic_adapter.wire_media_support("claude-sonnet-4-20250219") == (
        IMAGE_WIRE_MEDIA_TYPES | frozenset({"application/pdf"})
    )


class TestReasoningReplay:
    """Cross-run thinking replay (full_history policy) and its disabled guard."""

    def test_reasoning_replay_policy_is_full_history(self, anthropic_adapter):
        assert (
            anthropic_adapter.reasoning_replay_policy("claude-sonnet-4-20250219")
            == REASONING_REPLAY_FULL_HISTORY
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_replays_prior_run_thinking_blocks_byte_identical(self, anthropic_adapter):
        """A two-run same-model history resends persisted thinking unchanged."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            TWO_RUN_HISTORY,
            model_id="claude-sonnet-4-20250219",
            thinking_effort="high",
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["messages"][1]["content"] == [
            PRIOR_RUN_THINKING_BLOCK,
            PRIOR_RUN_REDACTED_BLOCK,
            {"type": "text", "text": "A1"},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_strips_replayed_thinking_blocks_when_thinking_disabled(
        self, anthropic_adapter
    ):
        """Disabled thinking must not carry historical thinking blocks."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            TWO_RUN_HISTORY,
            model_id="claude-sonnet-4-20250219",
            thinking_effort="none",
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["thinking"] == {"type": "disabled"}
        assert request_body["messages"][1]["content"] == [{"type": "text", "text": "A1"}]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_keeps_replayed_thinking_blocks_without_thinking_parameter(
        self, anthropic_adapter
    ):
        """An absent thinking parameter is not 'disabled' — blocks stay."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(TWO_RUN_HISTORY, model_id="claude-sonnet-4-20250219")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert "thinking" not in request_body
        assert request_body["messages"][1]["content"][:2] == [
            PRIOR_RUN_THINKING_BLOCK,
            PRIOR_RUN_REDACTED_BLOCK,
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_strips_replayed_thinking_blocks_for_non_reasoning_model(self):
        """Catalog-known non-reasoning models never receive thinking blocks."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            ANTHROPIC_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_test_model(model_id, reasoning=False),
        )

        await adapter.send(
            TWO_RUN_HISTORY,
            model_id="claude-3-5-haiku-20241022",
            thinking_effort="high",
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert "thinking" not in request_body
        assert request_body["messages"][1]["content"] == [{"type": "text", "text": "A1"}]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_drops_reasoning_only_assistant_turn_when_thinking_disabled(
        self, anthropic_adapter
    ):
        """Stripping must not leave an empty assistant content array on the wire."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {"role": "user", "content": "Q1"},
            {
                "role": "assistant",
                "model": "anthropic/claude-sonnet-4-20250219",
                "content": None,
                "reasoning": "Thinking-only turn",
                "reasoning_meta": {"content_blocks": [PRIOR_RUN_THINKING_BLOCK]},
            },
            {"role": "user", "content": "Q2"},
        ]

        await anthropic_adapter.send(
            messages,
            model_id="claude-sonnet-4-20250219",
            thinking_effort="none",
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "Q1"}]},
            {"role": "user", "content": [{"type": "text", "text": "Q2"}]},
        ]


# ---------------------------------------------------------------------------
# send() — headers and auth
# ---------------------------------------------------------------------------


class TestSendHeaders:
    """Verify that send() sends the correct auth and Anthropic headers."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_x_api_key_header(self, anthropic_adapter):
        """Anthropic config sends x-api-key header with the key directly."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        assert route.called
        api_key_header = route.calls.last.request.headers.get("x-api-key")
        assert api_key_header == API_KEY  # No "Bearer " prefix

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_anthropic_version_header(self, anthropic_adapter):
        """The anthropic-version header is sent in the request."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        version_header = route.calls.last.request.headers.get("anthropic-version")
        assert version_header == "2023-06-01"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_extra_headers(self, custom_adapter):
        """Custom config includes extra headers from provider config."""
        # Arrange
        route = respx.post(CUSTOM_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await custom_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        request = route.calls.last.request
        assert request.headers.get("x-custom-header") == "custom-value"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_no_bearer_prefix(self, anthropic_adapter):
        """Auth header does not have 'Bearer ' prefix."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        api_key_header = route.calls.last.request.headers.get("x-api-key")
        assert not api_key_header.startswith("Bearer ")
        assert api_key_header == API_KEY

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_uses_selected_connection_auth_header(self):
        """Selected connection auth metadata controls the request auth header."""
        # Arrange
        selected_connection = ANTHROPIC_MULTI_AUTH_CONFIG.get_connection("oauth")
        adapter = AnthropicAdapter(
            ANTHROPIC_MULTI_AUTH_CONFIG,
            API_KEY,
            auth_config=selected_connection.auth,
        )
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        request_headers = route.calls.last.request.headers
        assert request_headers.get("authorization") == f"Bearer {API_KEY}"
        assert request_headers.get("x-api-key") is None


# ---------------------------------------------------------------------------
# send() — success response
# ---------------------------------------------------------------------------


class TestSendSuccess:
    """Verify that send() returns the parsed response dict on success."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_returns_parsed_response(self, anthropic_adapter):
        """send() returns the full response body as a dict."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        result = await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        assert result == SUCCESS_RESPONSE
        assert result["id"] == "msg_01XFDUDYJGAAC8998t2N3v"
        assert result["content"][0]["text"] == "Hello!"

    def test_normalize_response_extracts_text_tool_calls_and_reasoning(self, anthropic_adapter):
        """Anthropic response blocks normalize to canonical assistant fields."""
        response = {
            "content": [
                {"type": "thinking", "thinking": "Need weather.", "signature": "opaque"},
                {"type": "text", "text": "Checking."},
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "get_weather",
                    "input": {"city": "Berlin"},
                },
            ]
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert normalized == {
            "role": "assistant",
            "content": "Checking.",
            "reasoning": "Need weather.",
            "reasoning_meta": {
                "content_blocks": [
                    {"type": "thinking", "thinking": "Need weather.", "signature": "opaque"}
                ]
            },
            "tool_calls": [
                {"id": "toolu_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
            ],
        }

    def test_normalize_response_preserves_redacted_thinking_block(self, anthropic_adapter):
        """Opaque redacted thinking metadata is preserved unchanged."""
        redacted_block = {"type": "redacted_thinking", "data": "opaque"}
        response = {
            "content": [
                {
                    "type": "thinking",
                    "thinking": "Visible reasoning",
                    "signature": "opaque-signature",
                },
                redacted_block,
            ]
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert normalized["reasoning"] == "Visible reasoning"
        assert normalized["reasoning_meta"] == {
            "content_blocks": [
                {
                    "type": "thinking",
                    "thinking": "Visible reasoning",
                    "signature": "opaque-signature",
                },
                redacted_block,
            ]
        }

    def test_normalize_response_includes_usage_with_both_fields(self, anthropic_adapter):
        """Usage with both input and output tokens is included in normalized response."""
        response = {
            "content": [{"type": "text", "text": "Hello!"}],
            "usage": {"input_tokens": 25, "output_tokens": 87},
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert normalized["usage"] == {"input_tokens": 25, "output_tokens": 87}

    def test_normalize_response_includes_usage_with_zero_output_tokens(self, anthropic_adapter):
        """Usage with input_tokens and output_tokens=0 (cache read) is included."""
        response = {
            "content": [{"type": "text", "text": "Cached."}],
            "usage": {"input_tokens": 2589, "output_tokens": 0},
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert normalized["usage"] == {"input_tokens": 2589, "output_tokens": 0}

    def test_normalize_response_folds_cache_tokens_into_input_tokens(self, anthropic_adapter):
        """Cache read/write tokens are exposed and added onto input_tokens.

        Anthropic reports cache tokens separately from input_tokens; canonical
        input_tokens means the total prompt including cached tokens.
        """
        response = {
            "content": [{"type": "text", "text": "Hello!"}],
            "usage": {
                "input_tokens": 25,
                "output_tokens": 87,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 200,
            },
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert normalized["usage"] == {
            "input_tokens": 1225,
            "output_tokens": 87,
            "cache_read_tokens": 1000,
            "cache_write_tokens": 200,
        }

    def test_normalize_response_ignores_non_int_cache_tokens(self, anthropic_adapter):
        """Non-integer cache token values are ignored and input_tokens stays raw."""
        response = {
            "content": [{"type": "text", "text": "Hello!"}],
            "usage": {
                "input_tokens": 25,
                "output_tokens": 87,
                "cache_read_input_tokens": None,
            },
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert normalized["usage"] == {"input_tokens": 25, "output_tokens": 87}

    def test_normalize_response_omits_usage_when_absent(self, anthropic_adapter):
        """Usage key is omitted when the response has no usage object."""
        response = {
            "content": [{"type": "text", "text": "Hello!"}],
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert "usage" not in normalized

    def test_normalize_response_omits_usage_when_null(self, anthropic_adapter):
        """Usage key is omitted when the response usage is None."""
        response = {
            "content": [{"type": "text", "text": "Hello!"}],
            "usage": None,
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert "usage" not in normalized


# ---------------------------------------------------------------------------
# send() — error classification
# ---------------------------------------------------------------------------


class TestSendErrorClassification:
    """Verify that send() raises the correct error type per HTTP status."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_401_raises_provider_auth_error(self, anthropic_adapter):
        """HTTP 401 raises ProviderAuthError (not retryable)."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                401,
                json={
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": "invalid x-api-key",
                    },
                },
            )
        )

        # Act / Assert
        with pytest.raises(ProviderAuthError, match="401"):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_403_raises_provider_auth_error(self, anthropic_adapter):
        """HTTP 403 raises ProviderAuthError (not retryable)."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                403,
                json={
                    "type": "error",
                    "error": {
                        "type": "permission_error",
                        "message": "Forbidden",
                    },
                },
            )
        )

        # Act / Assert
        with pytest.raises(ProviderAuthError, match="403"):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_429_raises_provider_rate_limit_error(self, anthropic_adapter):
        """HTTP 429 raises ProviderRateLimitError (retryable), retried then raised."""
        # Arrange — all retries fail
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                429,
                json={
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error",
                        "message": "Too many requests",
                    },
                },
            )
        )

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderRateLimitError, match="429"),
        ):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_timeout_raises_provider_timeout_error(self, anthropic_adapter):
        """Connection timeout raises ProviderTimeoutError."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(side_effect=httpx.TimeoutException("timed out"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderTimeoutError, match="timed out"),
        ):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_connect_error_raises_network_error(self, anthropic_adapter):
        """Connection failures raise NetworkError."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(side_effect=httpx.ConnectError("connection failed"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(NetworkError, match="Connection failed: connection failed"),
        ):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_500_raises_non_retryable_provider_error(self, anthropic_adapter):
        """HTTP 500 raises ProviderError with retryable=False."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                500,
                json={
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": "Internal server error",
                    },
                },
            )
        )

        # Act / Assert
        with pytest.raises(ProviderError) as exc_info:
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        assert exc_info.value.retryable is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_502_raises_retryable_provider_error(self, anthropic_adapter):
        """HTTP 502 raises ProviderError with retryable=True."""
        # Arrange — all retries fail
        respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(502, text="Bad Gateway"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderError) as exc_info,
        ):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        assert exc_info.value.retryable is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_529_raises_retryable_provider_error(self, anthropic_adapter):
        """HTTP 529 (Anthropic overloaded) raises retryable ProviderError."""
        # Arrange — all retries fail
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                529,
                json={
                    "type": "error",
                    "error": {
                        "type": "overloaded_error",
                        "message": "Overloaded",
                    },
                },
            )
        )

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderError) as exc_info,
        ):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        assert exc_info.value.retryable is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_read_error_raises_network_error(self, anthropic_adapter):
        """A non-streaming read failure (httpx.ReadError) is wrapped as NetworkError."""

        # Arrange
        respx.post(ANTHROPIC_URL).mock(side_effect=httpx.ReadError("connection reset"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(NetworkError, match="Connection failed: connection reset"),
        ):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_read_error_is_retried(self, anthropic_adapter):
        """A transient ReadError is retried; a subsequent success returns the response."""

        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.ReadError("connection reset"),
                httpx.Response(
                    200,
                    json={
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "model": "claude-sonnet-4-20250219",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 5, "output_tokens": 3},
                    },
                ),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await anthropic_adapter.send(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            )

        # Assert
        assert result["id"] == "msg_1"
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_remote_protocol_error_raises_network_error(self, anthropic_adapter):
        """A non-streaming RemoteProtocolError is wrapped as NetworkError."""

        # Arrange
        respx.post(ANTHROPIC_URL).mock(side_effect=httpx.RemoteProtocolError("server disconnected"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(NetworkError, match="Connection failed: server disconnected"),
        ):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_malformed_json_raises_non_retryable_provider_error(
        self,
        anthropic_adapter,
    ):
        """A 2xx response with unparseable JSON raises a non-retryable ProviderError."""

        # Arrange
        respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, text="not-valid-json{"))

        # Act / Assert
        with pytest.raises(ProviderError) as exc_info:
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        assert exc_info.value.retryable is False
        assert "malformed JSON" in str(exc_info.value)

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_parses_anthropic_error_format(self, anthropic_adapter):
        """Error messages include Anthropic's error type and message."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "max_tokens is required",
                    },
                },
            )
        )

        # Act / Assert
        with pytest.raises(ProviderError, match="invalid_request_error.*max_tokens is required"):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")


# ---------------------------------------------------------------------------
# send() — retry behaviour
# ---------------------------------------------------------------------------


class TestSendRetry:
    """Verify that send() retries on retryable errors, not on auth errors."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_429_then_succeeds(self, anthropic_adapter):
        """send() retries on 429 and succeeds on the next attempt."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.Response(429, text="Rate limited"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await anthropic_adapter.send(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            )

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_502_then_succeeds(self, anthropic_adapter):
        """send() retries on 502 and succeeds on the next attempt."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.Response(502, text="Bad Gateway"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await anthropic_adapter.send(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            )

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_503_then_succeeds(self, anthropic_adapter):
        """send() retries on 503 and succeeds on the next attempt."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.Response(503, text="Service Unavailable"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await anthropic_adapter.send(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            )

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_529_then_succeeds(self, anthropic_adapter):
        """send() retries on 529 (Anthropic overloaded) and succeeds."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.Response(529, text="Overloaded"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await anthropic_adapter.send(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            )

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_no_retry_on_401(self, anthropic_adapter):
        """send() raises ProviderAuthError immediately on 401."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )

        # Act / Assert
        with pytest.raises(ProviderAuthError):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_no_retry_on_403(self, anthropic_adapter):
        """send() raises ProviderAuthError immediately on 403."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(403, text="Forbidden"))

        # Act / Assert
        with pytest.raises(ProviderAuthError):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retry_on_timeout_then_succeeds(self, anthropic_adapter):
        """send() retries on timeout and succeeds on the next attempt."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.TimeoutException("Connection timed out"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await anthropic_adapter.send(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            )

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_multiple_retries_then_success(self, anthropic_adapter):
        """send() retries up to 3 times on consecutive 429s before success."""
        # Arrange — 3 rate-limited responses, then success on 4th attempt
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.Response(429, text="Rate limited"),
                httpx.Response(429, text="Rate limited"),
                httpx.Response(429, text="Rate limited"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await anthropic_adapter.send(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            )

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
    async def test_send_uses_base_url_from_config(self, custom_adapter):
        """The request goes to the base_url from ProviderConfig."""
        # Arrange
        route = respx.post(CUSTOM_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await custom_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_multiple_defaults_applied(self, custom_adapter):
        """Multiple defaults from the config are applied."""
        # Arrange
        route = respx.post(CUSTOM_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await custom_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert 0 < request_body["max_tokens"] < 8192
        assert request_body["temperature"] == 0.7


# ---------------------------------------------------------------------------
# _build_payload() — None-valued caller kwargs
# ---------------------------------------------------------------------------


class TestBuildPayloadNoneKwargs:
    """``None``-valued caller kwargs are dropped, letting provider defaults win.

    Falsy-but-not-None values (e.g. ``0.0``) must survive. Explicit non-None
    values must still override the default. Covers both ``send()`` and
    ``stream()`` payload construction (both call ``_build_payload``).
    """

    @respx.mock
    @pytest.mark.asyncio
    async def test_none_kwarg_drops_key_and_provider_default_applies(self, custom_adapter):
        """``temperature=None`` is absent from the payload; default fills in."""
        # Arrange — CUSTOM_CONFIG declares defaults.temperature=0.7
        route = respx.post(CUSTOM_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await custom_adapter.send(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219", temperature=None
        )

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert "temperature" in request_body
        assert request_body["temperature"] == 0.7  # from defaults

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_zero_kwarg_survives_through_send(self, custom_adapter):
        """``temperature=0.0`` (falsy but not None) survives the None filter."""
        # Arrange
        route = respx.post(CUSTOM_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await custom_adapter.send(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219", temperature=0.0
        )

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["temperature"] == 0.0

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_nonzero_kwarg_overrides_default(self, custom_adapter):
        """Explicit non-None kwargs continue to override the provider default."""
        # Arrange
        route = respx.post(CUSTOM_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await custom_adapter.send(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219", temperature=0.3
        )

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["temperature"] == 0.3

    @respx.mock
    @pytest.mark.asyncio
    async def test_none_kwarg_drops_key_for_stream(self, custom_adapter):
        """``stream()`` also drops ``None`` caller kwargs before sending."""
        sse_body = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        route = respx.post(CUSTOM_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        async for _ in custom_adapter.stream(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219", temperature=None
        ):
            pass

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["temperature"] == 0.7  # default applied
        assert request_body["stream"] is True  # stream() still adds stream=true
