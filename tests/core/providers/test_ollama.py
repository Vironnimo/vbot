"""Tests for the Ollama provider adapter.

Uses ``respx`` to mock httpx calls. The response fixtures are real payloads
captured from a live Ollama 0.24.0 instance on 2026-07-07 (see the plan's
live-probe notes): tool-call arguments are JSON objects, streaming is NDJSON,
and usage rides in ``prompt_eval_count``/``eval_count``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.models.models import (
    REASONING_CONTROL_LEVELS,
    REASONING_CONTROL_ON_OFF,
    Capabilities,
    Model,
    ReasoningCapabilities,
)
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.errors import ProviderError
from core.providers.ollama import OllamaAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.tools import HISTORY_TOOL_DESCRIPTION, HISTORY_TOOL_NAME, HISTORY_TOOL_PARAMETERS

OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"

OLLAMA_CONFIG = ProviderConfig(
    id="ollama",
    name="Ollama",
    adapter="ollama",
    base_url="http://localhost:11434",
    models_endpoint="/api/tags",
    connections=[
        ConnectionConfig(
            id="local",
            type="none",
            label="Local",
            auth=AuthConfig(header="", prefix="", credential_key=""),
        ),
        ConnectionConfig(
            id="cloud",
            type="api_key",
            label="Ollama Cloud",
            auth=AuthConfig(
                header="Authorization", prefix="Bearer ", credential_key="OLLAMA_API_KEY"
            ),
            base_url="https://ollama.com",
        ),
    ],
)

# Real non-streaming tool-call response (ministral-3:8b, live probe).
TOOL_CALL_RESPONSE: dict[str, Any] = {
    "model": "ministral-3:8b",
    "created_at": "2026-07-07T10:00:00.000000Z",
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
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 611,
    "eval_count": 12,
}

TEXT_RESPONSE: dict[str, Any] = {
    "model": "ministral-3:8b",
    "created_at": "2026-07-07T10:00:00.000000Z",
    "message": {"role": "assistant", "content": "Hello there."},
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 558,
    "eval_count": 4,
}

# Real /api/show shape (trimmed to the consumed fields).
SHOW_RESPONSE: dict[str, Any] = {
    "capabilities": ["completion", "vision", "tools"],
    "details": {
        "format": "gguf",
        "family": "mistral3",
        "parameter_size": "8.9B",
        "quantization_level": "Q4_K_M",
    },
    "model_info": {
        "general.architecture": "mistral3",
        "mistral3.context_length": 262144,
        "mistral3.rope.scaling.original_context_length": 16384,
    },
}

SAMPLE_MESSAGES = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hi"},
]

THINKING_MODEL = Model(
    model_id="thinking-model",
    name="Thinking Model",
    capabilities=Capabilities(
        vision=False,
        tools=True,
        json_mode=False,
        reasoning=ReasoningCapabilities(supported=True, control=REASONING_CONTROL_ON_OFF),
    ),
    context_window=32768,
    max_output_tokens=None,
)

PLAIN_MODEL = Model(
    model_id="plain-model",
    name="Plain Model",
    capabilities=Capabilities(
        vision=False,
        tools=True,
        json_mode=False,
        reasoning=ReasoningCapabilities(supported=False),
    ),
    context_window=32768,
    max_output_tokens=None,
)

GPT_OSS_MODEL = Model(
    model_id="gpt-oss:20b",
    name="GPT-OSS 20B",
    capabilities=Capabilities(
        vision=False,
        tools=True,
        json_mode=False,
        reasoning=ReasoningCapabilities(
            supported=True,
            control=REASONING_CONTROL_LEVELS,
            levels=("low", "medium", "high"),
        ),
    ),
    context_window=131_072,
    max_output_tokens=None,
)

_MODELS = {
    "thinking-model": THINKING_MODEL,
    "plain-model": PLAIN_MODEL,
    "gpt-oss:20b": GPT_OSS_MODEL,
}


def _model_lookup(model_id: str) -> Model | None:
    return _MODELS.get(model_id)


@pytest.fixture
def adapter() -> OllamaAdapter:
    return OllamaAdapter(OLLAMA_CONFIG, "", model_lookup=_model_lookup)


def _last_request_payload(route: respx.Route) -> dict[str, Any]:
    payload = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


# ---------------------------------------------------------------------------
# Payload building and headers
# ---------------------------------------------------------------------------


class TestPayloadBuilding:
    @respx.mock
    @pytest.mark.asyncio
    async def test_keyless_connection_sends_no_auth_header(self, adapter: OllamaAdapter) -> None:
        """The local connection's empty auth yields no Authorization header."""
        # Arrange
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="ministral-3:8b")

        # Assert
        assert "Authorization" not in route.calls.last.request.headers

    @respx.mock
    @pytest.mark.asyncio
    async def test_cloud_connection_sends_bearer_header(self) -> None:
        """An api_key connection carries the Bearer token."""
        # Arrange
        cloud_connection = OLLAMA_CONFIG.get_connection("cloud")
        adapter = OllamaAdapter(
            OLLAMA_CONFIG,
            "sk-cloud",
            cloud_connection.base_url,
            cloud_connection.auth,
        )
        route = respx.post("https://ollama.com/api/chat").mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="kimi-k2.6")

        # Assert
        assert route.calls.last.request.headers["Authorization"] == "Bearer sk-cloud"
        await adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_tools_are_wrapped_in_function_schema(self, adapter: OllamaAdapter) -> None:
        """Canonical flat tool definitions become OpenAI-style function schemas."""
        # Arrange
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TOOL_CALL_RESPONSE)
        )
        tools = [
            {
                "name": "get_weather",
                "description": "Get the weather.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ]

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="ministral-3:8b", tools=tools)

        # Assert
        payload = _last_request_payload(route)
        assert payload["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the weather.",
                    "parameters": tools[0]["parameters"],
                },
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_history_tool_is_wrapped_in_function_schema(self, adapter: OllamaAdapter) -> None:
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TOOL_CALL_RESPONSE)
        )
        definition = {
            "name": HISTORY_TOOL_NAME,
            "description": HISTORY_TOOL_DESCRIPTION,
            "parameters": HISTORY_TOOL_PARAMETERS,
        }

        await adapter.send(SAMPLE_MESSAGES, model_id="ministral-3:8b", tools=[definition])

        assert _last_request_payload(route)["tools"] == [
            {"type": "function", "function": definition}
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_temperature_rides_under_options(self, adapter: OllamaAdapter) -> None:
        """Sampling parameters translate onto Ollama's options object."""
        # Arrange
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="ministral-3:8b", temperature=0.2)

        # Assert
        payload = _last_request_payload(route)
        assert payload["options"] == {"temperature": 0.2}

    @respx.mock
    @pytest.mark.asyncio
    async def test_none_kwargs_are_dropped(self, adapter: OllamaAdapter) -> None:
        """None-valued caller kwargs mean 'not specified' and never reach the wire."""
        # Arrange
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="ministral-3:8b", temperature=None, tools=None)

        # Assert
        payload = _last_request_payload(route)
        assert "options" not in payload
        assert "tools" not in payload

    @respx.mock
    @pytest.mark.asyncio
    async def test_local_context_resolver_sets_num_ctx(self) -> None:
        """A resolved effective window is enforced via options.num_ctx."""
        # Arrange
        adapter = OllamaAdapter(
            OLLAMA_CONFIG,
            "",
            model_lookup=_model_lookup,
            local_context_resolver=lambda model_id: 16384,
        )
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="ministral-3:8b")

        # Assert
        payload = _last_request_payload(route)
        assert payload["options"]["num_ctx"] == 16384
        await adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_resolver_returning_none_sends_no_num_ctx(self) -> None:
        """Non-local models (resolver → None) carry no num_ctx."""
        # Arrange
        adapter = OllamaAdapter(
            OLLAMA_CONFIG,
            "",
            model_lookup=_model_lookup,
            local_context_resolver=lambda model_id: None,
        )
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="kimi-k2.6:cloud")

        # Assert
        assert "options" not in _last_request_payload(route)
        await adapter.aclose()


class TestReasoningToggle:
    @respx.mock
    @pytest.mark.asyncio
    async def test_effort_on_thinking_model_sends_think_true(self, adapter: OllamaAdapter) -> None:
        # Arrange
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="thinking-model", thinking_effort="high")

        # Assert
        assert _last_request_payload(route)["think"] is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_effort_none_on_thinking_model_sends_think_false(
        self, adapter: OllamaAdapter
    ) -> None:
        # Arrange
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="thinking-model", thinking_effort="none")

        # Assert
        assert _last_request_payload(route)["think"] is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_effort_selected_omits_think(self, adapter: OllamaAdapter) -> None:
        """No selected effort leaves the provider default untouched."""
        # Arrange
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="thinking-model")

        # Assert
        assert "think" not in _last_request_payload(route)

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_thinking_model_never_receives_think(self, adapter: OllamaAdapter) -> None:
        """Ollama rejects think on non-thinking models — the field must stay absent."""
        # Arrange
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="plain-model", thinking_effort="high")

        # Assert
        assert "think" not in _last_request_payload(route)

    @respx.mock
    @pytest.mark.asyncio
    async def test_unknown_model_omits_think(self, adapter: OllamaAdapter) -> None:
        """Unknown reasoning support (no catalog entry) omits the toggle."""
        # Arrange
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="unknown-model", thinking_effort="high")

        # Assert
        assert "think" not in _last_request_payload(route)

    @respx.mock
    @pytest.mark.asyncio
    async def test_gpt_oss_receives_level_string_instead_of_ignored_boolean(
        self,
        adapter: OllamaAdapter,
    ) -> None:
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        await adapter.send(
            SAMPLE_MESSAGES,
            model_id="gpt-oss:20b",
            thinking_effort="xhigh",
        )

        assert _last_request_payload(route)["think"] == "high"

    @respx.mock
    @pytest.mark.asyncio
    async def test_gpt_oss_unsupported_off_omits_think_instead_of_sending_boolean(
        self,
        adapter: OllamaAdapter,
    ) -> None:
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        await adapter.send(
            SAMPLE_MESSAGES,
            model_id="gpt-oss:20b",
            thinking_effort="none",
        )

        assert "think" not in _last_request_payload(route)


# ---------------------------------------------------------------------------
# Message translation
# ---------------------------------------------------------------------------


class TestMessageTranslation:
    @respx.mock
    @pytest.mark.asyncio
    async def test_tool_cycle_round_trips_object_arguments(self, adapter: OllamaAdapter) -> None:
        """Canonical dict arguments replay onto the wire as JSON objects, not strings."""
        # Arrange
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "Weather in Berlin?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_dmop6zf4", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_dmop6zf4", "content": "Sunny, 25°C"},
        ]

        # Act
        await adapter.send(messages, model_id="ministral-3:8b")

        # Assert
        wire_messages = _last_request_payload(route)["messages"]
        assert wire_messages[1]["tool_calls"] == [
            {
                "id": "call_dmop6zf4",
                "function": {"name": "get_weather", "arguments": {"city": "Berlin"}},
            }
        ]
        assert wire_messages[2] == {
            "role": "tool",
            "content": "Sunny, 25°C",
            "tool_call_id": "call_dmop6zf4",
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_rich_tool_result_uses_request_only_user_fallback(
        self,
        adapter: OllamaAdapter,
    ) -> None:
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )
        messages = [
            {
                "role": "tool",
                "tool_call_id": "call_image",
                "content": '{"ok":true}',
                TOOL_RESULT_CONTENT_BLOCKS_FIELD: [
                    {
                        "type": "media",
                        "base64": "aW1hZ2U=",
                        "media_type": "image/png",
                    },
                    {"type": "text", "text": "[Image path: C:/diagram.png]"},
                ],
            }
        ]

        await adapter.send(messages, model_id="ministral-3:8b")

        assert _last_request_payload(route)["messages"] == [
            {
                "role": "tool",
                "content": '{"ok":true}\n\n[Image path: C:/diagram.png]',
                "tool_call_id": "call_image",
            },
            {
                "role": "user",
                "content": "",
                "images": ["aW1hZ2U="],
            },
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_image_blocks_become_per_message_images_list(
        self, adapter: OllamaAdapter
    ) -> None:
        """Canonical media blocks map to Ollama's bare-base64 images array."""
        # Arrange
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image?"},
                    {"type": "media", "base64": "aW1hZ2U=", "media_type": "image/png"},
                ],
            }
        ]

        # Act
        await adapter.send(messages, model_id="ministral-3:8b")

        # Assert
        wire_message = _last_request_payload(route)["messages"][0]
        assert wire_message["content"] == "What is in this image?"
        assert wire_message["images"] == ["aW1hZ2U="]

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_image_media_raises_provider_error(self, adapter: OllamaAdapter) -> None:
        # Arrange
        messages = [
            {
                "role": "user",
                "content": [{"type": "media", "base64": "d2F2", "media_type": "audio/wav"}],
            }
        ]

        # Act / Assert
        with pytest.raises(ProviderError, match="only image media blocks"):
            await adapter.send(messages, model_id="ministral-3:8b")

    @respx.mock
    @pytest.mark.asyncio
    async def test_assistant_reasoning_replays_as_thinking_field(
        self, adapter: OllamaAdapter
    ) -> None:
        # Arrange
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )
        messages = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello.", "reasoning": "The user greets me."},
            {"role": "user", "content": "How are you?"},
        ]

        # Act
        await adapter.send(messages, model_id="thinking-model")

        # Assert
        assert _last_request_payload(route)["messages"][1]["thinking"] == "The user greets me."


# ---------------------------------------------------------------------------
# Response normalization
# ---------------------------------------------------------------------------


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

    def test_missing_usage_counters_omit_usage(self, adapter: OllamaAdapter) -> None:
        # Act
        normalized = adapter.normalize_response({"message": {"content": "x"}, "done": True})

        # Assert
        assert "usage" not in normalized


# ---------------------------------------------------------------------------
# Streaming (NDJSON)
# ---------------------------------------------------------------------------


def _ndjson(*chunks: dict[str, Any]) -> str:
    return "\n".join(json.dumps(chunk) for chunk in chunks) + "\n"


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
        with pytest.raises(ProviderError, match="model not found"):
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

        with pytest.raises(NetworkError, match="without a done chunk"):
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

        with pytest.raises(NetworkError, match="is the Ollama service running"):
            await adapter.send(SAMPLE_MESSAGES, model_id="ministral-3:8b")


# ---------------------------------------------------------------------------
# Catalog normalization and enrichment
# ---------------------------------------------------------------------------


class TestCatalogNormalization:
    def test_local_entry_is_stamped_local(self) -> None:
        # Arrange — real /api/tags local entry (trimmed).
        raw = {
            "name": "ministral-3:8b",
            "model": "ministral-3:8b",
            "size": 6022236616,
            "details": {
                "format": "gguf",
                "family": "mistral3",
                "parameter_size": "8.9B",
                "quantization_level": "Q4_K_M",
            },
        }

        # Act
        model = OllamaAdapter.normalize_catalog_entry(raw)

        # Assert
        assert model.model_id == "ministral-3:8b"
        assert model.family == "mistral3"
        assert model.metadata["ollama"] == {"local": True}
        assert model.capabilities.tools is False
        assert model.context_window is None

    def test_proxied_cloud_entry_is_stamped_remote(self) -> None:
        # Arrange — real /api/tags proxied cloud entry (trimmed). ``remote_host``
        # is the fact; the ``:cloud`` name suffix is convention.
        raw = {
            "name": "kimi-k2.6:cloud",
            "model": "kimi-k2.6:cloud",
            "remote_model": "kimi-k2.6",
            "remote_host": "https://ollama.com:443",
            "details": {"family": "kimi"},
        }

        # Act
        model = OllamaAdapter.normalize_catalog_entry(raw)

        # Assert
        assert model.metadata["ollama"] == {"remote": True}
        assert model.family == "kimi"

    def test_entry_without_model_id_raises(self) -> None:
        with pytest.raises(ProviderError, match="no model id"):
            OllamaAdapter.normalize_catalog_entry({"details": {}})


class TestEnrichment:
    @pytest.mark.asyncio
    async def test_show_response_fills_capabilities_and_window(self) -> None:
        # Arrange
        base = OllamaAdapter.normalize_catalog_entry(
            {"model": "ministral-3:8b", "details": {"family": "mistral3"}}
        )
        posted: list[tuple[str, dict[str, Any]]] = []

        async def post_json(endpoint: str, payload: dict[str, Any]) -> Any:
            posted.append((endpoint, payload))
            return SHOW_RESPONSE

        # Act
        enriched = await OllamaAdapter.enrich_discovered_models({"ministral-3:8b": base}, post_json)

        # Assert
        model = enriched["ministral-3:8b"]
        assert posted == [("/api/show", {"model": "ministral-3:8b"})]
        assert model.capabilities.tools is True
        assert model.capabilities.vision is True
        assert model.capabilities.reasoning.supported is False
        # The exact "<arch>.context_length" key is read — never the rope
        # scaling original_context_length (16384 in the fixture).
        assert model.context_window == 262144
        assert model.metadata["ollama"] == {"local": True}
        assert model.family == "mistral3"

    @pytest.mark.asyncio
    async def test_thinking_capability_maps_to_on_off_control(self) -> None:
        # Arrange
        base = OllamaAdapter.normalize_catalog_entry({"model": "kimi-k2.6:cloud"})
        show = {"capabilities": ["completion", "tools", "thinking"], "model_info": {}}

        async def post_json(endpoint: str, payload: dict[str, Any]) -> Any:
            return show

        # Act
        enriched = await OllamaAdapter.enrich_discovered_models(
            {"kimi-k2.6:cloud": base}, post_json
        )

        # Assert
        reasoning = enriched["kimi-k2.6:cloud"].capabilities.reasoning
        assert reasoning.supported is True
        assert reasoning.control == REASONING_CONTROL_ON_OFF

    @pytest.mark.asyncio
    async def test_gpt_oss_thinking_capability_maps_to_level_control(self) -> None:
        base = OllamaAdapter.normalize_catalog_entry({"model": "gpt-oss:20b"})
        show = {"capabilities": ["completion", "tools", "thinking"], "model_info": {}}

        async def post_json(endpoint: str, payload: dict[str, Any]) -> Any:
            return show

        enriched = await OllamaAdapter.enrich_discovered_models(
            {"gpt-oss:20b": base},
            post_json,
        )

        reasoning = enriched["gpt-oss:20b"].capabilities.reasoning
        assert reasoning.control == REASONING_CONTROL_LEVELS
        assert reasoning.levels == ("low", "medium", "high")

    @pytest.mark.asyncio
    async def test_glm_4_7_discovery_profiles_full_history_thinking_replay(self) -> None:
        base = OllamaAdapter.normalize_catalog_entry({"model": "glm-4.7:latest"})
        show = {"capabilities": ["completion", "thinking"], "model_info": {}}

        async def post_json(endpoint: str, payload: dict[str, Any]) -> Any:
            return show

        enriched = await OllamaAdapter.enrich_discovered_models(
            {"glm-4.7:latest": base},
            post_json,
        )
        model = enriched["glm-4.7:latest"]
        lookup = {"glm-4.7:latest": model}.get
        adapter = OllamaAdapter(OLLAMA_CONFIG, "", model_lookup=lookup)

        assert model.metadata["ollama"]["reasoning_replay"] == "full_history"
        assert adapter.reasoning_replay_policy("glm-4.7:latest") == "full_history"
        await adapter.aclose()

    @pytest.mark.asyncio
    async def test_failed_show_keeps_conservative_baseline(self) -> None:
        """A failed per-model /api/show leaves that model at its baseline."""
        # Arrange
        base = OllamaAdapter.normalize_catalog_entry({"model": "broken-model"})

        async def post_json(endpoint: str, payload: dict[str, Any]) -> Any:
            raise ProviderError("boom", retryable=False)

        # Act
        enriched = await OllamaAdapter.enrich_discovered_models({"broken-model": base}, post_json)

        # Assert — no enriched entry; discovery keeps the baseline model.
        assert enriched == {}

    @pytest.mark.asyncio
    async def test_missing_architecture_leaves_window_unknown(self) -> None:
        # Arrange
        base = OllamaAdapter.normalize_catalog_entry({"model": "odd-model"})
        show = {"capabilities": ["completion"], "model_info": {"other.context_length": 4096}}

        async def post_json(endpoint: str, payload: dict[str, Any]) -> Any:
            return show

        # Act
        enriched = await OllamaAdapter.enrich_discovered_models({"odd-model": base}, post_json)

        # Assert — honest unknown, never a guessed window.
        assert enriched["odd-model"].context_window is None
