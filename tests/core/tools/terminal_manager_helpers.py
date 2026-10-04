"""Shared fixtures and fakes for terminal manager behavior tests."""

from __future__ import annotations

import asyncio
import inspect
import queue
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import core.tools._terminal_session as terminal_session
import core.tools.terminal_manager as terminal_manager_module
from core.tools._terminal_process_tree import (
    ProcessTreeFacts,
    ProgramExit,
    RunningProcess,
)
from core.tools.terminal_manager import (
    TerminalInfo,
    TerminalManager,
    TerminalOwner,
    TerminalRenderHost,
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


class FakeTree:
    """A command's process tree: what runs, CPU used, failed children, and the kill."""

    def __init__(self) -> None:
        self.adapter: FakeTerminalAdapter | None = None
        self.running: tuple[RunningProcess, ...] = (RunningProcess(1, "pwsh.exe"),)
        self.cpu_seconds = 0.0
        self.started = 1
        self.exits: tuple[ProgramExit, ...] = ()
        self.terminated = 0
        self.closed = False

    def facts(self) -> ProcessTreeFacts:
        return ProcessTreeFacts(self.running, self.cpu_seconds, self.started, self.exits)

    def terminate(self) -> None:
        self.terminated += 1
        self.running = ()
        if self.adapter is not None:
            self.adapter.finish(1)

    def close(self) -> None:
        self.closed = True

    def shell_exits(self, code: int, *, survivors: tuple[RunningProcess, ...] = ()) -> None:
        self.running = survivors
        assert self.adapter is not None
        self.adapter.finish(code)


class AdapterFactory:
    def __init__(self, initial_output: str | None = None) -> None:
        self.initial_output = initial_output
        self.adapters: list[FakeTerminalAdapter] = []
        self.calls: list[tuple[list[str], Path, dict[str, str], int, int]] = []
        # The exact Windows command line of each call, when one was given.
        self.command_lines: list[str | None] = []
        # Set to fail every later start with this error; the call is still recorded.
        self.error: BaseException | None = None

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        rows: int,
        columns: int,
        *,
        command_line: str | None = None,
    ) -> FakeTerminalAdapter:
        self.calls.append((list(argv), cwd, dict(env), rows, columns))
        self.command_lines.append(command_line)
        if self.error is not None:
            raise self.error
        adapter = FakeTerminalAdapter(self.initial_output)
        self.adapters.append(adapter)
        return adapter


class PendingTriggerService:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.submissions: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.cancellations: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        # Set to fail every delivery terminally with this error.
        self.error: BaseException | None = None

    def submit_completion(self, *args: Any, **kwargs: Any) -> Any:
        self.submissions.append((args, kwargs))
        error = self.error

        async def pending() -> None:
            if error is not None:
                raise error
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


@pytest.fixture
def quick_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat a start screen or shell prompt as settled after 10 ms instead of 0.5 s."""
    monkeypatch.setattr(terminal_session, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)


@pytest.fixture
def default_shell(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make PowerShell the host's default interactive shell on every platform."""
    monkeypatch.setattr(terminal_manager_module, "default_terminal_argv", lambda _env: ["pwsh.exe"])
    return "pwsh.exe"


@pytest_asyncio.fixture
async def terminal_manager() -> AsyncIterator[tuple[TerminalManager, AdapterFactory]]:
    factory = AdapterFactory()
    manager = TerminalManager(
        adapter_factory=factory,
        render_host=TerminalRenderHost.in_process(),
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        yield manager, factory
    finally:
        await manager.aclose()


@pytest_asyncio.fixture
async def delivering_manager() -> AsyncIterator[
    tuple[TerminalManager, AdapterFactory, PendingTriggerService]
]:
    """A manager that delivers settled activity to a recording Trigger service."""
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        render_host=TerminalRenderHost.in_process(),
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        yield manager, factory, trigger
    finally:
        await manager.aclose()


@pytest_asyncio.fixture
async def clocked_manager() -> AsyncIterator[
    tuple[TerminalManager, AdapterFactory, PendingTriggerService, FakeClock]
]:
    """A delivering manager whose quiet and resize-grace timers follow a fake clock."""
    clock = FakeClock()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        render_host=TerminalRenderHost.in_process(),
        sweep_interval_seconds=3600,
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    try:
        yield manager, factory, trigger, clock
    finally:
        await manager.aclose()


def owner(session_id: str = "session-a") -> TerminalOwner:
    return TerminalOwner("project-a", "agent-a", session_id)


def terminal_info(manager: TerminalManager, terminal_id: str) -> TerminalInfo:
    """Return the current facts of a terminal, including one no Agent owns."""
    return next(item for item in manager.list_terminals() if item.terminal_id == terminal_id)


async def spawn(
    manager: TerminalManager,
    tmp_path: Path,
    *,
    command: str = "fake-tui",
    initial_text: str | None = None,
) -> TerminalInfo:
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
    """Poll *predicate* until it is true; an awaitable answer is awaited first."""
    for _ in range(attempts):
        answer = predicate()
        if inspect.isawaitable(answer):
            answer = await answer
        if answer:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached")


async def screen_shows(manager: TerminalManager, terminal_id: str, text: str) -> bool:
    """Whether the rendered screen of a terminal contains *text*."""
    screen: str = (await manager.read_for_operator(terminal_id))["screen"]
    return text in screen


async def settle_next_activity(
    clock: FakeClock,
    manager: TerminalManager,
    terminal_id: str,
    *,
    after_revision: int,
    quiet_seconds: float,
) -> None:
    """Let output that advanced the screen past *after_revision* reach its quiet boundary."""
    await eventually(lambda: terminal_info(manager, terminal_id).screen_revision > after_revision)
    await eventually(lambda: clock.sleeping)
    await clock.advance(quiet_seconds)
    await eventually(lambda: terminal_info(manager, terminal_id).state == "ready")


async def establish_delivered_baseline(
    manager: TerminalManager,
    factory: AdapterFactory,
    trigger: PendingTriggerService,
    clock: FakeClock,
    tmp_path: Path,
    *,
    quiet_seconds: float,
) -> TerminalInfo:
    """Start an attached terminal whose first settled screen was delivered."""
    started = await spawn(manager, tmp_path)
    terminal_id = started.terminal_id
    manager.attach(terminal_id, owner(), origin_run_id="attach-run")
    sent = await manager.send_input(
        terminal_id,
        owner(),
        data="go\r",
        text=None,
        key=None,
        expected_screen_revision=None,
        origin_run_id="run-0",
    )
    factory.adapters[0].emit("MENU> ")
    await settle_next_activity(
        clock,
        manager,
        terminal_id,
        after_revision=sent["screen_revision"],
        quiet_seconds=quiet_seconds,
    )
    await eventually(lambda: len(trigger.submissions) == 1)
    trigger.release.set()

    def delivered() -> bool:
        attention = terminal_info(manager, terminal_id).attention
        return attention is not None and attention.delivered

    await eventually(delivered)
    return terminal_info(manager, terminal_id)
