"""OpenAI-compatible response, usage, reasoning-field, and catalog normalization."""

from __future__ import annotations

from .openai_compatible_test_support import (
    API_KEY,
    OPENAI_URL,
    OPENROUTER_CONFIG,
    SAMPLE_MESSAGES,
    SUCCESS_RESPONSE,
    Capabilities,
    Model,
    OpenAICompatibleAdapter,
    ReasoningCapabilities,
    httpx,
    pytest,
    replace,
    respx,
)
from .openai_compatible_test_support import openai_adapter as openai_adapter
from .openai_compatible_test_support import openrouter_adapter as openrouter_adapter


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
            "terminal_outcome": "unknown",
        }

    @pytest.mark.parametrize(
        ("finish_reason", "expected_outcome"),
        [
            ("stop", "stop"),
            ("tool_calls", "tool_calls"),
            ("length", "output_truncated"),
            ("content_filter", "content_filtered"),
            ("network_error", "error"),
            ("provider_added_reason", "unknown"),
        ],
    )
    def test_normalize_response_preserves_terminal_outcome(
        self,
        openai_adapter,
        finish_reason,
        expected_outcome,
    ):
        response = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "partial"},
                    "finish_reason": finish_reason,
                }
            ]
        }

        normalized = openai_adapter.normalize_response(response)

        assert normalized["terminal_outcome"] == expected_outcome

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


def _model_with_reasoning_response_field(
    model_id: str,
    provider_metadata_key: str,
    response_field: str | None,
) -> Model:
    metadata: dict[str, object] = {}
    if response_field is not None:
        metadata = {provider_metadata_key: {"reasoning_response_field": response_field}}
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        ),
        context_window=128000,
        max_output_tokens=4096,
        metadata=metadata,
    )


class TestDataDrivenReasoningResponseField:
    """``metadata.<provider>.reasoning_response_field`` selects the response field."""

    def test_falls_back_to_default_scan_without_metadata(self) -> None:
        """No metadata field (and no model_id) → today's hardcoded default scan."""

        adapter = OpenAICompatibleAdapter(OPENROUTER_CONFIG, API_KEY)
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Done",
                        "reasoning_content": "Visible reasoning",
                    }
                }
            ]
        }

        normalized = adapter.normalize_response(response)

        assert normalized["reasoning"] == "Visible reasoning"

    def test_reasoning_content_field_drives_visible_reasoning(self) -> None:
        adapter = OpenAICompatibleAdapter(
            OPENROUTER_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _model_with_reasoning_response_field(
                model_id, "openrouter", "reasoning_content"
            ),
        )
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Done",
                        "reasoning_content": "Visible reasoning",
                    }
                }
            ]
        }

        normalized = adapter.normalize_response(response, model_id="deepseek/deepseek-v4-pro")

        assert normalized["reasoning"] == "Visible reasoning"

    def test_reasoning_details_field_surfaces_through_meta(self) -> None:
        reasoning_details = [{"type": "reasoning.text", "text": "opaque"}]
        adapter = OpenAICompatibleAdapter(
            OPENROUTER_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _model_with_reasoning_response_field(
                model_id, "openrouter", "reasoning_details"
            ),
        )
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Done",
                        "reasoning_details": reasoning_details,
                    }
                }
            ]
        }

        normalized = adapter.normalize_response(response, model_id="google/gemini-3-flash-preview")

        assert normalized["reasoning_meta"] == {"reasoning_details": reasoning_details}

    def test_custom_named_visible_field_is_preferred(self) -> None:
        """A catalog-named visible field not in the default scan still wins."""

        adapter = OpenAICompatibleAdapter(
            OPENROUTER_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _model_with_reasoning_response_field(
                model_id, "openrouter", "deep_thoughts"
            ),
        )
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Done",
                        "deep_thoughts": "Custom-field reasoning",
                    }
                }
            ]
        }

        normalized = adapter.normalize_response(response, model_id="lab/custom")

        assert normalized["reasoning"] == "Custom-field reasoning"

    def test_metadata_key_normalizes_provider_hyphens(self) -> None:
        """The metadata key uses underscores (opencode-go → opencode_go)."""

        config = replace(OPENROUTER_CONFIG, id="opencode-go")
        adapter = OpenAICompatibleAdapter(
            config,
            API_KEY,
            model_lookup=lambda model_id: _model_with_reasoning_response_field(
                model_id, "opencode_go", "reasoning_content"
            ),
        )
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Done",
                        "reasoning_content": "Visible reasoning",
                    }
                }
            ]
        }

        normalized = adapter.normalize_response(response, model_id="deepseek-v4-pro")

        assert normalized["reasoning"] == "Visible reasoning"


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

    def test_usage_preserves_reported_token_details(self, openai_adapter):
        """Prompt cache and output Reasoning subsets remain canonical Usage details."""
        response = {
            "choices": [{"message": {"role": "assistant", "content": "Hi"}}],
            "usage": {
                "prompt_tokens": 42,
                "completion_tokens": 13,
                "prompt_tokens_details": {
                    "cached_tokens": 30,
                    "cache_write_tokens": 5,
                },
                "completion_tokens_details": {"reasoning_tokens": 8},
            },
        }

        normalized = openai_adapter.normalize_response(response)

        assert normalized["usage"] == {
            "input_tokens": 42,
            "output_tokens": 13,
            "cache_read_tokens": 30,
            "cache_write_tokens": 5,
            "reasoning_tokens": 8,
        }

    def test_usage_omits_cache_read_tokens_when_cached_tokens_not_int(self, openai_adapter):
        """Non-integer cached_tokens values are ignored."""
        response = {
            "choices": [{"message": {"role": "assistant", "content": "Hi"}}],
            "usage": {
                "prompt_tokens": 42,
                "completion_tokens": 13,
                "prompt_tokens_details": {"cached_tokens": None},
            },
        }

        normalized = openai_adapter.normalize_response(response)

        assert normalized["usage"] == {"input_tokens": 42, "output_tokens": 13}

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
        # A window-less endpoint leaves context_window honestly None — no fake 0
        # masquerading as a discovered fact (Phase 6).
        assert model.context_window is None
        assert model.max_output_tokens is None
        assert model.capabilities.tools is True
        assert model.capabilities.json_mode is False
        assert model.capabilities.reasoning.supported is False
        assert model.capabilities.input_modalities == ("text",)
        assert model.capabilities.output_modalities == ("text",)
        assert model.capabilities.task_types == ("chat", "text_output")
