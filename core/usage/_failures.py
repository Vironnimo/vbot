"""Classify why a Model attempt failed, for the Usage ledger (``usage.md`` -> Failures).

An error states its own class (``VBotError.failure_class``); a wrapper without
one defers to its cause. An HTTP status classifies an error that states none,
and the remaining cases fall back to ``provider`` or ``internal``. Only the
class and the status are kept, never the error message, which can quote
request content.
"""

from __future__ import annotations

from typing import get_args

from core.utils.errors import FailureClass

FAILURE_CLASSES: frozenset[str] = frozenset(get_args(FailureClass.__value__))
# Deep enough for task errors that wrap a wrapped Provider error.
_MAX_CHAIN = 8


def classify_failure(
    failure: BaseException | FailureClass | None,
) -> tuple[FailureClass | None, int | None]:
    """The failure class and HTTP status of *failure*; ``(None, None)`` without one."""
    if failure is None:
        return None, None
    if isinstance(failure, str):
        if failure not in FAILURE_CLASSES:
            raise ValueError(f"Unknown failure class: {failure}")
        return failure, None
    chain = _chain(failure)
    status = next((code for error in chain if (code := _status(error)) is not None), None)
    for error in chain:
        declared = getattr(error, "failure_class", None)
        if isinstance(declared, str) and declared in FAILURE_CLASSES:
            return declared, status  # type: ignore[return-value]
    if status is not None:
        return _status_class(status), status
    if any(isinstance(error, TimeoutError) for error in chain):
        return "timeout", None
    # A ProviderError reported in-band carries no status and no subclass.
    if any(isinstance(getattr(error, "retryable", None), bool) for error in chain):
        return "provider", None
    return "internal", None


def _chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain and len(chain) < _MAX_CHAIN:
        chain.append(current)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return chain


def _status(error: BaseException) -> int | None:
    code = getattr(error, "status_code", None)
    if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599:
        return code
    return None


def _status_class(status: int) -> FailureClass:
    if status in (401, 403):
        return "auth"
    if status in (408, 504):
        return "timeout"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server"
    return "rejected"
