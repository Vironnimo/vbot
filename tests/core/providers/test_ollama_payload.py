"""Ollama: payload behavior."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.errors import ProviderError
from core.providers.ollama import (
    OllamaAdapter,
    OllamaCloudAdapter,
)
from core.tools import HISTORY_TOOL_DESCRIPTION, HISTORY_TOOL_NAME, HISTORY_TOOL_PARAMETERS
from tests.core.providers.ollama_helpers import (
    CLOUD_TEXT_RESPONSE,
    OLLAMA_CHAT_URL,
    OLLAMA_CLOUD_CHAT_URL,
    OLLAMA_CLOUD_CONFIG,
    OLLAMA_CONFIG,
    SAMPLE_MESSAGES,
    TEXT_RESPONSE,
    TOOL_CALL_RESPONSE,
    _last_request_payload,
    _model_lookup,
)
from tests.core.providers.ollama_helpers import (
    adapter as adapter,
)


# Payload building and headers
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
    async def test_cloud_connection_sends_bearer_header_without_doubling_v1(self) -> None:
        """An explicit compatible base and API key reach the exact Cloud route."""
        # Arrange
        cloud_connection = OLLAMA_CLOUD_CONFIG.get_connection("api-key")
        adapter = OllamaCloudAdapter(
            OLLAMA_CLOUD_CONFIG,
            "sk-cloud",
            "https://ollama.com/v1/",
            cloud_connection.auth,
            connection_mode=cloud_connection.mode,
        )
        route = respx.post(OLLAMA_CLOUD_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="minimax-m3")

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
    async def test_top_p_rides_under_options(self, adapter: OllamaAdapter) -> None:
        """top_p translates onto Ollama's options object like temperature."""
        # Arrange
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="ministral-3:8b", top_p=0.95)

        # Assert
        payload = _last_request_payload(route)
        assert payload["options"] == {"top_p": 0.95}

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

    @respx.mock
    @pytest.mark.asyncio
    async def test_cloud_reasoning_ladder_sends_max_level(self, adapter: OllamaAdapter) -> None:
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        await adapter.send(
            SAMPLE_MESSAGES,
            model_id="deepseek-v4-flash",
            thinking_effort="max",
        )

        assert _last_request_payload(route)["think"] == "max"

    @respx.mock
    @pytest.mark.asyncio
    async def test_cloud_reasoning_ladder_uses_boolean_off_switch(
        self,
        adapter: OllamaAdapter,
    ) -> None:
        route = respx.post(OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=TEXT_RESPONSE)
        )

        await adapter.send(
            SAMPLE_MESSAGES,
            model_id="deepseek-v4-flash",
            thinking_effort="none",
        )

        assert _last_request_payload(route)["think"] is False


# Message translation
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
            {
                "role": "tool",
                "tool_call_id": "call_dmop6zf4",
                "name": "get_weather",
                "content": "Sunny, 25°C",
            },
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
            "tool_name": "get_weather",
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
        with pytest.raises(ProviderError):
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
