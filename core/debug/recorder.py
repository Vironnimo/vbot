"""Provider debug recorder for vBot.

A :class:`ProviderDebugRecorder` is attached to a Provider adapter. The
shared HTTP capture transport (see ``core/providers/_http_shared.py``)
calls :meth:`begin_capture` for HTTP requests; a stateful non-HTTP
transport may feed the same recorder directly. The capture builds one
canonical trace (see ``.vorch/domain-maps/debug.md``), applies structured
secret redaction, and persists it through :class:`DebugTraceStore`.

Adapters normally only set the per-request :class:`DebugContext` and build
their client through the shared factory. OpenAI Subscription WebSocket
streaming is the sanctioned direct-capture exception because its frames do
not pass through httpx.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import uuid4

from core.debug.redaction import redact_headers, redact_url
from core.debug.store import DebugTraceStore
from core.utils.logging import get_logger

_logger = get_logger("debug")

_TRACE_TYPE_PROVIDER_REQUEST = "provider_request"


@dataclass(frozen=True)
class DebugContext:
    """Per-request context set by the chat loop before each provider call.

    Stored separately from the provider payload — it must never enter
    ``**kwargs`` or any provider-bound request body.
    """

    run_id: str
    agent_id: str
    session_id: str
    provider_id: str
    connection_id: str
    model_id: str
    streaming: bool
    iteration_number: int


class ProviderDebugRecorder:
    """Holds the active :class:`DebugContext` and the trace store.

    Owns no per-request mutable state: each request gets its own
    :class:`_TraceCapture` so concurrent or retried requests never share
    buffers.

    Args:
        store: Destination for finalized traces.
    """

    def __init__(self, store: DebugTraceStore) -> None:
        self._store = store
        self._context: DebugContext | None = None

    def set_context(self, ctx: DebugContext) -> None:
        """Set the context used for the next captured request(s)."""
        self._context = ctx

    def begin_capture(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> _TraceCapture:
        """Start capturing one request/response cycle.

        Called by the capturing transport immediately before the request
        is handed to the underlying transport. Request headers and URL are
        redacted here; immutable body bytes are retained for decoding on the
        trace thread (prompts are never redacted).
        """
        return _TraceCapture(
            store=self._store,
            context=self._context,
            method=method,
            url=redact_url(url),
            headers=redact_headers(headers),
            request_body=body,
        )


class _TraceCapture:
    """Accumulates one trace and persists it on :meth:`finalize`.

    Body bytes (for both streaming and non-streaming responses) arrive
    through :meth:`feed_body`. On finalize, the complete raw aggregate
    body — every byte of a streamed SSE response, the raw JSON of a
    non-streaming response, or the raw text of an error response — is
    stored under ``response.body``. No per-frame split is produced; the
    canonical trace is one request and one response.
    """

    def __init__(
        self,
        *,
        store: DebugTraceStore,
        context: DebugContext | None,
        method: str,
        url: str,
        headers: dict[str, str],
        request_body: bytes | None,
    ) -> None:
        self._store = store
        self._context = context
        self._start = time.monotonic()
        self._trace_id = uuid4().hex
        self._request = {
            "method": method,
            "url": url,
            "headers": headers,
        }
        self._request_body = request_body
        self._response: dict[str, Any] | None = None
        self._error: dict[str, str] | None = None
        self._body_chunks: list[bytes] = []
        self._finalized = False

    def record_response_head(self, status_code: int, headers: dict[str, str]) -> None:
        """Record response status and (redacted) headers before the body."""
        self._response = {
            "status_code": status_code,
            "headers": redact_headers(headers),
            "body": None,
        }

    def feed_body(self, chunk: bytes) -> None:
        """Accumulate one raw response body chunk as it is read."""
        self._body_chunks.append(chunk)

    def record_error(self, error: BaseException) -> None:
        """Record a transport-level failure (connect/timeout/etc.)."""
        self._error = {"type": type(error).__name__, "message": str(error)}

    def finalize(self) -> None:
        """Hand the canonical trace to the store without waiting. Runs at most once.

        Duration, timestamp and received body are fixed here; building and
        writing the trace happen on the store's trace thread, so the Provider
        call never waits for the durable write. Best-effort: any failure, or
        a full trace backlog, is logged and swallowed.
        """
        if self._finalized:
            return
        self._finalized = True

        try:
            build_trace = partial(
                self._build_trace,
                duration_ms=int((time.monotonic() - self._start) * 1000),
                timestamp=datetime.now(UTC).isoformat(),
                response=self._response,
                error=self._error,
                body_chunks=tuple(self._body_chunks),
            )
            if not self._store.save_trace_in_background(self._trace_id, build_trace):
                _logger.warning("Debug trace dropped: earlier traces are still being written")
        except Exception:
            _logger.warning("Failed to persist debug trace", exc_info=True)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_trace(
        self,
        *,
        duration_ms: int,
        timestamp: str,
        response: dict[str, Any] | None,
        error: dict[str, str] | None,
        body_chunks: tuple[bytes, ...],
    ) -> dict[str, Any]:
        body_text = _decode_body(b"".join(body_chunks)) if body_chunks else None

        # The complete raw aggregate body — including every byte of an SSE
        # stream — lives in response.body. We never split it into per-frame
        # metadata; the canonical trace is one request and one response. When
        # no response head was recorded, the whole response stays None.
        trace_response: dict[str, Any] | None = None
        if response is not None:
            trace_response = {**response, "body": body_text}

        trace: dict[str, Any] = {
            "trace_id": self._trace_id,
            "type": _TRACE_TYPE_PROVIDER_REQUEST,
            "timestamp": timestamp,
            "duration_ms": duration_ms,
            "context": self._context_dict(),
            "provider_id": self._context.provider_id if self._context else "",
            "model_id": self._context.model_id if self._context else "",
            "request": {**self._request, "body": _decode_body(self._request_body)},
            "response": trace_response,
        }

        if error is not None:
            trace["error"] = error

        return trace

    def _context_dict(self) -> dict[str, Any] | None:
        if self._context is None:
            return None
        return {
            "run_id": self._context.run_id,
            "agent_id": self._context.agent_id,
            "session_id": self._context.session_id,
            "connection_id": self._context.connection_id,
            "iteration_number": self._context.iteration_number,
            "streaming": self._context.streaming,
        }


def _decode_body(body: bytes | None) -> str | None:
    """Decode raw wire bytes to text, replacing undecodable bytes."""
    if not body:
        return None
    return body.decode("utf-8", errors="replace")
