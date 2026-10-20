"""Chat-owned recovery budget for retries, continuations and Model switches.

It also classifies the failure that interrupts a Run and remembers, for the
process, the Model routes whose recovery recently failed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from core.chat.messages import InterruptionCause
from core.providers.errors import NetworkError, ProviderError, ProviderTimeoutError
from core.runs import RunInterruptedError
from core.utils.retry import RetryNotice, compute_retry_delay

# Attempts of one Model step on one route, the initial request included: the
# last route of a Run waits longest, a route with a fallback behind it hands
# over sooner, and a route that failed recently gets only a short probe.
MAX_TARGET_ATTEMPTS = 9
FALLBACK_TARGET_ATTEMPTS = 7
PROBE_TARGET_ATTEMPTS = 2
# Stalls and timeouts cost minutes each, so a route with a fallback behind it
# hands over after this many.
FALLBACK_STALL_LIMIT = 2
RECOVERY_TIMEOUT_SECONDS = 1800.0
# New Runs pass over a route whose recovery failed for this long; each further
# failure before it recovers doubles the pause, up to the maximum.
ROUTE_COOLDOWN_SECONDS = 900.0
MAX_ROUTE_COOLDOWN_SECONDS = 3600.0

# Tests patch these seams instead of the process-wide ``asyncio.sleep`` and clock.
_sleep = asyncio.sleep
_wall_clock = time.time


class IncompleteResponseError(ProviderError):
    """The Model finished without completing a usable answer or Tool step."""

    failure_class = "incomplete"

    def __init__(self, message: str = "The Model response was incomplete") -> None:
        super().__init__(message, retryable=True)


def normalize_interruption_cause(error: BaseException | None) -> InterruptionCause:
    """Classify the failure that ended an unfinished Model step."""
    if isinstance(error, ProviderTimeoutError) or (
        error is not None
        and error.__class__.__name__
        in {"StreamingChunkTimeoutError", "StreamingProgressTimeoutError"}
    ):
        return "timeout"
    if isinstance(error, NetworkError):
        return "network"
    if isinstance(error, ProviderError):
        return "provider"
    return "internal"


@dataclass(frozen=True)
class RoutePatience:
    """How long one route's Model step may recover before the Run moves on."""

    attempts: int = MAX_TARGET_ATTEMPTS
    # Stalls and timeouts after which the route gives up; ``None`` leaves
    # them to the attempt limit.
    stalls: int | None = None
    # Whether another route can take over; rate limits then skip retries.
    fallback_follows: bool = False

    @classmethod
    def for_route(cls, *, fallback_follows: bool, failed_recently: bool) -> RoutePatience:
        if not fallback_follows:
            return cls()
        return cls(
            attempts=PROBE_TARGET_ATTEMPTS if failed_recently else FALLBACK_TARGET_ATTEMPTS,
            stalls=FALLBACK_STALL_LIMIT,
            fallback_follows=True,
        )


@dataclass
class _RouteRecovery:
    attempts: int = 0
    stalls: int = 0
    deadline: float | None = None


@dataclass
class RecoveryBudget:
    """Bound one unfinished Model step on each route that serves it.

    Every route has its own attempts and its own thirty-minute deadline, which
    begins at that route's first failure, so a slow failure on one route never
    uses up the next route's chance. A complete Model response (final answer or
    Tool-call turn) resets the budget for the next step. Partial text,
    Reasoning, truncated turns, transport heartbeats and Model switches do not.
    """

    clock: Callable[[], float] = time.monotonic
    last_error: Exception | None = None
    failed_target: str | None = None
    _routes: dict[str, _RouteRecovery] = field(default_factory=dict)
    _patience: dict[str, RoutePatience] = field(default_factory=dict)
    _completed: set[str] = field(default_factory=set)

    def serve(self, target: str, patience: RoutePatience) -> None:
        """Set the patience of ``target`` for the rest of the Run."""
        self._patience[target] = patience

    def patience(self, target: str) -> RoutePatience:
        return self._patience.get(target, RoutePatience())

    def completed(self, target: str) -> bool:
        """Whether ``target`` completed a Model response during this Run."""
        return target in self._completed

    def deadline(self, target: str) -> float | None:
        route = self._routes.get(target)
        return route.deadline if route is not None else None

    def available(self, target: str) -> bool:
        route = self._routes.get(target, _RouteRecovery())
        patience = self.patience(target)
        return (
            route.attempts < patience.attempts
            and (patience.stalls is None or route.stalls < patience.stalls)
            and (route.deadline is None or self.clock() < route.deadline)
        )

    def failed(self, error: Exception, target: str) -> None:
        route = self._routes.setdefault(target, _RouteRecovery())
        if route.deadline is None:
            route.deadline = self.clock() + RECOVERY_TIMEOUT_SECONDS
        if normalize_interruption_cause(error) == "timeout":
            route.stalls += 1
        self.last_error = error
        self.failed_target = target

    def exhausted(self, *, result: object = None) -> RunInterruptedError:
        return RunInterruptedError(normalize_interruption_cause(self.last_error), result=result)

    async def begin(self, target: str, notify: Callable[[RetryNotice], None]) -> None:
        if not self.available(target):
            raise self.exhausted() from self.last_error
        route = self._routes.setdefault(target, _RouteRecovery())
        if self.last_error is not None and self.failed_target == target:
            hint = getattr(self.last_error, "retry_after", None)
            delay, _ = compute_retry_delay(
                max(0, route.attempts - 1),
                retry_after=hint if isinstance(hint, (int, float)) else None,
            )
            if route.deadline is not None and self.clock() + delay >= route.deadline:
                raise self.exhausted() from self.last_error
            # Status describes this Model route, not attempts on other routes.
            attempt = route.attempts + 1
            max_attempts = self.patience(target).attempts
            notify(RetryNotice(self.last_error, attempt, max_attempts, delay, True))
            await _sleep(delay)
            if not self.available(target):
                raise self.exhausted() from self.last_error
            notify(RetryNotice(self.last_error, attempt, max_attempts, 0, False))
        route.attempts += 1

    def complete(self, target: str) -> None:
        """End the step: ``target`` answered, and the next step starts afresh."""
        self._completed.add(target)
        self._routes.clear()
        self.last_error = None
        self.failed_target = None


@dataclass
class _RouteFailures:
    failures: int
    retry_at: float


class RouteHealth:
    """Remember, for the process, the Model routes whose recovery recently failed.

    A route is the exact Provider, Connection, account and Model of a request.
    New Runs pass over a route while it cools down and give it only a short
    probe afterwards, until it completes a Model response again.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock if clock is not None else lambda: _wall_clock()
        self._routes: dict[str, _RouteFailures] = {}

    def failed(self, route: str) -> float:
        """Record that ``route`` gave up; return when new Runs try it again."""
        record = self._routes.get(route)
        failures = (record.failures if record is not None else 0) + 1
        pause = min(ROUTE_COOLDOWN_SECONDS * 2.0 ** (failures - 1), MAX_ROUTE_COOLDOWN_SECONDS)
        retry_at = self._clock() + pause
        self._routes[route] = _RouteFailures(failures, retry_at)
        return retry_at

    def recovered(self, route: str) -> None:
        self._routes.pop(route, None)

    def retry_at(self, route: str) -> float | None:
        """When new Runs try ``route`` again, or ``None`` when it is not cooling down."""
        record = self._routes.get(route)
        if record is None or record.retry_at <= self._clock():
            return None
        return record.retry_at

    def failed_recently(self, route: str) -> bool:
        """Whether ``route`` gave up and has not completed a response since."""
        return route in self._routes
