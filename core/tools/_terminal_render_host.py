"""Terminal screens rendered off the Event Loop in isolated subinterpreters.

VT rendering is pure-Python CPU work proportional to program output. Run on
the Event Loop, a program printing heavily stalls every other server task;
run in a thread, it still competes for the one GIL. Each renderer worker
therefore owns a subinterpreter with its own GIL, loaded with only
``terminal_emulator.py`` and pyte, and serves the screens assigned to it in
call order. A screen always stays on one worker, so its calls stay ordered.

``inline`` hosts render in the calling thread instead: deterministic tests,
and the fallback when a subinterpreter cannot start.
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
from collections.abc import Callable
from typing import Any, Protocol

from core.tools import terminal_emulator
from core.tools.terminal_emulator import EmulatorUpdate, ScreenObservation, TerminalEmulator
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.terminal_manager")
_DEFAULT_WORKERS = max(1, min(4, (os.cpu_count() or 2) // 2))
_WORKER_JOIN_SECONDS = 2.0


class TerminalScreen:
    """The rendered screen of one Terminal Session, used from the Event Loop.

    Calls are applied in the order they are made; awaiting one does not let a
    later call overtake it.
    """

    def __init__(self, backend: _ScreenBackend, columns: int, rows: int) -> None:
        self._backend = backend
        self.columns = columns
        self.rows = rows

    async def feed(self, text: str) -> EmulatorUpdate:
        update: EmulatorUpdate = await self._backend.call("feed", (text,))
        return update

    async def resize(self, columns: int, rows: int) -> None:
        self.columns = columns
        self.rows = rows
        await self._backend.call("resize", (columns, rows))

    async def observe(self, tail_lines: int) -> ScreenObservation:
        observation: ScreenObservation = await self._backend.call("observe", (tail_lines,))
        return observation

    async def screen_text(self) -> str:
        text: str = await self._backend.call("screen_text", ())
        return text

    async def page(self, *, start_line: int | None, limit: int) -> dict[str, Any]:
        page: dict[str, Any] = await self._backend.call(
            "page", (), {"start_line": start_line, "limit": limit}
        )
        return page

    async def mark_input(self) -> None:
        await self._backend.call("mark_input", ())

    async def pattern_text(self, *, start_line: int | None) -> dict[str, Any]:
        found: dict[str, Any] = await self._backend.call(
            "pattern_text", (), {"start_line": start_line}
        )
        return found

    async def ansi_snapshot(self) -> str:
        ansi: str = await self._backend.call("ansi_snapshot", ())
        return ansi

    async def commit_transcript(self) -> tuple[str, ...]:
        lines: tuple[str, ...] = await self._backend.call("commit_transcript", ())
        return lines

    async def pending_transcript(self) -> tuple[str, ...]:
        lines: tuple[str, ...] = await self._backend.call("pending_transcript", ())
        return lines

    def close(self) -> None:
        """Release the screen; later calls fail."""
        self._backend.close()


class TerminalRenderHost:
    """Create Terminal screens on renderer workers (or inline)."""

    def __init__(self, *, workers: int = _DEFAULT_WORKERS, inline: bool = False) -> None:
        if workers < 1:
            raise ValueError("A Terminal render host needs at least one worker")
        self._worker_count = workers
        self._inline = inline
        self._workers: list[_InterpreterWorker] = []
        self._keys = itertools.count(1)
        self._next_worker = itertools.cycle(range(workers))
        self._lock = threading.Lock()
        self._closed = False

    @classmethod
    def in_process(cls) -> TerminalRenderHost:
        """A host that renders in the calling thread."""
        return cls(workers=1, inline=True)

    def open_screen(
        self, columns: int, rows: int, *, scrollback_lines: int, transcript: bool = False
    ) -> TerminalScreen:
        """Open a screen; with *transcript* its feeds also yield final output lines."""
        if self._closed:
            raise RuntimeError("Terminal render host is closed")
        worker = None if self._inline else self._worker()
        if worker is None:
            emulator = TerminalEmulator(
                columns, rows, scrollback_lines=scrollback_lines, transcript=transcript
            )
            return TerminalScreen(_InlineBackend(emulator), columns, rows)
        key = next(self._keys)
        worker.submit_nowait(
            terminal_emulator.hosted_create, key, columns, rows, scrollback_lines, transcript
        )
        return TerminalScreen(_WorkerBackend(worker, key), columns, rows)

    def close(self) -> None:
        """Stop the workers; open screens fail afterwards."""
        self._closed = True
        with self._lock:
            workers, self._workers = self._workers, []
        for worker in workers:
            worker.stop()
        for worker in workers:
            worker.join(_WORKER_JOIN_SECONDS)

    def prepare(self) -> None:
        """Start the workers now instead of on the first screen; blocks briefly.

        A subinterpreter that cannot start makes the host render inline.
        """
        with self._lock:
            if self._inline or self._workers or self._closed:
                return
            try:
                self._workers = [_InterpreterWorker(index) for index in range(self._worker_count)]
            except Exception:
                _LOGGER.warning(
                    "Terminal renderer subinterpreters are unavailable; rendering on the "
                    "Event Loop",
                    exc_info=True,
                )
                self._inline = True

    def _worker(self) -> _InterpreterWorker | None:
        self.prepare()
        with self._lock:
            if self._inline or not self._workers:
                return None
            return self._workers[next(self._next_worker)]


class _ScreenBackend(Protocol):
    async def call(
        self, method: str, arguments: tuple[Any, ...], options: dict[str, Any] | None = None
    ) -> Any: ...

    def close(self) -> None: ...


class _InlineBackend:
    def __init__(self, emulator: TerminalEmulator) -> None:
        self._emulator: TerminalEmulator | None = emulator

    async def call(
        self, method: str, arguments: tuple[Any, ...], options: dict[str, Any] | None = None
    ) -> Any:
        if self._emulator is None:
            raise RuntimeError("Terminal screen is closed")
        return getattr(self._emulator, method)(*arguments, **(options or {}))

    def close(self) -> None:
        self._emulator = None


class _WorkerBackend:
    def __init__(self, worker: _InterpreterWorker, key: int) -> None:
        self._worker = worker
        self._key = key
        self._closed = False

    async def call(
        self, method: str, arguments: tuple[Any, ...], options: dict[str, Any] | None = None
    ) -> Any:
        if self._closed:
            raise RuntimeError("Terminal screen is closed")
        return await self._worker.submit(
            terminal_emulator.hosted_call, self._key, method, arguments, options or {}
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # A stopped worker has already dropped every screen with its interpreter.
        with contextlib.suppress(RuntimeError):
            self._worker.submit_nowait(terminal_emulator.hosted_drop, self._key)


_STOP = object()


class _InterpreterWorker:
    """One thread running calls in its own subinterpreter, in submission order."""

    def __init__(self, index: int) -> None:
        self._jobs: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._accepting = threading.Lock()
        self._stopping = False
        started: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._thread = threading.Thread(
            target=self._run,
            args=(started,),
            name=f"vbot-terminal-render-{index}",
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

    def _put(self, job: Any) -> None:
        # Nothing may queue behind the stop marker: no thread would run it.
        with self._accepting:
            if self._stopping:
                raise RuntimeError("Terminal renderer stopped")
            self._jobs.put(job)

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)

    def _run(self, started: concurrent.futures.Future[None]) -> None:
        from concurrent import interpreters

        try:
            interpreter = interpreters.create()
        except BaseException as error:
            started.set_exception(error)
            return
        try:
            try:
                interpreter.exec(_bootstrap_source())
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
                        _LOGGER.warning("Terminal renderer call failed: %s", error)
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
                _resolve(loop, future, None, RuntimeError("Terminal renderer stopped"))


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


def _bootstrap_source() -> str:
    """Load only the emulator module (and pyte) under its canonical name.

    Importing ``core.tools`` would run the whole Tool package, whose
    dependencies need not support subinterpreters; the module is loaded from
    its file instead, so calls address its functions by their usual name.
    """
    name = terminal_emulator.__name__
    path = terminal_emulator.__file__
    return (
        "import importlib.util, sys\n"
        f"sys.path[:] = {list(sys.path)!r}\n"
        f"spec = importlib.util.spec_from_file_location({name!r}, {path!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = module\n"
        "spec.loader.exec_module(module)\n"
    )
