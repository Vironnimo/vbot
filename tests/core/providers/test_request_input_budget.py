"""Scoped Context budgets preserve payloads without repeating wire estimation."""

from __future__ import annotations

from typing import Any

import pytest

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.adapter import request_input_budget
from core.providers.anthropic_compatible import AnthropicCompatibleAdapter
from core.providers.errors import ProviderError
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.openai import OpenAIAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.xai import XAIAdapter

_MODEL_ID = "gpt-4.1"
_MESSAGES = [{"role": "user", "content": "Keep this request unchanged."}]
_TOOLS = [
    {
        "name": "lookup",
        "description": "Look up test data.",
        "parameters": {"type": "object", "properties": {}},
    }
]


def _adapter(wire: str) -> OpenAICompatibleAdapter | AnthropicCompatibleAdapter:
    adapters: dict[
        str, tuple[str, type[OpenAICompatibleAdapter] | type[AnthropicCompatibleAdapter]]
    ] = {
        "chat": ("compatible", OpenAICompatibleAdapter),
        "messages": ("compatible", AnthropicCompatibleAdapter),
        "openai-responses": ("openai", OpenAIAdapter),
        "xai-responses": ("xai", XAIAdapter),
        "copilot-chat": ("github-copilot", GitHubCopilotAdapter),
        "copilot-responses": ("github-copilot", GitHubCopilotAdapter),
    }
    provider_id, adapter_type = adapters[wire]
    model = Model(
        model_id=_MODEL_ID,
        name=_MODEL_ID,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=256_000,
        max_output_tokens=256_000,
        metadata={
            "openai": {"wire_policies": {"api-key": {"protocol": "responses"}}},
            "github_copilot": {
                "vendor": "OpenAI",
                "family": _MODEL_ID,
                "supported_endpoints": [
                    "/responses" if wire == "copilot-responses" else "/chat/completions"
                ],
                "max_prompt_tokens": 200_000,
                "tool_calls": True,
            },
        },
    )
    config = ProviderConfig(
        id=provider_id,
        name=provider_id,
        adapter=provider_id,
        base_url="https://provider.example/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="Test",
                auth=AuthConfig(header="Authorization", prefix="Bearer "),
            )
        ],
    )
    return adapter_type(config, "test-token", model_lookup=lambda _: model)


def _payload(
    adapter: OpenAICompatibleAdapter | AnthropicCompatibleAdapter, wire: str
) -> dict[str, Any]:
    if wire == "copilot-responses":
        assert isinstance(adapter, GitHubCopilotAdapter)
        return adapter._responses_request_kwargs_with_defaults(
            _MESSAGES, _MODEL_ID, {"tools": _TOOLS}
        )
    if wire.endswith("responses"):
        assert isinstance(adapter, OpenAIAdapter)
        return adapter._build_responses_payload(_MESSAGES, model_id=_MODEL_ID, tools=_TOOLS)
    return adapter._build_payload(_MESSAGES, model_id=_MODEL_ID, tools=_TOOLS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire",
    ["chat", "messages", "openai-responses", "xai-responses", "copilot-chat", "copilot-responses"],
)
async def test_scoped_budget_skips_estimation_and_keeps_the_same_payload(wire, monkeypatch):
    adapter = _adapter(wire)
    calls = []

    def estimate(messages, *, model_id, tools=None):
        calls.append((messages, model_id, tools))
        return 150_000

    def unexpected_estimate(*_args, **_kwargs):
        raise AssertionError("A matching Context budget must not re-estimate the request")

    try:
        monkeypatch.setattr(adapter, "estimate_request_input_tokens", estimate)
        standalone = _payload(adapter, wire)
        assert calls
        assert all(call == (_MESSAGES, _MODEL_ID, _TOOLS) for call in calls)
        limit = "max_output_tokens" if "max_output_tokens" in standalone else "max_tokens"
        assert standalone[limit] == 68_500

        calls.clear()
        with request_input_budget("another-model", 250_000):
            assert _payload(adapter, wire) == standalone
        assert calls

        monkeypatch.setattr(adapter, "estimate_request_input_tokens", unexpected_estimate)
        with request_input_budget(_MODEL_ID, 150_000):
            assert _payload(adapter, wire) == standalone
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_scoped_budget_still_enforces_copilot_prompt_limit(monkeypatch):
    adapter = _adapter("copilot-responses")

    def unexpected_estimate(*_args, **_kwargs):
        raise AssertionError("Prompt-limit validation must use the matching Context budget")

    monkeypatch.setattr(adapter, "estimate_request_input_tokens", unexpected_estimate)
    try:
        with request_input_budget(_MODEL_ID, 200_001), pytest.raises(ProviderError) as error:
            _payload(adapter, "copilot-responses")
        assert error.value.retryable is False
    finally:
        await adapter.aclose()
