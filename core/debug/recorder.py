"""Provider debug recorder for vBot.

A single :class:`ProviderDebugRecorder` is attached to a provider's
debug-aware HTTP client. The capturing transport (see
``core/providers/_http_shared.py``) calls :meth:`begin_capture` for every
request that flows over the wire and feeds raw request/response data into
the returned :class:`_TraceCapture`. The capture builds one canonical
trace (see ``.vorch/specs/debug.md``), applies structured secret
redaction, and persists it through :class:`DebugTraceStore`.

Adapters contain no capture logic — they only set the per-request
:class:`DebugContext` via :meth:`set_context` and build their client
through the shared factory.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from core.debug.redaction import redact_headers, redact_url
from core.debug.store import DebugTraceStore
from core.utils.logging import get_logger

_logger = get_logger("debug")

_TRACE_TYPE_PROVIDER_REQUEST = "provider_request"
_HTTP_ERROR_THRESHOLD = 400


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
        redacted here; the body is stored raw (prompts are never redacted).
        """
        return _TraceCapture(
            store=self._store,
            context=self._context,
            method=method,
            url=redact_url(url),
            headers=redact_headers(headers),
            request_body=_decode_body(body),
        )


class _TraceCapture:
    """Accumulates one trace and persists it on :meth:`finalize`.

    Body bytes (for both streaming and non-streaming responses) arrive
    through :meth:`feed_body`. On finalize, streaming responses are split
    into raw SSE frames under ``stream.events`` while non-streaming
    responses keep their raw text under ``response.body``.
    """

    def __init__(
        self,
        *,
        store: DebugTraceStore,
        context: DebugContext | None,
        method: str,
        url: str,
        headers: dict[str, str],
        request_body: str | None,
    ) -> None:
        self._store = store
        self._context = context
        self._start = time.monotonic()
        self._trace_id = uuid4().hex
        self._request = {
            "method": method,
            "url": url,
            "headers": headers,
            "body": request_body,
        }
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
        """Build the canonical trace and persist it. Runs at most once.

        Best-effort: any failure is logged and swallowed so the provider
        call is never affected.
        """
        if self._finalized:
            return
        self._finalized = True

        try:
            trace = self._build_trace()
            self._store.save_trace(trace["trace_id"], trace)
        except Exception:
            _logger.warning("Failed to persist debug trace", exc_info=True)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_trace(self) -> dict[str, Any]:
        duration_ms = int((time.monotonic() - self._start) * 1000)
        body_text = _decode_body(b"".join(self._body_chunks)) if self._body_chunks else None

        trace: dict[str, Any] = {
            "trace_id": self._trace_id,
            "type": _TRACE_TYPE_PROVIDER_REQUEST,
            "timestamp": datetime.now(UTC).isoformat(),
            "duration_ms": duration_ms,
            "context": self._context_dict(),
            "provider_id": self._context.provider_id if self._context else "",
            "model_id": self._context.model_id if self._context else "",
            "request": self._request,
            "response": self._response,
        }

        # A streaming success body is a sequence of SSE frames; anything else
        # (non-streaming, or an error response on a streaming request) keeps
        # its raw body under response.body.
        if self._is_streaming_body():
            trace["stream"] = {"events": _split_sse_frames(body_text)}
        elif self._response is not None:
            self._response["body"] = body_text

        if self._error is not None:
            trace["error"] = self._error

        return trace

    def _is_streaming_body(self) -> bool:
        if self._context is None or not self._context.streaming:
            return False
        if self._response is None:
            return False
        status_code = self._response.get("status_code")
        return isinstance(status_code, int) and status_code < _HTTP_ERROR_THRESHOLD

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


def _split_sse_frames(body_text: str | None) -> list[str]:
    """Split a raw SSE response body into individual event frames.

    Events are separated by a blank line. Frames are kept raw (including
    ``data:`` / ``event:`` prefixes); no JSON parsing is performed.
    """
    if not body_text:
        return []
    normalized = body_text.replace("\r\n", "\n")
    return [frame for frame in normalized.split("\n\n") if frame.strip()]
