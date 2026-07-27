"""OpenAI-compatible streaming, stream usage, connection retries, and lifecycle."""

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


# ---------------------------------------------------------------------------
# send() — provider config integration
# ---------------------------------------------------------------------------


class TestStreamSSE:
    """Verify that stream() correctly parses SSE event chunks."""

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
        with pytest.raises(NetworkError, match=r"Stream ended without \[DONE\] marker"):
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
        with pytest.raises(ProviderError, match="Provider stream error: quota exceeded"):
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
        with pytest.raises(ProviderError, match="malformed JSON") as exc_info:
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass
        assert exc_info.value.retryable is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_reasoning_deltas_and_opaque_metadata(self, openai_adapter):
        """Reasoning text streams visibly while recognized metadata stays opaque."""
        # Arrange
        reasoning_details = [{"type": "reasoning.text", "text": "opaque"}]
        chunk = {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "delta": {
                        "reasoning_content": "Think",
                        "encrypted_content": "secret",
                        "reasoning_details": reasoning_details,
                        "unknown_provider_field": "ignored",
                    }
                }
            ],
        }
        sse_body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
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
            {"type": "reasoning_delta", "text": "Think"},
            {
                "type": "reasoning_meta",
                "reasoning_meta": {
                    "encrypted_content": "secret",
                    "reasoning_details": reasoning_details,
                },
            },
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_index_keyed_tool_call_deltas_with_stable_ids(
        self,
        openai_adapter,
    ):
        """Tool calls are normalized by index and get stable IDs when providers omit IDs."""
        # Arrange
        first_chunk = {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"city"'},
                            }
                        ]
                    }
                }
            ],
        }
        second_chunk = {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": ':"Berlin"}'},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        sse_body = (
            f"data: {json.dumps(first_chunk)}\n\n"
            f"data: {json.dumps(second_chunk)}\n\n"
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
            {
                "type": "tool_call_delta",
                "id": "tool_call_0",
                "name_delta": "get_weather",
                "arguments_delta": '{"city"',
            },
            {
                "type": "tool_call_delta",
                "id": "tool_call_0",
                "name_delta": "",
                "arguments_delta": ':"Berlin"}',
            },
            {"type": "finish", "reason": "tool_calls"},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_preserves_provider_tool_call_ids(self, openai_adapter):
        """Provider-supplied tool call IDs are reused for later index-only fragments."""
        # Arrange
        first_chunk = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_provider",
                                "function": {"name": "read_file"},
                            }
                        ]
                    }
                }
            ]
        }
        second_chunk = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 1, "function": {"arguments": '{"path":"README.md"}'}}
                        ]
                    }
                }
            ]
        }
        sse_body = (
            f"data: {json.dumps(first_chunk)}\n\n"
            f"data: {json.dumps(second_chunk)}\n\n"
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
            {
                "type": "tool_call_delta",
                "id": "call_provider",
                "name_delta": "read_file",
                "arguments_delta": "",
            },
            {
                "type": "tool_call_delta",
                "id": "call_provider",
                "name_delta": "",
                "arguments_delta": '{"path":"README.md"}',
            },
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_normalizes_unknown_finish_reason_from_pending_tool_calls(
        self,
        openai_adapter,
    ):
        """Unknown finish reasons become tool_calls when a tool call was seen."""
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
        assert chunks[-1] == {"type": "finish", "reason": "tool_calls"}

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
            pytest.raises(ProviderTimeoutError, match="timed out"),
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
            pytest.raises(NetworkError, match="Connection failed: connection failed"),
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
            pytest.raises(NetworkError, match="Stream read failed: connection reset"),
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
            pytest.raises(ProviderTimeoutError, match="stream timed out"),
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
            pytest.raises(NetworkError, match="Stream read failed: server disconnected"),
        ):
            async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
                pass


# ---------------------------------------------------------------------------
# stream() — usage delta
# ---------------------------------------------------------------------------


class TestStreamUsageDelta:
    """Verify that stream() yields usage deltas from streaming chunks with usage data."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_usage_delta_from_final_chunk(self, openai_adapter):
        """A streaming chunk with a usage object containing prompt_tokens yields a usage delta."""
        # Arrange — typical OpenAI final chunk with stream_options.include_usage
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[{"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":42,"completion_tokens":13,"total_tokens":55}}\n\n'
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
            {"type": "content_delta", "text": "Hi"},
            {"type": "finish", "reason": "stop"},
            {"type": "usage", "input_tokens": 42, "output_tokens": 13},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_usage_delta_with_zero_completion_tokens(self, openai_adapter):
        """Usage with prompt_tokens but no completion_tokens defaults output_tokens to 0."""
        # Arrange
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[],"usage":{"prompt_tokens":100}}\n\n'
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
            {"type": "content_delta", "text": "Hi"},
            {"type": "usage", "input_tokens": 100, "output_tokens": 0},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_usage_delta_includes_cache_read_tokens(self, openai_adapter):
        """A final chunk with prompt_tokens_details.cached_tokens yields cache_read_tokens."""
        # Arrange
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[],'
            '"usage":{"prompt_tokens":42,"completion_tokens":13,'
            '"prompt_tokens_details":{"cached_tokens":30}}}\n\n'
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
            {"type": "content_delta", "text": "Hi"},
            {
                "type": "usage",
                "input_tokens": 42,
                "output_tokens": 13,
                "cache_read_tokens": 30,
            },
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_no_usage_delta_when_usage_absent(self, openai_adapter):
        """Chunks without a usage object do not yield usage deltas."""
        # Arrange — standard stream without stream_options.include_usage
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
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
        assert all(c["type"] != "usage" for c in chunks)

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_no_usage_delta_when_usage_is_null(self, openai_adapter):
        """A chunk with usage: null does not yield a usage delta."""
        # Arrange — OpenAI sometimes sends usage: null when stream_options is not set
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
            'data: {"id":"chatcmpl-1",'
            '"choices":[{"delta":{},"finish_reason":"stop"}],"usage":null}\n\n'
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
        assert all(c["type"] != "usage" for c in chunks)

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_no_usage_delta_when_prompt_tokens_is_null(self, openai_adapter):
        """A chunk with usage where prompt_tokens is null does not yield a usage delta."""
        # Arrange
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[],'
            '"usage":{"prompt_tokens":null,"completion_tokens":5}}\n\n'
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
        assert all(c["type"] != "usage" for c in chunks)

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_no_usage_delta_when_prompt_tokens_missing(self, openai_adapter):
        """A chunk with usage but no prompt_tokens field does not yield a usage delta."""
        # Arrange
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[],'
            '"usage":{"completion_tokens":5}}\n\n'
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
        assert all(c["type"] != "usage" for c in chunks)

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_usage_delta_with_zero_tokens(self, openai_adapter):
        """Usage with both prompt_tokens=0 and completion_tokens=0 is still emitted."""
        # Arrange — legitimate zero-token usage
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":""}}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[{"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":0,"completion_tokens":0}}\n\n'
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
        usage_deltas = [c for c in chunks if c["type"] == "usage"]
        assert len(usage_deltas) == 1
        assert usage_deltas[0] == {"type": "usage", "input_tokens": 0, "output_tokens": 0}

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_no_usage_delta_when_usage_is_wrong_type(self, openai_adapter):
        """A chunk with usage as a non-dict type (e.g. a string) does not yield a usage delta."""
        # Arrange
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[{"delta":{},"finish_reason":"stop"}],'
            '"usage":"not-a-dict"}\n\n'
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
        assert all(c["type"] != "usage" for c in chunks)


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
