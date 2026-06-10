"""Shared HTTP error classification utilities for provider adapters.

Private module — not exported from ``core.providers``.
Provides common constants and functions used by both OpenAI-compatible
and Anthropic adapters for classifying HTTP errors and wrapping network
exceptions.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx

from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

# ---------------------------------------------------------------------------
# HTTP status constants
# ---------------------------------------------------------------------------

# Standard retryable HTTP status codes (common to all providers).
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 502, 503})

# Auth-related HTTP status codes — not retryable.
_AUTH_ERROR_STATUS_CODES: frozenset[int] = frozenset({401, 403})
_PROVIDER_HTTP_TIMEOUT_SECONDS = 60.0


def provider_chat_timeout() -> httpx.Timeout:
    """Return timeout settings for long-running provider generation requests."""
    return httpx.Timeout(
        connect=_PROVIDER_HTTP_TIMEOUT_SECONDS,
        read=None,
        write=_PROVIDER_HTTP_TIMEOUT_SECONDS,
        pool=_PROVIDER_HTTP_TIMEOUT_SECONDS,
    )


# ---------------------------------------------------------------------------
# Provider HTTP client factory + debug capture
# ---------------------------------------------------------------------------


def build_async_client(
    *,
    base_url: str,
    timeout: httpx.Timeout | None = None,
    debug_recorder: ProviderDebugRecorder | None = None,
) -> httpx.AsyncClient:
    """Build the provider HTTP client — the single place wire capture is wired.

    When *debug_recorder* is provided, the client's transport is wrapped so
    every request and response (including streamed bodies) is captured raw and
    persisted as a debug trace. With no recorder, a plain client is returned
    and there is zero capture overhead.

    This is the only sanctioned way for a provider adapter to construct its
    client; adapters must not build a bare ``httpx.AsyncClient`` directly, or
    their traffic will silently not be traced.
    """
    effective_timeout = timeout if timeout is not None else provider_chat_timeout()
    if debug_recorder is None:
        return httpx.AsyncClient(base_url=base_url, timeout=effective_timeout)

    transport = _DebugCaptureTransport(httpx.AsyncHTTPTransport(), debug_recorder)
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=effective_timeout,
        transport=transport,
    )


class _DebugCaptureTransport(httpx.AsyncBaseTransport):
    """httpx transport wrapper that records raw request/response traffic.

    Capture is best-effort: it must never change the bytes the adapter sees
    or surface its own errors to the caller.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        recorder: ProviderDebugRecorder,
    ) -> None:
        self._inner = inner
        self._recorder = recorder

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        capture = self._recorder.begin_capture(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            body=request.content,
        )
        try:
            response = await self._inner.handle_async_request(request)
        except Exception as exc:
            capture.record_error(exc)
            capture.finalize()
            raise

        capture.record_response_head(response.status_code, dict(response.headers))
        inner_stream = response.stream
        if isinstance(inner_stream, httpx.AsyncByteStream):
            response.stream = _CaptureByteStream(inner_stream, capture)
        else:
            # An async transport always yields an async stream; finalize
            # defensively so a trace is never left unpersisted.
            capture.finalize()
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class _CaptureByteStream(httpx.AsyncByteStream):
    """Tees response body chunks into a trace capture as they are read.

    Yields each chunk straight through without buffering ahead, so streaming
    latency and back-pressure are unchanged. The trace is finalized when the
    stream is closed (covers both full reads and streamed iteration).
    """

    def __init__(self, inner: httpx.AsyncByteStream, capture: Any) -> None:
        self._inner = inner
        self._capture = capture

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._capture.feed_body(chunk)
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._capture.finalize()


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def classify_http_status(
    status_code: int,
    *,
    extra_retryable: set[int] | None = None,
    detail: str = "",
) -> None:
    """Classify an HTTP status code and raise the appropriate provider error.

    If *status_code* indicates success (< 400) the function returns
    silently.  Otherwise it raises the correct subclass of
    ``ProviderError`` with the ``retryable`` flag set appropriately.

    Args:
        status_code: HTTP response status code.
        extra_retryable: Provider-specific status codes to treat as retryable
            in addition to the standard set (e.g. ``{529}`` for Anthropic's
            overloaded error).
        detail: Optional detail string for the error message. If empty,
            ``str(status_code)`` is used.

    Raises:
        ProviderAuthError: 401 / 403 (not retryable).
        ProviderRateLimitError: 429 (retryable).
        ProviderError: Other 4xx/5xx (retryable only for status codes in
            the retryable set).
    """
    if not detail:
        detail = str(status_code)

    if status_code in _AUTH_ERROR_STATUS_CODES:
        raise ProviderAuthError(f"Authentication error: {detail}")
    if status_code == 429:
        raise ProviderRateLimitError(f"Rate limited: {detail}")
    if status_code >= 400:
        retryable_codes = set(_RETRYABLE_STATUS_CODES)
        if extra_retryable:
            retryable_codes |= extra_retryable
        retryable = status_code in retryable_codes
        raise ProviderError(f"Provider error: {detail}", retryable=retryable)


# ---------------------------------------------------------------------------
# Network error wrapping
# ---------------------------------------------------------------------------


def wrap_network_error(error: Exception) -> NetworkError | ProviderTimeoutError:
    """Wrap an httpx network exception with the appropriate error type.

    Maps ``httpx.TimeoutException`` (and its subclasses) to
    ``ProviderTimeoutError`` (retryable). All other ``httpx.TransportError``
    subclasses — ``ConnectError``, ``ReadError``, ``WriteError``,
    ``RemoteProtocolError``, ``ProtocolError``, ``ProxyError``, ``UnsupportedProtocol``,
    ``LocalProtocolError``, ``NetworkError``, and any other transport-level
    failure — are wrapped as ``NetworkError`` (retryable and not
    provider-specific). ``NetworkError`` deliberately stays a non-``ProviderError``
    so it never triggers model fallback (see ``.vorch/specs/providers.md`` gotchas).
    """
    if isinstance(error, httpx.TimeoutException):
        return ProviderTimeoutError(f"Request failed: {error}")
    if isinstance(error, httpx.TransportError):
        return NetworkError(f"Connection failed: {error}")
    # Anything else (shouldn't happen at request-submission sites): surface as
    # a transport failure so retry semantics match.
    return NetworkError(f"Connection failed: {error}")


async def iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Yield complete Server-Sent Event data payloads from an HTTPX stream.

    SSE events may contain multiple ``data:`` lines. HTTPX yields individual
    lines, so adapters should consume framed payloads instead of parsing every
    line as a complete JSON document.
    """
    data_parts: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_parts:
                yield "\n".join(data_parts)
                data_parts = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_parts.append(line[len("data:") :].lstrip(" "))

    if data_parts:
        yield "\n".join(data_parts)


def parse_sse_json_data(data: str, *, context: str) -> Any:
    """Parse one SSE data payload and classify malformed JSON as provider error."""
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"{context} sent malformed JSON in stream: {exc.msg}",
            retryable=False,
        ) from exc
