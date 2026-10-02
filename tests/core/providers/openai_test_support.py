"""Shared configuration, Models and wire helpers for the OpenAI Adapter tests.

Covers the ``api-key`` connection (``/chat/completions`` fallback and public
``/responses``) and the ``subscription`` connection (Codex Responses over SSE
or WebSocket with ``connection_mode="codex_responses"``).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import respx

from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers.openai import (
    CODEX_RESPONSES_ENDPOINT,
    CODEX_RESPONSES_MODE,
    OpenAIAdapter,
)
from core.providers.openai_compatible import CHAT_COMPLETIONS_ENDPOINT
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.token_getter import TokenGetter

OPENAI_SUBSCRIPTION_URL = f"https://chatgpt.com/backend-api{CODEX_RESPONSES_ENDPOINT}"
OPENAI_PLATFORM_BASE_URL = "https://api.openai.com/v1"
CHAT_COMPLETIONS_URL = f"{OPENAI_PLATFORM_BASE_URL}{CHAT_COMPLETIONS_ENDPOINT}"
PLATFORM_RESPONSES_URL = f"{OPENAI_PLATFORM_BASE_URL}/responses"

ACCOUNT_ID = "acct_vbot"
API_KEY = "sk-test"

SAMPLE_MESSAGES = [
    {"role": "system", "content": "Use concise answers."},
    {"role": "user", "content": "Hello"},
]

CODEX_TOOLS = [
    {
        "name": "lookup",
        "description": "Look up data",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "strict": True,
    }
]

COMPLETED_RESPONSE: dict[str, Any] = {"id": "resp_1", "status": "completed", "output": []}

_RESOURCES_DIR = Path(__file__).resolve().parents[3] / "resources"


def subscription_config(*, include_mode: bool = True) -> ProviderConfig:
    """Provider config matching the ChatGPT ``subscription`` connection."""

    return ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://chatgpt.com/backend-api",
        connections=[
            ConnectionConfig(
                id="subscription",
                type="oauth",
                label="ChatGPT Plus/Pro",
                auth=AuthConfig(header="Authorization", prefix="Bearer "),
                mode=CODEX_RESPONSES_MODE if include_mode else None,
            )
        ],
        defaults={"max_tokens": 8192},
    )


def platform_config() -> ProviderConfig:
    """Provider config matching the OpenAI Platform ``api-key`` connection."""

    return ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url=OPENAI_PLATFORM_BASE_URL,
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENAI_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
    )


def jwt_with_account(account_id: str = ACCOUNT_ID) -> str:
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
        }
    }
    encoded_payload = (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    )
    return f"header.{encoded_payload}.signature"


def codex_adapter(
    token_getter: TokenGetter | str | None = None,
    *,
    config: ProviderConfig | None = None,
    **kwargs: Any,
) -> OpenAIAdapter:
    """The Adapter on the ``subscription`` connection (Codex Responses)."""

    return OpenAIAdapter(
        config or subscription_config(),
        token_getter or jwt_with_account(),
        connection_mode=CODEX_RESPONSES_MODE,
        **kwargs,
    )


def platform_adapter(**kwargs: Any) -> OpenAIAdapter:
    """The Adapter on the ``api-key`` connection."""

    return OpenAIAdapter(platform_config(), API_KEY, **kwargs)


class RotatingTokenGetter:
    """Async token getter that yields the next token on each call."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self.calls = 0

    async def __call__(self) -> str:
        token = self._tokens[min(self.calls, len(self._tokens) - 1)]
        self.calls += 1
        return token


def gpt_reasoning_model(
    model_id: str,
    *,
    connection_context_windows: dict[str, int] | None = None,
) -> Model:
    """A reasoning Model with the full GPT effort ladder on both Connections.

    Its id selects the bundled wire profile (``resources/wire/openai.json``),
    which routes the profiled GPT ids to Responses on the ``api-key``
    connection and adds the public ``all_turns`` reasoning context for
    ``gpt-5.6*``.
    """

    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=True,
                control="levels",
                levels=("none", "low", "medium", "high", "xhigh", "max"),
            ),
            input_modalities=("text", "image", "pdf"),
            output_modalities=("text",),
            supported_parameters=("parallel_tool_calls", "reasoning", "response_format", "tools"),
        ),
        context_window=1_050_000,
        max_output_tokens=128_000,
        connection_context_windows=(
            connection_context_windows
            if connection_context_windows is not None
            else {"api-key": 1_050_000, "subscription": 272_000}
        ),
    )


def ladder_model_lookup(levels: tuple[str, ...]) -> Callable[[str], Model]:
    """A reasoning Model lookup with the given effort ladder (empty: no ladder)."""

    def model_lookup(model_id: str) -> Model:
        return Model(
            model_id=model_id,
            name=model_id,
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(
                    supported=True,
                    control="levels" if levels else None,
                    levels=levels,
                ),
            ),
            context_window=128000,
            max_output_tokens=4096,
        )

    return model_lookup


def bundled_model_lookup() -> Callable[[str], Model]:
    """Resolve ``openai`` Model ids from the bundled Model DB resources."""

    registry = ModelRegistry.load(_RESOURCES_DIR)

    def lookup(model_id: str) -> Model:
        return registry.get("openai", model_id)

    return lookup


def codex_sse(*events: dict[str, Any] | str) -> str:
    """Frame Responses events as Codex SSE; raw strings are inserted verbatim."""

    return "".join(
        event
        if isinstance(event, str)
        else f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        for event in events
    )


def sse_response(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def codex_sse_response(response: dict[str, object]) -> httpx.Response:
    return sse_response(codex_sse({"type": "response.completed", "response": response}))


async def send_codex_request(
    adapter: OpenAIAdapter,
    messages: list[dict[str, Any]] = SAMPLE_MESSAGES,
    *,
    model_id: str = "gpt-5.5",
    **kwargs: Any,
) -> httpx.Request:
    """Send once against a mocked Codex SSE endpoint and return the wire request."""

    with respx.mock:
        route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
            return_value=codex_sse_response(COMPLETED_RESPONSE)
        )
        await adapter.send(messages, model_id=model_id, **kwargs)
    return route.calls.last.request


async def codex_payload(
    adapter: OpenAIAdapter,
    messages: list[dict[str, Any]] = SAMPLE_MESSAGES,
    **kwargs: Any,
) -> dict[str, Any]:
    """The JSON body of one Codex ``send()``."""

    request = await send_codex_request(adapter, messages, **kwargs)
    payload: dict[str, Any] = json.loads(request.content)
    return payload


async def platform_payload(
    adapter: OpenAIAdapter,
    messages: list[dict[str, Any]] = SAMPLE_MESSAGES,
    *,
    model_id: str = "gpt-5.5",
    **kwargs: Any,
) -> dict[str, Any]:
    """The JSON body of one public Responses ``send()`` on the ``api-key`` connection."""

    with respx.mock:
        route = respx.post(PLATFORM_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=COMPLETED_RESPONSE)
        )
        await adapter.send(messages, model_id=model_id, **kwargs)
    payload: dict[str, Any] = json.loads(route.calls.last.request.content)
    return payload
