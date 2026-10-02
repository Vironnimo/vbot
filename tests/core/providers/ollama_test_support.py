"""Shared Ollama test builders for the local native wire and the direct Cloud wire.

The local response fixtures are real payloads captured from a live Ollama 0.24.0
instance on 2026-07-07: Tool Call arguments are JSON objects, streaming is
NDJSON, and usage rides in ``prompt_eval_count``/``eval_count``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import respx

from core.models.models import (
    REASONING_CONTROL_LEVELS,
    REASONING_CONTROL_ON_OFF,
    Capabilities,
    Model,
    ModelRegistry,
    ReasoningCapabilities,
)
from core.providers.ollama import (
    OLLAMA_CLOUD_MODE,
    OLLAMA_LOCAL_MODE,
    OllamaAdapter,
    OllamaCloudAdapter,
)
from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    ProviderConfig,
    ProviderRegistry,
)

LOCAL_CHAT_URL = "http://localhost:11434/api/chat"

CLOUD_CHAT_URL = "https://ollama.com/v1/chat/completions"

RESOURCES = Path(__file__).resolve().parents[3] / "resources"

LOCAL_CONFIG = ProviderConfig(
    id="ollama",
    name="Ollama",
    adapter="ollama",
    base_url="http://localhost:11434",
    models_endpoint="/api/tags",
    connections=[
        ConnectionConfig(
            id="local",
            type="none",
            label="Local",
            auth=AuthConfig(header="", prefix="", credential_key=""),
            mode=OLLAMA_LOCAL_MODE,
        ),
    ],
)

CLOUD_CONFIG = ProviderConfig(
    id="ollama-cloud",
    name="Ollama Cloud",
    adapter="ollama_cloud",
    base_url="https://ollama.com",
    models_endpoint="/api/tags",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API key",
            auth=AuthConfig(
                header="Authorization", prefix="Bearer ", credential_key="OLLAMA_API_KEY"
            ),
            mode=OLLAMA_CLOUD_MODE,
            catalog_requires_credentials=False,
        )
    ],
)

SAMPLE_MESSAGES = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hi"},
]

# Real non-streaming local Tool Call response (ministral-3:8b, live probe).
TOOL_CALL_RESPONSE: dict[str, Any] = {
    "model": "ministral-3:8b",
    "created_at": "2026-07-07T10:00:00.000000Z",
    "message": {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_dmop6zf4",
                "function": {
                    "index": 0,
                    "name": "get_weather",
                    "arguments": {"city": "Berlin"},
                },
            }
        ],
    },
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 611,
    "eval_count": 12,
}

TEXT_RESPONSE: dict[str, Any] = {
    "model": "ministral-3:8b",
    "created_at": "2026-07-07T10:00:00.000000Z",
    "message": {"role": "assistant", "content": "Hello there."},
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 558,
    "eval_count": 4,
}

CLOUD_TEXT_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-ollama-cloud",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "OK",
                "reasoning": "The user requested exactly OK.",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 0, "completion_tokens": 9, "total_tokens": 9},
}


def _model(
    model_id: str,
    reasoning: ReasoningCapabilities,
    *,
    metadata: dict[str, Any] | None = None,
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(vision=False, tools=True, json_mode=False, reasoning=reasoning),
        context_window=32768,
        max_output_tokens=None,
        metadata=metadata or {},
    )


_CLOUD_PROFILE = {
    "ollama": {"remote": True},
    "ollama_cloud": {"interleaved_field": "reasoning"},
}

_MODELS = {
    model.model_id: model
    for model in (
        _model("thinking-model", ReasoningCapabilities(True, REASONING_CONTROL_ON_OFF)),
        _model("plain-model", ReasoningCapabilities(False)),
        _model(
            "gpt-oss:20b",
            ReasoningCapabilities(True, REASONING_CONTROL_LEVELS, ("low", "medium", "high")),
        ),
        _model(
            "deepseek-v4-flash",
            ReasoningCapabilities(True, REASONING_CONTROL_LEVELS, ("high", "max")),
            metadata={"ollama": {"remote": True}},
        ),
        _model(
            "minimax-m3",
            ReasoningCapabilities(True, REASONING_CONTROL_LEVELS, ("low", "medium", "high", "max")),
            metadata=_CLOUD_PROFILE,
        ),
    )
}


def model_lookup(model_id: str) -> Model | None:
    return _MODELS.get(model_id)


def local_adapter(**kwargs: Any) -> OllamaAdapter:
    return OllamaAdapter(LOCAL_CONFIG, "", model_lookup=model_lookup, **kwargs)


def cloud_adapter(
    config: ProviderConfig = CLOUD_CONFIG,
    base_url: str | None = None,
    auth_config: AuthConfig | None = None,
) -> OllamaCloudAdapter:
    return OllamaCloudAdapter(
        config,
        "ollama-secret",
        base_url,
        auth_config,
        model_lookup=model_lookup,
        connection_mode=OLLAMA_CLOUD_MODE,
    )


def bundled_cloud_adapter() -> OllamaCloudAdapter:
    """A direct Cloud Adapter wired like Runtime from the bundled Provider and Model files."""

    models = ModelRegistry.load(RESOURCES)
    config = ProviderRegistry.load(RESOURCES).get("ollama-cloud")
    connection = config.get_connection("api-key")
    return OllamaCloudAdapter(
        config,
        "test-key",
        connection.base_url,
        connection.auth,
        model_lookup=lambda model_id: models.get("ollama-cloud", model_id),
        connection_mode=connection.mode,
    )


def sent_body(route: respx.Route) -> dict[str, Any]:
    payload = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def ndjson(*chunks: dict[str, Any]) -> httpx.Response:
    """A native NDJSON stream: one JSON object per line, no SSE framing."""

    return httpx.Response(200, text="\n".join(json.dumps(chunk) for chunk in chunks) + "\n")


def cloud_sse(*chunks: dict[str, Any]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
