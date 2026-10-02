"""Shared configuration, Model profiles and wire bodies for the OpenCode Go Adapter tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers import OpenCodeGoAdapter
from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    ProviderConfig,
    ProviderRegistry,
)

API_KEY = "test-opencode-go-key"
BASE_URL = "https://opencode-go.example/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"
MESSAGES_URL = f"{BASE_URL}/messages"
RESPONSES_URL = f"{BASE_URL}/responses"
HELLO = [{"role": "user", "content": "hello"}]

# One synthetic Model per wire, used wherever only the wire matters.
CHAT_MODEL = "deepseek-v4-flash"
MESSAGES_MODEL = "minimax-m3"
RESPONSES_MODEL = "gpt-5.6-luna"
WIRES = {
    "chat": (CHAT_MODEL, CHAT_URL),
    "messages": (MESSAGES_MODEL, MESSAGES_URL),
    "responses": (RESPONSES_MODEL, RESPONSES_URL),
}

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

_LEVELS: dict[str, tuple[str, ...]] = {
    "gpt-5.6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
    "muse-spark-1.3-contributor": ("minimal", "low", "medium", "high", "xhigh"),
    "grok-4.6": ("low", "medium", "high", "xhigh"),
    "kimi-k3": ("low", "high", "max"),
}

# The catalog protocol hint (models.dev AI SDK package) of the hinted entries.
_NPM: dict[str, str] = {
    "minimax-m2.7": "@ai-sdk/anthropic",
    "minimax-m3": "@ai-sdk/anthropic",
    "gpt-5.6-luna": "@ai-sdk/openai",
    "muse-spark-1.3-contributor": "@ai-sdk/openai",
    "grok-4.6": "@ai-sdk/openai",
}

# Test-owned catalog entries, so wire behavior stays pinned when the live catalog
# changes. Each entry's protocol hint routes it as in the live catalog; wire
# shaping comes from the bundled wire profile (``resources/wire/opencode-go.json``).
CATALOG_IDS = frozenset(
    {
        "minimax-m2.7",
        "minimax-m3",
        "deepseek-v4-flash",
        "kimi-k2.6",
        "kimi-k2.7-code",
        "kimi-k3",
        "gpt-5.6-luna",
        "muse-spark-1.3-contributor",
        "grok-4.6",
    }
)


def go_model(
    model_id: str,
    *,
    context_window: int = 1_000_000,
    max_output_tokens: int = 131_072,
) -> Model:
    """The test-owned OpenCode Go catalog entry for ``model_id``."""

    levels = _LEVELS.get(model_id, ())
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=True,
                control="levels" if levels else "on_off" if model_id == "kimi-k2.6" else None,
                levels=levels,
            ),
        ),
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        metadata={"opencode_go": {"npm": _NPM[model_id]}} if model_id in _NPM else {},
    )


def catalog_lookup(model_id: str) -> Model | None:
    """Resolve the catalog entry for one bare, suffixed or vendor-prefixed id."""

    bare = model_id.split("::", 1)[0]
    for candidate in (model_id, bare, bare.rsplit("/", 1)[-1]):
        if candidate in CATALOG_IDS:
            return go_model(candidate)
    return None


def go_config(**changes: Any) -> ProviderConfig:
    fields: dict[str, Any] = {
        "id": "opencode-go",
        "name": "OpenCode Go",
        "adapter": "opencode_go",
        "base_url": BASE_URL,
        "extra_headers": {"User-Agent": "vBot"},
        "connections": [
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENCODE_API_KEY",
                ),
            )
        ],
    }
    fields.update(changes)
    return ProviderConfig(**fields)


def go_adapter(
    model_lookup: Callable[[str], Model | None] = catalog_lookup,
    **config_changes: Any,
) -> OpenCodeGoAdapter:
    return OpenCodeGoAdapter(go_config(**config_changes), API_KEY, model_lookup=model_lookup)


_RESOURCES = Path(__file__).resolve().parents[3] / "resources"


def bundled_go() -> tuple[OpenCodeGoAdapter, Callable[[str], Model | None], ProviderConfig]:
    """The bundled Provider config and Model catalog, with a lookup for render descriptions."""

    registry = ModelRegistry.load(_RESOURCES)
    config = ProviderRegistry.load(_RESOURCES).get("opencode-go")
    assert config is not None

    def lookup(model_id: str) -> Model | None:
        return registry.get("opencode-go", model_id)

    return OpenCodeGoAdapter(config, "test-token", model_lookup=lookup), lookup, config


RESPONSES_COMPLETED = {
    "id": "resp_1",
    "object": "response",
    "status": "completed",
    "model": "gpt-5.6-luna",
    "output": [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc-blob"},
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

_COMPLETED_BODIES: dict[str, dict[str, Any]] = {
    "chat": {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
    },
    "messages": {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
    },
    "responses": RESPONSES_COMPLETED,
}

_STREAM_BODIES: dict[str, str] = {
    "chat": (
        'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    ),
    "messages": (
        'event: message_delta\ndata: {"type":"message_delta",'
        '"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    ),
    "responses": (
        'event: response.output_text.delta\ndata: {"delta":"Done"}\n\n'
        f"event: response.completed\ndata: {json.dumps({'response': RESPONSES_COMPLETED})}\n\n"
    ),
}


def success_response(wire: str, *, streaming: bool) -> httpx.Response:
    """A completed reply on ``wire`` ("chat", "messages" or "responses")."""

    if streaming:
        return httpx.Response(
            200, text=_STREAM_BODIES[wire], headers={"content-type": "text/event-stream"}
        )
    return httpx.Response(200, json=_COMPLETED_BODIES[wire])


async def go_request(
    adapter: OpenCodeGoAdapter,
    model_id: str,
    *,
    streaming: bool,
    messages: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Send or stream one request; returns the stream deltas (empty for send)."""

    request_messages = messages if messages is not None else HELLO
    if streaming:
        return [
            delta async for delta in adapter.stream(request_messages, model_id=model_id, **kwargs)
        ]
    await adapter.send(request_messages, model_id=model_id, **kwargs)
    return []
