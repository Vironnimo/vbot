"""Bounded worker pools and ordered workers for blocking in-process work.

``asyncio.to_thread`` protects the Event Loop, but every call shares the loop's
default executor and submits work before any application-level backpressure can
apply.  This module owns the stronger cross-domain boundary: a named dedicated
executor, a per-Event-Loop admission limit, and cancellation that does not report
completion while an already-started worker is still mutating process state.

``OrderedWorker`` is the single-thread variant for owners whose operations must
run in exactly the order the Event Loop submitted them, such as full-document
snapshots written by a scheduler and by operator edits.

Every pool records its admission wait and run durations
(``worker_pool.<name>.wait`` / ``.run``) and its ``.active`` / ``.waiting``
gauges; an ordered worker records ``worker_pool.<name>.run``. During a
performance recording they also appear on the ``worker pool <name>`` track.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import weakref
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from time import perf_counter
from typing import Any, TypeVar

from core.performance.performance import measure, record_span, set_gauge

_WorkerResult = TypeVar("_WorkerResult")
_OrderedResult = TypeVar("_OrderedResult")
# Admission waits shorter than this stay histogram-only in recordings.
_WAIT_SPAN_MIN_MS = 1.0


class BoundedWorkerPool:
    """Run blocking callables in one dedicated, backpressured executor.

    The semaphore is loop-local because asyncio synchronization primitives must
    never be shared across loops.  The executor is process-wide for this pool and
    may safely serve those loops; ``ThreadPoolExecutor`` creates its threads lazily.
    """

    def __init__(self, *, name: str, max_workers: int) -> None:
        if not name:
            raise ValueError("Worker pool name must be non-empty")
        if max_workers < 1:
            raise ValueError("Worker pool max_workers must be at least 1")
        self._max_workers = max_workers
        self._track = f"worker pool {name}"
        self._wait_metric = f"worker_pool.{name}.wait"
        self._run_metric = f"worker_pool.{name}.run"
        self._active_gauge = f"worker_pool.{name}.active"
        self._waiting_gauge = f"worker_pool.{name}.waiting"
        self._counts_lock = threading.Lock()
        self._active = 0
        self._waiting = 0
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"vbot-{name}",
        )
        self._semaphores: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Semaphore
        ] = weakref.WeakKeyDictionary()

    @property
    def max_workers(self) -> int:
        """Return the maximum number of admitted worker calls."""
        return self._max_workers

    async def run(
        self,
        function: Callable[..., _WorkerResult],
        *arguments: Any,
        **keyword_arguments: Any,
    ) -> _WorkerResult:
        """Run one callable without blocking the Event Loop.

        Cancellation while waiting for admission starts no worker.  Once the
        callable has started, cancellation is deferred until it settles.  Python
        cannot stop a worker thread safely; waiting keeps the semaphore honest and
        prevents callers from treating an in-flight mutation as abandoned.
        """
        semaphore = self._semaphore()
        entered = perf_counter()
        self._count_waiting(1)
        try:
            await semaphore.acquire()
        finally:
            self._count_waiting(-1)
        try:
            record_span(
                self._wait_metric,
                entered,
                track=self._track,
                name="wait",
                min_span_ms=_WAIT_SPAN_MIN_MS,
            )
            self._count_active(1)
            try:
                with measure(self._run_metric, track=self._track, name=_callable_name(function)):
                    return await self._run_admitted(function, arguments, keyword_arguments)
            finally:
                self._count_active(-1)
        finally:
            semaphore.release()

    async def _run_admitted(
        self,
        function: Callable[..., _WorkerResult],
        arguments: tuple[Any, ...],
        keyword_arguments: dict[str, Any],
    ) -> _WorkerResult:
        loop = asyncio.get_running_loop()
        call = partial(function, *arguments, **keyword_arguments)
        future = loop.run_in_executor(self._executor, call)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError as cancellation:
            # Cancellation must win over a late worker failure, but only after
            # the worker has actually settled.  A caller may cancel the Task
            # more than once, so keep shielding until the executor Future is
            # done instead of allowing a repeated cancellation to release the
            # semaphore early.
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if future.done() and not future.cancelled():
                with contextlib.suppress(BaseException):
                    future.exception()
            raise cancellation

    def _count_waiting(self, delta: int) -> None:
        with self._counts_lock:
            self._waiting += delta
            waiting = self._waiting
        set_gauge(self._waiting_gauge, waiting, track=self._track)

    def _count_active(self, delta: int) -> None:
        with self._counts_lock:
            self._active += delta
            active = self._active
        set_gauge(self._active_gauge, active, track=self._track)

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        semaphore = self._semaphores.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._max_workers)
            self._semaphores[loop] = semaphore
        return semaphore

    def shutdown(self, *, wait: bool = True) -> None:
        """Reject new submissions and release an owner's executor on shutdown."""
        self._executor.shutdown(wait=wait, cancel_futures=True)


class OrderedWorker:
    """Run blocking callables on one dedicated thread, strictly in submission order.

    Submission order is the order of the ``call``, ``call_async`` and ``hand_off``
    calls, so an owner that snapshots its state and submits the write in one
    synchronous step gets its snapshots on disk in the order it took them, from
    the Event Loop and from synchronous callers alike.

    A submitted operation always runs. Cancelling a ``call_async`` caller defers
    the cancellation until its operation has settled, as ``BoundedWorkerPool``
    does, so shutdown never abandons a half-finished write. ``drain`` waits for
    every operation submitted so far.
    """

    def __init__(self, *, name: str) -> None:
        if not name:
            raise ValueError("Ordered worker name must be non-empty")
        self._track = f"worker pool {name}"
        self._run_metric = f"worker_pool.{name}.run"
        self._local = threading.local()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"vbot-{name}",
            initializer=self._mark_worker_thread,
        )
        self._hand_offs = 0
        self._hand_offs_lock = threading.Lock()

    def call(self, operation: Callable[[], _OrderedResult]) -> _OrderedResult:
        """Run *operation* after all earlier work and wait for it, blocking the caller.

        For synchronous callers only; on the worker thread itself it runs inline.
        """
        if getattr(self._local, "is_worker_thread", False):
            return operation()
        return self._submit(operation).result()

    async def call_async(self, operation: Callable[[], _OrderedResult]) -> _OrderedResult:
        """Run *operation* after all earlier work without blocking the Event Loop."""
        future = asyncio.wrap_future(self._submit(operation))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError as cancellation:
            # Wait for the operation even when the caller is cancelled repeatedly;
            # cancellation still wins over a late failure once it has settled.
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if future.done() and not future.cancelled():
                with contextlib.suppress(BaseException):
                    future.exception()
            raise cancellation

    def hand_off(self, operation: Callable[[], None], *, limit: int) -> bool:
        """Queue *operation* without waiting; ``False`` when *limit* hand-offs are pending.

        The operation must handle its own failures: nobody observes its result.
        """
        with self._hand_offs_lock:
            if self._hand_offs >= limit:
                return False
            self._hand_offs += 1
        try:
            future = self._submit(operation)
        except BaseException:
            self._settle_hand_off()
            raise
        future.add_done_callback(lambda _future: self._settle_hand_off())
        return True

    async def drain(self) -> None:
        """Wait until every operation submitted so far has finished."""
        await self.call_async(_nothing)

    def _submit(self, operation: Callable[[], _OrderedResult]) -> Future[_OrderedResult]:
        return self._executor.submit(self._run_measured, operation)

    def _run_measured(self, operation: Callable[[], _OrderedResult]) -> _OrderedResult:
        with measure(self._run_metric, track=self._track, name=_callable_name(operation)):
            return operation()

    def _settle_hand_off(self) -> None:
        with self._hand_offs_lock:
            self._hand_offs -= 1

    def _mark_worker_thread(self) -> None:
        self._local.is_worker_thread = True


def _nothing() -> None:
    return None


def _callable_name(function: Callable[..., Any]) -> str:
    """Return a code identifier for span names; never arguments or content."""
    while isinstance(function, partial):
        function = function.func
    return getattr(function, "__qualname__", None) or type(function).__name__
