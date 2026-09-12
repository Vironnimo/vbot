"""Anthropic requests: reasoning behavior."""

from __future__ import annotations

from dataclasses import replace

from .anthropic_test_support import (
    ANTHROPIC_CONFIG,
    ANTHROPIC_URL,
    API_KEY,
    CUSTOM_CONFIG,
    CUSTOM_URL,
    MINIMAL_URL,
    NO_DEFAULTS_CONFIG,
    SAMPLE_MESSAGES,
    SUCCESS_RESPONSE,
    AnthropicAdapter,
    _anthropic_control_model,
    _anthropic_test_model,
    _strip_cache_control,
    httpx,
    json,
    pytest,
    respx,
)
from .anthropic_test_support import anthropic_adapter as anthropic_adapter
from .anthropic_test_support import custom_adapter as custom_adapter


class TestSendRequestFormat:
    "Verify that send() translates messages to the correct Anthropic format."

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
    async def test_send_rejected_sampling_parameter_retries_once_without_it(
        self, anthropic_adapter
    ):
        """A Messages-style 400 blaming temperature strips it and retries once."""
        # Arrange — thinking disabled so the proactive strip does not apply and
        # the reactive fallback is what removes the parameter.
        route = respx.post(ANTHROPIC_URL).mock(
            side_effect=[
                httpx.Response(
                    400,
                    json={
                        "error": {
                            "type": "invalid_request_error",
                            "message": "temperature is not supported for this model",
                        }
                    },
                ),
                httpx.Response(200, json=SUCCESS_RESPONSE),
            ]
        )

        # Act
        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            temperature=0.5,
            thinking_effort="none",
        )

        # Assert
        assert route.call_count == 2
        first_body = _strip_cache_control(json.loads(route.calls[0].request.content))
        second_body = _strip_cache_control(json.loads(route.calls[1].request.content))
        assert first_body["temperature"] == 0.5
        assert "temperature" not in second_body

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
