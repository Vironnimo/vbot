"""Tests for AnthropicAdapter.

Uses ``respx`` to mock httpx calls.  Verifies request building, header
injection, message translation, SSE streaming with Anthropic event types,
retry on retryable errors, and immediate failure on auth errors.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.anthropic import AnthropicAdapter, _to_anthropic_user_content_block
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ANTHROPIC_CONFIG = ProviderConfig(
    id="anthropic",
    name="Anthropic",
    adapter="anthropic",
    base_url="https://api.anthropic.com/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(header="x-api-key", prefix="", credential_key="ANTHROPIC_API_KEY"),
        )
    ],
    defaults={"max_tokens": 4096},
)

ANTHROPIC_MULTI_AUTH_CONFIG = ProviderConfig(
    id="anthropic",
    name="Anthropic",
    adapter="anthropic",
    base_url="https://api.anthropic.com/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(header="x-api-key", prefix="", credential_key="ANTHROPIC_API_KEY"),
        ),
        ConnectionConfig(
            id="oauth",
            type="oauth",
            label="OAuth",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="ANTHROPIC_OAUTH_TOKEN",
            ),
        ),
    ],
    defaults={"max_tokens": 4096},
)

CUSTOM_CONFIG = ProviderConfig(
    id="anthropic-custom",
    name="Anthropic Custom",
    adapter="anthropic",
    base_url="https://custom.anthropic.example/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="x-api-key",
                prefix="",
                credential_key="CUSTOM_ANTHROPIC_API_KEY",
            ),
        )
    ],
    defaults={"max_tokens": 8192, "temperature": 0.7},
    extra_headers={"X-Custom-Header": "custom-value"},
)

NO_DEFAULTS_CONFIG = ProviderConfig(
    id="anthropic-minimal",
    name="Anthropic Minimal",
    adapter="anthropic",
    base_url="https://minimal.anthropic.example/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="x-api-key",
                prefix="",
                credential_key="MINIMAL_ANTHROPIC_API_KEY",
            ),
        )
    ],
)

API_KEY = "test-anthropic-key-12345"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
CUSTOM_URL = "https://custom.anthropic.example/v1/messages"
MINIMAL_URL = "https://minimal.anthropic.example/v1/messages"

SUCCESS_RESPONSE = {
    "id": "msg_01XFDUDYJGAAC8998t2N3v",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "Hello!"}],
    "model": "claude-sonnet-4-20250219",
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 5},
}

SAMPLE_MESSAGES = [
    {"role": "user", "content": "Hello"},
]

CANONICAL_MESSAGES_WITH_TOOL_LOOP = [
    {
        "role": "system",
        "model": "anthropic/claude-sonnet-4-20250219",
        "content": "You are helpful.",
    },
    {"role": "user", "content": "Weather?"},
    {
        "role": "assistant",
        "model": "anthropic/claude-sonnet-4-20250219",
        "content": None,
        "reasoning": "Need weather.",
        "reasoning_meta": {
            "content_blocks": [
                {
                    "type": "thinking",
                    "thinking": "Need weather.",
                    "signature": "opaque-current-turn",
                }
            ]
        },
        "tool_calls": [
            {
                "id": "toolu_abc",
                "name": "get_weather",
                "arguments": {"city": "Berlin"},
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "toolu_abc",
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

SAMPLE_MESSAGES_WITH_SYSTEM = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello"},
]

MULTITURN_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {
        "role": "user",
        "content": [{"type": "text", "text": "What is 2+2?"}],
    },
    {
        "role": "assistant",
        "content": [{"type": "text", "text": "2+2 equals 4."}],
    },
    {
        "role": "user",
        "content": [{"type": "text", "text": "And 3+3?"}],
    },
]


@pytest.fixture()
def anthropic_adapter():
    """Anthropic adapter with default Anthropic config."""
    return AnthropicAdapter(ANTHROPIC_CONFIG, API_KEY)


@pytest.fixture()
def custom_adapter():
    """Anthropic adapter with custom config (extra headers, overrides)."""
    return AnthropicAdapter(CUSTOM_CONFIG, API_KEY)


def test_client_timeout_allows_long_generation_reads(anthropic_adapter):
    timeout = anthropic_adapter._client.timeout  # noqa: SLF001 - verify adapter wiring.

    assert timeout.connect == 60.0
    assert timeout.read is None
    assert timeout.write == 60.0
    assert timeout.pool == 60.0


def _anthropic_test_model(model_id: str, *, reasoning: bool) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=reasoning),
        ),
        context_window=200000,
        max_output_tokens=8192,
    )


# ---------------------------------------------------------------------------
# Constructor contract
# ---------------------------------------------------------------------------


class TestConstructorContract:
    """Verify the shared optional model_lookup constructor contract."""

    def test_constructor_defaults_model_lookup_to_none(self):
        """Constructing without model_lookup keeps _model_lookup unset (None)."""
        adapter = AnthropicAdapter(ANTHROPIC_CONFIG, API_KEY)

        assert adapter._model_lookup is None

    def test_constructor_stores_model_lookup_callable(self):
        """Constructing with model_lookup stores the callable for later adapter use."""

        def model_lookup(model_id: str):
            _ = model_id
            return None

        adapter = AnthropicAdapter(ANTHROPIC_CONFIG, API_KEY, model_lookup=model_lookup)

        assert adapter._model_lookup is model_lookup


# ---------------------------------------------------------------------------
# send() — request format
# ---------------------------------------------------------------------------


class TestSendRequestFormat:
    """Verify that send() translates messages to the correct Anthropic format."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_includes_model_and_messages(self, anthropic_adapter):
        """The request payload contains the model ID and messages."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        assert route.called
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["model"] == "claude-sonnet-4-20250219"
        assert request_body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]}
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_user_media_blocks_to_anthropic_image_source(self, anthropic_adapter):
        """Resolved media blocks map to Anthropic image base64 source blocks."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "media",
                        "base64": "iVBORw0KGgoAAAANSUhEUgAA",
                        "media_type": "image/png",
                    }
                ],
            }
        ]

        # Act
        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["messages"] == [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBORw0KGgoAAAANSUhEUgAA",
                        },
                    }
                ],
            }
        ]

    @pytest.mark.parametrize(
        "invalid_block",
        [
            {"type": "media", "base64": None, "media_type": "image/png"},
            {"type": "media", "base64": "aW1n", "media_type": None},
            {"type": "media", "base64": "aW1n", "media_type": ""},
            {"type": "media"},
        ],
    )
    def test_invalid_media_block_raises_instead_of_raw_passthrough(self, invalid_block):
        """Malformed media blocks must never reach the wire as raw dicts."""
        with pytest.raises(ProviderError, match="media content block requires"):
            _to_anthropic_user_content_block(invalid_block)

    @pytest.mark.parametrize("media_type", ["audio/wav", "audio/ogg", "video/mp4"])
    def test_non_image_media_block_raises_clear_error(self, media_type):
        """Anthropic's wire has no audio/video input; reject instead of mislabeling."""
        block = {"type": "media", "base64": "YXVkaW8=", "media_type": media_type}

        with pytest.raises(ProviderError, match="supports only image media blocks"):
            _to_anthropic_user_content_block(block)

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_user_text_blocks_to_anthropic_text_parts(self, anthropic_adapter):
        """Resolved text blocks keep Anthropic text-part wire shape."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "First line."},
                    {"type": "text", "text": "Second line."},
                ],
            }
        ]

        # Act
        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["messages"] == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "First line."},
                    {"type": "text", "text": "Second line."},
                ],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_mixed_user_blocks_in_order(self, anthropic_adapter):
        """Mixed resolved text/media blocks preserve order after conversion."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image:"},
                    {
                        "type": "media",
                        "base64": "dGVzdC1pbWFnZS1ieXRlcw==",
                        "media_type": "image/jpeg",
                    },
                    {"type": "text", "text": "Use one sentence."},
                ],
            }
        ]

        # Act
        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["messages"] == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image:"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": "dGVzdC1pbWFnZS1ieXRlcw==",
                        },
                    },
                    {"type": "text", "text": "Use one sentence."},
                ],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_keeps_string_user_content_behavior(self, anthropic_adapter):
        """String user content keeps the existing single text-block mapping."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [{"role": "user", "content": "Hello from plain text."}]

        # Act
        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["messages"] == [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Hello from plain text."}],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_extracts_system_message(self, anthropic_adapter):
        """System-role messages are extracted to the system field."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(
            SAMPLE_MESSAGES_WITH_SYSTEM,
            model_id="claude-sonnet-4-20250219",
        )

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["system"] == "You are a helpful assistant."
        for msg in request_body["messages"]:
            assert msg["role"] != "system"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_combines_multiple_system_messages(self, anthropic_adapter):
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {"role": "system", "content": "Follow the project rules."},
            {"role": "system", "content": "Keep answers concise."},
            {"role": "user", "content": "Hello"},
        ]

        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["system"] == "Follow the project rules.\n\nKeep answers concise."
        for msg in request_body["messages"]:
            assert msg["role"] != "system"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_no_system_message(self, anthropic_adapter):
        """When no system message is present, the system field is omitted."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert "system" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_applies_defaults_from_config(self, anthropic_adapter):
        """Defaults from ProviderConfig are included when not overridden."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["max_tokens"] == 4096

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_kwargs_override_defaults(self, anthropic_adapter):
        """Caller kwargs take precedence over provider defaults."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            max_tokens=8192,
        )

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["max_tokens"] == 8192  # overridden

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_without_defaults(self):
        """When config has no defaults, only model and messages are sent."""
        # Arrange
        adapter = AnthropicAdapter(NO_DEFAULTS_CONFIG, API_KEY)
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert "model" in request_body
        assert "messages" in request_body
        assert "max_tokens" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_thinking_kwargs_pass_through(self, anthropic_adapter):
        """Thinking and output_config kwargs are passed through."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        thinking = {"type": "enabled", "budget_tokens": 10000}
        output_config = {"effort": "high"}

        # Act
        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            thinking=thinking,
            output_config=output_config,
        )

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["thinking"] == thinking
        assert request_body["output_config"] == output_config

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_tool_use_content_blocks(self, anthropic_adapter):
        """Tool use content blocks are passed through correctly."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        tool_messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "What's the weather?"}],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check."},
                    {
                        "type": "tool_use",
                        "id": "toolu_01A",
                        "name": "get_weather",
                        "input": {"location": "San Francisco"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01A",
                        "content": "72°F and sunny",
                    }
                ],
            },
        ]

        # Act
        await anthropic_adapter.send(tool_messages, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert len(request_body["messages"]) == 3
        assistant_msg = request_body["messages"][1]
        assert assistant_msg["role"] == "assistant"
        assert any(block["type"] == "tool_use" for block in assistant_msg["content"])
        user_msg = request_body["messages"][2]
        assert user_msg["role"] == "user"
        assert any(block["type"] == "tool_result" for block in user_msg["content"])

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_system_content_blocks(self, anthropic_adapter):
        """System messages with content block arrays are extracted."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        system_blocks = [{"type": "text", "text": "You are a helpful assistant."}]
        messages_with_system_blocks = [
            {"role": "system", "content": system_blocks},
            {"role": "user", "content": "Hello"},
        ]

        # Act
        await anthropic_adapter.send(
            messages_with_system_blocks,
            model_id="claude-sonnet-4-20250219",
        )

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["system"] == system_blocks
        for msg in request_body["messages"]:
            assert msg["role"] != "system"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_combines_multiple_system_content_block_messages(
        self,
        anthropic_adapter,
    ):
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        first_blocks = [{"type": "text", "text": "Follow the project rules."}]
        second_blocks = [{"type": "text", "text": "Keep answers concise."}]
        messages = [
            {"role": "system", "content": first_blocks},
            {"role": "system", "content": second_blocks},
            {"role": "user", "content": "Hello"},
        ]

        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["system"] == [*first_blocks, *second_blocks]
        for msg in request_body["messages"]:
            assert msg["role"] != "system"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_combines_mixed_system_content_messages(self, anthropic_adapter):
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        blocks = [{"type": "text", "text": "Keep answers concise."}]
        messages = [
            {"role": "system", "content": "Follow the project rules."},
            {"role": "system", "content": blocks},
            {"role": "user", "content": "Hello"},
        ]

        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["system"] == [
            {"type": "text", "text": "Follow the project rules."},
            *blocks,
        ]
        for msg in request_body["messages"]:
            assert msg["role"] != "system"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_endpoint_is_messages(self, anthropic_adapter):
        """The request goes to /messages, not /chat/completions."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        assert route.called
        request = route.calls.last.request
        assert "/messages" in str(request.url)

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_canonical_messages_tools_and_reasoning(self, anthropic_adapter):
        """Canonical messages, tool definitions, and effort map to Anthropic wire format."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            CANONICAL_MESSAGES_WITH_TOOL_LOOP,
            model_id="claude-sonnet-4-20250219",
            tools=SAMPLE_TOOLS,
            thinking_effort="high",
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["system"] == "You are helpful."
        assert request_body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "Weather?"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Need weather.",
                        "signature": "opaque-current-turn",
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_abc",
                        "name": "get_weather",
                        "input": {"city": "Berlin"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": '{"temp":22}',
                    }
                ],
            },
        ]
        assert request_body["tools"] == [
            {
                "name": "get_weather",
                "description": "Get current weather",
                "input_schema": SAMPLE_TOOLS[0]["parameters"],
            }
        ]
        assert request_body["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert request_body["output_config"] == {"effort": "high"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_read_definition_to_input_schema(self, anthropic_adapter):
        """The compact read definition maps to Anthropic input_schema tools."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            tools=[READ_TOOL_DEFINITION],
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["tools"] == [
            {
                "name": "read",
                "description": READ_TOOL_DEFINITION["description"],
                "input_schema": READ_TOOL_DEFINITION["parameters"],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_groups_multiple_tool_results_in_one_user_message(self, anthropic_adapter):
        """Consecutive canonical tool messages become one Anthropic user message."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {"role": "user", "content": "Check two cities."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "toolu_a", "name": "get_weather", "arguments": {"city": "Berlin"}},
                    {"id": "toolu_b", "name": "get_weather", "arguments": {"city": "Paris"}},
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_a",
                "name": "get_weather",
                "content": '{"temp":22}',
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_b",
                "name": "get_weather",
                "content": '{"temp":19}',
            },
        ]

        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["messages"][2] == {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_a", "content": '{"temp":22}'},
                {"type": "tool_result", "tool_use_id": "toolu_b", "content": '{"temp":19}'},
            ],
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_round_trips_reasoning_meta_blocks_unchanged(self, anthropic_adapter):
        """Supported opaque reasoning blocks keep provider wire shape on resend."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        thinking_block = {
            "type": "thinking",
            "thinking": "Need weather.",
            "signature": "opaque-signature",
        }
        redacted_block = {"type": "redacted_thinking", "data": "opaque-redacted"}
        messages = [
            {"role": "user", "content": "Weather?"},
            {
                "role": "assistant",
                "content": None,
                "reasoning": "Need weather.",
                "reasoning_meta": {"content_blocks": [thinking_block, redacted_block]},
                "tool_calls": [
                    {"id": "toolu_a", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
        ]

        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["messages"][1]["content"][:2] == [thinking_block, redacted_block]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_does_not_convert_readable_reasoning_to_thinking_block(
        self,
        anthropic_adapter,
    ):
        """Readable reasoning without opaque metadata is not provider thinking."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {"role": "user", "content": "Previous question"},
            {
                "role": "assistant",
                "content": "Previous answer",
                "reasoning": "Old readable reasoning",
            },
            {"role": "user", "content": "Fresh follow-up"},
        ]

        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        request_body = json.loads(route.calls.last.request.content)
        assistant_content = request_body["messages"][1]["content"]
        assert assistant_content == [{"type": "text", "text": "Previous answer"}]
        assert all(block["type"] != "thinking" for block in assistant_content)

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_none_thinking_effort_disables_thinking(self, anthropic_adapter):
        """The vBot 'none' effort maps to Anthropic disabled thinking."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            thinking_effort="none",
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["thinking"] == {"type": "disabled"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_omits_temperature_when_thinking_effort_is_active(self, anthropic_adapter):
        """Anthropic rejects temperature alongside active thinking — drop it."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            temperature=0.5,
            thinking_effort="high",
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert "temperature" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_omits_temperature_when_raw_thinking_kwarg_is_active(
        self, anthropic_adapter
    ):
        """A raw enabled-thinking kwarg also conflicts with temperature."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            temperature=0.5,
            thinking={"type": "enabled", "budget_tokens": 10000},
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["thinking"] == {"type": "enabled", "budget_tokens": 10000}
        assert "temperature" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_skips_default_temperature_when_thinking_is_active(self):
        """The provider-default temperature must not refill the dropped kwarg."""
        route = respx.post(CUSTOM_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        adapter = AnthropicAdapter(CUSTOM_CONFIG, API_KEY)

        await adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            thinking_effort="high",
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert "temperature" not in request_body
        assert request_body["max_tokens"] == 8192

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_keeps_temperature_when_thinking_is_disabled(self, anthropic_adapter):
        """Disabled thinking does not conflict with temperature."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            temperature=0.5,
            thinking_effort="none",
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["thinking"] == {"type": "disabled"}
        assert request_body["temperature"] == 0.5

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_suppresses_reasoning_when_catalog_disables_it(self):
        """Catalog-known non-reasoning models do not receive Anthropic thinking controls."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            ANTHROPIC_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_test_model(model_id, reasoning=False),
        )

        await adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-3-5-haiku-20241022",
            thinking_effort="high",
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": "high"},
            include_reasoning=True,
        )

        request_body = json.loads(route.calls.last.request.content)
        assert "thinking" not in request_body
        assert "output_config" not in request_body
        assert "include_reasoning" not in request_body


# ---------------------------------------------------------------------------
# send() — headers and auth
# ---------------------------------------------------------------------------


class TestSendHeaders:
    """Verify that send() sends the correct auth and Anthropic headers."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_x_api_key_header(self, anthropic_adapter):
        """Anthropic config sends x-api-key header with the key directly."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        assert route.called
        api_key_header = route.calls.last.request.headers.get("x-api-key")
        assert api_key_header == API_KEY  # No "Bearer " prefix

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_anthropic_version_header(self, anthropic_adapter):
        """The anthropic-version header is sent in the request."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        version_header = route.calls.last.request.headers.get("anthropic-version")
        assert version_header == "2023-06-01"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_extra_headers(self, custom_adapter):
        """Custom config includes extra headers from provider config."""
        # Arrange
        route = respx.post(CUSTOM_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await custom_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        request = route.calls.last.request
        assert request.headers.get("x-custom-header") == "custom-value"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_no_bearer_prefix(self, anthropic_adapter):
        """Auth header does not have 'Bearer ' prefix."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        api_key_header = route.calls.last.request.headers.get("x-api-key")
        assert not api_key_header.startswith("Bearer ")
        assert api_key_header == API_KEY

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_uses_selected_connection_auth_header(self):
        """Selected connection auth metadata controls the request auth header."""
        # Arrange
        selected_connection = ANTHROPIC_MULTI_AUTH_CONFIG.get_connection("oauth")
        adapter = AnthropicAdapter(
            ANTHROPIC_MULTI_AUTH_CONFIG,
            API_KEY,
            auth_config=selected_connection.auth,
        )
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        request_headers = route.calls.last.request.headers
        assert request_headers.get("authorization") == f"Bearer {API_KEY}"
        assert request_headers.get("x-api-key") is None


# ---------------------------------------------------------------------------
# send() — success response
# ---------------------------------------------------------------------------


class TestSendSuccess:
    """Verify that send() returns the parsed response dict on success."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_returns_parsed_response(self, anthropic_adapter):
        """send() returns the full response body as a dict."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        result = await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        assert result == SUCCESS_RESPONSE
        assert result["id"] == "msg_01XFDUDYJGAAC8998t2N3v"
        assert result["content"][0]["text"] == "Hello!"

    def test_normalize_response_extracts_text_tool_calls_and_reasoning(self, anthropic_adapter):
        """Anthropic response blocks normalize to canonical assistant fields."""
        response = {
            "content": [
                {"type": "thinking", "thinking": "Need weather.", "signature": "opaque"},
                {"type": "text", "text": "Checking."},
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "get_weather",
                    "input": {"city": "Berlin"},
                },
            ]
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert normalized == {
            "role": "assistant",
            "content": "Checking.",
            "reasoning": "Need weather.",
            "reasoning_meta": {
                "content_blocks": [
                    {"type": "thinking", "thinking": "Need weather.", "signature": "opaque"}
                ]
            },
            "tool_calls": [
                {"id": "toolu_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
            ],
        }

    def test_normalize_response_preserves_redacted_thinking_block(self, anthropic_adapter):
        """Opaque redacted thinking metadata is preserved unchanged."""
        redacted_block = {"type": "redacted_thinking", "data": "opaque"}
        response = {
            "content": [
                {
                    "type": "thinking",
                    "thinking": "Visible reasoning",
                    "signature": "opaque-signature",
                },
                redacted_block,
            ]
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert normalized["reasoning"] == "Visible reasoning"
        assert normalized["reasoning_meta"] == {
            "content_blocks": [
                {
                    "type": "thinking",
                    "thinking": "Visible reasoning",
                    "signature": "opaque-signature",
                },
                redacted_block,
            ]
        }

    def test_normalize_response_includes_usage_with_both_fields(self, anthropic_adapter):
        """Usage with both input and output tokens is included in normalized response."""
        response = {
            "content": [{"type": "text", "text": "Hello!"}],
            "usage": {"input_tokens": 25, "output_tokens": 87},
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert normalized["usage"] == {"input_tokens": 25, "output_tokens": 87}

    def test_normalize_response_includes_usage_with_zero_output_tokens(self, anthropic_adapter):
        """Usage with input_tokens and output_tokens=0 (cache read) is included."""
        response = {
            "content": [{"type": "text", "text": "Cached."}],
            "usage": {"input_tokens": 2589, "output_tokens": 0},
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert normalized["usage"] == {"input_tokens": 2589, "output_tokens": 0}

    def test_normalize_response_folds_cache_tokens_into_input_tokens(self, anthropic_adapter):
        """Cache read/write tokens are exposed and added onto input_tokens.

        Anthropic reports cache tokens separately from input_tokens; canonical
        input_tokens means the total prompt including cached tokens.
        """
        response = {
            "content": [{"type": "text", "text": "Hello!"}],
            "usage": {
                "input_tokens": 25,
                "output_tokens": 87,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 200,
            },
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert normalized["usage"] == {
            "input_tokens": 1225,
            "output_tokens": 87,
            "cache_read_tokens": 1000,
            "cache_write_tokens": 200,
        }

    def test_normalize_response_ignores_non_int_cache_tokens(self, anthropic_adapter):
        """Non-integer cache token values are ignored and input_tokens stays raw."""
        response = {
            "content": [{"type": "text", "text": "Hello!"}],
            "usage": {
                "input_tokens": 25,
                "output_tokens": 87,
                "cache_read_input_tokens": None,
            },
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert normalized["usage"] == {"input_tokens": 25, "output_tokens": 87}

    def test_normalize_response_omits_usage_when_absent(self, anthropic_adapter):
        """Usage key is omitted when the response has no usage object."""
        response = {
            "content": [{"type": "text", "text": "Hello!"}],
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert "usage" not in normalized

    def test_normalize_response_omits_usage_when_null(self, anthropic_adapter):
        """Usage key is omitted when the response usage is None."""
        response = {
            "content": [{"type": "text", "text": "Hello!"}],
            "usage": None,
        }

        normalized = anthropic_adapter.normalize_response(response)

        assert "usage" not in normalized


# ---------------------------------------------------------------------------
# send() — error classification
# ---------------------------------------------------------------------------


class TestSendErrorClassification:
    """Verify that send() raises the correct error type per HTTP status."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_401_raises_provider_auth_error(self, anthropic_adapter):
        """HTTP 401 raises ProviderAuthError (not retryable)."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                401,
                json={
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": "invalid x-api-key",
                    },
                },
            )
        )

        # Act / Assert
        with pytest.raises(ProviderAuthError, match="401"):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_403_raises_provider_auth_error(self, anthropic_adapter):
        """HTTP 403 raises ProviderAuthError (not retryable)."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                403,
                json={
                    "type": "error",
                    "error": {
                        "type": "permission_error",
                        "message": "Forbidden",
                    },
                },
            )
        )

        # Act / Assert
        with pytest.raises(ProviderAuthError, match="403"):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_429_raises_provider_rate_limit_error(self, anthropic_adapter):
        """HTTP 429 raises ProviderRateLimitError (retryable), retried then raised."""
        # Arrange — all retries fail
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                429,
                json={
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error",
                        "message": "Too many requests",
                    },
                },
            )
        )

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderRateLimitError, match="429"),
        ):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_timeout_raises_provider_timeout_error(self, anthropic_adapter):
        """Connection timeout raises ProviderTimeoutError."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(side_effect=httpx.TimeoutException("timed out"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderTimeoutError, match="timed out"),
        ):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_connect_error_raises_network_error(self, anthropic_adapter):
        """Connection failures raise NetworkError."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(side_effect=httpx.ConnectError("connection failed"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(NetworkError, match="Connection failed: connection failed"),
        ):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_500_raises_non_retryable_provider_error(self, anthropic_adapter):
        """HTTP 500 raises ProviderError with retryable=False."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                500,
                json={
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": "Internal server error",
                    },
                },
            )
        )

        # Act / Assert
        with pytest.raises(ProviderError) as exc_info:
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        assert exc_info.value.retryable is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_502_raises_retryable_provider_error(self, anthropic_adapter):
        """HTTP 502 raises ProviderError with retryable=True."""
        # Arrange — all retries fail
        respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(502, text="Bad Gateway"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderError) as exc_info,
        ):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        assert exc_info.value.retryable is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_529_raises_retryable_provider_error(self, anthropic_adapter):
        """HTTP 529 (Anthropic overloaded) raises retryable ProviderError."""
        # Arrange — all retries fail
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                529,
                json={
                    "type": "error",
                    "error": {
                        "type": "overloaded_error",
                        "message": "Overloaded",
                    },
                },
            )
        )

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderError) as exc_info,
        ):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        assert exc_info.value.retryable is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_read_error_raises_network_error(self, anthropic_adapter):
        """A non-streaming read failure (httpx.ReadError) is wrapped as NetworkError."""

        # Arrange
        respx.post(ANTHROPIC_URL).mock(side_effect=httpx.ReadError("connection reset"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(NetworkError, match="Connection failed: connection reset"),
        ):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_read_error_is_retried(self, anthropic_adapter):
        """A transient ReadError is retried; a subsequent success returns the response."""

        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.ReadError("connection reset"),
                httpx.Response(
                    200,
                    json={
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "model": "claude-sonnet-4-20250219",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 5, "output_tokens": 3},
                    },
                ),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await anthropic_adapter.send(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            )

        # Assert
        assert result["id"] == "msg_1"
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_remote_protocol_error_raises_network_error(self, anthropic_adapter):
        """A non-streaming RemoteProtocolError is wrapped as NetworkError."""

        # Arrange
        respx.post(ANTHROPIC_URL).mock(side_effect=httpx.RemoteProtocolError("server disconnected"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(NetworkError, match="Connection failed: server disconnected"),
        ):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_malformed_json_raises_non_retryable_provider_error(
        self,
        anthropic_adapter,
    ):
        """A 2xx response with unparseable JSON raises a non-retryable ProviderError."""

        # Arrange
        respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, text="not-valid-json{"))

        # Act / Assert
        with pytest.raises(ProviderError) as exc_info:
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        assert exc_info.value.retryable is False
        assert "malformed JSON" in str(exc_info.value)

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_parses_anthropic_error_format(self, anthropic_adapter):
        """Error messages include Anthropic's error type and message."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "max_tokens is required",
                    },
                },
            )
        )

        # Act / Assert
        with pytest.raises(ProviderError, match="invalid_request_error.*max_tokens is required"):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")


# ---------------------------------------------------------------------------
# send() — retry behaviour
# ---------------------------------------------------------------------------


class TestSendRetry:
    """Verify that send() retries on retryable errors, not on auth errors."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_429_then_succeeds(self, anthropic_adapter):
        """send() retries on 429 and succeeds on the next attempt."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.Response(429, text="Rate limited"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await anthropic_adapter.send(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            )

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_502_then_succeeds(self, anthropic_adapter):
        """send() retries on 502 and succeeds on the next attempt."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.Response(502, text="Bad Gateway"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await anthropic_adapter.send(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            )

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_503_then_succeeds(self, anthropic_adapter):
        """send() retries on 503 and succeeds on the next attempt."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.Response(503, text="Service Unavailable"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await anthropic_adapter.send(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            )

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retries_on_529_then_succeeds(self, anthropic_adapter):
        """send() retries on 529 (Anthropic overloaded) and succeeds."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.Response(529, text="Overloaded"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await anthropic_adapter.send(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            )

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_no_retry_on_401(self, anthropic_adapter):
        """send() raises ProviderAuthError immediately on 401."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )

        # Act / Assert
        with pytest.raises(ProviderAuthError):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_no_retry_on_403(self, anthropic_adapter):
        """send() raises ProviderAuthError immediately on 403."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(403, text="Forbidden"))

        # Act / Assert
        with pytest.raises(ProviderAuthError):
            await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retry_on_timeout_then_succeeds(self, anthropic_adapter):
        """send() retries on timeout and succeeds on the next attempt."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.TimeoutException("Connection timed out"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await anthropic_adapter.send(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            )

        # Assert
        assert result == SUCCESS_RESPONSE
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_multiple_retries_then_success(self, anthropic_adapter):
        """send() retries up to 3 times on consecutive 429s before success."""
        # Arrange — 3 rate-limited responses, then success on 4th attempt
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.Response(429, text="Rate limited"),
                httpx.Response(429, text="Rate limited"),
                httpx.Response(429, text="Rate limited"),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await anthropic_adapter.send(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            )

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
    async def test_send_uses_base_url_from_config(self, custom_adapter):
        """The request goes to the base_url from ProviderConfig."""
        # Arrange
        route = respx.post(CUSTOM_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await custom_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_multiple_defaults_applied(self, custom_adapter):
        """Multiple defaults from the config are applied."""
        # Arrange
        route = respx.post(CUSTOM_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await custom_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["max_tokens"] == 8192
        assert request_body["temperature"] == 0.7


# ---------------------------------------------------------------------------
# _build_payload() — None-valued caller kwargs
# ---------------------------------------------------------------------------


class TestBuildPayloadNoneKwargs:
    """``None``-valued caller kwargs are dropped, letting provider defaults win.

    Falsy-but-not-None values (e.g. ``0.0``) must survive. Explicit non-None
    values must still override the default. Covers both ``send()`` and
    ``stream()`` payload construction (both call ``_build_payload``).
    """

    @respx.mock
    @pytest.mark.asyncio
    async def test_none_kwarg_drops_key_and_provider_default_applies(self, custom_adapter):
        """``temperature=None`` is absent from the payload; default fills in."""
        # Arrange — CUSTOM_CONFIG declares defaults.temperature=0.7
        route = respx.post(CUSTOM_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await custom_adapter.send(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219", temperature=None
        )

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert "temperature" in request_body
        assert request_body["temperature"] == 0.7  # from defaults

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_zero_kwarg_survives_through_send(self, custom_adapter):
        """``temperature=0.0`` (falsy but not None) survives the None filter."""
        # Arrange
        route = respx.post(CUSTOM_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await custom_adapter.send(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219", temperature=0.0
        )

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["temperature"] == 0.0

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_nonzero_kwarg_overrides_default(self, custom_adapter):
        """Explicit non-None kwargs continue to override the provider default."""
        # Arrange
        route = respx.post(CUSTOM_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await custom_adapter.send(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219", temperature=0.3
        )

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["temperature"] == 0.3

    @respx.mock
    @pytest.mark.asyncio
    async def test_none_kwarg_drops_key_for_stream(self, custom_adapter):
        """``stream()`` also drops ``None`` caller kwargs before sending."""
        sse_body = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        route = respx.post(CUSTOM_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        async for _ in custom_adapter.stream(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219", temperature=None
        ):
            pass

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["temperature"] == 0.7  # default applied
        assert request_body["stream"] is True  # stream() still adds stream=true


# ---------------------------------------------------------------------------
# stream() — SSE parsing
# ---------------------------------------------------------------------------


class TestStreamSSE:
    """Verify that stream() correctly parses Anthropic SSE event chunks."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_normalized_content_and_finish_deltas(self, anthropic_adapter):
        """stream() parses Anthropic SSE lines into normalized content and finish deltas."""
        # Arrange
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
        route = respx.post(ANTHROPIC_URL).mock(
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

        # Assert — 7 chunks: message_start, content_block_start,
        # 2x content_block_delta, content_block_stop, message_delta,
        # message_stop; only visible deltas and finish are yielded.
        assert chunks == [
            {"type": "content_delta", "text": "Hello"},
            {"type": "content_delta", "text": " world"},
            {"type": "finish", "reason": "stop"},
        ]
        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_accepts_multiline_sse_data_frames(self, anthropic_adapter):
        """SSE data fields may be split across multiple data lines."""
        # Arrange
        sse_body = (
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,\n'
            'data: "delta":{"type":"text_delta","text":"Hello"}}\n'
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

        # Assert
        assert chunks == [
            {"type": "content_delta", "text": "Hello"},
            {"type": "finish", "reason": "stop"},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_raises_provider_error_on_malformed_sse_json(self, anthropic_adapter):
        """Malformed SSE JSON is classified as a non-retryable provider error."""
        # Arrange
        sse_body = 'event: content_block_delta\ndata: {"type":\n\n'
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act / Assert
        with pytest.raises(ProviderError, match="malformed JSON") as exc_info:
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            ):
                pass
        assert exc_info.value.retryable is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_reasoning_deltas_and_opaque_metadata(self, anthropic_adapter):
        """Thinking text streams visibly while supported thinking metadata stays opaque."""
        # Arrange
        sse_body = (
            "event: content_block_start\n"
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"thinking","thinking":""}}\n'
            "\n"
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"thinking_delta","thinking":"Need"}}\n'
            "\n"
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"thinking_delta","thinking":" weather."}}\n'
            "\n"
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"signature_delta","signature":"opaque-signature"}}\n'
            "\n"
            "event: content_block_stop\n"
            'data: {"type":"content_block_stop","index":0}\n'
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

        # Assert
        assert chunks == [
            {"type": "reasoning_delta", "text": "Need"},
            {"type": "reasoning_delta", "text": " weather."},
            {
                "type": "reasoning_meta",
                "reasoning_meta": {
                    "content_blocks": [
                        {
                            "type": "thinking",
                            "thinking": "Need weather.",
                            "signature": "opaque-signature",
                        }
                    ]
                },
            },
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_yields_tool_call_input_fragments_and_finish(self, anthropic_adapter):
        """Tool-use blocks stream name and input fragments as normalized tool deltas."""
        # Arrange
        sse_body = (
            "event: content_block_start\n"
            'data: {"type":"content_block_start","index":2,'
            '"content_block":{"type":"tool_use","id":"toolu_abc","name":"get_weather",'
            '"input":{}}}\n'
            "\n"
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":2,'
            '"delta":{"type":"input_json_delta","partial_json":"{\\"city\\""}}\n'
            "\n"
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":2,'
            '"delta":{"type":"input_json_delta","partial_json":":\\"Berlin\\"}"}}\n'
            "\n"
            "event: message_delta\n"
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n'
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

        # Assert
        assert chunks == [
            {
                "type": "tool_call_delta",
                "id": "toolu_abc",
                "name_delta": "get_weather",
                "arguments_delta": "",
            },
            {
                "type": "tool_call_delta",
                "id": "toolu_abc",
                "name_delta": "",
                "arguments_delta": '{"city"',
            },
            {
                "type": "tool_call_delta",
                "id": "toolu_abc",
                "name_delta": "",
                "arguments_delta": ':"Berlin"}',
            },
            {"type": "finish", "reason": "tool_calls"},
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_preserves_redacted_thinking_metadata(self, anthropic_adapter):
        """Redacted-thinking blocks are preserved as opaque metadata without visible deltas."""
        # Arrange
        sse_body = (
            "event: content_block_start\n"
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"redacted_thinking","data":"opaque-redacted"}}\n'
            "\n"
            "event: content_block_stop\n"
            'data: {"type":"content_block_stop","index":0}\n'
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

        # Assert
        assert chunks == [
            {
                "type": "reasoning_meta",
                "reasoning_meta": {
                    "content_blocks": [{"type": "redacted_thinking", "data": "opaque-redacted"}]
                },
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_ignores_ping_and_message_bookkeeping_events(self, anthropic_adapter):
        """Ping, message_start, and message_stop do not leak raw provider events."""
        # Arrange
        sse_body = (
            "event: ping\n"
            'data: {"type":"ping"}\n'
            "\n"
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"msg_01"}}\n'
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

        # Assert
        assert chunks == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_includes_stream_true_in_payload(self, anthropic_adapter):
        """stream() sends stream=true in the request payload."""
        # Arrange
        sse_body = (
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"msg_01"}}\n'
            "\n"
            "event: message_stop\n"
            'data: {"type":"message_stop"}\n'
            "\n"
        )
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        async for _ in anthropic_adapter.stream(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
        ):
            pass

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["stream"] is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_ignores_comment_lines(self, anthropic_adapter):
        """stream() skips comment lines, empty lines, and raw bookkeeping events."""
        # Arrange
        sse_body = (
            ": this is a comment\n"
            "\n"
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"msg_01"}}\n'
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

        # Assert
        assert chunks == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_extracts_system_message(self, anthropic_adapter):
        """stream() extracts system messages into the system field."""
        # Arrange
        sse_body = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        async for _ in anthropic_adapter.stream(
            SAMPLE_MESSAGES_WITH_SYSTEM,
            model_id="claude-sonnet-4-20250219",
        ):
            pass

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["system"] == "You are a helpful assistant."
        for msg in request_body["messages"]:
            assert msg["role"] != "system"

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_raises_network_error_on_eof_without_message_stop(self, anthropic_adapter):
        """stream() raises NetworkError when the stream ends without message_stop."""
        # Arrange
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
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n'
            "\n"
        )
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act / Assert
        with pytest.raises(NetworkError, match="Stream ended without message_stop event"):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES,
                model_id="claude-sonnet-4-20250219",
            ):
                pass

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_raises_provider_error_on_in_band_error_event(self, anthropic_adapter):
        """stream() raises ProviderError when an in-band Anthropic error event arrives."""
        # Arrange
        sse_body = (
            "event: error\n"
            'data: {"type":"error","error":{"type":"invalid_request_error","message":"bad"}}\n'
            "\n"
        )
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act / Assert
        with pytest.raises(ProviderError, match="Provider stream error: bad"):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES,
                model_id="claude-sonnet-4-20250219",
            ):
                pass

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_401_raises_provider_auth_error(self, anthropic_adapter):
        """stream() raises ProviderAuthError on 401 — no retry."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )

        # Act / Assert
        with pytest.raises(ProviderAuthError):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            ):
                pass

        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_applies_extra_headers(self, custom_adapter):
        """stream() includes extra_headers from provider config."""
        # Arrange
        sse_body = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        route = respx.post(CUSTOM_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        async for _ in custom_adapter.stream(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"):
            pass

        # Assert
        request = route.calls.last.request
        assert request.headers.get("x-custom-header") == "custom-value"

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_timeout_raises_provider_timeout_error(self, anthropic_adapter):
        """stream() raises ProviderTimeoutError on connection timeout."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(side_effect=httpx.TimeoutException("timed out"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(ProviderTimeoutError, match="timed out"),
        ):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            ):
                pass

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_connect_error_raises_network_error(self, anthropic_adapter):
        """stream() raises NetworkError on connection failures."""
        # Arrange
        respx.post(ANTHROPIC_URL).mock(side_effect=httpx.ConnectError("connection failed"))

        # Act / Assert
        with (
            patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(NetworkError, match="Connection failed: connection failed"),
        ):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            ):
                pass

    @pytest.mark.asyncio
    async def test_stream_read_error_raises_network_error(self, anthropic_adapter):
        """stream() wraps mid-stream httpx.ReadError as NetworkError."""

        request = httpx.Request("POST", ANTHROPIC_URL)

        class _BrokenLineIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise httpx.ReadError("socket closed", request=request)

        class _BrokenStreamResponse:
            status_code = 200

            def __init__(self) -> None:
                self.closed = False

            def aiter_lines(self):
                return _BrokenLineIterator()

            async def aclose(self) -> None:
                self.closed = True

        broken_response = _BrokenStreamResponse()
        with (
            patch.object(
                anthropic_adapter._client,
                "send",
                new=AsyncMock(return_value=broken_response),
            ),
            pytest.raises(NetworkError, match="Stream read failed: socket closed"),
        ):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES,
                model_id="claude-sonnet-4-20250219",
            ):
                pass

        assert broken_response.closed is True

    @pytest.mark.asyncio
    async def test_stream_raises_provider_timeout_error_on_mid_stream_timeout(
        self,
        anthropic_adapter,
    ):
        """stream() wraps mid-stream httpx.TimeoutException as ProviderTimeoutError."""

        request = httpx.Request("POST", ANTHROPIC_URL)

        class _BrokenTimeoutLineIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise httpx.TimeoutException("timed out", request=request)

        class _BrokenTimeoutStreamResponse:
            status_code = 200

            def __init__(self) -> None:
                self.closed = False

            def aiter_lines(self):
                return _BrokenTimeoutLineIterator()

            async def aclose(self) -> None:
                self.closed = True

        broken_response = _BrokenTimeoutStreamResponse()
        with (
            patch.object(
                anthropic_adapter._client,
                "send",
                new=AsyncMock(return_value=broken_response),
            ),
            pytest.raises(ProviderTimeoutError, match="timed out"),
        ):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES,
                model_id="claude-sonnet-4-20250219",
            ):
                pass

        assert broken_response.closed is True

    @pytest.mark.asyncio
    async def test_stream_raises_network_error_on_mid_stream_remote_protocol_error(
        self,
        anthropic_adapter,
    ):
        """stream() wraps mid-stream httpx.RemoteProtocolError as NetworkError (h11 disconnect)."""

        request = httpx.Request("POST", ANTHROPIC_URL)

        class _BrokenProtocolLineIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise httpx.RemoteProtocolError("server disconnected", request=request)

        class _BrokenProtocolStreamResponse:
            status_code = 200

            def __init__(self) -> None:
                self.closed = False

            def aiter_lines(self):
                return _BrokenProtocolLineIterator()

            async def aclose(self) -> None:
                self.closed = True

        broken_response = _BrokenProtocolStreamResponse()
        with (
            patch.object(
                anthropic_adapter._client,
                "send",
                new=AsyncMock(return_value=broken_response),
            ),
            pytest.raises(NetworkError, match="Stream read failed: server disconnected"),
        ):
            async for _ in anthropic_adapter.stream(
                SAMPLE_MESSAGES,
                model_id="claude-sonnet-4-20250219",
            ):
                pass

        assert broken_response.closed is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_retries_on_429_then_succeeds(self, anthropic_adapter):
        """stream() retries on 429 and succeeds on next attempt."""
        # Arrange
        sse_body = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.Response(429, text="Rate limited"),
                httpx.Response(
                    200,
                    text=sse_body,
                    headers={"content-type": "text/event-stream"},
                ),
            ]
        )

        # Act
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            chunks = []
            async for chunk in anthropic_adapter.stream(
                SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
            ):
                chunks.append(chunk)

        # Assert
        assert route.call_count == 2
        assert chunks == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_applies_anthropic_version_header(self, anthropic_adapter):
        """stream() sends the anthropic-version header."""
        # Arrange
        sse_body = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )

        # Act
        async for _ in anthropic_adapter.stream(
            SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219"
        ):
            pass

        # Assert
        version_header = route.calls.last.request.headers.get("anthropic-version")
        assert version_header == "2023-06-01"

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_thinking_kwargs_in_payload(self, anthropic_adapter):
        """stream() passes through thinking and output_config kwargs."""
        # Arrange
        sse_body = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                text=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        )
        thinking = {"type": "adaptive"}
        output_config = {"effort": "high"}

        # Act
        async for _ in anthropic_adapter.stream(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            thinking=thinking,
            output_config=output_config,
        ):
            pass

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["thinking"] == thinking
        assert request_body["output_config"] == output_config


# ---------------------------------------------------------------------------
# stream() — usage delta emission
# ---------------------------------------------------------------------------


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
    async def test_stream_no_usage_delta_without_input_tokens(self, anthropic_adapter):
        """stream() does not yield a usage delta when message_start lacks input_tokens
        even if message_delta has output_tokens."""
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

        # Assert — no usage delta without input_tokens
        usage_deltas = [c for c in chunks if c.get("type") == "usage"]
        assert len(usage_deltas) == 0

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
