"""Shared OpenRouter test builders: Provider config, Adapter, catalog Models, wire bodies."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openrouter import OPENROUTER_RESPONSES_ENDPOINT, OpenRouterAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

API_KEY = "test-openrouter-key"

BASE_URL = "https://openrouter.ai/api/v1"

CHAT_URL = f"{BASE_URL}/chat/completions"

RESPONSES_URL = f"{BASE_URL}{OPENROUTER_RESPONSES_ENDPOINT}"

# One of the exact GPT-5.6 ids OpenRouter serves through its stateless Responses wire.
RESPONSES_MODEL = "openai/gpt-5.6-sol"

HELLO = [{"role": "user", "content": "Hello"}]

CHAT_SUCCESS = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}


def openrouter_config() -> ProviderConfig:
    return ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
        base_url=BASE_URL,
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


def openrouter_adapter(
    model_lookup: Callable[[str], Model | None] | None = None,
    **kwargs: Any,
) -> OpenRouterAdapter:
    return OpenRouterAdapter(openrouter_config(), API_KEY, model_lookup=model_lookup, **kwargs)


def catalog_model(
    model_id: str,
    *,
    reasoning: bool = True,
    control: str | None = None,
    levels: tuple[str, ...] = (),
    **fields: Any,
) -> Model:
    """A chat catalog Model whose only varying facts are its reasoning controls."""

    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=reasoning, control=control, levels=levels),
        ),
        context_window=128_000,
        max_output_tokens=4096,
        **fields,
    )


def catalog_lookup(*models: Model) -> Callable[[str], Model | None]:
    return {model.model_id: model for model in models}.get


def sent_body(route: respx.Route) -> dict[str, Any]:
    body = json.loads(route.calls.last.request.content)
    assert isinstance(body, dict)
    return body


def chat_sse(*chunks: dict[str, Any]) -> httpx.Response:
    """A Chat Completions SSE stream of ``chunks`` closed by ``[DONE]``."""

    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def responses_sse(*events: tuple[str, dict[str, Any]]) -> httpx.Response:
    """A Responses SSE stream of named ``(event, data)`` frames."""

    body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
