"""Tests for streaming (SSE) provider wire capture via the debug transport.

Driving ``OpenAICompatibleAdapter.stream()`` with a recorder-backed client,
these assert the persisted trace follows the canonical shape in
``.vorch/specs/debug.md``: one complete raw aggregate SSE response body under
``response.body`` (including the ``data: [DONE]`` sentinel), the run context
records ``streaming: true``, and an error-status response keeps its raw body
under ``response.body``. No ``stream.events`` split is produced — the canonical
trace is one request and one response.

The full raw SSE text is captured verbatim off the wire, including the
``data: `` prefix and the ``\n\n`` frame separator. Errors raised by the adapter
*after* the stream connects (e.g. a missing ``[DONE]`` marker, or a retryable
error status) are surfaced to the caller; the partial trace is still persisted
from the bytes seen on the wire.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from core.debug.recorder import DebugContext, ProviderDebugRecorder
from core.debug.store import DebugTraceStore
from core.providers.errors import NetworkError, ProviderRateLimitError
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

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
)

OPENAI_URL = "https://api.openai.com/v1/chat/completions"

SAMPLE_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello"},
]

_RAW_JSON_1 = (
    '{"id":"chatcmpl-123","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}'
)
_RAW_JSON_2 = (
    '{"id":"chatcmpl-123","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}'
)
_RAW_JSON_3 = (
    '{"id":"chatcmpl-123","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}'
)
_RAW_JSON_FINAL = (
    '{"id":"chatcmpl-123","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}'
)

_SSE_CONTENT = (
    f"data: {_RAW_JSON_1}\n\n"
    f"data: {_RAW_JSON_2}\n\n"
    f"data: {_RAW_JSON_3}\n\n"
    f"data: {_RAW_JSON_FINAL}\n\n"
    "data: [DONE]\n\n"
)

_SSE_SHORT = (
    f"data: {_RAW_JSON_2}\n\ndata: {_RAW_JSON_3}\n\ndata: {_RAW_JSON_FINAL}\n\ndata: [DONE]\n\n"
)

_SSE_NO_DONE = f"data: {_RAW_JSON_2}\n\ndata: {_RAW_JSON_3}\n\n"


@pytest.fixture
def debug_store(tmp_path: Path) -> DebugTraceStore:
    return DebugTraceStore(tmp_path, trace_limit=50)


@pytest.fixture
def debug_recorder(debug_store: DebugTraceStore) -> ProviderDebugRecorder:
    return ProviderDebugRecorder(debug_store)


@pytest.fixture
def streaming_ctx() -> DebugContext:
    return DebugContext(
        run_id="run-stream-1",
        agent_id="agent-1",
        session_id="session-1",
        provider_id="openai",
        connection_id="openai:api-key",
        model_id="gpt-5.2",
        streaming=True,
        iteration_number=1,
    )


def _adapter_with_debug(recorder: ProviderDebugRecorder) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(OPENAI_CONFIG, _TEST_TOKEN, debug_recorder=recorder)


def _sse_response(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        content=content,
        headers={"Content-Type": "text/event-stream"},
    )


def _latest_trace(store: DebugTraceStore) -> dict:
    traces = store.get_traces()
    assert traces, "expected at least one trace in store"
    return store.get_trace(traces[0]["trace_id"])


async def _drain(adapter: OpenAICompatibleAdapter) -> list[dict]:
    return [delta async for delta in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2")]


@respx.mock
@pytest.mark.asyncio
async def test_streaming_response_body_captured_in_full(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """The full raw SSE text — every frame including ``[DONE]`` — is in ``response.body``."""
    respx.post(OPENAI_URL).mock(return_value=_sse_response(_SSE_CONTENT))

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)
    assert await _drain(adapter)

    trace = _latest_trace(debug_store)
    body = trace["response"]["body"]
    assert isinstance(body, str)
    # Every raw frame the provider sent, verbatim, is present in arrival order.
    assert body == _SSE_CONTENT
    # The canonical trace is one request and one response — no per-frame split.
    assert "stream" not in trace


@respx.mock
@pytest.mark.asyncio
async def test_captured_streaming_body_round_trips_to_provider_json(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """A captured content frame inside ``response.body`` parses to provider JSON."""
    respx.post(OPENAI_URL).mock(return_value=_sse_response(_SSE_SHORT))

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)
    await _drain(adapter)

    body = _latest_trace(debug_store)["response"]["body"]
    assert isinstance(body, str)
    hello_line = next(line for line in body.splitlines() if "Hello" in line)
    parsed = json.loads(hello_line.removeprefix("data: "))
    assert parsed["choices"][0]["delta"]["content"] == "Hello"


@respx.mock
@pytest.mark.asyncio
async def test_streaming_body_chunks_appear_in_arrival_order(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """Chunks appear in ``response.body`` in the order they were read from the wire."""
    respx.post(OPENAI_URL).mock(return_value=_sse_response(_SSE_SHORT))

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)
    await _drain(adapter)

    body = _latest_trace(debug_store)["response"]["body"]
    assert isinstance(body, str)
    assert body.index("Hello") < body.index(" world")


@respx.mock
@pytest.mark.asyncio
async def test_response_head_and_body_captured_for_streaming(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """A streaming success records status, headers, and the full raw body."""
    respx.post(OPENAI_URL).mock(return_value=_sse_response(_SSE_SHORT))

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)
    await _drain(adapter)

    response = _latest_trace(debug_store)["response"]
    assert response["status_code"] == 200
    assert isinstance(response["headers"], dict)
    # The aggregate raw SSE body, not a frame split, not None.
    assert response["body"] == _SSE_SHORT


@respx.mock
@pytest.mark.asyncio
async def test_capture_layer_produces_exactly_one_trace_for_provider_call(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """Capture happens at the shared HTTP transport, not adapter-side.

    Behavioral proof: an adapter built through ``build_async_client`` with a
    recorder, driving a single mocked provider call, persists exactly one
    provider-request trace carrying both ``request`` and ``response`` — and
    that response is the complete raw aggregate body, with no ``stream``
    key. Drift into adapter-side capture would either double-write a trace
    or fail to populate ``response.body``.
    """
    respx.post(OPENAI_URL).mock(return_value=_sse_response(_SSE_SHORT))

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)
    assert await _drain(adapter)

    traces = debug_store.get_traces()
    assert len(traces) == 1
    trace = debug_store.get_trace(traces[0]["trace_id"])
    assert trace["type"] == "provider_request"
    assert trace["request"]["method"] == "POST"
    assert trace["request"]["body"] is not None
    assert trace["response"]["status_code"] == 200
    assert trace["response"]["body"] == _SSE_SHORT
    assert "stream" not in trace


@respx.mock
@pytest.mark.asyncio
async def test_streaming_duration_is_recorded(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """Duration is recorded for streaming requests."""
    respx.post(OPENAI_URL).mock(return_value=_sse_response(_SSE_SHORT))

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)
    await _drain(adapter)

    duration_ms = _latest_trace(debug_store)["duration_ms"]
    assert isinstance(duration_ms, int)
    assert duration_ms >= 0


@respx.mock
@pytest.mark.asyncio
async def test_streaming_flag_recorded_in_context(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """The trace context records ``streaming: true``."""
    respx.post(OPENAI_URL).mock(return_value=_sse_response(_SSE_SHORT))

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)
    await _drain(adapter)

    assert _latest_trace(debug_store)["context"]["streaming"] is True


@respx.mock
@pytest.mark.asyncio
async def test_streaming_response_headers_are_redacted(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """Sensitive streaming response headers are redacted in the trace."""
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            content=_SSE_SHORT,
            headers={
                "Content-Type": "text/event-stream",
                "X-Request-Id": "stream-req-001",
                "X-Debug-Secret": "do-not-leak",
            },
        )
    )

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)
    await _drain(adapter)

    headers = _latest_trace(debug_store)["response"]["headers"]
    assert headers["x-debug-secret"] == "[REDACTED]"
    assert headers["x-request-id"] == "stream-req-001"


@respx.mock
@pytest.mark.asyncio
async def test_partial_streaming_response_persisted_when_stream_ends_without_done(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """A stream that ends without ``[DONE]`` still persists the bytes read."""
    respx.post(OPENAI_URL).mock(return_value=_sse_response(_SSE_NO_DONE))

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)

    with pytest.raises(NetworkError):
        await _drain(adapter)

    trace = _latest_trace(debug_store)
    # The partial bytes are persisted verbatim under response.body, not split.
    assert "stream" not in trace
    body = trace["response"]["body"]
    assert isinstance(body, str)
    assert _SSE_NO_DONE in body


@respx.mock
@pytest.mark.asyncio
async def test_error_status_stream_persists_response_body(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """An error status on a streaming request keeps its raw body in the trace."""
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            429,
            json={"error": {"message": "Rate limit exceeded"}},
        )
    )

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)

    with pytest.raises(ProviderRateLimitError):
        await _drain(adapter)

    trace = _latest_trace(debug_store)
    assert trace["request"]["method"] == "POST"
    assert trace["response"]["status_code"] == 429
    # Error responses keep their raw body even on a streaming request.
    assert "Rate limit exceeded" in trace["response"]["body"]
    assert "stream" not in trace
