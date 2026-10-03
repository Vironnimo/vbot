"""OpenAI Adapter requests: wire selection, payload shaping, headers and reasoning efforts."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
import respx

from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES
from core.providers.errors import ProviderAuthError
from core.providers.openai import (
    CODEX_EXTRA_HEADERS,
    OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS,
    OpenAIAdapter,
)

from .openai_test_support import (
    ACCOUNT_ID,
    API_KEY,
    CHAT_COMPLETIONS_URL,
    CODEX_TOOLS,
    OPENAI_SUBSCRIPTION_URL,
    PLATFORM_RESPONSES_URL,
    SAMPLE_MESSAGES,
    bundled_model_lookup,
    codex_adapter,
    codex_payload,
    codex_sse_response,
    gpt_reasoning_model,
    jwt_with_account,
    ladder_model_lookup,
    platform_adapter,
    platform_payload,
    send_codex_request,
    subscription_config,
)

# ---------------------------------------------------------------------------
# api-key connection: Chat Completions fallback and public Responses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("completion", "outcome"),
    [
        pytest.param(
            {
                "id": "chatcmpl-1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hello back"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            },
            {"usage": {"input_tokens": 1, "output_tokens": 2}, "terminal_outcome": "stop"},
            id="finished-with-usage",
        ),
        pytest.param(
            {"choices": [{"message": {"role": "assistant", "content": "Hello back"}}]},
            {"terminal_outcome": "unknown"},
            id="no-finish-reason-or-usage",
        ),
    ],
)
@pytest.mark.asyncio
async def test_api_key_fallback_posts_chat_completions_without_codex_routing(
    completion, outcome
) -> None:
    """Unprofiled Models use ``/chat/completions``; Codex headers and context stay off."""

    adapter = platform_adapter()
    context = adapter.request_context_kwargs(
        agent_id="orchestrator",
        session_id="sess-42",
        prompt_cache_affinity_id="shared-cache-lineage",
    )

    with respx.mock:
        route = respx.post(CHAT_COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json=completion)
        )
        response = await adapter.send(SAMPLE_MESSAGES, model_id="gpt-4.1", **context)

    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    for codex_header in (
        "OpenAI-Beta",
        "originator",
        "chatgpt-account-id",
        "session_id",
        "x-client-request-id",
    ):
        assert codex_header not in request.headers
    payload = json.loads(request.content)
    assert 0 < payload.pop("max_tokens") < 8192
    assert payload == {"model": "gpt-4.1", "messages": SAMPLE_MESSAGES}
    assert adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Hello back",
        "reasoning": None,
        "reasoning_meta": None,
        "tool_calls": None,
        **outcome,
    }


@pytest.mark.asyncio
async def test_platform_401_does_not_refresh_static_api_key() -> None:
    """Static API keys never acquire an OAuth recovery capability."""

    adapter = platform_adapter(model_lookup=gpt_reasoning_model)

    with respx.mock:
        route = respx.post(PLATFORM_RESPONSES_URL).mock(
            return_value=httpx.Response(401, json={"error": {"message": "Invalid API key"}})
        )
        with pytest.raises(ProviderAuthError):
            await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.6-terra")

    assert route.call_count == 1


@pytest.mark.parametrize(
    ("model_id", "effort", "reasoning_context"),
    [
        pytest.param("gpt-5.6-sol", "high", "all_turns", id="gpt-5.6-all-turns"),
        pytest.param("gpt-5.5", None, None, id="earlier-model-without-context"),
    ],
)
@pytest.mark.asyncio
async def test_platform_responses_sends_reasoning_context_only_when_the_wire_profile_declares_it(
    model_id: str, effort: str | None, reasoning_context: str | None
) -> None:
    adapter = platform_adapter(model_lookup=gpt_reasoning_model)
    output = [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
        {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Done."}],
        },
    ]
    completed: dict[str, Any] = {"id": "resp_1", "status": "completed", "output": output}
    if reasoning_context is not None:
        completed["reasoning"] = {"context": reasoning_context}

    with respx.mock:
        route = respx.post(PLATFORM_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=completed)
        )
        response = await adapter.send(SAMPLE_MESSAGES, model_id=model_id, thinking_effort=effort)

    payload = json.loads(route.calls.last.request.content)
    normalized = adapter.normalize_response(response, model_id=model_id)
    if reasoning_context is None:
        assert "reasoning" not in payload
    else:
        assert payload["reasoning"] == {
            "effort": effort,
            "summary": "auto",
            "context": reasoning_context,
        }
    assert payload["store"] is False
    assert "stream" not in payload
    assert normalized["content"] == "Done."
    assert normalized["phase"] == "final_answer"
    assert normalized["reasoning_meta"]["response_output"] == output
    assert normalized["reasoning_meta"].get("reasoning_context") == reasoning_context
    assert adapter.reasoning_replay_policy(model_id) == "full_history"


@pytest.mark.asyncio
async def test_platform_output_budget_uses_the_api_key_context_window() -> None:
    """Output budgeting clamps against the active Connection's window, not the Model-wide one."""

    model = gpt_reasoning_model(
        "gpt-5.5", connection_context_windows={"api-key": 16_000, "subscription": 272_000}
    )
    adapter = platform_adapter(model_lookup=lambda _model_id: model)

    payload = await platform_payload(adapter, model_id="gpt-5.5")

    assert 0 < payload["max_output_tokens"] < 16_000


@pytest.mark.parametrize(
    ("adapter_factory", "model_id", "expected"),
    [
        pytest.param(
            platform_adapter,
            "gpt-4.1",
            IMAGE_WIRE_MEDIA_TYPES | {"audio/wav", "audio/mpeg", "application/pdf"},
            id="chat-completions",
        ),
        pytest.param(
            platform_adapter,
            "gpt-5.5",
            IMAGE_WIRE_MEDIA_TYPES | {"application/pdf"},
            id="platform-responses",
        ),
        pytest.param(codex_adapter, "gpt-5.5", IMAGE_WIRE_MEDIA_TYPES, id="codex-responses"),
    ],
)
def test_wire_media_support_follows_the_selected_wire(adapter_factory, model_id, expected) -> None:
    assert adapter_factory().wire_media_support(model_id) == expected


# ---------------------------------------------------------------------------
# subscription connection: Codex Responses request shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_send_posts_responses_payload_with_account_and_beta_headers() -> None:
    """Codex send() targets ``/codex/responses`` with only the adapter-owned headers."""

    access_token = jwt_with_account()
    config = replace(subscription_config(), extra_headers={"X-Injected": "leak"})
    adapter = codex_adapter(access_token, config=config)
    output = [{"type": "message", "content": [{"type": "output_text", "text": "Hi"}]}]

    with respx.mock:
        route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
            return_value=codex_sse_response(
                {
                    "id": "resp_1",
                    "status": "completed",
                    "output": output,
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                }
            )
        )
        response = await adapter.send(
            SAMPLE_MESSAGES,
            model_id="gpt-5-codex",
            thinking_effort="max",
            response_format={"type": "json_object"},
            temperature=0.2,
            tools=[
                {
                    "name": "search",
                    "description": "Search docs",
                    "parameters": {"type": "object"},
                    "strict": True,
                }
            ],
        )

    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {access_token}"
    assert request.headers["chatgpt-account-id"] == ACCOUNT_ID
    assert request.headers["OpenAI-Beta"] == CODEX_EXTRA_HEADERS["OpenAI-Beta"]
    assert request.headers["originator"] == CODEX_EXTRA_HEADERS["originator"]
    assert "X-Injected" not in request.headers
    assert json.loads(request.content) == {
        "model": "gpt-5-codex",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
        "instructions": "Use concise answers.",
        "tools": [
            {
                "type": "function",
                "name": "search",
                "description": "Search docs",
                "parameters": {"type": "object"},
                "strict": False,
            }
        ],
        "reasoning": {"effort": "xhigh", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "text": {"format": {"type": "json_object"}},
        "store": False,
        "stream": True,
    }
    assert adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Hi",
        "reasoning": None,
        "reasoning_meta": {"response_id": "resp_1", "response_output": output},
        "tool_calls": None,
        "usage": {"input_tokens": 2, "output_tokens": 3},
        "terminal_outcome": "stop",
    }


@pytest.mark.parametrize("top_p", [None, 0.9])
@pytest.mark.asyncio
async def test_codex_payload_adds_required_fields_and_drops_rejected_parameters(top_p) -> None:
    """Codex needs instructions and ``store: false`` and rejects output limits and top_p."""

    payload = await codex_payload(
        codex_adapter(),
        [{"role": "user", "content": "Hello"}],
        model_id="gpt-5.6-luna",
        max_tokens=2048,
        max_output_tokens=1024,
        top_p=top_p,
    )

    assert payload["instructions"] == OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS
    assert payload["store"] is False
    for rejected in ("max_tokens", "max_output_tokens", "top_p"):
        assert rejected not in payload


@pytest.mark.asyncio
async def test_codex_send_skips_output_limit_clamp() -> None:
    """Codex must not abort a still-fitting request over a reserve it never sends."""

    adapter = codex_adapter(model_lookup=ladder_model_lookup(("low", "medium", "high", "xhigh")))
    reasoning_item = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque" * 20_000}
    huge_messages: list[dict[str, Any]] = [
        {"role": "system", "content": "Use concise answers."},
        {"role": "user", "content": "x" * 50_000},
        {
            "role": "assistant",
            "content": "ok",
            "reasoning_meta": {
                "response_output": [reasoning_item],
                "reasoning_items": [reasoning_item],
                "encrypted_content": ["opaque" * 20_000],
            },
        },
        {"role": "user", "content": "continue"},
    ]

    payload = await codex_payload(adapter, huge_messages, model_id="gpt-5.6-luna")

    assert "max_output_tokens" not in payload
    assert "max_tokens" not in payload


@pytest.mark.parametrize(
    ("context", "expected_scope"),
    [
        pytest.param(
            {
                "agent_id": "orchestrator",
                "session_id": "sess-42",
                "prompt_cache_affinity_id": "shared-cache-lineage",
            },
            "shared-cache-lineage",
            id="affinity",
        ),
        pytest.param(None, None, id="no-conversation"),
    ],
)
@pytest.mark.asyncio
async def test_codex_sse_cache_scope_headers_follow_the_request_context(
    context, expected_scope
) -> None:
    """The cache affinity rides on headers only; no conversation means no blank headers."""

    adapter = codex_adapter(codex_transport="sse")
    request_kwargs = adapter.request_context_kwargs(**context) if context else {}
    if context:
        assert request_kwargs == {
            "conversation_id": "orchestrator:sess-42",
            "prompt_cache_affinity_id": "shared-cache-lineage",
        }

    request = await send_codex_request(adapter, model_id="gpt-5-codex", **request_kwargs)

    assert request.headers.get("session_id") == expected_scope
    assert request.headers.get("x-client-request-id") == expected_scope
    body = json.loads(request.content)
    assert "conversation_id" not in body
    assert "prompt_cache_affinity_id" not in body


def test_codex_discovery_headers_add_account_routing_and_beta_headers() -> None:
    config = subscription_config()
    connection = config.connections[0]
    headers = OpenAIAdapter.discovery_headers(
        config, jwt_with_account(), {"User-Agent": "vbot-test"}, connection=connection
    )

    assert headers == {
        "User-Agent": "vbot-test",
        "chatgpt-account-id": ACCOUNT_ID,
        **CODEX_EXTRA_HEADERS,
    }
    with pytest.raises(ProviderAuthError):
        OpenAIAdapter.discovery_headers(config, "not-a-jwt", {}, connection=connection)


# ---------------------------------------------------------------------------
# Reasoning effort rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("levels", "effort", "expected"),
    [
        pytest.param(None, "xhigh", "xhigh", id="no-catalog-keeps-subscription-effort"),
        pytest.param((), "xhigh", "xhigh", id="catalog-without-ladder-uses-subscription-floor"),
        pytest.param(("low", "medium"), "xhigh", "medium", id="catalog-ladder-wins"),
    ],
)
@pytest.mark.asyncio
async def test_codex_effort_snaps_to_the_model_ladder_or_the_subscription_floor(
    levels, effort, expected
) -> None:
    model_lookup = ladder_model_lookup(levels) if levels is not None else None
    adapter = codex_adapter(model_lookup=model_lookup)

    payload = await codex_payload(adapter, thinking_effort=effort)

    assert payload["reasoning"] == {"effort": expected, "summary": "auto"}


@pytest.mark.parametrize("model_id", ["gpt-6-astra", "gpt-6.1-sol"])
@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        pytest.param(None, None, id="omitted-keeps-provider-default"),
        pytest.param("turbo", None, id="unknown-effort-dropped"),
        pytest.param("none", "low", id="none-raised-to-verified-minimum"),
        pytest.param("minimal", "low", id="minimal-snaps-to-ladder"),
        pytest.param("max", "max", id="catalog-level-above-subscription-floor"),
    ],
)
@pytest.mark.asyncio
async def test_codex_gpt6_uses_catalog_efforts_and_verified_minimum(
    model_id, effort, expected
) -> None:
    lookup = bundled_model_lookup()
    adapter = codex_adapter(model_lookup=lookup)

    try:
        payload = await codex_payload(
            adapter,
            model_id=model_id,
            thinking_effort=effort,
            max_output_tokens=1234,
            top_p=0.9,
            tools=CODEX_TOOLS,
        )
    finally:
        await adapter.aclose()

    assert payload.get("reasoning", {}).get("effort") == expected
    assert "context" not in payload.get("reasoning", {})
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["store"] is False
    assert "max_output_tokens" not in payload
    assert "top_p" not in payload
    assert all(tool["strict"] is False for tool in payload["tools"])
    assert adapter.reasoning_replay_policy(model_id) == "full_history"
    intent = adapter.describe_reasoning_render(model_id, effort)
    assert intent.effort_level == expected


@pytest.mark.parametrize(
    ("model_id", "make_adapter", "send_payload", "effort", "wire_effort"),
    [
        pytest.param(
            "gpt-6-sol",
            platform_adapter,
            platform_payload,
            "none",
            "none",
            id="platform-explicit-none",
        ),
        pytest.param(
            "gpt-6-sol", platform_adapter, platform_payload, "max", "max", id="platform-max"
        ),
        pytest.param(
            "gpt-6-sol", codex_adapter, codex_payload, "none", "low", id="codex-verified-minimum"
        ),
        pytest.param(
            "gpt-6.1-sol",
            platform_adapter,
            platform_payload,
            "none",
            "low",
            id="gpt61-platform-minimum",
        ),
        pytest.param(
            "gpt-6.1-sol",
            platform_adapter,
            platform_payload,
            "minimal",
            "low",
            id="gpt61-platform-minimal",
        ),
        pytest.param(
            "gpt-6.1-sol", platform_adapter, platform_payload, "max", "max", id="gpt61-platform-max"
        ),
    ],
)
@pytest.mark.asyncio
async def test_gpt6_sol_renders_catalog_effort_per_connection(
    model_id, make_adapter, send_payload, effort, wire_effort
) -> None:
    """Bundled GPT-6 profiles select Responses and the supported effort per Connection."""

    adapter = make_adapter(model_lookup=bundled_model_lookup())
    try:
        payload = await send_payload(
            adapter, model_id=model_id, thinking_effort=effort, tools=CODEX_TOOLS
        )
    finally:
        await adapter.aclose()

    assert payload["model"] == model_id
    assert payload["reasoning"] == {"effort": wire_effort, "summary": "auto"}
    assert payload["store"] is False
    assert all(tool["strict"] is False for tool in payload["tools"])


@pytest.mark.asyncio
async def test_codex_gpt_5_6_does_not_assume_the_public_reasoning_context() -> None:
    adapter = codex_adapter(model_lookup=gpt_reasoning_model)

    payload = await codex_payload(adapter, model_id="gpt-5.6-sol", thinking_effort="high")

    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
    assert adapter.reasoning_replay_policy("gpt-5.6-sol") == "full_history"
