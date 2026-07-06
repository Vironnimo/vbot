"""Tests for the shared HTTP retryable-status policy.

Verifies the single source of truth in :mod:`core.utils.http_status`: the
method-agnostic retryable set (429/502/503/504), the idempotent-only code
(500), provider-specific ``extra`` codes, and ``Retry-After`` header parsing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from core.utils.http_status import (
    IDEMPOTENT_RETRYABLE_STATUS_CODES,
    RETRYABLE_STATUS_CODES,
    is_retryable_status,
    parse_retry_after,
)

# ----- Method-agnostic retryable codes -----


@pytest.mark.parametrize("status_code", [429, 502, 503, 504])
@pytest.mark.parametrize("idempotent", [True, False])
def test_always_retryable_codes_regardless_of_method(status_code: int, idempotent: bool) -> None:
    """429/502/503/504 are retryable for any method."""
    assert is_retryable_status(status_code, idempotent=idempotent) is True


# ----- 500: idempotent-only -----


def test_500_retryable_only_when_idempotent() -> None:
    """500 is retryable for an idempotent request (GET/HEAD)."""
    assert is_retryable_status(500, idempotent=True) is True


def test_500_not_retryable_for_non_idempotent() -> None:
    """500 is not retryable for a non-idempotent request (POST)."""
    assert is_retryable_status(500, idempotent=False) is False


# ----- Non-retryable codes -----


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422, 501])
@pytest.mark.parametrize("idempotent", [True, False])
def test_non_retryable_codes(status_code: int, idempotent: bool) -> None:
    """Client errors and non-listed server errors are never retryable."""
    assert is_retryable_status(status_code, idempotent=idempotent) is False


# ----- Provider-specific extra codes -----


def test_extra_codes_are_retryable() -> None:
    """An ``extra`` code (e.g. Anthropic's 529) is retryable regardless of method."""
    assert is_retryable_status(529, idempotent=False, extra={529}) is True


def test_extra_does_not_make_500_retryable_for_non_idempotent() -> None:
    """``extra`` adds its own codes but does not relax the idempotent-only rule."""
    assert is_retryable_status(500, idempotent=False, extra={529}) is False


def test_unlisted_code_without_extra_is_not_retryable() -> None:
    """A code that is neither standard nor in ``extra`` is not retryable."""
    assert is_retryable_status(529, idempotent=True) is False


# ----- Constant content (locks the decided policy) -----


def test_retryable_set_is_method_agnostic_codes() -> None:
    """The always-retryable set is exactly 429/502/503/504."""
    assert frozenset({429, 502, 503, 504}) == RETRYABLE_STATUS_CODES


def test_idempotent_only_set_is_500() -> None:
    """The idempotent-only set is exactly 500."""
    assert frozenset({500}) == IDEMPOTENT_RETRYABLE_STATUS_CODES


# ----- parse_retry_after — Retry-After header parsing -----


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

    future = datetime.now(UTC) + timedelta(seconds=120)
    headers = httpx.Headers({"Retry-After": format_datetime(future, usegmt=True)})

    seconds = parse_retry_after(headers)

    assert seconds is not None
    # Allow scheduling slack — should land just under the full 120s window.
    assert 110 <= seconds <= 121


def test_parse_retry_after_http_date_in_past_clamps_to_zero() -> None:
    """An HTTP-date already in the past means "retry now" (clamped to 0)."""

    past = datetime.now(UTC) - timedelta(seconds=120)
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


def test_parse_retry_after_accepts_plain_lowercase_dict() -> None:
    """A plain lowercase-keyed mapping works too (web_fetch's normalized headers)."""

    assert parse_retry_after({"retry-after": "5"}) == 5.0
