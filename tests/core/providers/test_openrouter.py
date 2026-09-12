"""Openrouter: routing behavior."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openrouter import (
    OpenRouterAdapter,
)
from core.providers.providers import ProviderConfig
from tests.core.providers.openrouter_helpers import (
    API_KEY,
    OPENROUTER_RESPONSES_URL,
    OPENROUTER_URL,
    SAMPLE_MESSAGES,
    SUCCESS_RESPONSE,
)
from tests.core.providers.openrouter_helpers import (
    openrouter_adapter as openrouter_adapter,
)
from tests.core.providers.openrouter_helpers import (
    openrouter_config as openrouter_config,
)


def _gpt_5_6_model(model_id: str) -> Model:
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
                levels=("low", "medium", "high", "xhigh"),
            ),
            supported_parameters=(
                "tools",
                "parallel_tool_calls",
                "response_format",
                "reasoning",
            ),
        ),
        context_window=400000,
        max_output_tokens=128000,
    )


def _ladder_lookup(levels: tuple[str, ...]):
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
                    control="levels",
                    levels=levels,
                ),
            ),
            context_window=128000,
            max_output_tokens=4096,
        )

    return model_lookup


def _control_lookup(control: str):
    def model_lookup(model_id: str) -> Model:
        return Model(
            model_id=model_id,
            name=model_id,
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True, control=control),
            ),
            context_window=128000,
            max_output_tokens=4096,
        )

    return model_lookup


@respx.mock
@pytest.mark.asyncio
async def test_gpt_5_6_uses_responses_and_replays_exact_output(
    openrouter_config: ProviderConfig,
) -> None:
    route = respx.post(OPENROUTER_RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp_new",
                "output": [
                    {"type": "reasoning", "id": "rs_new", "encrypted_content": "cipher-new"},
                    {
                        "type": "message",
                        "id": "msg_new",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "Done"}],
                    },
                ],
                "usage": {"input_tokens": 9, "output_tokens": 4},
            },
        )
    )
    adapter = OpenRouterAdapter(
        openrouter_config,
        API_KEY,
        model_lookup=lambda model_id: _gpt_5_6_model(model_id),
    )
    prior_output = [
        {"type": "reasoning", "id": "rs_old", "encrypted_content": "cipher-old"},
        {
            "type": "message",
            "id": "msg_old",
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "Earlier"}],
        },
    ]

    response = await adapter.send(
        [
            {"role": "user", "content": "First"},
            {
                "role": "assistant",
                "content": "Earlier",
                "phase": "commentary",
                "reasoning_meta": {"response_output": prior_output},
            },
            {"role": "user", "content": "Continue"},
        ],
        model_id="openai/gpt-5.6-sol",
        thinking_effort="high",
        session_id="vbot-session",
        tools=[
            {
                "name": "lookup",
                "description": "Look up a value.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": [],
                },
            }
        ],
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["input"][1:3] == prior_output
    assert request_body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert request_body["include"] == ["reasoning.encrypted_content"]
    assert request_body["store"] is False
    assert request_body["session_id"] == "vbot-session"
    assert request_body["tools"][0]["strict"] is False
    assert request_body["tools"][0]["parameters"]["required"] == []
    assert "temperature" not in request_body
    assert adapter.reasoning_replay_policy("openai/gpt-5.6-sol") == "full_history"

    normalized = adapter.normalize_response(response)
    assert normalized["content"] == "Done"
    assert normalized["phase"] == "final_answer"
    assert normalized["reasoning_meta"]["response_output"] == response["output"]


@respx.mock
@pytest.mark.asyncio
async def test_gpt_5_6_responses_stream_uses_same_replay_policy(
    openrouter_config: ProviderConfig,
) -> None:
    completed = {
        "id": "resp_stream",
        "output": [
            {
                "type": "message",
                "id": "msg_stream",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Hi"}],
            }
        ],
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }
    route = respx.post(OPENROUTER_RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            text=(
                'event: response.output_text.delta\ndata: {"delta":"Hi"}\n\n'
                f"event: response.completed\ndata: {json.dumps({'response': completed})}\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    adapter = OpenRouterAdapter(
        openrouter_config,
        API_KEY,
        model_lookup=lambda model_id: _gpt_5_6_model(model_id),
    )

    deltas = [
        delta
        async for delta in adapter.stream(
            SAMPLE_MESSAGES,
            model_id="openai/gpt-5.6-terra",
            thinking_effort="medium",
        )
    ]

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["stream"] is True
    assert request_body["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert [delta["type"] for delta in deltas] == [
        "content_delta",
        "reasoning_meta",
        "usage",
        "finish",
    ]


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_reasoning_uses_openrouter_wire_format(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await openrouter_adapter.send(
        SAMPLE_MESSAGES,
        model_id="openai/gpt-5.2",
        thinking_effort="xhigh",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"effort": "xhigh"}
    assert request_body["include_reasoning"] is True
    assert "reasoning_effort" not in request_body


@pytest.mark.parametrize(
    ("thinking_effort", "expected_effort", "includes_reasoning"),
    [("none", "none", False), ("max", "xhigh", True)],
)
@respx.mock
@pytest.mark.asyncio
async def test_openrouter_reasoning_maps_to_nearest_supported_effort(
    openrouter_adapter: OpenRouterAdapter,
    thinking_effort: str,
    expected_effort: str,
    includes_reasoning: bool,
) -> None:
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await openrouter_adapter.send(
        SAMPLE_MESSAGES,
        model_id="openai/gpt-5.2",
        thinking_effort=thinking_effort,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"effort": expected_effort}
    assert ("include_reasoning" in request_body) is includes_reasoning
    if includes_reasoning:
        assert request_body["include_reasoning"] is True


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_normalizes_explicit_reasoning_effort_kwarg(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await openrouter_adapter.send(
        SAMPLE_MESSAGES,
        model_id="openai/gpt-5.2",
        reasoning_effort="xhigh",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"effort": "xhigh"}
    assert request_body["include_reasoning"] is True
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_suppresses_reasoning_when_catalog_disables_it(
    openrouter_config: ProviderConfig,
) -> None:
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    adapter = OpenRouterAdapter(
        openrouter_config,
        API_KEY,
        model_lookup=lambda model_id: Model(
            model_id=model_id,
            name=model_id,
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=False),
            ),
            context_window=128000,
            max_output_tokens=4096,
        ),
    )

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="openai/gpt-4o",
        thinking_effort="high",
        reasoning={"effort": "high"},
        include_reasoning=True,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert "reasoning" not in request_body
    assert "include_reasoning" not in request_body
    assert "reasoning_effort" not in request_body


@pytest.mark.parametrize(
    ("thinking_effort", "expected_effort"),
    [("max", "xhigh"), ("medium", "high"), ("high", "high")],
)
@respx.mock
@pytest.mark.asyncio
async def test_openrouter_snaps_against_effective_model_ladder(
    openrouter_config: ProviderConfig,
    thinking_effort: str,
    expected_effort: str,
) -> None:
    """A model with a feed ladder snaps within that ladder, not the provider constant.

    ``deepseek/deepseek-v4-pro`` at OpenRouter publishes ``[high, xhigh]``; every
    selection must land inside it (``max`` -> ``xhigh``, ``medium`` -> ``high``).
    The provider constant (which includes ``low``/``medium``) is bypassed.
    """
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    adapter = OpenRouterAdapter(
        openrouter_config,
        API_KEY,
        model_lookup=_ladder_lookup(("high", "xhigh")),
    )

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="deepseek/deepseek-v4-pro",
        thinking_effort=thinking_effort,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"effort": expected_effort}
    assert request_body["include_reasoning"] is True


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_falls_back_to_constant_without_ladder(
    openrouter_config: ProviderConfig,
) -> None:
    """A model with an empty feed ladder snaps against the provider constant (floor).

    The constant carries ``low`` (the ladder above does not), so a ``low``
    selection surviving as ``low`` proves the floor path is taken when the
    looked-up model has no ladder.
    """
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    adapter = OpenRouterAdapter(
        openrouter_config,
        API_KEY,
        model_lookup=_ladder_lookup(()),
    )

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="some/model-without-ladder",
        thinking_effort="low",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"effort": "low"}


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_on_off_model_toggles_enabled(
    openrouter_config: ProviderConfig,
) -> None:
    """An ``on_off`` model toggles ``reasoning.enabled`` rather than sending an effort."""
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    adapter = OpenRouterAdapter(openrouter_config, API_KEY, model_lookup=_control_lookup("on_off"))

    await adapter.send(SAMPLE_MESSAGES, model_id="some/toggle-model", thinking_effort="high")

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"enabled": True}
    assert request_body["include_reasoning"] is True


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_on_off_model_disables_on_none(
    openrouter_config: ProviderConfig,
) -> None:
    """An ``on_off`` model sends the native off-shape for a ``none`` selection."""
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    adapter = OpenRouterAdapter(openrouter_config, API_KEY, model_lookup=_control_lookup("on_off"))

    await adapter.send(SAMPLE_MESSAGES, model_id="some/toggle-model", thinking_effort="none")

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"enabled": False}
    assert "include_reasoning" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_budget_model_renders_as_effort(
    openrouter_config: ProviderConfig,
) -> None:
    """A ``budget`` model renders as an effort — OpenRouter maps effort→budget itself."""
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    adapter = OpenRouterAdapter(openrouter_config, API_KEY, model_lookup=_control_lookup("budget"))

    await adapter.send(
        SAMPLE_MESSAGES, model_id="anthropic/claude-opus-4-1", thinking_effort="high"
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"effort": "high"}
    assert request_body["include_reasoning"] is True
    assert "budget_tokens" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_stream_requests_usage(openrouter_adapter: OpenRouterAdapter) -> None:
    sse_body = 'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
    route = respx.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    async for _ in openrouter_adapter.stream(SAMPLE_MESSAGES, model_id="openai/gpt-5.2"):
        pass

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["stream"] is True
    assert request_body["stream_options"] == {"include_usage": True}


def test_openrouter_session_id_is_stable_scoped_and_opaque(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    first = openrouter_adapter.request_context_kwargs(
        project_id="vbot",
        agent_id="builder",
        session_id="main",
        prompt_cache_affinity_id="shared-lineage",
    )
    fork = openrouter_adapter.request_context_kwargs(
        project_id="vbot",
        agent_id="builder",
        session_id="reflection-fork",
        prompt_cache_affinity_id="shared-lineage",
    )
    other_lineage = openrouter_adapter.request_context_kwargs(
        project_id="vbot",
        agent_id="builder",
        session_id="main",
        prompt_cache_affinity_id="other-lineage",
    )

    assert first == fork
    assert first != other_lineage
    assert first["session_id"].startswith("vbot-")
    assert "builder" not in first["session_id"]
    assert "main" not in first["session_id"]
    assert "shared-lineage" not in first["session_id"]
    assert len(first["session_id"]) < 256


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_applies_global_allowed_and_blocked_providers(
    openrouter_config: ProviderConfig,
) -> None:
    adapter = OpenRouterAdapter(
        openrouter_config,
        API_KEY,
        routing={
            "default": {
                "mode": "allowed",
                "providers": ["anthropic", "amazon-bedrock"],
                "blocked": ["google-vertex"],
                "allow_fallbacks": False,
            },
            "models": {},
        },
    )
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await adapter.send(SAMPLE_MESSAGES, model_id="anthropic/claude-sonnet-4")

    body = json.loads(route.calls.last.request.content)
    assert body["provider"] == {
        "only": ["anthropic", "amazon-bedrock"],
        "ignore": ["google-vertex"],
        "allow_fallbacks": False,
    }


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_model_override_replaces_selection_and_adds_blocks(
    openrouter_config: ProviderConfig,
) -> None:
    adapter = OpenRouterAdapter(
        openrouter_config,
        API_KEY,
        routing={
            "default": {
                "mode": "allowed",
                "providers": ["anthropic", "amazon-bedrock"],
                "blocked": ["deepinfra"],
                "allow_fallbacks": True,
            },
            "models": {
                "anthropic/claude-sonnet-4": {
                    "mode": "ordered",
                    "providers": ["google-vertex/europe", "anthropic"],
                    "blocked": ["chutes"],
                    "allow_fallbacks": False,
                }
            },
        },
    )
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await adapter.send(SAMPLE_MESSAGES, model_id="anthropic/claude-sonnet-4")

    assert json.loads(route.calls.last.request.content)["provider"] == {
        "order": ["google-vertex/europe", "anthropic"],
        "ignore": ["deepinfra", "chutes"],
        "allow_fallbacks": False,
    }


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_automatic_model_override_clears_global_selection(
    openrouter_config: ProviderConfig,
) -> None:
    adapter = OpenRouterAdapter(
        openrouter_config,
        API_KEY,
        routing={
            "default": {
                "mode": "ordered",
                "providers": ["anthropic"],
                "blocked": ["deepinfra"],
                "allow_fallbacks": True,
            },
            "models": {
                "openai/gpt-5.2": {
                    "mode": "automatic",
                    "providers": [],
                    "blocked": [],
                    "allow_fallbacks": True,
                }
            },
        },
    )
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await adapter.send(SAMPLE_MESSAGES, model_id="openai/gpt-5.2")

    assert json.loads(route.calls.last.request.content)["provider"] == {"ignore": ["deepinfra"]}


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_routing_options_normalize_global_and_model_catalogs(
    openrouter_config: ProviderConfig,
) -> None:
    adapter = OpenRouterAdapter(openrouter_config, API_KEY)
    respx.get("https://openrouter.ai/api/v1/providers").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"name": "Anthropic", "slug": "anthropic"},
                    {"name": "Google", "slug": "google-vertex"},
                ]
            },
        )
    )
    respx.get("https://openrouter.ai/api/v1/models/anthropic/claude-sonnet-4/endpoints").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "endpoints": [
                        {"provider_name": "Google", "tag": "google-vertex/europe"},
                        {"provider_name": "Anthropic", "tag": "anthropic"},
                    ]
                }
            },
        )
    )

    global_options = await adapter.routing_provider_options()
    model_options = await adapter.routing_provider_options("anthropic/claude-sonnet-4")

    assert global_options == [
        {"slug": "anthropic", "name": "Anthropic"},
        {"slug": "google-vertex", "name": "Google"},
    ]
    assert model_options == [
        {"slug": "anthropic", "name": "Anthropic"},
        {"slug": "google-vertex/europe", "name": "Google"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_routing_options_retry_http_500(
    openrouter_config: ProviderConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("core.utils.retry.asyncio.sleep", _no_sleep)
    adapter = OpenRouterAdapter(openrouter_config, API_KEY)
    route = respx.get("https://openrouter.ai/api/v1/providers")
    route.side_effect = [
        httpx.Response(500, text="Internal Server Error"),
        httpx.Response(200, json={"data": [{"name": "Anthropic", "slug": "anthropic"}]}),
    ]

    options = await adapter.routing_provider_options()

    assert options == [{"slug": "anthropic", "name": "Anthropic"}]
    assert route.call_count == 2
