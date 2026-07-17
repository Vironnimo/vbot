"""OpenAI-compatible request construction, headers, defaults, and limits."""

from __future__ import annotations

from core.providers.providers import resolve_request_output_limit
from core.utils.tokens import estimate_request_input_tokens

from .openai_compatible_test_support import (
    API_KEY,
    CANONICAL_MESSAGES_WITH_TOOL_LOOP,
    HISTORY_TOOL_DESCRIPTION,
    HISTORY_TOOL_NAME,
    HISTORY_TOOL_PARAMETERS,
    IMAGE_WIRE_MEDIA_TYPES,
    MINIMAL_URL,
    NO_DEFAULTS_CONFIG,
    OPENAI_CONFIG,
    OPENAI_MULTI_AUTH_CONFIG,
    OPENAI_URL,
    OPENROUTER_URL,
    READ_TOOL_DEFINITION,
    SAMPLE_MESSAGES,
    SAMPLE_TOOLS,
    SUCCESS_RESPONSE,
    AuthConfig,
    Capabilities,
    ConnectionConfig,
    Model,
    OpenAICompatibleAdapter,
    ProviderError,
    ReasoningCapabilities,
    _to_openai_assistant_message,
    _to_openai_user_content_part,
    httpx,
    json,
    pytest,
    replace,
    respx,
)
from .openai_compatible_test_support import openai_adapter as openai_adapter
from .openai_compatible_test_support import openrouter_adapter as openrouter_adapter


def test_client_timeout_allows_long_generation_reads(openai_adapter):
    timeout = openai_adapter._client.timeout  # noqa: SLF001 - verify adapter wiring.

    assert timeout.connect == 60.0
    assert timeout.read is None
    assert timeout.write == 60.0
    assert timeout.pool == 60.0


def test_reasoning_replay_policy_stays_current_run(openai_adapter):
    """Deliberate Phase-3 choice: the generic wire keeps the conservative default."""
    assert openai_adapter.reasoning_replay_policy("gpt-4o") == "current_run"


def test_wire_media_support_is_images_plus_openai_audio(openai_adapter):
    """The generic OpenAI-compatible wire carries images plus WAV/MP3 — no PDF.

    Generic providers (OpenRouter, MiniMax, OpenCode-Go, Mistral) inherit this set.
    """
    supported = openai_adapter.wire_media_support("gpt-4o")

    assert supported == IMAGE_WIRE_MEDIA_TYPES | frozenset({"audio/wav", "audio/mpeg"})
    assert "application/pdf" not in supported


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
        with pytest.raises(ProviderError, match="media content block requires"):
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

        with pytest.raises(ProviderError, match="unsupported media type"):
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
        with pytest.raises(ProviderError, match="document content block requires"):
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
    async def test_send_maps_history_definition_without_special_case(self, openai_adapter):
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        definition = {
            "name": HISTORY_TOOL_NAME,
            "description": HISTORY_TOOL_DESCRIPTION,
            "parameters": HISTORY_TOOL_PARAMETERS,
        }

        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", tools=[definition])

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["tools"] == [{"type": "function", "function": definition}]

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
    async def test_send_keyless_connection_omits_auth_header(self):
        """A none connection sends no auth header while preserving extra headers."""
        # Arrange
        keyless_config = replace(
            NO_DEFAULTS_CONFIG,
            connections=[
                ConnectionConfig(
                    id="local",
                    type="none",
                    label="Local",
                    auth=AuthConfig(header="", prefix="", credential_key=""),
                )
            ],
            extra_headers={"X-Client": "vBot"},
        )
        adapter = OpenAICompatibleAdapter(keyless_config, "")
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="minimal-model")

        # Assert
        request_headers = route.calls.last.request.headers
        assert request_headers.get("authorization") is None
        assert request_headers["x-client"] == "vBot"

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
    async def test_none_kwarg_drops_key_and_provider_default_applies(self, openai_adapter):
        """``temperature=None`` is absent from the payload; default fills in."""
        # Arrange — OPENAI_CONFIG declares defaults.temperature=0.7
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", temperature=None)

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert "temperature" in request_body
        assert request_body["temperature"] == 0.7  # from defaults

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_zero_kwarg_survives_through_send(self, openai_adapter):
        """``temperature=0.0`` (falsy but not None) survives the None filter."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", temperature=0.0)

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["temperature"] == 0.0

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_nonzero_kwarg_overrides_default(self, openai_adapter):
        """Explicit non-None kwargs continue to override the provider default."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", temperature=0.3)

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["temperature"] == 0.3

    @respx.mock
    @pytest.mark.asyncio
    async def test_none_kwarg_drops_key_for_stream(self, openai_adapter):
        """``stream()`` also drops ``None`` caller kwargs before sending."""
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
        )
        route = respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2", temperature=None):
            pass

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["temperature"] == 0.7  # default applied
        assert "stream" in request_body  # stream() still adds stream=true


# ---------------------------------------------------------------------------
# stream() — SSE parsing
# ---------------------------------------------------------------------------


def _model_with_output_ceiling(
    model_id: str,
    ceiling: int | None,
    *,
    context_window: int = 200_000,
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=context_window,
        max_output_tokens=ceiling,
    )


class TestOutputLimitDefault:
    """The output allowance defaults to the model's catalog ceiling.

    Sibling of the Anthropic adapter's ceiling-aware ``max_tokens``: the flat
    provider-config ``max_tokens`` default (e.g. 4096/8192) truncates any model
    whose real ceiling is higher, so an unspecified allowance defaults to the
    catalog ``max_output_tokens`` instead.
    """

    @respx.mock
    @pytest.mark.asyncio
    async def test_defaults_max_tokens_to_model_ceiling_over_config_default(self):
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        adapter = OpenAICompatibleAdapter(
            OPENAI_CONFIG,  # config default max_tokens=4096
            API_KEY,
            model_lookup=lambda model_id: _model_with_output_ceiling(model_id, 128_000),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["max_tokens"] == 128_000

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_caller_limit_wins_over_ceiling(self):
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        adapter = OpenAICompatibleAdapter(
            OPENAI_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _model_with_output_ceiling(model_id, 128_000),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", max_tokens=512)

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["max_tokens"] == 512

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_max_completion_tokens_suppresses_ceiling_default(self):
        """A caller output limit under any accepted key suppresses the ceiling inject."""
        adapter = OpenAICompatibleAdapter(
            NO_DEFAULTS_CONFIG,  # no config max_tokens fallback to muddy the assertion
            API_KEY,
            model_lookup=lambda model_id: _model_with_output_ceiling(model_id, 128_000),
        )
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="minimal-model", max_completion_tokens=777)

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["max_completion_tokens"] == 777
        assert "max_tokens" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_unknown_ceiling_keeps_config_default(self):
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        adapter = OpenAICompatibleAdapter(
            OPENAI_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _model_with_output_ceiling(model_id, None),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["max_tokens"] == 4096

    @respx.mock
    @pytest.mark.asyncio
    async def test_equal_context_and_output_ceiling_leaves_room_for_nemo_request_input(self):
        """Regression: Nemo's 256K output ceiling must not consume its whole context."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {"role": "system", "content": "You are a concise assistant."},
            {"role": "user", "content": "x" * 8_000},
        ]
        tools = [
            {
                "name": "large_tool",
                "description": "y" * 24_000,
                "parameters": {"type": "object", "properties": {}},
            }
        ]
        adapter = OpenAICompatibleAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _model_with_output_ceiling(
                model_id,
                256_000,
                context_window=256_000,
            ),
        )

        await adapter.send(messages, model_id="nvidia/nemotron-nano-9b-v2:free", tools=tools)

        request_body = json.loads(route.calls.last.request.content)
        estimated_input, _ = estimate_request_input_tokens(messages, tools)
        expected = resolve_request_output_limit(
            explicit_limit=None,
            model_output_limit=256_000,
            provider_default=None,
            effective_context_window=256_000,
            estimated_input_tokens=estimated_input,
        )
        assert request_body["max_tokens"] == expected
        assert 0 < request_body["max_tokens"] < 256_000
