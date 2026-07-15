"""Anthropic streaming usage and adapter lifecycle tests."""

from __future__ import annotations

from .anthropic_test_support import (
    ANTHROPIC_CONFIG,
    ANTHROPIC_URL,
    API_KEY,
    SAMPLE_MESSAGES,
    AnthropicAdapter,
    httpx,
    pytest,
    respx,
)
from .anthropic_test_support import anthropic_adapter as anthropic_adapter


class TestStreamUsageDelta:
    """Verify that stream() yields usage deltas from Anthropic SSE events."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_usage_delta_with_both_tokens(self, anthropic_adapter):
        """stream() yields a usage delta when message_start provides input_tokens
        and message_delta provides output_tokens."""
        # Arrange — realistic SSE sequence with usage data in both events
        sse_body = (
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"msg_01","usage":{"input_tokens":25}}}\n'
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
            'data: {"type":"message_delta",'
            '"delta":{"stop_reason":"end_turn"},'
            '"usage":{"output_tokens":10}}\n'
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

        # Assert — usage delta appears with both token counts
        usage_deltas = [c for c in chunks if c.get("type") == "usage"]
        assert len(usage_deltas) == 1
        assert usage_deltas[0] == {
            "type": "usage",
            "input_tokens": 25,
            "output_tokens": 10,
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_usage_delta_folds_cache_tokens_from_message_start(
        self, anthropic_adapter
    ):
        """Cache tokens from message_start usage are folded into the usage delta."""
        # Arrange — message_start reports cache read/write alongside input_tokens
        sse_body = (
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"msg_01",'
            '"usage":{"input_tokens":25,"cache_read_input_tokens":1000,'
            '"cache_creation_input_tokens":200}}}\n'
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
            'data: {"type":"message_delta",'
            '"delta":{"stop_reason":"end_turn"},'
            '"usage":{"output_tokens":10}}\n'
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

        # Assert — input_tokens is the total prompt including cached tokens
        usage_deltas = [c for c in chunks if c.get("type") == "usage"]
        assert len(usage_deltas) == 1
        assert usage_deltas[0] == {
            "type": "usage",
            "input_tokens": 1225,
            "output_tokens": 10,
            "cache_read_tokens": 1000,
            "cache_write_tokens": 200,
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_usage_delta_ordering_with_finish(self, anthropic_adapter):
        """The usage delta comes after the finish delta from the same message_delta event."""
        # Arrange
        sse_body = (
            "event: message_start\n"
            'data: {"type":"message_start",'
            '"message":{"id":"msg_01","usage":{"input_tokens":100}}}\n'
            "\n"
            "event: content_block_start\n"
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text","text":""}}\n'
            "\n"
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"Hi"}}\n'
            "\n"
            "event: content_block_stop\n"
            'data: {"type":"content_block_stop","index":0}\n'
            "\n"
            "event: message_delta\n"
            'data: {"type":"message_delta",'
            '"delta":{"stop_reason":"end_turn"},'
            '"usage":{"output_tokens":50}}\n'
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

        # Assert — finish delta comes before usage delta from same message_delta event
        usage_idx = next(i for i, c in enumerate(chunks) if c.get("type") == "usage")
        finish_idx = next(i for i, c in enumerate(chunks) if c.get("type") == "finish")
        assert finish_idx < usage_idx

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_usage_delta_degrades_to_zero_input_without_start_usage(
        self, anthropic_adapter
    ):
        """stream() still yields a usage delta when message_start lacks input_tokens,
        degrading input to 0 rather than dropping the run's output accounting."""
        # Arrange — message_start without usage data
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
            'data: {"type":"message_delta",'
            '"delta":{"stop_reason":"end_turn"},'
            '"usage":{"output_tokens":10}}\n'
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

        # Assert — output accounting is preserved by degrading input to 0
        usage_deltas = [c for c in chunks if c.get("type") == "usage"]
        assert usage_deltas == [{"type": "usage", "input_tokens": 0, "output_tokens": 10}]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_no_usage_delta_without_output_tokens(self, anthropic_adapter):
        """stream() does not yield a usage delta when message_delta lacks output_tokens
        even if message_start has input_tokens."""
        # Arrange — message_start with input_tokens but message_delta without usage
        sse_body = (
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"msg_01","usage":{"input_tokens":25}}}\n'
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

        # Assert — no usage delta without output_tokens
        usage_deltas = [c for c in chunks if c.get("type") == "usage"]
        assert len(usage_deltas) == 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_no_usage_delta_without_any_usage_data(self, anthropic_adapter):
        """stream() does not yield a usage delta when no usage data is in the stream."""
        # Arrange — stream with no usage data at all (existing test scenario)
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

        # Assert — no usage deltas at all
        usage_deltas = [c for c in chunks if c.get("type") == "usage"]
        assert len(usage_deltas) == 0
        # Content deltas and finish still work normally
        assert chunks == [
            {"type": "content_delta", "text": "Hello"},
            {"type": "content_delta", "text": " world"},
            {"type": "finish", "reason": "stop"},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_usage_delta_with_zero_output_tokens(self, anthropic_adapter):
        """stream() yields a usage delta even when output_tokens is 0."""
        # Arrange — output_tokens can be 0 (e.g. cache-miss response that was cancelled)
        sse_body = (
            "event: message_start\n"
            'data: {"type":"message_start",'
            '"message":{"id":"msg_01","usage":{"input_tokens":2589}}}\n'
            "\n"
            "event: message_delta\n"
            'data: {"type":"message_delta",'
            '"delta":{"stop_reason":"end_turn"},'
            '"usage":{"output_tokens":0}}\n'
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

        # Assert — usage delta with 0 output_tokens is still yielded
        usage_deltas = [c for c in chunks if c.get("type") == "usage"]
        assert len(usage_deltas) == 1
        assert usage_deltas[0] == {
            "type": "usage",
            "input_tokens": 2589,
            "output_tokens": 0,
        }


# ---------------------------------------------------------------------------
# Lifecycle: aclose() and async context manager
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Verify that aclose() and async context manager work correctly."""

    @pytest.mark.asyncio
    async def test_aclose_closes_http_client(self):
        """aclose() closes the underlying httpx.AsyncClient."""
        adapter = AnthropicAdapter(ANTHROPIC_CONFIG, API_KEY)
        assert not adapter._client.is_closed
        await adapter.aclose()
        assert adapter._client.is_closed

    @pytest.mark.asyncio
    async def test_context_manager_closes_client(self):
        """Using 'async with' closes the client on exit."""
        async with AnthropicAdapter(ANTHROPIC_CONFIG, API_KEY) as adapter:
            assert not adapter._client.is_closed
        assert adapter._client.is_closed

    @pytest.mark.asyncio
    async def test_context_manager_yields_adapter(self):
        """The context manager yields the adapter instance."""
        async with AnthropicAdapter(ANTHROPIC_CONFIG, API_KEY) as adapter:
            assert isinstance(adapter, AnthropicAdapter)


# ---------------------------------------------------------------------------
# Catalog discovery (normalize_catalog_entry, discovery headers/params)
# ---------------------------------------------------------------------------
