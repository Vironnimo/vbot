"""Fidelity tests for reasoning replay serialization.

The replay *scope* (how far back reasoning may go) is chat-layer policy; these
tests pin the *fidelity* dimension: each wire declares which class of persisted
reasoning state it carries back, and the base OpenAI-compatible serializer emits
exactly one class — opaque meta when present and allowed, otherwise readable
text, never both — and the request estimator counts exactly that class.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from core.providers.adapter import ProviderAdapter
from core.providers.anthropic_compatible import AnthropicCompatibleAdapter
from core.providers.kimi import KimiAdapter
from core.providers.minimax import MiniMaxAdapter
from core.providers.mistral import MistralAdapter
from core.providers.ollama import OLLAMA_CLOUD_MODE, OllamaCloudAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.reasoning import (
    REASONING_REPLAY_FIDELITY_META_ONLY,
    REASONING_REPLAY_FIDELITY_META_PREFERRED,
    REASONING_REPLAY_FIDELITY_READABLE_ONLY,
)
from core.utils.tokens import estimate_structured_tokens

from .adapter_test_support import bind_connection

API_KEY = "test-api-key-12345"
CHAT_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}
REASONING_KEYS = ("reasoning", "reasoning_content", "reasoning_details", "encrypted_content")

# Large enough that counting an unserialized class would move the estimate.
READABLE = "Readable accounting sentinel. " * 200
DETAILS = [{"type": "reasoning.text", "text": "Metadata accounting sentinel. " * 100}]

ANSWER: dict[str, Any] = {"role": "assistant", "content": "Answer", "tool_calls": None}
READABLE_ONLY_MESSAGE: dict[str, Any] = {**ANSWER, "reasoning": READABLE}
BOTH_CLASSES_MESSAGE: dict[str, Any] = {
    **READABLE_ONLY_MESSAGE,
    "reasoning_meta": {"reasoning_details": DETAILS},
}


def _config(provider_id: str) -> ProviderConfig:
    return ProviderConfig(
        id=provider_id,
        name=provider_id,
        adapter="openai_compatible",
        base_url=f"https://{provider_id}.example/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key=f"{provider_id.upper()}_API_KEY",
                ),
            )
        ],
    )


@pytest.mark.parametrize(
    ("build_adapter", "model_id", "expected"),
    [
        # OpenRouter's documented contract: reasoning_details supersede plaintext.
        pytest.param(
            lambda: OpenRouterAdapter(_config("openrouter"), API_KEY),
            "anthropic/claude",
            REASONING_REPLAY_FIDELITY_META_PREFERRED,
            id="openrouter-inherits-meta-preferred",
        ),
        # The M2.x key-wire captures reasoning_details; meta must keep winning.
        pytest.param(
            lambda: MiniMaxAdapter(_config("minimax"), API_KEY),
            "minimax-m2.5",
            REASONING_REPLAY_FIDELITY_META_PREFERRED,
            id="minimax-key-wire-declares-nothing-narrower",
        ),
        # The subscription speaks Messages: only signed thinking blocks replay.
        pytest.param(
            lambda: bind_connection(
                MiniMaxAdapter(_config("minimax"), API_KEY),
                provider_id="minimax",
                connection_id="subscription",
                model_lookup=None,
            ),
            "MiniMax-M2.7",
            REASONING_REPLAY_FIDELITY_META_ONLY,
            id="minimax-subscription-messages-meta-only",
        ),
        pytest.param(
            lambda: KimiAdapter(_config("kimi"), API_KEY),
            "kimi-k3",
            REASONING_REPLAY_FIDELITY_READABLE_ONLY,
            id="kimi-readable-only",
        ),
        pytest.param(
            lambda: OllamaCloudAdapter(
                _config("ollama-cloud"), "ollama-secret", connection_mode=OLLAMA_CLOUD_MODE
            ),
            "glm-5.2",
            REASONING_REPLAY_FIDELITY_READABLE_ONLY,
            id="ollama-cloud-readable-only",
        ),
        pytest.param(
            lambda: AnthropicCompatibleAdapter(_config("anthropic-compatible"), "secret"),
            "claude",
            REASONING_REPLAY_FIDELITY_META_ONLY,
            id="anthropic-compatible-meta-only",
        ),
        pytest.param(
            lambda: MistralAdapter(_config("mistral"), API_KEY),
            "mistral-large",
            REASONING_REPLAY_FIDELITY_META_ONLY,
            id="mistral-meta-only",
        ),
    ],
)
def test_wire_declares_its_reasoning_replay_fidelity(
    build_adapter: Callable[[], ProviderAdapter], model_id: str, expected: str
) -> None:
    assert build_adapter().reasoning_replay_fidelity(model_id) == expected


@pytest.mark.parametrize(
    ("adapter_class", "provider_id", "message", "expected_reasoning"),
    [
        pytest.param(
            OpenAICompatibleAdapter,
            "minimal",
            BOTH_CLASSES_MESSAGE,
            {"reasoning_details": DETAILS},
            id="meta-preferred-meta-supersedes-readable",
        ),
        pytest.param(
            OpenAICompatibleAdapter,
            "minimal",
            {**READABLE_ONLY_MESSAGE, "reasoning_meta": {"encrypted_content": "opaque-bytes"}},
            {"encrypted_content": "opaque-bytes"},
            id="meta-preferred-encrypted-content-alone-counts-as-meta",
        ),
        pytest.param(
            OpenAICompatibleAdapter,
            "minimal",
            READABLE_ONLY_MESSAGE,
            {"reasoning_content": READABLE},
            id="meta-preferred-readable-only-turn-replays-readably",
        ),
        pytest.param(
            OpenAICompatibleAdapter,
            "minimal",
            ANSWER,
            {},
            id="meta-preferred-turn-without-reasoning-carries-nothing",
        ),
        pytest.param(
            KimiAdapter,
            "kimi",
            BOTH_CLASSES_MESSAGE,
            {"reasoning_content": READABLE},
            id="readable-only-strips-stray-meta",
        ),
        pytest.param(
            MistralAdapter,
            "mistral",
            READABLE_ONLY_MESSAGE,
            {},
            id="meta-only-never-emits-readable-field",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_replay_serializes_and_estimates_exactly_one_reasoning_class(
    adapter_class: type[OpenAICompatibleAdapter],
    provider_id: str,
    message: dict[str, Any],
    expected_reasoning: dict[str, Any],
) -> None:
    route = respx.post(f"https://{provider_id}.example/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=CHAT_RESPONSE)
    )
    adapter = adapter_class(_config(provider_id), API_KEY)
    history = [
        {"role": "user", "content": "Question"},
        dict(message),
        {"role": "user", "content": "Next"},
    ]

    try:
        await adapter.send(history, model_id="m")
        estimate = adapter.estimate_request_input_tokens(history, model_id="m")
    finally:
        await adapter.aclose()

    wire_messages = json.loads(route.calls.last.request.content)["messages"]
    replayed = wire_messages[1]
    assert {key: replayed[key] for key in REASONING_KEYS if key in replayed} == expected_reasoning
    assert estimate == estimate_structured_tokens(wire_messages)[0]
