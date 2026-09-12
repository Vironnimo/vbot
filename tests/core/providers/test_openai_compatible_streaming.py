"""Openai compatible streaming: transport behavior."""

from __future__ import annotations

from .openai_compatible_test_support import (
    API_KEY,
    OPENAI_CONFIG,
    OPENAI_URL,
    OPENROUTER_URL,
    SAMPLE_MESSAGES,
    AsyncMock,
    NetworkError,
    OpenAICompatibleAdapter,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    httpx,
    json,
    patch,
    pytest,
    respx,
)
from .openai_compatible_test_support import openai_adapter as openai_adapter
from .openai_compatible_test_support import openrouter_adapter as openrouter_adapter


class _RotatingTokenGetter:
    """Async token getter that yields a fresh token on each call."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self.calls = 0

    async def __call__(self) -> str:
        token = self._tokens[min(self.calls, len(self._tokens) - 1)]
        self.calls += 1
        return token


class TestStreamConnectRetryRebuildsHeaders:
    """stream() must re-consult the token getter on each connect attempt."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_rebuilds_auth_header_per_connect_attempt(self) -> None:
        """A retried stream connect uses a token refreshed during the backoff."""
        # Arrange — token rotates between the failed attempt and the retry,
        # mimicking an OAuth refresh inside the 503 backoff window.
        token_getter = _RotatingTokenGetter(["stale-token", "fresh-token"])
        adapter = OpenAICompatibleAdapter(OPENAI_CONFIG, token_getter)
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
        )
        route = respx.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(503, text="Service Unavailable"),
                httpx.Response(200, text=sse_body, headers={"content-type": "text/event-stream"}),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            async for _ in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass

        # Assert — first attempt used the stale token, retry used the fresh one.
        assert route.call_count == 2
        assert route.calls[0].request.headers.get("authorization") == "Bearer stale-token"
        assert route.calls[1].request.headers.get("authorization") == "Bearer fresh-token"

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_rejected_temperature_retries_once_without_it(self) -> None:
        """A stream-connect 400 blaming temperature strips it and reconnects once."""
        # Arrange
        adapter = OpenAICompatibleAdapter(OPENAI_CONFIG, _RotatingTokenGetter(["key"]))
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
        )
        route = respx.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(400, text="Unsupported parameter: 'temperature'"),
                httpx.Response(200, text=sse_body, headers={"content-type": "text/event-stream"}),
            ]
        )

        # Act
        async for _ in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2", temperature=0.2):
            pass

        # Assert
        assert route.call_count == 2
        first_body = json.loads(route.calls[0].request.content)
        second_body = json.loads(route.calls[1].request.content)
        assert first_body["temperature"] == 0.2
        assert "temperature" not in second_body


class TestStreamSSE:
    "Verify that stream() correctly parses SSE event chunks."

    # send() — provider config integration
    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_normalized_content_and_finish_deltas(self, openai_adapter):
        """stream() parses SSE data lines into normalized content and finish deltas."""
        # Arrange
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"}}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":" world"}}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
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
        assert chunks == [
            {"type": "content_delta", "text": "Hello"},
            {"type": "content_delta", "text": " world"},
            {"type": "finish", "reason": "stop"},
        ]
        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_exposes_sse_comments_as_transport_heartbeats(self, openai_adapter):
        """Gateway pings preserve liveness without claiming Model progress."""
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Writing"}}]}\n\n'
            ": ping - 2026-07-27T10:00:00Z\n\n"
            ": ping - 2026-07-27T10:00:15Z\n\n"
            'data: {"id":"chatcmpl-1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        chunks = [
            chunk async for chunk in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2")
        ]

        assert chunks == [
            {"type": "content_delta", "text": "Writing"},
            {"type": "heartbeat"},
            {"type": "heartbeat"},
            {"type": "finish", "reason": "stop"},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_raises_network_error_on_eof_without_done_marker(self, openai_adapter):
        """stream() raises NetworkError when SSE ends without the [DONE] marker."""
        # Arrange
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"}}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        )
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        # Act / Assert
        with pytest.raises(NetworkError):
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_raises_provider_error_on_in_band_error_chunk(self, openai_adapter):
        """stream() raises ProviderError when the provider sends an in-band error chunk."""
        # Arrange
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"}}]}\n\n'
            'data: {"error":{"message":"quota exceeded"}}\n\n'
            "data: [DONE]\n\n"
        )
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        # Act / Assert
        with pytest.raises(ProviderError):
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_classifies_structured_in_band_error_chunk(self, openai_adapter):
        """A structured in-band error chunk maps into the shared error taxonomy."""

        # Arrange
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"}}]}\n\n'
            "data: "
            + json.dumps(
                {
                    "error": {
                        "code": 429,
                        "message": "Rate limit exceeded",
                        "metadata": {"error_type": "rate_limit_exceeded"},
                    }
                }
            )
            + "\n\n"
            "data: [DONE]\n\n"
        )
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        # Act / Assert
        with pytest.raises(ProviderRateLimitError):
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_accepts_multiline_sse_data_frames(self, openai_adapter):
        """SSE data fields may be split across multiple data lines."""
        # Arrange
        sse_body = (
            'data: {"id":"chatcmpl-1",\n'
            'data: "choices":[{"delta":{"content":"Hello"}}]}\n\n'
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
        assert chunks == [{"type": "content_delta", "text": "Hello"}]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_raises_provider_error_on_malformed_sse_json(self, openai_adapter):
        """Malformed SSE JSON is classified as a non-retryable provider error."""
        # Arrange
        sse_body = 'data: {"id":\n\n'
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        # Act / Assert
        with pytest.raises(ProviderError) as exc_info:
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass
        assert exc_info.value.retryable is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_maps_unknown_finish_reason_to_unknown(
        self,
        openai_adapter,
    ):
        """Unknown finish reasons remain unsafe even when a Tool fragment was seen."""
        # Arrange
        sse_body = (
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"name":"search"}}]}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"provider_tool_stop"}]}\n\n'
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
        assert chunks[-1] == {"type": "finish", "reason": "unknown"}

    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("finish_reason", "expected_outcome"),
        [
            ("length", "output_truncated"),
            ("content_filter", "content_filtered"),
            ("network_error", "error"),
        ],
    )
    async def test_stream_preserves_unsafe_terminal_outcomes(
        self,
        openai_adapter,
        finish_reason,
        expected_outcome,
    ):
        sse_body = (
            f'data: {{"choices":[{{"delta":{{}},"finish_reason":"{finish_reason}"}}]}}\n\n'
            "data: [DONE]\n\n"
        )
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        chunks = [
            chunk async for chunk in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2")
        ]

        assert chunks == [{"type": "finish", "reason": expected_outcome}]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_stop_with_native_network_error_raises_network_error(
        self,
        openai_adapter,
    ):
        """A stop that conceals native_finish_reason=network_error is not complete."""
        chunk = {
            "id": "gen-1",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "", "role": "assistant"},
                    "finish_reason": "stop",
                    "native_finish_reason": "network_error",
                }
            ],
        }
        sse_body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        with pytest.raises(NetworkError, match="native_finish_reason=network_error"):
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_stop_with_native_server_error_raises_retryable_provider_error(
        self,
        openai_adapter,
    ):
        """A stop that conceals native_finish_reason=server_error is retryable."""
        chunk = {
            "choices": [
                {
                    "delta": {"content": ""},
                    "finish_reason": "stop",
                    "native_finish_reason": "server_error",
                }
            ],
        }
        sse_body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        with pytest.raises(ProviderError, match="native_finish_reason=server_error") as caught:
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass
        assert caught.value.retryable is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_includes_stream_true_and_usage_request_in_payload(self, openai_adapter):
        """stream() sends stream=true and requests usage in the payload."""
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
        assert request_body["stream_options"] == {"include_usage": True}

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_merges_usage_request_with_existing_stream_options(self, openai_adapter):
        """stream() preserves caller stream_options while requesting usage generically."""
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
        )
        route = respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        async for _ in openai_adapter.stream(
            SAMPLE_MESSAGES,
            model_id="gpt-5.2",
            stream_options={"foo": "bar", "include_usage": False},
        ):
            pass

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["stream_options"] == {"foo": "bar", "include_usage": True}

    @respx.mock
    @pytest.mark.asyncio
    async def test_openrouter_stream_requests_usage_in_payload(self, openrouter_adapter):
        """OpenRouter stream payload explicitly requests usage reporting."""
        # Arrange
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
        )
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        # Act
        async for _ in openrouter_adapter.stream(SAMPLE_MESSAGES, model_id="openai/gpt-5.2"):
            pass

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["stream"] is True
        assert request_body["stream_options"] == {"include_usage": True}

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_surfaces_comments_and_ignores_other_non_data_lines(self, openai_adapter):
        """SSE comments become heartbeats while unrelated lines stay ignored."""
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
        assert chunks == [
            {"type": "heartbeat"},
            {"type": "content_delta", "text": "A"},
        ]

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
            pytest.raises(ProviderTimeoutError),
        ):
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_connect_error_raises_network_error(self, openai_adapter):
        """stream() raises NetworkError on connection failures."""
        # Arrange
        respx.post(OPENAI_URL).mock(side_effect=httpx.ConnectError("connection failed"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(NetworkError, match="connection failed"),
        ):
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass

    @pytest.mark.asyncio
    async def test_stream_read_error_raises_network_error(self, openai_adapter):
        """stream() wraps mid-stream httpx.ReadError as NetworkError."""

        class _ReadErrorStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"id":"1","choices":[{"delta":{"content":"A"}}]}\n\n'
                raise httpx.ReadError("connection reset")

            async def aclose(self) -> None:
                pass

        with (
            patch.object(
                openai_adapter._client,
                "send",
                new=AsyncMock(
                    return_value=httpx.Response(
                        200,
                        stream=_ReadErrorStream(),
                        headers={"content-type": "text/event-stream"},
                    )
                ),
            ),
            pytest.raises(NetworkError, match="connection reset"),
        ):
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass

    @pytest.mark.asyncio
    async def test_stream_raises_provider_timeout_error_on_mid_stream_timeout(
        self,
        openai_adapter,
    ):
        """stream() wraps mid-stream httpx.TimeoutException as ProviderTimeoutError."""

        class _TimeoutStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"id":"1","choices":[{"delta":{"content":"A"}}]}\n\n'
                raise httpx.TimeoutException("stream timed out")

            async def aclose(self) -> None:
                pass

        with (
            patch.object(
                openai_adapter._client,
                "send",
                new=AsyncMock(
                    return_value=httpx.Response(
                        200,
                        stream=_TimeoutStream(),
                        headers={"content-type": "text/event-stream"},
                    )
                ),
            ),
            pytest.raises(ProviderTimeoutError),
        ):
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass

    @pytest.mark.asyncio
    async def test_stream_raises_network_error_on_mid_stream_remote_protocol_error(
        self,
        openai_adapter,
    ):
        """stream() wraps mid-stream httpx.RemoteProtocolError as NetworkError (h11 disconnect)."""

        class _ProtocolErrorStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"id":"1","choices":[{"delta":{"content":"A"}}]}\n\n'
                raise httpx.RemoteProtocolError("server disconnected")

            async def aclose(self) -> None:
                pass

        with (
            patch.object(
                openai_adapter._client,
                "send",
                new=AsyncMock(
                    return_value=httpx.Response(
                        200,
                        stream=_ProtocolErrorStream(),
                        headers={"content-type": "text/event-stream"},
                    )
                ),
            ),
            pytest.raises(NetworkError, match="server disconnected"),
        ):
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass


# Lifecycle: aclose() and async context manager
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
