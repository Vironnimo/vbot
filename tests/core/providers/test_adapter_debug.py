"""Tests for non-streaming provider wire capture via the debug transport.

When an adapter is built with a ``ProviderDebugRecorder``, its HTTP client
carries the shared capture transport (see ``core/providers/_http_shared.py``).
These tests drive ``OpenAICompatibleAdapter.send()`` and assert the persisted
trace follows the canonical shape in ``.vorch/domain-maps/debug.md``: raw request /
response bodies, nested ``context``, redacted headers, and metadata. They also
assert the debug context never leaks into the provider-bound payload.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from core.debug.recorder import DebugContext, ProviderDebugRecorder
from core.debug.store import DebugTraceStore
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

_TEST_TOKEN = "test-api-key-12345"
_REDACTED = "[REDACTED]"

OPENAI_CONFIG = ProviderConfig(
    id="openai",
    name="OpenAI",
    adapter="openai_compatible",
    base_url="https://api.openai.com/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="OPENAI_API_KEY",
            ),
        )
    ],
    extra_headers={"X-Custom-Header": "test-value"},
)

OPENAI_URL = "https://api.openai.com/v1/chat/completions"

SAMPLE_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello"},
]

SUCCESS_RESPONSE = {
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


@pytest.fixture
def debug_store(tmp_path: Path) -> DebugTraceStore:
    return DebugTraceStore(tmp_path, trace_limit=50)


@pytest.fixture
def debug_recorder(debug_store: DebugTraceStore) -> ProviderDebugRecorder:
    return ProviderDebugRecorder(debug_store)


@pytest.fixture
def debug_ctx() -> DebugContext:
    return DebugContext(
        run_id="run-debug-1",
        agent_id="agent-1",
        session_id="session-1",
        provider_id="openai",
        connection_id="openai:api-key",
        model_id="gpt-5.2",
        streaming=False,
        iteration_number=1,
    )


@pytest.fixture
def adapter_with_debug(debug_recorder: ProviderDebugRecorder) -> OpenAICompatibleAdapter:
    """Adapter built with a debug recorder, as ``Runtime.get_adapter()`` does."""
    return OpenAICompatibleAdapter(OPENAI_CONFIG, _TEST_TOKEN, debug_recorder=debug_recorder)


def _latest_trace(store: DebugTraceStore) -> dict:
    traces = store.get_traces()
    assert traces, "expected at least one trace in store"
    return store.get_trace(traces[0]["trace_id"])


@respx.mock
@pytest.mark.asyncio
async def test_request_method_url_headers_body_are_captured(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """Non-streaming send captures method, URL, headers, and raw body."""
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    assert route.called
    request = _latest_trace(debug_store)["request"]
    assert request["method"] == "POST"
    assert request["url"].startswith(OPENAI_URL)
    # Header keys are wire-normalized (lowercase) by httpx.
    assert request["headers"]["x-custom-header"] == "test-value"
    body = json.loads(request["body"])
    assert body["model"] == "gpt-5.2"
    assert body["messages"] == SAMPLE_MESSAGES


@respx.mock
@pytest.mark.asyncio
async def test_captured_request_body_matches_wire(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """The captured raw body is exactly what was sent over the wire."""
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    sent = route.calls.last.request.content.decode("utf-8")
    assert _latest_trace(debug_store)["request"]["body"] == sent


@respx.mock
@pytest.mark.asyncio
async def test_response_status_and_body_are_captured(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """Response status and raw body are captured for non-streaming requests."""
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    response = _latest_trace(debug_store)["response"]
    assert response["status_code"] == 200
    body = json.loads(response["body"])
    assert body["choices"][0]["message"]["content"] == "Hello!"
    assert isinstance(response["headers"], dict)


@respx.mock
@pytest.mark.asyncio
async def test_duration_is_recorded(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """``duration_ms`` is a non-negative integer after a successful request."""
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    duration_ms = _latest_trace(debug_store)["duration_ms"]
    assert isinstance(duration_ms, int)
    assert duration_ms >= 0


@respx.mock
@pytest.mark.asyncio
async def test_auth_request_header_is_redacted(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """The Authorization request header value is redacted in the trace."""
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    headers = _latest_trace(debug_store)["request"]["headers"]
    assert headers["authorization"] == _REDACTED
    assert headers["x-custom-header"] == "test-value"


@respx.mock
@pytest.mark.asyncio
async def test_sensitive_response_headers_are_redacted(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """Sensitive response header values are redacted in the trace."""
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            json=SUCCESS_RESPONSE,
            headers={
                "X-Request-Id": "req-001",
                "X-Debug-Secret": "do-not-leak",
                "X-Refresh-Token": "refresh-tkn-xxx",
                "X-Api-Key": "api-key-val",
            },
        )
    )

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    headers = _latest_trace(debug_store)["response"]["headers"]
    assert headers["x-debug-secret"] == _REDACTED
    assert headers["x-refresh-token"] == _REDACTED
    assert headers["x-api-key"] == _REDACTED
    assert headers["x-request-id"] == "req-001"


@respx.mock
@pytest.mark.asyncio
async def test_debug_context_not_in_provider_payload(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
) -> None:
    """The debug context never appears in the actual HTTP request body."""
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    body = json.loads(route.calls.last.request.content)
    for leaked in (
        "run_id",
        "agent_id",
        "session_id",
        "provider_id",
        "connection_id",
        "streaming",
        "iteration_number",
        "context",
    ):
        assert leaked not in body


@respx.mock
@pytest.mark.asyncio
async def test_trace_carries_context_and_identity(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """Provider/model sit at the top level; run context sits under ``context``."""
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    trace = _latest_trace(debug_store)
    assert trace["type"] == "provider_request"
    assert trace["provider_id"] == debug_ctx.provider_id
    assert trace["model_id"] == debug_ctx.model_id

    context = trace["context"]
    assert context["run_id"] == debug_ctx.run_id
    assert context["agent_id"] == debug_ctx.agent_id
    assert context["session_id"] == debug_ctx.session_id
    assert context["connection_id"] == debug_ctx.connection_id
    assert context["iteration_number"] == debug_ctx.iteration_number
    assert context["streaming"] == debug_ctx.streaming


@respx.mock
@pytest.mark.asyncio
async def test_adapter_without_recorder_does_not_capture(tmp_path: Path) -> None:
    """An adapter built without a recorder works normally and stores nothing."""
    store = DebugTraceStore(tmp_path, trace_limit=50)
    adapter = OpenAICompatibleAdapter(OPENAI_CONFIG, _TEST_TOKEN)

    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    # set_debug_context with no recorder is a no-op (must not crash).
    adapter.set_debug_context(
        DebugContext(
            run_id="r1",
            agent_id="a1",
            session_id="s1",
            provider_id="openai",
            connection_id="openai:api-key",
            model_id="gpt-5.2",
            streaming=False,
            iteration_number=1,
        )
    )
    result = await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    assert result["choices"][0]["message"]["content"] == "Hello!"
    assert store.get_traces() == []
