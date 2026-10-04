"""The shell Tool: results, hand-off, delivery, stops, environment and definitions."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import re
import shutil
import sys
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from core.projects import ProjectNotFoundError
from core.storage.temp_files import TemporaryFileManager
from core.tools._terminal_process_tree import ProgramExit, RunningProcess
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.shell import (
    SHELL_HANDOFF_SECONDS,
    SHELL_TOOL_DESCRIPTION,
    SHELL_TOOL_NAME,
    SHELL_TOOL_PARAMETERS,
    background_command_statuses,
    command_terminal_result,
    project_shell_tool_definitions,
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
        self.executor = TrackedExecutor()

    def _track(self, _pid: int) -> FakeTree:
        tree = FakeTree()
        tree.adapter = self.factory.adapters[-1]
        self.trees.append(tree)
        return tree

    def context(self, *, depth: int = 0) -> ToolContext:
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
            nesting_depth=depth,
            tool_settings={"bash": {"allowed_env": ["API_TOKEN"]}},
            cancel_registration_hook=self.cancel_callbacks.append,
            background_registration_hook=self.background_callbacks.append,
            result_persisted_hook=self.persisted.append,
        )

    def call(
        self, arguments: JsonObject, *, depth: int = 0, terminal: bool = True
    ) -> asyncio.Task[JsonObject]:
        """Call the shell as the executor does, for an Agent with or without the terminal Tool."""
        allowed = [SHELL_TOOL_NAME, "terminal"] if terminal else [SHELL_TOOL_NAME]
        return asyncio.ensure_future(
            dispatch(self.registry, self.context(depth=depth), arguments, allowed)
        )

    async def started(self) -> tuple[FakeTerminalAdapter, FakeTree]:
        await eventually(lambda: bool(self.trees))
        return self.factory.adapters[-1], self.trees[-1]

    async def run_clock(self, task: asyncio.Future[Any], *, until: float) -> None:
        """Advance fake time in half seconds while *task* waits on it."""
        while self.clock.now < until and not task.done():
            await settle(self.clock, self.executor, task)
            await self.clock.advance(0.5)

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
async def test_leniently_read_arguments_run_with_a_note(
    shell: Shell, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "original-path")
    call = shell.call({"cmd": "ls", "timeout": 60000, "env_keys": ["PATH"]})
    _adapter, tree = await shell.started()
    tree.shell_exits(0)

    assert data(await call)["notes"] == [
        "timeout 60000 was read as milliseconds (60 s); timeout takes seconds.",
        "PATH is not a granted credential, so the command sees the value it inherits; "
        "env_keys is only for granted credentials.",
    ]


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
    assert "[... 380 lines omitted; log_file has the full output ...]" in lines
    log = Path(result["log_file"]).read_text(encoding="utf-8").splitlines()
    assert len(log) == 500


@pytest.mark.asyncio
async def test_running_output_is_the_whole_transcript_and_screen(shell: Shell) -> None:
    # 120 lines on a 50-row screen: 71 scrolled off into the transcript, the rest
    # are screen rows.
    call = shell.call({"command": "build"})
    adapter, _tree = await shell.started()
    adapter.emit("".join(f"row {number}\r\n" for number in range(1, 121)))
    await shell.run_clock(call, until=SHELL_HANDOFF_SECONDS)

    result = data(await call)
    assert result["status"] == "running"
    assert result["output"].splitlines() == [f"row {number}" for number in range(1, 121)]


@pytest.mark.parametrize("depth", [0, 1])
@pytest.mark.asyncio
async def test_idle_command_continues_as_terminal_and_only_depth_zero_gets_its_result(
    shell: Shell, depth: int
) -> None:
    call = shell.call({"command": "read-name"}, depth=depth)
    adapter, tree = await shell.started()
    adapter.emit("Name: ")
    await shell.run_clock(call, until=SHELL_HANDOFF_SECONDS)

    result = data(await call)
    terminal_id, next_text = result["terminal_id"], result["next"]
    assert result["status"] == "running"
    assert result["output"] == "Name:"
    assert next_text.startswith(f"The command in terminal {terminal_id} has printed nothing")
    assert re.search(r"; its 600-second timeout stops it in \d+ seconds\.", next_text)
    # Answering comes first, without an answer for the Agent to copy; then exact calls.
    assert f'answer it with terminal action "input", terminal_id "{terminal_id}"' in next_text
    assert '"text"' not in next_text
    assert terminal_calls(next_text) == [
        {"action": "wait", "terminal_id": terminal_id},
        {"action": "kill", "terminal_id": terminal_id},
    ]
    assert ("Its result arrives as a new message when it exits." in next_text) is (depth == 0)
    assert ("Its result does not arrive on its own." in next_text) is (depth == 1)
    assert terminal_id in [info.terminal_id for info in shell.manager.list_terminals()]

    tree.shell_exits(0)
    await eventually(lambda: shell.manager.command_report(result["terminal_id"]).exited)
    await asyncio.sleep(0)
    assert len(shell.bodies()) == (1 if depth == 0 else 0)


@pytest.mark.parametrize(
    ("timeout", "limit"),
    [
        # The foreground default counts from the start, so 510 of 600 seconds remain.
        (None, "its 600-second timeout stops it in 510 seconds"),
        (0, "it has no timeout"),
    ],
)
@pytest.mark.asyncio
async def test_long_command_is_handed_off_at_depth_zero_and_its_result_delivered(
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
    assert result["next"].startswith(f"The command keeps running in the background; {limit}.")
    assert "Its result arrives as a new message when it exits." in result["next"]
    assert terminal_calls(result["next"]) == []

    tree.shell_exits(0)
    await eventually(lambda: bool(shell.bodies()))
    body = shell.bodies()[0]
    assert body.startswith(
        f"The command in terminal {result['terminal_id']} (sleep) exited with code 0."
    )


@pytest.mark.asyncio
async def test_subagent_without_terminal_waits_until_the_command_exits(shell: Shell) -> None:
    call = shell.call({"command": "slow"}, depth=1, terminal=False)
    _adapter, tree = await shell.started()
    await shell.run_clock(call, until=SHELL_HANDOFF_SECONDS * 2)
    assert not call.done()

    tree.shell_exits(0)
    assert data(await call)["status"] == "exited"


@pytest.mark.parametrize("depth", [0, 1])
@pytest.mark.asyncio
async def test_timeout_stops_the_command_and_says_how_to_let_it_finish(
    shell: Shell, depth: int
) -> None:
    call = shell.call({"command": "hang", "timeout": 5}, depth=depth)
    adapter, tree = await shell.started()
    await shell.run_clock(call, until=5)
    await eventually(lambda: "\x03" in adapter.writes)
    tree.shell_exits(1)

    result = data(await call)
    assert result["status"] == "stopped"
    assert result["stopped_because"].startswith("it was still running at the 5-second timeout")
    # Only where mode exists does the text point servers to it.
    assert result["stopped_because"].endswith(
        "or 0 for no limit; run servers and watchers with mode background."
        if depth == 0
        else "or 0 for no limit."
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

    if request_kind == "cancel":
        stopping = shell.cancel_callbacks[0]()
        await eventually(lambda: "\x03" in adapter.writes)
        tree.shell_exits(1)
        await stopping
        result = data(await call)
        assert (result["status"], result["stopped_because"]) == ("stopped", "the user stopped it.")
    else:
        assert shell.background_callbacks[0]() is True
        result = data(await call)
        assert result["status"] == "running"
        assert result["next"].startswith(
            "The user moved the command to the background. The command keeps running in "
            f"terminal {result['terminal_id']}; its 600-second timeout"
        )
        tree.shell_exits(0)
        await eventually(lambda: bool(shell.bodies()))


@pytest.mark.parametrize(
    ("depth", "timeout", "limit"),
    [
        # A server runs until it exits: background mode has no default timeout.
        (0, None, "it has no timeout"),
        (0, 30, "its 30-second timeout stops it in 30 seconds"),
        (1, None, None),
    ],
)
@pytest.mark.asyncio
async def test_background_mode_returns_at_once_but_not_in_a_subagent(
    shell: Shell, depth: int, timeout: int | None, limit: str | None
) -> None:
    arguments: JsonObject = {"command": "serve", "mode": "background"}
    if timeout is not None:
        arguments["timeout"] = timeout
    result = await shell.call(arguments, depth=depth)

    if limit is None:
        assert result["error"]["code"] == "background_unavailable_in_subagent"
        assert shell.factory.calls == []
        return
    running = data(result)
    terminal_id = running["terminal_id"]
    assert running["status"] == "running"
    assert running["next"].startswith(
        f"The command keeps running in terminal {terminal_id}; {limit}. Its result arrives "
        "as a new message when it exits."
    )
    assert terminal_calls(running["next"]) == [{"action": "wait", "terminal_id": terminal_id}]
    assert "add pattern to wait for a line it prints" in running["next"]
    assert len(shell.manager.list_terminals()) == 1

    _adapter, tree = await shell.started()
    tree.shell_exits(0)
    await eventually(lambda: bool(shell.bodies()))
    assert shell.bodies()[0].startswith(f"The command in terminal {terminal_id} (serve) exited")


@pytest.mark.asyncio
async def test_processes_left_running_are_reported_and_listed(shell: Shell) -> None:
    call = shell.call({"command": "start-server"})
    _adapter, tree = await shell.started()
    tree.shell_exits(0, survivors=(RunningProcess(42, "server.exe"),))

    result = data(await call)
    assert result["still_running"] == ["server.exe (pid 42)"]
    assert terminal_calls(result["next"]) == [
        {"action": "kill", "terminal_id": result["terminal_id"]}
    ]
    assert result["terminal_id"] in [info.terminal_id for info in shell.manager.list_terminals()]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [True, False])
async def test_delivery_names_processes_left_running_and_how_to_stop_them(
    shell: Shell, terminal: bool
) -> None:
    running = data(
        await shell.call({"command": "start-server", "mode": "background"}, terminal=terminal)
    )
    _adapter, tree = await shell.started()
    tree.shell_exits(0, survivors=(RunningProcess(42, "server.exe"),))
    await eventually(lambda: bool(shell.bodies()))

    line = next(
        line for line in shell.bodies()[0].splitlines() if line.startswith("Processes it started")
    )
    assert line.startswith("Processes it started still run: server.exe (pid 42).")
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
    assert status == f"The command in terminal {terminal_id} (make) exited with code 127."
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
        f"The command keeps running; it has no timeout. Wait for it with {call('wait')}, or stop "
        f"it with {call('kill')}; its result arrives as a new message when it exits."
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
        "CPU; it has no timeout. If its output ends in a question or prompt, answer it"
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
    # Omitting workdir cannot help when the working directory itself is missing.
    assert missing_cwd["error"]["message"] == (
        f"{SHELL_MODEL_NAME} was not run: the working directory {model_path(gone)} is not an "
        "existing directory. Pass an existing directory as workdir."
    )
    assert "not granted to this Agent" in secret["error"]["message"]
    assert shell.factory.calls == []


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

        # A command that cannot start releases its token at once.
        shell.factory.error = OSError("spawn unavailable")
        failed = await shell.call({"command": "vbot update"})
        assert failed["ok"] is False
        assert released(shell.factory.calls[-1][2]["VBOT_UPDATE_HANDOFF"])


@pytest.mark.asyncio
async def test_statuses_of_handed_off_commands_fold_from_results_and_deliveries(
    shell: Shell,
) -> None:
    call = shell.call({"command": "serve", "description": "Serve (dev)"}, depth=0)
    _adapter, tree = await shell.started()
    await shell.run_clock(call, until=SHELL_HANDOFF_SECONDS + 1)
    running = await call
    terminal_id = data(running)["terminal_id"]
    tree.shell_exits(2)
    await eventually(lambda: len(shell.trigger.submissions) == 1)

    def record(role: str, content: str, name: str | None = None) -> Any:
        return type("Record", (), {"role": role, "content": content, "name": name})()

    result = record("tool", json.dumps(running), SHELL_TOOL_NAME)
    delivery = record("note", "Background results:\n" + shell.bodies()[0])
    stopped = record(
        "note", "The command in terminal term_x (a (b) c) was stopped: vBot shut down."
    )

    assert background_command_statuses([result]) == {terminal_id: "running"}
    assert background_command_statuses([result, delivery, stopped]) == {
        terminal_id: "failed",
        "term_x": "stopped",
    }
    assert shell.manager.command_status(terminal_id) == "failed"
    assert shell.manager.command_status("term_unknown") is None


@pytest.mark.parametrize(
    ("depth", "offered", "continuation", "has_mode"),
    [
        (
            0,
            {"terminal"},
            "A command still running after 90 seconds, or waiting for input, keeps running in "
            "a terminal, and the result says how to follow it up.",
            True,
        ),
        (
            0,
            set(),
            "A command still running after 90 seconds keeps running in the background, and its "
            "result arrives as a new message when it exits.",
            True,
        ),
        (
            1,
            {"terminal"},
            "A command waiting for input keeps running in a terminal, and the result says how "
            "to follow it up.",
            False,
        ),
        (1, set(), None, False),
    ],
)
def test_definition_fits_the_session_depth_and_offered_tools(
    depth: int, offered: set[str], continuation: str | None, has_mode: bool
) -> None:
    definitions: list[JsonObject] = [
        {
            "name": SHELL_TOOL_NAME,
            "description": SHELL_TOOL_DESCRIPTION,
            "parameters": SHELL_TOOL_PARAMETERS,
        },
        *({"name": name} for name in sorted(offered)),
    ]

    projected = project_shell_tool_definitions(definitions, nesting_depth=depth)[0]

    description = projected["description"]
    if continuation is None:
        assert "keeps running" not in description
    else:
        assert continuation in description
    # The boundary to the terminal Tool appears only where that Tool is offered.
    assert ("such as REPLs, TUIs and coding-agent CLIs, use terminal." in description) is (
        "terminal" in offered
    )
    properties = projected["parameters"]["properties"]
    assert ("mode" in properties) is has_mode
    assert properties["timeout"]["description"] == (
        "Seconds before the command is stopped, counted from its start; 0 for no limit. "
        + (
            "Omitted, it is 600 in foreground and no limit in background."
            if has_mode
            else "Omitted, it is 600."
        )
    )


@pytest.mark.parametrize(
    ("offered", "sentence"),
    [
        (
            {"read", "search_files", "apply_patch", "web_fetch"},
            "For reading, searching, creating and editing files, use read, search_files and "
            "apply_patch instead of shell commands; for web pages, use web_fetch.",
        ),
        (
            {"read", "search_files", "write", "edit"},
            "For reading, searching, creating and editing files, use read, search_files, write "
            "and edit instead of shell commands.",
        ),
        ({"read"}, "For reading files, use read instead of shell commands."),
        ({"web_fetch"}, "For web pages, use web_fetch."),
        (set(), None),
    ],
)
def test_definition_names_only_the_offered_file_and_web_tools(
    offered: set[str], sentence: str | None
) -> None:
    definitions: list[JsonObject] = [
        {
            "name": SHELL_TOOL_NAME,
            "description": SHELL_TOOL_DESCRIPTION,
            "parameters": SHELL_TOOL_PARAMETERS,
        },
        *({"name": name} for name in sorted({"terminal", *offered})),
    ]

    description = project_shell_tool_definitions(definitions, nesting_depth=0)[0]["description"]

    if sentence is not None:
        assert sentence in description
    for name in ("read", "search_files", "apply_patch", "write", "edit", "web_fetch"):
        assert (re.search(rf"\b{name}\b", description) is not None) is (name in offered)


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
