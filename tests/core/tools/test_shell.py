"""The shell Tool: results, hand-off, delivery, stops, environment and definitions."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import re
import shutil
import sys
import threading
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from core.projects import ProjectNotFoundError
from core.storage.temp_files import TemporaryFileManager
from core.tools import shell as shell_module
from core.tools import terminal_manager
from core.tools._terminal_process_tree import ProgramExit, RunningProcess
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.shell import (
    SHELL_HANDOFF_SECONDS,
    SHELL_TOOL_DESCRIPTION,
    SHELL_TOOL_NAME,
    SHELL_TOOL_PARAMETERS,
    background_command_statuses,
    command_terminal_result,
    register_shell_tool,
    shell_detail_blocks,
)
from core.tools.terminal_manager import TerminalManager, TerminalRenderHost
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure_for_exception
from core.tools.update_handoff import (
    UpdateHandoffs,
    UpdateHandoffUnavailableError,
    read_update_handoff_ticket,
)
from core.utils.paths import model_path
from tests.core.tools.terminal_manager_helpers import (
    AdapterFactory,
    FakeClock,
    FakeTerminalAdapter,
    FakeTree,
    PendingTriggerService,
    TrackedExecutor,
    eventually,
    settle,
)
from tests.core.tools.tools_test_support import dispatch_as_executor


class FakeProjects:
    """Projects by id, each with its directory."""

    def __init__(self, directories: dict[str, Path]) -> None:
        self.directories = directories

    def get(self, project_id: str) -> Any:
        if project_id not in self.directories:
            raise ProjectNotFoundError(f"Project not found: {project_id}")
        return SimpleNamespace(project_id=project_id, cwd=str(self.directories[project_id]))


class Shell:
    """The shell Tool over a TerminalManager with fake terminals, trees and time."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        update_handoffs: UpdateHandoffs | None = None,
        projects: FakeProjects | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.clock = FakeClock()
        self.trigger = PendingTriggerService()
        self.factory = AdapterFactory()
        self.trees: list[FakeTree] = []
        self.manager = TerminalManager(
            self.trigger,
            adapter_factory=self.factory,
            render_host=TerminalRenderHost.in_process(),
            temporary_files=TemporaryFileManager(
                tmp_path / "temp", retention={"commands": timedelta(hours=1)}
            ),
            sweep_interval_seconds=3600,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
            process_tracker=self._track,
        )
        self.registry = ToolRegistry()
        register_shell_tool(
            self.registry,
            self.manager,
            credential_resolver=lambda name: f"value-of-{name}",
            update_handoffs=update_handoffs,
            projects=projects,  # type: ignore[arg-type]
        )
        self.cancel_callbacks: list[Callable[[], Any]] = []
        self.background_callbacks: list[Callable[[], bool]] = []
        # Callbacks the call registered to run once its Tool Result is persisted.
        self.persisted: list[Callable[[], None]] = []
        # Live output events of the calls: terminal id and screen.
        self.live_output: list[JsonObject] = []
        self.executor = TrackedExecutor()

    def _track(self, _pid: int) -> FakeTree:
        tree = FakeTree()
        tree.adapter = self.factory.adapters[-1]
        self.trees.append(tree)
        return tree

    def context(self) -> ToolContext:
        return ToolContext(
            agent_id="agent-a",
            session_id="session-a",
            run_id="run-a",
            tool_call_id="call-a",
            tool_name=SHELL_TOOL_NAME,
            tool_call_index=0,
            workspace=self.tmp_path,
            vbot_root=self.tmp_path,
            data_root=self.tmp_path,
            cwd=self.tmp_path,
            project_id="project-a",
            tool_settings={"bash": {"allowed_env": ["API_TOKEN"]}},
            cancel_registration_hook=self.cancel_callbacks.append,
            background_registration_hook=self.background_callbacks.append,
            result_persisted_hook=self.persisted.append,
            emit_hook=lambda _event, payload: self.live_output.append(payload),
        )

    def call(self, arguments: JsonObject, *, terminal: bool = True) -> asyncio.Task[JsonObject]:
        """Call the shell as the executor does, for an Agent with or without the terminal Tool."""
        allowed = [SHELL_TOOL_NAME, "terminal"] if terminal else [SHELL_TOOL_NAME]
        return asyncio.ensure_future(dispatch(self.registry, self.context(), arguments, allowed))

    async def started(self) -> tuple[FakeTerminalAdapter, FakeTree]:
        await eventually(lambda: bool(self.trees))
        return self.factory.adapters[-1], self.trees[-1]

    async def shows(self, text: str) -> None:
        """Wait until the running command's screen shows *text*.

        A reader thread renders what the fake terminal prints, and fake time
        does not wait for it.
        """
        await eventually(lambda: bool(self.live_output))
        terminal_id = self.live_output[0]["terminal_id"]

        async def shown() -> bool:
            return text in await self.manager.command_screen(terminal_id, 1)

        await eventually(shown)

    async def run_clock(self, task: asyncio.Future[Any], *, until: float) -> None:
        """Advance fake time in half seconds while *task* waits on it."""
        while self.clock.now < until and not task.done():
            await settle(self.clock, self.executor, task)
            await self.clock.advance(0.5)

    async def run_clock_until_delivered(self, *, until: float) -> None:
        """Advance fake time in half seconds until a result is delivered."""
        while self.clock.now < until and not self.bodies():
            await settle(self.clock, self.executor)
            await self.clock.advance(0.5)
        await eventually(lambda: bool(self.bodies()))

    def bodies(self) -> list[str]:
        return [kwargs["body"] for _args, kwargs in self.trigger.submissions]


@contextlib.asynccontextmanager
async def started_shell(tmp_path: Path, **options: Any) -> AsyncIterator[Shell]:
    harness = Shell(tmp_path, **options)
    asyncio.get_running_loop().set_default_executor(harness.executor)
    harness.manager.start()
    try:
        yield harness
    finally:
        harness.trigger.release.set()
        await harness.manager.aclose()


@pytest_asyncio.fixture
async def shell(tmp_path: Path) -> AsyncIterator[Shell]:
    async with started_shell(tmp_path) as harness:
        yield harness


async def dispatch(
    registry: ToolRegistry, context: ToolContext, arguments: JsonObject, allowed: list[str]
) -> JsonObject:
    try:
        return await registry.dispatch(context, arguments, allowed)
    except Exception as error:
        return tool_failure_for_exception(context.tool_name, error)


def data(result: JsonObject) -> JsonObject:
    assert result["ok"] is True, result
    payload: JsonObject = result["data"]
    return payload


_TERMINAL_CALL = re.compile(r"terminal (\{.*?\})")


def terminal_calls(text: str) -> list[JsonObject]:
    """The complete terminal calls a text names, parsed as the Agent would send them."""
    return [json.loads(call) for call in _TERMINAL_CALL.findall(text)]


_LOG_SENTENCE = re.compile(r" The command's complete output is written live to (\S+)\.(?=\s|$)")


def log_sentence(text: str) -> str:
    """The sentence of a running command's text that names its existing log file."""
    match = _LOG_SENTENCE.search(text)
    assert match is not None, text
    assert Path(match[1]).is_file()
    return match[0]


@pytest.mark.parametrize(
    ("exits", "exit_code", "failed_programs"),
    [
        ((ProgramExit("python.exe", 5),), 1, ["python.exe exited with code 5"]),
        # One failed program whose code is the exit code repeats it.
        ((ProgramExit("git.exe", 128),), 128, None),
        ((ProgramExit("app.exe", 0xC0000005),), 0xC0000005, None),
        (
            (ProgramExit("git.exe", 128), ProgramExit("python.exe", 128)),
            128,
            ["git.exe exited with code 128", "python.exe exited with code 128"],
        ),
    ],
)
@pytest.mark.asyncio
async def test_finished_command_reports_output_exit_code_failed_programs_and_environment(
    shell: Shell,
    exits: tuple[ProgramExit, ...],
    exit_code: int,
    failed_programs: list[str] | None,
) -> None:
    call = shell.call({"command": "build --all", "env_keys": ["API_TOKEN"], "env": {"MODE": "ci"}})
    adapter, tree = await shell.started()
    adapter.emit("compiling\r\ndone\r\n")
    tree.exits = exits
    tree.shell_exits(exit_code)

    result = data(await call)
    expected: JsonObject = {"status": "exited", "exit_code": exit_code, "output": "compiling\ndone"}
    if failed_programs is not None:
        expected["failed_programs"] = failed_programs
    assert result == expected
    argv, cwd, env, _rows, _columns = shell.factory.calls[0]
    assert "build --all" in argv[-1]
    assert argv[1:3] == (
        ["-NoProfile", "-Command"] if sys.platform == "win32" else ["-c", argv[-1]]
    )
    assert cwd == shell.tmp_path
    assert (env["VBOT_RUN_AGENT_ID"], env["VBOT_RUN_PROJECT_ID"]) == ("agent-a", "project-a")
    assert (env["API_TOKEN"], env["MODE"]) == ("value-of-API_TOKEN", "ci")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert shell.manager.list_terminals() == []


@pytest.mark.asyncio
async def test_leniently_read_arguments_run_with_a_note(shell: Shell) -> None:
    call = shell.call({"cmd": "ls", "timeout": 60000, "env_keys": ["PATH"]})
    _adapter, tree = await shell.started()
    tree.shell_exits(0)

    assert data(await call)["notes"] == [
        "timeout 60000 was read as milliseconds (60 s); timeout takes seconds.",
        "PATH is not a granted credential, so the command sees the value it inherits; "
        "env_keys is only for granted credentials.",
    ]


@pytest.mark.asyncio
async def test_each_shell_start_resolves_its_fresh_environment_off_loop(
    shell: Shell, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()
    prepared: list[int] = []
    resolved: list[tuple[int, str | None]] = []
    paths = iter(("first-path", "changed-path"))

    def environment(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        prepared.append(threading.get_ident())
        return {"PATH": next(paths)}

    def executable(_name: str, *, path: str | None = None) -> str:
        resolved.append((threading.get_ident(), path))
        return str(shell.tmp_path / str(path) / "fixture-shell")

    monkeypatch.setattr(shell_module, "command_environment", environment)
    monkeypatch.setattr(shell_module.shutil, "which", executable)
    for path in ("first-path", "changed-path"):
        result = data(await shell.call({"command": "build", "mode": "background"}))
        argv, _cwd, env, _rows, _columns = shell.factory.calls[-1]
        assert argv[0] == str(shell.tmp_path / path / "fixture-shell")
        assert env["PATH"] == path
        shell.trees[-1].shell_exits(0)
        await shell.manager.wait_finished(result["terminal_id"])

    assert [path for _thread, path in resolved] == ["first-path", "changed-path"]
    assert prepared == [thread for thread, _path in resolved]
    assert all(thread != loop_thread for thread in prepared)


@pytest.mark.asyncio
async def test_long_output_keeps_head_and_tail_and_points_to_the_log_file(shell: Shell) -> None:
    call = shell.call({"command": "test"})
    adapter, tree = await shell.started()
    adapter.emit("".join(f"line {number}\r\n" for number in range(500)))
    await asyncio.sleep(0.05)
    tree.shell_exits(0)

    result = data(await call)
    lines = result["output"].splitlines()
    assert (lines[0], lines[-1]) == ("line 0", "line 499")
    # The marker names the file with the complete output.
    omitted = next(line for line in lines if line.startswith("[... "))
    marker = re.fullmatch(
        r"\[\.\.\. 380 lines omitted; the complete output is in (\S+) \.\.\.\]", omitted
    )
    assert marker is not None, omitted
    assert "log_file" not in result
    log = Path(marker[1]).read_text(encoding="utf-8").splitlines()
    assert len(log) == 500


@pytest.mark.asyncio
async def test_running_output_is_the_newest_rows_and_its_log_file_has_every_row(
    shell: Shell,
) -> None:
    # 120 lines on a 50-row screen: 71 scrolled off into the transcript, the rest
    # are screen rows.
    call = shell.call({"command": "build"})
    adapter, _tree = await shell.started()
    adapter.emit("".join(f"row {number}\r\n" for number in range(1, 121)))
    await shell.shows("row 120")
    await shell.clock.advance(1)
    # Rows that were on screen scroll off now: the log file holds each row once.
    adapter.emit("".join(f"row {number}\r\n" for number in range(121, 151)))
    await shell.shows("row 150")
    await shell.run_clock(call, until=SHELL_HANDOFF_SECONDS)

    result = data(await call)
    rows = [f"row {number}" for number in range(1, 151)]
    assert result["status"] == "running"
    # The result shows the newest rows, the log file every row once, and both
    # the marker and the next text name that file.
    log = log_sentence(result["next"])
    path = _LOG_SENTENCE.search(log)[1]  # type: ignore[index]
    assert result["output"].splitlines() == [
        f"[... 130 earlier lines omitted; the complete output is in {path} ...]",
        *rows[-20:],
    ]
    assert Path(path).read_text(encoding="utf-8").splitlines() == rows


@pytest.mark.asyncio
async def test_idle_command_continues_as_terminal_and_its_result_is_delivered(
    shell: Shell,
) -> None:
    # Silent and without CPU use, as a command waiting for input is.
    call = shell.call({"command": "read-name"})
    _adapter, tree = await shell.started()
    await shell.run_clock(call, until=SHELL_HANDOFF_SECONDS)

    result = data(await call)
    terminal_id, next_text = result["terminal_id"], result["next"]
    assert result["status"] == "running"
    assert next_text.startswith(f"The command in terminal {terminal_id} has printed nothing")
    assert re.search(r"; its 600-second timeout stops it in 9 minutes \d+ seconds\.", next_text)
    # Answering comes first, without an answer for the Agent to copy; then exact calls.
    assert f'answer it with terminal action "input", terminal_id "{terminal_id}"' in next_text
    assert '"text"' not in next_text
    assert terminal_calls(next_text) == [{"action": "kill", "terminal_id": terminal_id}]
    assert "Its result arrives as a new message when it exits." in next_text
    assert terminal_id in [info.terminal_id for info in shell.manager.list_terminals()]

    tree.shell_exits(0)
    # The finishing session starts the delivery before it reports the end.
    await shell.manager.wait_finished(terminal_id)
    assert len(shell.bodies()) == 1


@pytest.mark.parametrize(
    ("timeout", "limit"),
    [
        # The foreground default counts from the start, so 510 of 600 seconds remain.
        (None, "its 600-second timeout stops it in 8 minutes 30 seconds"),
        (0, "it has no timeout"),
    ],
)
@pytest.mark.asyncio
async def test_long_command_is_handed_off_and_its_result_delivered(
    shell: Shell, timeout: int | None, limit: str
) -> None:
    arguments: JsonObject = {"command": "sleep"}
    if timeout is not None:
        arguments["timeout"] = timeout
    # Without the terminal Tool, quiet commands are not handed off as idle.
    call = shell.call(arguments, terminal=False)
    _adapter, tree = await shell.started()
    await shell.run_clock(call, until=SHELL_HANDOFF_SECONDS + 5)

    result = data(await call)
    assert result["status"] == "running"
    assert shell.clock.now >= SHELL_HANDOFF_SECONDS
    assert result["next"] == (
        "The command was still running after 90 seconds and keeps running in the background; "
        f"{limit}.{log_sentence(result['next'])} Its result arrives as a new message when it "
        "exits. Continue other work, or "
        "end your turn if your next step needs the result; do not start it again, and do not "
        "sleep or poll for its result."
    )
    assert terminal_calls(result["next"]) == []

    await shell.clock.advance(30)
    tree.shell_exits(0)
    await eventually(lambda: bool(shell.bodies()))
    status = shell.bodies()[0].splitlines()[0]
    assert re.fullmatch(
        rf"The command in terminal {result['terminal_id']} \(sleep\) exited with code 0 "
        r"after 2 minutes\.",
        status,
    )


@pytest.mark.asyncio
async def test_timeout_stops_the_command_and_says_how_to_let_it_finish(shell: Shell) -> None:
    call = shell.call({"command": "hang", "timeout": 5})
    adapter, tree = await shell.started()
    await shell.run_clock(call, until=5)
    await eventually(lambda: "\x03" in adapter.writes)
    tree.shell_exits(1)

    result = data(await call)
    assert result["status"] == "stopped"
    assert result["stopped_because"].startswith("it was still running at its 5-second timeout")
    assert result["stopped_because"].endswith(
        "or 0 for no limit; run servers and watchers with mode background."
    )
    assert result["exit_code"] == 1


@pytest.mark.parametrize("request_kind", ["cancel", "background"])
@pytest.mark.asyncio
async def test_user_can_stop_the_command_or_move_it_to_the_background(
    shell: Shell, request_kind: str
) -> None:
    call = shell.call({"command": "build"})
    adapter, tree = await shell.started()
    await eventually(lambda: bool(shell.cancel_callbacks))
    await shell.clock.advance(12)

    if request_kind == "cancel":
        stopping = shell.cancel_callbacks[0]()
        await eventually(lambda: "\x03" in adapter.writes)
        tree.shell_exits(1)
        await stopping
        result = data(await call)
        assert (result["status"], result["stopped_because"]) == (
            "stopped",
            "the user stopped it after 12 seconds.",
        )
    else:
        assert shell.background_callbacks[0]() is True
        result = data(await call)
        assert result["status"] == "running"
        assert result["next"].startswith(
            "The user moved the command to the background after 12 seconds. It keeps running "
            f"in terminal {result['terminal_id']}; its 600-second timeout stops it in "
            f"9 minutes 48 seconds.{log_sentence(result['next'])} Its result arrives as a new "
            "message when it exits."
        )
        tree.shell_exits(0)
        await eventually(lambda: bool(shell.bodies()))


@pytest.mark.parametrize(
    ("timeout", "limit"),
    [
        # A server runs until it exits: background mode has no default timeout.
        (None, "it has no timeout"),
        (90, "its 90-second timeout stops it in 1 minute 30 seconds"),
    ],
)
@pytest.mark.asyncio
async def test_background_mode_returns_at_once(
    shell: Shell, timeout: int | None, limit: str
) -> None:
    arguments: JsonObject = {"command": "serve", "mode": "background"}
    if timeout is not None:
        arguments["timeout"] = timeout
    running = data(await shell.call(arguments))
    terminal_id = running["terminal_id"]
    assert running["status"] == "running"
    wait = json.dumps({"action": "wait", "terminal_id": terminal_id})
    assert running["next"] == (
        f"The command runs in the background in terminal {terminal_id}; "
        f"{limit}.{log_sentence(running['next'])} Its result arrives as a new message when it "
        "exits. If it runs until stopped, such as a server, "
        f"and your next step needs it ready, call terminal {wait} with pattern set to a line "
        "it prints when ready. Otherwise continue other work, or end your turn if your next "
        "step needs the result; do not start it again, and do not sleep or poll for its result."
    )
    assert len(shell.manager.list_terminals()) == 1

    _adapter, tree = await shell.started()
    tree.shell_exits(0)
    await eventually(lambda: bool(shell.bodies()))
    assert shell.bodies()[0].startswith(f"The command in terminal {terminal_id} (serve) exited")


@pytest.mark.asyncio
async def test_quiet_processes_left_running_are_reported_and_listed(shell: Shell) -> None:
    call = shell.call({"command": "start-server"})
    adapter, tree = await shell.started()
    tree.shell_exits(0, survivors=(RunningProcess(42, "server.exe"),))
    # Fake time counts from when the session saw the shell's output end.
    await eventually(lambda: adapter.output_ended)

    # A server the command started and that waits quietly holds the call
    # only briefly: one second for the quiet baseline, two quiet seconds.
    await shell.run_clock(call, until=10)
    assert 3 <= shell.clock.now <= 4
    result = data(await call)
    assert result["still_running"] == ["server.exe (pid 42)"]
    # The foreground's default timeout still stops them.
    assert re.match(
        r"The shell exited, but processes the command started still run: server\.exe "
        r"\(pid 42\); the command's 600-second timeout stops them in 9 minutes 5[67] "
        r"seconds\. "
        r"Stop them with ",
        result["next"],
    )
    terminal_id = result["terminal_id"]
    assert terminal_calls(result["next"]) == [{"action": "kill", "terminal_id": terminal_id}]
    assert terminal_id in [info.terminal_id for info in shell.manager.list_terminals()]


@pytest.mark.parametrize("mode", ["foreground", "background"])
@pytest.mark.asyncio
async def test_working_processes_left_running_hold_the_command_until_their_output_is_complete(
    shell: Shell, mode: str
) -> None:
    # A GUI-subsystem program run from PowerShell: the shell exits at once, and
    # the program prints its answer to the shell's terminal afterwards.
    call = shell.call({"command": "watchtower due", "mode": mode})
    adapter, tree = await shell.started()
    if mode == "background":
        running = data(await call)
        assert running["status"] == "running"
        terminal_id = running["terminal_id"]
    else:
        await eventually(lambda: bool(shell.live_output))
        terminal_id = shell.live_output[0]["terminal_id"]
    tree.shell_exits(0, survivors=(RunningProcess(42, "watchtower.exe"),), survivors_print=True)
    for _second in range(10):
        tree.cpu_seconds += 1
        await shell.run_clock(call, until=shell.clock.now + 1)
    assert not shell.bodies()
    if mode == "foreground":
        assert not call.done()
        # The call reports the command running, without the processes' output yet.
        status = await command_terminal_result(shell.manager, shell.context(), terminal_id)
        assert status["status"] == "running"

    # More lines than the screen holds: the last ones never scroll off it.
    adapter.emit("".join(f"line {number}\r\n" for number in range(70)))

    async def rendered() -> bool:
        return "line 69" in await shell.manager.command_screen(terminal_id, 1)

    await eventually(rendered)
    tree.running = ()
    lines = [f"line {number}" for number in range(70)]
    if mode == "foreground":
        await shell.run_clock(call, until=shell.clock.now + 10)
        result = data(await call)
        assert result["status"] == "exited"
        assert result["output"].splitlines() == lines
        assert "still_running" not in result
        assert "terminal_id" not in result
    else:
        await shell.run_clock_until_delivered(until=shell.clock.now + 10)
        body = shell.bodies()[0]
        assert body.startswith(f"The command in terminal {terminal_id} (watchtower due)")
        assert "\n".join(lines) in body
        assert "Processes it started still run" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "timeout", "until"),
    [
        (True, None, "."),
        (False, 60, "; the command's 60-second timeout stops them in 5"),
    ],
)
async def test_delivery_names_processes_left_running_and_how_to_stop_them(
    shell: Shell, terminal: bool, timeout: int | None, until: str
) -> None:
    arguments: JsonObject = {"command": "start-server", "mode": "background"}
    if timeout is not None:
        arguments["timeout"] = timeout
    running = data(await shell.call(arguments, terminal=terminal))
    adapter, tree = await shell.started()
    tree.shell_exits(0, survivors=(RunningProcess(42, "server.exe"),))
    # Fake time counts from when the session saw the shell's output end.
    await eventually(lambda: adapter.output_ended)
    # Delivered once they went quiet: one second for the baseline, two quiet seconds,
    # checked each second.
    await shell.run_clock_until_delivered(until=10)
    assert 3 <= shell.clock.now <= 5

    line = next(
        line for line in shell.bodies()[0].splitlines() if line.startswith("Processes it started")
    )
    assert line.startswith(f"Processes it started still run: server.exe (pid 42){until}")
    kill = {"action": "kill", "terminal_id": running["terminal_id"]}
    assert terminal_calls(line) == ([kill] if terminal else [])
    # The processes stay listed, so the call the delivery names finds them.
    assert running["terminal_id"] in [info.terminal_id for info in shell.manager.list_terminals()]


_MISSING_MAKE = "bash: make: command not found\r\n"


@pytest.mark.asyncio
async def test_failed_command_gets_a_hint_in_its_result_and_its_delivery(shell: Shell) -> None:
    call = shell.call({"command": "make"})
    adapter, tree = await shell.started()
    adapter.emit(_MISSING_MAKE)
    tree.shell_exits(127)

    result = await call
    assert data(result)["hint"].startswith("`make` is not installed or not on PATH.")
    # The user sees the hint as well.
    assert {
        "type": "notice",
        "level": "info",
        "text": f"Hint: {data(result)['hint']}",
    } in shell_detail_blocks({"command": "make"}, result)

    running = data(await shell.call({"command": "make", "mode": "background"}))
    adapter, tree = await shell.started()
    adapter.emit(_MISSING_MAKE)
    tree.exits = (ProgramExit("make.exe", 127),)
    tree.shell_exits(127)
    await eventually(lambda: bool(shell.bodies()))
    # The hint follows the status line, which the status fold reads.
    status, hint = shell.bodies()[0].splitlines()[:2]
    terminal_id = running["terminal_id"]
    assert status == (
        f"The command in terminal {terminal_id} (make) exited with code 127 after 0 seconds."
    )
    assert hint == f"Hint: {data(result)['hint']}"
    # A single failed program with the exit code the status line names is not repeated.
    assert "Failed programs" not in shell.bodies()[0]


@pytest.mark.asyncio
async def test_terminal_results_for_a_command_have_the_shell_result_shape(shell: Shell) -> None:
    terminal_id = data(await shell.call({"command": "make", "mode": "background"}))["terminal_id"]
    adapter, tree = await shell.started()
    adapter.emit("Continue? ")
    await eventually(lambda: shell.manager.command_screen(terminal_id, 1))

    def call(action: str) -> str:
        return "terminal " + json.dumps({"action": action, "terminal_id": terminal_id})

    following = await command_terminal_result(shell.manager, shell.context(), terminal_id)
    assert (following["status"], following["output"]) == ("running", "Continue?")
    assert following["next"] == (
        "The command has run for 0 seconds and keeps running; it has no "
        f"timeout.{log_sentence(following['next'])} Its result arrives as a new message when it "
        "exits. Continue other work, or end your turn if your "
        "next step needs the result; do not sleep or poll for its result. Stop it with "
        f"{call('kill')} when it is no longer needed."
    )
    # Printed nothing and used no CPU for the idle period: the text says how long.
    await shell.clock.advance(2)
    await command_terminal_result(shell.manager, shell.context(), terminal_id)
    await shell.clock.advance(15)
    idle = await command_terminal_result(
        shell.manager, shell.context(), terminal_id, wait_ended="timeout"
    )
    assert (idle["status"], idle["wait_ended"]) == ("running", "timeout")
    assert idle["next"].startswith(
        f"The command in terminal {terminal_id} has printed nothing for 17 seconds and uses no "
        f"CPU; it has no timeout.{log_sentence(idle['next'])} If its output ends in a question "
        "or prompt, answer it"
    )

    adapter.emit(_MISSING_MAKE)
    tree.shell_exits(127)
    await eventually(lambda: shell.manager.command_report(terminal_id).exited)
    exited = await command_terminal_result(shell.manager, shell.context(), terminal_id)
    assert exited == {
        "status": "exited",
        "exit_code": 127,
        "output": "Continue? bash: make: command not found",
        "hint": exited["hint"],
    }
    assert exited["hint"].startswith("`make` is not installed")


@pytest.mark.asyncio
async def test_missing_workdir_and_ungranted_credentials_run_nothing(shell: Shell) -> None:
    missing = await shell.call({"command": "ls", "workdir": "nope"})
    (shell.tmp_path / "notes.txt").write_text("", encoding="utf-8")
    a_file = await shell.call({"command": "ls", "workdir": "notes.txt"})
    secret = await shell.call({"command": "ls", "env_keys": ["UNKNOWN_SECRET_FOR_TEST"]})
    gone = shell.tmp_path / "gone"
    missing_cwd = await dispatch(
        shell.registry,
        dataclasses.replace(shell.context(), cwd=gone),
        {"command": "ls"},
        [SHELL_TOOL_NAME],
    )

    assert missing["error"]["message"] == (
        f"{SHELL_MODEL_NAME} was not run: workdir {model_path(shell.tmp_path / 'nope')} is not "
        "an existing directory. Pass an existing directory, or omit workdir to use the working "
        "directory."
    )
    assert a_file["error"]["message"] == (
        f"{SHELL_MODEL_NAME} was not run: workdir {model_path(shell.tmp_path / 'notes.txt')} is "
        "a file, not a directory. Pass the directory it is in, or omit workdir to use the "
        "working directory."
    )
    # Omitting workdir cannot help when the working directory itself is missing.
    assert missing_cwd["error"]["message"] == (
        f"{SHELL_MODEL_NAME} was not run: the working directory {model_path(gone)} is not an "
        "existing directory. Pass an existing directory as workdir."
    )
    assert "not granted to this Agent" in secret["error"]["message"]
    assert shell.factory.calls == []


@pytest.mark.parametrize("terminal", [True, False])
@pytest.mark.asyncio
async def test_command_over_the_running_command_limit_says_how_to_make_room(
    shell: Shell, monkeypatch: pytest.MonkeyPatch, terminal: bool
) -> None:
    monkeypatch.setattr(terminal_manager, "TERMINAL_MAX_LIVE_COMMANDS", 2)
    for _ in range(2):
        await shell.call({"command": "serve", "mode": "background"})

    refused = await shell.call({"command": "ls"}, terminal=terminal)

    assert (refused["error"]["code"], refused["error"]["retryable"]) == ("command_limit", True)
    room = (
        'Find your running commands with terminal {"action": "list"}, stop the ones you no '
        'longer need with action "kill", then run the command again.'
        if terminal
        else "Run the command again once one of your running commands has exited."
    )
    assert refused["error"]["message"] == (
        f"{SHELL_MODEL_NAME} was not run: vBot already runs 2 commands, the most it runs at "
        f"once. {room}"
    )
    assert len(shell.factory.calls) == 2


@pytest.mark.asyncio
async def test_project_workdir_runs_in_the_project_directory(tmp_path: Path) -> None:
    directory = tmp_path / "repository"
    directory.mkdir()
    async with started_shell(tmp_path, projects=FakeProjects({"vbot": directory})) as shell:
        call = shell.call({"command": "ls", "workdir": "project:vbot"})
        _adapter, tree = await shell.started()
        tree.shell_exits(0)
        assert data(await call)["status"] == "exited"
        unknown = await shell.call({"command": "ls", "workdir": "project:other"})

    assert shell.factory.calls[0][1] == directory.resolve()
    assert unknown["error"]["message"] == (
        f'{SHELL_MODEL_NAME} was not run: workdir "project:other" names no existing Project. '
        "Pass the Project's directory path instead."
    )
    assert len(shell.factory.calls) == 1


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None, reason="Windows PowerShell 7"
)
@pytest.mark.asyncio
async def test_command_too_long_for_the_windows_command_line_names_the_room(
    shell: Shell,
) -> None:
    refused = await shell.call({"command": "#" * 32_000})
    message = refused["error"]["message"]
    room = int(re.findall(r"at most about ([\d,]+)", message)[0].replace(",", ""))
    assert message.endswith("Save the script to a .ps1 file and run that file.")
    assert shell.factory.calls == []

    # Exactly the room named fits.
    call = shell.call({"command": "#" * room})
    _adapter, tree = await shell.started()
    tree.shell_exits(0)
    assert data(await call)["status"] == "exited"


@pytest.mark.asyncio
async def test_update_handoff_token_is_claimable_exactly_while_the_command_runs(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    handoffs = UpdateHandoffs(data_dir)
    async with started_shell(tmp_path, update_handoffs=handoffs) as shell:
        call = shell.call({"command": "vbot update", "mode": "background"})
        _adapter, tree = await shell.started()
        assert data(await call)["status"] == "running"
        token = shell.factory.calls[0][2]["VBOT_UPDATE_HANDOFF"]

        ticket = handoffs.mint(token)
        assert read_update_handoff_ticket(data_dir, ticket.ticket_id).acknowledged is False
        for callback in shell.persisted:
            callback()
        assert read_update_handoff_ticket(data_dir, ticket.ticket_id).acknowledged
        tree.shell_exits(0)
        finished = asyncio.ensure_future(
            shell.manager.wait_finished(data(await call)["terminal_id"])
        )
        await shell.run_clock(finished, until=30)
        await finished

        def released(candidate: str) -> bool:
            try:
                handoffs.mint(candidate)
            except UpdateHandoffUnavailableError:
                return True
            return False

        await eventually(lambda: released(token))

        # A command that cannot start says so and releases its token at once.
        shell.factory.error = OSError("spawn unavailable")
        failed = await shell.call({"command": "vbot update"})
        assert failed["error"]["code"] == "command_not_started"
        assert failed["error"]["retryable"] is True
        assert failed["error"]["message"].startswith(
            f"{SHELL_MODEL_NAME} was not run: the shell could not be started ("
        )
        assert failed["error"]["message"].endswith(
            "Run the command again; if it fails the same way, tell the user."
        )
        assert released(shell.factory.calls[-1][2]["VBOT_UPDATE_HANDOFF"])


@pytest.mark.asyncio
async def test_statuses_of_handed_off_commands_fold_from_results_and_deliveries(
    shell: Shell,
) -> None:
    call = shell.call({"command": "serve", "description": "Serve (dev)"})
    _adapter, tree = await shell.started()
    await shell.run_clock(call, until=SHELL_HANDOFF_SECONDS + 1)
    running = await call
    terminal_id = data(running)["terminal_id"]
    tree.shell_exits(2)
    await eventually(lambda: len(shell.trigger.submissions) == 1)
    # A delivery named by its description shows the command as well.
    assert shell.bodies()[0].splitlines()[1] == "Command: serve"

    def record(role: str, content: str, name: str | None = None) -> Any:
        return type("Record", (), {"role": role, "content": content, "name": name})()

    result = record("tool", json.dumps(running), SHELL_TOOL_NAME)
    delivery = record("note", "Background results:\n" + shell.bodies()[0])
    stopped = record(
        "note",
        "The command in terminal term_x (a (b) c) was stopped after 1 minute 5 seconds: vBot "
        "shut down.",
    )
    unknown = record(
        "note",
        "The command in terminal term_y (y) exited after 3 seconds; its exit code is unknown.",
    )
    # Deliveries stored before they named the time still fold.
    earlier = record("note", "The command in terminal term_z (z) exited with code 0.")

    assert background_command_statuses([result]) == {terminal_id: "running"}
    assert background_command_statuses([result, delivery, stopped, unknown, earlier]) == {
        terminal_id: "failed",
        "term_x": "stopped",
        "term_y": "failed",
        "term_z": "completed",
    }
    assert shell.manager.command_status(terminal_id) == "failed"
    assert shell.manager.command_status("term_unknown") is None


def test_definition_states_the_host_shell_and_its_detached_start() -> None:
    windows = sys.platform == "win32"
    mode = SHELL_TOOL_PARAMETERS["properties"]["mode"]["description"]

    assert ("Write PowerShell, not bash or cmd:" in SHELL_TOOL_DESCRIPTION) is windows
    assert ("Use bash syntax." in SHELL_TOOL_DESCRIPTION) is not windows
    assert f"instead of {'Start-Process' if windows else 'nohup'} or a trailing &." in mode


@pytest.mark.skipif(
    shutil.which("pwsh" if sys.platform == "win32" else "bash") is None,
    reason="the host shell is not installed",
)
@pytest.mark.asyncio
async def test_real_shell_reports_exit_code_and_unicode_output(tmp_path: Path) -> None:
    trigger = PendingTriggerService()
    manager = TerminalManager(
        trigger,
        temporary_files=TemporaryFileManager(
            tmp_path / "temp", retention={"commands": timedelta(hours=1)}
        ),
    )
    registry = ToolRegistry()
    register_shell_tool(registry, manager)
    # The exit code of the last program, which pwsh -Command alone reduces to 1.
    program = f"& '{sys.executable}'" if sys.platform == "win32" else f"'{sys.executable}'"
    print_text = "Write-Output 'Grüße'" if sys.platform == "win32" else "printf 'Grüße\\n'"
    command = f'{print_text}; {program} -c "import sys; sys.exit(3)"'
    context = ToolContext(
        agent_id="agent-a",
        session_id="session-a",
        run_id="run-a",
        tool_call_id="call-a",
        tool_name=SHELL_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        cwd=tmp_path,
    )
    try:
        result = data(await dispatch_as_executor(registry, context, {"command": command}))
    finally:
        trigger.release.set()
        await manager.aclose()

    assert (result["status"], result["exit_code"], result["output"]) == ("exited", 3, "Grüße")
