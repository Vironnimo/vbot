"""Tests for ProviderDebugRecorder / _TraceCapture: shape, redaction, lifecycle.

These drive the capture object directly (as the HTTP transport does) without a
real network, asserting the canonical trace shape in ``.vorch/domain-maps/debug.md``:
one complete raw request and one complete raw response per provider call, with
the full aggregate body (streaming or not) under ``response.body`` and no
``stream.events`` split.
"""

from __future__ import annotations

import threading
from pathlib import Path
from urllib.parse import unquote

import pytest

from core.debug import store as store_module
from core.debug.recorder import DebugContext, ProviderDebugRecorder
from core.debug.store import DebugTraceStore

_REDACTED = "[REDACTED]"


@pytest.fixture
def store(tmp_path: Path) -> DebugTraceStore:
    return DebugTraceStore(tmp_path, trace_limit=50)


@pytest.fixture
def recorder(store: DebugTraceStore) -> ProviderDebugRecorder:
    return ProviderDebugRecorder(store)


def _make_context(**overrides) -> DebugContext:
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
    traces = store.get_traces()
    assert traces, "expected at least one trace in store"
    return store.get_trace(traces[0]["trace_id"])


class TestFullCycle:
    def test_non_streaming_cycle_produces_complete_trace(self, recorder, store):
        recorder.set_context(_make_context())
        capture = recorder.begin_capture(
            method="POST",
            url="https://api.example.com/v1/chat",
            headers={"Content-Type": "application/json", "Authorization": "Bearer sk-123"},
            body=b'{"model":"gpt-4"}',
        )
        capture.record_response_head(200, {"Content-Type": "application/json"})
        capture.feed_body(b'{"choices":[{"message":{"content":"hi"}}]}')
        capture.finalize()

        trace = _latest_trace(store)
        assert trace["trace_id"]
        assert trace["type"] == "provider_request"
        assert "timestamp" in trace
        assert trace["provider_id"] == "openai"
        assert trace["model_id"] == "gpt-4"
        assert trace["context"]["run_id"] == "run-1"
        assert trace["context"]["connection_id"] == "conn-1"
        assert trace["context"]["iteration_number"] == 1
        assert trace["context"]["streaming"] is False
        assert trace["request"]["method"] == "POST"
        assert trace["request"]["body"] == '{"model":"gpt-4"}'
        assert trace["response"]["status_code"] == 200
        assert trace["response"]["body"] == '{"choices":[{"message":{"content":"hi"}}]}'
        assert isinstance(trace["duration_ms"], int)


class TestRedaction:
    def test_request_auth_header_is_redacted(self, recorder, store):
        recorder.set_context(_make_context())
        capture = recorder.begin_capture(
            method="POST",
            url="https://api.example.com/v1/chat",
            headers={"Content-Type": "application/json", "Authorization": "Bearer sk-abc"},
            body=None,
        )
        capture.record_response_head(200, {})
        capture.finalize()

        headers = _latest_trace(store)["request"]["headers"]
        assert headers["Authorization"] == _REDACTED
        assert headers["Content-Type"] == "application/json"

    def test_url_query_params_are_redacted(self, recorder, store):
        recorder.set_context(_make_context())
        capture = recorder.begin_capture(
            method="GET",
            url="https://api.example.com/v1/models?token=secret123&limit=10",
            headers={},
            body=None,
        )
        capture.record_response_head(200, {})
        capture.finalize()

        stored_url = _latest_trace(store)["request"]["url"]
        assert "secret123" not in stored_url
        assert "limit=10" in stored_url
        assert _REDACTED in unquote(stored_url)

    def test_response_headers_are_redacted(self, recorder, store):
        recorder.set_context(_make_context())
        capture = recorder.begin_capture(
            method="POST", url="https://api.example.com/v1/chat", headers={}, body=None
        )
        capture.record_response_head(200, {"X-Request-Id": "req-1", "X-Refresh-Token": "leak"})
        capture.finalize()

        headers = _latest_trace(store)["response"]["headers"]
        assert headers["X-Refresh-Token"] == _REDACTED
        assert headers["X-Request-Id"] == "req-1"

    def test_identifying_request_and_response_headers_are_redacted(self, recorder, store):
        """Provider Account ids, organization ids, and cookies never persist verbatim."""
        recorder.set_context(_make_context())
        capture = recorder.begin_capture(
            method="POST",
            url="https://chatgpt.com/backend-api/codex/responses",
            headers={"chatgpt-account-id": "acct-123", "cookie": "session=abc"},
            body=None,
        )
        capture.record_response_head(
            200,
            {"openai-organization": "org-123", "set-cookie": "session=def", "x-request-id": "r1"},
        )
        capture.finalize()

        trace = _latest_trace(store)
        assert trace["request"]["headers"] == {
            "chatgpt-account-id": _REDACTED,
            "cookie": _REDACTED,
        }
        assert trace["response"]["headers"] == {
            "openai-organization": _REDACTED,
            "set-cookie": _REDACTED,
            "x-request-id": "r1",
        }

    def test_request_body_is_stored_raw_not_redacted(self, recorder, store):
        """Bodies are stored verbatim — prompt/payload content is never redacted."""
        recorder.set_context(_make_context())
        capture = recorder.begin_capture(
            method="POST",
            url="https://api.example.com/v1/chat",
            headers={},
            body=b'{"api_key":"sk-secret","model":"gpt-4"}',
        )
        capture.record_response_head(200, {})
        capture.finalize()

        assert _latest_trace(store)["request"]["body"] == (
            '{"api_key":"sk-secret","model":"gpt-4"}'
        )


class TestStreaming:
    def test_streaming_body_stored_as_raw_aggregate_in_response_body(self, recorder, store):
        """A streaming success keeps the full raw SSE text in ``response.body``."""
        recorder.set_context(_make_context(streaming=True))
        capture = recorder.begin_capture(
            method="POST", url="https://api.example.com/v1/chat", headers={}, body=None
        )
        capture.record_response_head(200, {})
        capture.feed_body(b'data: {"delta":"hi"}\n\n')
        capture.feed_body(b"data: [DONE]\n\n")
        capture.finalize()

        trace = _latest_trace(store)
        assert trace["response"]["body"] == 'data: {"delta":"hi"}\n\ndata: [DONE]\n\n'
        # The canonical trace is one request and one response — no per-frame
        # split is produced for streaming success.
        assert "stream" not in trace

    def test_non_streaming_body_stored_in_response_body(self, recorder, store):
        """A non-streaming response keeps its raw body in ``response.body``."""
        recorder.set_context(_make_context(streaming=False))
        capture = recorder.begin_capture(
            method="POST", url="https://api.example.com/v1/chat", headers={}, body=None
        )
        capture.record_response_head(200, {})
        capture.feed_body(b'{"ok":true}')
        capture.finalize()

        trace = _latest_trace(store)
        assert trace["response"]["body"] == '{"ok":true}'
        assert "stream" not in trace

    def test_streaming_error_status_keeps_raw_error_body(self, recorder, store):
        """A streaming request that returns an error status keeps its raw body."""
        recorder.set_context(_make_context(streaming=True))
        capture = recorder.begin_capture(
            method="POST", url="https://api.example.com/v1/chat", headers={}, body=None
        )
        capture.record_response_head(429, {})
        capture.feed_body(b'{"error":{"message":"Rate limit exceeded"}}')
        capture.finalize()

        trace = _latest_trace(store)
        assert trace["response"]["status_code"] == 429
        assert trace["response"]["body"] == '{"error":{"message":"Rate limit exceeded"}}'
        assert "stream" not in trace


class TestErrorCapture:
    def test_records_error_type_and_message(self, recorder, store):
        recorder.set_context(_make_context())
        capture = recorder.begin_capture(
            method="POST", url="https://api.example.com/v1/chat", headers={}, body=None
        )
        capture.record_error(ValueError("test error message"))
        capture.finalize()

        error = _latest_trace(store)["error"]
        assert error["type"] == "ValueError"
        assert error["message"] == "test error message"


class TestLifecycle:
    def test_double_finalize_writes_once(self, recorder, store):
        recorder.set_context(_make_context())
        capture = recorder.begin_capture(
            method="POST", url="https://api.example.com/v1/chat", headers={}, body=None
        )
        capture.record_response_head(200, {})
        capture.finalize()
        capture.finalize()

        assert len(store.get_traces()) == 1

    def test_no_capture_persists_nothing(self, recorder, store):
        recorder.set_context(_make_context())
        assert store.get_traces() == []

    def test_finalize_returns_before_the_durable_write(self, recorder, store, monkeypatch):
        """The Provider call hands the capture off; it never waits for the durable write."""
        entered = threading.Event()
        release = threading.Event()
        write = store_module.atomic_write_text

        def slow_write(path, text):
            entered.set()
            release.wait(timeout=5)
            write(path, text)

        monkeypatch.setattr(store_module, "atomic_write_text", slow_write)
        recorder.set_context(_make_context())
        capture = recorder.begin_capture(
            method="POST", url="https://api.example.com/v1/chat", headers={}, body=b"{}"
        )
        capture.record_response_head(200, {})
        capture.feed_body(b"first")
        try:
            capture.finalize()
            assert entered.wait(timeout=5)
            assert not release.is_set()
            # Bytes arriving after finalize never change the handed-off trace.
            capture.feed_body(b"late")
        finally:
            release.set()

        trace = _latest_trace(store)
        assert trace["response"]["body"] == "first"

    def test_full_backlog_drops_the_trace_with_a_warning(
        self, recorder, store, monkeypatch, caplog
    ):
        monkeypatch.setattr(store, "save_trace_in_background", lambda *_args: False)
        caplog.set_level("WARNING", logger="vbot.debug")
        recorder.set_context(_make_context())
        capture = recorder.begin_capture(
            method="POST", url="https://api.example.com/v1/chat", headers={}, body=None
        )

        capture.finalize()

        assert store.get_traces() == []
        assert [record.name for record in caplog.records] == ["vbot.debug"]

    def test_capture_uses_context_at_begin_time(self, recorder, store):
        """A trace reflects whichever context was active when capture began."""
        recorder.set_context(_make_context(provider_id="anthropic", model_id="claude"))
        capture = recorder.begin_capture(
            method="POST", url="https://api.anthropic.com/v1/messages", headers={}, body=None
        )
        # A later context change must not retroactively alter the in-flight trace.
        recorder.set_context(_make_context(provider_id="openai", model_id="gpt-4"))
        capture.record_response_head(200, {})
        capture.finalize()

        trace = _latest_trace(store)
        assert trace["provider_id"] == "anthropic"
        assert trace["model_id"] == "claude"
