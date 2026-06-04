"""Tests for adapter debug capture on non-streaming send().

Verifies that when a ``ProviderDebugRecorder`` is injected into an
adapter, the adapter captures request payload, URL, method, headers,
response body, status, headers, and duration.  Verifies that auth headers
are redacted in the stored trace and that the debug context does NOT
appear in the provider-bound payload.
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TEST_TOKEN = "test-api-key-12345"

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

_REDACTED = "[REDACTED]"


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
    """Adapter with debug recorder injected (as Runtime.get_adapter() would do)."""
    adapter = OpenAICompatibleAdapter(OPENAI_CONFIG, _TEST_TOKEN)
    adapter._debug_recorder = debug_recorder  # noqa: SLF001 - test injection
    return adapter


def _latest_trace(store: DebugTraceStore) -> dict:
    """Return the full trace dict of the most-recently saved trace."""
    traces = store.get_traces()
    assert traces, "expected at least one trace in store"
    return store.get_trace(traces[0]["trace_id"])


# ---------------------------------------------------------------------------
# Request payload, URL, method, headers are captured
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_request_payload_url_method_headers_are_captured(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """Non-streaming send captures method, URL, headers, and body in the trace."""
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    assert route.called
    trace = _latest_trace(debug_store)

    request = trace["request"]
    assert request["method"] == "POST"
    assert request["url"].startswith("https://api.openai.com/v1/chat/completions")
    # Extra headers from ProviderConfig are present (Content-Type is set by
    # httpx, not by _build_headers, so it won't appear in captured headers).
    assert request["headers"]["X-Custom-Header"] == "test-value"
    body = request["body"]
    assert body["model"] == "gpt-5.2"
    assert body["messages"] == SAMPLE_MESSAGES


@respx.mock
@pytest.mark.asyncio
async def test_request_captured_after_mapping_and_defaults(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """Captured request body is the final payload after defaults are applied."""
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    actual_sent = json.loads(route.calls.last.request.content)
    trace = _latest_trace(debug_store)

    # The captured body should match what was actually sent.
    assert trace["request"]["body"]["model"] == actual_sent["model"]
    assert trace["request"]["body"]["messages"] == actual_sent["messages"]


# ---------------------------------------------------------------------------
# Response body, status, headers are captured
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_response_body_and_status_are_captured(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """Response status code and body are captured."""
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    trace = _latest_trace(debug_store)
    response = trace["response"]
    assert response["status_code"] == 200
    assert response["body"]["choices"][0]["message"]["content"] == "Hello!"
    # Response headers dict is present (even if empty from mock).
    assert isinstance(response["headers"], dict)


# ---------------------------------------------------------------------------
# Duration is recorded
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_duration_is_recorded(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """The duration_ms field is a non-negative integer after a successful request."""
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    trace = _latest_trace(debug_store)
    assert isinstance(trace["duration_ms"], int)
    assert trace["duration_ms"] >= 0


# ---------------------------------------------------------------------------
# Auth headers are redacted in stored trace
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_auth_headers_are_redacted(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """Authorization header value is redacted in the stored trace."""
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    trace = _latest_trace(debug_store)
    request_headers = trace["request"]["headers"]

    # Authorization header value should be redacted.
    assert "Authorization" in request_headers
    assert request_headers["Authorization"] == _REDACTED
    # Non-sensitive extra_headers remain.
    assert request_headers["X-Custom-Header"] == "test-value"


def test_response_sensitive_headers_are_redacted(
    debug_recorder: ProviderDebugRecorder,
    debug_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """Sensitive response headers (with token/secret/key in name) are
    redacted when passed to capture_response()."""
    debug_recorder.start_request(debug_ctx)
    debug_recorder.capture_request("POST", "https://api.example.com/v1/chat", {}, {})
    debug_recorder.capture_response(
        200,
        {
            "X-Request-Id": "req-001",
            # "secret" is a sensitive word in the header name.
            "X-Debug-Secret": "do-not-leak",
            # "token" is a sensitive word in the header name.
            "X-Refresh-Token": "refresh-tkn-xxx",
            # "key" is a sensitive word in the header name.
            "X-Api-Key": "api-key-val",
        },
        {},
        100,
    )
    debug_recorder.finish()

    trace = _latest_trace(debug_store)
    response_headers = trace["response"]["headers"]

    # Sensitive headers are redacted.
    assert response_headers["X-Debug-Secret"] == _REDACTED
    assert response_headers["X-Refresh-Token"] == _REDACTED
    assert response_headers["X-Api-Key"] == _REDACTED
    # Non-sensitive headers remain.
    assert response_headers["X-Request-Id"] == "req-001"


# ---------------------------------------------------------------------------
# Debug context does NOT appear in provider-bound payload
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_debug_context_not_in_provider_payload(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
) -> None:
    """The debug context is never included in the actual HTTP request body."""
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    actual_body = json.loads(route.calls.last.request.content)
    # Debug context fields must NOT appear in the provider payload.
    assert "run_id" not in actual_body
    assert "agent_id" not in actual_body
    assert "session_id" not in actual_body
    assert "provider_id" not in actual_body
    assert "connection_id" not in actual_body
    assert "streaming" not in actual_body
    assert "iteration_number" not in actual_body
    assert "debug_context" not in actual_body


# ---------------------------------------------------------------------------
# Trace metadata includes debug context
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_trace_metadata_includes_debug_context(
    adapter_with_debug: OpenAICompatibleAdapter,
    debug_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """All fields from DebugContext appear at the top level of the trace."""
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    adapter_with_debug.set_debug_context(debug_ctx)
    await adapter_with_debug.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    trace = _latest_trace(debug_store)
    assert trace["run_id"] == debug_ctx.run_id
    assert trace["agent_id"] == debug_ctx.agent_id
    assert trace["session_id"] == debug_ctx.session_id
    assert trace["provider_id"] == debug_ctx.provider_id
    assert trace["connection_id"] == debug_ctx.connection_id
    assert trace["model_id"] == debug_ctx.model_id
    assert trace["streaming"] == debug_ctx.streaming
    assert trace["iteration_number"] == debug_ctx.iteration_number


# ---------------------------------------------------------------------------
# Adapter without debug recorder is a no-op
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_adapter_without_debug_recorder_does_not_capture(tmp_path: Path) -> None:
    """An adapter without a debug recorder set still works normally."""
    store = DebugTraceStore(tmp_path, trace_limit=50)
    adapter = OpenAICompatibleAdapter(OPENAI_CONFIG, _TEST_TOKEN)

    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    # set_debug_context with no recorder is a no-op (no crash).
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
    # No traces should have been persisted.
    assert store.get_traces() == []
