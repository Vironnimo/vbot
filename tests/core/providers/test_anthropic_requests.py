"""Anthropic request construction and model-policy tests."""

from __future__ import annotations

from dataclasses import replace

from core.providers._http_shared import PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.tool_schema import render_tool_definitions

from .anthropic_test_support import (
    ANTHROPIC_CONFIG,
    ANTHROPIC_URL,
    API_KEY,
    CANONICAL_MESSAGES_WITH_TOOL_LOOP,
    CUSTOM_CONFIG,
    CUSTOM_URL,
    HISTORY_TOOL_DESCRIPTION,
    HISTORY_TOOL_NAME,
    HISTORY_TOOL_PARAMETERS,
    IMAGE_WIRE_MEDIA_TYPES,
    MINIMAL_URL,
    NO_DEFAULTS_CONFIG,
    READ_TOOL_DEFINITION,
    SAMPLE_MESSAGES,
    SAMPLE_MESSAGES_WITH_SYSTEM,
    SAMPLE_TOOLS,
    SUCCESS_RESPONSE,
    AnthropicAdapter,
    AnthropicCompatibleAdapter,
    AsyncMock,
    ProviderError,
    _anthropic_control_model,
    _anthropic_test_model,
    _strip_cache_control,
    _to_anthropic_user_content_block,
    httpx,
    json,
    pytest,
    respx,
)
from .anthropic_test_support import anthropic_adapter as anthropic_adapter
from .anthropic_test_support import custom_adapter as custom_adapter


def test_native_anthropic_uses_reusable_compatible_adapter() -> None:
    assert issubclass(AnthropicAdapter, AnthropicCompatibleAdapter)


def test_public_package_exports_anthropic_compatible_adapter() -> None:
    from core.providers import AnthropicCompatibleAdapter as PublicAnthropicCompatibleAdapter

    assert PublicAnthropicCompatibleAdapter is AnthropicCompatibleAdapter


@pytest.mark.asyncio
async def test_compatible_defaults_do_not_leak_native_anthropic_policy() -> None:
    adapter = AnthropicCompatibleAdapter(NO_DEFAULTS_CONFIG, API_KEY)
    try:
        payload = adapter._build_payload(
            [{"role": "user", "content": "hello"}],
            model_id="compatible-model",
        )

        assert adapter.wire_media_support("compatible-model") == IMAGE_WIRE_MEDIA_TYPES
        assert "cache_control" not in payload["messages"][-1]["content"][-1]
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_compatible_borrows_transport_and_allows_version_header_opt_out() -> None:
    borrowed_client = AsyncMock()
    adapter = AnthropicCompatibleAdapter(
        NO_DEFAULTS_CONFIG,
        API_KEY,
        client=borrowed_client,
        api_version=None,
    )

    headers = await adapter._build_headers()
    await adapter.aclose()

    assert headers["x-api-key"] == API_KEY
    assert "anthropic-version" not in headers
    borrowed_client.aclose.assert_not_awaited()


def test_client_timeout_bounds_non_streaming_generation_reads(anthropic_adapter):
    timeout = anthropic_adapter._client.timeout  # noqa: SLF001 - verify adapter wiring.

    assert timeout.connect == 60.0
    assert timeout.read == PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS
    assert timeout.write == 60.0
    assert timeout.pool == 60.0


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
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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

    def test_document_block_maps_to_anthropic_document_part(self):
        """A canonical document block becomes an Anthropic base64 document block."""
        block = {
            "type": "document",
            "base64": "JVBERi0=",
            "media_type": "application/pdf",
            "filename": "report.pdf",
        }

        result = _to_anthropic_user_content_block(block)

        assert result == {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": "JVBERi0=",
            },
        }

    @pytest.mark.parametrize(
        "block",
        [
            {"type": "document", "base64": None, "media_type": "application/pdf"},
            {"type": "document", "base64": "JVBERi0=", "media_type": ""},
        ],
    )
    def test_invalid_document_block_raises(self, block):
        """Malformed document blocks must never reach the wire as raw dicts."""
        with pytest.raises(ProviderError, match="document content block requires"):
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
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["system"] == [{"type": "text", "text": "You are a helpful assistant."}]
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

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["system"] == [
            {"type": "text", "text": "Follow the project rules.\n\nKeep answers concise."}
        ]
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
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["max_tokens"] == 4096

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_kwargs_override_defaults_with_context_safety(self, anthropic_adapter):
        """Caller kwargs win, then clamp against the conservative unknown-model window."""
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
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert 4096 < request_body["max_tokens"] < 8192

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
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert len(request_body["messages"]) == 3
        assistant_msg = request_body["messages"][1]
        assert assistant_msg["role"] == "assistant"
        assert any(block["type"] == "tool_use" for block in assistant_msg["content"])
        user_msg = request_body["messages"][2]
        assert user_msg["role"] == "user"
        assert any(block["type"] == "tool_result" for block in user_msg["content"])

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_renders_image_inside_native_tool_result(self, anthropic_adapter):
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {
                "role": "tool",
                "tool_call_id": "toolu_image",
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

        await anthropic_adapter.send(
            messages,
            model_id="claude-sonnet-4-20250219",
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        tool_result = request_body["messages"][0]["content"][0]
        assert tool_result == {
            "type": "tool_result",
            "tool_use_id": "toolu_image",
            "content": [
                {"type": "text", "text": '{"ok":true}'},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "aW1hZ2U=",
                    },
                },
                {"type": "text", "text": "[Image path: C:/diagram.png]"},
            ],
        }

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
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["system"] == [{"type": "text", "text": "You are helpful."}]
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

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        rendered = render_tool_definitions(
            [READ_TOOL_DEFINITION],
            profile="omit_strict",
        )[0]
        assert request_body["tools"] == [
            {
                "name": "read",
                "description": READ_TOOL_DEFINITION["description"],
                "input_schema": rendered["parameters"],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_history_definition_to_input_schema(self, anthropic_adapter):
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            tools=[
                {
                    "name": HISTORY_TOOL_NAME,
                    "description": HISTORY_TOOL_DESCRIPTION,
                    "parameters": HISTORY_TOOL_PARAMETERS,
                }
            ],
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        definition = {
            "name": HISTORY_TOOL_NAME,
            "description": HISTORY_TOOL_DESCRIPTION,
            "parameters": HISTORY_TOOL_PARAMETERS,
        }
        rendered = render_tool_definitions([definition], profile="omit_strict")[0]
        assert request_body["tools"] == [
            {
                "name": HISTORY_TOOL_NAME,
                "description": HISTORY_TOOL_DESCRIPTION,
                "input_schema": rendered["parameters"],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_preserves_tool_input_schema(self, anthropic_adapter):
        """A nullable-union schema keeps its canonical meaning on the wire."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        tool = {
            "name": "search",
            "description": "Search records",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "tag": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["query"],
            },
        }

        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            tools=[tool],
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["tools"][0]["input_schema"]["properties"]["tag"] == {
            "anyOf": [{"type": "string"}, {"type": "null"}]
        }

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

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["messages"][2] == {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_a", "content": '{"temp":22}'},
                {"type": "tool_result", "tool_use_id": "toolu_b", "content": '{"temp":19}'},
            ],
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_marks_only_failed_canonical_tool_results_as_native_errors(
        self,
        anthropic_adapter,
    ):
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        success = json.dumps(
            {"ok": True, "error": None, "data": {"value": 1}, "artifacts": []},
            separators=(",", ":"),
        )
        failure = json.dumps(
            {
                "ok": False,
                "error": {"code": "lookup_failed", "message": "Lookup failed."},
                "data": None,
                "artifacts": [],
            },
            separators=(",", ":"),
        )
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "toolu_ok", "name": "lookup", "arguments": {}},
                    {"id": "toolu_failed", "name": "lookup", "arguments": {}},
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_ok", "content": success},
            {"role": "tool", "tool_call_id": "toolu_failed", "content": failure},
        ]

        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        result_blocks = request_body["messages"][1]["content"]
        assert result_blocks == [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_ok",
                "content": success,
            },
            {
                "type": "tool_result",
                "tool_use_id": "toolu_failed",
                "content": failure,
                "is_error": True,
            },
        ]

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

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert "temperature" not in request_body
        assert 0 < request_body["max_tokens"] < 8192

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

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
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

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert "thinking" not in request_body
        assert "output_config" not in request_body
        assert "include_reasoning" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_budget_model_sends_native_budget_tokens(self):
        """A budget-control Claude sends native ``thinking.budget_tokens`` from effort."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(model_id, control="budget"),
        )

        await adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-opus-4-1",
            temperature=0.5,
            thinking_effort="high",
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["thinking"] == {"type": "enabled", "budget_tokens": 16384}
        assert "output_config" not in request_body
        # Thinking is active, so temperature must be dropped.
        assert "temperature" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_budget_model_scales_with_budget_max(self):
        """A published ``budget_max`` makes the budget proportional to the effort."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(
                model_id, control="budget", budget_max=40000
            ),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-opus-4-1", thinking_effort="medium")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["thinking"] == {"type": "enabled", "budget_tokens": 20000}

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_budget_model_clamps_under_max_tokens(self):
        """The budget stays strictly under an explicit output ``max_tokens``."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            ANTHROPIC_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(model_id, control="budget"),
        )

        # An explicit small max_tokens wins over the model ceiling; the high-effort
        # budget (16384) is clamped strictly under it.
        await adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-opus-4-1",
            thinking_effort="high",
            max_tokens=4096,
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["max_tokens"] == 4096
        assert request_body["thinking"] == {"type": "enabled", "budget_tokens": 4095}

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_budget_model_disables_thinking_on_none(self):
        """A ``none`` selection disables thinking even on a budget model."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(model_id, control="budget"),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-opus-4-1", thinking_effort="none")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["thinking"] == {"type": "disabled"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_adaptive_required_model_never_sends_rejected_disabled_shape(self):
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adaptive_model = replace(
            _anthropic_control_model("claude-fable-5", control="levels"),
            metadata={
                "anthropic": {
                    "requires_adaptive_thinking": True,
                    "supports_temperature": False,
                }
            },
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda _model_id: adaptive_model,
        )

        await adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-fable-5",
            thinking_effort="none",
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert "thinking" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_on_off_model_enables_with_floor_budget(self):
        """An ``on_off`` Claude enables thinking with the floor budget."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(model_id, control="on_off"),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-opus-4-1", thinking_effort="high")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["thinking"] == {"type": "enabled", "budget_tokens": 1024}


class TestMaxTokensResolution:
    """The output ``max_tokens`` defaults to the model's catalog ceiling."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_defaults_max_tokens_to_model_ceiling(self):
        """With no caller value, ``max_tokens`` is the model's output ceiling."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(model_id, control="budget"),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-opus-4-1")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["max_tokens"] == 64000

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_explicit_max_tokens_wins_over_ceiling(self):
        """An explicit positive caller value overrides the model ceiling."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(model_id, control="budget"),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-opus-4-1", max_tokens=1234)

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["max_tokens"] == 1234

    @respx.mock
    @pytest.mark.asyncio
    async def test_context_clamp_also_bounds_reasoning_budget(self):
        """A context-clamped output allowance remains the reasoning budget's hard bound."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(
                model_id,
                control="budget",
                context_window=10_000,
                max_output_tokens=10_000,
            ),
        )
        messages = [{"role": "user", "content": "x" * 8_000}]

        await adapter.send(
            messages, model_id="claude-context-equals-output", thinking_effort="high"
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert 0 < request_body["max_tokens"] < 10_000
        assert request_body["thinking"] == {
            "type": "enabled",
            "budget_tokens": request_body["max_tokens"] - 1,
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_non_positive_max_tokens_falls_back_to_ceiling(self):
        """A non-positive caller value is ignored (it would 400) — ceiling wins."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(model_id, control="budget"),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-opus-4-1", max_tokens=0)

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["max_tokens"] == 64000

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_max_tokens_falls_back_to_config_default_without_ceiling(self):
        """When the ceiling is unknown (no lookup), the config default is used."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(ANTHROPIC_CONFIG, API_KEY)  # default max_tokens=4096, no lookup

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["max_tokens"] == 4096

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_budget_effort_keeps_output_headroom_under_ceiling(self):
        """Regression: a mid-effort budget no longer consumes the whole allowance.

        Under a flat 8K cap the ``medium`` budget (8192) was clamped to ~8191,
        leaving ~1 token for the answer. Defaulting ``max_tokens`` to the model's
        real ceiling leaves the budget intact with ample output headroom.
        """
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(model_id, control="budget"),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-opus-4-1", thinking_effort="medium")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["max_tokens"] == 64000
        assert request_body["thinking"] == {"type": "enabled", "budget_tokens": 8192}
