"""Tests for the shared HTTP retryable-status policy.

Verifies the single source of truth in :mod:`core.utils.http_status`: the
method-agnostic retryable set (429/502/503/504), the idempotent-only code
(500), provider-specific ``extra`` codes, and ``Retry-After`` header parsing.
"""

from __future__ import annotations

from collections.abc import Mapping
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


def test_retryable_status_sets_lock_the_decided_policy() -> None:
    assert frozenset({429, 502, 503, 504}) == RETRYABLE_STATUS_CODES
    assert frozenset({500}) == IDEMPOTENT_RETRYABLE_STATUS_CODES


@pytest.mark.parametrize(
    ("status_code", "idempotent", "extra", "retryable"),
    [
        # 429/502/503/504 are retryable for any method.
        (503, False, None, True),
        # 500 is retryable only for an idempotent request (GET/HEAD).
        (500, True, None, True),
        (500, False, None, False),
        # Client errors and unlisted server errors never are.
        (404, True, None, False),
        (501, True, None, False),
        # An ``extra`` code (e.g. Anthropic's 529) is retryable for any method ...
        (529, False, {529}, True),
        (529, True, None, False),
        # ... but does not relax the idempotent-only rule.
        (500, False, {529}, False),
    ],
)
def test_is_retryable_status(
    status_code: int, idempotent: bool, extra: set[int] | None, retryable: bool
) -> None:
    assert is_retryable_status(status_code, idempotent=idempotent, extra=extra) is retryable


@pytest.mark.parametrize(
    ("headers", "seconds"),
    [
        (httpx.Headers({"Retry-After": "5"}), 5.0),
        # Lenient over the RFC's integer form.
        (httpx.Headers({"Retry-After": "2.5"}), 2.5),
        (httpx.Headers({"retry-after-ms": "1500"}), 1.5),
        # The finer-grained millisecond hint wins when both headers are present.
        (httpx.Headers({"retry-after-ms": "250", "Retry-After": "5"}), 0.25),
        # A plain lowercase-keyed mapping works too (web_fetch's normalized headers).
        ({"retry-after": "5"}, 5.0),
        (httpx.Headers({}), None),
        (httpx.Headers({"Retry-After": "   "}), None),
        (httpx.Headers({"Retry-After": "soon-ish"}), None),
        # A negative delay is meaningless.
        (httpx.Headers({"Retry-After": "-3"}), None),
    ],
)
def test_parse_retry_after_reads_a_delay_in_seconds(
    headers: Mapping[str, str], seconds: float | None
) -> None:
    assert parse_retry_after(headers) == seconds


def test_parse_retry_after_reads_an_http_date_as_the_time_until_then() -> None:
    def at(offset: timedelta) -> httpx.Headers:
        date = datetime.now(UTC) + offset
        return httpx.Headers({"Retry-After": format_datetime(date, usegmt=True)})

    seconds = parse_retry_after(at(timedelta(seconds=120)))

    # Allow scheduling slack: just under the full 120 s window.
    assert seconds is not None and 110 <= seconds <= 121
    # A date already in the past means "retry now".
    assert parse_retry_after(at(timedelta(seconds=-120))) == 0.0
