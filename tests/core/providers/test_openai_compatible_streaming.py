"""OpenAI-compatible streaming, stream usage, connection retries, and lifecycle."""

from __future__ import annotations

from core.chat.streaming import StreamingAccumulator

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
    async def test_stream_preserves_separate_tool_reasoning_details_through_replay(
        self,
        openai_adapter,
    ):
        first_detail = {
            "type": "reasoning.encrypted",
            "id": "call_first",
            "data": "opaque-first",
        }
        second_detail = {
            "type": "reasoning.encrypted",
            "id": "call_second",
            "data": "opaque-second",
        }
        chunks = [
            {"choices": [{"delta": {"reasoning_details": [first_detail]}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_first",
                                    "function": {"name": "first", "arguments": "{}"},
                                },
                                {
                                    "index": 1,
                                    "id": "call_second",
                                    "function": {"name": "second", "arguments": "{}"},
                                },
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {"reasoning_details": [second_detail]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        sse_body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        sse_body += "data: [DONE]\n\n"
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        accumulator = StreamingAccumulator()
        async for delta in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
            accumulator.add_delta(delta)

        fields = accumulator.finalize_assistant_fields()
        assert fields.reasoning_meta == {
            "reasoning_details": [first_detail, second_detail],
        }
        assert [call["id"] for call in fields.tool_calls or []] == [
            "call_first",
            "call_second",
        ]

        payload = openai_adapter._build_payload(
            [
                {
                    "role": "assistant",
                    "content": fields.content,
                    "reasoning": fields.reasoning,
                    "reasoning_meta": fields.reasoning_meta,
                    "tool_calls": fields.tool_calls,
                }
            ],
            model_id="gpt-5.2",
        )
        assert payload["messages"][0]["reasoning_details"] == [
            first_detail,
            second_detail,
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_merges_consecutive_reasoning_text_delta_fragments(self, openai_adapter):
        """Per-delta reasoning.text fragments persist as one logical block."""
        # Arrange — gateways such as OpenRouter stream one tiny fragment per
        # delta; consecutive same-shape fragments are one reasoning block.
        fragment_shape = {"type": "reasoning.text", "format": "unknown", "index": 0}
        chunks = [
            {"choices": [{"delta": {"reasoning_details": [{**fragment_shape, "text": "Think"}]}}]},
            {"choices": [{"delta": {"reasoning_details": [{**fragment_shape, "text": "ing"}]}}]},
            {"choices": [{"delta": {"reasoning_details": [{**fragment_shape, "text": "."}]}}]},
        ]
        sse_body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        sse_body += "data: [DONE]\n\n"
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        # Act
        accumulator = StreamingAccumulator()
        async for delta in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
            accumulator.add_delta(delta)
        fields = accumulator.finalize_assistant_fields()

        # Assert
        assert fields.reasoning_meta == {
            "reasoning_details": [{**fragment_shape, "text": "Thinking."}]
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_keeps_reasoning_text_blocks_apart_on_shape_change(self, openai_adapter):
        """A changed index or oversized text starts a new detail item."""
        # Arrange — the oversized fragment shares shape with "first", so only
        # the delta-size guard prevents merging; "second" differs by index.
        big_text = "x" * (257)
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_details": [
                                {
                                    "type": "reasoning.text",
                                    "format": "unknown",
                                    "index": 0,
                                    "text": "first",
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_details": [
                                {
                                    "type": "reasoning.text",
                                    "format": "unknown",
                                    "index": 0,
                                    "text": big_text,
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_details": [
                                {
                                    "type": "reasoning.text",
                                    "format": "unknown",
                                    "index": 1,
                                    "text": "second",
                                }
                            ]
                        }
                    }
                ]
            },
        ]
        sse_body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        sse_body += "data: [DONE]\n\n"
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        # Act
        accumulator = StreamingAccumulator()
        async for delta in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
            accumulator.add_delta(delta)
        fields = accumulator.finalize_assistant_fields()

        # Assert
        details = fields.reasoning_meta["reasoning_details"]
        assert [item["text"] for item in details] == ["first", big_text, "second"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_index_keyed_tool_call_deltas_without_premature_ids(
        self,
        openai_adapter,
    ):
        """Tool calls keep the wire index while a missing Provider ID remains unknown."""
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
                "slot": 0,
                "name_delta": "get_weather",
                "arguments_delta": '{"city"',
            },
            {
                "type": "tool_call_delta",
                "slot": 0,
                "name_delta": "",
                "arguments_delta": ':"Berlin"}',
            },
            {"type": "finish", "reason": "tool_calls"},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_preserves_provider_tool_call_ids(self, openai_adapter):
        """Provider-supplied IDs attach to their stable index slot."""
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
                "slot": 1,
                "id": "call_provider",
                "name_delta": "read_file",
                "arguments_delta": "",
            },
            {
                "type": "tool_call_delta",
                "slot": 1,
                "name_delta": "",
                "arguments_delta": '{"path":"README.md"}',
            },
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_accepts_provider_tool_call_id_after_content_fragments(
        self,
        openai_adapter,
    ):
        """A late real ID remains attached to the original index slot."""
        sse_body = (
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"name":"search","arguments":"{\\"query\\":\\""}}]}}]}\n\n'
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"id":"call_provider_late","function":{"arguments":"Berlin\\"}"}}]},'
            '"finish_reason":"tool_calls"}]}\n\n'
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
            {
                "type": "tool_call_delta",
                "slot": 0,
                "name_delta": "search",
                "arguments_delta": '{"query":"',
            },
            {
                "type": "tool_call_delta",
                "slot": 0,
                "id": "call_provider_late",
                "name_delta": "",
                "arguments_delta": 'Berlin"}',
            },
            {"type": "finish", "reason": "tool_calls"},
        ]

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
    async def test_stream_usage_delta_omits_missing_completion_tokens(self, openai_adapter):
        """A missing completion counter remains absent instead of becoming a measured zero."""
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
            {"type": "usage", "input_tokens": 100},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_usage_delta_preserves_reported_token_details(self, openai_adapter):
        """The final Usage chunk retains cache and Reasoning output subsets."""
        # Arrange
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[],'
            '"usage":{"prompt_tokens":42,"completion_tokens":13,'
            '"prompt_tokens_details":{"cached_tokens":30,"cache_write_tokens":5},'
            '"completion_tokens_details":{"reasoning_tokens":8}}}\n\n'
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
                "cache_write_tokens": 5,
                "reasoning_tokens": 8,
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
    async def test_stream_usage_delta_keeps_completion_when_prompt_tokens_is_null(
        self, openai_adapter
    ):
        """A reported completion counter survives without an input counter."""
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
        assert chunks == [
            {"type": "content_delta", "text": "Hi"},
            {"type": "usage", "output_tokens": 5},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_usage_delta_when_prompt_tokens_missing(self, openai_adapter):
        """A reported completion counter survives without a prompt_tokens field."""
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
        assert chunks == [
            {"type": "content_delta", "text": "Hi"},
            {"type": "usage", "output_tokens": 5},
        ]

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


# ---------------------------------------------------------------------------
# Reused wire index: Ollama-compatible servers distinguish parallel Tool Calls
# by id while reusing one index for the whole batch.
# ---------------------------------------------------------------------------


class TestReusedToolCallIndexRedirect:
    """A same-index delta carrying a different id starts a fresh slot."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_same_index_different_ids_split_into_separate_slots(
        self,
        openai_adapter,
    ):
        """Two parallel calls reusing index 0 accumulate as two calls."""
        # Arrange
        first_call_chunk = {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "function": {"name": "get_weather", "arguments": '{"ci'},
                            }
                        ]
                    }
                }
            ],
        }
        second_call_chunk = {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_b",
                                "function": {"name": "get_time", "arguments": '{"tz'},
                            }
                        ]
                    }
                }
            ],
        }
        continuation_chunk = {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": ':"UTC"}'},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        sse_body = (
            f"data: {json.dumps(first_call_chunk)}\n\n"
            f"data: {json.dumps(second_call_chunk)}\n\n"
            f"data: {json.dumps(continuation_chunk)}\n\n"
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

        # Assert — the id-less continuation fragment stays on the redirected
        # slot of its call (call_b), not the raw index 0 of call_a.
        assert chunks == [
            {
                "type": "tool_call_delta",
                "slot": 0,
                "id": "call_a",
                "name_delta": "get_weather",
                "arguments_delta": '{"ci',
            },
            {
                "type": "tool_call_delta",
                "slot": 1,
                "id": "call_b",
                "name_delta": "get_time",
                "arguments_delta": '{"tz',
            },
            {
                "type": "tool_call_delta",
                "slot": 1,
                "name_delta": "",
                "arguments_delta": ':"UTC"}',
            },
            {"type": "finish", "reason": "tool_calls"},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_repeated_same_id_keeps_one_slot(
        self,
        openai_adapter,
    ):
        """A provider repeating the same id on every fragment stays on one slot."""
        # Arrange
        first_chunk = {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "function": {"name": "get_weather", "arguments": '{"ci'},
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
                                "id": "call_a",
                                "function": {"arguments": 'ty":"Berlin"}'},
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
        tool_call_deltas = [chunk for chunk in chunks if chunk["type"] == "tool_call_delta"]
        assert [delta["slot"] for delta in tool_call_deltas] == [0, 0]
