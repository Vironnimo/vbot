"""OpenRouter request shaping: reasoning render, routing policy, cache affinity and markers,
and the stateless Responses route for GPT-5.6."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from tests.core.providers.openrouter_test_support import (
    CHAT_SUCCESS,
    CHAT_URL,
    HELLO,
    RESPONSES_MODEL,
    RESPONSES_URL,
    catalog_lookup,
    catalog_model,
    openrouter_adapter,
    responses_sse,
    sent_body,
)

EPHEMERAL = {"type": "ephemeral"}

_REASONING_CATALOG = catalog_lookup(
    catalog_model("deepseek/deepseek-v4-pro", control="levels", levels=("high", "xhigh")),
    catalog_model("some/model-without-ladder", control="levels"),
    catalog_model("some/toggle-model", control="on_off"),
    catalog_model("anthropic/claude-opus-4-1", control="budget"),
    catalog_model("openai/gpt-4o", reasoning=False),
)

_ALLOWED_ROUTING = {
    "mode": "allowed",
    "providers": ["anthropic", "amazon-bedrock"],
    "blocked": ["google-vertex"],
    "allow_fallbacks": False,
}


@pytest.mark.parametrize(
    ("model_id", "request_kwargs", "reasoning", "include_reasoning"),
    [
        pytest.param("openai/gpt-5.2", {"thinking_effort": "xhigh"}, {"effort": "xhigh"}, True),
        pytest.param(
            "openai/gpt-5.2",
            {"reasoning_effort": "xhigh"},
            {"effort": "xhigh"},
            True,
            id="generic-reasoning-effort-kwarg",
        ),
        pytest.param(
            "openai/gpt-5.2", {"thinking_effort": "max"}, {"effort": "xhigh"}, True, id="max"
        ),
        pytest.param(
            "openai/gpt-5.2", {"thinking_effort": "none"}, {"effort": "none"}, None, id="off"
        ),
        pytest.param("openai/gpt-5.2", {}, None, None, id="default-omits-reasoning"),
        pytest.param(
            "deepseek/deepseek-v4-pro",
            {"thinking_effort": "medium"},
            {"effort": "high"},
            True,
            id="snaps-within-model-ladder",
        ),
        pytest.param(
            "some/model-without-ladder",
            {"thinking_effort": "low"},
            {"effort": "low"},
            True,
            id="empty-ladder-uses-provider-floor",
        ),
        pytest.param(
            "some/toggle-model",
            {"thinking_effort": "high"},
            {"enabled": True},
            True,
            id="on-off-on",
        ),
        pytest.param(
            "some/toggle-model",
            {"thinking_effort": "none"},
            {"enabled": False},
            None,
            id="on-off-off",
        ),
        pytest.param(
            "anthropic/claude-opus-4-1",
            {"thinking_effort": "high"},
            {"effort": "high"},
            True,
            id="budget-renders-effort",
        ),
        pytest.param(
            "openai/gpt-4o",
            {"thinking_effort": "high", "reasoning": {"effort": "high"}, "include_reasoning": True},
            None,
            None,
            id="catalog-without-reasoning-strips-controls",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_reasoning_renders_openrouter_controls(
    model_id: str,
    request_kwargs: dict[str, Any],
    reasoning: dict[str, Any] | None,
    include_reasoning: bool | None,
) -> None:
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=CHAT_SUCCESS))

    await openrouter_adapter(_REASONING_CATALOG).send(HELLO, model_id=model_id, **request_kwargs)

    body = sent_body(route)
    assert body.get("reasoning") == reasoning
    # An off request must never ask an upstream to return Reasoning.
    assert body.get("include_reasoning") is include_reasoning
    assert "reasoning_effort" not in body
    # OpenRouter maps effort to a budget itself; vBot never sends a token budget.
    assert "budget_tokens" not in body


@pytest.mark.parametrize(
    ("routing", "expected_provider"),
    [
        pytest.param(
            {"default": _ALLOWED_ROUTING, "models": {}},
            {
                "only": ["anthropic", "amazon-bedrock"],
                "ignore": ["google-vertex"],
                "allow_fallbacks": False,
            },
            id="global-allowed-and-blocked",
        ),
        pytest.param(
            {
                "default": {**_ALLOWED_ROUTING, "blocked": ["deepinfra"], "allow_fallbacks": True},
                "models": {
                    "anthropic/claude-sonnet-4": {
                        "mode": "ordered",
                        "providers": ["google-vertex/europe", "anthropic"],
                        "blocked": ["chutes"],
                        "allow_fallbacks": False,
                    }
                },
            },
            {
                "order": ["google-vertex/europe", "anthropic"],
                "ignore": ["deepinfra", "chutes"],
                "allow_fallbacks": False,
            },
            id="model-override-replaces-selection-and-adds-blocks",
        ),
        pytest.param(
            {
                "default": {
                    "mode": "ordered",
                    "providers": ["anthropic"],
                    "blocked": ["deepinfra"],
                    "allow_fallbacks": True,
                },
                "models": {
                    "anthropic/claude-sonnet-4": {
                        "mode": "automatic",
                        "providers": [],
                        "blocked": [],
                        "allow_fallbacks": True,
                    }
                },
            },
            {"ignore": ["deepinfra"]},
            id="automatic-override-clears-global-selection",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_routing_policy_renders_the_provider_preferences(
    routing: dict[str, Any], expected_provider: dict[str, Any]
) -> None:
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=CHAT_SUCCESS))

    await openrouter_adapter(routing=routing).send(HELLO, model_id="anthropic/claude-sonnet-4")

    assert sent_body(route)["provider"] == expected_provider


def test_session_id_is_stable_per_cache_lineage_scoped_and_opaque() -> None:
    adapter = openrouter_adapter()

    def session_id(session: str, lineage: str) -> str:
        kwargs = adapter.request_context_kwargs(
            project_id="vbot",
            agent_id="builder",
            session_id=session,
            prompt_cache_affinity_id=lineage,
        )
        return str(kwargs["session_id"])

    first = session_id("main", "shared-lineage")

    assert session_id("reflection-fork", "shared-lineage") == first
    assert session_id("main", "other-lineage") != first
    assert first.startswith("vbot-")
    for local_value in ("builder", "main", "shared-lineage"):
        assert local_value not in first
    assert len(first) < 256


def _marked(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(part, dict) and "cache_control" in part for part in content
    )


@pytest.mark.parametrize(
    ("model_id", "marked"),
    [
        ("anthropic/claude-sonnet-4.6", True),
        pytest.param("~anthropic/CLAUDE-haiku-latest", True, id="auto-router-any-case"),
        ("openai/gpt-5.2", False),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_only_claude_family_system_prompts_carry_an_envelope_cache_marker(
    model_id: str, marked: bool
) -> None:
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=CHAT_SUCCESS))

    await openrouter_adapter().send(
        [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "Hi"}],
        model_id=model_id,
    )

    messages = sent_body(route)["messages"]
    if marked:
        assert messages[0]["content"] == [
            {"type": "text", "text": "You are helpful.", "cache_control": EPHEMERAL}
        ]
    else:
        # Other families cache implicitly; a stray marker risks a strict-upstream 400.
        assert messages[0]["content"] == "You are helpful."
        assert not any(_marked(message) for message in messages)


def _alternating(count: int) -> list[dict[str, Any]]:
    return [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"m{index}"}
        for index in range(count)
    ]


@pytest.mark.parametrize(
    ("history", "expected_marks"),
    [
        pytest.param(
            [{"role": "system", "content": "Sys"}, *_alternating(6)],
            [True, False, False, False, True, True, True],
            id="system-plus-three-most-recent-within-four-breakpoints",
        ),
        pytest.param(
            [
                {"role": "user", "content": "run the tool"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "call_1", "name": "do_it", "arguments": {"x": 1}}],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "done"},
            ],
            [True, False, True],
            id="pure-tool-call-turn-cannot-carry-a-marker",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_claude_history_markers_roll_over_the_most_recent_markable_messages(
    history: list[dict[str, Any]], expected_marks: list[bool]
) -> None:
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=CHAT_SUCCESS))

    await openrouter_adapter().send(history, model_id="anthropic/claude-sonnet-4.6")

    assert [_marked(message) for message in sent_body(route)["messages"]] == expected_marks


def _gpt_5_6_lookup(model_id: str) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=True, control="levels", levels=("low", "medium", "high", "xhigh")
            ),
            supported_parameters=("tools", "parallel_tool_calls", "response_format", "reasoning"),
        ),
        context_window=400_000,
        max_output_tokens=128_000,
    )


@respx.mock
@pytest.mark.asyncio
async def test_gpt_5_6_sends_stateless_responses_and_replays_exact_output() -> None:
    route = respx.post(RESPONSES_URL).mock(
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
    adapter = openrouter_adapter(
        _gpt_5_6_lookup, routing={"default": _ALLOWED_ROUTING, "models": {}}
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
        model_id=RESPONSES_MODEL,
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

    body = sent_body(route)
    assert body["input"][1:3] == prior_output
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["store"] is False
    assert body["session_id"] == "vbot-session"
    assert body["provider"]["only"] == ["anthropic", "amazon-bedrock"]
    # Explicit opt-out of Responses-style automatic strict Tool normalization.
    assert body["tools"][0]["strict"] is False
    assert body["tools"][0]["parameters"]["required"] == []
    assert "temperature" not in body
    normalized = adapter.normalize_response(response)
    assert normalized["content"] == "Done"
    assert normalized["phase"] == "final_answer"
    assert normalized["reasoning_meta"]["response_output"] == response["output"]


@respx.mock
@pytest.mark.asyncio
async def test_gpt_5_6_streams_through_responses() -> None:
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
    route = respx.post(RESPONSES_URL).mock(
        return_value=responses_sse(
            ("response.output_text.delta", {"delta": "Hi"}),
            ("response.completed", {"response": completed}),
        )
    )

    deltas = [
        delta
        async for delta in openrouter_adapter(_gpt_5_6_lookup).stream(
            HELLO, model_id="openai/gpt-5.6-terra", thinking_effort="medium"
        )
    ]

    body = sent_body(route)
    assert body["stream"] is True
    assert body["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert [delta["type"] for delta in deltas] == [
        "content_delta",
        "reasoning_meta",
        "usage",
        "finish",
    ]


@respx.mock
@pytest.mark.asyncio
async def test_routing_options_normalize_global_and_model_catalogs() -> None:
    adapter = openrouter_adapter()
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

    assert await adapter.routing_provider_options() == [
        {"slug": "anthropic", "name": "Anthropic"},
        {"slug": "google-vertex", "name": "Google"},
    ]
    assert await adapter.routing_provider_options("anthropic/claude-sonnet-4") == [
        {"slug": "anthropic", "name": "Anthropic"},
        {"slug": "google-vertex/europe", "name": "Google"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_routing_options_retry_http_500(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("core.utils.retry._sleep", _no_sleep)
    route = respx.get("https://openrouter.ai/api/v1/providers")
    route.side_effect = [
        httpx.Response(500, text="Internal Server Error"),
        httpx.Response(200, json={"data": [{"name": "Anthropic", "slug": "anthropic"}]}),
    ]

    options = await openrouter_adapter().routing_provider_options()

    assert options == [{"slug": "anthropic", "name": "Anthropic"}]
    assert route.call_count == 2
