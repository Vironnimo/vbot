"""Openai: codex requests behavior."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import ProviderTimeoutError
from core.providers.openai import (
    CODEX_EXTRA_HEADERS,
    CODEX_RESPONSES_MODE,
    OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS,
    OpenAIAdapter,
)
from tests.core.providers.openai_helpers import (
    OPENAI_SUBSCRIPTION_URL,
    SAMPLE_MESSAGES,
    _codex_sse_response,
    _jwt_with_account,
    _subscription_config,
)


def _subscription_model_lookup(levels: tuple[str, ...]):
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


# Codex Responses mode (subscription connection)
@pytest.mark.asyncio
async def test_codex_headers_ignore_provider_extra_headers() -> None:
    """Provider extra_headers must not leak onto the Codex Responses wire."""
    access_token = _jwt_with_account("acct_openai")
    config = replace(_subscription_config(), extra_headers={"X-Injected": "leak"})
    adapter = OpenAIAdapter(config, access_token, connection_mode=CODEX_RESPONSES_MODE)

    headers = await adapter._build_codex_headers()

    assert "X-Injected" not in headers
    assert headers["OpenAI-Beta"] == CODEX_EXTRA_HEADERS["OpenAI-Beta"]
    assert headers["originator"] == CODEX_EXTRA_HEADERS["originator"]


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_posts_responses_payload_with_account_and_beta_headers() -> None:
    """Codex send() targets ``/codex/responses`` with the unified Codex headers."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response(
            {
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Hi"}],
                    }
                ],
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
    assert request.headers["chatgpt-account-id"] == "acct_openai"
    assert request.headers["OpenAI-Beta"] == CODEX_EXTRA_HEADERS["OpenAI-Beta"]
    assert request.headers["originator"] == CODEX_EXTRA_HEADERS["originator"]
    payload = json.loads(request.content)
    assert payload == {
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
        "reasoning_meta": {
            "response_id": "resp_1",
            "response_output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Hi"}],
                }
            ],
        },
        "tool_calls": None,
        "usage": {"input_tokens": 2, "output_tokens": 3},
        "terminal_outcome": "stop",
    }


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_collects_text_deltas_when_completed_output_is_empty() -> None:
    """The live Codex wire may leave completed.output empty after streaming text."""

    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
    )
    sse_body = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"Generated "}\n\n'
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"title"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"id":"resp_1",'
        '"status":"completed","output":[],"usage":{"input_tokens":2,'
        '"output_tokens":2}}}\n\n'
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    response = await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.5")

    assert route.call_count == 1
    assert adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Generated title",
        "reasoning": None,
        "reasoning_meta": {"response_id": "resp_1"},
        "tool_calls": None,
        "usage": {"input_tokens": 2, "output_tokens": 2},
        "terminal_outcome": "stop",
    }


@pytest.mark.asyncio
async def test_codex_send_times_out_when_its_internal_stream_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-streaming adapter semantics bound the internally streamed Codex wire."""

    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account(),
        connection_mode=CODEX_RESPONSES_MODE,
    )

    async def _stalled_stream(*args: Any, **kwargs: Any):
        del args, kwargs
        await asyncio.Future()
        yield {}

    monkeypatch.setattr(adapter, "_stream_responses", _stalled_stream)
    monkeypatch.setattr(
        "core.providers.openai.PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS",
        0.01,
    )

    with pytest.raises(ProviderTimeoutError):
        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.5")


def test_request_context_kwargs_separates_conversation_and_cache_affinity() -> None:
    """The hook keeps transport identity distinct from cache routing."""

    adapter = OpenAIAdapter(_subscription_config(), _jwt_with_account())

    context = adapter.request_context_kwargs(
        agent_id="orchestrator",
        session_id="sess-42",
        prompt_cache_affinity_id="shared-cache-lineage",
    )

    assert context == {
        "conversation_id": "orchestrator:sess-42",
        "prompt_cache_affinity_id": "shared-cache-lineage",
    }


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_stamps_cache_scope_headers() -> None:
    """The Codex request pins the prompt cache with per-conversation headers."""

    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
        codex_transport="sse",
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response(
            {
                "id": "resp_1",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )
    )

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5-codex",
        conversation_id="orchestrator:sess-42",
        prompt_cache_affinity_id="shared-cache-lineage",
    )

    request = route.calls.last.request
    assert request.headers["session_id"] == "shared-cache-lineage"
    assert request.headers["x-client-request-id"] == "shared-cache-lineage"
    # The routing hint rides on headers only — never on the request body.
    request_body = json.loads(request.content)
    assert "conversation_id" not in request_body
    assert "prompt_cache_affinity_id" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_omits_cache_scope_headers_without_conversation() -> None:
    """Absent a conversation id, no cache-scope headers are sent (no blank values)."""

    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response(
            {
                "id": "resp_1",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5-codex")

    request = route.calls.last.request
    assert "session_id" not in request.headers
    assert "x-client-request-id" not in request.headers


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_preserves_xhigh_reasoning_effort() -> None:
    """GPT-5.5 advertises xhigh reasoning through the Codex models endpoint."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.5", thinking_effort="xhigh")

    payload = json.loads(route.calls.last.request.content)
    assert payload["reasoning"] == {"effort": "xhigh", "summary": "auto"}


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_snaps_against_effective_model_ladder() -> None:
    """A subscription model with a feed ladder snaps within it, not the constant.

    A model whose effective ladder is ``[low, medium]`` snaps ``xhigh`` down to
    ``medium`` — the ``OPENAI_SUBSCRIPTION_REASONING_EFFORTS`` constant (which
    carries ``xhigh``) is bypassed in favor of the per-model ladder.
    """
    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
        model_lookup=_subscription_model_lookup(("low", "medium")),
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.5", thinking_effort="xhigh")

    payload = json.loads(route.calls.last.request.content)
    assert payload["reasoning"] == {"effort": "medium", "summary": "auto"}


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_falls_back_to_constant_without_feed_ladder() -> None:
    """A subscription reasoning model with no feed ladder uses the constant floor.

    ``xhigh`` is inside ``OPENAI_SUBSCRIPTION_REASONING_EFFORTS`` but outside a
    narrow ladder; with an empty ladder it survives as ``xhigh``, proving the
    constant floor is used when the looked-up model has no ladder.
    """
    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
        model_lookup=_subscription_model_lookup(()),
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.5", thinking_effort="xhigh")

    payload = json.loads(route.calls.last.request.content)
    assert payload["reasoning"] == {"effort": "xhigh", "summary": "auto"}


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_adds_default_instructions_without_system_message() -> None:
    """The Codex backend requires instructions even without a system prompt."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )

    await adapter.send([{"role": "user", "content": "Hello"}], model_id="gpt-5.5")

    payload = json.loads(route.calls.last.request.content)
    assert payload["instructions"] == OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS
    assert payload["store"] is False
    assert "max_output_tokens" not in payload


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_omits_unsupported_output_token_limits() -> None:
    """The Codex backend rejects Responses output-token limit parameters."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5.5",
        max_tokens=2048,
        max_output_tokens=1024,
    )

    payload = json.loads(route.calls.last.request.content)
    assert "max_output_tokens" not in payload


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_omits_unspecified_top_p() -> None:
    """An unspecified Chat-loop top_p must not become a Codex body field."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.6-luna", top_p=None)

    payload = json.loads(route.calls.last.request.content)
    assert "top_p" not in payload


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_skips_output_limit_clamp() -> None:
    """Codex must not abort a still-fitting request over a reserve it never sends."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
        model_lookup=_subscription_model_lookup(levels=("low", "medium", "high", "xhigh")),
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )
    huge_messages: list[dict[str, Any]] = [
        {"role": "system", "content": "Use concise answers."},
        {"role": "user", "content": "x" * 50_000},
        {
            "role": "assistant",
            "content": "ok",
            "reasoning_meta": {
                "response_output": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "encrypted_content": "opaque" * 20_000,
                    }
                ],
                "reasoning_items": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "encrypted_content": "opaque" * 20_000,
                    }
                ],
                "encrypted_content": ["opaque" * 20_000],
            },
        },
        {"role": "user", "content": "continue"},
    ]

    await adapter.send(huge_messages, model_id="gpt-5.6-luna")

    payload = json.loads(route.calls.last.request.content)
    assert "max_output_tokens" not in payload
    assert "max_tokens" not in payload


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_omits_unsupported_top_p() -> None:
    """The Codex backend rejects sampling top_p for subscription models."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.6-luna", top_p=0.9)

    payload = json.loads(route.calls.last.request.content)
    assert "top_p" not in payload
