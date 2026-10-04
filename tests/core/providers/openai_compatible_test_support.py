"""Shared configuration, Models and wire helpers for the OpenAI-compatible Adapter tests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openai_compatible import (
    CHAT_COMPLETIONS_ENDPOINT,
    SSE_DONE_MARKER,
    OpenAICompatibleAdapter,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

MODEL_ID = "gpt-5.2"
API_KEY = "test-api-key-12345"


def _api_key_connection(
    header: str = "Authorization", prefix: str = "Bearer ", credential_key: str = "OPENAI_API_KEY"
) -> ConnectionConfig:
    return ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(header=header, prefix=prefix, credential_key=credential_key),
    )


# The ``openai`` id selects OpenAI's wire profile (``resources/wire/openai.json``:
# explicit ``strict: false``, ``reasoning_effort: "none"``); the defaults
# exercise config fallbacks.
OPENAI_CONFIG = ProviderConfig(
    id="openai",
    name="OpenAI",
    adapter="openai_compatible",
    base_url="https://api.openai.com/v1",
    connections=[_api_key_connection()],
    defaults={"max_tokens": 4096, "temperature": 0.7},
)

OPENAI_MULTI_AUTH_CONFIG = ProviderConfig(
    id="openai",
    name="OpenAI",
    adapter="openai_compatible",
    base_url="https://api.openai.com/v1",
    connections=[
        _api_key_connection(),
        ConnectionConfig(
            id="service-account",
            type="api_key",
            label="Service Account",
            auth=AuthConfig(
                header="x-service-token", prefix="Token ", credential_key="OPENAI_SERVICE_TOKEN"
            ),
        ),
    ],
)

# A generic gateway with extra headers and only an output-limit default.
OPENROUTER_CONFIG = ProviderConfig(
    id="openrouter",
    name="OpenRouter",
    adapter="openai_compatible",
    base_url="https://openrouter.ai/api/v1",
    connections=[_api_key_connection(credential_key="OPENROUTER_API_KEY")],
    defaults={"max_tokens": 4096},
    extra_headers={"HTTP-Referer": "https://vbot.app", "X-Title": "vBot"},
)

# A generic gateway without defaults and with an unprefixed key header.
NO_DEFAULTS_CONFIG = ProviderConfig(
    id="minimal",
    name="Minimal Provider",
    adapter="openai_compatible",
    base_url="https://api.minimal.example/v1",
    connections=[_api_key_connection("x-api-key", "", "MINIMAL_API_KEY")],
)


def chat_url(config: ProviderConfig) -> str:
    """The Chat Completions endpoint a Provider config sends to."""

    return f"{config.base_url}{CHAT_COMPLETIONS_ENDPOINT}"


OPENAI_URL = chat_url(OPENAI_CONFIG)
OPENROUTER_URL = chat_url(OPENROUTER_CONFIG)
MINIMAL_URL = chat_url(NO_DEFAULTS_CONFIG)

SUCCESS_RESPONSE = {
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

SAMPLE_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello"},
]


def make_adapter(
    config: ProviderConfig = OPENAI_CONFIG,
    *,
    model: Model | None = None,
    **kwargs: Any,
) -> OpenAICompatibleAdapter:
    """Build the compatible Adapter, resolving every Model id to ``model`` when given."""

    if model is not None:
        kwargs["model_lookup"] = lambda _model_id: model
    return OpenAICompatibleAdapter(config, kwargs.pop("token_getter", API_KEY), **kwargs)


def catalog_model(
    model_id: str = MODEL_ID,
    *,
    reasoning: bool = True,
    control: str | None = None,
    levels: tuple[str, ...] = (),
    context_window: int = 128_000,
    max_output_tokens: int | None = 4096,
    metadata: Mapping[str, Any] | None = None,
) -> Model:
    """A catalog Model with the reasoning, limit and metadata facts under test."""

    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=reasoning, control=control, levels=levels),
        ),
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        metadata=dict(metadata or {}),
    )


async def send_request(
    adapter: OpenAICompatibleAdapter,
    messages: list[dict[str, Any]] = SAMPLE_MESSAGES,
    *,
    url: str = OPENAI_URL,
    model_id: str = MODEL_ID,
    **kwargs: Any,
) -> httpx.Request:
    """Send once against a mocked Chat Completions endpoint and return the wire request."""

    with respx.mock:
        route = respx.post(url).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        await adapter.send(messages, model_id=model_id, **kwargs)
    return route.calls.last.request


async def sent_payload(
    adapter: OpenAICompatibleAdapter,
    messages: list[dict[str, Any]] = SAMPLE_MESSAGES,
    **kwargs: Any,
) -> dict[str, Any]:
    """The JSON body of one ``send()``."""

    payload: dict[str, Any] = json.loads((await send_request(adapter, messages, **kwargs)).content)
    return payload


def sse(*chunks: Mapping[str, Any] | str, done: bool = True) -> str:
    """Frame chunks as SSE data events; raw strings are inserted verbatim."""

    frames = [
        chunk if isinstance(chunk, str) else f"data: {json.dumps(chunk)}\n\n" for chunk in chunks
    ]
    if done:
        frames.append(f"data: {SSE_DONE_MARKER}\n\n")
    return "".join(frames)


def sse_response(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


async def collect(
    adapter: OpenAICompatibleAdapter,
    messages: list[dict[str, Any]] = SAMPLE_MESSAGES,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Every normalized delta of one ``stream()`` call."""

    return [chunk async for chunk in adapter.stream(messages, model_id=MODEL_ID, **kwargs)]


async def stream_chunks(body: str) -> list[dict[str, Any]]:
    """Stream one mocked SSE body through the compatible Adapter."""

    with respx.mock:
        respx.post(OPENAI_URL).mock(return_value=sse_response(body))
        return await collect(make_adapter())
