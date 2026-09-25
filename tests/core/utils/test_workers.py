from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from concurrent.futures import Future
from pathlib import Path

import pytest

from core.performance import PerformanceService
from core.performance.performance import reset_for_tests
from core.utils.workers import BoundedWorkerPool, OrderedWorker


@pytest.fixture
def performance(tmp_path: Path) -> Iterator[PerformanceService]:
    reset_for_tests()
    yield PerformanceService(tmp_path / "performance")
    reset_for_tests()


@pytest.mark.asyncio
async def test_owned_worker_pool_shutdown_rejects_new_work() -> None:
    pool = BoundedWorkerPool(name="test-shutdown", max_workers=1)
    assert await pool.run(lambda: 7) == 7
    pool.shutdown()
    pool.shutdown()
    with pytest.raises(RuntimeError, match="shutdown"):
        await pool.run(lambda: 8)


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


@pytest.mark.asyncio
async def test_worker_pool_records_admission_wait_run_time_and_occupancy(
    performance: PerformanceService,
) -> None:
    pool = BoundedWorkerPool(name="test-metrics", max_workers=1)
    release_first = threading.Event()
    first_started: Future[None] = Future()

    def hold_first_slot() -> str:
        first_started.set_result(None)
        release_first.wait(timeout=5)
        return "first"

    performance.start_recording()
    first_task = asyncio.create_task(pool.run(hold_first_slot))
    await asyncio.wrap_future(first_started)
    second_task = asyncio.create_task(pool.run(lambda: "second"))
    await asyncio.sleep(0.05)
    occupied = (await performance.snapshot())["gauges"]

    release_first.set()
    assert await first_task == "first"
    assert await second_task == "second"
    result = await performance.stop_recording()
    snapshot = await performance.snapshot()

    assert occupied["worker_pool.test-metrics.active"] == 1
    assert occupied["worker_pool.test-metrics.waiting"] == 1
    assert snapshot["gauges"]["worker_pool.test-metrics.active"] == 0
    assert snapshot["gauges"]["worker_pool.test-metrics.waiting"] == 0
    assert snapshot["metrics"]["worker_pool.test-metrics.wait"]["count"] == 2
    assert snapshot["metrics"]["worker_pool.test-metrics.run"]["count"] == 2
    assert snapshot["metrics"]["worker_pool.test-metrics.wait"]["max_ms"] >= 25
    events = json.loads(Path(result["trace_path"]).read_text(encoding="utf-8"))["traceEvents"]
    track_pid = next(
        event["pid"]
        for event in events
        if event["name"] == "process_name" and event["args"]["name"] == "worker pool test-metrics"
    )
    spans = [event for event in events if event["ph"] == "X" and event["pid"] == track_pid]
    names = sorted(span["name"] for span in spans)
    # Only the second call waited long enough for a wait span.
    assert names == [
        "test_worker_pool_records_admission_wait_run_time_and_occupancy.<locals>.<lambda>",
        "test_worker_pool_records_admission_wait_run_time_and_occupancy.<locals>.hold_first_slot",
        "wait",
    ]
    assert {span["cat"] for span in spans} == {"worker_pool"}


@pytest.mark.asyncio
async def test_ordered_worker_runs_operations_in_submission_order() -> None:
    worker = OrderedWorker(name="test-ordered")
    first_started = threading.Event()
    release_first = threading.Event()
    ran: list[str] = []

    def first() -> str:
        first_started.set()
        release_first.wait(timeout=5)
        ran.append("first")
        return "first"

    try:
        first_task = asyncio.create_task(worker.call_async(first))
        assert await asyncio.to_thread(first_started.wait, 5)
        assert worker.hand_off(lambda: ran.append("handed off"), limit=1) is True
        assert worker.hand_off(lambda: ran.append("over the limit"), limit=1) is False
        third_task = asyncio.create_task(worker.call_async(lambda: ran.append("third")))
        await asyncio.sleep(0.01)
        assert ran == []
    finally:
        release_first.set()

    assert await first_task == "first"
    await third_task
    await worker.drain()
    assert ran == ["first", "handed off", "third"]


@pytest.mark.asyncio
async def test_ordered_worker_runs_a_cancelled_callers_operation_before_cancelling() -> None:
    worker = OrderedWorker(name="test-ordered-cancel")
    first_started = threading.Event()
    release_first = threading.Event()
    written: list[str] = []

    def first() -> None:
        first_started.set()
        release_first.wait(timeout=5)

    try:
        first_task = asyncio.create_task(worker.call_async(first))
        assert await asyncio.to_thread(first_started.wait, 5)
        queued = asyncio.create_task(worker.call_async(lambda: written.append("snapshot")))
        await asyncio.sleep(0)
        queued.cancel()
        queued.cancel()
        await asyncio.sleep(0.01)
        assert queued.done() is False
    finally:
        release_first.set()

    await first_task
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert written == ["snapshot"]


@pytest.mark.asyncio
async def test_ordered_worker_call_runs_inline_on_its_own_thread() -> None:
    worker = OrderedWorker(name="test-ordered-nested")

    def outer() -> tuple[str, str]:
        inner_thread = worker.call(lambda: threading.current_thread().name)
        return threading.current_thread().name, inner_thread

    outer_thread, inner_thread = await worker.call_async(outer)

    assert outer_thread == inner_thread
    assert outer_thread.startswith("vbot-test-ordered-nested")
