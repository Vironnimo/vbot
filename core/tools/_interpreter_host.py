"""Objects that do CPU-heavy work off the Event Loop, in isolated subinterpreters.

Pure-Python work proportional to program output, such as rendering a terminal
screen or cleaning a command's output, stalls every other server task when it
runs on the Event Loop; in a thread it still competes for the one GIL. An
``InterpreterHost`` therefore runs its objects on workers. Each worker is one
thread that owns a subinterpreter with its own GIL, loaded with only the
host's modules, and runs the calls to the objects assigned to it in the order
they were made. An object stays on one worker, so its calls stay ordered;
objects are assigned to the workers in turn. Workers start as objects need
them, up to the host's limit.

Hosts share no workers: the objects of one host never wait behind another's.

``inline`` hosts run their objects in the calling thread instead: for
deterministic tests, and as the fallback when a subinterpreter cannot start.
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
        self._inline = inline
        self._workers: list[_InterpreterWorker] = []
        self._starting = 0
        self._started = 0
        self._turn = 0
        self._keys = itertools.count(1)
        self._lock = threading.Lock()
        self._closed = False

    @classmethod
    def in_process(cls, *, name: str = "in-process") -> InterpreterHost:
        """A host that runs its objects in the calling thread."""
        return cls((), name=name, workers=1, inline=True)

    def open(self, factory: Callable[..., Any], *arguments: Any, **options: Any) -> HostedObject:
        """Create ``factory(*arguments, **options)`` on the next worker.

        Without a started worker, this starts one and blocks briefly: call
        ``prepare`` off the Event Loop first.
        """
        worker = self._next_worker()
        if worker is None and not self._inline:
            self.prepare()
            worker = self._next_worker()
        if worker is None:
            return _InlineObject(factory(*arguments, **options), self._name)
        key = next(self._keys)
        worker.submit_nowait(_hosted_objects.create, key, factory, arguments, options)
        return _WorkerObject(worker, key, self._name)

    def prepare(self) -> None:
        """Start one more worker unless the limit is reached; blocks briefly.

        A subinterpreter that cannot start makes a host without workers run
        its objects inline, and one with workers stop adding more.
        """
        with self._lock:
            if self._inline or self._closed or len(self._workers) + self._starting >= self._limit:
                return
            self._starting += 1
            index = self._started
            self._started += 1
        try:
            worker = _InterpreterWorker(self._name, index, _bootstrap_source(self._modules))
        except Exception:
            with self._lock:
                self._starting -= 1
                fallback = not self._workers
                if fallback:
                    self._inline = True
                else:
                    self._limit = len(self._workers)
            _LOGGER.warning(
                "%s subinterpreters are unavailable; %s",
                self._name,
                "running on the Event Loop" if fallback else "using the started ones",
                exc_info=True,
            )
            return
        with self._lock:
            self._starting -= 1
            if not self._closed:
                self._workers.append(worker)
                return
        worker.stop()
        worker.join(_WORKER_JOIN_SECONDS)

    def close(self) -> None:
        """Stop the workers after the calls already made; later calls fail."""
        with self._lock:
            self._closed = True
            workers, self._workers = self._workers, []
        for worker in workers:
            worker.stop()
        for worker in workers:
            worker.join(_WORKER_JOIN_SECONDS)

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
        started.result()

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
        from concurrent import interpreters

        try:
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
