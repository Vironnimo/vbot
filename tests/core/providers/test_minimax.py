"""Tests for MiniMaxAdapter."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import ProviderError
from core.providers.minimax import MiniMaxAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

API_KEY = "test-minimax-key"
MINIMAX_URL = "https://api.minimax.io/v1/chat/completions"
MINIMAX_MESSAGES_URL = "https://api.minimax.io/anthropic/v1/messages"
SUCCESS_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}
SAMPLE_MESSAGES = [{"role": "user", "content": "Hello"}]


@pytest.fixture()
def minimax_config() -> ProviderConfig:
    return ProviderConfig(
        id="minimax",
        name="MiniMax",
        adapter="minimax",
        base_url="https://api.minimax.io/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API / Token Plan Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="MINIMAX_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
    )


@pytest.fixture()
def minimax_adapter(minimax_config: ProviderConfig) -> MiniMaxAdapter:
    return MiniMaxAdapter(minimax_config, API_KEY)


@pytest.fixture()
def minimax_subscription_adapter(minimax_config: ProviderConfig) -> MiniMaxAdapter:
    return MiniMaxAdapter(
        minimax_config,
        API_KEY,
        base_url="https://api.minimax.io/anthropic/v1",
        auth_config=AuthConfig(header="Authorization", prefix="Bearer "),
        model_lookup=lambda model_id: MiniMaxAdapter.normalize_catalog_entry({"id": model_id}),
        connection_mode="anthropic_messages",
    )


def test_reasoning_replay_policy_is_full_history(minimax_adapter: MiniMaxAdapter) -> None:
    """MiniMax's own guidance requires cross-turn reasoning replay (probe deferred)."""
    assert minimax_adapter.reasoning_replay_policy("MiniMax-M3") == "full_history"
    assert minimax_adapter.reasoning_replay_policy("MiniMax-M2.7") == "full_history"
    assert minimax_adapter.reasoning_replay_policy("MiniMax-future") == "current_run"


def test_normalize_catalog_entry_maps_m3_capabilities() -> None:
    model = MiniMaxAdapter.normalize_catalog_entry({"id": "MiniMax-M3"}, {"max_tokens": 8192})

    assert model == Model(
        model_id="MiniMax-M3",
        name="MiniMax M3",
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=True),
            input_modalities=("text", "image", "video"),
            output_modalities=("text",),
            supported_parameters=(
                "max_completion_tokens",
                "max_tokens",
                "reasoning_split",
                "stream_options",
                "temperature",
                "thinking",
                "tools",
                "top_p",
            ),
            task_types=(
                "chat",
                "text_output",
                "image_input",
                "image_understanding",
                "video_input",
                "video_understanding",
            ),
        ),
        context_window=1000000,
        max_output_tokens=131072,
    )


def test_normalize_catalog_entry_maps_m2_chat_model() -> None:
    model = MiniMaxAdapter.normalize_catalog_entry({"id": "MiniMax-M2.7"}, {"max_tokens": 8192})

    assert model.model_id == "MiniMax-M2.7"
    assert model.name == "MiniMax M2.7"
    assert model.context_window == 204800
    assert model.max_output_tokens == 65536
    assert model.capabilities.vision is False
    assert model.capabilities.tools is True
    assert model.capabilities.reasoning.supported is True
    assert model.capabilities.input_modalities == ("text",)
    assert model.capabilities.supported_parameters == (
        "max_tokens",
        "reasoning_split",
        "temperature",
        "tools",
        "top_p",
    )


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_maps_m3_active_thinking_to_adaptive(
    minimax_adapter: MiniMaxAdapter,
) -> None:
    route = respx.post(MINIMAX_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await minimax_adapter.send(SAMPLE_MESSAGES, model_id="MiniMax-M3", thinking_effort="high")

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["thinking"] == {"type": "adaptive"}
    assert request_body["reasoning_split"] is True
    assert "reasoning_effort" not in request_body
    assert "reasoning" not in request_body
    assert "include_reasoning" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_maps_m3_none_thinking_to_disabled(
    minimax_adapter: MiniMaxAdapter,
) -> None:
    route = respx.post(MINIMAX_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await minimax_adapter.send(SAMPLE_MESSAGES, model_id="MiniMax-M3", thinking_effort="none")

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["thinking"] == {"type": "disabled"}
    assert "reasoning_split" not in request_body
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_suppresses_openai_reasoning_effort_for_m2(
    minimax_adapter: MiniMaxAdapter,
) -> None:
    route = respx.post(MINIMAX_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await minimax_adapter.send(SAMPLE_MESSAGES, model_id="MiniMax-M2.7", thinking_effort="high")

    request_body = json.loads(route.calls.last.request.content)
    assert "thinking" not in request_body
    assert request_body["reasoning_split"] is True
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_send_defaults_max_tokens_to_recommended_ceiling_for_m2(
    minimax_config: ProviderConfig,
) -> None:
    """Without a caller limit, M2.x sends its recommended ceiling, not the flat 8192."""
    adapter = MiniMaxAdapter(
        minimax_config,
        API_KEY,
        model_lookup=lambda model_id: MiniMaxAdapter.normalize_catalog_entry({"id": model_id}),
    )
    route = respx.post(MINIMAX_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await adapter.send(SAMPLE_MESSAGES, model_id="MiniMax-M2.7", thinking_effort="high")

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["max_tokens"] == 65536


@respx.mock
@pytest.mark.asyncio
async def test_send_defaults_max_tokens_to_recommended_ceiling_for_m3(
    minimax_config: ProviderConfig,
) -> None:
    """Without a caller limit, M3 sends its recommended ceiling, not the flat 8192."""
    adapter = MiniMaxAdapter(
        minimax_config,
        API_KEY,
        model_lookup=lambda model_id: MiniMaxAdapter.normalize_catalog_entry({"id": model_id}),
    )
    route = respx.post(MINIMAX_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await adapter.send(SAMPLE_MESSAGES, model_id="MiniMax-M3", thinking_effort="high")

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["max_tokens"] == 131072


@respx.mock
@pytest.mark.asyncio
async def test_send_explicit_max_tokens_wins_over_ceiling(
    minimax_config: ProviderConfig,
) -> None:
    """An explicit caller limit is never overridden by the ceiling default."""
    adapter = MiniMaxAdapter(
        minimax_config,
        API_KEY,
        model_lookup=lambda model_id: MiniMaxAdapter.normalize_catalog_entry({"id": model_id}),
    )
    route = respx.post(MINIMAX_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await adapter.send(SAMPLE_MESSAGES, model_id="MiniMax-M2.7", max_tokens=1024)

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["max_tokens"] == 1024


def test_normalize_response_extracts_reasoning_details_text(
    minimax_adapter: MiniMaxAdapter,
) -> None:
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Final answer",
                    "reasoning_details": [{"text": "Reasoning trace"}],
                }
            }
        ]
    }

    normalized = minimax_adapter.normalize_response(response)

    assert normalized["content"] == "Final answer"
    assert normalized["reasoning"] == "Reasoning trace"
    assert normalized["reasoning_meta"] == {"reasoning_details": [{"text": "Reasoning trace"}]}


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_replays_reasoning_details_on_history(
    minimax_adapter: MiniMaxAdapter,
) -> None:
    """A historical assistant turn replays reasoning_details back onto the wire."""
    route = respx.post(MINIMAX_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "Hi"},
        {
            "role": "assistant",
            "content": "Earlier answer",
            "reasoning_meta": {"reasoning_details": [{"text": "Earlier reasoning"}]},
        },
        {"role": "user", "content": "Follow-up"},
    ]

    await minimax_adapter.send(history, model_id="MiniMax-M3", thinking_effort="high")

    request_body = json.loads(route.calls.last.request.content)
    assistant_message = request_body["messages"][1]
    assert assistant_message["reasoning_details"] == [{"text": "Earlier reasoning"}]


@respx.mock
@pytest.mark.asyncio
async def test_subscription_uses_messages_wire_and_replays_signed_reasoning(
    minimax_subscription_adapter: MiniMaxAdapter,
) -> None:
    route = respx.post(MINIMAX_MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
            },
        )
    )
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "Hi"},
        {
            "role": "assistant",
            "content": "Earlier answer",
            "reasoning": "Earlier reasoning",
            "reasoning_meta": {
                "content_blocks": [
                    {
                        "type": "thinking",
                        "thinking": "Earlier reasoning",
                        "signature": "signed-trace",
                    }
                ]
            },
        },
        {"role": "user", "content": "Follow-up"},
    ]

    await minimax_subscription_adapter.send(
        history,
        model_id="MiniMax-M2.7",
        thinking_effort="none",
        reasoning_split=True,
    )

    request = route.calls.last.request
    request_body = json.loads(request.content)
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert request_body["max_tokens"] == 65536
    assert "thinking" not in request_body
    assert "output_config" not in request_body
    assert "reasoning_split" not in request_body
    assert request_body["messages"][1]["content"][0] == {
        "type": "thinking",
        "thinking": "Earlier reasoning",
        "signature": "signed-trace",
    }
    assert "cache_control" in json.dumps(request_body)


def test_subscription_normalizes_signed_reasoning_blocks(
    minimax_subscription_adapter: MiniMaxAdapter,
) -> None:
    thinking_block = {
        "type": "thinking",
        "thinking": "Reasoning trace",
        "signature": "signed-trace",
    }
    normalized = minimax_subscription_adapter.normalize_response(
        {
            "role": "assistant",
            "content": [thinking_block, {"type": "text", "text": "Final answer"}],
            "stop_reason": "end_turn",
        },
        model_id="MiniMax-M2.7",
    )

    assert normalized["content"] == "Final answer"
    assert normalized["reasoning"] == "Reasoning trace"
    assert normalized["reasoning_meta"] == {"content_blocks": [thinking_block]}
    assert normalized["terminal_outcome"] == "stop"


def test_subscription_wire_is_text_only(
    minimax_subscription_adapter: MiniMaxAdapter,
) -> None:
    assert minimax_subscription_adapter.wire_media_support("MiniMax-M2.7") == frozenset()


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("temperature", [0, -0.1, 1.1, float("nan")])
async def test_direct_wire_rejects_unsupported_temperature_locally(
    minimax_adapter: MiniMaxAdapter,
    temperature: float,
) -> None:
    route = respx.post(MINIMAX_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    with pytest.raises(ProviderError):
        await minimax_adapter.send(
            SAMPLE_MESSAGES,
            model_id="MiniMax-M2.7",
            temperature=temperature,
        )

    assert route.called is False


@respx.mock
@pytest.mark.asyncio
async def test_subscription_wire_rejects_zero_temperature_locally(
    minimax_subscription_adapter: MiniMaxAdapter,
) -> None:
    route = respx.post(MINIMAX_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json={"content": [], "stop_reason": "end_turn"})
    )

    with pytest.raises(ProviderError):
        await minimax_subscription_adapter.send(
            SAMPLE_MESSAGES,
            model_id="MiniMax-M2.7",
            temperature=0,
        )

    assert route.called is False
