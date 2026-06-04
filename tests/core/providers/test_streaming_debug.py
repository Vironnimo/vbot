"""Tests for adapter debug capture on streaming (SSE) requests.

Verifies SSE event capture (raw frames and parsed JSON), partial trace
persistence on stream errors/cancellation, stream events array
accumulation, and response headers captured at stream end.

Note: ``iter_sse_data()`` strips the ``data: `` prefix before
yielding, so raw values in trace stream events do NOT include the
``data: `` prefix.
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
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
)
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
)

OPENAI_URL = "https://api.openai.com/v1/chat/completions"

SAMPLE_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello"},
]

# SSE chunks simulating OpenAI streaming.
# aiter_lines() splits on \n, so each chunk ends with \n.
# iter_sse_data strips "data: " prefix before yielding.
_RAW_JSON_1 = '{"id":"chatcmpl-123","object":"chat.completion.chunk",'
_RAW_JSON_1 += (
    '"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}'
)

_RAW_JSON_2 = '{"id":"chatcmpl-123","object":"chat.completion.chunk",'
_RAW_JSON_2 += '"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}'

_RAW_JSON_3 = '{"id":"chatcmpl-123","object":"chat.completion.chunk",'
_RAW_JSON_3 += '"choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}'

_RAW_JSON_FINAL = '{"id":"chatcmpl-123","object":"chat.completion.chunk",'
_RAW_JSON_FINAL += '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
_RAW_JSON_FINAL += '"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}'

# Full SSE content for respx mock.
_SSE_CONTENT = (
    f"data: {_RAW_JSON_1}\n\n"
    f"data: {_RAW_JSON_2}\n\n"
    f"data: {_RAW_JSON_3}\n\n"
    f"data: {_RAW_JSON_FINAL}\n\n"
    "data: [DONE]\n\n"
)

# Shorter SSE content (2 content deltas + final + DONE).
_SSE_SHORT = (
    f"data: {_RAW_JSON_2}\n\ndata: {_RAW_JSON_3}\n\ndata: {_RAW_JSON_FINAL}\n\ndata: [DONE]\n\n"
)

# SSE content without [DONE] (for error tests).
_SSE_NO_DONE = f"data: {_RAW_JSON_2}\n\ndata: {_RAW_JSON_3}\n\n"

# Parsed event objects for comparison.
_PARSED_2 = json.loads(_RAW_JSON_2)
_PARSED_3 = json.loads(_RAW_JSON_3)
_PARSED_FINAL = json.loads(_RAW_JSON_FINAL)


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


def _adapter_with_debug(
    recorder: ProviderDebugRecorder,
) -> OpenAICompatibleAdapter:
    adapter = OpenAICompatibleAdapter(OPENAI_CONFIG, _TEST_TOKEN)
    adapter._debug_recorder = recorder  # noqa: SLF001
    return adapter


def _latest_trace(store: DebugTraceStore) -> dict:
    traces = store.get_traces()
    assert traces, "expected at least one trace in store"
    return store.get_trace(traces[0]["trace_id"])


# ---------------------------------------------------------------------------
# SSE event capture (raw frames and parsed JSON)
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_sse_events_are_captured(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """Each SSE data line is captured with its raw and parsed forms.

    The raw field contains data AFTER the ``data: `` prefix has been
    stripped by ``iter_sse_data()``.
    """
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            content=_SSE_CONTENT,
            headers={"Content-Type": "text/event-stream"},
        )
    )

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)

    deltas = [delta async for delta in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2")]
    assert len(deltas) > 0

    trace = _latest_trace(debug_store)
    events = trace["stream_events"]

    # Content deltas + final chunk + [DONE] = at least 4 events.
    assert len(events) >= 4

    # Each event has raw and parsed fields.
    for event in events:
        assert "raw" in event
        assert "parsed" in event

    # Check raw values (no "data: " prefix).
    raw_values = [e["raw"] for e in events]
    assert _RAW_JSON_1 in raw_values
    assert _RAW_JSON_2 in raw_values
    assert _RAW_JSON_3 in raw_values

    # [DONE] marker event.
    done_events = [e for e in events if e["raw"].strip() == "[DONE]"]
    assert len(done_events) == 1
    assert done_events[0]["parsed"] is None


@respx.mock
@pytest.mark.asyncio
async def test_sse_event_parsed_matches_received_json(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """Parsed JSON in captured events matches what was sent by the provider."""
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            content=_SSE_SHORT,
            headers={"Content-Type": "text/event-stream"},
        )
    )

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)

    async for _delta in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
        pass

    trace = _latest_trace(debug_store)
    events = trace["stream_events"]

    # First parsed event = content "Hello".
    content_events = [e for e in events if e["parsed"] is not None and "choices" in e["parsed"]]
    assert len(content_events) >= 1
    assert content_events[0]["parsed"]["choices"][0]["delta"]["content"] == "Hello"


# ---------------------------------------------------------------------------
# Stream events array accumulates correctly
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_stream_events_accumulate_in_order(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """Stream events are appended in the order they are received."""
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            content=_SSE_SHORT,
            headers={"Content-Type": "text/event-stream"},
        )
    )

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)

    async for _delta in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
        pass

    trace = _latest_trace(debug_store)
    events = trace["stream_events"]

    # Events with text = "Hello" should come before events with "world".
    raw_values = [e["raw"] for e in events]
    hello_index = next(i for i, r in enumerate(raw_values) if "Hello" in r)
    world_index = next(i for i, r in enumerate(raw_values) if "world" in r)
    assert hello_index < world_index


# ---------------------------------------------------------------------------
# Response headers captured at stream end
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_response_captured_at_stream_end(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """Response status and headers dict are captured at stream end.
    The body is None (captured as stream events instead)."""
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            content=_SSE_SHORT,
            headers={"Content-Type": "text/event-stream"},
        )
    )

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)

    async for _delta in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
        pass

    trace = _latest_trace(debug_store)
    response = trace["response"]
    assert response["status_code"] == 200
    assert isinstance(response["headers"], dict)
    # Streaming body is None (captured as stream events).
    assert response["body"] is None


@respx.mock
@pytest.mark.asyncio
async def test_streaming_duration_is_recorded(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """Duration is recorded even for streaming requests."""
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            content=_SSE_SHORT,
            headers={"Content-Type": "text/event-stream"},
        )
    )

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)

    async for _delta in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
        pass

    trace = _latest_trace(debug_store)
    assert isinstance(trace["duration_ms"], int)
    assert trace["duration_ms"] >= 0


# ---------------------------------------------------------------------------
# Partial trace persistence on stream error
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_partial_trace_persisted_on_stream_mid_read_error(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """When the stream ends without [DONE], a trace is persisted with
    captured events and error details."""
    # Stream that delivers chunks then ends without [DONE] marker.
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            content=_SSE_NO_DONE,
            headers={"Content-Type": "text/event-stream"},
        )
    )

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)

    with pytest.raises(NetworkError):
        async for _delta in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
            pass

    # Still persists a trace.
    trace = _latest_trace(debug_store)
    assert "stream_events" in trace
    assert len(trace["stream_events"]) >= 2
    assert "error" in trace


@respx.mock
@pytest.mark.asyncio
async def test_partial_trace_persisted_on_stream_error_status(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """When the stream connection fails with an error status, a trace
    is persisted with error details."""
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            429,
            json={"error": {"message": "Rate limit exceeded"}},
            headers={"Content-Type": "application/json"},
        )
    )

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)

    with pytest.raises(ProviderRateLimitError):
        async for _delta in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
            pass

    trace = _latest_trace(debug_store)
    assert "error" in trace
    assert trace["error"]["type"] == "ProviderRateLimitError"

    # Request should still be captured.
    assert "request" in trace
    assert trace["request"]["method"] == "POST"
    # No response entry for error-status streams.
    assert "response" not in trace


# ---------------------------------------------------------------------------
# Streaming metadata
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_streaming_flag_is_true_in_trace(
    debug_store: DebugTraceStore,
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
) -> None:
    """Trace marks streaming=True."""
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            content=_SSE_SHORT,
            headers={"Content-Type": "text/event-stream"},
        )
    )

    adapter = _adapter_with_debug(debug_recorder)
    adapter.set_debug_context(streaming_ctx)

    async for _delta in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2"):
        pass

    trace = _latest_trace(debug_store)
    assert trace["streaming"] is True


# ---------------------------------------------------------------------------
# Response headers redaction via recorder (streaming path)
# ---------------------------------------------------------------------------


def test_streaming_response_headers_are_redacted(
    debug_recorder: ProviderDebugRecorder,
    streaming_ctx: DebugContext,
    debug_store: DebugTraceStore,
) -> None:
    """Response headers with sensitive names are redacted when the
    recorder captures them (the streaming path also calls
    capture_response at stream end)."""
    debug_recorder.start_request(streaming_ctx)
    debug_recorder.capture_request("POST", "https://api.example.com/v1/chat", {}, {})
    debug_recorder.capture_stream_event(
        '{"choices":[{"delta":{"content":"hi"}}]}',
        {"choices": [{"delta": {"content": "hi"}}]},
    )
    debug_recorder.capture_response(
        200,
        {
            "X-Request-Id": "stream-req-001",
            "X-Debug-Secret": "do-not-leak",
        },
        None,
        150,
    )
    debug_recorder.finish()

    trace = _latest_trace(debug_store)
    response_headers = trace["response"]["headers"]

    assert response_headers["X-Debug-Secret"] == "[REDACTED]"
    assert response_headers["X-Request-Id"] == "stream-req-001"
    assert "stream_events" in trace
    assert len(trace["stream_events"]) == 1
