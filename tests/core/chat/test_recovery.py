"""One budget must cover every way an unfinished Model step is retried."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from core.chat.recovery import RecoveryBudget
from core.chat.streaming import StreamingProgressTimeoutError, iter_with_chunk_timeout
from core.providers.errors import ProviderRateLimitError
from core.runs import RunInterruptedError


@pytest.mark.asyncio
async def test_retry_after_applies_to_shared_budget_and_is_observable(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("core.chat.recovery.asyncio.sleep", sleep)
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


@pytest.mark.asyncio
async def test_switches_do_not_reset_total_budget_or_deadline(monkeypatch):
    now = [10.0]
    monkeypatch.setattr("core.chat.recovery.compute_retry_delay", lambda *a, **kw: (0, False))
    budget = RecoveryBudget(clock=lambda: now[0])
    for target in ("primary", "primary", "primary", "backup", "backup", "backup"):
        await budget.begin(target, lambda notice: None)
        budget.failed(ProviderRateLimitError("limited"), target)
        now[0] += 1
    assert not budget.available()
    assert budget.deadline == 310
    with pytest.raises(RunInterruptedError):
        await budget.begin("third", lambda notice: None)
    budget.reset()
    assert budget.available("primary")
    assert budget.deadline is None


@pytest.mark.asyncio
async def test_no_retry_starts_when_wait_would_exceed_remaining_budget(monkeypatch):
    now = [0.0]
    sleep = AsyncMock()
    monkeypatch.setattr("core.chat.recovery.asyncio.sleep", sleep)
    budget = RecoveryBudget(clock=lambda: now[0])
    await budget.begin("primary", lambda notice: None)
    error = ProviderRateLimitError("limited")
    error.retry_after = 60
    budget.failed(error, "primary")
    now[0] = 290
    with pytest.raises(RunInterruptedError):
        await budget.begin("primary", lambda notice: None)
    sleep.assert_not_awaited()
    assert budget.attempts == 1


@pytest.mark.asyncio
async def test_recovery_deadline_stops_fresh_deltas_and_closes_stream():
    closed = asyncio.Event()

    async def progressing():
        try:
            while True:
                yield {"type": "content_delta", "text": "x"}
                await asyncio.sleep(0.005)
        finally:
            closed.set()

    deltas = []
    with pytest.raises(StreamingProgressTimeoutError, match="recovery time budget"):
        async for delta in iter_with_chunk_timeout(
            progressing(),
            timeout_seconds=None,
            progress_timeout_seconds=None,
            deadline=time.monotonic() + 0.1,
        ):
            deltas.append(delta)
    assert deltas
    assert closed.is_set()


@pytest.mark.asyncio
async def test_backoff_is_cancellable_without_spending_an_attempt():
    waiting = asyncio.Event()
    budget = RecoveryBudget()
    await budget.begin("primary", lambda notice: None)
    error = ProviderRateLimitError("limited")
    error.retry_after = 60
    budget.failed(error, "primary")
    task = asyncio.create_task(budget.begin("primary", lambda notice: waiting.set()))
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert budget.attempts == 1
