"""Openai compatible streaming: reasoning behavior."""

from __future__ import annotations

from core.chat.streaming import StreamingAccumulator

from .openai_compatible_test_support import (
    OPENAI_URL,
    SAMPLE_MESSAGES,
    httpx,
    json,
    pytest,
    respx,
)
from .openai_compatible_test_support import openai_adapter as openai_adapter
from .openai_compatible_test_support import openrouter_adapter as openrouter_adapter


class TestStreamSSE:
    "Verify that stream() correctly parses SSE event chunks."

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
