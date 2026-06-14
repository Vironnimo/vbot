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
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from core.providers._http_shared import (
    classify_http_status,
    decode_response_json,
    parse_retry_after,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

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
# parse_retry_after — Retry-After header parsing
# ---------------------------------------------------------------------------


def test_parse_retry_after_delay_seconds() -> None:
    """``Retry-After`` as a plain integer is read as seconds."""

    assert parse_retry_after(httpx.Headers({"Retry-After": "5"})) == 5.0


def test_parse_retry_after_fractional_seconds() -> None:
    """A fractional seconds value is accepted (lenient over RFC's integer form)."""

    assert parse_retry_after(httpx.Headers({"Retry-After": "2.5"})) == 2.5


def test_parse_retry_after_negative_seconds_is_ignored() -> None:
    """A negative delay is meaningless and is treated as no hint."""

    assert parse_retry_after(httpx.Headers({"Retry-After": "-3"})) is None


def test_parse_retry_after_ms_header() -> None:
    """``retry-after-ms`` (millisecond hint) is converted to seconds."""

    assert parse_retry_after(httpx.Headers({"retry-after-ms": "1500"})) == 1.5


def test_parse_retry_after_ms_takes_priority_over_seconds() -> None:
    """The finer-grained millisecond hint wins when both headers are present."""

    headers = httpx.Headers({"retry-after-ms": "250", "Retry-After": "5"})

    assert parse_retry_after(headers) == 0.25


def test_parse_retry_after_http_date_future() -> None:
    """An HTTP-date in the future yields the seconds until that moment."""

    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    headers = httpx.Headers({"Retry-After": format_datetime(future, usegmt=True)})

    seconds = parse_retry_after(headers)

    assert seconds is not None
    # Allow scheduling slack — should land just under the full 120s window.
    assert 110 <= seconds <= 121


def test_parse_retry_after_http_date_in_past_clamps_to_zero() -> None:
    """An HTTP-date already in the past means "retry now" (clamped to 0)."""

    past = datetime.now(timezone.utc) - timedelta(seconds=120)
    headers = httpx.Headers({"Retry-After": format_datetime(past, usegmt=True)})

    assert parse_retry_after(headers) == 0.0


def test_parse_retry_after_missing_header_is_none() -> None:
    """No ``Retry-After`` header yields no hint."""

    assert parse_retry_after(httpx.Headers({})) is None


def test_parse_retry_after_blank_header_is_none() -> None:
    """A whitespace-only header value yields no hint."""

    assert parse_retry_after(httpx.Headers({"Retry-After": "   "})) is None


def test_parse_retry_after_malformed_header_is_none() -> None:
    """An unparseable value is ignored rather than raising."""

    assert parse_retry_after(httpx.Headers({"Retry-After": "soon-ish"})) is None


# ---------------------------------------------------------------------------
# classify_http_status — Retry-After attachment
# ---------------------------------------------------------------------------


def test_classify_http_status_attaches_retry_after_to_rate_limit() -> None:
    """A 429 carries the parsed ``Retry-After`` onto the rate-limit error."""

    with pytest.raises(ProviderRateLimitError) as exc_info:
        classify_http_status(429, response_headers=httpx.Headers({"Retry-After": "7"}))

    assert exc_info.value.retry_after == 7.0


def test_classify_http_status_attaches_retry_after_to_retryable_error() -> None:
    """A retryable 503 carries the parsed ``Retry-After`` onto the error."""

    with pytest.raises(ProviderError) as exc_info:
        classify_http_status(503, response_headers=httpx.Headers({"retry-after-ms": "2000"}))

    assert exc_info.value.retryable is True
    assert exc_info.value.retry_after == 2.0


def test_classify_http_status_504_is_retryable_in_provider_path() -> None:
    """A 504 Gateway Timeout is retryable on the (non-idempotent) provider path."""

    with pytest.raises(ProviderError) as exc_info:
        classify_http_status(504)

    assert exc_info.value.retryable is True


def test_classify_http_status_500_is_not_retryable_in_provider_path() -> None:
    """A 500 is not retryable on the non-idempotent provider path."""

    with pytest.raises(ProviderError) as exc_info:
        classify_http_status(500)

    assert exc_info.value.retryable is False


def test_classify_http_status_rate_limit_without_headers_has_no_hint() -> None:
    """With no headers passed, ``retry_after`` stays ``None``."""

    with pytest.raises(ProviderRateLimitError) as exc_info:
        classify_http_status(429)

    assert exc_info.value.retry_after is None


def test_classify_http_status_does_not_attach_to_non_retryable_error() -> None:
    """A non-retryable 4xx never carries a retry hint even if the header is present."""

    with pytest.raises(ProviderError) as exc_info:
        classify_http_status(400, response_headers=httpx.Headers({"Retry-After": "9"}))

    assert exc_info.value.retryable is False
    assert exc_info.value.retry_after is None


def test_classify_http_status_auth_error_ignores_retry_after() -> None:
    """A 401 raises an auth error (not retryable); its hint stays the default ``None``."""

    with pytest.raises(ProviderAuthError) as exc_info:
        classify_http_status(401, response_headers=httpx.Headers({"Retry-After": "9"}))

    assert exc_info.value.retry_after is None
