"""Canonical default policy for which HTTP status codes are worth retrying.

Single source of truth shared by the provider adapters and the HTTP-calling
tools, so "retryable" means the same thing everywhere instead of drifting per
call site. This policy applies to replay-safe requests and to callers whose
contract accepts the repeat risk. Billed, non-idempotent task generation adds a
stricter endpoint policy in ``core.providers.task_client`` because gateway
failures can arrive after the provider has started the operation.

- **429 / 502 / 503 — retryable by the default policy.** These usually mean the
  server refused or could not serve the request.
- **504 (Gateway Timeout) — retryable by the default policy.** The origin *may*
  have begun processing, so non-idempotent task generation must not inherit
  this decision without a stronger endpoint guarantee.
- **500 (Internal Server Error) — retryable only for idempotent requests**
  (GET/HEAD). On a non-idempotent POST the origin may have already applied the
  request, and a 500 is often deterministic, so retrying risks duplicate work
  for no gain.

Callers pass ``idempotent`` to declare whether their request is safe to repeat,
plus any provider-specific ``extra`` codes (e.g. Anthropic's 529 "overloaded").

The desktop wakeword worker keeps its own copy of the always-retryable set: it
runs across the desktop/server boundary and must not import from ``core`` (see
``.vorch/PROJECT.md`` → architecture).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

# Retryable under the shared default; stricter callers may suppress ambiguous replays.
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 502, 503, 504})

# Additionally retryable only for idempotent requests (safe to repeat).
IDEMPOTENT_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({500})


def is_retryable_status(
    status_code: int,
    *,
    idempotent: bool,
    extra: Collection[int] | None = None,
) -> bool:
    """Return whether *status_code* should be retried under the shared policy.

    Args:
        status_code: HTTP response status code.
        idempotent: Whether the request is safe to repeat (GET/HEAD and other
            side-effect-free calls). When True, idempotent-only codes (500) are
            retryable in addition to the method-agnostic set.
        extra: Provider-specific status codes to treat as retryable in addition
            to the standard set (e.g. ``{529}`` for Anthropic's overloaded error).

    Returns:
        True if the status code is retryable, False otherwise.
    """
    if status_code in RETRYABLE_STATUS_CODES:
        return True
    if extra is not None and status_code in extra:
        return True
    return idempotent and status_code in IDEMPOTENT_RETRYABLE_STATUS_CODES


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Parse a server's requested retry delay from response headers.

    On 429/503 (and similar) servers commonly signal how long to wait before
    retrying. Honored forms, in priority order:

    1. ``retry-after-ms`` — a millisecond hint some providers send (e.g. OpenAI).
    2. ``Retry-After`` as ``delay-seconds`` — a non-negative number of seconds
       (RFC 9110).
    3. ``Retry-After`` as an ``HTTP-date`` — converted to seconds from now.

    Args:
        headers: The response headers. Lookups use lowercase keys, so the
            mapping must either be case-insensitive (``httpx.Headers``) or
            already lowercase-keyed.

    Returns:
        The delay in seconds, clamped to ``>= 0``, or ``None`` when no usable
        hint is present or the value cannot be parsed (a malformed header is
        ignored rather than treated as an error).
    """
    milliseconds = headers.get("retry-after-ms")
    if milliseconds is not None:
        try:
            from_ms = float(milliseconds) / 1000.0
        except ValueError:
            from_ms = None
        if from_ms is not None and from_ms >= 0:
            return from_ms

    raw = headers.get("retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None

    # delay-seconds form: a plain (non-negative) number of seconds.
    try:
        seconds = float(raw)
    except ValueError:
        seconds = None
    if seconds is not None:
        return seconds if seconds >= 0 else None

    # HTTP-date form: seconds from now, never negative (a past date means "now").
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if retry_at is None:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    delta = (retry_at - datetime.now(UTC)).total_seconds()
    return delta if delta > 0 else 0.0


@dataclass(frozen=True)
class HttpRequestFailure:
    """A classified failure from an HTTP tool's internal request loop.

    Threads the retry decision from a tool's request helper (which swallows the
    underlying ``httpx`` error and returns a string) up to its handler, so the
    tool result envelope can carry ``retryable``/``attempts_made`` without the
    handler having to re-classify by inspecting the message text. ``retryable``
    is True only when the tool gave up on a retryable status or transport error
    after exhausting its own retries; ``attempts_made`` then records how many
    attempts it made. Validation/fatal failures use ``retryable=False`` and no
    attempt count.
    """

    message: str
    retryable: bool = False
    attempts_made: int | None = None
