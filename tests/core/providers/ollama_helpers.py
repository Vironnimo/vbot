"""Shared fixtures and fakes for ollama behavior tests.

Uses ``respx`` to mock httpx calls. The response fixtures are real payloads
captured from a live Ollama 0.24.0 instance on 2026-07-07 (see the plan's
live-probe notes): tool-call arguments are JSON objects, streaming is NDJSON,
and usage rides in ``prompt_eval_count``/``eval_count``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx

from core.models.models import (
    REASONING_CONTROL_LEVELS,
    REASONING_CONTROL_ON_OFF,
    Capabilities,
    Model,
    ReasoningCapabilities,
)
from core.providers.ollama import (
    OLLAMA_CLOUD_MODE,
    OLLAMA_LOCAL_MODE,
    OllamaAdapter,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"

OLLAMA_CLOUD_CHAT_URL = "https://ollama.com/v1/chat/completions"

OLLAMA_CONFIG = ProviderConfig(
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

OLLAMA_CLOUD_CONFIG = ProviderConfig(
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

# Real non-streaming tool-call response (ministral-3:8b, live probe).
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

SAMPLE_MESSAGES = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hi"},
]

THINKING_MODEL = Model(
    model_id="thinking-model",
    name="Thinking Model",
    capabilities=Capabilities(
        vision=False,
        tools=True,
        json_mode=False,
        reasoning=ReasoningCapabilities(supported=True, control=REASONING_CONTROL_ON_OFF),
    ),
    context_window=32768,
    max_output_tokens=None,
)

PLAIN_MODEL = Model(
    model_id="plain-model",
    name="Plain Model",
    capabilities=Capabilities(
        vision=False,
        tools=True,
        json_mode=False,
        reasoning=ReasoningCapabilities(supported=False),
    ),
    context_window=32768,
    max_output_tokens=None,
)

GPT_OSS_MODEL = Model(
    model_id="gpt-oss:20b",
    name="GPT-OSS 20B",
    capabilities=Capabilities(
        vision=False,
        tools=True,
        json_mode=False,
        reasoning=ReasoningCapabilities(
            supported=True,
            control=REASONING_CONTROL_LEVELS,
            levels=("low", "medium", "high"),
        ),
    ),
    context_window=131_072,
    max_output_tokens=None,
)

DEEPSEEK_CLOUD_MODEL = Model(
    model_id="deepseek-v4-flash",
    name="DeepSeek V4 Flash",
    capabilities=Capabilities(
        vision=False,
        tools=True,
        json_mode=False,
        reasoning=ReasoningCapabilities(
            supported=True,
            control=REASONING_CONTROL_LEVELS,
            levels=("high", "max"),
        ),
    ),
    context_window=1_048_576,
    max_output_tokens=None,
    metadata={"ollama": {"remote": True}},
)

GLM_CLOUD_MODEL = Model(
    model_id="glm-5.2",
    name="GLM 5.2",
    capabilities=Capabilities(
        vision=False,
        tools=True,
        json_mode=False,
        reasoning=ReasoningCapabilities(
            supported=True,
            control=REASONING_CONTROL_LEVELS,
            levels=("high", "max"),
        ),
    ),
    context_window=1_000_000,
    max_output_tokens=None,
    metadata={
        "ollama": {"remote": True},
        "ollama_cloud": {
            "reasoning_response_field": "reasoning",
        },
    },
    reasoning_replay="full_history",
)

DEEPSEEK_CLOUD_FULL_HISTORY_MODEL = Model(
    model_id="deepseek-v4-flash:0731",
    name="DeepSeek V4 Flash",
    capabilities=Capabilities(
        vision=False,
        tools=True,
        json_mode=False,
        reasoning=ReasoningCapabilities(
            supported=True,
            control=REASONING_CONTROL_LEVELS,
            levels=("low", "high", "max"),
        ),
    ),
    context_window=1_048_576,
    max_output_tokens=65_536,
    metadata={
        "ollama": {"remote": True},
        "ollama_cloud": {"reasoning_response_field": "reasoning"},
    },
)

KIMI_CLOUD_MODEL = Model(
    model_id="kimi-k2.6",
    name="Kimi K2.6",
    capabilities=Capabilities(
        vision=False,
        tools=True,
        json_mode=False,
        reasoning=ReasoningCapabilities(
            supported=True,
            control=REASONING_CONTROL_ON_OFF,
        ),
    ),
    context_window=262_144,
    max_output_tokens=None,
    metadata={
        "ollama": {"remote": True},
        "ollama_cloud": {"reasoning_response_field": "reasoning"},
    },
    reasoning_replay="current_run",
)

MINIMAX_M2_7_CLOUD_MODEL = Model(
    model_id="minimax-m2.7",
    name="MiniMax M2.7",
    capabilities=Capabilities(
        vision=False,
        tools=True,
        json_mode=False,
        reasoning=ReasoningCapabilities(
            supported=True,
            control=REASONING_CONTROL_ON_OFF,
        ),
    ),
    context_window=196_608,
    max_output_tokens=None,
    metadata={
        "ollama": {"remote": True},
        "ollama_cloud": {"reasoning_response_field": "reasoning"},
    },
    reasoning_replay="none",
)

MINIMAX_M3_FULL_HISTORY_MODEL = Model(
    model_id="minimax-m3",
    name="MiniMax M3",
    capabilities=Capabilities(
        vision=False,
        tools=True,
        json_mode=False,
        reasoning=ReasoningCapabilities(
            supported=True,
            control=REASONING_CONTROL_LEVELS,
            levels=("low", "medium", "high", "max"),
        ),
    ),
    context_window=524_288,
    max_output_tokens=None,
    metadata={
        "ollama": {"remote": True},
        "ollama_cloud": {"reasoning_response_field": "reasoning"},
    },
)

_MODELS = {
    "thinking-model": THINKING_MODEL,
    "plain-model": PLAIN_MODEL,
    "gpt-oss:20b": GPT_OSS_MODEL,
    "deepseek-v4-flash": DEEPSEEK_CLOUD_MODEL,
    "minimax-m3": MINIMAX_M3_FULL_HISTORY_MODEL,
    "glm-5.2": GLM_CLOUD_MODEL,
    "deepseek-v4-flash:0731": DEEPSEEK_CLOUD_FULL_HISTORY_MODEL,
    "kimi-k2.6": KIMI_CLOUD_MODEL,
    "minimax-m2.7": MINIMAX_M2_7_CLOUD_MODEL,
}


def _model_lookup(model_id: str) -> Model | None:
    return _MODELS.get(model_id)


@pytest.fixture
def adapter() -> OllamaAdapter:
    return OllamaAdapter(OLLAMA_CONFIG, "", model_lookup=_model_lookup)


def _last_request_payload(route: respx.Route) -> dict[str, Any]:
    payload = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


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


# Streaming (NDJSON)
def _ndjson(*chunks: dict[str, Any]) -> str:
    return "\n".join(json.dumps(chunk) for chunk in chunks) + "\n"
