"""Openai compatible streaming: tool calls behavior."""

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
    @pytest.mark.parametrize("name", [None, "", 42])
    async def test_empty_idless_tool_attempt_is_preserved_for_rejection(self, openai_adapter, name):
        calls = [
            {"index": 0, "function": {"name": name, "arguments": ""}},
            {"index": 1, "id": "call_valid", "function": {"name": "read", "arguments": "{}"}},
        ]
        chunk = {"choices": [{"delta": {"tool_calls": calls}, "finish_reason": "tool_calls"}]}
        body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, text=body))
        accumulator = StreamingAccumulator()

        async for delta in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
            accumulator.add_delta(delta)

        result = accumulator.finalize_assistant_fields().tool_calls
        assert result is not None
        assert len(result) == 2
        assert result[0]["id"] == "tool_call_0"
        assert result[0]["rejection"]["code"] == "malformed_tool_call"
        assert result[1] == {"id": "call_valid", "name": "read", "arguments": {}}

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
    @pytest.mark.parametrize("late_id", [False, True])
    async def test_redirected_slot_does_not_capture_a_later_native_index(
        self, openai_adapter, late_id
    ):
        calls = [
            {"index": 0, "id": "call_a", "function": {"name": "first", "arguments": "{}"}},
            {"index": 0, "id": "call_b", "function": {"name": "second", "arguments": "{}"}},
            {"index": 1, "function": {"name": "third", "arguments": '{"value":'}},
            {"index": 1, "id": "call_c", "function": {"arguments": "3}"}},
        ]
        if not late_id:
            calls[2]["id"] = "call_c"
        body = "".join(
            "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [call]}}]}) + "\n\n"
            for call in calls
        )
        body += 'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        body += "data: [DONE]\n\n"
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, text=body))
        accumulator = StreamingAccumulator()

        async for delta in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
            accumulator.add_delta(delta)

        calls = accumulator.finalize_assistant_fields().tool_calls
        assert calls == [
            {"id": "call_a", "name": "first", "arguments": {}},
            {"id": "call_b", "name": "second", "arguments": {}},
            {"id": "call_c", "name": "third", "arguments": {"value": 3}},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_repeated_name_is_deduplicated_but_argument_bytes_are_kept(self, openai_adapter):
        # Some compatible servers resend the full name on every fragment;
        # argument fragments are true deltas even when they repeat a prefix.
        fragments = ['{"value":', '{"value":', "1}}"]
        body = "".join(
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_a",
                                        "function": {"name": "write", "arguments": fragment},
                                    }
                                ]
                            }
                        }
                    ]
                }
            )
            + "\n\n"
            for fragment in fragments
        )
        body += 'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        body += "data: [DONE]\n\n"
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, text=body))
        accumulator = StreamingAccumulator()
        tool_deltas = []

        async for delta in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
            if delta["type"] == "tool_call_delta":
                tool_deltas.append(delta)
            accumulator.add_delta(delta)

        assert [delta["name_delta"] for delta in tool_deltas] == ["write", "", ""]
        assert [delta["arguments_delta"] for delta in tool_deltas] == fragments
        assert accumulator.finalize_assistant_fields().tool_calls == [
            {"id": "call_a", "name": "write", "arguments": {"value": {"value": 1}}}
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_interleaved_reused_index_returns_to_the_original_call(self, openai_adapter):
        calls = [
            {"index": 0, "id": "call_a", "function": {"name": "first", "arguments": '{"a":'}},
            {"index": 0, "id": "call_b", "function": {"name": "second", "arguments": '{"b":'}},
            {"index": 0, "id": "call_a", "function": {"arguments": "1}"}},
            {"index": 0, "id": "call_b", "function": {"arguments": "2}"}},
        ]
        body = "".join(
            "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [call]}}]}) + "\n\n"
            for call in calls
        )
        body += 'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        body += "data: [DONE]\n\n"
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, text=body))
        accumulator = StreamingAccumulator()

        async for delta in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
            accumulator.add_delta(delta)

        assert accumulator.finalize_assistant_fields().tool_calls == [
            {"id": "call_a", "name": "first", "arguments": {"a": 1}},
            {"id": "call_b", "name": "second", "arguments": {"b": 2}},
        ]

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
