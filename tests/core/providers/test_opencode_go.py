"""Tests for OpenCodeGoAdapter."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

import core.providers.opencode_go as opencode_go_module
from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES
from core.providers.anthropic_compatible import AnthropicCompatibleAdapter
from core.providers.errors import ProviderError
from core.providers.github_copilot_responses import estimate_responses_input_tokens
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import (
    OPENCODE_SESSION_HEADER,
    OPENCODE_SESSION_ID_KWARG,
    OpenCodeGoAdapter,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.utils.tokens import estimate_request_input_tokens

API_KEY = "test-opencode-go-key"
OPENCODE_GO_URL = "https://opencode-go.example/v1/chat/completions"
OPENCODE_GO_MESSAGES_URL = "https://opencode-go.example/v1/messages"
OPENCODE_GO_RESPONSES_URL = "https://opencode-go.example/v1/responses"
CLOSED_TOOL = {
    "name": "inspect_probe",
    "description": "Inspect one synthetic value.",
    "parameters": {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
        "additionalProperties": False,
    },
}
# Per-model wire protocol is now DATA (metadata.opencode_go.protocol), not a
# hardcoded adapter set. These ids carry "anthropic" in the protocol map below
# and must route through the internal Messages adapter.
ANTHROPIC_MESSAGES_MODELS: tuple[str, ...] = (
    "minimax-m2.5",
    "minimax-m2.7",
    "minimax-m3",
    "qwen3.6-plus",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.8-flash",
    "qwen3.8-max",
)
# Ids carrying "responses" in the protocol map below must route through the
# shared stateless Responses machinery (/responses endpoint).
RESPONSES_MODELS: tuple[str, ...] = (
    "gpt-5.6-luna",
    "grok-4.5",
    "grok-4.6",
    "muse-spark-1.2-contributor",
    "muse-spark-1.3-contributor",
)
# Small per-model profiles mirroring the independent facts carried by
# ``metadata.opencode_go``. Models absent here are unknown to the adapter.
_PROFILE_BY_MODEL: dict[str, dict[str, object]] = {
    "minimax-m2.7": {"protocol": "anthropic"},
    "minimax-m2.5": {"protocol": "anthropic"},
    "minimax-m3": {"protocol": "anthropic"},
    "qwen3.7-plus": {"protocol": "anthropic"},
    "qwen3.7-max": {"protocol": "anthropic"},
    "qwen3.8-max": {"protocol": "anthropic"},
    "qwen3.8-flash": {"protocol": "anthropic"},
    "qwen3.6-plus": {"protocol": "anthropic"},
    "deepseek-v4-flash": {"protocol": "openai"},
    "deepseek-v4-flash-vision-exp": {"protocol": "openai"},
    "deepseek-v4-pro": {"protocol": "openai"},
    "glm-5.1": {"protocol": "openai", "reasoning_response_field": "reasoning_content"},
    "glm-5.2": {"protocol": "openai", "reasoning_response_field": "reasoning_content"},
    "glm-5.3": {"protocol": "openai", "reasoning_response_field": "reasoning_content"},
    "glm-5.3-flash": {
        "protocol": "openai",
        "reasoning_response_field": "reasoning_content",
    },
    "longcat-2.0": {
        "protocol": "openai",
        "reasoning_response_field": "reasoning_content",
    },
    "gpt-5.6-luna": {"protocol": "responses"},
    "muse-spark-1.2-contributor": {"protocol": "responses"},
    "muse-spark-1.3-contributor": {"protocol": "responses"},
    "grok-4.5": {
        "minimum_reasoning_effort": "low",
        "protocol": "responses",
    },
    "grok-4.6": {
        "minimum_reasoning_effort": "low",
        "protocol": "responses",
    },
    "hy3": {"protocol": "openai", "reasoning_response_field": "reasoning"},
    "hy4-preview": {"protocol": "openai", "reasoning_response_field": "reasoning"},
    "kimi-k2.5": {
        "protocol": "openai",
        "thinking_control": "toggle",
    },
    "kimi-k2.6": {
        "protocol": "openai",
        "reasoning_response_field": "reasoning",
        "thinking_control": "toggle",
        "thinking_keep": "all",
    },
    "kimi-k2.7-code": {
        "protocol": "openai",
        "thinking_control": "always_enabled",
    },
    "kimi-k3": {
        "minimum_reasoning_effort": "low",
        "protocol": "openai",
        "reasoning_response_field": "reasoning",
    },
    "mimo-v2.5": {"protocol": "openai", "reasoning_response_field": "reasoning_content"},
    "mimo-v2.5-pro": {
        "protocol": "openai",
        "reasoning_response_field": "reasoning_content",
    },
    "qwen3.6-plus-openai": {
        "protocol": "openai",
    },
}


def _model_with_profile(
    model_id: str,
    profile: dict[str, object] | None,
) -> Model:
    metadata: dict[str, object] = {}
    if profile is not None:
        metadata = {"opencode_go": profile}
    reasoning = ReasoningCapabilities(supported=True)
    if model_id in {"kimi-k2.5", "kimi-k2.6"}:
        reasoning = ReasoningCapabilities(supported=True, control="on_off")
    elif model_id == "gpt-5.6-luna":
        reasoning = ReasoningCapabilities(
            supported=True,
            control="levels",
            levels=("none", "low", "medium", "high", "xhigh", "max"),
        )
    elif model_id in {"muse-spark-1.2-contributor", "muse-spark-1.3-contributor"}:
        reasoning = ReasoningCapabilities(
            supported=True,
            control="levels",
            levels=("minimal", "low", "medium", "high", "xhigh"),
        )
    elif model_id in {"grok-4.5", "grok-4.6"}:
        reasoning = ReasoningCapabilities(
            supported=True,
            control="levels",
            levels=("low", "medium", "high"),
        )
    elif model_id == "kimi-k3":
        reasoning = ReasoningCapabilities(
            supported=True,
            control="levels",
            levels=("low", "high", "max"),
        )
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=reasoning,
        ),
        context_window=1_000_000,
        max_output_tokens=131_072,
        metadata=metadata,
    )


def _protocol_lookup(model_id: str) -> Model | None:
    """Resolve the metadata-carrying model for one bare or vendor-prefixed id."""

    bare = model_id.split("::", 1)[0]
    candidates = [model_id, bare]
    if "/" in bare:
        candidates.append(bare.rsplit("/", 1)[-1])
    for candidate in candidates:
        if candidate in _PROFILE_BY_MODEL:
            return _model_with_profile(candidate, _PROFILE_BY_MODEL[candidate])
    return None


def test_public_package_exports_opencode_go_adapter() -> None:
    from core.providers import OpenCodeGoAdapter as PublicOpenCodeGoAdapter

    assert PublicOpenCodeGoAdapter is OpenCodeGoAdapter


@pytest.fixture()
def opencode_go_config() -> ProviderConfig:
    return ProviderConfig(
        id="opencode-go",
        name="OpenCode Go",
        adapter="opencode_go",
        base_url="https://opencode-go.example/v1",
        extra_headers={"User-Agent": "vBot"},
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENCODE_GO_API_KEY",
                ),
            )
        ],
    )


@pytest.fixture()
def opencode_go_adapter(opencode_go_config: ProviderConfig) -> OpenCodeGoAdapter:
    # The adapter routes on ``metadata.opencode_go.protocol`` resolved via
    # ``model_lookup``; inject the protocol map so routing is data-driven.
    return OpenCodeGoAdapter(opencode_go_config, API_KEY, model_lookup=_protocol_lookup)


def model_with_output_limit(
    model_id: str,
    max_output_tokens: int,
    *,
    context_window: int = 1_000_000,
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        ),
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )


class TestOpenCodeGoAdapter:
    def test_request_context_uses_opaque_prompt_cache_affinity(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        source = opencode_go_adapter.request_context_kwargs(
            project_id="vbot",
            agent_id="builder",
            session_id="source",
            prompt_cache_affinity_id="shared-lineage",
        )
        fork = opencode_go_adapter.request_context_kwargs(
            project_id="vbot",
            agent_id="builder",
            session_id="fork",
            prompt_cache_affinity_id="shared-lineage",
        )
        fallback = opencode_go_adapter.request_context_kwargs(
            project_id="vbot",
            agent_id="builder",
            session_id="source",
        )

        assert source == {OPENCODE_SESSION_ID_KWARG: "vbot-shared-lineage"}
        assert fork == source
        assert fallback == opencode_go_adapter.request_context_kwargs(
            project_id="vbot",
            agent_id="builder",
            session_id="source",
        )
        assert fallback[OPENCODE_SESSION_ID_KWARG].startswith("vbot-")
        assert "source" not in fallback[OPENCODE_SESSION_ID_KWARG]

    @pytest.mark.parametrize(
        ("model_id", "url", "response_kind"),
        [
            ("deepseek-v4-flash", OPENCODE_GO_URL, "openai"),
            ("minimax-m3", OPENCODE_GO_MESSAGES_URL, "anthropic"),
            ("gpt-5.6-luna", OPENCODE_GO_RESPONSES_URL, "responses"),
        ],
    )
    @respx.mock
    @pytest.mark.asyncio
    async def test_session_header_reaches_every_non_streaming_wire(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
        url: str,
        response_kind: str,
    ) -> None:
        if response_kind == "openai":
            response = httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        elif response_kind == "anthropic":
            response = httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )
        else:
            response = httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
        route = respx.post(url).mock(return_value=response)
        request_context = opencode_go_adapter.request_context_kwargs(
            project_id="vbot",
            agent_id="builder",
            session_id="session",
            prompt_cache_affinity_id="cache-affinity",
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id=model_id,
            **request_context,
        )

        request = route.calls.last.request
        assert request.headers[OPENCODE_SESSION_HEADER] == "vbot-cache-affinity"
        assert request.headers["user-agent"] == "vBot"
        assert OPENCODE_SESSION_ID_KWARG not in json.loads(request.content)

    @pytest.mark.parametrize(
        ("model_id", "url", "response_kind"),
        [
            ("deepseek-v4-flash", OPENCODE_GO_URL, "openai"),
            ("minimax-m3", OPENCODE_GO_MESSAGES_URL, "anthropic"),
            ("gpt-5.6-luna", OPENCODE_GO_RESPONSES_URL, "responses"),
        ],
    )
    @respx.mock
    @pytest.mark.asyncio
    async def test_session_header_reaches_every_streaming_wire(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
        url: str,
        response_kind: str,
    ) -> None:
        if response_kind == "openai":
            response_text = "data: [DONE]\n\n"
        elif response_kind == "anthropic":
            response_text = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        else:
            completed = {
                "id": "resp_stream",
                "object": "response",
                "status": "completed",
                "output": RESPONSES_COMPLETED_RESPONSE["output"],
                "usage": RESPONSES_COMPLETED_RESPONSE["usage"],
            }
            response_text = (
                'event: response.output_text.delta\ndata: {"delta":"Done"}\n\n'
                f"event: response.completed\ndata: {json.dumps({'response': completed})}\n\n"
            )
        route = respx.post(url).mock(
            return_value=httpx.Response(
                200,
                text=response_text,
                headers={"content-type": "text/event-stream"},
            )
        )
        request_context = opencode_go_adapter.request_context_kwargs(
            project_id="vbot",
            agent_id="builder",
            session_id="session",
            prompt_cache_affinity_id="cache-affinity",
        )

        _ = [
            delta
            async for delta in opencode_go_adapter.stream(
                [{"role": "user", "content": "hello"}],
                model_id=model_id,
                **request_context,
            )
        ]

        request = route.calls.last.request
        assert request.headers[OPENCODE_SESSION_HEADER] == "vbot-cache-affinity"
        assert request.headers["user-agent"] == "vBot"
        assert OPENCODE_SESSION_ID_KWARG not in json.loads(request.content)

    @pytest.mark.parametrize(
        "model_id",
        [
            *ANTHROPIC_MESSAGES_MODELS,
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
            "deepseek/deepseek-v4-flash",
            "glm-5.3-flash",
            "kimi-k3",
            "longcat-2.0",
            "mimo-v2.5",
        ],
    )
    def test_reasoning_replay_policy_is_full_history_on_both_routes(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
    ) -> None:
        assert opencode_go_adapter.reasoning_replay_policy(model_id) == "full_history"

    def test_unknown_model_reasoning_replay_is_full_history(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        assert opencode_go_adapter.reasoning_replay_policy("new-unprofiled-model") == "full_history"

    @pytest.mark.parametrize("model_id", ["kimi-k2.5", "kimi-k2.6", "kimi-k2.7-code"])
    def test_kimi_models_inherit_full_history_replay(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
    ) -> None:
        assert opencode_go_adapter.reasoning_replay_policy(model_id) == "full_history"

    @pytest.mark.parametrize("model_id", ["glm-5.2", "glm-5.3"])
    def test_glm_reasoning_is_replayed_byte_for_byte_in_request_payload(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
    ) -> None:
        reasoning = "EXACT old Reasoning: äöü\nline two\n"

        payload = opencode_go_adapter._build_payload(
            [
                {"role": "user", "content": "First turn"},
                {
                    "role": "assistant",
                    "content": "First answer",
                    "reasoning": reasoning,
                },
                {"role": "user", "content": "Second turn"},
            ],
            model_id,
        )

        assert opencode_go_adapter.reasoning_replay_policy(model_id) == "full_history"
        assistant_message = payload["messages"][1]
        assert assistant_message["reasoning_content"] == reasoning
        assert "<reasoning_history>" not in (assistant_message.get("content") or "")

    def test_kimi_k2_6_enables_full_history_rendering(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        payload = opencode_go_adapter._build_payload(
            [{"role": "user", "content": "Continue"}],
            "kimi-k2.6",
        )

        assert payload["thinking"] == {"type": "enabled", "keep": "all"}
        assert "reasoning_effort" not in payload

    def test_kimi_k2_6_respects_explicit_thinking_off(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        payload = opencode_go_adapter._build_payload(
            [{"role": "user", "content": "Continue"}],
            "kimi-k2.6",
            thinking_effort="none",
        )

        assert payload["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in payload

    def test_kimi_k2_7_never_sends_unsupported_reasoning_effort_or_disables_thinking(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        payload = opencode_go_adapter._build_payload(
            [{"role": "user", "content": "Continue"}],
            "kimi-k2.7-code",
            thinking_effort="none",
        )

        assert payload["thinking"] == {"type": "enabled"}
        assert "reasoning_effort" not in payload

    @pytest.mark.parametrize("model_id", ["grok-4.5", "kimi-k3"])
    def test_always_reasoning_effort_models_map_none_to_lowest_supported_level(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
    ) -> None:
        payload = opencode_go_adapter._build_payload(
            [{"role": "user", "content": "Continue"}],
            model_id,
            thinking_effort="none",
        )

        assert payload["reasoning_effort"] == "low"

    def test_chat_route_declares_readable_only_reasoning_replay(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        assert opencode_go_adapter.reasoning_replay_fidelity("kimi-k3") == "readable_only"

    def test_kimi_k3_response_and_history_use_independent_reasoning_fields(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        normalized = opencode_go_adapter.normalize_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Answer",
                            "reasoning": "Readable trace",
                            "reasoning_details": [{"type": "reasoning.text", "text": "meta"}],
                        }
                    }
                ]
            },
            model_id="kimi-k3",
        )

        payload = opencode_go_adapter._build_payload([normalized], "kimi-k3")
        assistant = payload["messages"][0]

        assert normalized["reasoning"] == "Readable trace"
        assert normalized["reasoning_meta"] == {
            "reasoning_details": [{"type": "reasoning.text", "text": "meta"}]
        }
        assert assistant["reasoning_content"] == "Readable trace"
        assert "reasoning" not in assistant
        assert "reasoning_details" not in assistant

    def test_format_assistant_message_adds_reasoning_content(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        internal_message = {
            "role": "assistant",
            "content": "Answer",
            "reasoning": "I think...",
            "tool_calls": None,
            "reasoning_meta": None,
        }

        wire = opencode_go_adapter._format_assistant_message(
            internal_message,
            model_id="kimi-k3",
        )

        assert wire["reasoning_content"] == "I think..."
        assert wire["content"] == "Answer"
        assert "reasoning" not in wire

    def test_format_assistant_message_skips_reasoning_content_when_reasoning_is_none(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        internal_message = {
            "role": "assistant",
            "content": "Hi",
            "reasoning": None,
            "tool_calls": None,
            "reasoning_meta": None,
        }

        wire = opencode_go_adapter._format_assistant_message(
            internal_message,
            model_id="kimi-k3",
        )

        assert "reasoning_content" not in wire

    def test_format_assistant_message_skips_reasoning_content_when_reasoning_is_empty(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        internal_message = {
            "role": "assistant",
            "content": "Hi",
            "reasoning": "",
            "tool_calls": None,
            "reasoning_meta": None,
        }

        wire = opencode_go_adapter._format_assistant_message(
            internal_message,
            model_id="kimi-k3",
        )

        assert "reasoning_content" not in wire

    @respx.mock
    @pytest.mark.asyncio
    async def test_round_trip_tool_loop_payload_includes_reasoning_content(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        reasoning_text = "Need to call tool first"
        route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "reasoning_content": reasoning_text,
                                "tool_calls": [
                                    {
                                        "id": "call_weather",
                                        "type": "function",
                                        "function": {
                                            "name": "get_weather",
                                            "arguments": '{"city":"Berlin"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            )
        )

        first_response = await opencode_go_adapter.send(
            [{"role": "user", "content": "Weather in Berlin?"}],
            model_id="deepseek/deepseek-v4-flash",
        )
        normalized_assistant = opencode_go_adapter.normalize_response(first_response)

        payload = opencode_go_adapter._build_payload(
            [
                {"role": "user", "content": "Weather in Berlin?"},
                normalized_assistant,
                {
                    "role": "tool",
                    "tool_call_id": "call_weather",
                    "name": "get_weather",
                    "content": json.dumps({"temp": 22}),
                },
            ],
            model_id="deepseek/deepseek-v4-flash",
        )

        assistant_wire = next(msg for msg in payload["messages"] if msg.get("role") == "assistant")
        assert route.called
        assert assistant_wire["reasoning_content"] == reasoning_text

    def test_base_adapter_build_payload_uses_reasoning_content_fallback(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        base_adapter = OpenAICompatibleAdapter(opencode_go_config, API_KEY)

        payload = base_adapter._build_payload(
            [
                {
                    "role": "assistant",
                    "content": "Answer",
                    "reasoning": "I think...",
                    "tool_calls": None,
                    "reasoning_meta": None,
                }
            ],
            model_id="deepseek/deepseek-v4-flash",
        )

        assert payload["messages"][0]["reasoning_content"] == "I think..."

    def test_build_payload_replays_reasoning_for_all_assistants_on_openai_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        payload = opencode_go_adapter._build_payload(
            [
                {"role": "user", "content": "First question"},
                {
                    "role": "assistant",
                    "content": "First answer",
                    "reasoning": "first reasoning",
                    "reasoning_meta": {"reasoning_details": [{"trace": "first"}]},
                    "tool_calls": None,
                },
                {"role": "user", "content": "Second question"},
                {
                    "role": "assistant",
                    "content": "Second answer",
                    "reasoning": "second reasoning",
                    "reasoning_meta": {"reasoning_details": [{"trace": "second"}]},
                    "tool_calls": None,
                },
            ],
            model_id="deepseek/deepseek-v4-flash",
        )

        assistant_messages = [
            message for message in payload["messages"] if message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        assert assistant_messages[0]["reasoning_content"] == "first reasoning"
        assert "reasoning_details" not in assistant_messages[0]
        assert assistant_messages[1]["reasoning_content"] == "second reasoning"
        assert "reasoning_details" not in assistant_messages[1]

    def test_deepseek_none_thinking_effort_omits_reasoning_effort(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        payload = opencode_go_adapter._build_payload(
            [{"role": "user", "content": "Reply OK."}],
            model_id="deepseek-v4-flash",
            thinking_effort="none",
        )

        assert "reasoning_effort" not in payload

    def test_build_payload_uses_catalog_output_limit_over_provider_default(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        config = replace(opencode_go_config, defaults={"max_tokens": 4096})
        catalog_model = model_with_output_limit("deepseek-v4-flash", 384_000)
        adapter = OpenCodeGoAdapter(
            config,
            API_KEY,
            model_lookup=lambda model_id: (
                catalog_model if model_id == catalog_model.model_id else None
            ),
        )

        payload = adapter._build_payload(
            [{"role": "user", "content": "Write a complete HTML app."}],
            model_id="deepseek-v4-flash",
        )

        assert payload["max_tokens"] == 384_000

    def test_build_payload_uses_catalog_output_limit_for_vendor_prefixed_model_id(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        config = replace(opencode_go_config, defaults={"max_tokens": 4096})
        catalog_model = model_with_output_limit("deepseek-v4-flash", 384_000)
        adapter = OpenCodeGoAdapter(
            config,
            API_KEY,
            model_lookup=lambda model_id: (
                catalog_model if model_id == catalog_model.model_id else None
            ),
        )

        payload = adapter._build_payload(
            [{"role": "user", "content": "Write a complete HTML app."}],
            model_id="deepseek/deepseek-v4-flash",
        )

        assert payload["max_tokens"] == 384_000

    def test_build_payload_preserves_explicit_output_limit(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        config = replace(opencode_go_config, defaults={"max_tokens": 4096})
        catalog_model = model_with_output_limit("deepseek-v4-flash", 384_000)
        adapter = OpenCodeGoAdapter(
            config,
            API_KEY,
            model_lookup=lambda model_id: (
                catalog_model if model_id == catalog_model.model_id else None
            ),
        )

        payload = adapter._build_payload(
            [{"role": "user", "content": "Write a short file."}],
            model_id="deepseek-v4-flash",
            max_tokens=2048,
        )

        assert payload["max_tokens"] == 2048

    def test_build_payload_clamps_equal_context_and_output_catalog_limits(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        catalog_model = model_with_output_limit(
            "deepseek-v4-flash",
            384_000,
            context_window=384_000,
        )
        adapter = OpenCodeGoAdapter(
            opencode_go_config,
            API_KEY,
            model_lookup=lambda model_id: (
                catalog_model if model_id == catalog_model.model_id else None
            ),
        )

        payload = adapter._build_payload(
            [{"role": "user", "content": "x" * 8_000}],
            model_id="deepseek-v4-flash",
        )

        assert 0 < payload["max_tokens"] < 384_000


class TestOpenCodeGoAdapterMinimaxRouting:
    @pytest.mark.asyncio
    async def test_constructor_accepts_runtime_factory_signature(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        runtime_base_url = "https://runtime-opencode-go.example/v1"
        runtime_auth = AuthConfig(
            header="Authorization",
            prefix="Bearer ",
            credential_key="RUNTIME_OPENCODE_GO_KEY",
        )
        adapter = OpenCodeGoAdapter(opencode_go_config, API_KEY, runtime_base_url, runtime_auth)

        try:
            assert str(adapter._client.base_url).rstrip("/") == runtime_base_url
            assert isinstance(adapter._messages, AnthropicCompatibleAdapter)
            assert adapter._messages._client is adapter._client
            assert adapter._messages._token_getter is adapter._token_getter
            assert adapter._messages._auth_config.header == "x-api-key"
            assert adapter._messages._auth_config.prefix == ""
            assert adapter._messages._auth_config.credential_key == runtime_auth.credential_key
        finally:
            await adapter.aclose()

    def test_wire_media_support_routes_by_model_protocol(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        assert opencode_go_adapter.wire_media_support("minimax-m2.7") == (IMAGE_WIRE_MEDIA_TYPES)
        assert opencode_go_adapter.wire_media_support("deepseek-v4-flash") == (
            OpenAICompatibleAdapter.wire_media_support(
                opencode_go_adapter,
                "deepseek-v4-flash",
            )
        )

    @pytest.mark.parametrize("model_id", ANTHROPIC_MESSAGES_MODELS)
    @respx.mock
    @pytest.mark.asyncio
    async def test_messages_model_send_uses_anthropic_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                    "stop_reason": "end_turn",
                },
            )
        )
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "fallback"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id=model_id,
        )

        assert messages_route.called
        assert not chat_route.called
        request = messages_route.calls.last.request
        assert request.headers["x-api-key"] == API_KEY
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers["user-agent"] == "vBot"
        body = json.loads(request.content)
        assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_messages_path_never_enables_strict_mode(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "inspect"}],
            model_id="minimax-m3",
            tools=[CLOSED_TOOL],
        )

        body = json.loads(messages_route.calls.last.request.content)
        assert body["tools"] == [
            {
                "name": CLOSED_TOOL["name"],
                "description": CLOSED_TOOL["description"],
                "input_schema": CLOSED_TOOL["parameters"],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_messages_path_keeps_large_tool_set_non_strict(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )
        )
        tools = [{**CLOSED_TOOL, "name": f"inspect_probe_{index}"} for index in range(21)]

        await opencode_go_adapter.send(
            [{"role": "user", "content": "inspect"}],
            model_id="minimax-m3",
            tools=tools,
        )

        body = json.loads(messages_route.calls.last.request.content)
        assert len(body["tools"]) == 21
        assert all("strict" not in tool for tool in body["tools"])

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_marked_model_send_uses_openai_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "messages"}],
                    "stop_reason": "end_turn",
                },
            )
        )
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "chat"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id="deepseek-v4-flash",
        )

        assert chat_route.called
        assert not messages_route.called
        assert chat_route.calls.last.request.headers["user-agent"] == "vBot"

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_path_omits_unsupported_strict_field(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "inspect"}],
            model_id="deepseek-v4-flash",
            tools=[CLOSED_TOOL],
        )

        body = json.loads(chat_route.calls.last.request.content)
        assert "strict" not in body["tools"][0]["function"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_path_does_not_retry_permanent_subscription_limit(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                429,
                json={
                    "error": {
                        "type": "GoUsageLimitError",
                        "message": "Monthly usage limit reached. Enable available balance.",
                    }
                },
            )
        )

        with pytest.raises(ProviderError, match="subscription limit reached") as exc_info:
            await opencode_go_adapter.send(
                [{"role": "user", "content": "hello"}],
                model_id="deepseek-v4-flash",
            )

        assert exc_info.value.retryable is False
        assert chat_route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_messages_path_does_not_retry_permanent_subscription_limit(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                429,
                json={
                    "type": "error",
                    "error": {
                        "type": "FreeUsageLimitError",
                        "message": "Monthly usage limit has been reached.",
                    },
                },
            )
        )

        with pytest.raises(ProviderError, match="subscription limit reached") as exc_info:
            await opencode_go_adapter.send(
                [{"role": "user", "content": "hello"}],
                model_id="minimax-m3",
            )

        assert exc_info.value.retryable is False
        assert messages_route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_stream_does_not_retry_permanent_subscription_limit(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                429,
                json={
                    "error": {
                        "code": "insufficient_quota",
                        "message": "Quota exceeded.",
                    }
                },
            )
        )

        with pytest.raises(ProviderError, match="subscription limit reached") as exc_info:
            async for _ in opencode_go_adapter.stream(
                [{"role": "user", "content": "hello"}],
                model_id="deepseek-v4-flash",
            ):
                pass

        assert exc_info.value.retryable is False
        assert chat_route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_messages_stream_does_not_retry_permanent_subscription_limit(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                429,
                json={
                    "type": "error",
                    "error": {
                        "type": "GoUsageLimitError",
                        "message": "Use available balance to continue.",
                    },
                },
            )
        )

        with pytest.raises(ProviderError, match="subscription limit reached") as exc_info:
            async for _ in opencode_go_adapter.stream(
                [{"role": "user", "content": "hello"}],
                model_id="minimax-m3",
            ):
                pass

        assert exc_info.value.retryable is False
        assert messages_route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_path_still_retries_transient_rate_limit(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            side_effect=[
                httpx.Response(
                    429,
                    json={"error": {"type": "rate_limit_error", "message": "Slow down."}},
                ),
                httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ]
                    },
                ),
            ]
        )

        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            response = await opencode_go_adapter.send(
                [{"role": "user", "content": "hello"}],
                model_id="deepseek-v4-flash",
                **opencode_go_adapter.request_context_kwargs(
                    project_id="vbot",
                    agent_id="builder",
                    session_id="session",
                    prompt_cache_affinity_id="retry-affinity",
                ),
            )

        assert response["choices"][0]["message"]["content"] == "ok"
        assert chat_route.call_count == 2
        assert all(
            call.request.headers[OPENCODE_SESSION_HEADER] == "vbot-retry-affinity"
            for call in chat_route.calls
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_unknown_model_defaults_to_openai_path_and_warns(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A model with no protocol metadata routes the safe OpenAI default + logs a warn."""

        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "messages"}],
                    "stop_reason": "end_turn",
                },
            )
        )
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "chat"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        )
        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(
                200,
                json={"id": "resp_x", "status": "completed", "output": []},
            )
        )

        opencode_go_module._warned_unmarked_models.clear()
        with caplog.at_level("WARNING", logger="vbot.providers.opencode_go"):
            await opencode_go_adapter.send(
                [{"role": "user", "content": "hello"}],
                model_id="brand-new-unlisted-model",
            )

        assert chat_route.called
        assert not messages_route.called
        assert not responses_route.called
        assert any(
            "no metadata protocol" in record.getMessage()
            and "brand-new-unlisted-model" in record.getMessage()
            for record in caplog.records
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_unknown_model_warns_once_per_process(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An unmarked model logs its routing warning once, not on every request."""

        respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "chat"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        )

        opencode_go_module._warned_unmarked_models.clear()
        with caplog.at_level("WARNING", logger="vbot.providers.opencode_go"):
            for _ in range(3):
                await opencode_go_adapter.send(
                    [{"role": "user", "content": "hello"}],
                    model_id="repeated-unlisted-model",
                )

        warnings = [
            record
            for record in caplog.records
            if "no metadata protocol" in record.getMessage()
            and "repeated-unlisted-model" in record.getMessage()
        ]
        assert len(warnings) == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_send_replays_reasoning_meta_for_all_assistants(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        captured_payload: dict[str, Any] = {}

        def _capture_messages_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )

        respx.post(OPENCODE_GO_MESSAGES_URL).mock(side_effect=_capture_messages_request)

        await opencode_go_adapter.send(
            [
                {"role": "user", "content": "First"},
                {
                    "role": "assistant",
                    "content": "Older assistant",
                    "reasoning": "old thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {"type": "thinking", "thinking": "old thinking", "signature": "sig-old"}
                        ]
                    },
                    "tool_calls": [{"id": "call_old", "name": "old_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_old",
                    "name": "old_tool",
                    "content": json.dumps({"ok": True}),
                },
                {
                    "role": "assistant",
                    "content": "Latest assistant",
                    "reasoning": "latest thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {
                                "type": "thinking",
                                "thinking": "latest thinking",
                                "signature": "sig-latest",
                            }
                        ]
                    },
                    "tool_calls": [{"id": "call_latest", "name": "latest_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_latest",
                    "name": "latest_tool",
                    "content": json.dumps({"ok": True}),
                },
            ],
            model_id="minimax-m2.7",
        )

        assistant_messages = [
            message
            for message in captured_payload.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        older_blocks = assistant_messages[0].get("content", [])
        latest_blocks = assistant_messages[1].get("content", [])
        assert isinstance(older_blocks, list)
        assert isinstance(latest_blocks, list)
        older_thinking = [
            block
            for block in older_blocks
            if isinstance(block, dict) and block.get("type") == "thinking"
        ]
        latest_thinking = [
            block
            for block in latest_blocks
            if isinstance(block, dict) and block.get("type") == "thinking"
        ]
        assert older_thinking == [
            {"type": "thinking", "thinking": "old thinking", "signature": "sig-old"}
        ]
        assert latest_thinking == [
            {"type": "thinking", "thinking": "latest thinking", "signature": "sig-latest"}
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_send_passes_assistant_reasoning_through_unchanged(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        captured_payload: dict[str, Any] = {}

        def _capture_messages_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )

        respx.post(OPENCODE_GO_MESSAGES_URL).mock(side_effect=_capture_messages_request)

        await opencode_go_adapter.send(
            [
                {"role": "user", "content": "First"},
                {
                    "role": "assistant",
                    "content": "Older assistant",
                    "reasoning": "old thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {"type": "thinking", "thinking": "old thinking", "signature": "sig-old"}
                        ]
                    },
                    "tool_calls": [{"id": "call_old", "name": "old_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_old",
                    "name": "old_tool",
                    "content": json.dumps({"ok": True}),
                },
                {
                    "role": "assistant",
                    "content": "Latest assistant",
                    "tool_calls": [{"id": "call_latest", "name": "latest_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_latest",
                    "name": "latest_tool",
                    "content": json.dumps({"ok": True}),
                },
            ],
            model_id="minimax-m2.7",
        )

        assistant_messages = [
            message
            for message in captured_payload.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        older_blocks = assistant_messages[0].get("content", [])
        latest_blocks = assistant_messages[1].get("content", [])
        assert isinstance(older_blocks, list)
        assert isinstance(latest_blocks, list)
        assert any(
            isinstance(block, dict) and block.get("type") == "thinking" for block in older_blocks
        )
        assert not any(
            isinstance(block, dict) and block.get("type") == "thinking" for block in latest_blocks
        )
        assert any(
            isinstance(block, dict) and block.get("type") == "tool_use" for block in latest_blocks
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_minimax_send_uses_openai_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        captured_payload: dict[str, Any] = {}

        def _capture_chat_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        chat_route = respx.post(OPENCODE_GO_URL).mock(side_effect=_capture_chat_request)
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "unused"}],
                    "stop_reason": "end_turn",
                },
            )
        )

        await opencode_go_adapter.send(
            [
                {"role": "user", "content": "First"},
                {
                    "role": "assistant",
                    "content": "Older assistant",
                    "reasoning": "old thinking",
                    "reasoning_meta": {
                        "reasoning_details": [{"trace": "old"}],
                    },
                    "tool_calls": None,
                },
                {"role": "user", "content": "Second"},
                {
                    "role": "assistant",
                    "content": "Latest assistant",
                    "reasoning": "latest thinking",
                    "reasoning_meta": {
                        "reasoning_details": [{"trace": "latest"}],
                    },
                    "tool_calls": None,
                },
                {"role": "user", "content": "Continue"},
            ],
            model_id="deepseek/deepseek-v4-flash",
        )

        assert chat_route.called
        assert not messages_route.called
        assistant_messages = [
            message
            for message in captured_payload.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        assert assistant_messages[0]["reasoning_content"] == "old thinking"
        assert "reasoning_details" not in assistant_messages[0]
        assert assistant_messages[1]["reasoning_content"] == "latest thinking"
        assert "reasoning_details" not in assistant_messages[1]

    @pytest.mark.parametrize("model_id", ANTHROPIC_MESSAGES_MODELS)
    @respx.mock
    @pytest.mark.asyncio
    async def test_messages_model_stream_uses_anthropic_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                text='event: message_stop\ndata: {"type":"message_stop"}\n\n',
                headers={"content-type": "text/event-stream"},
            )
        )
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                text="data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        )

        chunks: list[dict[str, str]] = []
        async for chunk in opencode_go_adapter.stream(
            [{"role": "user", "content": "hello"}],
            model_id=model_id,
        ):
            chunks.append(chunk)

        assert chunks == []
        assert messages_route.called
        assert not chat_route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_minimax_stream_uses_openai_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                text="data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        )
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                text='event: message_stop\ndata: {"type":"message_stop"}\n\n',
                headers={"content-type": "text/event-stream"},
            )
        )

        chunks: list[dict[str, str]] = []
        async for chunk in opencode_go_adapter.stream(
            [{"role": "user", "content": "hello"}],
            model_id="deepseek/deepseek-v4-flash",
        ):
            chunks.append(chunk)

        assert chunks == []
        assert chat_route.called
        assert not messages_route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_stream_replays_reasoning_meta_for_all_assistants(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        captured_payload: dict[str, Any] = {}

        def _capture_messages_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                text='event: message_stop\ndata: {"type":"message_stop"}\n\n',
                headers={"content-type": "text/event-stream"},
            )

        respx.post(OPENCODE_GO_MESSAGES_URL).mock(side_effect=_capture_messages_request)

        chunks: list[dict[str, str]] = []
        async for chunk in opencode_go_adapter.stream(
            [
                {"role": "user", "content": "First"},
                {
                    "role": "assistant",
                    "content": "Older assistant",
                    "reasoning": "old thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {"type": "thinking", "thinking": "old thinking", "signature": "sig-old"}
                        ]
                    },
                    "tool_calls": [{"id": "call_old", "name": "old_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_old",
                    "name": "old_tool",
                    "content": json.dumps({"ok": True}),
                },
                {
                    "role": "assistant",
                    "content": "Latest assistant",
                    "reasoning": "latest thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {
                                "type": "thinking",
                                "thinking": "latest thinking",
                                "signature": "sig-latest",
                            }
                        ]
                    },
                    "tool_calls": [{"id": "call_latest", "name": "latest_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_latest",
                    "name": "latest_tool",
                    "content": json.dumps({"ok": True}),
                },
            ],
            model_id="minimax-m2.7",
        ):
            chunks.append(chunk)

        assert chunks == []
        assistant_messages = [
            message
            for message in captured_payload.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        older_blocks = assistant_messages[0].get("content", [])
        latest_blocks = assistant_messages[1].get("content", [])
        assert isinstance(older_blocks, list)
        assert isinstance(latest_blocks, list)
        assert any(
            isinstance(block, dict) and block.get("type") == "thinking" for block in older_blocks
        )
        assert any(
            isinstance(block, dict) and block.get("type") == "thinking" for block in latest_blocks
        )

    def test_normalize_response_routes_openai_by_choices_key(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        result = opencode_go_adapter.normalize_response(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "hi"},
                    }
                ],
                "id": "1",
            },
            model_id="deepseek-v4-flash",
        )

        assert result["role"] == "assistant"
        assert result["content"] == "hi"

    def test_normalize_response_routes_anthropic_when_no_choices(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        result = opencode_go_adapter.normalize_response(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
            }
        )

        assert result["role"] == "assistant"
        assert result["content"] == "hi"

    def test_normalize_response_routes_by_model_when_shape_is_ambiguous(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        result = opencode_go_adapter.normalize_response(
            {
                "choices": [{"message": {"role": "assistant", "content": "wrong wire"}}],
                "content": [{"type": "text", "text": "messages wire"}],
            },
            model_id="minimax-m2.7",
        )

        assert result["content"] == "messages wire"

    @pytest.mark.asyncio
    async def test_aclose_closes_shared_client_once(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        shared_client = AsyncMock()
        opencode_go_adapter._client = shared_client
        opencode_go_adapter._messages._client = shared_client

        await opencode_go_adapter.aclose()

        shared_client.aclose.assert_awaited_once()


RESPONSES_COMPLETED_RESPONSE = {
    "id": "resp_1",
    "object": "response",
    "status": "completed",
    "model": "gpt-5.6-luna",
    "output": [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "encrypted_content": "enc-blob",
        },
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Done"}],
        },
    ],
    "usage": {"input_tokens": 11, "output_tokens": 5},
}


class TestOpenCodeGoResponsesRouting:
    @pytest.mark.parametrize("model_id", RESPONSES_MODELS)
    def test_responses_models_resolve_responses_protocol(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
    ) -> None:
        assert opencode_go_adapter._model_protocol(model_id) == "responses"

    def test_current_chat_and_messages_models_resolve_documented_protocols(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        assert opencode_go_adapter._model_protocol("longcat-2.0") == "openai"
        assert opencode_go_adapter._model_protocol("hy4-preview") == "openai"
        assert opencode_go_adapter._model_protocol("qwen3.8-flash") == "anthropic"
        assert opencode_go_adapter._model_protocol("qwen3.8-max") == "anthropic"

    def test_responses_context_estimate_uses_rendered_responses_items(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "Keep answers concise."},
            {"role": "user", "content": "Inspect the repository."},
            {
                "role": "assistant",
                "content": None,
                "reasoning_meta": {
                    "response_output": [
                        {
                            "type": "reasoning",
                            "encrypted_content": "opaque-continuity",
                        }
                    ],
                    "reasoning_items": [{"type": "reasoning", "text": "duplicated " * 20_000}],
                    "encrypted_content": ["duplicated " * 20_000],
                },
            },
        ]

        estimated = opencode_go_adapter.estimate_request_input_tokens(
            messages,
            model_id="muse-spark-1.2-contributor",
            tools=[CLOSED_TOOL],
        )
        raw_chat_estimate, _ = estimate_request_input_tokens(messages, [CLOSED_TOOL])

        assert estimated == estimate_responses_input_tokens(messages, tools=[CLOSED_TOOL])
        assert raw_chat_estimate < 1_000

    @respx.mock
    @pytest.mark.asyncio
    async def test_responses_output_limit_uses_responses_context_estimate(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
        )
        with patch.object(
            opencode_go_module,
            "estimate_responses_input_tokens",
            return_value=700_000,
        ) as estimator:
            await opencode_go_adapter.send(
                [{"role": "user", "content": "hello"}],
                model_id="muse-spark-1.2-contributor",
            )

        assert responses_route.called
        estimator.assert_called_once()

    @respx.mock
    @pytest.mark.asyncio
    async def test_responses_model_send_uses_responses_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
        )
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(200, json={"choices": []})
        )
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(200, json={"type": "message"})
        )

        response = await opencode_go_adapter.send(
            [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "hello"},
            ],
            model_id="gpt-5.6-luna",
        )

        assert responses_route.called
        assert not chat_route.called
        assert not messages_route.called
        request_body = json.loads(responses_route.calls.last.request.content)
        assert responses_route.calls.last.request.headers["user-agent"] == "vBot"
        # Stateless shape: complete history as input items, never stored.
        assert request_body["store"] is False
        assert request_body["instructions"] == "Be brief."
        assert [item["role"] for item in request_body["input"]] == ["user"]
        normalized = opencode_go_adapter.normalize_response(response, model_id="gpt-5.6-luna")
        assert normalized["content"] == "Done"
        assert normalized["phase"] == "final_answer"
        assert (
            normalized["reasoning_meta"]["response_output"]
            == (RESPONSES_COMPLETED_RESPONSE["output"])
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_responses_payload_renders_effort_and_encrypted_include(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id="gpt-5.6-luna",
            thinking_effort="high",
            session_id="vbot-session",
            tools=[CLOSED_TOOL],
        )

        request_body = json.loads(responses_route.calls.last.request.content)
        assert request_body["reasoning"] == {"effort": "high", "summary": "auto"}
        assert request_body["include"] == ["reasoning.encrypted_content"]
        assert request_body["tools"][0]["type"] == "function"
        # Non-strict invariant: the field is carried explicitly as false.
        assert request_body["tools"][0]["strict"] is False
        # The gateway publishes no sticky-conversation contract; the routing
        # kwarg is dropped instead of leaking onto the wire.
        assert "session_id" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_gpt_luna_sends_live_verified_none_effort(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id="gpt-5.6-luna",
            thinking_effort="none",
        )

        request_body = json.loads(responses_route.calls.last.request.content)
        assert request_body["reasoning"] == {"effort": "none", "summary": "auto"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_muse_omits_unsupported_none_effort(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id="muse-spark-1.3-contributor",
            thinking_effort="none",
        )

        request_body = json.loads(responses_route.calls.last.request.content)
        assert "reasoning" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_grok_none_effort_maps_to_minimum_on_responses_wire(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        """The wire rejects effort ``none`` (HTTP 400); the override's minimum rung wins."""

        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id="grok-4.5",
            thinking_effort="none",
        )

        request_body = json.loads(responses_route.calls.last.request.content)
        assert request_body["reasoning"] == {"effort": "low", "summary": "auto"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_responses_model_stream_happy_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        completed = {
            "id": "resp_stream",
            "object": "response",
            "status": "completed",
            "output": RESPONSES_COMPLETED_RESPONSE["output"],
            "usage": RESPONSES_COMPLETED_RESPONSE["usage"],
        }
        responses_route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
            return_value=httpx.Response(
                200,
                text=(
                    'event: response.output_text.delta\ndata: {"delta":"Done"}\n\n'
                    f"event: response.completed\ndata: {json.dumps({'response': completed})}\n\n"
                ),
                headers={"content-type": "text/event-stream"},
            )
        )

        deltas = [
            delta
            async for delta in opencode_go_adapter.stream(
                [{"role": "user", "content": "hello"}],
                model_id="muse-spark-1.2-contributor",
                thinking_effort="low",
            )
        ]

        request_body = json.loads(responses_route.calls.last.request.content)
        assert request_body["stream"] is True
        assert request_body["reasoning"] == {"effort": "low", "summary": "auto"}
        assert [delta["type"] for delta in deltas] == [
            "content_delta",
            "reasoning_meta",
            "usage",
            "finish",
        ]

    def test_normalize_response_routes_responses_shape_by_model(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        result = opencode_go_adapter.normalize_response(
            {
                "choices": [{"message": {"role": "assistant", "content": "wrong wire"}}],
                "output": RESPONSES_COMPLETED_RESPONSE["output"],
            },
            model_id="grok-4.5",
        )

        assert result["content"] == "Done"

    def test_normalize_response_infers_responses_shape_without_model_id(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        result = opencode_go_adapter.normalize_response(RESPONSES_COMPLETED_RESPONSE)

        assert result["content"] == "Done"
