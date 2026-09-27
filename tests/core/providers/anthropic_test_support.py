"""Shared configuration, Models and wire helpers for the Anthropic Adapter tests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import httpx
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.anthropic import (
    ANTHROPIC_METADATA_KEY,
    SUPPORTS_TEMPERATURE_METADATA_FIELD,
    AnthropicAdapter,
)
from core.providers.anthropic_compatible import AnthropicCompatibleAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

MODEL_ID = "claude-sonnet-4-20250219"
API_KEY = "test-anthropic-key-12345"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
CUSTOM_URL = "https://custom.anthropic.example/v1/messages"
MINIMAL_URL = "https://minimal.anthropic.example/v1/messages"

_API_KEY_CONNECTION = ConnectionConfig(
    id="api-key",
    type="api_key",
    label="API Key",
    auth=AuthConfig(header="x-api-key", prefix="", credential_key="ANTHROPIC_API_KEY"),
)

ANTHROPIC_CONFIG = ProviderConfig(
    id="anthropic",
    name="Anthropic",
    adapter="anthropic",
    base_url="https://api.anthropic.com/v1",
    connections=[_API_KEY_CONNECTION],
    defaults={"max_tokens": 4096},
)

ANTHROPIC_MULTI_AUTH_CONFIG = ProviderConfig(
    id="anthropic",
    name="Anthropic",
    adapter="anthropic",
    base_url="https://api.anthropic.com/v1",
    connections=[
        _API_KEY_CONNECTION,
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

# Extra headers plus a sampling default the thinking policy must not refill.
CUSTOM_CONFIG = ProviderConfig(
    id="anthropic-custom",
    name="Anthropic Custom",
    adapter="anthropic",
    base_url="https://custom.anthropic.example/v1",
    connections=[_API_KEY_CONNECTION],
    defaults={"max_tokens": 8192, "temperature": 0.7},
    extra_headers={"X-Custom-Header": "custom-value"},
)

NO_DEFAULTS_CONFIG = ProviderConfig(
    id="anthropic-minimal",
    name="Anthropic Minimal",
    adapter="anthropic",
    base_url="https://minimal.anthropic.example/v1",
    connections=[_API_KEY_CONNECTION],
)

SUCCESS_RESPONSE = {
    "id": "msg_01XFDUDYJGAAC8998t2N3v",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "Hello!"}],
    "model": MODEL_ID,
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 5},
}

SAMPLE_MESSAGES = [{"role": "user", "content": "Hello"}]

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

THINKING_BLOCK = {
    "type": "thinking",
    "thinking": "Need weather.",
    "signature": "opaque-current-turn",
}

CANONICAL_MESSAGES_WITH_TOOL_LOOP = [
    {"role": "system", "model": f"anthropic/{MODEL_ID}", "content": "You are helpful."},
    {"role": "user", "content": "Weather?"},
    {
        "role": "assistant",
        "model": f"anthropic/{MODEL_ID}",
        "content": None,
        "reasoning": "Need weather.",
        "reasoning_meta": {"content_blocks": [THINKING_BLOCK]},
        "tool_calls": [{"id": "toolu_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}],
    },
    {
        "role": "tool",
        "tool_call_id": "toolu_abc",
        "name": "get_weather",
        "content": '{"temp":22}',
    },
]


def make_adapter(
    config: ProviderConfig = ANTHROPIC_CONFIG,
    *,
    model: Model | None = None,
    **kwargs: Any,
) -> AnthropicAdapter:
    """Build the native Adapter, resolving every Model id to ``model`` when given."""

    if model is not None:
        kwargs["model_lookup"] = lambda _model_id: model
    return AnthropicAdapter(config, kwargs.pop("token_getter", API_KEY), **kwargs)


def claude_model(
    *,
    reasoning: bool = True,
    control: str | None = None,
    levels: tuple[str, ...] = (),
    budget_max: int | None = None,
    context_window: int = 200000,
    max_output_tokens: int = 64000,
    anthropic_metadata: Mapping[str, bool] | None = None,
) -> Model:
    """A catalog Claude with the reasoning facts and Anthropic metadata under test."""

    model = Model(
        model_id=MODEL_ID,
        name=MODEL_ID,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=reasoning,
                control=control,
                levels=levels,
                budget_max=budget_max,
            ),
        ),
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )
    if anthropic_metadata:
        model = replace(model, metadata={ANTHROPIC_METADATA_KEY: dict(anthropic_metadata)})
    return model


def sampling_model(*, supports_temperature: bool) -> Model:
    """A Claude carrying the discovery-derived sampling flag."""

    return claude_model(
        anthropic_metadata={SUPPORTS_TEMPERATURE_METADATA_FIELD: supports_temperature}
    )


def strip_cache_control(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload`` without the always-on prompt-cache markers.

    Structural tests assert the underlying wire mapping; marker placement has
    its own tests.
    """

    cleaned = dict(payload)
    if isinstance(cleaned.get("system"), list):
        cleaned["system"] = [_without_cache(block) for block in cleaned["system"]]
    if isinstance(cleaned.get("messages"), list):
        cleaned["messages"] = [
            {**message, "content": [_without_cache(block) for block in message["content"]]}
            if isinstance(message.get("content"), list)
            else message
            for message in cleaned["messages"]
        ]
    return cleaned


def _without_cache(block: Any) -> Any:
    if isinstance(block, dict) and "cache_control" in block:
        return {key: value for key, value in block.items() if key != "cache_control"}
    return block


async def send_request(
    adapter: AnthropicCompatibleAdapter,
    messages: list[Any] = SAMPLE_MESSAGES,
    *,
    url: str = ANTHROPIC_URL,
    model_id: str = MODEL_ID,
    **kwargs: Any,
) -> httpx.Request:
    """Send once against a mocked Messages endpoint and return the wire request."""

    with respx.mock:
        route = respx.post(url).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        await adapter.send(messages, model_id=model_id, **kwargs)
    return route.calls.last.request


async def sent_payload(
    adapter: AnthropicCompatibleAdapter,
    messages: list[Any] = SAMPLE_MESSAGES,
    **kwargs: Any,
) -> dict[str, Any]:
    """The JSON body of one ``send()`` without prompt-cache markers."""

    request = await send_request(adapter, messages, **kwargs)
    return strip_cache_control(json.loads(request.content))


def sse(*events: dict[str, Any] | str, stop: bool = True) -> str:
    """Frame Messages events as SSE; raw strings are inserted verbatim."""

    frames = [
        event
        if isinstance(event, str)
        else f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        for event in events
    ]
    if stop:
        frames.append('event: message_stop\ndata: {"type":"message_stop"}\n\n')
    return "".join(frames)


def sse_response(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
