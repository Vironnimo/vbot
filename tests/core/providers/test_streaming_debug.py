"""Tests for streaming (SSE) provider wire capture via the debug transport.

Driving ``OpenAICompatibleAdapter.stream()`` with a recorder-backed client,
these assert the persisted trace follows the canonical shape in
``.vorch/specs/debug.md``: raw SSE frames under ``stream.events``, ``response.body``
is ``None`` for a streaming success, the run context records ``streaming: true``,
and an error-status response keeps its raw body under ``response.body``.

Raw frames are captured verbatim off the wire, so they include the ``data: ``
prefix. Errors raised by the adapter *after* the stream connects (e.g. a missing
``[DONE]`` marker, or a retryable error status) are surfaced to the caller; the
partial trace is still persisted from the bytes seen on the wire.
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
async def test_sse_frames_are_captured_raw(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """Each SSE frame is captured verbatim (including the ``data: `` prefix)."""
    respx.post(OPENAI_URL).mock(return_value=_sse_response(_SSE_CONTENT))

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)
    assert await _drain(adapter)

    events = _latest_trace(debug_store)["stream"]["events"]
    assert len(events) >= 4
    assert all(isinstance(frame, str) for frame in events)
    assert any(_RAW_JSON_1 in frame for frame in events)
    assert any(_RAW_JSON_2 in frame for frame in events)
    assert any(frame.strip() == "data: [DONE]" for frame in events)


@respx.mock
@pytest.mark.asyncio
async def test_captured_frame_is_valid_provider_json(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """A captured content frame round-trips to the JSON the provider sent."""
    respx.post(OPENAI_URL).mock(return_value=_sse_response(_SSE_SHORT))

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)
    await _drain(adapter)

    events = _latest_trace(debug_store)["stream"]["events"]
    hello_frame = next(frame for frame in events if "Hello" in frame)
    parsed = json.loads(hello_frame.removeprefix("data: "))
    assert parsed["choices"][0]["delta"]["content"] == "Hello"


@respx.mock
@pytest.mark.asyncio
async def test_frames_accumulate_in_order(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """Frames are stored in arrival order."""
    respx.post(OPENAI_URL).mock(return_value=_sse_response(_SSE_SHORT))

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)
    await _drain(adapter)

    events = _latest_trace(debug_store)["stream"]["events"]
    hello_index = next(i for i, frame in enumerate(events) if "Hello" in frame)
    world_index = next(i for i, frame in enumerate(events) if " world" in frame)
    assert hello_index < world_index


@respx.mock
@pytest.mark.asyncio
async def test_response_head_captured_body_none_for_streaming(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """A streaming success records status + headers; the body lives in frames."""
    respx.post(OPENAI_URL).mock(return_value=_sse_response(_SSE_SHORT))

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)
    await _drain(adapter)

    response = _latest_trace(debug_store)["response"]
    assert response["status_code"] == 200
    assert isinstance(response["headers"], dict)
    assert response["body"] is None


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
async def test_partial_trace_persisted_when_stream_ends_without_done(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """A stream that ends without ``[DONE]`` still persists the frames read."""
    respx.post(OPENAI_URL).mock(return_value=_sse_response(_SSE_NO_DONE))

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)

    with pytest.raises(NetworkError):
        await _drain(adapter)

    trace = _latest_trace(debug_store)
    assert "stream" in trace
    assert len(trace["stream"]["events"]) >= 2


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
