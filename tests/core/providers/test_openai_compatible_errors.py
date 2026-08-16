"""OpenAI-compatible send error classification, retries, and reasoning diagnostics."""

from __future__ import annotations

from .openai_compatible_test_support import (
    OPENAI_URL,
    SAMPLE_MESSAGES,
    SUCCESS_RESPONSE,
    Any,
    AsyncMock,
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    httpx,
    json,
    logging,
    patch,
    pytest,
    respx,
)
from .openai_compatible_test_support import openai_adapter as openai_adapter
from .openai_compatible_test_support import openrouter_adapter as openrouter_adapter


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
            pytest.raises(ProviderTimeoutError),
        ):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_connect_error_raises_network_error(self, openai_adapter):
        """Connection failures raise NetworkError."""
        # Arrange
        respx.post(OPENAI_URL).mock(side_effect=httpx.ConnectError("connection failed"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(NetworkError, match="connection failed"),
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

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_read_error_raises_network_error(self, openai_adapter):
        """A non-streaming read failure (httpx.ReadError) is wrapped as NetworkError."""

        # Arrange
        respx.post(OPENAI_URL).mock(side_effect=httpx.ReadError("connection reset"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(NetworkError, match="connection reset"),
        ):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_read_error_is_retried(self, openai_adapter):
        """A transient ReadError is retried; a subsequent success returns the response."""

        # Arrange
        route = respx.post(OPENAI_URL).mock(
            side_effect=[
                httpx.ReadError("connection reset"),
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
    async def test_send_remote_protocol_error_raises_network_error(self, openai_adapter):
        """A non-streaming RemoteProtocolError is wrapped as NetworkError."""

        # Arrange
        respx.post(OPENAI_URL).mock(side_effect=httpx.RemoteProtocolError("server disconnected"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(NetworkError, match="server disconnected"),
        ):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_malformed_json_raises_non_retryable_provider_error(self, openai_adapter):
        """A 2xx response with unparseable JSON raises a non-retryable ProviderError."""

        # Arrange
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, text="not-valid-json{"))

        # Act / Assert
        with pytest.raises(ProviderError) as exc_info:
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        assert exc_info.value.retryable is False
        assert "malformed JSON" in str(exc_info.value)


# ---------------------------------------------------------------------------
# send() — retry behaviour
# ---------------------------------------------------------------------------


class TestSendRetry:
    """Verify that send() retries on retryable errors and not on auth errors."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_rejected_temperature_retries_once_without_it(
        self, openai_adapter, caplog: Any
    ):
        """A 400 blaming temperature strips it and retries once; the default never refills."""
        # Arrange — the test config carries a provider default temperature 0.7,
        # so a rebuild would put the key back; the retry must use the stripped payload.
        route = respx.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(400, text="Unsupported parameter: 'temperature'"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with caplog.at_level(logging.WARNING, logger=_OPENAI_COMPATIBLE_LOGGER):
            result = await openai_adapter.send(
                SAMPLE_MESSAGES, model_id="gpt-5.2", temperature=0.2
            )

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2
        first_body = json.loads(route.calls[0].request.content)
        second_body = json.loads(route.calls[1].request.content)
        assert first_body["temperature"] == 0.2
        assert "temperature" not in second_body
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING and "temperature" in record.getMessage()
        ]
        assert len(warnings) == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_rejection_of_unsent_sampling_parameter_still_raises(
        self, openai_adapter
    ):
        """A rejection naming a parameter the payload never carried is fatal."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(400, text="Unsupported parameter: 'top_k'")
        )

        # Act / Assert
        with pytest.raises(ProviderError) as exc_info:
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        assert exc_info.value.retryable is False
        assert route.call_count == 1

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
# send() — reasoning observability signals
# ---------------------------------------------------------------------------


_OPENAI_COMPATIBLE_LOGGER = "vbot.providers.openai_compatible"

# A successful response whose usage reports the model did no reasoning.
RESPONSE_WITH_ZERO_REASONING_TOKENS = {
    "id": "chatcmpl-zero",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "completion_tokens_details": {"reasoning_tokens": 0},
    },
}

RESPONSE_WITH_REASONING_TOKENS = {
    "id": "chatcmpl-think",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 25,
        "total_tokens": 35,
        "completion_tokens_details": {"reasoning_tokens": 20},
    },
}


class TestSendReasoningObservability:
    """send() surfaces the two reasoning feedback signals without changing behavior."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_400_naming_effort_warns_and_still_raises(
        self, openai_adapter, caplog: Any
    ) -> None:
        """A 400 naming a rejected effort warns and still raises the same fatal error."""
        # Arrange
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(400, text="invalid value for 'reasoning_effort': 'max'")
        )

        # Act / Assert — classification is unchanged: fatal, non-retryable.
        with (
            caplog.at_level(logging.WARNING, logger=_OPENAI_COMPATIBLE_LOGGER),
            pytest.raises(ProviderError) as exc_info,
        ):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", thinking_effort="max")

        assert exc_info.value.retryable is False
        warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "gpt-5.2" in message
        assert "max" in message

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_400_unrelated_detail_does_not_warn(
        self, openai_adapter, caplog: Any
    ) -> None:
        """A 400 that does not name an effort raises but emits no effort warning."""
        # Arrange
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(400, text="context length exceeded")
        )

        # Act / Assert
        with (
            caplog.at_level(logging.WARNING, logger=_OPENAI_COMPATIBLE_LOGGER),
            pytest.raises(ProviderError),
        ):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", thinking_effort="max")

        assert [record for record in caplog.records if record.levelno == logging.WARNING] == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_effort_with_zero_reasoning_tokens_warns(
        self, openai_adapter, caplog: Any
    ) -> None:
        """A non-none effort that returns 0 reasoning tokens emits a structured warning."""
        # Arrange
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(200, json=RESPONSE_WITH_ZERO_REASONING_TOKENS)
        )

        # Act
        with caplog.at_level(logging.WARNING, logger=_OPENAI_COMPATIBLE_LOGGER):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", thinking_effort="high")

        # Assert
        warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "gpt-5.2" in message
        assert "high" in message

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_effort_with_reasoning_tokens_does_not_warn(
        self, openai_adapter, caplog: Any
    ) -> None:
        """A non-none effort with non-zero reasoning tokens emits no warning."""
        # Arrange
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(200, json=RESPONSE_WITH_REASONING_TOKENS)
        )

        # Act
        with caplog.at_level(logging.WARNING, logger=_OPENAI_COMPATIBLE_LOGGER):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", thinking_effort="high")

        # Assert
        assert [record for record in caplog.records if record.levelno == logging.WARNING] == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_none_effort_with_zero_reasoning_tokens_does_not_warn(
        self, openai_adapter, caplog: Any
    ) -> None:
        """Effort 'none' that returns 0 reasoning tokens is expected, not a swallowed effort."""
        # Arrange
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(200, json=RESPONSE_WITH_ZERO_REASONING_TOKENS)
        )

        # Act
        with caplog.at_level(logging.WARNING, logger=_OPENAI_COMPATIBLE_LOGGER):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", thinking_effort="none")

        # Assert
        assert [record for record in caplog.records if record.levelno == logging.WARNING] == []
