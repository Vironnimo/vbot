"""Recovery budgets bound each route of an unfinished Model step; route health spans Runs."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from core.chat.recovery import RecoveryBudget, RouteHealth, RoutePatience
from core.providers.errors import ProviderRateLimitError, ProviderTimeoutError
from core.runs import RunInterruptedError


@pytest.mark.asyncio
async def test_each_route_has_its_own_attempts_and_deadline(monkeypatch):
    now = [0.0]
    delays = []

    async def sleep(delay):
        delays.append(delay)
        now[0] += delay

    monkeypatch.setattr("core.chat.recovery._sleep", sleep)
    monkeypatch.setattr("core.utils.retry.random.uniform", lambda low, high: high)
    budget = RecoveryBudget(clock=lambda: now[0])
    for target in ("primary", "backup"):
        started = now[0]
        for _ in range(9):
            await budget.begin(target, lambda notice: None)
            now[0] += 150  # A slow failed request: two routes outlast one deadline.
            budget.failed(ProviderRateLimitError("unavailable"), target)
        assert not budget.available(target)
        assert budget.deadline(target) == started + 150 + 1800
    assert delays == [1.5, 3, 6, 12, 24, 48, 96, 192] * 2
    assert now[0] > budget.deadline("primary")

    budget.complete("backup")
    assert budget.completed("backup") and not budget.completed("primary")
    assert budget.available("primary") and budget.deadline("primary") is None


@pytest.mark.asyncio
async def test_retry_after_applies_to_the_route_and_is_observable(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("core.chat.recovery._sleep", sleep)
    notices = []
    budget = RecoveryBudget()
    await budget.begin("primary", notices.append)
    error = ProviderRateLimitError("limited")
    error.retry_after = 60
    budget.failed(error, "primary")
    await budget.begin("primary", notices.append)

    assert sleep.await_count == 1
    assert 60 <= sleep.await_args.args[0] <= 60.5
    assert [notice.waiting for notice in notices] == [True, False]
    assert notices[0].attempt == 2
    assert notices[0].max_attempts == 9


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fallback_follows", "failed_recently", "attempts"),
    [(False, False, 9), (False, True, 9), (True, False, 7), (True, True, 2)],
    ids=["last-route", "last-route-failed-before", "fallback-follows", "probe"],
)
async def test_route_patience_sets_attempts(
    monkeypatch, fallback_follows, failed_recently, attempts
):
    monkeypatch.setattr("core.chat.recovery.compute_retry_delay", lambda *a, **kw: (0, False))
    budget = RecoveryBudget()
    budget.serve(
        "current",
        RoutePatience.for_route(fallback_follows=fallback_follows, failed_recently=failed_recently),
    )
    notices = []
    for _ in range(attempts):
        await budget.begin("current", notices.append)
        budget.failed(ProviderRateLimitError("limited"), "current")
    with pytest.raises(RunInterruptedError):
        await budget.begin("current", notices.append)
    assert [(notice.attempt, notice.max_attempts, notice.waiting) for notice in notices] == [
        (attempt, attempts, waiting)
        for attempt in range(2, attempts + 1)
        for waiting in (True, False)
    ]


@pytest.mark.parametrize(("fallback_follows", "available"), [(True, False), (False, True)])
def test_stalls_end_a_route_only_when_a_fallback_follows(fallback_follows, available):
    budget = RecoveryBudget()
    budget.serve(
        "current", RoutePatience.for_route(fallback_follows=fallback_follows, failed_recently=False)
    )
    for _ in range(2):
        budget.failed(ProviderTimeoutError("stalled"), "current")
    assert budget.available("current") is available


@pytest.mark.asyncio
async def test_no_retry_starts_when_wait_would_exceed_remaining_budget(monkeypatch):
    now = [0.0]
    sleep = AsyncMock()
    monkeypatch.setattr("core.chat.recovery._sleep", sleep)
    budget = RecoveryBudget(clock=lambda: now[0])
    await budget.begin("primary", lambda notice: None)
    error = ProviderRateLimitError("limited")
    error.retry_after = 60
    budget.failed(error, "primary")
    now[0] = 1790
    with pytest.raises(RunInterruptedError):
        await budget.begin("primary", lambda notice: None)
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_backoff_is_cancellable_without_spending_an_attempt(monkeypatch):
    async def endless_backoff(delay):
        await asyncio.Event().wait()

    monkeypatch.setattr("core.chat.recovery._sleep", endless_backoff)
    waiting = asyncio.Event()
    budget = RecoveryBudget()
    budget.serve("primary", RoutePatience(attempts=2))
    await budget.begin("primary", lambda notice: None)
    error = ProviderRateLimitError("limited")
    error.retry_after = 60
    budget.failed(error, "primary")
    task = asyncio.create_task(budget.begin("primary", lambda notice: waiting.set()))
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert budget.available("primary")


def test_route_health_pauses_a_failed_route_and_doubles_each_repeat():
    now = [1000.0]
    health = RouteHealth(clock=lambda: now[0])
    pauses = []
    for _ in range(4):
        pauses.append(health.failed("route") - now[0])
    assert pauses == [900, 1800, 3600, 3600]
    assert health.retry_at("route") == now[0] + 3600

    now[0] += 3600
    assert health.retry_at("route") is None
    assert health.failed_recently("route")
    health.recovered("route")
    assert not health.failed_recently("route")
    assert health.failed("route") == now[0] + 900
