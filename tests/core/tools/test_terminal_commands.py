"""Shell commands as Terminal Sessions: outcome facts, hand-off, delivery, stops and idleness."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from core.tools._terminal_command import CommandReport
from core.tools._terminal_process_tree import (
    ProcessTreeFacts,
    ProgramExit,
    RunningProcess,
)
from core.tools.terminal_manager import (
    TerminalClosedError,
    TerminalManager,
    TerminalRenderHost,
)
from tests.core.tools.terminal_manager_helpers import (
    AdapterFactory,
    FakeClock,
    FakeTerminalAdapter,
    PendingTriggerService,
    eventually,
    owner,
)
from tests.core.tools.terminal_manager_helpers import shell_environment as shell_environment


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


class Harness:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.trigger = PendingTriggerService()
        self.factory = AdapterFactory()
        self.trees: list[FakeTree] = []
        self.manager = TerminalManager(
            self.trigger,
            adapter_factory=self.factory,
            render_host=TerminalRenderHost.in_process(),
            sweep_interval_seconds=3600,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
            process_tracker=self._track,
        )

    def _track(self, _pid: int) -> FakeTree:
        tree = FakeTree()
        tree.adapter = self.factory.adapters[-1]
        self.trees.append(tree)
        return tree

    async def start(
        self, command: str = "build", *, timeout: float | None = None, run: str = "run-a"
    ) -> tuple[str, FakeTerminalAdapter, FakeTree]:
        terminal_id = await self.manager.spawn_command(
            owner(),
            ["pwsh", "-NoProfile", "-Command", command],
            command=command,
            description="Build it",
            cwd=Path.cwd(),
            env={"PATH": "/bin"},
            timeout_seconds=timeout,
            formatter=format_report,
            origin_run_id=run,
        )
        return terminal_id, self.factory.adapters[-1], self.trees[-1]

    def listed(self) -> list[str]:
        return [info.terminal_id for info in self.manager.list_terminals()]

    def bodies(self) -> list[str]:
        return [kwargs["body"] for _args, kwargs in self.trigger.submissions]


def format_report(report: CommandReport) -> str:
    return (
        f"{report.terminal_id} exit={report.exit_code} stop={report.stop_reason} "
        f"lines={report.transcript.total_lines}"
    )


@pytest_asyncio.fixture
async def harness() -> AsyncIterator[Harness]:
    harness = Harness()
    harness.manager.start()
    try:
        yield harness
    finally:
        harness.trigger.release.set()
        await harness.manager.aclose()


@pytest.mark.asyncio
async def test_finished_command_reports_transcript_exit_code_and_tree_facts(
    harness: Harness,
) -> None:
    terminal_id, adapter, tree = await harness.start()
    # A foreground command is no listed Terminal and wakes nobody.
    assert terminal_id not in harness.listed()

    adapter.emit("compiling\r\n" + "x" * 250 + "\r\nwarning: unused\r\n")
    tree.exits = (ProgramExit("python.exe", 5),)
    tree.shell_exits(1, survivors=())

    outcome = await harness.manager.wait_command(terminal_id, deadline=None, idle_seconds=None)
    report = harness.manager.command_report(terminal_id)
    assert outcome == "exited"
    assert (report.exit_code, report.stop_reason, report.exited) == (1, None, True)
    # Rendered lines, long ones joined again from the 200-column screen.
    assert report.transcript.head == ("compiling", "x" * 250, "warning: unused")
    assert report.nonzero_exits == ("python.exe exited with code 5",)
    assert report.still_running == ()
    assert harness.trigger.submissions == []
    await eventually(lambda: tree.closed)


@pytest.mark.asyncio
async def test_long_transcript_keeps_head_and_tail_and_counts_the_rest(harness: Harness) -> None:
    terminal_id, adapter, tree = await harness.start()
    adapter.emit("".join(f"line {number}\r\n" for number in range(500)))
    await eventually(lambda: harness.manager.command_report(terminal_id).transcript.total_lines > 0)
    tree.shell_exits(0)
    await harness.manager.wait_command(terminal_id, deadline=None, idle_seconds=None)

    transcript = harness.manager.command_report(terminal_id).transcript
    assert transcript.total_lines == 500
    assert (transcript.head[0], transcript.head[-1]) == ("line 0", "line 39")
    assert (transcript.tail[0], transcript.tail[-1]) == ("line 420", "line 499")
    assert transcript.omitted_lines == 380


@pytest.mark.parametrize(
    ("ending", "delivered"),
    [
        ("exit", "exit=0 stop=None"),
        ("operator_kill", "exit=1 stop=user"),
        ("agent_kill", None),
    ],
)
@pytest.mark.asyncio
async def test_handed_off_command_is_listed_and_delivers_its_result_unless_the_agent_killed_it(
    harness: Harness, ending: str, delivered: str | None
) -> None:
    terminal_id, adapter, tree = await harness.start()
    outcome = await harness.manager.wait_command(
        terminal_id, deadline=harness.clock.now, idle_seconds=None
    )
    assert outcome == "deadline"

    report = harness.manager.hand_off_command(terminal_id, deliver=True)
    assert not report.exited
    assert terminal_id in harness.listed()

    if ending == "exit":
        tree.shell_exits(0)
    elif ending == "operator_kill":
        # The shell ignores Ctrl+C; after the grace period the tree is killed.
        stopping = asyncio.create_task(harness.manager.kill_for_operator(terminal_id))
        await eventually(lambda: "\x03" in adapter.writes)
        await eventually(lambda: harness.clock.sleeping)
        await harness.clock.advance(5)
        await stopping
        assert tree.terminated == 1
    else:
        await harness.manager.kill(terminal_id, owner())

    await eventually(lambda: harness.manager.command_report(terminal_id).exited)
    await asyncio.sleep(0)
    expected = [] if delivered is None else [f"{terminal_id} {delivered} lines=0"]
    assert harness.bodies() == expected


@pytest.mark.asyncio
async def test_timeout_interrupts_the_command_and_reports_why_it_stopped(
    harness: Harness,
) -> None:
    terminal_id, adapter, tree = await harness.start(timeout=600)
    waiting = asyncio.create_task(
        harness.manager.wait_command(terminal_id, deadline=None, idle_seconds=None)
    )
    await eventually(lambda: harness.clock.sleeping)
    await harness.clock.advance(600)
    await eventually(lambda: "\x03" in adapter.writes)
    # The shell honours Ctrl+C: nothing has to be killed.
    tree.shell_exits(1)

    assert await waiting == "exited"
    report = harness.manager.command_report(terminal_id)
    assert (report.exit_code, report.stop_reason) == (1, "timeout")
    assert tree.terminated == 0 or tree.running == ()


async def run_clock(harness: Harness, task: asyncio.Task[object], *, until: float) -> None:
    """Advance the fake clock in half seconds while *task* waits on it."""
    while harness.clock.now < until and not task.done():
        for _ in range(200):
            if harness.clock.sleeping or task.done():
                break
            await asyncio.sleep(0.005)
        await harness.clock.advance(0.5)


@pytest.mark.asyncio
async def test_command_is_idle_after_quiet_output_and_cpu_but_not_while_working(
    harness: Harness,
) -> None:
    terminal_id, adapter, tree = await harness.start()
    waiting: asyncio.Task[object] = asyncio.create_task(
        harness.manager.wait_command(terminal_id, deadline=None, idle_seconds=15)
    )
    adapter.emit("Name: ")
    await run_clock(harness, waiting, until=3)
    # CPU time without output is work: the quiet period starts again.
    tree.cpu_seconds += 5
    await run_clock(harness, waiting, until=25)
    assert not waiting.done()

    await run_clock(harness, waiting, until=40)
    assert waiting.result() == "idle"
    assert 30 <= harness.clock.now <= 34
    assert "Name:" in await harness.manager.command_screen(terminal_id, 5)


@pytest.mark.asyncio
async def test_survivors_keep_a_finished_command_live_until_they_are_killed(
    harness: Harness,
) -> None:
    terminal_id, _adapter, tree = await harness.start()
    survivor = RunningProcess(42, "server.exe")
    tree.shell_exits(0, survivors=(survivor,))

    await harness.manager.wait_command(terminal_id, deadline=None, idle_seconds=None)
    report = harness.manager.command_report(terminal_id)
    assert (report.exit_code, report.still_running) == (0, (survivor,))
    session = harness.manager._get(terminal_id)
    assert not session.finished

    await harness.manager.kill(terminal_id, owner())
    assert tree.terminated == 1
    await eventually(lambda: session.finished)
    assert harness.manager.command_report(terminal_id).still_running == ()


@pytest.mark.asyncio
async def test_cancelled_run_kills_its_commands_and_starts_no_new_ones(harness: Harness) -> None:
    terminal_id, _adapter, tree = await harness.start(run="run-x")
    harness.manager.hand_off_command(terminal_id, deliver=True)

    await harness.manager.cancel_run("run-x")
    assert tree.terminated == 1
    assert harness.manager.command_report(terminal_id).stop_reason == "run_cancelled"
    with pytest.raises(TerminalClosedError):
        await harness.start(run="run-x")
    assert harness.bodies() == []

    harness.manager.release_run("run-x")
    await harness.start(run="run-x")


@pytest.mark.asyncio
async def test_shutdown_submits_the_stopped_result_of_handed_off_commands(
    harness: Harness,
) -> None:
    handed_off, _adapter, _tree = await harness.start()
    harness.manager.hand_off_command(handed_off, deliver=True)
    foreground, _other, _other_tree = await harness.start()

    await harness.manager.shutdown_commands()

    assert harness.bodies() == [f"{handed_off} exit=1 stop=shutdown lines=0"]
    assert harness.manager.command_report(foreground).stop_reason == "shutdown"
