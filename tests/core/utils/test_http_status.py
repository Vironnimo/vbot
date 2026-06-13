"""Tests for the shared HTTP retryable-status policy.

Verifies the single source of truth in :mod:`core.utils.http_status`: the
method-agnostic retryable set (429/502/503/504), the idempotent-only code
(500), and provider-specific ``extra`` codes.
"""

from __future__ import annotations

import pytest

from core.utils.http_status import (
    IDEMPOTENT_RETRYABLE_STATUS_CODES,
    RETRYABLE_STATUS_CODES,
    is_retryable_status,
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
    assert RETRYABLE_STATUS_CODES == frozenset({429, 502, 503, 504})


def test_idempotent_only_set_is_500() -> None:
    """The idempotent-only set is exactly 500."""
    assert IDEMPOTENT_RETRYABLE_STATUS_CODES == frozenset({500})
