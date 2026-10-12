"""Objects that do CPU-heavy work off the Event Loop, in isolated subinterpreters.

Pure-Python work proportional to program output, such as rendering a terminal
screen or cleaning a command's output, stalls every other server task when it
runs on the Event Loop; in a thread it still competes for the one GIL. An
``InterpreterHost`` therefore runs its objects on workers. Each worker is one
thread that owns a subinterpreter with its own GIL, loaded with only the
host's modules, and runs the calls to the objects assigned to it in the order
they were made. An object stays on one worker, so its calls stay ordered;
objects are assigned to the workers in turn. Workers start as objects need
them, up to the host's limit; an object opened while the first worker is still
starting waits for it.

Hosts share no workers: the objects of one host never wait behind another's.

``inline`` hosts run their objects in the calling thread instead: for
deterministic tests, and as the fallback when no subinterpreter can start.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import itertools
import os
import queue
import sys
import threading
from collections.abc import Callable, Sequence
from types import ModuleType
from typing import Any, Protocol

from core.utils.logging import get_logger

from . import _hosted_objects

_LOGGER = get_logger("tools.interpreter_host")
DEFAULT_WORKERS = max(1, min(4, (os.cpu_count() or 2) // 2))
_WORKER_JOIN_SECONDS = 2.0
# A worker that has not started by then counts as failed, so a start that never
# resolves cannot hold every later launch. A start takes 35-90 ms (2026-10-12).
_WORKER_START_SECONDS = 30.0


class HostedObject(Protocol):
    """One object of an ``InterpreterHost``, used from the Event Loop.

    Calls run in the order they are made; awaiting one does not let a later
    one overtake it. Arguments and results cross between interpreters: they
    are shareable values (bytes, str, numbers, tuples of them) or pickled.
    """

    async def call(self, method: str, *arguments: Any, **options: Any) -> Any:
        """Run the object's *method* and return its result."""
        ...

    def call_nowait(self, method: str, *arguments: Any, **options: Any) -> None:
        """Run the object's *method* after the calls before it; a failure is logged."""

    def close(self) -> None:
        """Release the object after the calls made before; later calls fail."""


class InterpreterHost:
    """Create objects on subinterpreter workers (or inline).

    *modules* are loaded into each worker under their canonical names; they
    may import only the standard library, third-party packages, and modules
    loaded before them. The factory of an object, and the types it returns,
    are defined in them.
    """

    def __init__(
        self,
        modules: Sequence[ModuleType],
        *,
        name: str,
        workers: int = DEFAULT_WORKERS,
        inline: bool = False,
    ) -> None:
        if workers < 1:
            raise ValueError(f"The {name} host needs at least one worker")
        self._modules = tuple(modules)
        self._name = name
        self._limit = workers
        self._created_inline = inline
        # Runs its objects inline: created so, or no subinterpreter could start.
        self._inline = inline
        self._inline_reported = False
        self._workers: list[_InterpreterWorker] = []
        self._starting = 0
        self._started = 0
        self._turn = 0
        self._keys = itertools.count(1)
        self._lock = threading.Lock()
        # Notified when a worker started, a start failed, or the host closed.
        self._changed = threading.Condition(self._lock)
        self._closed = False

    @classmethod
    def in_process(cls, *, name: str = "in-process") -> InterpreterHost:
        """A host that runs its objects in the calling thread."""
        return cls((), name=name, workers=1, inline=True)

    def open(self, factory: Callable[..., Any], *arguments: Any, **options: Any) -> HostedObject:
        """Create ``factory(*arguments, **options)`` on the next worker.

        Without a started worker, this starts one or waits for the one
        starting, and blocks briefly: call ``prepare`` off the Event Loop
        first. The object runs inline only on an inline host. Raises
        RuntimeError once the host is closed.
        """
        worker = self._next_worker()
        if worker is None and not self._inline:
            self.prepare()
            worker = self._next_worker()
        if worker is None:
            self._report_inline()
            return _InlineObject(factory(*arguments, **options), self._name)
        key = next(self._keys)
        worker.submit_nowait(_hosted_objects.create, key, factory, arguments, options)
        return _WorkerObject(worker, key, self._name)

    def prepare(self) -> None:
        """Start one more worker unless the limit is reached; blocks briefly.

        It returns once a worker runs, the host runs its objects inline or it
        closed, so the next ``open`` never waits: while no worker runs yet and
        starts are in flight, its own included, it waits until one started or
        every start failed. A subinterpreter that cannot start, or does not
        start within ``_WORKER_START_SECONDS``, stops the host from adding
        more workers; once none started and none is starting, the host runs
        its objects inline.
        """
        with self._changed:
            if not self._await_room():
                return
            self._starting += 1
            index = self._started
            self._started += 1
        try:
            worker = _InterpreterWorker(self._name, index, _bootstrap_source(self._modules))
        except BaseException as error:
            with self._changed:
                self._starting -= 1
                # The starts in progress decide whether any worker runs.
                self._limit = len(self._workers) + self._starting
                fallback = self._limit == 0
                if fallback:
                    self._inline = True
                self._changed.notify_all()
            if not isinstance(error, Exception):
                raise
            _LOGGER.warning(
                "%s subinterpreters are unavailable; %s",
                self._name,
                "running on the Event Loop" if fallback else "starting no more workers",
                exc_info=True,
            )
            # Another start may still give a worker: wait for it here, off the
            # Event Loop, so that ``open`` does not. The lowered limit leaves
            # no room for another start of this call.
            with self._changed:
                self._await_room()
            return
        with self._changed:
            self._starting -= 1
            self._changed.notify_all()
            if not self._closed:
                self._workers.append(worker)
                return
        worker.stop()
        worker.join(_WORKER_JOIN_SECONDS)

    def _await_room(self) -> bool:
        """Whether one more worker may start now; called with the lock held.

        While no worker runs and starts are in flight, it waits until one
        started, every start failed or the host closed. Every start resolves
        within ``_WORKER_START_SECONDS``, so the wait ends.
        """
        while True:
            if self._inline or self._closed:
                return False
            if len(self._workers) + self._starting < self._limit:
                return True
            if self._workers or not self._starting:
                return False
            self._changed.wait()

    def close(self) -> None:
        """Stop the workers after the calls already made; later calls fail."""
        with self._changed:
            self._closed = True
            workers, self._workers = self._workers, []
            self._changed.notify_all()
        for worker in workers:
            worker.stop()
        for worker in workers:
            worker.join(_WORKER_JOIN_SECONDS)

    def _report_inline(self) -> None:
        """Warn once when a host meant for workers runs an object on the caller's thread."""
        with self._lock:
            if self._created_inline or self._inline_reported:
                return
            self._inline_reported = True
        _LOGGER.warning(
            "The %s host runs its objects in the calling thread, on the Event Loop", self._name
        )

    def _next_worker(self) -> _InterpreterWorker | None:
        with self._lock:
            if self._closed:
                raise RuntimeError(f"The {self._name} host is closed")
            if self._inline or not self._workers:
                return None
            worker = self._workers[self._turn % len(self._workers)]
            self._turn += 1
            return worker


class _InlineObject:
    def __init__(self, instance: Any, name: str) -> None:
        self._instance: Any = instance
        self._name = name

    async def call(self, method: str, *arguments: Any, **options: Any) -> Any:
        return self._method(method)(*arguments, **options)

    def call_nowait(self, method: str, *arguments: Any, **options: Any) -> None:
        try:
            self._method(method)(*arguments, **options)
        except Exception:
            _LOGGER.warning("A %s call failed", self._name, exc_info=True)

    def close(self) -> None:
        self._instance = None

    def _method(self, method: str) -> Callable[..., Any]:
        if self._instance is None:
            raise RuntimeError(f"The {self._name} object is closed")
        function: Callable[..., Any] = getattr(self._instance, method)
        return function


class _WorkerObject:
    def __init__(self, worker: _InterpreterWorker, key: int, name: str) -> None:
        self._worker = worker
        self._key = key
        self._name = name
        self._closed = False

    async def call(self, method: str, *arguments: Any, **options: Any) -> Any:
        self._check()
        return await self._worker.submit(
            _hosted_objects.call, self._key, method, arguments, options
        )

    def call_nowait(self, method: str, *arguments: Any, **options: Any) -> None:
        self._check()
        self._worker.submit_nowait(_hosted_objects.call, self._key, method, arguments, options)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # A stopped worker has already dropped every object with its interpreter.
        with contextlib.suppress(RuntimeError):
            self._worker.submit_nowait(_hosted_objects.drop, self._key)

    def _check(self) -> None:
        if self._closed:
            raise RuntimeError(f"The {self._name} object is closed")


_STOP = object()


class _InterpreterWorker:
    """One thread running calls in its own subinterpreter, in submission order."""

    def __init__(self, name: str, index: int, bootstrap: str) -> None:
        self._name = name
        self._jobs: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._accepting = threading.Lock()
        self._stopping = False
        started: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._thread = threading.Thread(
            target=self._run,
            args=(bootstrap, started),
            name=f"vbot-{name.replace(' ', '-')}-{index}",
            daemon=True,
        )
        self._thread.start()
        try:
            started.result(_WORKER_START_SECONDS)
        except BaseException as error:
            # Nothing uses this worker: its thread ends once its start ends.
            self.stop()
            if isinstance(error, TimeoutError) and not started.done():
                raise TimeoutError(
                    f"The {name} subinterpreter did not start within "
                    f"{_WORKER_START_SECONDS:g} seconds"
                ) from None
            raise

    async def submit(self, function: Callable[..., Any], *arguments: Any) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._put((function, arguments, loop, future))
        return await future

    def submit_nowait(self, function: Callable[..., Any], *arguments: Any) -> None:
        self._put((function, arguments, None, None))

    def stop(self) -> None:
        with self._accepting:
            if not self._stopping:
                self._stopping = True
                self._jobs.put(_STOP)

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)

    def _put(self, job: Any) -> None:
        # Nothing may queue behind the stop marker: no thread would run it.
        with self._accepting:
            if self._stopping:
                raise RuntimeError(f"The {self._name} worker stopped")
            self._jobs.put(job)

    def _run(self, bootstrap: str, started: concurrent.futures.Future[None]) -> None:
        # Every failure to start resolves ``started``, which the starting
        # thread waits for (at most ``_WORKER_START_SECONDS``).
        try:
            from concurrent import interpreters

            interpreter = interpreters.create()
        except BaseException as error:
            started.set_exception(error)
            return
        try:
            try:
                interpreter.exec(bootstrap)
            except BaseException as error:
                started.set_exception(error)
                return
            started.set_result(None)
            while True:
                job = self._jobs.get()
                if job is _STOP:
                    break
                function, arguments, loop, future = job
                try:
                    result = interpreter.call(function, *arguments)
                except BaseException as error:
                    if future is None:
                        _LOGGER.warning("A %s call failed: %s", self._name, error)
                    else:
                        _resolve(loop, future, None, RuntimeError(str(error)))
                else:
                    if future is not None:
                        _resolve(loop, future, result, None)
        finally:
            self._fail_pending()
            with contextlib.suppress(Exception):
                interpreter.close()

    def _fail_pending(self) -> None:
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            if job is _STOP:
                continue
            _function, _arguments, loop, future = job
            if future is not None:
                _resolve(loop, future, None, RuntimeError(f"The {self._name} worker stopped"))


def _resolve(
    loop: asyncio.AbstractEventLoop,
    future: asyncio.Future[Any],
    result: Any,
    error: BaseException | None,
) -> None:
    def settle() -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(settle)


def _bootstrap_source(modules: Sequence[ModuleType]) -> str:
    """Load only the hosted modules, each from its file under its canonical name.

    Importing their package would run all of it, and its dependencies need not
    support subinterpreters; calls then address the modules' functions and
    types by their usual names.
    """
    lines = ["import importlib.util, sys", f"sys.path[:] = {list(sys.path)!r}"]
    for module in (_hosted_objects, *modules):
        lines += [
            "spec = importlib.util.spec_from_file_location("
            f"{module.__name__!r}, {module.__file__!r})",
            "module = importlib.util.module_from_spec(spec)",
            "sys.modules[spec.name] = module",
            "spec.loader.exec_module(module)",
        ]
    return "\n".join(lines) + "\n"


__all__ = ["DEFAULT_WORKERS", "HostedObject", "InterpreterHost"]
