"""Provider-specific exception classes.

Domain-specific provider exceptions and related network error classes.

Provider subclasses hard-code their ``retryable`` flags so that the retry
utility can decide whether to re-attempt the call.
"""

import json
from collections.abc import Mapping
from typing import Any

from core.utils.errors import ProviderError, VBotError


class NetworkError(VBotError):
    """Network-level error (dropped connection, DNS failure, etc.).

    Not a subclass of ProviderError - a network error is not provider-specific
    and must not trigger model fallback. Retryable - the user can retry once
    connectivity is restored.
    """

    retryable: bool = True


class ProviderAuthError(ProviderError):
    """Authentication or authorization error (HTTP 401 / 403).

    Not retryable — the request will fail again until credentials change.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message, retryable=False)


class ProviderStreamingUnsupportedError(ProviderError):
    """The provider/model cannot serve this request as a stream.

    Not retryable — retrying the same streaming request will fail again. The
    chat loop catches this specific type to transparently fall back to a
    non-streaming request before any visible output has been emitted.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message, retryable=False)


class ProviderRateLimitError(ProviderError):
    """Rate-limit error (HTTP 429).

    Retryable — the server is throttling requests and will accept them later.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message, retryable=True)


class ProviderTimeoutError(ProviderError):
    """Connection or read timeout.

    Retryable — transient network issues may resolve on retry.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message, retryable=True)


class ProviderOutcomeUnknownError(ProviderError):
    """A non-idempotent provider request may have completed.

    Retrying is unsafe because the provider may already have produced and
    billed the result even though vBot could not receive or parse it.
    """

    code = "provider_outcome_unknown"

    def __init__(self, message: str, *, operation_key: str) -> None:
        self.operation_key = operation_key
        super().__init__(
            f"{self.code} (operation_key={operation_key}): {message}",
            retryable=False,
        )


class CatalogEntrySkipped(VBotError):  # noqa: N818
    """Signal that a model catalog entry should be skipped during discovery.

    Raised by adapter normalize_catalog_entry() implementations for expected
    non-error skip conditions (for example non-chat or archived models).
    The discovery loop catches and discards it.
    """


# ---------------------------------------------------------------------------
# In-band provider error classification
# ---------------------------------------------------------------------------

# Provider in-band error objects (SSE error chunks, structured HTTP error
# bodies) carry optional structured fields across the OpenAI-compatible and
# Responses-shaped wires: ``message``, a string or numeric ``code``,
# ``metadata.error_type`` / top-level ``error_type`` (the typed vocabulary
# below), and an ``availability`` object whose documented ``retryable`` /
# ``retry_after`` pair is the router's own recovery hint.
IN_BAND_AUTH_ERROR_CODES = frozenset(
    {"authentication", "authentication_error", "invalid_api_key", "unauthorized"}
)
IN_BAND_RATE_LIMIT_ERROR_CODES = frozenset({"rate_limit_exceeded"})
IN_BAND_TIMEOUT_ERROR_CODES = frozenset({"timeout"})
IN_BAND_TRANSIENT_ERROR_CODES = frozenset(
    {"provider_overloaded", "provider_unavailable", "server", "server_error"}
)
IN_BAND_RETRYABLE_NUMERIC_CODES = frozenset({429, 502, 503, 504})

# Deterministic failures where an identical retry can never help, even on a
# multi-upstream router: token/context limits, billing, permissions, and
# content policy. Everything else unclassified may become retryable when the
# caller opts in via ``lenient_unknown``.
IN_BAND_FATAL_ERROR_CODES = frozenset(
    {
        "context_length_exceeded",
        "max_tokens_exceeded",
        "token_limit_exceeded",
        "string_too_long",
        "payment_required",
        "permission_denied",
        "content_policy_violation",
        "refusal",
    }
)


def _first_non_empty_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _error_text(message: str | None, error: Mapping[str, Any]) -> str:
    """Build the persisted error text with the raw body appended as JSON.

    Run failures persist ``str(exception)``, so the structured payload only
    survives to the UI's collapsible details block when it is part of the
    message. The trailing-JSON shape (``<summary>: {body}``) matches what the
    HTTP-status path already produces and what the WebUI error parser expects.
    A payload without its own message stays a bare JSON object instead of an
    unparseable Python repr.
    """

    body = json.dumps(error, ensure_ascii=False)
    if message:
        return f"{message}: {body}"
    return body


def classify_in_band_provider_error(
    error: Any,
    *,
    lenient_unknown: bool = False,
) -> ProviderError:
    """Map one provider in-band error object into vBot's shared error taxonomy.

    Reads the documented structured fields — the typed ``error_type``
    (top-level or under ``metadata``), a string or numeric ``code``, and the
    router's ``availability.retryable`` / ``availability.retry_after`` hints —
    instead of treating every in-band error as fatal. With
    ``lenient_unknown=True`` (multi-upstream routers such as OpenRouter), an
    error carrying no known classification becomes retryable: routing can serve
    the identical request through a different upstream endpoint, and Chat bounds
    the resulting retries. Without it (single-endpoint wires), unclassified
    errors stay fatal exactly as before.

    The raised error's message embeds the raw payload as trailing JSON so the
    provider's structured detail (upstream message, code, router metadata)
    survives into persisted run failures and the UI details block; see
    :func:`_error_text`.
    """

    if not isinstance(error, Mapping):
        return ProviderError(str(error), retryable=False)

    raw_message = error.get("message")
    message = raw_message if isinstance(raw_message, str) and raw_message else None
    message = _error_text(message, error)
    metadata = error.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    code = error.get("code")
    classifier = _first_non_empty_string(metadata.get("error_type"), error.get("error_type"))
    if classifier is None and isinstance(code, str) and code:
        classifier = code
    numeric_code = code if isinstance(code, int) and not isinstance(code, bool) else None

    availability = error.get("availability")
    availability = availability if isinstance(availability, Mapping) else {}

    def _build(retryable: bool) -> ProviderError:
        provider_error = ProviderError(message, retryable=retryable)
        if retryable:
            retry_after = availability.get("retry_after")
            if (
                isinstance(retry_after, int)
                and not isinstance(retry_after, bool)
                and retry_after > 0
            ):
                provider_error.retry_after = retry_after
        return provider_error

    if classifier in IN_BAND_AUTH_ERROR_CODES or numeric_code in (401, 403):
        return ProviderAuthError(message)
    if classifier in IN_BAND_RATE_LIMIT_ERROR_CODES or numeric_code == 429:
        return _rate_limit_error(message, availability)
    if classifier in IN_BAND_TIMEOUT_ERROR_CODES or numeric_code == 504:
        return ProviderTimeoutError(message)
    if classifier in IN_BAND_TRANSIENT_ERROR_CODES or numeric_code in {502, 503}:
        return _build(True)
    if classifier in IN_BAND_FATAL_ERROR_CODES:
        return _build(False)
    if availability.get("retryable") is True:
        return _build(True)
    return _build(lenient_unknown)


def _rate_limit_error(message: str, availability: Mapping[str, Any]) -> ProviderRateLimitError:
    rate_limit_error = ProviderRateLimitError(message)
    retry_after = availability.get("retry_after")
    if isinstance(retry_after, int) and not isinstance(retry_after, bool) and retry_after > 0:
        rate_limit_error.retry_after = retry_after
    return rate_limit_error
