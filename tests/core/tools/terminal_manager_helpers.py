"""Shared fixtures and fakes for terminal manager behavior tests."""

from __future__ import annotations

import asyncio
import queue
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest_asyncio

from core.tools.terminal_manager import (
    TerminalManager,
    TerminalOwner,
)

TEST_ACTIVITY_QUIET_SECONDS = 1.0


class FakeTerminalAdapter:
    def __init__(self, initial_output: str | None = None) -> None:
        self._output: queue.Queue[str | None] = queue.Queue()
        if initial_output is not None:
            self._output.put(initial_output)
        self.writes: list[str] = []
        self.resizes: list[tuple[int, int]] = []
        self.alive = True
        self.code: int | None = None
        self.write_error: BaseException | None = None

    @property
    def pid(self) -> int:
        return 987_654

    def read(self, _size: int) -> str:
        value = self._output.get()
        if value is None:
            raise EOFError
        return value

    def write(self, text: str) -> None:
        if self.write_error is not None:
            error = self.write_error
            self.finish(0)
            raise error
        self.writes.append(text)

    def resize(self, rows: int, columns: int) -> None:
        self.resizes.append((rows, columns))

    def is_alive(self) -> bool:
        return self.alive

    def exit_code(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.finish(-1)

    def close(self) -> None:
        self.finish(-1)
        self._output.put(None)

    def emit(self, text: str) -> None:
        self._output.put(text)

    def finish(self, code: int = 0) -> None:
        if not self.alive:
            return
        self.code = code
        self.alive = False
        self._output.put(None)


class AdapterFactory:
    def __init__(self, initial_output: str | None = None) -> None:
        self.initial_output = initial_output
        self.adapters: list[FakeTerminalAdapter] = []
        self.calls: list[tuple[list[str], Path, dict[str, str], int, int]] = []

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        rows: int,
        columns: int,
    ) -> FakeTerminalAdapter:
        adapter = FakeTerminalAdapter(self.initial_output)
        self.adapters.append(adapter)
        self.calls.append((list(argv), cwd, dict(env), rows, columns))
        return adapter


class PendingTriggerService:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.submissions: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.cancellations: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def submit_completion(self, *args: Any, **kwargs: Any) -> Any:
        self.submissions.append((args, kwargs))

        async def pending() -> None:
            await self.release.wait()

        return pending()

    def cancel_completion(self, *args: Any, **kwargs: Any) -> None:
        self.cancellations.append((args, kwargs))


class FakeClock:
    """Controllable monotonic time for quiet/grace state-machine tests."""

    def __init__(self) -> None:
        self.now = 0.0
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        deadline = self.now + delay
        future = asyncio.get_running_loop().create_future()
        waiter = (deadline, future)
        self._waiters.append(waiter)
        try:
            await future
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    @property
    def sleeping(self) -> bool:
        return any(not future.done() for _, future in self._waiters)

    async def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("Fake time cannot move backwards")
        self.now += seconds
        due = [future for deadline, future in self._waiters if deadline <= self.now]
        for future in due:
            if not future.done():
                future.set_result(None)
        await asyncio.sleep(0)


@pytest_asyncio.fixture
async def terminal_manager() -> AsyncIterator[tuple[TerminalManager, AdapterFactory]]:
    factory = AdapterFactory()
    manager = TerminalManager(
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        yield manager, factory
    finally:
        await manager.aclose()


def owner(session_id: str = "session-a") -> TerminalOwner:
    return TerminalOwner("project-a", "agent-a", session_id)


async def spawn(
    manager: TerminalManager,
    tmp_path: Path,
    *,
    command: str = "fake-tui",
    initial_text: str | None = None,
) -> Any:
    return await manager.spawn(
        owner(),
        [command],
        cwd=tmp_path,
        env=None,
        columns=120,
        rows=32,
        origin_run_id="run-a",
        initial_text=initial_text,
    )


async def eventually(predicate: Any, *, attempts: int = 200) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached")


async def settle_next_activity(
    clock: FakeClock,
    session: Any,
    *,
    after_generation: int,
    quiet_seconds: float,
) -> None:
    await eventually(lambda: session.activity_generation > after_generation)
    await eventually(lambda: clock.sleeping)
    await clock.advance(quiet_seconds)
    await eventually(lambda: session.state == "ready")


async def establish_delivered_baseline(
    manager: TerminalManager,
    factory: AdapterFactory,
    trigger: PendingTriggerService,
    clock: FakeClock,
    tmp_path: Path,
    *,
    quiet_seconds: float,
) -> Any:
    session = await spawn(manager, tmp_path)
    session.state = "working"
    manager.attach(session.terminal_id, owner(), origin_run_id="attach-run")
    await manager.send_input(
        session.terminal_id,
        owner(),
        data="go\r",
        text=None,
        key=None,
        expected_screen_revision=None,
        origin_run_id="run-0",
    )
    generation = session.activity_generation
    factory.adapters[0].emit("MENU> ")
    await settle_next_activity(
        clock,
        session,
        after_generation=generation,
        quiet_seconds=quiet_seconds,
    )
    await eventually(lambda: len(trigger.submissions) == 1)
    trigger.release.set()
    await eventually(lambda: session.attention is not None and session.attention.delivered)
    return session
