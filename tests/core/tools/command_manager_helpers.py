"""Fakes for ``CommandManager``: processes, trees, launches, deliveries and time.

``command_harness`` builds a manager over them; tests drive each fake process's
output, exit and leftover processes, and let fake time pass with ``advance``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.permissions import PermissionBoundary
from core.runs import RunExecutionOwner
from core.sessions import SessionAddress
from core.storage.temp_files import TemporaryFileManager
from core.tools._interpreter_host import InterpreterHost
from core.tools.command_manager import (
    CommandManager,
    CommandReport,
    CommandReportFormatter,
    NotRunReason,
)
from core.tools.process_tree import (
    ProcessTreeFacts,
    ProcessTreeTracker,
    ProgramExit,
    RunningProcess,
)
from tests.core.tools.terminal_manager_helpers import FakeClock

OWNER = SessionAddress("project-a", "agent-a", "session-a")


def format_report(report: CommandReport) -> str:
    return f"{report.command_id} {report.status} {report.stop_reason} {report.exit_code}"


async def run_inline(function: Callable[..., Any], /, *args: Any) -> Any:
    return function(*args)


async def turns(count: int = 40) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


class FakeTree:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.running: list[RunningProcess] = [RunningProcess(process.pid, "shell")]
        self.cpu_seconds = 0.0
        self.started = 1
        self.exits: list[ProgramExit] = []
        # PowerShell job hosts that ended.
        self.jobs_ended = 0
        # Raised by the process queries instead of answering.
        self.facts_error: OSError | None = None
        self.terminate_error: OSError | None = None
        self.terminations = 0
        self.closed = False

    def facts(self) -> ProcessTreeFacts:
        if self.facts_error is not None:
            raise self.facts_error
        return ProcessTreeFacts(
            tuple(self.running), self.cpu_seconds, self.started, tuple(self.exits), self.jobs_ended
        )

    def exit_count(self) -> int:
        return len(self.exits)

    def terminate(self) -> None:
        self.terminations += 1
        if self.terminate_error is not None:
            raise self.terminate_error
        self.running.clear()
        self.process.exit(1)

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    """A shell whose output, exit and leftover processes the test controls."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.tree = FakeTree(self)
        self._output: asyncio.Queue[bytes] = asyncio.Queue()
        self._exit: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._eof = False
        # POSIX: an interrupt reaches the process group; on Windows none is sent.
        self.interruptible = False
        self.exits_on_interrupt = False
        self.interrupts = 0
        self.released = False

    async def read(self) -> bytes:
        if self._eof:
            return b""
        data = await self._output.get()
        self._eof = not data
        return data

    async def wait(self) -> int:
        return await asyncio.shield(self._exit)

    def interrupt(self) -> bool:
        if not self.interruptible:
            return False
        self.interrupts += 1
        if self.exits_on_interrupt:
            self.tree.running.clear()
            self.exit(130)
        return True

    def close_output(self) -> None:
        self._output.put_nowait(b"")

    def close(self) -> None:
        self.released = True

    # The test's side

    @property
    def unread(self) -> int:
        """Chunks written and not read yet."""
        return self._output.qsize()

    def write(self, data: str | bytes) -> None:
        self._output.put_nowait(data.encode() if isinstance(data, str) else data)

    def exit(self, code: int, *, leftovers: Iterable[RunningProcess] = ()) -> None:
        """The shell exits; *leftovers* keep running and hold the output pipe."""
        self.tree.running = list(leftovers)
        if not self._exit.done():
            self._exit.set_result(code)
        if all(process.name == "conhost.exe" for process in self.tree.running):
            self._output.put_nowait(b"")

    def end_leftovers(self) -> None:
        self.tree.running.clear()
        self._output.put_nowait(b"")


class FakeLauncher:
    """Starts fake processes; records each launch's argv, directory, environment, request
    and whether a warm shell could serve it."""

    def __init__(self) -> None:
        self.processes: list[FakeProcess] = []
        self.argv: list[list[str]] = []
        self.cwds: list[Path] = []
        self.envs: list[dict[str, str]] = []
        self.requests: list[bytes | None] = []
        self.warm: list[bool] = []
        # While set and not open, launches wait for it.
        self.gate: asyncio.Event | None = None
        # Raised by the next launches instead of starting a process.
        self.error: OSError | None = None
        self.closed = False

    async def launch(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        tracker: ProcessTreeTracker,
        request: bytes | None = None,
        *,
        warm: bool = True,
    ) -> FakeProcess:
        self.argv.append(list(argv))
        self.cwds.append(cwd)
        self.envs.append(dict(env))
        self.requests.append(request)
        self.warm.append(warm)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        process = FakeProcess(10_000 + len(self.processes))
        self.processes.append(process)
        return process

    async def aclose(self) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


class FakeSink:
    """TriggerService's completion delivery; results stay pending until handed over."""

    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self.cancellations: list[str] = []
        self.futures: list[asyncio.Future[None]] = []
        # Notice ids a delivery claimed: they can no longer be withdrawn.
        self.claimed: set[str] = set()

    def submit_completion(self, address: SessionAddress, **kwargs: Any) -> Awaitable[None]:
        self.submissions.append({"address": address, **kwargs})
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.futures.append(future)
        return future

    def cancel_completion(self, address: SessionAddress, *, notice_id: str) -> bool:
        """Withdraw the pending submission of *notice_id* to this address."""
        self.cancellations.append(notice_id)
        if notice_id in self.claimed:
            return False
        for submission, future in zip(self.submissions, self.futures, strict=True):
            submitted_to = submission["address"]
            if submission["notice_id"] == notice_id and submitted_to == address:
                if future.done():
                    return False
                future.cancel()
                return True
        return False

    @property
    def bodies(self) -> list[str]:
        return [submission["body"] for submission in self.submissions]


@dataclass
class Harness:
    """A ``CommandManager`` over fake processes on a fake clock."""

    manager: CommandManager
    launcher: FakeLauncher
    clock: FakeClock
    sink: FakeSink
    cwd: Path
    changes: list[str] = field(default_factory=list)

    async def launch(
        self,
        *,
        owner: SessionAddress = OWNER,
        run_id: str = "run-a",
        on_output: Callable[[str], Awaitable[None]] | None = None,
        execution_owner: RunExecutionOwner | None = None,
        command: str = "work",
        description: str | None = None,
        formatter: CommandReportFormatter = format_report,
        permissions: PermissionBoundary = PermissionBoundary.UNRESTRICTED,
        not_run_codes: Mapping[int, NotRunReason] | None = None,
    ) -> tuple[str, FakeProcess]:
        """Start a foreground command on a fake process, for a full-access Session
        unless *permissions* say otherwise."""
        handle = await self.manager.launch(
            owner,
            ["shell", "-c", command],
            command=command,
            description=description,
            cwd=self.cwd,
            env={},
            origin_run_id=run_id,
            formatter=formatter,
            permissions=permissions,
            execution_owner=execution_owner,
            on_output=on_output,
            not_run_codes=not_run_codes,
        )
        process = self.launcher.processes[-1]
        assert handle.pid == process.pid
        return handle.command_id, process

    async def advance(self, seconds: float, *, step: float = 0.25) -> None:
        """Let *seconds* pass on the fake clock in steps, settling the loop at each."""
        await turns()
        end = self.clock.now + seconds
        while self.clock.now < end - 1e-9:
            await self.clock.advance(min(step, end - self.clock.now))
            await turns()

    async def finish(self, command_id: str) -> CommandReport:
        """Let fake time pass until no process of the command runs."""
        await turns()
        for _step in range(100):
            report = self.manager.report(command_id)
            if report.finished:
                return report
            await self.advance(0.25)
        raise AssertionError(f"command {command_id} did not finish")


@asynccontextmanager
async def command_harness(
    tmp_path: Path,
    *,
    max_live: int = 3,
    sink: FakeSink | None = None,
    run_blocking: Callable[..., Awaitable[Any]] = run_inline,
    checkpoint_store: Any = None,
    read_listening: Callable[[], frozenset[int]] = frozenset,
) -> AsyncIterator[Harness]:
    """A started manager over fakes, with its log files under *tmp_path*; closed after.

    Process queries and kills run inline unless *run_blocking* says otherwise;
    output is cleaned inline. No process listens on a TCP port unless
    *read_listening* says otherwise.
    """
    clock = FakeClock()
    launcher = FakeLauncher()
    sink = sink if sink is not None else FakeSink()
    manager = CommandManager(
        sink,
        temporary_files=TemporaryFileManager(tmp_path / "data"),
        launcher=launcher,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        run_blocking=run_blocking,
        max_live=max_live,
        output_host=InterpreterHost.in_process(),
        checkpoint_store=checkpoint_store,
        read_listening=read_listening,
    )
    harness = Harness(manager, launcher, clock, sink, tmp_path)
    manager.add_changed_callback(harness.changes.append)
    manager.start()
    try:
        yield harness
    finally:
        await manager.aclose()


__all__ = [
    "OWNER",
    "FakeClock",
    "FakeLauncher",
    "FakeProcess",
    "FakeSink",
    "FakeTree",
    "Harness",
    "command_harness",
    "format_report",
    "run_inline",
    "turns",
]
