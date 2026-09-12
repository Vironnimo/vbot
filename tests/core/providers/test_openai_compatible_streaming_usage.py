"""Openai compatible streaming: usage behavior."""

from __future__ import annotations

from .openai_compatible_test_support import (
    OPENAI_URL,
    SAMPLE_MESSAGES,
    httpx,
    pytest,
    respx,
)
from .openai_compatible_test_support import openai_adapter as openai_adapter
from .openai_compatible_test_support import openrouter_adapter as openrouter_adapter


# stream() — usage delta
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
