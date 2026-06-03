"""Tests for OpenAICompatibleAdapter.

Uses ``respx`` to mock httpx calls.  Verifies request building, header
and defaults injection, SSE streaming, retry on retryable errors, and
immediate failure on auth errors.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
    _to_openai_assistant_message,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OPENAI_CONFIG = ProviderConfig(
    id="openai",
    name="OpenAI",
    adapter="openai_compatible",
    base_url="https://api.openai.com/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="OPENAI_API_KEY",
            ),
        )
    ],
    defaults={"max_tokens": 4096, "temperature": 0.7},
)

OPENAI_MULTI_AUTH_CONFIG = ProviderConfig(
    id="openai",
    name="OpenAI",
    adapter="openai_compatible",
    base_url="https://api.openai.com/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="OPENAI_API_KEY",
            ),
        ),
        ConnectionConfig(
            id="service-account",
            type="api_key",
            label="Service Account",
            auth=AuthConfig(
                header="x-service-token",
                prefix="Token ",
                credential_key="OPENAI_SERVICE_TOKEN",
            ),
        ),
    ],
)

OPENROUTER_CONFIG = ProviderConfig(
    id="openrouter",
    name="OpenRouter",
    adapter="openai_compatible",
    base_url="https://openrouter.ai/api/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="OPENROUTER_API_KEY",
            ),
        )
    ],
    defaults={"max_tokens": 4096},
    extra_headers={"HTTP-Referer": "https://vbot.app", "X-Title": "vBot"},
)

NO_DEFAULTS_CONFIG = ProviderConfig(
    id="minimal",
    name="Minimal Provider",
    adapter="openai_compatible",
    base_url="https://api.minimal.example/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(header="x-api-key", prefix="", credential_key="MINIMAL_API_KEY"),
        )
    ],
)

API_KEY = "test-api-key-12345"

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MINIMAL_URL = "https://api.minimal.example/v1/chat/completions"

SUCCESS_RESPONSE = {
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

SAMPLE_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello"},
]

CANONICAL_MESSAGES_WITH_TOOL_LOOP = [
    {"role": "system", "model": "openai/gpt-5.2", "content": "You are helpful."},
    {"role": "user", "content": "Weather?"},
    {
        "role": "assistant",
        "model": "openai/gpt-5.2",
        "content": None,
        "reasoning_meta": {"encrypted_content": "opaque-current-turn"},
        "tool_calls": [
            {
                "id": "call_abc",
                "name": "get_weather",
                "arguments": {"city": "Berlin"},
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_abc",
        "name": "get_weather",
        "content": '{"temp":22}',
    },
]

SAMPLE_TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]

READ_TOOL_DEFINITION = {
    "name": "read",
    "description": "Read a text file from disk. Relative paths resolve from the workspace.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}


@pytest.fixture()
def openai_adapter():
    """OpenAI-compatible adapter with default OpenAI config."""
    return OpenAICompatibleAdapter(OPENAI_CONFIG, API_KEY)


@pytest.fixture()
def openrouter_adapter():
    """OpenAI-compatible adapter with OpenRouter config (extra headers)."""
    return OpenAICompatibleAdapter(OPENROUTER_CONFIG, API_KEY)


def _openai_test_model(model_id: str, *, reasoning: bool) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=reasoning),
        ),
        context_window=128000,
        max_output_tokens=4096,
    )


class TestAssistantMessageFormatting:
    """Verify assistant wire-message formatting edge cases."""

    def test_assistant_message_without_tool_calls_uses_empty_content_string(self) -> None:
        wire_message = _to_openai_assistant_message(
            {
                "role": "assistant",
                "content": None,
            }
        )

        assert wire_message["content"] == ""
        assert "tool_calls" not in wire_message

    def test_assistant_message_with_tool_calls_keeps_null_content(self) -> None:
        wire_message = _to_openai_assistant_message(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "name": "read",
                        "arguments": {"path": "README.md"},
                    }
                ],
            }
        )

        assert wire_message["content"] is None
        assert wire_message["tool_calls"] == [
            {
                "id": "call_abc",
                "type": "function",
                "function": {
                    "name": "read",
                    "arguments": '{"path":"README.md"}',
                },
            }
        ]


# ---------------------------------------------------------------------------
# send() — request format
# ---------------------------------------------------------------------------


class TestSendRequestFormat:
    """Verify that send() translates messages to the correct OpenAI format."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_includes_model_and_messages(self, openai_adapter):
        """The request payload contains the model ID and messages."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert route.called
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["model"] == "gpt-5.2"
        assert request_body["messages"] == SAMPLE_MESSAGES

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_user_list_content_image_to_data_url_part(self, openai_adapter):
        """Resolved media blocks are translated to OpenAI image_url data URLs."""
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "media",
                        "base64": "aW1hZ2UtYnl0ZXM=",
                        "media_type": "image/png",
                    }
                ],
            }
        ]

        await openai_adapter.send(messages, model_id="gpt-5.2")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["messages"] == [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,aW1hZ2UtYnl0ZXM="},
                    }
                ],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_user_list_content_text_part(self, openai_adapter):
        """Resolved text blocks are translated to OpenAI text parts."""
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        messages = [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]

        await openai_adapter.send(messages, model_id="gpt-5.2")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["messages"] == [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Hello"}],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_user_list_content_mixed_parts_in_order(self, openai_adapter):
        """Mixed resolved user parts keep order and translate media parts."""
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Before"},
                    {
                        "type": "media",
                        "base64": "YmFzZTY0LWltYWdl",
                        "media_type": "image/jpeg",
                    },
                    {"type": "text", "text": "After"},
                ],
            }
        ]

        await openai_adapter.send(messages, model_id="gpt-5.2")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["messages"] == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Before"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,YmFzZTY0LWltYWdl"},
                    },
                    {"type": "text", "text": "After"},
                ],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_keeps_user_string_content_unchanged(self, openai_adapter):
        """User string content keeps existing behavior."""
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        messages = [{"role": "user", "content": "Plain string"}]

        await openai_adapter.send(messages, model_id="gpt-5.2")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["messages"] == [{"role": "user", "content": "Plain string"}]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_applies_defaults_from_config(self, openai_adapter):
        """Defaults from ProviderConfig are included when not overridden."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["max_tokens"] == 4096
        assert request_body["temperature"] == 0.7

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_kwargs_override_defaults(self, openai_adapter):
        """Caller kwargs take precedence over provider defaults."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", temperature=1.2)

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["temperature"] == 1.2  # overridden
        assert request_body["max_tokens"] == 4096  # from defaults

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_without_defaults(self):
        """When config has no defaults, only model and messages are sent."""
        # Arrange
        adapter = OpenAICompatibleAdapter(NO_DEFAULTS_CONFIG, API_KEY)
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="minimal-model")

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert "model" in request_body
        assert "messages" in request_body
        assert "max_tokens" not in request_body
        assert "temperature" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_canonical_messages_tools_and_reasoning(self, openai_adapter):
        """Canonical messages, tool definitions, and effort map to OpenAI wire format."""
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        await openai_adapter.send(
            CANONICAL_MESSAGES_WITH_TOOL_LOOP,
            model_id="gpt-5.2",
            tools=SAMPLE_TOOLS,
            thinking_effort="high",
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["messages"] == [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Weather?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city":"Berlin"}',
                        },
                    }
                ],
                "encrypted_content": "opaque-current-turn",
            },
            {"role": "tool", "tool_call_id": "call_abc", "content": '{"temp":22}'},
        ]
        assert request_body["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather",
                    "parameters": SAMPLE_TOOLS[0]["parameters"],
                },
            }
        ]
        assert request_body["reasoning_effort"] == "high"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_read_definition_to_function_tool(self, openai_adapter):
        """The compact read definition maps to OpenAI function tools."""
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", tools=[READ_TOOL_DEFINITION])

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": READ_TOOL_DEFINITION["description"],
                    "parameters": READ_TOOL_DEFINITION["parameters"],
                },
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("thinking_effort", "expected_reasoning_effort"),
        [
            ("minimal", "low"),
            ("low", "low"),
            ("medium", "medium"),
            ("high", "high"),
            ("xhigh", "high"),
            ("max", "high"),
        ],
    )
    async def test_send_maps_to_nearest_openai_reasoning_effort(
        self,
        openai_adapter,
        thinking_effort,
        expected_reasoning_effort,
    ):
        """Base OpenAI-compatible reasoning maps vBot levels to safe OpenAI efforts."""
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        await openai_adapter.send(
            SAMPLE_MESSAGES,
            model_id="gpt-5.2",
            thinking_effort=thinking_effort,
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["reasoning_effort"] == expected_reasoning_effort
        assert "reasoning" not in request_body
        assert "include_reasoning" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_uses_explicit_none_when_catalog_confirms_reasoning_model(self):
        """Explicit none is sent only when the catalog says reasoning is supported."""
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        adapter = OpenAICompatibleAdapter(
            OPENAI_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _openai_test_model(model_id, reasoning=True),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", thinking_effort="none")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["reasoning_effort"] == "none"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_omits_explicit_none_for_generic_compatible_provider(self):
        """Generic OpenAI-compatible gateways do not inherit OpenAI-only none support."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = OpenAICompatibleAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _openai_test_model(model_id, reasoning=True),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="deepseek-v4-flash", thinking_effort="none")

        request_body = json.loads(route.calls.last.request.content)
        assert "reasoning_effort" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_normalizes_explicit_reasoning_effort_kwarg(self, openai_adapter):
        """Raw reasoning_effort kwargs follow the same nearest-effort mapping."""
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        await openai_adapter.send(
            SAMPLE_MESSAGES,
            model_id="gpt-5.2",
            reasoning_effort="max",
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["reasoning_effort"] == "high"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_suppresses_reasoning_when_catalog_disables_it(self):
        """Catalog-known non-reasoning models do not receive reasoning controls."""
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        adapter = OpenAICompatibleAdapter(
            OPENAI_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _openai_test_model(model_id, reasoning=False),
        )

        await adapter.send(
            SAMPLE_MESSAGES,
            model_id="gpt-4o",
            thinking_effort="high",
            reasoning_effort="high",
            reasoning={"effort": "high"},
            include_reasoning=True,
        )

        request_body = json.loads(route.calls.last.request.content)
        assert "reasoning_effort" not in request_body
        assert "reasoning" not in request_body
        assert "include_reasoning" not in request_body


# ---------------------------------------------------------------------------
# send() — headers and auth
# ---------------------------------------------------------------------------


class TestSendHeaders:
    """Verify that send() sends the correct auth and extra headers."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_bearer_auth_header(self, openai_adapter):
        """OpenAI config sends Authorization: Bearer <key>."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert route.called
        auth_header = route.calls.last.request.headers.get("authorization")
        assert auth_header == f"Bearer {API_KEY}"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_custom_auth_header(self):
        """Config with x-api-key header sends the key without Bearer prefix."""
        # Arrange
        adapter = OpenAICompatibleAdapter(NO_DEFAULTS_CONFIG, API_KEY)
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="minimal-model")

        # Assert
        api_key_header = route.calls.last.request.headers.get("x-api-key")
        assert api_key_header == API_KEY

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_uses_selected_connection_auth_header(self):
        """Selected connection auth metadata controls the request auth header."""
        # Arrange
        selected_connection = OPENAI_MULTI_AUTH_CONFIG.get_connection("service-account")
        adapter = OpenAICompatibleAdapter(
            OPENAI_MULTI_AUTH_CONFIG,
            API_KEY,
            auth_config=selected_connection.auth,
        )
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        request_headers = route.calls.last.request.headers
        assert request_headers.get("x-service-token") == f"Token {API_KEY}"
        assert request_headers.get("authorization") is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_extra_headers(self, openrouter_adapter):
        """OpenRouter config includes extra HTTP-Referer and X-Title headers."""
        # Arrange
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await openrouter_adapter.send(SAMPLE_MESSAGES, model_id="openai/gpt-5.2")

        # Assert
        request = route.calls.last.request
        assert request.headers.get("http-referer") == "https://vbot.app"
        assert request.headers.get("x-title") == "vBot"


# ---------------------------------------------------------------------------
# send() — success response
# ---------------------------------------------------------------------------


class TestSendSuccess:
    """Verify that send() returns the parsed response dict on success."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_returns_parsed_response(self, openai_adapter):
        """send() returns the full response body as a dict."""
        # Arrange
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        result = await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert result == SUCCESS_RESPONSE
        assert result["id"] == "chatcmpl-abc123"
        assert result["choices"][0]["message"]["content"] == "Hello!"

    def test_normalize_response_extracts_text_tool_calls_and_reasoning(self, openai_adapter):
        """Provider response is normalized to canonical assistant fields."""
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning": "Need weather.",
                        "encrypted_content": "opaque",
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":"Berlin"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }

        normalized = openai_adapter.normalize_response(response)

        assert normalized == {
            "role": "assistant",
            "content": None,
            "reasoning": "Need weather.",
            "reasoning_meta": {"encrypted_content": "opaque"},
            "tool_calls": [
                {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
            ],
        }

    def test_normalize_response_drops_tool_call_for_malformed_tool_json(self, openai_adapter):
        """Malformed provider tool-call JSON is ignored instead of becoming fake empty arguments."""
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": "{not-json}",
                                },
                            }
                        ],
                    }
                }
            ]
        }

        normalized = openai_adapter.normalize_response(response)

        assert normalized["tool_calls"] is None

    def test_normalize_response_keeps_valid_tool_calls_when_one_is_malformed(
        self,
        openai_adapter,
    ):
        """Malformed tool-call JSON does not suppress valid sibling tool calls."""
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_bad",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":',
                                },
                            },
                            {
                                "id": "call_ok",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"README.md"}',
                                },
                            },
                        ],
                    }
                }
            ]
        }

        normalized = openai_adapter.normalize_response(response)

        assert normalized["tool_calls"] == [
            {
                "id": "call_ok",
                "name": "read_file",
                "arguments": {"path": "README.md"},
            }
        ]

    def test_normalize_response_preserves_openrouter_reasoning_details(self, openrouter_adapter):
        """OpenRouter opaque reasoning_details are preserved unchanged."""
        reasoning_details = [{"type": "reasoning.text", "text": "opaque"}]
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Done",
                        "reasoning_content": "Visible reasoning",
                        "reasoning_details": reasoning_details,
                    }
                }
            ]
        }

        normalized = openrouter_adapter.normalize_response(response)

        assert normalized["content"] == "Done"
        assert normalized["reasoning"] == "Visible reasoning"
        assert normalized["reasoning_meta"] == {"reasoning_details": reasoning_details}


# ---------------------------------------------------------------------------
# normalize_response() — usage extraction
# ---------------------------------------------------------------------------


class TestNormalizeResponseUsage:
    """Verify that normalize_response extracts token usage from OpenAI responses."""

    def test_usage_included_when_both_token_fields_present(self, openai_adapter):
        """usage is present when both prompt_tokens and completion_tokens are provided."""
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hi there",
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 42,
                "completion_tokens": 13,
                "total_tokens": 55,
            },
        }

        normalized = openai_adapter.normalize_response(response)

        assert normalized["usage"] == {"input_tokens": 42, "output_tokens": 13}

    def test_usage_included_when_only_prompt_tokens_present(self, openai_adapter):
        """usage is present with output_tokens defaulting to 0 when only prompt_tokens is given."""
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hi",
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 100,
            },
        }

        normalized = openai_adapter.normalize_response(response)

        assert normalized["usage"] == {"input_tokens": 100, "output_tokens": 0}

    def test_usage_omitted_when_usage_absent(self, openai_adapter):
        """No usage key in normalized response when response has no usage object."""
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hello",
                    }
                }
            ],
        }

        normalized = openai_adapter.normalize_response(response)

        assert "usage" not in normalized

    def test_usage_omitted_when_usage_is_none(self, openai_adapter):
        """No usage key in normalized response when response.usage is null."""
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hello",
                    }
                }
            ],
            "usage": None,
        }

        normalized = openai_adapter.normalize_response(response)

        assert "usage" not in normalized

    def test_usage_omitted_when_usage_fields_are_none(self, openai_adapter):
        """No usage key when both token fields are None."""
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hello",
                    }
                }
            ],
            "usage": {
                "prompt_tokens": None,
                "completion_tokens": None,
            },
        }

        normalized = openai_adapter.normalize_response(response)

        assert "usage" not in normalized

    def test_usage_included_with_zero_tokens(self, openai_adapter):
        """usage is included when token counts are legitimately zero."""
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
        }

        normalized = openai_adapter.normalize_response(response)

        assert normalized["usage"] == {"input_tokens": 0, "output_tokens": 0}

    def test_usage_omitted_when_usage_is_wrong_type(self, openai_adapter):
        """No usage key when usage is not a dict (e.g. a string)."""
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hello",
                    }
                }
            ],
            "usage": "not-a-dict",
        }

        normalized = openai_adapter.normalize_response(response)

        assert "usage" not in normalized


# ---------------------------------------------------------------------------
# send() — error classification
# ---------------------------------------------------------------------------


class TestSendErrorClassification:
    """Verify that send() raises the correct error type for each HTTP status."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_401_raises_provider_auth_error(self, openai_adapter):
        """HTTP 401 raises ProviderAuthError (not retryable)."""
        # Arrange
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(401, text="Invalid API key"))

        # Act / Assert
        with pytest.raises(ProviderAuthError, match="401"):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_403_raises_provider_auth_error(self, openai_adapter):
        """HTTP 403 raises ProviderAuthError (not retryable)."""
        # Arrange
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(403, text="Forbidden"))

        # Act / Assert
        with pytest.raises(ProviderAuthError, match="403"):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_429_raises_provider_rate_limit_error(self, openai_adapter):
        """HTTP 429 raises ProviderRateLimitError (retryable), retried then raised."""
        # Arrange — 4 requests: 3 retries + 1 final that also fails
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(429, text="Rate limited"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderRateLimitError, match="429"),
        ):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_timeout_raises_provider_timeout_error(self, openai_adapter):
        """Connection timeout raises ProviderTimeoutError."""
        # Arrange
        respx.post(OPENAI_URL).mock(side_effect=httpx.TimeoutException("timed out"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderTimeoutError, match="timed out"),
        ):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_connect_error_raises_network_error(self, openai_adapter):
        """Connection failures raise NetworkError."""
        # Arrange
        respx.post(OPENAI_URL).mock(side_effect=httpx.ConnectError("connection failed"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(NetworkError, match="Connection failed: connection failed"),
        ):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_500_raises_non_retryable_provider_error(self, openai_adapter):
        """HTTP 500 raises ProviderError with retryable=False (not in retryable set)."""
        # Arrange
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(500, text="Internal Server Error"))

        # Act / Assert
        with pytest.raises(ProviderError) as exc_info:
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        assert exc_info.value.retryable is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_502_raises_retryable_provider_error(self, openai_adapter):
        """HTTP 502 raises ProviderError with retryable=True."""
        # Arrange — all retries fail
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(502, text="Bad Gateway"))

        # Act / Assert
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ProviderError) as exc_info:
                await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

            assert exc_info.value.retryable is True


# ---------------------------------------------------------------------------
# send() — retry behaviour
# ---------------------------------------------------------------------------


class TestSendRetry:
    """Verify that send() retries on retryable errors and not on auth errors."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_429_then_succeeds(self, openai_adapter):
        """send() retries on 429 and succeeds when the next attempt returns 200."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(429, text="Rate limited"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_502_then_succeeds(self, openai_adapter):
        """send() retries on 502 and succeeds when the next attempt returns 200."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(502, text="Bad Gateway"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_503_then_succeeds(self, openai_adapter):
        """send() retries on 503 and succeeds when the next attempt returns 200."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(503, text="Service Unavailable"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_no_retry_on_401(self, openai_adapter):
        """send() raises ProviderAuthError immediately on 401 — no retry."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(401, text="Unauthorized"))

        # Act / Assert
        with pytest.raises(ProviderAuthError):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_no_retry_on_403(self, openai_adapter):
        """send() raises ProviderAuthError immediately on 403 — no retry."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(403, text="Forbidden"))

        # Act / Assert
        with pytest.raises(ProviderAuthError):
            await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retry_on_timeout_then_succeeds(self, openai_adapter):
        """send() retries on timeout and succeeds on the next attempt."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(
            side_effect=[
                httpx.TimeoutException("Connection timed out"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_multiple_retries_then_success(self, openai_adapter):
        """send() retries up to 3 times on consecutive 429s before success."""
        # Arrange — 3 rate-limited responses, then success on 4th attempt
        route = respx.post(OPENAI_URL).mock(
            side_effect=[
                httpx.Response(429, text="Rate limited"),
                httpx.Response(429, text="Rate limited"),
                httpx.Response(429, text="Rate limited"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 4  # 3 retries + 1 initial = 4 total


# ---------------------------------------------------------------------------
# send() — provider config integration
# ---------------------------------------------------------------------------


class TestSendProviderConfig:
    """Verify that provider config values are correctly used."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_uses_base_url_from_config(self, openrouter_adapter):
        """The request goes to the base_url specified in ProviderConfig."""
        # Arrange
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await openrouter_adapter.send(SAMPLE_MESSAGES, model_id="openai/gpt-5.2")

        # Assert
        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_uses_auth_from_config(self):
        """Config with prefix='' sends the key directly in the auth header."""
        # Arrange
        adapter = OpenAICompatibleAdapter(NO_DEFAULTS_CONFIG, API_KEY)
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="minimal-model")

        # Assert
        api_key_header = route.calls.last.request.headers.get("x-api-key")
        assert api_key_header == API_KEY  # No "Bearer " prefix


# ---------------------------------------------------------------------------
# stream() — SSE parsing
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
        with pytest.raises(NetworkError, match=r"Stream ended without \[DONE\] marker"):
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
        with pytest.raises(ProviderError, match="Provider stream error: quota exceeded"):
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
        with pytest.raises(ProviderError, match="malformed JSON") as exc_info:
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
    async def test_stream_yields_index_keyed_tool_call_deltas_with_stable_ids(
        self,
        openai_adapter,
    ):
        """Tool calls are normalized by index and get stable IDs when providers omit IDs."""
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
                "id": "tool_call_0",
                "name_delta": "get_weather",
                "arguments_delta": '{"city"',
            },
            {
                "type": "tool_call_delta",
                "id": "tool_call_0",
                "name_delta": "",
                "arguments_delta": ':"Berlin"}',
            },
            {"type": "finish", "reason": "tool_calls"},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_preserves_provider_tool_call_ids(self, openai_adapter):
        """Provider-supplied tool call IDs are reused for later index-only fragments."""
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
                "id": "call_provider",
                "name_delta": "read_file",
                "arguments_delta": "",
            },
            {
                "type": "tool_call_delta",
                "id": "call_provider",
                "name_delta": "",
                "arguments_delta": '{"path":"README.md"}',
            },
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_normalizes_unknown_finish_reason_from_pending_tool_calls(
        self,
        openai_adapter,
    ):
        """Unknown finish reasons become tool_calls when a tool call was seen."""
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
        assert chunks[-1] == {"type": "finish", "reason": "tool_calls"}

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
    async def test_stream_ignores_non_data_lines(self, openai_adapter):
        """stream() skips lines that don't start with 'data: '."""
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
        assert len(chunks) == 1
        assert chunks[0] == {"type": "content_delta", "text": "A"}

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
            pytest.raises(ProviderTimeoutError, match="timed out"),
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
            pytest.raises(NetworkError, match="Connection failed: connection failed"),
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
            pytest.raises(NetworkError, match="Stream read failed: connection reset"),
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
            pytest.raises(ProviderTimeoutError, match="stream timed out"),
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
    async def test_stream_yields_usage_delta_with_zero_completion_tokens(self, openai_adapter):
        """Usage with prompt_tokens but no completion_tokens defaults output_tokens to 0."""
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
            {"type": "usage", "input_tokens": 100, "output_tokens": 0},
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
    async def test_stream_no_usage_delta_when_prompt_tokens_is_null(self, openai_adapter):
        """A chunk with usage where prompt_tokens is null does not yield a usage delta."""
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
        assert all(c["type"] != "usage" for c in chunks)

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_no_usage_delta_when_prompt_tokens_missing(self, openai_adapter):
        """A chunk with usage but no prompt_tokens field does not yield a usage delta."""
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
        assert all(c["type"] != "usage" for c in chunks)

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


class TestNormalizeCatalogEntry:
    """Verify generic OpenAI-compatible catalog normalization."""

    def test_standard_fields_map_to_model(self):
        raw_model = {
            "id": "gpt-4.1",
            "name": "GPT-4.1",
            "context_window": 1047576,
            "max_output_tokens": 32768,
            "supported_parameters": ["response_format", "reasoning_effort"],
            "input_modalities": ["text", "image"],
            "output_modalities": ["text", "image"],
        }

        model = OpenAICompatibleAdapter.normalize_catalog_entry(raw_model, {"max_tokens": 8192})

        assert model.model_id == "gpt-4.1"
        assert model.name == "GPT-4.1"
        assert model.context_window == 1047576
        assert model.max_output_tokens == 32768
        assert model.capabilities.vision is True
        assert model.capabilities.tools is True
        assert model.capabilities.json_mode is True
        assert model.capabilities.reasoning.supported is True
        assert model.capabilities.input_modalities == ("text", "image")
        assert model.capabilities.output_modalities == ("text", "image")
        assert model.capabilities.supported_parameters == (
            "reasoning_effort",
            "response_format",
        )
        assert "image_generation" in model.capabilities.task_types

    def test_missing_optional_fields_preserve_unknown_output_limit(self):
        raw_model = {"id": "minimal-model"}

        model = OpenAICompatibleAdapter.normalize_catalog_entry(raw_model, {"max_tokens": 8192})

        assert model.name == "minimal-model"
        assert model.context_window == 0
        assert model.max_output_tokens is None
        assert model.capabilities.tools is True
        assert model.capabilities.json_mode is False
        assert model.capabilities.reasoning.supported is False
        assert model.capabilities.input_modalities == ("text",)
        assert model.capabilities.output_modalities == ("text",)
        assert model.capabilities.task_types == ("chat", "text_output")
