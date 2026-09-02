"""Tests for xAI's Responses and per-Model reasoning policy."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.xai import XAIAdapter

XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"
SAMPLE_MESSAGES = [{"role": "user", "content": "Hello"}]
SUCCESS_RESPONSE = {
    "id": "response-id",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
}


def _model(
    model_id: str,
    *,
    reasoning: bool,
    levels: tuple[str, ...] = (),
    tools: bool = True,
    supported_parameters: tuple[str, ...] | None = None,
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=True,
            tools=tools,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=reasoning,
                control="levels" if levels else None,
                levels=levels,
            ),
            input_modalities=("text", "image"),
            output_modalities=("text",),
            supported_parameters=(
                supported_parameters
                if supported_parameters is not None
                else (
                    "max_output_tokens",
                    "parallel_tool_calls",
                    "prompt_cache_key",
                    "reasoning_effort",
                    "response_format",
                    "service_tier",
                    "temperature",
                    "tools",
                    "top_p",
                )
            ),
        ),
        context_window=500000,
        max_output_tokens=30000,
    )


@pytest.fixture()
def models() -> dict[str, Model]:
    return {
        "grok-4.5": _model(
            "grok-4.5",
            reasoning=True,
            levels=("low", "medium", "high"),
        ),
        "grok-4.3": _model(
            "grok-4.3",
            reasoning=True,
            levels=("none", "low", "medium", "high"),
        ),
        "grok-4.20-0309-reasoning": _model(
            "grok-4.20-0309-reasoning",
            reasoning=True,
        ),
        "grok-4.20-0309-non-reasoning": _model(
            "grok-4.20-0309-non-reasoning",
            reasoning=False,
        ),
        "grok-4.20-multi-agent-0309": _model(
            "grok-4.20-multi-agent-0309",
            reasoning=True,
            levels=("low", "medium", "high", "xhigh"),
            tools=False,
            supported_parameters=(
                "prompt_cache_key",
                "reasoning_effort",
                "response_format",
                "service_tier",
                "temperature",
                "top_p",
            ),
        ),
    }


@pytest.fixture()
def xai_adapter(models: dict[str, Model]) -> XAIAdapter:
    config = ProviderConfig(
        id="xai",
        name="xAI",
        adapter="xai",
        base_url="https://api.x.ai/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="XAI_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
    )
    return XAIAdapter(config, "xai-secret", model_lookup=models.get)


@respx.mock
@pytest.mark.asyncio
async def test_grok_45_maps_none_to_low_and_sends_xai_responses_fields(
    xai_adapter: XAIAdapter,
) -> None:
    route = respx.post(XAI_RESPONSES_URL).mock(
        return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
    )

    await xai_adapter.send(
        SAMPLE_MESSAGES,
        model_id="grok-4.5",
        thinking_effort="none",
        prompt_cache_key="cache-affinity",
        service_tier="priority",
    )

    body = json.loads(route.calls.last.request.content)
    assert route.calls.last.request.headers["authorization"] == "Bearer xai-secret"
    assert body["reasoning"] == {"effort": "low", "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["prompt_cache_key"] == "cache-affinity"
    assert body["service_tier"] == "priority"
    assert body["max_output_tokens"] == 30000
    assert body["store"] is False


@respx.mock
@pytest.mark.asyncio
async def test_grok_43_can_disable_reasoning(xai_adapter: XAIAdapter) -> None:
    route = respx.post(XAI_RESPONSES_URL).mock(
        return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
    )

    await xai_adapter.send(
        SAMPLE_MESSAGES,
        model_id="grok-4.3",
        thinking_effort="none",
    )

    body = json.loads(route.calls.last.request.content)
    assert "reasoning" not in body
    assert body["include"] == ["reasoning.encrypted_content"]


@respx.mock
@pytest.mark.asyncio
async def test_fixed_reasoning_model_omits_effort_but_keeps_encrypted_replay(
    xai_adapter: XAIAdapter,
) -> None:
    route = respx.post(XAI_RESPONSES_URL).mock(
        return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
    )

    await xai_adapter.send(
        SAMPLE_MESSAGES,
        model_id="grok-4.20-0309-reasoning",
        thinking_effort="high",
    )

    body = json.loads(route.calls.last.request.content)
    assert "reasoning" not in body
    assert body["include"] == ["reasoning.encrypted_content"]


@respx.mock
@pytest.mark.asyncio
async def test_non_reasoning_model_strips_reasoning_and_invalid_service_tier(
    xai_adapter: XAIAdapter,
) -> None:
    route = respx.post(XAI_RESPONSES_URL).mock(
        return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
    )

    await xai_adapter.send(
        SAMPLE_MESSAGES,
        model_id="grok-4.20-0309-non-reasoning",
        thinking_effort="high",
        include_reasoning=True,
        service_tier="untrusted-tier",
    )

    body = json.loads(route.calls.last.request.content)
    assert "reasoning" not in body
    assert "include" not in body
    assert "service_tier" not in body


def test_multi_agent_preserves_xhigh_effort(xai_adapter: XAIAdapter) -> None:
    payload = xai_adapter._build_responses_payload(
        SAMPLE_MESSAGES,
        model_id="grok-4.20-multi-agent-0309",
        thinking_effort="xhigh",
        tools=[{"type": "function", "function": {"name": "lookup"}}],
    )

    assert payload["reasoning"] == {"effort": "xhigh", "summary": "auto"}
    assert "tools" not in payload
    assert "max_output_tokens" not in payload


def test_output_limit_budgets_against_wire_items_not_persisted_meta(
    xai_adapter: XAIAdapter,
) -> None:
    """A long reasoning session must not fail the local context clamp.

    The persisted ``reasoning_meta`` carries redundant copies of the reasoning
    items (``response_output``, ``reasoning_items``, ``encrypted_content``) that
    never reach the wire. The output-limit clamp must budget against the actual
    Responses input items, so a session that still fits the context window keeps
    working instead of raising "leaves no output capacity".
    """

    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "opaque",
    }
    messages: list[dict[str, Any]] = []
    for index in range(40):
        messages.append(
            {
                "role": "assistant",
                "content": f"Step {index}.",
                "reasoning_meta": {
                    "response_output": [dict(reasoning_item)],
                    "reasoning_items": [dict(reasoning_item)],
                    "encrypted_content": ["opaque"],
                },
                "tool_calls": [{"id": f"call_{index}", "name": "search", "arguments": {}}],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "name": "search",
                "content": "result",
            }
        )
    messages.append({"role": "user", "content": "Continue"})

    payload = xai_adapter._build_responses_payload(
        messages,
        model_id="grok-4.5",
    )

    assert payload["max_output_tokens"] == 30000


def test_request_context_uses_shared_prompt_cache_affinity(xai_adapter: XAIAdapter) -> None:
    assert xai_adapter.request_context_kwargs(
        agent_id="agent",
        session_id="session",
        prompt_cache_affinity_id="shared-prefix",
    ) == {"prompt_cache_key": "shared-prefix"}


def test_xai_media_is_model_scoped_and_reasoning_defaults_to_full_history(
    xai_adapter: XAIAdapter,
) -> None:
    assert xai_adapter.wire_media_support("grok-4.5") == {
        "image/jpeg",
        "image/png",
    }
    assert xai_adapter.reasoning_replay_policy("grok-4.5") == "full_history"
    assert xai_adapter.reasoning_replay_policy("grok-4.20-0309-non-reasoning") == "full_history"
    assert xai_adapter.reasoning_replay_policy("future-model") == "full_history"


def test_xai_discovery_headers_keep_connection_header_without_account_routing() -> None:
    config = ProviderConfig(
        id="xai",
        name="xAI",
        adapter="xai",
        base_url="https://api.x.ai/v1",
        connections=[],
        defaults={},
    )
    headers = XAIAdapter.discovery_headers(
        config,
        "xai-access-token",
        {"Authorization": "Bearer xai-access-token"},
    )
    assert headers == {"Authorization": "Bearer xai-access-token"}
    assert XAIAdapter.discovery_params() == {}


@pytest.mark.asyncio
async def test_xai_resolve_discovery_params_makes_no_network_call() -> None:
    async def _fetch_json(url: str) -> object:
        raise AssertionError("xAI discovery must not fetch external metadata")

    resolved = await XAIAdapter.resolve_discovery_params(_fetch_json)
    assert resolved == {}
