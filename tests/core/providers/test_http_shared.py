"""Tests for the shared HTTP helpers in :mod:`core.providers._http_shared`.

Covers ``wrap_network_error`` mapping (any non-timeout
``httpx.TransportError`` becomes ``NetworkError``; only
``httpx.TimeoutException`` becomes ``ProviderTimeoutError``),
``parse_sse_json_data`` (malformed JSON becomes a non-retryable
``ProviderError``), and ``decode_response_json`` (non-object or
malformed JSON becomes a non-retryable ``ProviderError``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.providers._http_shared import (
    PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS,
    build_async_client,
    build_streaming_request,
    classify_http_status,
    connect_streaming_with_retry,
    decode_response_json,
    execute_with_sampling_fallback,
    format_http_error_detail,
    parse_sse_json_data,
    post_json_with_retry,
    provider_chat_timeout,
    provider_streaming_timeout,
    unsupported_sampling_parameter,
    wrap_network_error,
)
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)


def test_provider_chat_timeout_bounds_every_non_streaming_phase() -> None:
    """The shared client default cannot wait forever for response bytes."""

    timeout = provider_chat_timeout()

    assert timeout.connect == 60.0
    assert timeout.read == PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS == 180.0
    assert timeout.write == 60.0
    assert timeout.pool == 60.0


def test_provider_streaming_timeout_disables_only_the_read_timeout() -> None:
    """Open streams leave reads to higher-level stream liveness clocks."""

    timeout = provider_streaming_timeout()

    assert timeout.connect == 60.0
    assert timeout.read is None
    assert timeout.write == 60.0
    assert timeout.pool == 60.0


@pytest.mark.asyncio
async def test_streaming_requests_override_the_bounded_client_default() -> None:
    """The shared stream builder opts out of the client's finite read timeout."""

    client = build_async_client(base_url="https://example.com")
    try:
        non_streaming = client.build_request("POST", "/response")
        streaming = build_streaming_request(client, "POST", "/stream")
    finally:
        await client.aclose()

    assert non_streaming.extensions["timeout"]["read"] == 180.0
    assert streaming.extensions["timeout"]["read"] is None


# ---------------------------------------------------------------------------
# wrap_network_error — exhaustive mapping table
# ---------------------------------------------------------------------------


def test_wrap_network_error_timeout_exception_is_provider_timeout_error() -> None:
    """A bare ``httpx.TimeoutException`` becomes ``ProviderTimeoutError``."""

    wrapped = wrap_network_error(httpx.TimeoutException("timed out"))

    assert isinstance(wrapped, ProviderTimeoutError)
    assert wrapped.retryable is True
    assert "timed out" in str(wrapped)


def test_wrap_network_error_connect_timeout_is_provider_timeout_error() -> None:
    """``httpx.ConnectTimeout`` (subclass of TimeoutException) → ``ProviderTimeoutError``."""

    wrapped = wrap_network_error(httpx.ConnectTimeout("connect timed out"))

    assert isinstance(wrapped, ProviderTimeoutError)
    assert wrapped.retryable is True


def test_wrap_network_error_read_timeout_is_provider_timeout_error() -> None:
    """``httpx.ReadTimeout`` (subclass of TimeoutException) → ``ProviderTimeoutError``."""

    wrapped = wrap_network_error(httpx.ReadTimeout("read timed out"))

    assert isinstance(wrapped, ProviderTimeoutError)
    assert wrapped.retryable is True


def test_wrap_network_error_pool_timeout_is_provider_timeout_error() -> None:
    """``httpx.PoolTimeout`` (subclass of TimeoutException) → ``ProviderTimeoutError``."""

    wrapped = wrap_network_error(httpx.PoolTimeout("pool timed out"))

    assert isinstance(wrapped, ProviderTimeoutError)
    assert wrapped.retryable is True


def test_wrap_network_error_connect_error_is_network_error() -> None:
    """``httpx.ConnectError`` becomes ``NetworkError`` and stays a non-ProviderError."""

    wrapped = wrap_network_error(httpx.ConnectError("connection refused"))

    assert isinstance(wrapped, NetworkError)
    assert wrapped.retryable is True
    # ``NetworkError`` must remain a non-``ProviderError`` so it never triggers
    # model fallback (see ``.vorch/domain-maps/providers.md`` gotchas).
    assert not isinstance(wrapped, ProviderError)
    assert "connection refused" in str(wrapped)


def test_wrap_network_error_read_error_is_network_error() -> None:
    """``httpx.ReadError`` becomes ``NetworkError`` (retryable, not a ProviderError)."""

    request = httpx.Request("POST", "https://example.com/")
    wrapped = wrap_network_error(httpx.ReadError("connection reset", request=request))

    assert isinstance(wrapped, NetworkError)
    assert wrapped.retryable is True
    assert not isinstance(wrapped, ProviderError)


def test_wrap_network_error_write_error_is_network_error() -> None:
    """``httpx.WriteError`` becomes ``NetworkError``."""

    request = httpx.Request("POST", "https://example.com/")
    wrapped = wrap_network_error(httpx.WriteError("write failed", request=request))

    assert isinstance(wrapped, NetworkError)
    assert not isinstance(wrapped, ProviderError)


def test_wrap_network_error_remote_protocol_error_is_network_error() -> None:
    """``httpx.RemoteProtocolError`` becomes ``NetworkError``."""

    request = httpx.Request("POST", "https://example.com/")
    wrapped = wrap_network_error(httpx.RemoteProtocolError("server disconnected", request=request))

    assert isinstance(wrapped, NetworkError)
    assert wrapped.retryable is True
    assert not isinstance(wrapped, ProviderError)
    assert "server disconnected" in str(wrapped)


def test_wrap_network_error_local_protocol_error_is_network_error() -> None:
    """``httpx.LocalProtocolError`` is wrapped as ``NetworkError`` (non-ProviderError)."""

    request = httpx.Request("POST", "https://example.com/")
    wrapped = wrap_network_error(httpx.LocalProtocolError("local protocol error", request=request))

    assert isinstance(wrapped, NetworkError)
    assert not isinstance(wrapped, ProviderError)


def test_wrap_network_error_protocol_error_is_network_error() -> None:
    """``httpx.ProtocolError`` (subclass of TransportError) → ``NetworkError``."""

    request = httpx.Request("POST", "https://example.com/")
    wrapped = wrap_network_error(httpx.ProtocolError("protocol error", request=request))

    assert isinstance(wrapped, NetworkError)
    assert not isinstance(wrapped, ProviderError)


def test_wrap_network_error_preserves_cause_via_from_exc() -> None:
    """The returned exception can be raised with ``from`` to preserve the original cause."""

    original = httpx.ReadError("connection reset")
    wrapped = wrap_network_error(original)

    try:
        raise wrapped from original
    except NetworkError as exc:
        assert exc.__cause__ is original


# ---------------------------------------------------------------------------
# parse_sse_json_data — malformed JSON classification
# ---------------------------------------------------------------------------


def test_parse_sse_json_data_returns_dict_for_valid_json() -> None:
    """Valid JSON decodes to a Python object."""

    decoded = parse_sse_json_data('{"id":"1"}', context="test provider")

    assert decoded == {"id": "1"}


def test_parse_sse_json_data_raises_non_retryable_provider_error_on_malformed_json() -> None:
    """Malformed SSE data raises a non-retryable ``ProviderError``."""

    with pytest.raises(ProviderError) as exc_info:
        parse_sse_json_data('{"id":\n', context="test provider")

    assert exc_info.value.retryable is False
    assert "test provider" in str(exc_info.value)
    assert "malformed JSON" in str(exc_info.value)


def test_parse_sse_json_data_preserves_cause_via_from_exc() -> None:
    """The original ``json.JSONDecodeError`` is preserved as ``__cause__``."""

    with pytest.raises(ProviderError) as exc_info:
        parse_sse_json_data("not-json", context="test provider")

    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)


# ---------------------------------------------------------------------------
# decode_response_json — non-streaming response body classification
# ---------------------------------------------------------------------------


def _fake_response(payload: str) -> httpx.Response:
    """Build a synthetic 200 response with a raw JSON body for decode tests."""
    request = httpx.Request("POST", "https://example.com/")
    return httpx.Response(200, content=payload.encode("utf-8"), request=request)


def test_decode_response_json_returns_dict_for_object_body() -> None:
    """A JSON object body is returned as a dict."""

    decoded = decode_response_json(_fake_response('{"id":"1","name":"a"}'), context="test provider")

    assert decoded == {"id": "1", "name": "a"}


def test_decode_response_json_raises_non_retryable_provider_error_on_malformed_json() -> None:
    """Malformed JSON raises a non-retryable ``ProviderError`` keyed to *context*."""

    with pytest.raises(ProviderError) as exc_info:
        decode_response_json(_fake_response('{"id":\n'), context="test provider")

    assert exc_info.value.retryable is False
    assert "test provider" in str(exc_info.value)
    assert "malformed JSON" in str(exc_info.value)


def test_decode_response_json_raises_non_retryable_provider_error_on_non_object_json() -> None:
    """A top-level JSON array is rejected as a non-object response."""

    with pytest.raises(ProviderError) as exc_info:
        decode_response_json(_fake_response("[1, 2, 3]"), context="test provider")

    assert exc_info.value.retryable is False
    assert "non-object JSON" in str(exc_info.value)
    assert "test provider" in str(exc_info.value)


def test_decode_response_json_raises_non_retryable_provider_error_on_scalar_json() -> None:
    """A top-level JSON scalar is rejected as a non-object response."""

    with pytest.raises(ProviderError) as exc_info:
        decode_response_json(_fake_response("42"), context="test provider")

    assert exc_info.value.retryable is False
    assert "non-object JSON" in str(exc_info.value)


def test_decode_response_json_preserves_cause_via_from_exc() -> None:
    """The original ``json.JSONDecodeError`` is preserved as ``__cause__``."""

    with pytest.raises(ProviderError) as exc_info:
        decode_response_json(_fake_response("not-json"), context="test provider")

    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)


# ---------------------------------------------------------------------------
# classify_http_status — Retry-After attachment
# (``parse_retry_after`` itself lives in core.utils.http_status and is tested
# in tests/core/utils/test_http_status.py)
# ---------------------------------------------------------------------------


def test_classify_http_status_attaches_retry_after_to_rate_limit() -> None:
    """A 429 carries the parsed ``Retry-After`` onto the rate-limit error."""

    with pytest.raises(ProviderRateLimitError) as exc_info:
        classify_http_status(
            429,
            idempotent=False,
            response_headers=httpx.Headers({"Retry-After": "7"}),
        )

    assert exc_info.value.retry_after == 7.0


def test_classify_http_status_attaches_retry_after_to_retryable_error() -> None:
    """A retryable 503 carries the parsed ``Retry-After`` onto the error."""

    with pytest.raises(ProviderError) as exc_info:
        classify_http_status(
            503,
            idempotent=False,
            response_headers=httpx.Headers({"retry-after-ms": "2000"}),
        )

    assert exc_info.value.retryable is True
    assert exc_info.value.retry_after == 2.0


def test_classify_http_status_504_is_retryable_in_provider_path() -> None:
    """A 504 Gateway Timeout is retryable on the (non-idempotent) provider path."""

    with pytest.raises(ProviderError) as exc_info:
        classify_http_status(504, idempotent=False)

    assert exc_info.value.retryable is True


def test_classify_http_status_500_is_not_retryable_in_provider_path() -> None:
    """A 500 is not retryable on the non-idempotent provider path."""

    with pytest.raises(ProviderError) as exc_info:
        classify_http_status(500, idempotent=False)

    assert exc_info.value.retryable is False


def test_classify_http_status_500_is_retryable_for_idempotent_request() -> None:
    """A 500 is retryable when the caller declares the request replay-safe."""

    with pytest.raises(ProviderError) as exc_info:
        classify_http_status(500, idempotent=True)

    assert exc_info.value.retryable is True


def test_classify_http_status_rate_limit_without_headers_has_no_hint() -> None:
    """With no headers passed, ``retry_after`` stays ``None``."""

    with pytest.raises(ProviderRateLimitError) as exc_info:
        classify_http_status(429, idempotent=False)

    assert exc_info.value.retry_after is None


def test_classify_http_status_does_not_attach_to_non_retryable_error() -> None:
    """A non-retryable 4xx never carries a retry hint even if the header is present."""

    with pytest.raises(ProviderError) as exc_info:
        classify_http_status(
            400,
            idempotent=False,
            response_headers=httpx.Headers({"Retry-After": "9"}),
        )

    assert exc_info.value.retryable is False
    assert exc_info.value.retry_after is None


def test_classify_http_status_auth_error_ignores_retry_after() -> None:
    """A 401 raises an auth error (not retryable); its hint stays the default ``None``."""

    with pytest.raises(ProviderAuthError) as exc_info:
        classify_http_status(
            401,
            idempotent=False,
            response_headers=httpx.Headers({"Retry-After": "9"}),
        )

    assert exc_info.value.retry_after is None


def test_unsupported_sampling_parameter_matches_provider_rejection_wordings() -> None:
    """Marker plus parameter name identifies the blamed sampling parameter."""

    assert (
        unsupported_sampling_parameter("400 Unsupported parameter: 'temperature'") == "temperature"
    )
    assert (
        unsupported_sampling_parameter("400 temperature is not supported when thinking is enabled")
        == "temperature"
    )
    assert unsupported_sampling_parameter("400 Unknown parameter: top_k") == "top_k"
    assert unsupported_sampling_parameter("400 Unrecognized request argument: top_p") == "top_p"


def test_unsupported_sampling_parameter_ignores_incomplete_or_foreign_details() -> None:
    """Neither a marker nor a parameter alone is evidence; other parameters are ignored."""

    assert unsupported_sampling_parameter("") is None
    assert unsupported_sampling_parameter("400 temperature") is None
    assert unsupported_sampling_parameter("400 Unsupported parameter: 'max_tokens'") is None
    assert unsupported_sampling_parameter("Rate limited: too many requests") is None


@pytest.mark.asyncio
async def test_execute_with_sampling_fallback_strips_blamed_parameter_and_retries_once() -> None:
    """A fatal rejection of a sent sampling parameter triggers exactly one stripped retry."""

    payload = {"model": "m", "temperature": 0.7, "top_p": 0.9}
    attempts: list[dict[str, Any]] = []

    async def attempt() -> str:
        attempts.append(dict(payload))
        if len(attempts) == 1:
            raise ProviderError(
                "Provider error: 400 Unsupported parameter: 'temperature'",
                retryable=False,
            )
        return "ok"

    result = await execute_with_sampling_fallback(
        attempt, payload, logger=logging.getLogger("test"), provider_label="stub"
    )

    assert result == "ok"
    assert attempts == [
        {"model": "m", "temperature": 0.7, "top_p": 0.9},
        {"model": "m", "top_p": 0.9},
    ]


@pytest.mark.asyncio
async def test_execute_with_sampling_fallback_reraises_when_parameter_not_sent() -> None:
    """A rejection of a parameter the payload never carried is not retried."""

    payload = {"model": "m"}
    attempts: list[dict[str, Any]] = []

    async def attempt() -> str:
        attempts.append(dict(payload))
        raise ProviderError(
            "Provider error: 400 Unsupported parameter: 'temperature'",
            retryable=False,
        )

    with pytest.raises(ProviderError):
        await execute_with_sampling_fallback(
            attempt, payload, logger=logging.getLogger("test"), provider_label="stub"
        )

    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_execute_with_sampling_fallback_reraises_unrelated_errors() -> None:
    """Auth and other errors pass through untouched."""

    payload = {"model": "m", "temperature": 0.7}

    async def attempt() -> str:
        raise ProviderAuthError("Authentication error: 401 no key")

    with pytest.raises(ProviderAuthError):
        await execute_with_sampling_fallback(
            attempt, payload, logger=logging.getLogger("test"), provider_label="stub"
        )


# ---------------------------------------------------------------------------
# connect_streaming_with_retry / post_json_with_retry — establishment
# ---------------------------------------------------------------------------


def _never_expected_error_status(status_code: int, error_body: str, headers: httpx.Headers) -> None:
    raise AssertionError(f"error handler unexpectedly called for {status_code}: {error_body}")


def _mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="https://example.com", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_connect_streaming_with_retry_returns_established_stream() -> None:
    """A successful POST returns the streamed response with the built headers."""

    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=b"stream-bytes")

    async def build_headers() -> dict[str, str]:
        return {"Authorization": "Bearer t1"}

    client = _mock_client(handler)
    try:
        response = await connect_streaming_with_retry(
            client,
            "/v1/chat",
            {"model": "m"},
            build_headers=build_headers,
            handle_error_status=_never_expected_error_status,
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert [request.url.path for request in seen_requests] == ["/v1/chat"]
    assert json.loads(seen_requests[0].content) == {"model": "m"}
    assert seen_requests[0].headers["Authorization"] == "Bearer t1"


@pytest.mark.asyncio
async def test_connect_streaming_with_retry_rebuilds_headers_on_every_attempt() -> None:
    """Each retry re-consults the header builder so refreshed tokens go out."""

    sent_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_tokens.append(request.headers["Authorization"])
        if len(sent_tokens) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200)

    builds: list[str] = []

    async def build_headers() -> dict[str, str]:
        token = f"t{len(builds) + 1}"
        builds.append(token)
        return {"Authorization": f"Bearer {token}"}

    def handle_error_status(status_code: int, error_body: str, headers: httpx.Headers) -> None:
        classify_http_status(
            status_code,
            idempotent=False,
            detail=format_http_error_detail(status_code, error_body),
            response_headers=headers,
        )

    client = _mock_client(handler)
    try:
        with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            response = await connect_streaming_with_retry(
                client,
                "/v1/chat",
                {},
                build_headers=build_headers,
                handle_error_status=handle_error_status,
            )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert builds == ["t1", "t2"]
    assert sent_tokens == ["Bearer t1", "Bearer t2"]


@pytest.mark.asyncio
async def test_connect_streaming_with_retry_passes_read_body_to_error_handler() -> None:
    """The error handler receives the fully-read body of a failed establishment."""

    calls: list[tuple[int, str]] = []

    def handle_error_status(status_code: int, error_body: str, headers: httpx.Headers) -> None:
        calls.append((status_code, error_body))
        raise ProviderAuthError(f"Authentication error: {error_body}")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"bad key")

    async def build_headers() -> dict[str, str]:
        return {}

    client = _mock_client(handler)
    try:
        with pytest.raises(ProviderAuthError):
            await connect_streaming_with_retry(
                client,
                "/v1/chat",
                {},
                build_headers=build_headers,
                handle_error_status=handle_error_status,
            )
    finally:
        await client.aclose()

    assert calls == [(401, "bad key")]


@pytest.mark.asyncio
async def test_connect_streaming_with_retry_applies_custom_transport_wrapper() -> None:
    """An adapter's transport wrapper re-labels connection failures in place."""

    wrapped_errors: list[Exception] = []

    class TerminalTransportError(Exception):
        pass

    def wrap_transport_error(error: httpx.TransportError) -> Exception:
        wrapped_errors.append(error)
        return TerminalTransportError("provider offline")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async def build_headers() -> dict[str, str]:
        return {}

    client = _mock_client(handler)
    try:
        with pytest.raises(TerminalTransportError):
            await connect_streaming_with_retry(
                client,
                "/v1/chat",
                {},
                build_headers=build_headers,
                handle_error_status=_never_expected_error_status,
                wrap_transport_error=wrap_transport_error,
            )
    finally:
        await client.aclose()

    assert isinstance(wrapped_errors[0], httpx.ConnectError)


@pytest.mark.asyncio
async def test_post_json_with_retry_decodes_object_reply() -> None:
    """A 2xx JSON object reply is decoded under the provider context."""

    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=b'{"ok": true}')

    async def build_headers() -> dict[str, str]:
        return {"Authorization": "Bearer t1"}

    client = _mock_client(handler)
    try:
        result = await post_json_with_retry(
            client,
            "/v1/responses",
            {"model": "m"},
            build_headers=build_headers,
            handle_error_status=_never_expected_error_status,
            provider_context="Stub provider",
        )
    finally:
        await client.aclose()

    assert result == {"ok": True}
    assert json.loads(seen_requests[0].content) == {"model": "m"}
    assert seen_requests[0].headers["Authorization"] == "Bearer t1"


@pytest.mark.asyncio
async def test_post_json_with_retry_passes_buffered_text_to_error_handler() -> None:
    """Non-streaming error bodies arrive as buffered text, not raw bytes."""

    calls: list[tuple[int, str]] = []

    def handle_error_status(status_code: int, error_body: str, headers: httpx.Headers) -> None:
        calls.append((status_code, error_body))
        raise ProviderError(f"Provider error: {status_code}", retryable=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="nope")

    async def build_headers() -> dict[str, str]:
        return {}

    client = _mock_client(handler)
    try:
        with pytest.raises(ProviderError):
            await post_json_with_retry(
                client,
                "/v1/responses",
                {},
                build_headers=build_headers,
                handle_error_status=handle_error_status,
                provider_context="Stub provider",
            )
    finally:
        await client.aclose()

    assert calls == [(400, "nope")]


def test_format_http_error_detail_prefers_body_and_falls_back_to_status() -> None:
    """Non-empty bodies render after the status; empty bodies leave bare status."""

    assert format_http_error_detail(502, "gateway boom") == "502 gateway boom"
    assert format_http_error_detail(502, "") == "502"
    assert format_http_error_detail(502, None) == "502"
