"""Anthropic streaming transport and SSE protocol tests."""

from __future__ import annotations

from .anthropic_test_support import (
    ANTHROPIC_CONFIG,
    ANTHROPIC_URL,
    CUSTOM_URL,
    SAMPLE_MESSAGES,
    SAMPLE_MESSAGES_WITH_SYSTEM,
    AnthropicAdapter,
    AsyncMock,
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
    _strip_cache_control,
    httpx,
    json,
    patch,
    pytest,
    respx,
)
from .anthropic_test_support import anthropic_adapter as anthropic_adapter
from .anthropic_test_support import custom_adapter as custom_adapter


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
        token_getter = _RotatingTokenGetter(["stale-token", "fresh-token"])
        adapter = AnthropicAdapter(ANTHROPIC_CONFIG, token_getter)
        sse_body = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.Response(503, text="Service Unavailable"),
                httpx.Response(200, text=sse_body, headers={"content-type": "text/event-stream"}),
            ]
        )

        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            async for _ in adapter.stream(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"):
                pass

        assert route.call_count == 2
        assert route.calls[0].request.headers.get("x-api-key") == "stale-token"
        assert route.calls[1].request.headers.get("x-api-key") == "fresh-token"


# ---------------------------------------------------------------------------
# stream() — SSE parsing
# ---------------------------------------------------------------------------


class TestStreamSSE:
    """Verify that stream() correctly parses Anthropic SSE event chunks."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_normalized_content_and_finish_deltas(self, anthropic_adapter):
        """stream() parses Anthropic SSE lines into normalized content and finish deltas."""
        # Arrange
        sse_body = (
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"msg_01"}}\n'
            "\n"
            "event: content_block_start\n"
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text","text":""}}\n'
            "\n"
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"Hello"}}\n'
            "\n"
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":" world"}}\n'
            "\n"
            "event: content_block_stop\n"
            'data: {"type":"content_block_stop","index":0}\n'
            "\n"
            "event: message_delta\n"
            'data: {"type":"message_delta",'
            '"delta":{"stop_reason":"end_turn"}}\n'
            "\n"
            "event: message_stop\n"
            'data: {"type":"message_stop"}\n'
            "\n"
        )
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        chunks = []
        async for chunk in anthropic_adapter.stream(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
        ):
            chunks.append(chunk)

        # Assert — 7 chunks: message_start, content_block_start,
        # 2x content_block_delta, content_block_stop, message_delta,
        # message_stop; only visible deltas and finish are yielded.
        assert chunks == [
            {"type": "content_delta", "text": "Hello"},
            {"type": "content_delta", "text": " world"},
            {"type": "finish", "reason": "stop"},
        ]
        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_preserves_max_tokens_as_output_truncation(self, anthropic_adapter):
        sse_body = (
            "event: message_delta\n"
            'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}\n'
            "\n"
            "event: message_stop\n"
            'data: {"type":"message_stop"}\n'
            "\n"
        )
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        chunks = [
            chunk
            async for chunk in anthropic_adapter.stream(
                SAMPLE_MESSAGES,
                model_id="claude-sonnet-4-20250219",
            )
        ]

        assert chunks == [{"type": "finish", "reason": "output_truncated"}]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_accepts_multiline_sse_data_frames(self, anthropic_adapter):
        """SSE data fields may be split across multiple data lines."""
        # Arrange
        sse_body = (
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,\n'
            'data: "delta":{"type":"text_delta","text":"Hello"}}\n'
            "\n"
            "event: message_delta\n"
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n'
            "\n"
            "event: message_stop\n"
            'data: {"type":"message_stop"}\n'
            "\n"
        )
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        chunks = []
        async for chunk in anthropic_adapter.stream(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
        ):
            chunks.append(chunk)

        # Assert
        assert chunks == [
            {"type": "content_delta", "text": "Hello"},
            {"type": "finish", "reason": "stop"},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_raises_provider_error_on_malformed_sse_json(self, anthropic_adapter):
        """Malformed SSE JSON is classified as a non-retryable provider error."""
        # Arrange
        sse_body = 'event: content_block_delta\ndata: {"type":\n\n'
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act / Assert
        with pytest.raises(ProviderError) as exc_info:
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            ):
                pass
        assert exc_info.value.retryable is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_reasoning_deltas_and_opaque_metadata(self, anthropic_adapter):
        """Thinking text streams visibly while supported thinking metadata stays opaque."""
        # Arrange
        sse_body = (
            "event: content_block_start\n"
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"thinking","thinking":""}}\n'
            "\n"
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"thinking_delta","thinking":"Need"}}\n'
            "\n"
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"thinking_delta","thinking":" weather."}}\n'
            "\n"
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"signature_delta","signature":"opaque-signature"}}\n'
            "\n"
            "event: content_block_stop\n"
            'data: {"type":"content_block_stop","index":0}\n'
            "\n"
            "event: message_stop\n"
            'data: {"type":"message_stop"}\n'
            "\n"
        )
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        chunks = []
        async for chunk in anthropic_adapter.stream(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
        ):
            chunks.append(chunk)

        # Assert
        assert chunks == [
            {"type": "reasoning_delta", "text": "Need"},
            {"type": "reasoning_delta", "text": " weather."},
            {
                "type": "reasoning_meta",
                "reasoning_meta": {
                    "content_blocks": [
                        {
                            "type": "thinking",
                            "thinking": "Need weather.",
                            "signature": "opaque-signature",
                        }
                    ]
                },
            },
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_tool_call_input_fragments_and_finish(self, anthropic_adapter):
        """Tool-use blocks stream name and input fragments as normalized tool deltas."""
        # Arrange
        sse_body = (
            "event: content_block_start\n"
            'data: {"type":"content_block_start","index":2,'
            '"content_block":{"type":"tool_use","id":"toolu_abc","name":"get_weather",'
            '"input":{}}}\n'
            "\n"
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":2,'
            '"delta":{"type":"input_json_delta","partial_json":"{\\"city\\""}}\n'
            "\n"
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":2,'
            '"delta":{"type":"input_json_delta","partial_json":":\\"Berlin\\"}"}}\n'
            "\n"
            "event: message_delta\n"
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n'
            "\n"
            "event: message_stop\n"
            'data: {"type":"message_stop"}\n'
            "\n"
        )
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        chunks = []
        async for chunk in anthropic_adapter.stream(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
        ):
            chunks.append(chunk)

        # Assert
        assert chunks == [
            {
                "type": "tool_call_delta",
                "id": "toolu_abc",
                "name_delta": "get_weather",
                "arguments_delta": "",
            },
            {
                "type": "tool_call_delta",
                "id": "toolu_abc",
                "name_delta": "",
                "arguments_delta": '{"city"',
            },
            {
                "type": "tool_call_delta",
                "id": "toolu_abc",
                "name_delta": "",
                "arguments_delta": ':"Berlin"}',
            },
            {"type": "finish", "reason": "tool_calls"},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_preserves_redacted_thinking_metadata(self, anthropic_adapter):
        """Redacted-thinking blocks are preserved as opaque metadata without visible deltas."""
        # Arrange
        sse_body = (
            "event: content_block_start\n"
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"redacted_thinking","data":"opaque-redacted"}}\n'
            "\n"
            "event: content_block_stop\n"
            'data: {"type":"content_block_stop","index":0}\n'
            "\n"
            "event: message_stop\n"
            'data: {"type":"message_stop"}\n'
            "\n"
        )
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        chunks = []
        async for chunk in anthropic_adapter.stream(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
        ):
            chunks.append(chunk)

        # Assert
        assert chunks == [
            {
                "type": "reasoning_meta",
                "reasoning_meta": {
                    "content_blocks": [{"type": "redacted_thinking", "data": "opaque-redacted"}]
                },
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_ignores_ping_and_message_bookkeeping_events(self, anthropic_adapter):
        """Ping, message_start, and message_stop do not leak raw provider events."""
        # Arrange
        sse_body = (
            "event: ping\n"
            'data: {"type":"ping"}\n'
            "\n"
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"msg_01"}}\n'
            "\n"
            "event: message_stop\n"
            'data: {"type":"message_stop"}\n'
            "\n"
        )
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        chunks = []
        async for chunk in anthropic_adapter.stream(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
        ):
            chunks.append(chunk)

        # Assert
        assert chunks == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_includes_stream_true_in_payload(self, anthropic_adapter):
        """stream() sends stream=true in the request payload."""
        # Arrange
        sse_body = (
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"msg_01"}}\n'
            "\n"
            "event: message_stop\n"
            'data: {"type":"message_stop"}\n'
            "\n"
        )
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        async for _ in anthropic_adapter.stream(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
        ):
            pass

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["stream"] is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_ignores_comment_lines(self, anthropic_adapter):
        """stream() skips comment lines, empty lines, and raw bookkeeping events."""
        # Arrange
        sse_body = (
            ": this is a comment\n"
            "\n"
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"msg_01"}}\n'
            "\n"
            "event: message_stop\n"
            'data: {"type":"message_stop"}\n'
            "\n"
        )
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        chunks = []
        async for chunk in anthropic_adapter.stream(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
        ):
            chunks.append(chunk)

        # Assert
        assert chunks == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_extracts_system_message(self, anthropic_adapter):
        """stream() extracts system messages into the system field."""
        # Arrange
        sse_body = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        async for _ in anthropic_adapter.stream(
            SAMPLE_MESSAGES_WITH_SYSTEM,
            model_id="claude-sonnet-4-20250219",
        ):
            pass

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["system"] == [{"type": "text", "text": "You are a helpful assistant."}]
        for msg in request_body["messages"]:
            assert msg["role"] != "system"

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_raises_network_error_on_eof_without_message_stop(self, anthropic_adapter):
        """stream() raises NetworkError when the stream ends without message_stop."""
        # Arrange
        sse_body = (
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"msg_01"}}\n'
            "\n"
            "event: content_block_start\n"
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text","text":""}}\n'
            "\n"
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"Hello"}}\n'
            "\n"
            "event: content_block_stop\n"
            'data: {"type":"content_block_stop","index":0}\n'
            "\n"
            "event: message_delta\n"
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n'
            "\n"
        )
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act / Assert
        with pytest.raises(NetworkError):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES,
                model_id="claude-sonnet-4-20250219",
            ):
                pass

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_raises_provider_error_on_in_band_error_event(self, anthropic_adapter):
        """stream() raises ProviderError when an in-band Anthropic error event arrives."""
        # Arrange
        sse_body = (
            "event: error\n"
            'data: {"type":"error","error":{"type":"invalid_request_error","message":"bad"}}\n'
            "\n"
        )
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act / Assert
        with pytest.raises(ProviderError, match="bad"):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES,
                model_id="claude-sonnet-4-20250219",
            ):
                pass

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_401_raises_provider_auth_error(self, anthropic_adapter):
        """stream() raises ProviderAuthError on 401 — no retry."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )

        # Act / Assert
        with pytest.raises(ProviderAuthError):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            ):
                pass

        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_applies_extra_headers(self, custom_adapter):
        """stream() includes extra_headers from provider config."""
        # Arrange
        sse_body = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        route = respx.post(CUSTOM_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        async for _ in custom_adapter.stream(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"):
            pass

        # Assert
        request = route.calls.last.request
        assert request.headers.get("x-custom-header") == "custom-value"

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_timeout_raises_provider_timeout_error(self, anthropic_adapter):
        """stream() raises ProviderTimeoutError on connection timeout."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(side_effect=httpx.TimeoutException("timed out"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderTimeoutError),
        ):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            ):
                pass

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_connect_error_raises_network_error(self, anthropic_adapter):
        """stream() raises NetworkError on connection failures."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(side_effect=httpx.ConnectError("connection failed"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(NetworkError, match="connection failed"),
        ):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            ):
                pass

    @pytest.mark.asyncio
    async def test_stream_read_error_raises_network_error(self, anthropic_adapter):
        """stream() wraps mid-stream httpx.ReadError as NetworkError."""

        request = httpx.Request("POST", ANTHROPIC_URL)

        class _BrokenLineIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise httpx.ReadError("socket closed", request=request)

        class _BrokenStreamResponse:
            status_code = 200

            def __init__(self) -> None:
                self.closed = False

            def aiter_lines(self):
                return _BrokenLineIterator()

            async def aclose(self) -> None:
                self.closed = True

        broken_response = _BrokenStreamResponse()
        with (
            patch.object(
                anthropic_adapter._client,
                "send",
                new=AsyncMock(return_value=broken_response),
            ),
            pytest.raises(NetworkError, match="socket closed"),
        ):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES,
                model_id="claude-sonnet-4-20250219",
            ):
                pass

        assert broken_response.closed is True

    @pytest.mark.asyncio
    async def test_stream_raises_provider_timeout_error_on_mid_stream_timeout(
        self,
        anthropic_adapter,
    ):
        """stream() wraps mid-stream httpx.TimeoutException as ProviderTimeoutError."""

        request = httpx.Request("POST", ANTHROPIC_URL)

        class _BrokenTimeoutLineIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise httpx.TimeoutException("timed out", request=request)

        class _BrokenTimeoutStreamResponse:
            status_code = 200

            def __init__(self) -> None:
                self.closed = False

            def aiter_lines(self):
                return _BrokenTimeoutLineIterator()

            async def aclose(self) -> None:
                self.closed = True

        broken_response = _BrokenTimeoutStreamResponse()
        with (
            patch.object(
                anthropic_adapter._client,
                "send",
                new=AsyncMock(return_value=broken_response),
            ),
            pytest.raises(ProviderTimeoutError),
        ):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES,
                model_id="claude-sonnet-4-20250219",
            ):
                pass

        assert broken_response.closed is True

    @pytest.mark.asyncio
    async def test_stream_raises_network_error_on_mid_stream_remote_protocol_error(
        self,
        anthropic_adapter,
    ):
        """stream() wraps mid-stream httpx.RemoteProtocolError as NetworkError (h11 disconnect)."""

        request = httpx.Request("POST", ANTHROPIC_URL)

        class _BrokenProtocolLineIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise httpx.RemoteProtocolError("server disconnected", request=request)

        class _BrokenProtocolStreamResponse:
            status_code = 200

            def __init__(self) -> None:
                self.closed = False

            def aiter_lines(self):
                return _BrokenProtocolLineIterator()

            async def aclose(self) -> None:
                self.closed = True

        broken_response = _BrokenProtocolStreamResponse()
        with (
            patch.object(
                anthropic_adapter._client,
                "send",
                new=AsyncMock(return_value=broken_response),
            ),
            pytest.raises(NetworkError, match="server disconnected"),
        ):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES,
                model_id="claude-sonnet-4-20250219",
            ):
                pass

        assert broken_response.closed is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_retries_on_429_then_succeeds(self, anthropic_adapter):
        """stream() retries on 429 and succeeds on next attempt."""
        # Arrange
        sse_body = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.Response(429, text="Rate limited"),
                httpx.Response(
                    200,
                    text=sse_body,
                    headers={"content-type": "text/event-stream"},
                ),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            chunks = []
            async for chunk in anthropic_adapter.stream(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            ):
                chunks.append(chunk)

        # Assert
        assert route.call_count == 2
        assert chunks == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_applies_anthropic_version_header(self, anthropic_adapter):
        """stream() sends the anthropic-version header."""
        # Arrange
        sse_body = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        async for _ in anthropic_adapter.stream(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
        ):
            pass

        # Assert
        version_header = route.calls.last.request.headers.get("anthropic-version")
        assert version_header == "2023-06-01"

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_thinking_kwargs_in_payload(self, anthropic_adapter):
        """stream() passes through thinking and output_config kwargs."""
        # Arrange
        sse_body = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )
        thinking = {"type": "adaptive"}
        output_config = {"effort": "high"}

        # Act
        async for _ in anthropic_adapter.stream(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            thinking=thinking,
            output_config=output_config,
        ):
            pass

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["thinking"] == thinking
        assert request_body["output_config"] == output_config


# ---------------------------------------------------------------------------
# stream() — usage delta emission
# ---------------------------------------------------------------------------
