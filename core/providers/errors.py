"""Provider-specific exception classes.

Domain-specific provider exceptions and related network error classes.

Provider subclasses hard-code their ``retryable`` flags so that the retry
utility can decide whether to re-attempt the call.
"""

import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from core.utils.errors import ProviderError, VBotError
from core.utils.retry import MAX_RETRY_AFTER_SECONDS


class NetworkError(VBotError):
    """Network-level error (dropped connection, DNS failure, etc.).

    Remains Provider-neutral and retryable. Chat owns same-Model recovery and
    may then advance an explicitly configured Model fallback chain, since
    another route can reach a different host.
    """

    failure_class = "network"

    retryable: bool = True


class ProviderAuthError(ProviderError):
    """Authentication or authorization error (HTTP 401 / 403).

    Not retryable — the request will fail again until credentials change.
    """

    failure_class = "auth"

    def __init__(self, message: str = "") -> None:
        super().__init__(message, retryable=False)


class ProviderRequestTooLargeError(ProviderError):
    """The request exceeds the Provider's body limit.

    Raised before any I/O when a wire measures its body against a verified
    limit (both sizes known), or when the Provider answers HTTP 413 (sizes
    unknown). Chat retires already delivered images and retries smaller.
    """

    failure_class = "rejected"

    code = "provider_request_too_large"

    def __init__(
        self, size_bytes: int | None = None, max_bytes: int | None = None, *, detail: str = ""
    ) -> None:
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        if size_bytes is not None and max_bytes is not None:
            message = (
                f"The request body is {size_bytes} bytes; this Provider allows at most "
                f"{max_bytes} bytes. Reduce the request size, for example by sending fewer "
                "or smaller images. Nothing was sent."
            )
        else:
            message = (
                "The Provider rejected the request as too large"
                + (f" ({detail})" if detail else "")
                + ". Reduce the request size, for example by sending fewer or smaller images."
            )
        super().__init__(message, retryable=False)


class ProviderStreamingUnsupportedError(ProviderError):
    """The provider/model cannot serve this request as a stream.

    Not retryable — retrying the same streaming request will fail again. The
    chat loop catches this specific type to transparently fall back to a
    non-streaming request before any visible output has been emitted.
    """

    failure_class = "rejected"

    def __init__(self, message: str = "") -> None:
        super().__init__(message, retryable=False)


class ProviderRateLimitError(ProviderError):
    """Rate-limit error (HTTP 429).

    Retryable — the server is throttling requests and will accept them later.
    """

    failure_class = "rate_limit"

    def __init__(self, message: str = "") -> None:
        super().__init__(message, retryable=True)


class ProviderUsageLimitError(ProviderError):
    """The account's usage limit is reached until the reset the Provider reported.

    Not retryable: the reset is usually hours away, far beyond any retry wait.
    A reset that falls within the retry cap is a ``ProviderRateLimitError``
    waiting exactly until then instead (:func:`usage_limit_error`).
    ``resets_at`` is the reported reset in UTC, ``None`` when none was reported.
    """

    failure_class = "usage_limit"

    def __init__(self, message: str, *, resets_at: datetime | None = None) -> None:
        super().__init__(message, retryable=False)
        self.resets_at = resets_at


class ProviderTimeoutError(ProviderError):
    """Connection or read timeout.

    Retryable — transient network issues may resolve on retry.
    """

    failure_class = "timeout"

    def __init__(self, message: str = "") -> None:
        super().__init__(message, retryable=True)


class ProviderOutcomeUnknownError(ProviderError):
    """A non-idempotent provider request may have completed.

    Retrying is unsafe because the provider may already have produced and
    billed the result even though vBot could not receive or parse it.
    """

    failure_class = "outcome_unknown"

    code = "provider_outcome_unknown"

    def __init__(self, message: str, *, operation_key: str) -> None:
        self.operation_key = operation_key
        super().__init__(
            f"{self.code} (operation_key={operation_key}): {message}",
            retryable=False,
        )


class ProviderContentRefusedError(ProviderError):
    """The provider declined to produce the requested content.

    A refusal is a known outcome - nothing was produced - so task clients
    report it instead of an unknown outcome even for non-idempotent requests.
    The unchanged request is refused again, so it is never retried.
    ``reason`` is the provider's own explanation, when it gave one.
    """

    failure_class = "refused"

    code = "generation_refused"

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        super().__init__(
            f"The provider refused the request: {reason}"
            if reason
            else "The provider refused the request without giving a reason",
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
# ``metadata.error_type`` / top-level ``error_type`` / ``type`` (the typed
# vocabulary below), and an ``availability`` object whose documented ``retryable`` /
# ``retry_after`` pair is the router's own recovery hint. Messages-compatible
# streams send ``{"type": "error", "error": {"type": ...}}`` after HTTP 200;
# their documented ``error.type`` values share the same vocabulary
# (https://platform.claude.com/docs/en/api/errors, read 2026-09-24).
IN_BAND_AUTH_ERROR_CODES = frozenset(
    {"authentication", "authentication_error", "invalid_api_key", "unauthorized"}
)
IN_BAND_RATE_LIMIT_ERROR_CODES = frozenset({"rate_limit_exceeded", "rate_limit_error"})
IN_BAND_TIMEOUT_ERROR_CODES = frozenset({"timeout", "timeout_error"})
IN_BAND_TRANSIENT_ERROR_CODES = frozenset(
    {
        "api_error",
        "overloaded_error",
        "provider_overloaded",
        "provider_unavailable",
        "server",
        "server_error",
        # OpenAI's Codex client retries this on a fresh connection whatever
        # the status of the WebSocket error frame carrying it.
        "websocket_connection_limit_reached",
    }
)

# An exhausted quota, spend cap or plan entitlement, whatever status carries it
# (also HTTP 429): waiting does not help. These are the codes and types OpenAI's
# Codex client stops on (``openai/codex`` ``codex-api/src/api_bridge.rs`` and
# ``codex-api/src/sse/responses_error.rs``, read 2026-10-19).
QUOTA_EXHAUSTED_ERROR_CODES = frozenset(
    {
        "insufficient_quota",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        "project_spend_limit_exceeded",
        "usage_not_included",
    }
)

# The ChatGPT subscription's usage limit (Codex backend), with the reset in
# ``resets_at`` (epoch seconds) or, from older backends, ``resets_in_seconds``.
USAGE_LIMIT_ERROR_TYPE = "usage_limit_reached"

# Deterministic failures where an identical retry can never help, even on a
# multi-upstream router: token/context limits, billing, permissions, and
# content policy. Everything else unclassified may become retryable when the
# caller opts in via ``lenient_unknown``. The quota, spend, prompt and policy
# codes are the ones OpenAI's Codex client stops on instead of retrying
# (``openai/codex`` ``codex-api/src/sse/responses_error.rs``, read 2026-09-29).
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
        "billing_error",
        "conflict_error",
        "invalid_request_error",
        "not_found_error",
        "permission_error",
        "request_too_large",
        *QUOTA_EXHAUSTED_ERROR_CODES,
        "invalid_prompt",
        "cyber_policy",
        "bio_policy",
        "misalignment_policy_violation",
    }
)


def _first_non_empty_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def in_band_error_text(message: str | None, error: Mapping[str, Any]) -> str:
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
    (top-level or under ``metadata``), a string or numeric ``code``, a fallback
    ``type``, and the router's ``availability.retryable`` / ``availability.retry_after`` hints —
    instead of treating every in-band error as fatal. With
    ``lenient_unknown=True`` (multi-upstream routers such as OpenRouter), an
    error carrying no known classification becomes retryable: routing can serve
    the identical request through a different upstream endpoint, and Chat bounds
    the resulting retries. Without it (single-endpoint wires), unclassified
    errors stay fatal exactly as before.

    The raised error's message embeds the raw payload as trailing JSON so the
    provider's structured detail (upstream message, code, router metadata)
    survives into persisted run failures and the UI details block; see
    :func:`in_band_error_text`.
    """

    if not isinstance(error, Mapping):
        return ProviderError(str(error), retryable=False)

    raw_message = error.get("message")
    message = raw_message if isinstance(raw_message, str) and raw_message else None
    message = in_band_error_text(message, error)
    metadata = error.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    code = error.get("code")
    classifier = _first_non_empty_string(metadata.get("error_type"), error.get("error_type"))
    if classifier is None and isinstance(code, str) and code:
        classifier = code
    if classifier is None:
        classifier = _first_non_empty_string(error.get("type"))
    numeric_code = code if isinstance(code, int) and not isinstance(code, bool) else None

    availability = error.get("availability")
    return classify_in_band_error_type(
        message,
        classifier=classifier,
        numeric_code=numeric_code,
        availability=availability if isinstance(availability, Mapping) else None,
        lenient_unknown=lenient_unknown,
    )


def classify_in_band_error_type(
    message: str,
    *,
    classifier: str | None,
    numeric_code: int | None = None,
    availability: Mapping[str, Any] | None = None,
    lenient_unknown: bool = False,
) -> ProviderError:
    """Map one extracted in-band error type or code into the shared taxonomy.

    ``classifier`` is the wire's typed error name (for example a Chat
    Completions ``error_type``/``code`` or a Messages ``error.type``);
    ``numeric_code`` an HTTP-like numeric code. Wires with their own payload
    shape extract these facts and share this single taxonomy, keeping their
    own ``message`` wording.
    """

    hints: Mapping[str, Any] = availability or {}

    def _build(retryable: bool) -> ProviderError:
        provider_error = ProviderError(message, retryable=retryable)
        if retryable:
            retry_after = hints.get("retry_after")
            if (
                isinstance(retry_after, int)
                and not isinstance(retry_after, bool)
                and retry_after > 0
            ):
                provider_error.retry_after = retry_after
        return provider_error

    if classifier in IN_BAND_AUTH_ERROR_CODES or numeric_code in (401, 403):
        return ProviderAuthError(message)
    # An exhausted quota arrives with status 429, yet waiting does not help.
    if classifier in QUOTA_EXHAUSTED_ERROR_CODES:
        return _build(False)
    if classifier in IN_BAND_RATE_LIMIT_ERROR_CODES or numeric_code == 429:
        return _rate_limit_error(message, hints)
    if classifier in IN_BAND_TIMEOUT_ERROR_CODES or numeric_code == 504:
        return ProviderTimeoutError(message)
    if classifier in IN_BAND_TRANSIENT_ERROR_CODES or numeric_code in {502, 503}:
        return _build(True)
    if classifier in IN_BAND_FATAL_ERROR_CODES:
        return _build(False)
    # Payment required: missing credits or a spending limit need the user, so
    # no retry or router leniency helps (OpenRouter's in-flight budget, which a
    # later attempt can pass, is classified before this).
    if numeric_code == 402:
        return _build(False)
    if hints.get("retryable") is True:
        return _build(True)
    return _build(lenient_unknown)


def _rate_limit_error(message: str, availability: Mapping[str, Any]) -> ProviderRateLimitError:
    rate_limit_error = ProviderRateLimitError(message)
    retry_after = availability.get("retry_after")
    if isinstance(retry_after, int) and not isinstance(retry_after, bool) and retry_after > 0:
        rate_limit_error.retry_after = retry_after
    return rate_limit_error


# ---------------------------------------------------------------------------
# Exhausted allowances
# ---------------------------------------------------------------------------


def exhausted_quota_code(error: Mapping[str, Any]) -> str | None:
    """The code or type by which an error object reports an exhausted quota, if any."""

    for value in (error.get("code"), error.get("type"), error.get("error_type")):
        if isinstance(value, str) and value in QUOTA_EXHAUSTED_ERROR_CODES:
            return value
    return None


def usage_limit_error(
    error: Mapping[str, Any],
    detail: str,
    *,
    reported_wait: float | None = None,
    display_timezone: tzinfo = UTC,
    now: datetime | None = None,
) -> ProviderError | None:
    """Classify a reached subscription usage limit by its reset; ``None`` for any other error.

    The reset is the error's ``resets_at`` (epoch seconds), else its
    ``resets_in_seconds``, else ``reported_wait`` (the ``Retry-After`` its
    response carried). A reset within the retry cap (``MAX_RETRY_AFTER_SECONDS``)
    becomes a ``ProviderRateLimitError`` whose ``retry_after`` waits exactly
    until then; a later or unreported reset becomes a non-retryable
    ``ProviderUsageLimitError``. The message names the reset as wall-clock time
    in ``display_timezone`` and ends with ``detail``, the Provider's evidence.
    """

    if USAGE_LIMIT_ERROR_TYPE not in (
        error.get("type"),
        error.get("code"),
        error.get("error_type"),
    ):
        return None
    current = now or datetime.now(UTC)
    resets_at = _reported_reset(error, reported_wait, current)
    if resets_at is None:
        return ProviderUsageLimitError(
            f"Subscription usage limit reached; no reset time was reported: {detail}"
        )
    wait = max(0.0, (resets_at - current).total_seconds())
    reset_text = resets_at.astimezone(display_timezone).strftime("%Y-%m-%d %H:%M %Z")
    if wait <= MAX_RETRY_AFTER_SECONDS:
        rate_limit_error = ProviderRateLimitError(
            f"Subscription usage limit reached; it resets at {reset_text}, "
            f"in {math.ceil(wait)} seconds: {detail}"
        )
        rate_limit_error.retry_after = wait
        return rate_limit_error
    return ProviderUsageLimitError(
        f"Subscription usage limit reached; it resets at {reset_text}: {detail}",
        resets_at=resets_at,
    )


def _reported_reset(
    error: Mapping[str, Any], reported_wait: float | None, now: datetime
) -> datetime | None:
    resets_at = _finite_number(error.get("resets_at"))
    if resets_at is not None:
        try:
            return datetime.fromtimestamp(resets_at, UTC)
        except OverflowError, OSError, ValueError:
            pass
    for seconds in (_finite_number(error.get("resets_in_seconds")), reported_wait):
        if seconds is not None and seconds >= 0:
            try:
                return now + timedelta(seconds=seconds)
            except OverflowError:
                return None
    return None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
