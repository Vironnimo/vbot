"""Chat-owned recovery budget shared by retries, continuations and Model switches."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from core.chat.continuation import normalize_interruption_cause
from core.providers.errors import ProviderError
from core.runs import RunInterruptedError
from core.utils.retry import RetryNotice, compute_retry_delay

MAX_TARGET_ATTEMPTS = 3
MAX_RECOVERY_ATTEMPTS = 6
RECOVERY_TIMEOUT_SECONDS = 300.0


class IncompleteResponseError(ProviderError):
    """The Model finished without completing a usable answer or Tool step."""

    def __init__(self, message: str = "The Model response was incomplete") -> None:
        super().__init__(message, retryable=True)


@dataclass
class RecoveryBudget:
    """Bound one unfinished Model step, including its configured fallback routes.

    Successful Tool boundaries reset the budget. Partial text, Reasoning,
    transport heartbeats and Model switches do not. The five-minute deadline
    begins at the first failure, leaving healthy initial generation unaffected.
    """

    clock: Callable[[], float] = time.monotonic
    attempts: int = 0
    target_attempts: dict[str, int] = field(default_factory=dict)
    deadline: float | None = None
    last_error: Exception | None = None
    failed_target: str | None = None

    def available(self, target: str | None = None) -> bool:
        return (
            self.attempts < MAX_RECOVERY_ATTEMPTS
            and (target is None or self.target_attempts.get(target, 0) < MAX_TARGET_ATTEMPTS)
            and (self.deadline is None or self.clock() < self.deadline)
        )

    def failed(self, error: Exception, target: str) -> None:
        if self.deadline is None:
            self.deadline = self.clock() + RECOVERY_TIMEOUT_SECONDS
        self.last_error = error
        self.failed_target = target

    def exhausted(self, *, result: object = None) -> RunInterruptedError:
        return RunInterruptedError(normalize_interruption_cause(self.last_error), result=result)

    async def begin(self, target: str, notify: Callable[[RetryNotice], None]) -> None:
        if not self.available(target):
            raise self.exhausted() from self.last_error
        if self.last_error is not None and self.failed_target == target:
            hint = getattr(self.last_error, "retry_after", None)
            delay, _ = compute_retry_delay(
                max(0, self.attempts - 1),
                retry_after=hint if isinstance(hint, (int, float)) else None,
            )
            if self.deadline is not None and self.clock() + delay >= self.deadline:
                raise self.exhausted() from self.last_error
            # Status describes this Model route, not hypothetical attempts on
            # fallback routes. The total budget can further narrow its ceiling.
            target_attempts = self.target_attempts.get(target, 0)
            attempt = target_attempts + 1
            max_attempts = min(
                MAX_TARGET_ATTEMPTS,
                target_attempts + MAX_RECOVERY_ATTEMPTS - self.attempts,
            )
            notice = RetryNotice(self.last_error, attempt, max_attempts, delay, True)
            notify(notice)
            await asyncio.sleep(delay)
            if not self.available(target):
                raise self.exhausted() from self.last_error
            notify(RetryNotice(self.last_error, attempt, max_attempts, 0, False))
        self.attempts += 1
        self.target_attempts[target] = self.target_attempts.get(target, 0) + 1

    def reset(self) -> None:
        self.attempts = 0
        self.target_attempts.clear()
        self.deadline = None
        self.last_error = None
        self.failed_target = None
