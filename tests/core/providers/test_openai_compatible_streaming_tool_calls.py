"""Openai compatible streaming: tool calls behavior."""

from __future__ import annotations

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


# Reused wire index: Ollama-compatible servers distinguish parallel Tool Calls
# by id while reusing one index for the whole batch.
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
