"""Canonical policy for which HTTP status codes are worth retrying.

Single source of truth shared by the provider adapters and the HTTP-calling
tools, so "retryable" means the same thing everywhere instead of drifting per
call site. Decided once, here:

- **429 / 502 / 503 — always retryable (any method).** The server explicitly
  refused or could not serve the request, so it was not acted on; re-issuing is
  safe regardless of HTTP method.
- **504 (Gateway Timeout) — always retryable.** A timeout at the gateway is
  transient. The origin *may* have begun processing, but for the requests vBot
  makes (LLM completions, web fetches, Home Assistant calls) re-issuing is
  acceptable.
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

from collections.abc import Collection

# Retryable regardless of HTTP method — the request was demonstrably not acted on.
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
