"""Shell commands as Terminal Sessions: outcome facts, hand-off, delivery, stops, idleness,
and the terminal Tool following them up."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

from core.projects import ProjectStore
from core.tools._terminal_command import CommandReport
from core.tools._terminal_process_tree import ProgramExit, RunningProcess
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.shell import SHELL_HANDOFF_SECONDS, SHELL_TOOL_NAME, register_shell_tool
from core.tools.terminal import TERMINAL_TOOL_NAME, register_terminal_tool
from core.tools.terminal_manager import (
    TerminalClosedError,
    TerminalManager,
    TerminalRenderHost,
)
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure_for_exception
from tests.core.tools.terminal_manager_helpers import (
    AdapterFactory,
    FakeClock,
    FakeTerminalAdapter,
    FakeTree,
    PendingTriggerService,
    TrackedExecutor,
    eventually,
    owner,
    settle,
)


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

    outcome = await harness.manager.wait_command(terminal_id, seconds=None, idle_seconds=None)
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
    await harness.manager.wait_command(terminal_id, seconds=None, idle_seconds=None)

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
    outcome = await harness.manager.wait_command(terminal_id, seconds=0, idle_seconds=None)
    assert outcome == "deadline"

    report = harness.manager.hand_off_command(terminal_id, deliver=True)
    assert not report.exited
    assert terminal_id in harness.listed()

    if ending == "exit":
        waiting = asyncio.create_task(harness.manager.wait(terminal_id, owner(), seconds=60))
        tree.shell_exits(0)
        # A wait that sees the exit still leaves its delivery to the Tool,
        # which acknowledges it only once its own result is persisted.
        assert await waiting == "exited"
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

    await harness.manager.wait_finished(terminal_id)
    await asyncio.sleep(0)
    expected = [] if delivered is None else [f"{terminal_id} {delivered} lines=0"]
    assert harness.bodies() == expected


@pytest.mark.asyncio
async def test_timeout_interrupts_the_command_and_reports_why_it_stopped(
    harness: Harness,
) -> None:
    terminal_id, adapter, tree = await harness.start(timeout=600)
    waiting = asyncio.create_task(
        harness.manager.wait_command(terminal_id, seconds=None, idle_seconds=None)
    )
    tree.exits = (ProgramExit("lint.exe", 2),)
    await eventually(lambda: harness.clock.sleeping)
    await harness.clock.advance(600)
    await eventually(lambda: "\x03" in adapter.writes)
    # The shell honours Ctrl+C: nothing has to be killed. A child ending from
    # the Ctrl+C is no failure of the command.
    tree.exits = (*tree.exits, ProgramExit("python.exe", 0xC000013A))
    tree.shell_exits(1)

    assert await waiting == "exited"
    report = harness.manager.command_report(terminal_id)
    assert (report.exit_code, report.stop_reason) == (1, "timeout")
    assert report.nonzero_exits == ("lint.exe exited with code 2",)
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
        harness.manager.wait_command(terminal_id, seconds=None, idle_seconds=15)
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

    await harness.manager.wait_command(terminal_id, seconds=None, idle_seconds=None)
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


# The terminal Tool on commands the shell Tool left running


class Tools(Harness):
    """The shell and terminal Tools over the harness's manager, as the executor runs them."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__()
        self.tmp_path = tmp_path
        self.registry = ToolRegistry()
        register_shell_tool(self.registry, self.manager)
        register_terminal_tool(self.registry, self.manager, ProjectStore(tmp_path))
        # Callbacks the calls registered to run once their results are kept.
        self.persisted: list[Callable[[], None]] = []
        self.executor = TrackedExecutor()

    async def run_clock(self, task: asyncio.Future[Any], *, until: float) -> None:
        """Advance fake time in half seconds while *task* waits on it."""
        while self.clock.now < until and not task.done():
            await settle(self.clock, self.executor, task)
            await self.clock.advance(0.5)

    async def call(
        self, tool: str, arguments: JsonObject, *, depth: int = 0, session: str = "session-a"
    ) -> JsonObject:
        context = ToolContext(
            agent_id="agent-a",
            session_id=session,
            run_id="run-a",
            tool_call_id="call-a",
            tool_name=tool,
            tool_call_index=0,
            workspace=self.tmp_path,
            vbot_root=self.tmp_path,
            data_root=self.tmp_path,
            cwd=self.tmp_path,
            project_id="project-a",
            nesting_depth=depth,
            result_persisted_hook=self.persisted.append,
        )
        try:
            return await self.registry.dispatch(
                context, arguments, [SHELL_TOOL_NAME, TERMINAL_TOOL_NAME]
            )
        except Exception as error:
            return tool_failure_for_exception(tool, error)

    async def terminal(self, arguments: JsonObject, **options: Any) -> JsonObject:
        result = await self.call(TERMINAL_TOOL_NAME, arguments, **options)
        assert result["ok"] is True, result
        data: JsonObject = result["data"]
        return data

    async def background(self, command: str) -> tuple[str, FakeTerminalAdapter, FakeTree]:
        """Run *command* in background mode; return its terminal, adapter and tree."""
        result = await self.call(SHELL_TOOL_NAME, {"command": command, "mode": "background"})
        assert result["ok"] is True, result
        return str(result["data"]["terminal_id"]), self.factory.adapters[-1], self.trees[-1]

    def keep_results(self) -> None:
        callbacks, self.persisted = self.persisted, []
        for callback in callbacks:
            callback()

    def delivered(self) -> list[str]:
        """Bodies submitted for delivery and not withdrawn again."""
        withdrawn = {kwargs["notice_id"] for _args, kwargs in self.trigger.cancellations}
        return [
            kwargs["body"]
            for _args, kwargs in self.trigger.submissions
            if kwargs["notice_id"] not in withdrawn
        ]


@pytest_asyncio.fixture
async def tools(tmp_path: Path) -> AsyncIterator[Tools]:
    harness = Tools(tmp_path)
    asyncio.get_running_loop().set_default_executor(harness.executor)
    harness.manager.start()
    try:
        yield harness
    finally:
        harness.trigger.release.set()
        await harness.manager.aclose()


def terminal_call(action: str, terminal_id: str) -> str:
    return "terminal " + json.dumps({"action": action, "terminal_id": terminal_id})


@pytest.mark.asyncio
async def test_terminal_reads_answers_and_waits_for_a_command_in_the_shell_result_shape(
    tools: Tools,
) -> None:
    terminal_id, adapter, _tree = await tools.background("serve")
    adapter.emit("Server LISTENING on :8080\r\nContinue? ")
    await eventually(lambda: shows(tools, terminal_id, "Continue?"))

    # Only the Session that ran the command sees it.
    assert (await tools.terminal({"action": "list"}))["terminals"] == [
        {"terminal_id": terminal_id, "program": "serve", "state": "running", "attached": "here"}
    ]
    assert (await tools.terminal({"action": "list"}, session="session-b"))["terminals"] == []
    hidden = await tools.call(
        TERMINAL_TOOL_NAME, {"action": "status", "terminal_id": terminal_id}, session="session-b"
    )
    assert hidden["error"]["code"] == "terminal_not_found"

    status = await tools.terminal({"action": "status", "terminal_id": terminal_id, "lines": 50})
    assert (status["status"], status["terminal_id"], status["output"]) == (
        "running",
        terminal_id,
        "Server LISTENING on :8080\nContinue?",
    )
    assert status["note"] == (
        "lines and start_line page an interactive terminal's screen; a command's result shows "
        "its output, and log_file, when present, holds all of it."
    )
    assert status["next"].startswith(f"The command keeps running in terminal {terminal_id};")

    # Output printed before the wait counts for its pattern.
    matched = await tools.terminal(
        {"action": "wait", "terminal_id": terminal_id, "pattern": r"listening on :\d+"}
    )
    assert (matched["status"], matched["wait_ended"]) == ("running", "matched")

    typed = await tools.terminal(
        {"action": "input", "terminal_id": terminal_id, "text": "y", "key": "enter"}
    )
    assert typed == {
        "terminal_id": terminal_id,
        "characters_sent": 2,
        "key": "enter",
        "state": "running",
        "next": "Its result arrives as a new message when it exits. To wait for it now, call "
        f"{terminal_call('wait', terminal_id)}.",
    }
    assert adapter.writes[-2:] == ["y", "\r"]

    # A command stays attached to the Session that ran it, so its result arrives there.
    attached = await tools.terminal({"action": "attach", "terminal_id": terminal_id})
    assert attached["note"] == (
        "A command stays attached to the Session that ran it; nothing changed."
    )
    detached = await tools.call(
        TERMINAL_TOOL_NAME, {"action": "detach", "terminal_id": terminal_id}
    )
    assert detached["error"] == {
        "code": "command_terminal",
        "message": f"{terminal_id} runs a command that {SHELL_MODEL_NAME} left running, and a "
        "command stays attached to the Session that ran it so its result arrives there. Nothing "
        f"was changed. To stop it, call {terminal_call('kill', terminal_id)}.",
        "retryable": False,
    }


@pytest.mark.parametrize("kept", [True, False], ids=["result-kept", "result-lost"])
@pytest.mark.asyncio
async def test_a_command_exit_a_kept_result_showed_is_not_delivered_again(
    tools: Tools, kept: bool
) -> None:
    terminal_id, adapter, tree = await tools.background("build")
    adapter.emit("compiled\r\n")
    waiting = asyncio.ensure_future(tools.terminal({"action": "wait", "terminal_id": terminal_id}))
    await eventually(lambda: tools.clock.sleeping)
    tree.shell_exits(0)

    exited = await waiting
    assert exited == {
        "status": "exited",
        "exit_code": 0,
        "output": "compiled",
        "wait_ended": "exited",
    }
    if kept:
        tools.keep_results()
    await tools.manager.wait_finished(terminal_id)
    await asyncio.sleep(0)

    delivered = tools.delivered()
    assert len(delivered) == (0 if kept else 1)
    if not kept:
        assert delivered[0].startswith(f"The command in terminal {terminal_id} (build) exited")


@pytest.mark.asyncio
async def test_kill_stops_a_command_and_returns_its_stopped_result(tools: Tools) -> None:
    terminal_id, adapter, tree = await tools.background("watch")
    adapter.emit("watching\r\n")
    await eventually(lambda: shows(tools, terminal_id, "watching"))

    result = await tools.terminal({"action": "kill", "terminal_id": terminal_id})

    assert (result["status"], result["stopped_because"], result["output"]) == (
        "stopped",
        "you stopped it.",
        "watching",
    )
    assert tree.terminated == 1
    tools.keep_results()
    await asyncio.sleep(0)
    assert tools.delivered() == []


@pytest.mark.parametrize("ending", ["quiet", "timeout"])
@pytest.mark.asyncio
async def test_command_wait_ends_quiet_only_after_output_during_the_wait(
    tools: Tools, ending: str
) -> None:
    terminal_id, adapter, _tree = await tools.background("deploy")
    # Output from before the wait does not make the command quiet.
    adapter.emit("Starting\r\n")
    await eventually(lambda: shows(tools, terminal_id, "Starting"))
    waiting: asyncio.Task[object] = asyncio.ensure_future(
        tools.terminal({"action": "wait", "terminal_id": terminal_id, "timeout": 40})
    )
    if ending == "quiet":
        await eventually(lambda: tools.clock.sleeping)
        adapter.emit("Password: ")
    await tools.run_clock(waiting, until=45)

    result = cast(JsonObject, waiting.result())
    assert (result["status"], result["wait_ended"]) == ("running", ending)
    if ending == "quiet":
        # Printed output, then nothing and no CPU for 15 seconds.
        assert 16 <= tools.clock.now <= 17
        assert result["next"].startswith(
            f"The command in terminal {terminal_id} has printed nothing for 15 seconds"
        )
    else:
        assert tools.clock.now >= 40


@pytest.mark.parametrize("depth", [0, 1])
@pytest.mark.asyncio
async def test_input_to_a_command_says_whether_its_result_arrives_on_its_own(
    tools: Tools, depth: int
) -> None:
    call: asyncio.Task[object] = asyncio.ensure_future(
        tools.call(SHELL_TOOL_NAME, {"command": "read-name"}, depth=depth)
    )
    await eventually(lambda: bool(tools.trees))
    tools.factory.adapters[-1].emit("Name: ")
    await tools.run_clock(call, until=SHELL_HANDOFF_SECONDS)
    terminal_id = cast(JsonObject, call.result())["data"]["terminal_id"]

    typed = await tools.terminal(
        {"action": "input", "terminal_id": terminal_id, "text": "Ada", "key": "enter"},
        depth=depth,
    )

    wait = terminal_call("wait", terminal_id)
    assert typed["next"] == (
        f"Its result arrives as a new message when it exits. To wait for it now, call {wait}."
        if depth == 0
        else f"To follow it, call {wait}."
    )


async def shows(tools: Tools, terminal_id: str, text: str) -> bool:
    return text in await tools.manager.command_screen(terminal_id, 5)
