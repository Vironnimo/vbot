"""Ollama: responses behavior."""

from __future__ import annotations

import httpx
import pytest
import respx

from core.providers.errors import NetworkError, ProviderError
from core.providers.ollama import (
    OllamaAdapter,
    OllamaCloudAdapter,
)
from tests.core.providers.ollama_helpers import (
    OLLAMA_CHAT_URL,
    OLLAMA_CLOUD_CONFIG,
    SAMPLE_MESSAGES,
    TEXT_RESPONSE,
    TOOL_CALL_RESPONSE,
    _last_request_payload,
    _ndjson,
)
from tests.core.providers.ollama_helpers import (
    adapter as adapter,
)


# Response normalization
class TestNormalizeResponse:
    def test_tool_call_response_maps_object_arguments(self, adapter: OllamaAdapter) -> None:
        # Act
        normalized = adapter.normalize_response(TOOL_CALL_RESPONSE)

        # Assert
        assert normalized["role"] == "assistant"
        assert normalized["content"] is None
        assert normalized["tool_calls"] == [
            {"id": "call_dmop6zf4", "name": "get_weather", "arguments": {"city": "Berlin"}}
        ]
        assert normalized["usage"] == {"input_tokens": 611, "output_tokens": 12}

    def test_text_response_maps_content_and_usage(self, adapter: OllamaAdapter) -> None:
        # Act
        normalized = adapter.normalize_response(TEXT_RESPONSE)

        # Assert
        assert normalized["content"] == "Hello there."
        assert normalized["tool_calls"] is None
        assert normalized["usage"] == {"input_tokens": 558, "output_tokens": 4}

    def test_thinking_field_maps_to_reasoning(self, adapter: OllamaAdapter) -> None:
        # Arrange
        response = {
            "message": {"role": "assistant", "content": "Answer.", "thinking": "Pondering."},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 10,
            "eval_count": 5,
        }

        # Act
        normalized = adapter.normalize_response(response)

        # Assert
        assert normalized["reasoning"] == "Pondering."
        assert normalized["content"] == "Answer."

    def test_tool_call_without_id_gets_positional_fallback(self, adapter: OllamaAdapter) -> None:
        # Arrange
        response = {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_weather", "arguments": {}}}],
            },
            "done": True,
        }

        # Act
        normalized = adapter.normalize_response(response)

        # Assert
        assert normalized["tool_calls"][0]["id"] == "tool_call_0"

    def test_malformed_tool_arguments_become_rejected_call(self, adapter: OllamaAdapter) -> None:
        normalized = adapter.normalize_response(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "function": {"name": "get_weather", "arguments": "{broken"},
                        }
                    ],
                },
                "done": True,
            }
        )

        assert normalized["tool_calls"][0]["arguments"] == {}
        assert normalized["tool_calls"][0]["rejection"]["code"] == ("malformed_tool_arguments")

    def test_collapsed_single_tool_call_object_is_preserved(self, adapter: OllamaAdapter) -> None:
        normalized = adapter.normalize_response(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": {
                        "id": "call_one",
                        "function": {"name": "get_weather", "arguments": {"city": "Berlin"}},
                    },
                },
                "done": True,
            }
        )

        assert normalized["tool_calls"] == [
            {"id": "call_one", "name": "get_weather", "arguments": {"city": "Berlin"}}
        ]

    def test_missing_usage_counters_omit_usage(self, adapter: OllamaAdapter) -> None:
        # Act
        normalized = adapter.normalize_response({"message": {"content": "x"}, "done": True})

        # Assert
        assert "usage" not in normalized

    def test_cloud_response_without_prompt_count_preserves_output_usage(
        self, adapter: OllamaAdapter
    ) -> None:
        normalized = adapter.normalize_response(
            {
                "message": {"content": "Answer."},
                "done": True,
                "done_reason": "stop",
                "eval_count": 2572,
            }
        )

        assert normalized["usage"] == {"output_tokens": 2572}


class TestStreamNdjson:
    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_content_usage_and_finish(self, adapter: OllamaAdapter) -> None:
        """NDJSON chunks map to content deltas, one usage delta, and a finish."""
        # Arrange — real chunk shapes from the live probe.
        body = _ndjson(
            {"model": "m", "message": {"role": "assistant", "content": "Hel"}, "done": False},
            {"model": "m", "message": {"role": "assistant", "content": "lo"}, "done": False},
            {
                "model": "m",
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 558,
                "eval_count": 4,
            },
        )
        respx.post(OLLAMA_CHAT_URL).mock(return_value=httpx.Response(200, text=body))

        # Act
        deltas = [d async for d in adapter.stream(SAMPLE_MESSAGES, model_id="ministral-3:8b")]

        # Assert
        assert deltas == [
            {"type": "content_delta", "text": "Hel"},
            {"type": "content_delta", "text": "lo"},
            {"type": "usage", "input_tokens": 558, "output_tokens": 4},
            {"type": "finish", "reason": "stop"},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_cloud_stream_preserves_eval_count_when_prompt_count_is_omitted(
        self, adapter: OllamaAdapter
    ) -> None:
        body = _ndjson(
            {"model": "m", "message": {"role": "assistant", "content": "Answer."}, "done": False},
            {
                "model": "m",
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "stop",
                "eval_count": 2572,
            },
        )
        respx.post(OLLAMA_CHAT_URL).mock(return_value=httpx.Response(200, text=body))

        deltas = [d async for d in adapter.stream(SAMPLE_MESSAGES, model_id="minimax-m3")]

        assert deltas == [
            {"type": "content_delta", "text": "Answer."},
            {"type": "usage", "output_tokens": 2572},
            {"type": "finish", "reason": "stop"},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_marks_stream_flag_true(self, adapter: OllamaAdapter) -> None:
        # Arrange
        body = _ndjson({"message": {"content": ""}, "done": True, "done_reason": "stop"})
        route = respx.post(OLLAMA_CHAT_URL).mock(return_value=httpx.Response(200, text=body))

        # Act
        async for _ in adapter.stream(SAMPLE_MESSAGES, model_id="ministral-3:8b"):
            pass

        # Assert
        assert _last_request_payload(route)["stream"] is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_tool_call_yields_single_delta_and_tool_finish(
        self, adapter: OllamaAdapter
    ) -> None:
        """A streamed tool call arrives whole: one delta with serialized arguments."""
        # Arrange
        body = _ndjson(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_dmop6zf4",
                            "function": {
                                "index": 0,
                                "name": "get_weather",
                                "arguments": {"city": "Berlin"},
                            },
                        }
                    ],
                },
                "done": False,
            },
            {
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 611,
                "eval_count": 12,
            },
        )
        respx.post(OLLAMA_CHAT_URL).mock(return_value=httpx.Response(200, text=body))

        # Act
        deltas = [d async for d in adapter.stream(SAMPLE_MESSAGES, model_id="ministral-3:8b")]

        # Assert
        assert deltas[0] == {
            "type": "tool_call_delta",
            "id": "call_dmop6zf4",
            "name_delta": "get_weather",
            "arguments_delta": '{"city":"Berlin"}',
        }
        assert deltas[-1] == {"type": "finish", "reason": "tool_calls"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_preserves_malformed_tool_arguments_for_chat_rejection(
        self, adapter: OllamaAdapter
    ) -> None:
        body = _ndjson(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "function": {"name": "get_weather", "arguments": "{broken"},
                        }
                    ],
                },
                "done": False,
            },
            {"message": {"content": ""}, "done": True, "done_reason": "stop"},
        )
        respx.post(OLLAMA_CHAT_URL).mock(return_value=httpx.Response(200, text=body))

        deltas = [d async for d in adapter.stream(SAMPLE_MESSAGES, model_id="ministral-3:8b")]

        assert deltas[0] == {
            "type": "tool_call_delta",
            "id": "call_bad",
            "name_delta": "get_weather",
            "arguments_delta": '"{broken"',
        }
        assert deltas[-1] == {"type": "finish", "reason": "tool_calls"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_reasoning_deltas(self, adapter: OllamaAdapter) -> None:
        # Arrange
        body = _ndjson(
            {"message": {"role": "assistant", "thinking": "Hmm"}, "done": False},
            {"message": {"role": "assistant", "content": "Hi"}, "done": False},
            {"message": {"content": ""}, "done": True, "done_reason": "stop"},
        )
        respx.post(OLLAMA_CHAT_URL).mock(return_value=httpx.Response(200, text=body))

        # Act
        deltas = [d async for d in adapter.stream(SAMPLE_MESSAGES, model_id="thinking-model")]

        # Assert
        assert deltas[0] == {"type": "reasoning_delta", "text": "Hmm"}
        assert deltas[1] == {"type": "content_delta", "text": "Hi"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_in_band_error_raises_provider_error(self, adapter: OllamaAdapter) -> None:
        # Arrange
        body = _ndjson({"error": "model not found"})
        respx.post(OLLAMA_CHAT_URL).mock(return_value=httpx.Response(200, text=body))

        # Act / Assert
        with pytest.raises(ProviderError):
            async for _ in adapter.stream(SAMPLE_MESSAGES, model_id="missing"):
                pass

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_without_done_chunk_raises_network_error(
        self, adapter: OllamaAdapter
    ) -> None:
        # Arrange
        body = _ndjson({"message": {"content": "partial"}, "done": False})
        respx.post(OLLAMA_CHAT_URL).mock(return_value=httpx.Response(200, text=body))

        # Act / Assert
        from core.providers.errors import NetworkError

        with pytest.raises(NetworkError):
            async for _ in adapter.stream(SAMPLE_MESSAGES, model_id="ministral-3:8b"):
                pass

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_connect_error_names_the_stopped_service(
        self, adapter: OllamaAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused connection surfaces 'is Ollama running?' instead of a socket error."""

        # Arrange — bypass retry backoff so the retryable NetworkError fails fast.
        async def _single_attempt(func, **_kwargs):
            return await func()

        monkeypatch.setattr("core.providers.ollama.retry_async", _single_attempt)
        respx.post(OLLAMA_CHAT_URL).mock(side_effect=httpx.ConnectError("connection refused"))

        # Act / Assert
        from core.providers.errors import NetworkError

        with pytest.raises(NetworkError):
            await adapter.send(SAMPLE_MESSAGES, model_id="ministral-3:8b")

    @pytest.mark.asyncio
    async def test_cloud_connect_error_does_not_suggest_starting_local_service(self) -> None:
        connection = OLLAMA_CLOUD_CONFIG.get_connection("api-key")
        adapter = OllamaCloudAdapter(
            OLLAMA_CLOUD_CONFIG,
            "sk-cloud",
            connection.base_url,
            connection.auth,
            connection_mode=connection.mode,
        )

        error = adapter._wrap_transport_error(httpx.ConnectError("connection refused"))

        assert isinstance(error, NetworkError)
        assert "Ollama Cloud is not reachable" in str(error)
        assert "service running" not in str(error)
        await adapter.aclose()
