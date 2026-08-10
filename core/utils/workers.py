"""Bounded worker pools for blocking in-process work.

``asyncio.to_thread`` protects the Event Loop, but every call shares the loop's
default executor and submits work before any application-level backpressure can
apply.  This module owns the stronger cross-domain boundary: a named dedicated
executor, a per-Event-Loop admission limit, and cancellation that does not report
completion while an already-started worker is still mutating process state.
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, TypeVar

_WorkerResult = TypeVar("_WorkerResult")


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
        async with semaphore:
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

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        semaphore = self._semaphores.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._max_workers)
            self._semaphores[loop] = semaphore
        return semaphore
