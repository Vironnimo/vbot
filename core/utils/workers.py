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
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from time import perf_counter
from typing import Any

from core.performance.performance import measure, record_span, set_gauge
from core.utils.errors import VBotError
from core.utils.logging import get_logger

# Admission waits shorter than this stay histogram-only in recordings.
_WAIT_SPAN_MIN_MS = 1.0

_LOGGER = get_logger("utils.workers")


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

    async def run[WorkerResult](
        self,
        function: Callable[..., WorkerResult],
        *arguments: Any,
        **keyword_arguments: Any,
    ) -> WorkerResult:
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

    async def _run_admitted[WorkerResult](
        self,
        function: Callable[..., WorkerResult],
        arguments: tuple[Any, ...],
        keyword_arguments: dict[str, Any],
    ) -> WorkerResult:
        loop = asyncio.get_running_loop()
        call = partial(function, *arguments, **keyword_arguments)
        # Settling first keeps a cancelled caller from releasing the semaphore early.
        return await settle_before_cancelling(
            loop.run_in_executor(self._executor, call),
            on_late_failure=partial(_log_late_failure, _callable_name(function)),
        )

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

    def call[OrderedResult](self, operation: Callable[[], OrderedResult]) -> OrderedResult:
        """Run *operation* after all earlier work and wait for it, blocking the caller.

        For synchronous callers only; on the worker thread itself it runs inline.
        """
        if getattr(self._local, "is_worker_thread", False):
            return operation()
        return self._submit(operation).result()

    async def call_async[OrderedResult](
        self, operation: Callable[[], OrderedResult]
    ) -> OrderedResult:
        """Run *operation* after all earlier work without blocking the Event Loop."""
        return await settle_before_cancelling(
            asyncio.wrap_future(self._submit(operation)),
            on_late_failure=partial(_log_late_failure, _callable_name(operation)),
        )

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

    def _submit[OrderedResult](
        self, operation: Callable[[], OrderedResult]
    ) -> Future[OrderedResult]:
        return self._executor.submit(self._run_measured, operation)

    def _run_measured[OrderedResult](self, operation: Callable[[], OrderedResult]) -> OrderedResult:
        with measure(self._run_metric, track=self._track, name=_callable_name(operation)):
            return operation()

    def _settle_hand_off(self) -> None:
        with self._hand_offs_lock:
            self._hand_offs -= 1

    def _mark_worker_thread(self) -> None:
        self._local.is_worker_thread = True


async def settle_before_cancelling[SettledResult](
    work: Awaitable[SettledResult],
    *,
    on_late_failure: Callable[[BaseException], None] | None = None,
) -> SettledResult:
    """Await *work*; when the caller is cancelled meanwhile, let *work* finish first.

    For work that mutates state and must not be seen as abandoned halfway, such
    as a started worker call or an edit that saves and then updates memory.
    Waiting never cancels *work*. The cancellation is re-raised once *work* has
    settled and wins over a late failure of it; repeated cancellations keep
    waiting. Nobody receives such a late failure, so it goes to
    *on_late_failure*, which reports it in the owner's words. Without one it is
    logged here: an expected ``VBotError`` at WARNING, anything else at ERROR
    with its traceback.
    """
    report = on_late_failure or partial(_log_late_failure, _work_name(work))
    future = asyncio.ensure_future(work)
    try:
        # Unlike a cancelled ``asyncio.shield``, which marks a late failure as
        # retrieved without reporting it, waiting leaves the failure to this owner.
        await asyncio.wait((future,))
    except asyncio.CancelledError:
        while not future.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait((future,))
        if not future.cancelled() and (error := future.exception()) is not None:
            report(error)
        raise
    return future.result()


async def finish_despite_cancel[FinishedResult](work: Awaitable[FinishedResult]) -> FinishedResult:
    """Await *work* and return its result, even when the caller is cancelled meanwhile.

    For work whose result must reach the caller once it has begun, because its
    effect cannot be taken back, such as a started Sub-Agent whose Parent must
    receive the start result. Unlike :func:`settle_before_cancelling`, the
    cancellation is absorbed: the caller returns the result normally, and an
    enclosing ``asyncio.timeout`` does not expire. A failure of *work* propagates.
    """
    future = asyncio.ensure_future(work)
    while True:
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if future.done():
                return future.result()


def _log_late_failure(work: str, error: BaseException) -> None:
    if isinstance(error, VBotError):
        _LOGGER.warning(
            "Work failed after its caller was cancelled (work=%s error=%s)",
            work,
            type(error).__name__,
        )
        return
    _LOGGER.error(
        "Work failed after its caller was cancelled (work=%s error=%s)",
        work,
        type(error).__name__,
        exc_info=error,
    )


def _work_name(work: Awaitable[Any]) -> str:
    """Return the code name of *work* for logs; never its arguments or content."""
    source: object = work.get_coro() if isinstance(work, asyncio.Task) else work
    return getattr(source, "__qualname__", None) or type(work).__name__


def _nothing() -> None:
    return None


def _callable_name(function: Callable[..., Any]) -> str:
    """Return a code identifier for span names; never arguments or content."""
    while isinstance(function, partial):
        function = function.func
    return getattr(function, "__qualname__", None) or type(function).__name__
