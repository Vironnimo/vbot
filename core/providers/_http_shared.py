"""Shared HTTP error classification utilities for provider adapters.

Private module — not exported from ``core.providers``.
Provides common constants and functions used by both OpenAI-compatible
and Anthropic adapters for classifying HTTP errors and wrapping network
exceptions.
"""

from __future__ import annotations

from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

# ---------------------------------------------------------------------------
# HTTP status constants
# ---------------------------------------------------------------------------

# Standard retryable HTTP status codes (common to all providers).
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 502, 503})

# Auth-related HTTP status codes — not retryable.
_AUTH_ERROR_STATUS_CODES: frozenset[int] = frozenset({401, 403})

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


def wrap_network_error(error: Exception) -> ProviderTimeoutError:
    """Wrap an httpx network exception in ``ProviderTimeoutError``.

    Converts ``httpx.TimeoutException`` and ``httpx.ConnectError`` into
    ``ProviderTimeoutError`` (retryable).  Other exceptions are wrapped
    as-is with a generic message.
    """
    return ProviderTimeoutError(f"Request failed: {error}")
