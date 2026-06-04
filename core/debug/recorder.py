"""Provider debug recorder for vBot.

Captures a single provider HTTP request/response cycle with full wire
data, SSE streaming events, and error details.  All sensitive headers,
URL query parameters, and JSON keys are redacted before the trace is
persisted through ``DebugTraceStore``.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from core.debug.redaction import redact_headers, redact_json_body, redact_url
from core.debug.store import DebugTraceStore
from core.utils.logging import get_logger

_logger = get_logger("debug")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class DebugContext:
    """Immutable context passed from the chat loop before each provider request.

    Attributes:
        run_id: Identifier of the active run.
        agent_id: Identifier of the agent the run belongs to.
        session_id: Identifier of the session the run belongs to.
        provider_id: Identifier of the provider being called.
        connection_id: Identifier of the provider connection in use.
        model_id: Identifier of the model being invoked.
        streaming: Whether the request uses streaming (SSE) delivery.
        iteration_number: Which agentic-loop iteration this request
            corresponds to (1-based).
    """

    run_id: str
    agent_id: str
    session_id: str
    provider_id: str
    connection_id: str
    model_id: str
    streaming: bool
    iteration_number: int


# ---------------------------------------------------------------------------
# ProviderDebugRecorder
# ---------------------------------------------------------------------------


class ProviderDebugRecorder:
    """Records a single provider request/response cycle for debugging.

    One recorder instance represents one provider call.  The expected
    lifecycle::

        recorder = ProviderDebugRecorder(store)
        recorder.start_request(ctx)
        recorder.capture_request(method, url, headers, body)
        # ... then either:
        recorder.capture_response(status, headers, body, duration_ms)
        # ... or (streaming):
        recorder.capture_stream_event(raw, parsed)   # called repeatedly
        recorder.capture_error(err)                   # optional
        recorder.finish()

    Calling ``finish()`` writes the trace to the configured
    ``DebugTraceStore`` and triggers retention pruning.  If
    ``start_request()`` was never called, ``finish()`` is a no-op.

    Args:
        store: The ``DebugTraceStore`` instance used to persist traces.
    """

    def __init__(self, store: DebugTraceStore) -> None:
        self._store = store
        self._trace_id: str | None = None
        self._start_time: datetime | None = None
        self._context: DebugContext | None = None
        self._trace_data: dict[str, Any] = {}
        self._stream_events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_request(self, ctx: DebugContext) -> None:
        """Begin recording a provider request.

        Generates a unique ``trace_id``, records the start timestamp
        (ISO 8601 UTC), and populates the trace metadata from *ctx*.

        Args:
            ctx: The debug context for this request.
        """
        self._trace_id = uuid4().hex
        self._start_time = datetime.now(UTC)
        self._context = ctx
        self._trace_data = {
            "trace_id": self._trace_id,
            "timestamp": self._start_time.isoformat(),
            "run_id": ctx.run_id,
            "agent_id": ctx.agent_id,
            "session_id": ctx.session_id,
            "provider_id": ctx.provider_id,
            "connection_id": ctx.connection_id,
            "model_id": ctx.model_id,
            "streaming": ctx.streaming,
            "iteration_number": ctx.iteration_number,
        }
        self._stream_events = []
        _logger.info(
            "Debug trace started: %s (provider=%s model=%s streaming=%s)",
            self._trace_id,
            ctx.provider_id,
            ctx.model_id,
            ctx.streaming,
        )

    def capture_request(self, method: str, url: str, headers: dict[str, str], body: Any) -> None:
        """Capture the final provider request exactly after adapter
        mapping and defaults have been applied.

        Headers and URL are redacted before storage to remove secrets.
        The *body* is redacted recursively for sensitive JSON keys.

        Args:
            method: HTTP method (e.g. ``"POST"``).
            url: Full request URL.
            headers: Request headers as a string-to-string mapping.
            body: The request payload (typically a dict for JSON).
        """
        self._trace_data["request"] = {
            "method": method,
            "url": redact_url(url),
            "headers": redact_headers(dict(headers) if headers else {}),
            "body": redact_json_body(body),
        }

    def capture_response(
        self,
        status_code: int,
        headers: dict[str, str],
        body: Any,
        duration_ms: int,
    ) -> None:
        """Capture the provider response.

        Stores status code, redacted headers, raw body (text or parsed
        JSON), and the HTTP round-trip duration in milliseconds.

        Args:
            status_code: HTTP status code from the provider.
            headers: Response headers as a string-to-string mapping.
            body: Raw response body (text, bytes, or parsed JSON).
            duration_ms: HTTP round-trip time in milliseconds.
        """
        self._trace_data["response"] = {
            "status_code": status_code,
            "headers": redact_headers(dict(headers) if headers else {}),
            "body": body,
        }
        self._trace_data["duration_ms"] = duration_ms

    def capture_stream_event(self, raw: str, parsed: Any) -> None:
        """Capture a single raw SSE event frame plus its parsed JSON.

        For streaming mode only.  Each call appends an entry with both
        the raw SSE data line and the parsed event object.

        Args:
            raw: The raw SSE data line received from the provider.
            parsed: The parsed event JSON (``dict`` or ``None``).
        """
        self._stream_events.append(
            {
                "raw": raw,
                "parsed": parsed,
            }
        )

    def capture_error(self, error: Exception) -> None:
        """Capture error details from a failed provider request.

        Records the exception type name, message, and a formatted
        traceback suitable for serialization.

        Args:
            error: The exception raised during the provider call.
        """
        self._trace_data["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exception_only(error),
        }

    def finish(self) -> None:
        """Finalize the trace, write it to ``DebugTraceStore``, and
        trigger retention pruning.

        Computes wall-clock duration if not already provided by
        ``capture_response()``, attaches any captured stream events, and
        adds a ``normalized`` placeholder for adapter-derived analysis
        data.  Safe to call even when ``start_request()`` was never
        called (no-op).
        """
        if self._trace_id is None:
            return

        # Compute wall-clock duration if capture_response didn't set it.
        if "duration_ms" not in self._trace_data and self._start_time is not None:
            elapsed = datetime.now(UTC) - self._start_time
            self._trace_data["duration_ms"] = int(elapsed.total_seconds() * 1000)

        # Attach stream events (may be empty for non-streaming requests).
        if self._stream_events:
            self._trace_data["stream_events"] = self._stream_events

        # Placeholder for adapter-derived analysis data.
        self._trace_data.setdefault("normalized", {})

        # Lift fields the store index needs to the top level.
        request = self._trace_data.get("request", {})
        self._trace_data.setdefault("request_method", request.get("method", ""))
        self._trace_data.setdefault("request_url", request.get("url", ""))
        self._trace_data.setdefault(
            "status_code",
            (self._trace_data.get("response") or {}).get("status_code"),
        )
        self._trace_data.setdefault("duration_ms", None)

        _logger.info(
            "Debug trace finished: %s (status=%s duration_ms=%s events=%s)",
            self._trace_id,
            self._trace_data.get("status_code"),
            self._trace_data.get("duration_ms"),
            len(self._stream_events),
        )

        try:
            self._store.save_trace(self._trace_id, self._trace_data)
        except Exception:
            _logger.error(
                "Failed to persist debug trace %s",
                self._trace_id,
                exc_info=True,
            )

        # Prevent double-finish.
        self._trace_id = None
