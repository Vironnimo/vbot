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

import httpx
import pytest

from core.providers._http_shared import (
    decode_response_json,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers.errors import NetworkError, ProviderError, ProviderTimeoutError

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
    # model fallback (see ``.vorch/specs/providers.md`` gotchas).
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
