"""Shared HTTP error classification utilities for provider adapters.

Private module — not exported from ``core.providers``.
Provides common constants and functions used by both OpenAI-compatible
and Anthropic adapters for classifying HTTP errors and wrapping network
exceptions.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from logging import Logger
from typing import TYPE_CHECKING, Any, TypeVar

import httpx

from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.utils.http_status import is_retryable_status, parse_retry_after

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

_T = TypeVar("_T")

# ---------------------------------------------------------------------------
# HTTP status constants
# ---------------------------------------------------------------------------

# Auth-related HTTP status codes — not retryable.
_AUTH_ERROR_STATUS_CODES: frozenset[int] = frozenset({401, 403})
_PROVIDER_HTTP_TIMEOUT_SECONDS = 60.0
PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class SSEEvent:
    """One framed SSE data payload or transport comment."""

    data: str | None = None
    comment: str | None = None


def provider_chat_timeout() -> httpx.Timeout:
    """Return the bounded default for non-streaming Provider requests."""
    return httpx.Timeout(
        connect=_PROVIDER_HTTP_TIMEOUT_SECONDS,
        read=PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS,
        write=_PROVIDER_HTTP_TIMEOUT_SECONDS,
        pool=_PROVIDER_HTTP_TIMEOUT_SECONDS,
    )


def provider_streaming_timeout() -> httpx.Timeout:
    """Return the Provider timeout with stream reads owned by Chat clocks."""
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


def build_streaming_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Request:
    """Build a request whose read stalls are governed by Chat streaming clocks."""
    return client.build_request(
        method,
        url,
        timeout=provider_streaming_timeout(),
        **kwargs,
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


# Retry-After parsing lives in core.utils.http_status (shared with the HTTP
# tools); ``parse_retry_after`` is re-imported above for the callers here.


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def classify_http_status(
    status_code: int,
    *,
    idempotent: bool,
    extra_retryable: set[int] | None = None,
    detail: str = "",
    response_headers: httpx.Headers | None = None,
) -> None:
    """Classify an HTTP status code and raise the appropriate provider error.

    If *status_code* indicates success (< 400) the function returns
    silently.  Otherwise it raises the correct subclass of
    ``ProviderError`` with the ``retryable`` flag set appropriately.

    Args:
        status_code: HTTP response status code.
        idempotent: Whether the request is safe to repeat. Idempotent requests
            additionally retry HTTP 500 under the shared status policy.
        extra_retryable: Provider-specific status codes to treat as retryable
            in addition to the shared set (e.g. ``{529}`` for Anthropic's
            overloaded error). See ``core.utils.http_status`` for the policy.
        detail: Optional detail string for the error message. If empty,
            ``str(status_code)`` is used.
        response_headers: The response headers, when available. Used to parse a
            ``Retry-After`` hint that is attached to retryable errors so
            ``retry_async`` can honor the provider's requested wait.

    Raises:
        ProviderAuthError: 401 / 403 (not retryable).
        ProviderRateLimitError: 429 (retryable).
        ProviderError: Other 4xx/5xx. Retryability follows the shared
            idempotency-aware ``is_retryable_status`` policy plus any
            ``extra_retryable`` codes.
    """
    if not detail:
        detail = str(status_code)

    if status_code in _AUTH_ERROR_STATUS_CODES:
        raise ProviderAuthError(f"Authentication error: {detail}")

    retry_after = parse_retry_after(response_headers) if response_headers is not None else None

    if status_code == 429:
        rate_limit_error = ProviderRateLimitError(f"Rate limited: {detail}")
        rate_limit_error.retry_after = retry_after
        raise rate_limit_error
    if status_code >= 400:
        retryable = is_retryable_status(
            status_code,
            idempotent=idempotent,
            extra=extra_retryable,
        )
        provider_error = ProviderError(f"Provider error: {detail}", retryable=retryable)
        if retryable:
            provider_error.retry_after = retry_after
        raise provider_error


# ---------------------------------------------------------------------------
# Sampling-parameter rejection fallback
# ---------------------------------------------------------------------------

# Sampling parameters Chat may put on the wire. Some backends reject them for
# specific models (thinking-only models, fixed-contract gateways) while the
# same backend accepts them for every other model, so they cannot be filtered
# proactively in every case.
SAMPLING_PARAMETER_NAMES: tuple[str, ...] = ("temperature", "top_p", "top_k")

# A rejection detail must name one of these markers AND the parameter name;
# both conditions together keep the detection conservative (mirrors
# ``detail_names_rejected_effort`` in ``core.providers.reasoning``).
_UNSUPPORTED_PARAMETER_DETAIL_MARKERS: tuple[str, ...] = (
    "unsupported parameter",
    "unsupported_parameter",
    "not supported",
    "does not support",
    "unknown parameter",
    "unrecognized request argument",
    "unrecognized parameter",
    "invalid parameter",
)


def unsupported_sampling_parameter(detail: str) -> str | None:
    """Return the sampling parameter name a rejection detail blames, if any.

    Matches provider wordings such as ``Unsupported parameter: 'temperature'``,
    ``temperature is not supported when thinking is enabled``, or ``Unknown
    parameter: top_k``. Detection never changes status classification; it only
    gates the one-shot strip-and-retry in :func:`execute_with_sampling_fallback`.
    """
    lowered = detail.lower()
    if not any(marker in lowered for marker in _UNSUPPORTED_PARAMETER_DETAIL_MARKERS):
        return None
    for parameter_name in SAMPLING_PARAMETER_NAMES:
        if parameter_name in lowered:
            return parameter_name
    return None


async def execute_with_sampling_fallback(
    execute_attempt: Callable[[], Awaitable[_T]],
    payload: dict[str, Any],
    *,
    logger: Logger,
    provider_label: str,
) -> _T:
    """Run one adapter request, retrying once without a rejected sampling parameter.

    ``execute_attempt`` must perform one full ``retry_async``-wrapped request
    over ``payload`` (the dict is shared, so a later attempt sees the strip).
    A fatal ``ProviderError`` whose message blames a sampling parameter that is
    actually present in ``payload`` removes exactly that parameter and retries
    once; every other error — auth, rate limit, network, and rejections of
    parameters we never sent — propagates unchanged.
    """
    try:
        return await execute_attempt()
    except ProviderError as error:
        blamed_parameter = unsupported_sampling_parameter(str(error))
        if blamed_parameter is None or blamed_parameter not in payload:
            raise
        payload.pop(blamed_parameter, None)
        logger.warning(
            "%s rejected sampling parameter %r; retrying once without it",
            provider_label,
            blamed_parameter,
        )
        return await execute_attempt()


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
    so it never triggers model fallback (see ``.vorch/domain-maps/providers.md`` gotchas).
    """
    if isinstance(error, httpx.TimeoutException):
        return ProviderTimeoutError(f"Request failed: {error}")
    if isinstance(error, httpx.TransportError):
        return NetworkError(f"Connection failed: {error}")
    # Anything else (shouldn't happen at request-submission sites): surface as
    # a transport failure so retry semantics match.
    return NetworkError(f"Connection failed: {error}")


async def iter_sse_events(response: httpx.Response) -> AsyncIterator[SSEEvent]:
    """Yield framed Server-Sent Event data payloads and transport comments.

    SSE events may contain multiple ``data:`` lines. HTTPX yields individual
    lines, so adapters should consume framed events instead of parsing every
    line as complete JSON. Comments are yielded immediately without disturbing
    an in-progress multi-line data event; an Adapter may translate them into a
    Provider heartbeat when the concrete wire uses comments as keepalives.
    """
    data_parts: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_parts:
                yield SSEEvent(data="\n".join(data_parts))
                data_parts = []
            continue
        if line.startswith(":"):
            yield SSEEvent(comment=line[1:].lstrip(" "))
            continue
        if line.startswith("data:"):
            data_parts.append(line[len("data:") :].lstrip(" "))

    if data_parts:
        yield SSEEvent(data="\n".join(data_parts))


async def iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Yield complete SSE data payloads while ignoring transport comments."""

    async for event in iter_sse_events(response):
        if event.data is not None:
            yield event.data


def parse_sse_json_data(data: str, *, context: str) -> Any:
    """Parse one SSE data payload and classify malformed JSON as provider error."""
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"{context} sent malformed JSON in stream: {exc.msg}",
            retryable=False,
        ) from exc


def decode_response_json(response: httpx.Response, context: str) -> dict[str, Any]:
    """Decode a 2xx response body to JSON or raise a non-retryable ProviderError.

    Used by non-streaming request paths in provider adapters to assert the
    response body is a JSON object. Mirrors :func:`parse_sse_json_data` for the
    streaming path: malformed JSON and non-object payloads both surface as
    non-retryable ``ProviderError`` (the caller's classify step already
    filtered the HTTP status).
    """
    try:
        decoded = response.json()
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"{context} sent malformed JSON in response: {exc.msg}",
            retryable=False,
        ) from exc
    if not isinstance(decoded, dict):
        raise ProviderError(
            f"{context} sent non-object JSON in response",
            retryable=False,
        )
    return decoded
