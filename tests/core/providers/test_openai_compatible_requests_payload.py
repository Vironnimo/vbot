"""Openai compatible requests: payload behavior."""

from __future__ import annotations

from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.tool_schema import render_tool_definitions

from .openai_compatible_test_support import (
    API_KEY,
    CANONICAL_MESSAGES_WITH_TOOL_LOOP,
    HISTORY_TOOL_DESCRIPTION,
    HISTORY_TOOL_NAME,
    HISTORY_TOOL_PARAMETERS,
    MINIMAL_URL,
    NO_DEFAULTS_CONFIG,
    OPENAI_CONFIG,
    OPENAI_URL,
    READ_TOOL_DEFINITION,
    SAMPLE_MESSAGES,
    SAMPLE_TOOLS,
    SUCCESS_RESPONSE,
    Capabilities,
    Model,
    OpenAICompatibleAdapter,
    ProviderError,
    ReasoningCapabilities,
    _to_openai_user_content_part,
    httpx,
    json,
    pytest,
    respx,
)
from .openai_compatible_test_support import openai_adapter as openai_adapter
from .openai_compatible_test_support import openrouter_adapter as openrouter_adapter


def _openai_test_model(
    model_id: str,
    *,
    reasoning: bool,
    levels: tuple[str, ...] = (),
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=reasoning,
                control="levels" if levels else None,
                levels=levels,
            ),
        ),
        context_window=128000,
        max_output_tokens=4096,
    )


# send() — request format
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

    @pytest.mark.parametrize(
        "invalid_part",
        [
            {"type": "media", "base64": None, "media_type": "image/png"},
            {"type": "media", "base64": "aW1n", "media_type": None},
            {"type": "media", "base64": "aW1n", "media_type": ""},
            {"type": "media"},
        ],
    )
    def test_invalid_media_part_raises_instead_of_empty_image(self, invalid_part):
        """Malformed media parts must not silently become empty data URLs."""
        with pytest.raises(ProviderError):
            _to_openai_user_content_part(invalid_part)

    @pytest.mark.parametrize(
        ("media_type", "expected_format"),
        [
            ("audio/wav", "wav"),
            ("audio/mpeg", "mp3"),
        ],
    )
    def test_audio_media_part_maps_to_input_audio(self, media_type, expected_format):
        """Native audio media parts translate to OpenAI input_audio parts."""
        part = {"type": "media", "base64": "YXVkaW8=", "media_type": media_type}

        result = _to_openai_user_content_part(part)

        assert result == {
            "type": "input_audio",
            "input_audio": {"data": "YXVkaW8=", "format": expected_format},
        }

    @pytest.mark.parametrize("media_type", ["audio/ogg", "video/mp4", "application/pdf"])
    def test_unsupported_media_type_part_raises(self, media_type):
        """Media types outside the supported wire set must raise, not degrade."""
        part = {"type": "media", "base64": "YXVkaW8=", "media_type": media_type}

        with pytest.raises(ProviderError):
            _to_openai_user_content_part(part)

    def test_document_part_maps_to_openai_file_part(self):
        """A canonical document block becomes a Chat Completions file part."""
        part = {
            "type": "document",
            "base64": "JVBERi0=",
            "media_type": "application/pdf",
            "filename": "report.pdf",
        }

        result = _to_openai_user_content_part(part)

        assert result == {
            "type": "file",
            "file": {
                "filename": "report.pdf",
                "file_data": "data:application/pdf;base64,JVBERi0=",
            },
        }

    @pytest.mark.parametrize(
        "part",
        [
            {
                "type": "document",
                "base64": None,
                "media_type": "application/pdf",
                "filename": "r.pdf",
            },
            {"type": "document", "base64": "JVBERi0=", "media_type": "", "filename": "r.pdf"},
            {
                "type": "document",
                "base64": "JVBERi0=",
                "media_type": "application/pdf",
                "filename": "",
            },
        ],
    )
    def test_invalid_document_part_raises(self, part):
        """Malformed document parts must not reach the wire as partial file parts."""
        with pytest.raises(ProviderError):
            _to_openai_user_content_part(part)

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
                    "strict": False,
                },
            }
        ]
        assert request_body["reasoning_effort"] == "high"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_uses_request_only_user_fallback_for_rich_tool_result(
        self,
        openai_adapter,
    ):
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
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

        await openai_adapter.send(messages, model_id="gpt-5.2")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["messages"] == [
            {
                "role": "tool",
                "tool_call_id": "call_image",
                "content": '{"ok":true}\n\n[Image path: C:/diagram.png]',
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                    }
                ],
            },
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_read_definition_to_function_tool(self, openai_adapter):
        """The compact read definition maps to OpenAI function tools."""
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", tools=[READ_TOOL_DEFINITION])

        request_body = json.loads(route.calls.last.request.content)
        rendered = render_tool_definitions(
            [READ_TOOL_DEFINITION],
            profile="explicit_non_strict",
        )[0]
        assert request_body["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": READ_TOOL_DEFINITION["description"],
                    "parameters": rendered["parameters"],
                    "strict": False,
                },
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_preserves_nullable_optional_and_disables_strict(self, openai_adapter):
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        definition = {
            "name": "inspect",
            "description": "Inspect one key.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "note": {"type": ["string", "null"]},
                },
                "required": ["key"],
                "additionalProperties": False,
            },
        }

        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", tools=[definition])

        function = json.loads(route.calls.last.request.content)["tools"][0]["function"]
        assert function["strict"] is False
        assert function["parameters"]["required"] == ["key"]
        assert function["parameters"]["properties"]["note"]["type"] == ["string", "null"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_history_definition_without_special_case(self, openai_adapter):
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        definition = {
            "name": HISTORY_TOOL_NAME,
            "description": HISTORY_TOOL_DESCRIPTION,
            "parameters": HISTORY_TOOL_PARAMETERS,
        }

        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", tools=[definition])

        request_body = json.loads(route.calls.last.request.content)
        rendered = render_tool_definitions([definition], profile="explicit_non_strict")[0]
        assert request_body["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": rendered["name"],
                    "description": rendered["description"],
                    "parameters": rendered["parameters"],
                    "strict": False,
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
    @pytest.mark.parametrize(
        ("thinking_effort", "expected_reasoning_effort"),
        [("low", "high"), ("medium", "high"), ("max", "xhigh")],
    )
    async def test_send_snaps_against_effective_model_ladder(
        self,
        thinking_effort,
        expected_reasoning_effort,
    ):
        """Snapping follows the per-model feed ladder, not the adapter constant.

        A model whose effective ladder is ``[high, xhigh]`` snaps ``low``/``medium``
        up to ``high`` and ``max`` to ``xhigh`` — values the hardcoded
        ``OPENAI_REASONING_EFFORTS`` (``low``/``medium``/``high``) cannot reach.
        """
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        adapter = OpenAICompatibleAdapter(
            OPENAI_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _openai_test_model(
                model_id, reasoning=True, levels=("high", "xhigh")
            ),
        )

        await adapter.send(
            SAMPLE_MESSAGES,
            model_id="gpt-5.2",
            thinking_effort=thinking_effort,
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["reasoning_effort"] == expected_reasoning_effort

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_falls_back_to_constant_without_feed_ladder(self):
        """A reasoning model with no feed ladder snaps against the adapter floor.

        ``xhigh`` is outside the ``OPENAI_REASONING_EFFORTS`` floor, so it must snap
        down to ``high`` — proving the constant is used when no ladder is present.
        """
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        adapter = OpenAICompatibleAdapter(
            OPENAI_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _openai_test_model(model_id, reasoning=True),
        )

        await adapter.send(
            SAMPLE_MESSAGES,
            model_id="gpt-5.2",
            thinking_effort="xhigh",
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["reasoning_effort"] == "high"

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

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_degrades_budget_control_to_effort_on_generic_wire(self):
        """A budget-control model on the generic wire degrades to a plain effort.

        The base ``/chat/completions`` wire has no native token-budget field, so a
        ``budget`` model snaps the effort to the adapter floor and sends only
        ``reasoning_effort`` — never a token budget.
        """
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        budget_model = Model(
            model_id="gpt-5.2",
            name="gpt-5.2",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True, control="budget"),
            ),
            context_window=128000,
            max_output_tokens=4096,
        )
        adapter = OpenAICompatibleAdapter(
            OPENAI_CONFIG,
            API_KEY,
            model_lookup=lambda _model_id: budget_model,
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", thinking_effort="high")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["reasoning_effort"] == "high"
        assert "thinking" not in request_body
        assert "budget_tokens" not in request_body
