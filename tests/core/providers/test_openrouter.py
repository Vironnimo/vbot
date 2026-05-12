"""Tests for OpenRouterAdapter."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

API_KEY = "test-openrouter-key"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SUCCESS_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}
SAMPLE_MESSAGES = [{"role": "user", "content": "Hello"}]


@pytest.fixture()
def openrouter_config() -> ProviderConfig:
    return ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
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
        defaults={"max_tokens": 8192},
        extra_headers={"HTTP-Referer": "https://vbot.app", "X-Title": "vBot"},
    )


@pytest.fixture()
def openrouter_adapter(openrouter_config: ProviderConfig) -> OpenRouterAdapter:
    return OpenRouterAdapter(openrouter_config, API_KEY)


def raw_openrouter_model(
    *,
    input_modalities: list[str] | None = None,
    supported_parameters: list[str] | None = None,
    max_completion_tokens: int | None = 64000,
) -> dict:
    return {
        "id": "anthropic/claude-sonnet-4",
        "name": "Anthropic: Claude Sonnet 4",
        "architecture": {"input_modalities": input_modalities or ["text", "image"]},
        "supported_parameters": (
            supported_parameters
            if supported_parameters is not None
            else ["tools", "response_format", "reasoning"]
        ),
        "context_length": 128000,
        "top_provider": {"max_completion_tokens": max_completion_tokens},
    }


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_reasoning_uses_openrouter_wire_format(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await openrouter_adapter.send(
        SAMPLE_MESSAGES,
        model_id="openai/gpt-5.2",
        thinking_effort="xhigh",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"effort": "xhigh"}
    assert request_body["include_reasoning"] is True
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_stream_requests_usage(openrouter_adapter: OpenRouterAdapter) -> None:
    sse_body = 'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
    route = respx.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    async for _ in openrouter_adapter.stream(SAMPLE_MESSAGES, model_id="openai/gpt-5.2"):
        pass

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["stream"] is True
    assert request_body["stream_options"] == {"include_usage": True}


def test_normalize_catalog_entry_maps_all_openrouter_fields() -> None:
    model = OpenRouterAdapter.normalize_catalog_entry(raw_openrouter_model(), {"max_tokens": 8192})

    assert model == Model(
        model_id="anthropic/claude-sonnet-4",
        name="Anthropic: Claude Sonnet 4",
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        ),
        context_window=128000,
        max_output_tokens=64000,
    )


def test_normalize_catalog_entry_uses_provider_default_for_null_max_tokens() -> None:
    model = OpenRouterAdapter.normalize_catalog_entry(
        raw_openrouter_model(max_completion_tokens=None),
        {"max_tokens": 8192},
    )

    assert model.max_output_tokens == 8192


@pytest.mark.parametrize(
    ("supported_parameters", "tools", "json_mode", "reasoning"),
    [
        (["tools"], True, False, False),
        (["response_format"], False, True, False),
        (["structured_outputs"], False, True, False),
        (["reasoning"], False, False, True),
        (["include_reasoning"], False, False, True),
        ([], False, False, False),
    ],
)
def test_supported_parameters_derive_capabilities(
    supported_parameters: list[str],
    tools: bool,
    json_mode: bool,
    reasoning: bool,
) -> None:
    model = OpenRouterAdapter.normalize_catalog_entry(
        raw_openrouter_model(supported_parameters=supported_parameters),
        {},
    )

    assert model.capabilities.tools is tools
    assert model.capabilities.json_mode is json_mode
    assert model.capabilities.reasoning.supported is reasoning


@pytest.mark.parametrize(
    ("input_modalities", "vision"), [(["text", "image"], True), (["text"], False)]
)
def test_input_modalities_derive_vision(input_modalities: list[str], vision: bool) -> None:
    model = OpenRouterAdapter.normalize_catalog_entry(
        raw_openrouter_model(input_modalities=input_modalities),
        {},
    )

    assert model.capabilities.vision is vision
