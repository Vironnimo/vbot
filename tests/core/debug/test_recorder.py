"""Tests for ProviderDebugRecorder capture, serialization, and lifecycle."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import pytest

from core.debug.recorder import DebugContext, ProviderDebugRecorder
from core.debug.store import DebugTraceStore

_REDACTED = "[REDACTED]"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> DebugTraceStore:
    return DebugTraceStore(tmp_path, trace_limit=50)


@pytest.fixture
def recorder(store: DebugTraceStore) -> ProviderDebugRecorder:
    return ProviderDebugRecorder(store)


def _make_context(**overrides) -> DebugContext:
    """Build a DebugContext with sensible defaults, overridable per-test."""
    defaults: dict = {
        "run_id": "run-1",
        "agent_id": "agent-1",
        "session_id": "session-1",
        "provider_id": "openai",
        "connection_id": "conn-1",
        "model_id": "gpt-4",
        "streaming": False,
        "iteration_number": 1,
    }
    defaults.update(overrides)
    return DebugContext(**defaults)


def _latest_trace(store: DebugTraceStore) -> dict:
    """Return the full trace dict of the most-recently saved trace."""
    traces = store.get_traces()
    assert traces, "expected at least one trace in store"
    return store.get_trace(traces[0]["trace_id"])


# ---------------------------------------------------------------------------
# Full request/response recording cycle
# ---------------------------------------------------------------------------


class TestFullCycle:
    def test_records_start_to_finish_non_streaming(self, recorder, store):
        """A non-streaming start→capture_request→capture_response→finish
        cycle produces a complete trace with all metadata and payloads."""
        ctx = _make_context()
        recorder.start_request(ctx)
        recorder.capture_request(
            method="POST",
            url="https://api.example.com/v1/chat",
            headers={"Content-Type": "application/json", "Authorization": "Bearer sk-123"},
            body={"model": "gpt-4", "messages": [{"role": "user", "content": "hello"}]},
        )
        recorder.capture_response(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body={"choices": [{"message": {"content": "hi"}}]},
            duration_ms=120,
        )
        recorder.finish()

        trace = _latest_trace(store)

        assert trace["trace_id"]
        assert "timestamp" in trace
        assert trace["run_id"] == "run-1"
        assert trace["agent_id"] == "agent-1"
        assert trace["session_id"] == "session-1"
        assert trace["provider_id"] == "openai"
        assert trace["connection_id"] == "conn-1"
        assert trace["model_id"] == "gpt-4"
        assert trace["streaming"] is False
        assert trace["iteration_number"] == 1
        assert trace["request"]["method"] == "POST"
        assert trace["response"]["status_code"] == 200
        assert trace["duration_ms"] == 120
        assert trace["normalized"] == {}
        assert trace["request_method"] == "POST"
        assert trace["request_url"].startswith("https://api.example.com")
        assert trace["status_code"] == 200


# ---------------------------------------------------------------------------
# Redaction through the recorder
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_headers_are_redacted_in_stored_request(self, recorder, store):
        """Authorization header is redacted in the persisted request."""
        ctx = _make_context()
        recorder.start_request(ctx)
        recorder.capture_request(
            method="POST",
            url="https://api.example.com/v1/chat",
            headers={"Content-Type": "application/json", "Authorization": "Bearer sk-abc"},
            body={"messages": [{"role": "user", "content": "hello"}]},
        )
        recorder.capture_response(200, {}, {}, 100)
        recorder.finish()

        trace = _latest_trace(store)
        assert trace["request"]["headers"]["Authorization"] == _REDACTED
        assert trace["request"]["headers"]["Content-Type"] == "application/json"

    def test_url_query_params_are_redacted(self, recorder, store):
        """URL query parameter values matching sensitive keys are redacted."""
        ctx = _make_context()
        recorder.start_request(ctx)
        recorder.capture_request(
            method="GET",
            url="https://api.example.com/v1/models?token=secret123&limit=10",
            headers={},
            body=None,
        )
        recorder.capture_response(200, {}, {}, 100)
        recorder.finish()

        trace = _latest_trace(store)
        stored_url = trace["request"]["url"]
        assert "secret123" not in stored_url
        assert "limit=10" in stored_url
        # urlencode percent-encodes [ and ]; verify the redacted placeholder
        # is present after decoding.
        assert _REDACTED in unquote(stored_url)

    def test_json_body_sensitive_keys_are_redacted(self, recorder, store):
        """JSON request body keys named 'api_key' are redacted."""
        ctx = _make_context()
        recorder.start_request(ctx)
        recorder.capture_request(
            method="POST",
            url="https://api.example.com/v1/chat",
            headers={},
            body={"model": "gpt-4", "api_key": "sk-secret", "messages": []},
        )
        recorder.capture_response(200, {}, {}, 100)
        recorder.finish()

        trace = _latest_trace(store)
        body = trace["request"]["body"]
        assert body["api_key"] == _REDACTED
        assert body["model"] == "gpt-4"


# ---------------------------------------------------------------------------
# Streaming event capture
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_captures_stream_events_and_persists_on_finish(self, recorder, store):
        """Each capture_stream_event() call appends to the event list;
        finish() writes them into the trace."""
        ctx = _make_context(streaming=True)
        recorder.start_request(ctx)
        recorder.capture_request("POST", "https://api.example.com/v1/chat", {}, {})
        recorder.capture_stream_event(
            'data: {"choices":[{"delta":{"content":"hi"}}]}',
            {"choices": [{"delta": {"content": "hi"}}]},
        )
        recorder.capture_stream_event(
            'data: {"choices":[{"delta":{"content":" there"}}]}',
            {"choices": [{"delta": {"content": " there"}}]},
        )
        recorder.finish()

        trace = _latest_trace(store)
        assert "stream_events" in trace
        events = trace["stream_events"]
        assert len(events) == 2
        assert events[0]["raw"] == 'data: {"choices":[{"delta":{"content":"hi"}}]}'
        assert events[0]["parsed"] == {"choices": [{"delta": {"content": "hi"}}]}
        assert events[1]["raw"] == 'data: {"choices":[{"delta":{"content":" there"}}]}'
        assert events[1]["parsed"] == {"choices": [{"delta": {"content": " there"}}]}

    def test_stream_events_omitted_for_non_streaming_requests(self, recorder, store):
        """When streaming is False and no events were captured,
        the trace does not contain a stream_events key."""
        ctx = _make_context(streaming=False)
        recorder.start_request(ctx)
        recorder.capture_request("POST", "https://api.example.com/v1/chat", {}, {})
        recorder.capture_response(200, {}, {}, 100)
        recorder.finish()

        trace = _latest_trace(store)
        assert "stream_events" not in trace


# ---------------------------------------------------------------------------
# Error capture
# ---------------------------------------------------------------------------


class TestErrorCapture:
    def test_captures_exception_type_message_and_traceback(self, recorder, store):
        """capture_error() records type name, message, and a formatted traceback."""
        ctx = _make_context()
        recorder.start_request(ctx)
        recorder.capture_request("POST", "https://api.example.com/v1/chat", {}, {})
        recorder.capture_error(ValueError("test error message"))
        recorder.finish()

        trace = _latest_trace(store)
        error = trace["error"]
        assert error["type"] == "ValueError"
        assert error["message"] == "test error message"
        assert isinstance(error["traceback"], list)
        assert len(error["traceback"]) > 0


# ---------------------------------------------------------------------------
# Duration recording
# ---------------------------------------------------------------------------


class TestDuration:
    def test_explicit_duration_is_recorded(self, recorder, store):
        """An explicit duration_ms passed to capture_response() is stored."""
        ctx = _make_context()
        recorder.start_request(ctx)
        recorder.capture_request("POST", "https://api.example.com/v1/chat", {}, {})
        recorder.capture_response(200, {}, {}, 250)
        recorder.finish()

        trace = _latest_trace(store)
        assert trace["duration_ms"] == 250

    def test_wall_clock_duration_computed_when_not_explicit(self, recorder, store):
        """When capture_response() is not called, finish() computes
        duration from the wall-clock elapsed time."""
        ctx = _make_context()
        recorder.start_request(ctx)
        recorder.capture_request("POST", "https://api.example.com/v1/chat", {}, {})

        recorder.finish()

        trace = _latest_trace(store)
        assert isinstance(trace["duration_ms"], int)
        assert trace["duration_ms"] >= 0


# ---------------------------------------------------------------------------
# DebugContext fields
# ---------------------------------------------------------------------------


class TestDebugContextFields:
    def test_all_context_fields_present_in_trace(self, recorder, store):
        """Every field from DebugContext appears at the top level of the trace."""
        ctx = DebugContext(
            run_id="my-run",
            agent_id="my-agent",
            session_id="my-session",
            provider_id="anthropic",
            connection_id="conn-xyz",
            model_id="claude-sonnet",
            streaming=True,
            iteration_number=5,
        )
        recorder.start_request(ctx)
        recorder.capture_request("POST", "https://api.example.com/v1/messages", {}, {})
        recorder.capture_response(200, {}, {}, 100)
        recorder.finish()

        trace = _latest_trace(store)
        assert trace["run_id"] == "my-run"
        assert trace["agent_id"] == "my-agent"
        assert trace["session_id"] == "my-session"
        assert trace["provider_id"] == "anthropic"
        assert trace["connection_id"] == "conn-xyz"
        assert trace["model_id"] == "claude-sonnet"
        assert trace["streaming"] is True
        assert trace["iteration_number"] == 5


# ---------------------------------------------------------------------------
# Lifecycle edge cases
# ---------------------------------------------------------------------------


class TestDoubleFinish:
    def test_second_finish_is_safe_no_double_write(self, recorder, store):
        """Calling finish() twice does not persist the trace twice."""
        ctx = _make_context()
        recorder.start_request(ctx)
        recorder.capture_request("POST", "https://api.example.com/v1/chat", {}, {})
        recorder.capture_response(200, {}, {}, 100)
        recorder.finish()
        recorder.finish()

        traces = store.get_traces()
        assert len(traces) == 1


class TestFinishWithoutStart:
    def test_finish_is_noop_when_start_request_not_called(self, recorder, store):
        """finish() does nothing when start_request() was never called."""
        recorder.finish()

        traces = store.get_traces()
        assert traces == []
