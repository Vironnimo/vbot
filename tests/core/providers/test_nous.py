"""Tests for Nous Portal request, catalog, and runtime policy."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import CatalogEntrySkipped, ProviderError
from core.providers.nous import NOUS_MAX_OUTPUT_TOKENS, NousAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

NOUS_CHAT_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
HELLO = [{"role": "user", "content": "Hello"}]
CHAT_SUCCESS = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}


def _config() -> ProviderConfig:
    return ProviderConfig(
        id="nous",
        name="Nous Portal",
        adapter="nous",
        base_url="https://inference-api.nousresearch.com/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="NOUS_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": NOUS_MAX_OUTPUT_TOKENS, "temperature": 0.7},
        context_window=128000,
    )


def _model(model_id: str, *, reasoning: bool = True) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=reasoning,
                control="levels" if reasoning else None,
                levels=("low", "medium", "high") if reasoning else (),
            ),
            input_modalities=("text", "image"),
            output_modalities=("text",),
            supported_parameters=("reasoning", "tools", "temperature"),
        ),
        context_window=1_000_000,
        max_output_tokens=128_000,
    )


@pytest.fixture()
def adapter() -> NousAdapter:
    models = {"openai/gpt-5.5-pro": _model("openai/gpt-5.5-pro")}
    return NousAdapter(_config(), "nous-secret", model_lookup=models.get)


async def _sent_body(adapter: NousAdapter, **kwargs: Any) -> dict[str, Any]:
    with respx.mock:
        route = respx.post(NOUS_CHAT_URL).mock(return_value=httpx.Response(200, json=CHAT_SUCCESS))
        await adapter.send(HELLO, model_id="openai/gpt-5.5-pro", **kwargs)
    body: dict[str, Any] = json.loads(route.calls.last.request.content)
    return body


@pytest.mark.asyncio
async def test_request_enforces_documented_output_sampling_and_media_contract(
    adapter: NousAdapter,
) -> None:
    body = await _sent_body(adapter, max_output_tokens=100_000, top_p=0.8, seed=42, temperature=1.5)

    assert body["max_tokens"] == NOUS_MAX_OUTPUT_TOKENS
    assert body["temperature"] == 1.5
    for removed in ("max_output_tokens", "top_p", "seed"):
        assert removed not in body, removed
    # The Portal wire documents no native multipart media.
    assert adapter.wire_media_support("openai/gpt-5.5-pro") == frozenset()


@pytest.mark.asyncio
async def test_invalid_temperature_fails_before_network(adapter: NousAdapter) -> None:
    with respx.mock:
        route = respx.post(NOUS_CHAT_URL)
        with pytest.raises(ProviderError) as exc_info:
            await adapter.send(HELLO, model_id="openai/gpt-5.5-pro", temperature=2.1)

    assert exc_info.value.retryable is False
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("effort", "reasoning"),
    [
        pytest.param("high", {"enabled": True, "effort": "high"}, id="effort-uses-nous-object"),
        pytest.param("none", None, id="off-is-omitted"),
    ],
)
@pytest.mark.asyncio
async def test_reasoning_uses_the_nous_object_and_off_is_omitted(
    adapter: NousAdapter, effort: str, reasoning: dict[str, Any] | None
) -> None:
    body = await _sent_body(adapter, thinking_effort=effort)

    assert body.get("reasoning") == reasoning
    assert "reasoning_effort" not in body


def test_catalog_requires_explicit_capability_evidence_and_caps_output() -> None:
    unknown = NousAdapter.normalize_catalog_entry(
        {"id": "future/model", "name": "Future"},
        {},
    )
    rich = NousAdapter.normalize_catalog_entry(
        {
            "id": "vendor/agent-model",
            "name": "Agent Model",
            "supported_parameters": ["tools", "reasoning", "response_format"],
            "context_length": 200000,
            "top_provider": {"max_completion_tokens": 64000},
        },
        {},
    )

    assert unknown.capabilities.tools is False
    assert unknown.capabilities.reasoning.supported is False
    assert unknown.max_output_tokens == NOUS_MAX_OUTPUT_TOKENS
    assert rich.capabilities.tools is True
    assert rich.capabilities.json_mode is True
    assert rich.capabilities.reasoning.supported is True
    assert rich.max_output_tokens == NOUS_MAX_OUTPUT_TOKENS


def test_catalog_keeps_hermes_chat_models_only_in_raw_audit() -> None:
    with pytest.raises(CatalogEntrySkipped, match="not recommended"):
        NousAdapter.normalize_catalog_entry(
            {"id": "Hermes-4-70B", "name": "Hermes 4 70B"},
            {},
        )


def test_tool_call_normalization_preserves_terminal_outcome(adapter: NousAdapter) -> None:
    normalized = adapter.normalize_response(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "Need a Tool",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "weather",
                                    "arguments": '{"city":"Berlin"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        model_id="openai/gpt-5.5-pro",
    )

    assert normalized["reasoning"] == "Need a Tool"
    assert normalized["tool_calls"] == [
        {"id": "call_1", "name": "weather", "arguments": {"city": "Berlin"}}
    ]
    assert normalized["terminal_outcome"] == "tool_calls"


@respx.mock
@pytest.mark.asyncio
async def test_streaming_requires_terminal_done_and_sends_bearer(adapter: NousAdapter) -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    route = respx.post(NOUS_CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )
    )

    deltas = [
        delta
        async for delta in adapter.stream(
            [{"role": "user", "content": "Hello"}],
            model_id="openai/gpt-5.5-pro",
        )
    ]

    assert deltas == [
        {"type": "content_delta", "text": "Hello"},
        {"type": "finish", "reason": "stop"},
    ]
    assert route.calls.last.request.headers["authorization"] == "Bearer nous-secret"
    request = json.loads(route.calls.last.request.content)
    assert request["stream"] is True
    assert request["stream_options"] == {"include_usage": True}


@respx.mock
@pytest.mark.asyncio
async def test_payment_required_is_non_retryable_entitlement_error(adapter: NousAdapter) -> None:
    route = respx.post(NOUS_CHAT_URL).mock(
        return_value=httpx.Response(402, json={"error": "subscription_required"})
    )

    with pytest.raises(ProviderError) as exc_info:
        await adapter.send(
            [{"role": "user", "content": "Hello"}],
            model_id="openai/gpt-5.5-pro",
        )

    assert route.call_count == 1
    assert exc_info.value.retryable is False
