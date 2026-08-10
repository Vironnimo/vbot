from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future

import pytest

from core.utils.workers import BoundedWorkerPool


@pytest.mark.asyncio
async def test_worker_pool_applies_backpressure_before_submission() -> None:
    pool = BoundedWorkerPool(name="test-backpressure", max_workers=1)
    release_first = threading.Event()
    first_started: Future[None] = Future()
    second_started = threading.Event()

    def first() -> str:
        first_started.set_result(None)
        release_first.wait(timeout=5)
        return "first"

    def second() -> str:
        second_started.set()
        return "second"

    first_task = asyncio.create_task(pool.run(first))
    await asyncio.wrap_future(first_started)
    second_task = asyncio.create_task(pool.run(second))
    await asyncio.sleep(0)

    assert second_started.is_set() is False

    release_first.set()
    assert await first_task == "first"
    assert await second_task == "second"


@pytest.mark.asyncio
async def test_worker_pool_waits_for_started_mutation_before_cancellation() -> None:
    pool = BoundedWorkerPool(name="test-cancellation", max_workers=1)
    started = threading.Event()
    release = threading.Event()
    mutation_finished = threading.Event()

    def mutate() -> None:
        started.set()
        release.wait(timeout=5)
        mutation_finished.set()

    task = asyncio.create_task(pool.run(mutate))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)

    assert task.done() is False
    assert mutation_finished.is_set() is False

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mutation_finished.is_set() is True


@pytest.mark.asyncio
async def test_worker_pool_keeps_cancellation_authoritative_after_worker_failure() -> None:
    pool = BoundedWorkerPool(name="test-cancelled-failure", max_workers=1)
    started = threading.Event()
    release = threading.Event()

    def fail_after_release() -> None:
        started.set()
        release.wait(timeout=5)
        raise RuntimeError("late worker failure")

    task = asyncio.create_task(pool.run(fail_after_release))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)

    assert task.done() is False

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
