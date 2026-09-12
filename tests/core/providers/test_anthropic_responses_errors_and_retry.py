"""Anthropic responses: errors and retry behavior."""

from __future__ import annotations

from .anthropic_test_support import (
    ANTHROPIC_URL,
    SAMPLE_MESSAGES,
    SUCCESS_RESPONSE,
    AsyncMock,
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    httpx,
    patch,
    pytest,
    respx,
)
from .anthropic_test_support import anthropic_adapter as anthropic_adapter
from .anthropic_test_support import custom_adapter as custom_adapter


# send() — error classification
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
            pytest.raises(ProviderTimeoutError),
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
            pytest.raises(NetworkError, match="connection failed"),
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
            pytest.raises(NetworkError, match="connection reset"),
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
            pytest.raises(NetworkError, match="server disconnected"),
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
        with pytest.raises(ProviderError, match="max_tokens is required"):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")


# send() — retry behaviour
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
