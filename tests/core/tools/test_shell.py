"""The shell Tool: results, the wait, the background, deliveries, stops and definitions.

Commands run on fake processes through a real ``CommandManager`` on a fake clock
(``command_manager_helpers``); the Tool is called through the registry as the
executor calls it. Real shells run only where the operating system's behavior is
the contract: PowerShell prompts, a program that asks for input, exit codes and
Unicode output.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import errno
import json
import re
import shutil
import sys
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from core.projects import ProjectNotFoundError
from core.sessions import SessionAddress
from core.storage.temp_files import TemporaryFileManager
from core.tools import shell as shell_module
from core.tools import shell_hints
from core.tools._command_launch import PipeLauncher
from core.tools._powershell import REQUEST_UNREADABLE_EXIT_CODE
from core.tools.background_tasks import BACKGROUND_TASKS_TOOL_NAME
from core.tools.command_manager import CommandManager, NotRunReason
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.process_tree import ProcessTreeFacts, ProgramExit, RunningProcess
from core.tools.scratch_areas import ScratchAreas
from core.tools.shell import (
    SHELL_DEFAULT_WAIT_SECONDS,
    SHELL_MAX_WAIT_SECONDS,
    SHELL_TOOL_DESCRIPTION,
    SHELL_TOOL_NAME,
    SHELL_TOOL_PARAMETERS,
    background_command_statuses,
    format_command_delivery,
    register_shell_tool,
    shell_detail_blocks,
)
from core.tools.shell_environment import CommandEnvironment, terminal_environment
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_success
from core.tools.update_handoff import (
    UpdateHandoffs,
    UpdateHandoffUnavailableError,
    read_update_handoff_ticket,
)
from core.utils.paths import model_path
from tests.core.tools.command_manager_helpers import (
    OWNER,
    FakeClock,
    FakeLauncher,
    FakeProcess,
    FakeSink,
    Harness,
    command_harness,
    turns,
)
from tests.core.tools.terminal_manager_helpers import eventually
from tests.core.tools.tools_test_support import dispatch_as_executor

# The Tools a call can name besides the shell, unless a test says otherwise.
COMPANIONS = (BACKGROUND_TASKS_TOOL_NAME, "terminal")


class FakeProjects:
    """Projects by id, each with its directory."""

    def __init__(self, directories: dict[str, Path]) -> None:
        self.directories = directories

    def get(self, project_id: str) -> Any:
        if project_id not in self.directories:
            raise ProjectNotFoundError(f"Project not found: {project_id}")
        return SimpleNamespace(project_id=project_id, cwd=str(self.directories[project_id]))


class Shell:
    """The shell Tool over a ``CommandManager`` with fake processes and time."""

    def __init__(
        self,
        commands: Harness,
        *,
        update_handoffs: UpdateHandoffs | None = None,
        projects: FakeProjects | None = None,
        scratch_areas: ScratchAreas | None = None,
    ) -> None:
        self.commands = commands
        self.registry = ToolRegistry()
        register_shell_tool(
            self.registry,
            commands.manager,
            credential_resolver=lambda name: f"value-of-{name}",
            update_handoffs=update_handoffs,
            projects=projects,  # type: ignore[arg-type]
            scratch_areas=scratch_areas,
        )
        self.cancel_callbacks: list[Callable[[], Any]] = []
        self.background_callbacks: list[Callable[[], bool]] = []
        # Callbacks the call registered to run once its Tool Result is persisted.
        self.persisted: list[Callable[[], None]] = []
        # Live output events of the calls.
        self.live_output: list[JsonObject] = []

    @property
    def manager(self) -> CommandManager:
        return self.commands.manager

    @property
    def launcher(self) -> FakeLauncher:
        return self.commands.launcher

    @property
    def clock(self) -> FakeClock:
        return self.commands.clock

    @property
    def sink(self) -> FakeSink:
        return self.commands.sink

    def context(self) -> ToolContext:
        tmp_path = self.commands.cwd
        return ToolContext(
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
            project_id="project-a",
            tool_settings={"bash": {"allowed_env": ["API_TOKEN"]}},
            cancel_registration_hook=self.cancel_callbacks.append,
            background_registration_hook=self.background_callbacks.append,
            result_persisted_hook=self.persisted.append,
            emit_hook=lambda _event, payload: self.live_output.append(payload),
        )

    def call(
        self, arguments: JsonObject, *, tools: tuple[str, ...] = COMPANIONS
    ) -> asyncio.Task[JsonObject]:
        """Call the shell as the executor does, for an Agent that can call *tools* too."""
        allowed = [SHELL_TOOL_NAME, *tools]
        return asyncio.ensure_future(
            dispatch_as_executor(self.registry, self.context(), arguments, allowed)
        )

    async def started(self, count: int = 1) -> FakeProcess:
        """The process of the *count*-th command, once it started.

        Each call prepares its environment in a thread before the launch.
        """
        await eventually(lambda: len(self.launcher.processes) >= count, attempts=2000)
        return self.launcher.processes[count - 1]

    async def returned(
        self, call: asyncio.Future[JsonObject], *, step: float = 0.5, within: float = 900
    ) -> JsonObject:
        """Let fake time pass until *call* returns, at most *within* seconds."""
        end = self.clock.now + within
        await turns()
        while not call.done():
            assert self.clock.now < end, "the call did not return"
            await self.commands.advance(step, step=step)
        return call.result()


@contextlib.asynccontextmanager
async def started_shell(tmp_path: Path, **options: Any) -> AsyncIterator[Shell]:
    async with command_harness(tmp_path) as commands:
        yield Shell(commands, **options)


@pytest_asyncio.fixture
async def shell(tmp_path: Path) -> AsyncIterator[Shell]:
    async with started_shell(tmp_path) as harness:
        yield harness


def data(result: JsonObject) -> JsonObject:
    assert result["ok"] is True, result
    payload: JsonObject = result["data"]
    return payload


_STOP_CALL = re.compile(r"background_tasks (\{.*?\})")


def stop_calls(text: str) -> list[JsonObject]:
    """The complete ``background_tasks`` calls a text names, as the Agent would send them."""
    return [json.loads(call) for call in _STOP_CALL.findall(text)]


_LOG_SENTENCE = re.compile(r"The command's complete raw output is written live to (\S+)\.(?=\s|$)")


def log_file(text: str) -> str:
    """The existing log file a running command's text names, as it names it."""
    match = _LOG_SENTENCE.search(text)
    assert match is not None, text
    assert Path(match[1]).is_file()
    return match[1]


def launched(launcher: FakeLauncher, index: int = 0) -> tuple[str, dict[str, str]]:
    """A launch's command and complete environment, as the host shell runs it.

    On Windows the command and the call's layer of the environment travel in
    the request PowerShell reads from stdin; elsewhere they are bash's argument
    and environment.
    """
    environment = dict(launcher.envs[index])
    request = launcher.requests[index]
    if request is None:
        return launcher.argv[index][-1], environment
    lines = request.decode("ascii").splitlines()
    assert lines[-1] == "."

    def text(field: str) -> str:
        return base64.b64decode(field).decode("utf-8")

    for line in lines[3:-1]:
        name, separator, value = line.partition(" ")
        if separator:
            environment[text(name)] = text(value)
        else:
            environment.pop(text(name), None)
    return text(lines[2]), environment


def not_run_code(launcher: FakeLauncher, reason: NotRunReason, index: int = 0) -> int:
    """The exit code by which PowerShell says it ran nothing of a launch's request:
    the request's first line, or the code after it for a workdir it cannot enter."""
    request = launcher.requests[index]
    assert request is not None
    code = int(request.split(b"\n", 1)[0])
    return code if reason == "request_unreadable" else code + 1


# The bootstrap that reports a command it ran nothing of is PowerShell's.
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="PowerShell's bootstrap")


# Command log files: tmp_<id>.log in Storage.
_LOG_PATH = re.compile(r"(\S+\.log)(?=[\s.]|$)")


def named_log(text: str) -> str:
    """The existing log file *text* names, as it names it."""
    match = _LOG_PATH.search(text)
    assert match is not None, text
    assert Path(match[1]).is_file()
    return match[1]


def note(content: str) -> Any:
    """A stored delivery, as the status fold reads it."""
    return SimpleNamespace(role="note", content=content, name=None)


# --- Ended commands ----------------------------------------------------------


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
async def test_ended_command_reports_output_exit_code_failed_programs_and_environment(
    shell: Shell,
    exits: tuple[ProgramExit, ...],
    exit_code: int,
    failed_programs: list[str] | None,
) -> None:
    call = shell.call({"command": "build --all", "env_keys": ["API_TOKEN"], "env": {"MODE": "ci"}})
    process = await shell.started()
    process.write("compiling\r\ndone\r\n")
    process.tree.exits = list(exits)
    process.exit(exit_code)

    result = data(await shell.returned(call))
    expected: JsonObject = {"status": "exited", "exit_code": exit_code, "output": "compiling\ndone"}
    if failed_programs is not None:
        expected["failed_programs"] = failed_programs
    assert result == expected
    command, env = launched(shell.launcher)
    assert command == "build --all"
    # No input, no terminal: PowerShell cannot prompt.
    assert shell.launcher.argv[0][1:-1] == (
        ["-NoProfile", "-NonInteractive", "-Command"] if sys.platform == "win32" else ["-c"]
    )
    assert shell.launcher.cwds[0] == shell.commands.cwd
    if sys.platform == "win32":
        # The call's own layer travels in the request: PowerShell starts with the same
        # environment for every call, so a shell started before the call can serve it.
        assert not {"API_TOKEN", "MODE", "VBOT_RUN_AGENT_ID"} & set(shell.launcher.envs[0])
    assert (env["VBOT_RUN_AGENT_ID"], env["VBOT_RUN_PROJECT_ID"]) == ("agent-a", "project-a")
    assert (env["API_TOKEN"], env["MODE"]) == ("value-of-API_TOKEN", "ci")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # A command that ended in its call is never listed as a background command.
    assert shell.manager.commands([OWNER]) == []
    assert shell.sink.submissions == []


@pytest.mark.asyncio
async def test_an_empty_env_value_reaches_the_command(shell: Shell) -> None:
    call = shell.call({"command": "build", "env": {"EMPTY": "", "MODE": "ci"}})
    (await shell.started()).exit(0)
    await shell.returned(call)

    command, env = launched(shell.launcher)
    assert (command, env["EMPTY"], env["MODE"]) == ("build", "", "ci")
    if sys.platform == "win32":
        # PowerShell on .NET 8 removes a variable it sets to an empty value, so the
        # call starts a shell of its own that starts with the value.
        assert shell.launcher.envs[0]["EMPTY"] == "" and shell.launcher.warm == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("scratch_available", [True, False])
async def test_each_command_launch_ensures_its_sessions_scratch_tmp(
    tmp_path: Path, scratch_available: bool, caplog: pytest.LogCaptureFixture
) -> None:
    def identity(address: SessionAddress) -> str:
        if not scratch_available:
            raise RuntimeError("private Scratch lookup details")
        return "generation_" + address.session_id

    scratch_areas = ScratchAreas(
        tmp_path,
        identity=identity,
        ancestors=lambda _address: (),
        busy_sessions=lambda: (),
    )
    ordinary_environment = terminal_environment()
    async with started_shell(tmp_path, scratch_areas=scratch_areas) as shell:
        for count in (1, 2):
            result = data(await shell.call({"command": "build", "run_in_background": True}))
            assert result["status"] == "running"
            _command, environment = launched(shell.launcher, count - 1)
            if scratch_available:
                own = Path(environment["VBOT_SCRATCH_DIR"])
                for name in ("TEMP", "TMP", "TMPDIR"):
                    assert Path(environment[name]) == own / "tmp"
                    assert Path(environment[name]).is_dir()
                # Removing it between calls must not make tempfile use the host temp.
                (own / "tmp").rmdir()
            else:
                assert "VBOT_SCRATCH_DIR" not in environment
                for name in ("TEMP", "TMP", "TMPDIR"):
                    assert environment.get(name) == ordinary_environment.get(name)
            if sys.platform == "win32":
                assert "VBOT_SCRATCH_DIR" not in shell.launcher.envs[-1]
            (await shell.started(count)).exit(0)
    if not scratch_available:
        assert sum(record.levelname == "WARNING" for record in caplog.records) == 1
        assert "private Scratch lookup details" not in caplog.text


@pytest.mark.parametrize("terminal", [True, False])
@pytest.mark.asyncio
async def test_leniently_read_arguments_run_with_a_note(shell: Shell, terminal: bool) -> None:
    tools = COMPANIONS if terminal else (BACKGROUND_TASKS_TOOL_NAME,)
    call = shell.call(
        {"cmd": "ls", "timeout": 60000, "env_keys": ["PATH"], "pty": True, "stdin": "y"},
        tools=tools,
    )
    (await shell.started()).exit(0)

    notes = data(await shell.returned(call))["notes"]
    assert notes[0] == "timeout 60000 was read as milliseconds (60 s); timeout takes seconds."
    # A terminal or input cannot be had; the note names terminal only when it can be called.
    assert notes[1].startswith("pty has no effect: commands run without a terminal")
    assert notes[1].endswith(" with terminal.") is terminal
    assert notes[2].startswith("stdin has no effect: commands get no input.")
    assert notes[3] == (
        "PATH is not a granted credential, so the command sees the value it inherits; "
        "env_keys is only for granted credentials."
    )
    assert len(notes) == 4


@pytest.mark.asyncio
async def test_each_shell_start_resolves_its_fresh_environment_off_loop(
    shell: Shell, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()
    prepared: list[int] = []
    resolved: list[tuple[int, str | None]] = []
    paths = iter(("first-path", "changed-path"))
    tmp_path = shell.commands.cwd

    def environment(*_args: Any, **_kwargs: Any) -> CommandEnvironment:
        prepared.append(threading.get_ident())
        return CommandEnvironment({"PATH": next(paths)}, {})

    def executable(_name: str, *, path: str | None = None) -> str:
        resolved.append((threading.get_ident(), path))
        return str(tmp_path / str(path) / "fixture-shell")

    monkeypatch.setattr(shell_module, "command_environment", environment)
    monkeypatch.setattr(shell_module.shutil, "which", executable)
    for count, path in enumerate(("first-path", "changed-path"), start=1):
        result = data(await shell.call({"command": "build", "run_in_background": True}))
        assert result["status"] == "running"
        assert shell.launcher.argv[-1][0] == str(tmp_path / path / "fixture-shell")
        assert shell.launcher.envs[-1]["PATH"] == path
        (await shell.started(count)).exit(0)

    assert [path for _thread, path in resolved] == ["first-path", "changed-path"]
    assert prepared == [thread for thread, _path in resolved]
    assert all(thread != loop_thread for thread in prepared)


def _numbered(count: int) -> list[str]:
    return [f"line {number:04d}" for number in range(count)]


def _letters(count: int) -> str:
    return "".join(chr(ord("a") + number % 26) for number in range(count))


@pytest.mark.parametrize(
    ("lines", "head", "tail", "omission"),
    [
        # 29,999 characters stay whole.
        (["x" * 99] * 300, 300, 0, None),
        # Whole lines: the first within 2,000 characters, the last within the rest.
        (_numbered(5_000), 200, 2_800, "2,000 lines"),
        # A last line longer than the tail's room keeps its end.
        (["start", "y" * 25_000 + "z" * 25_000], 1, 1, "20,005 characters"),
        # A line longer than both parts keeps its start and its end.
        ([_letters(100_000)], 1, 1, "70,000 characters"),
        (["s" * 5_000, *_numbered(4_000)], 1, 2_800, "1,200 lines and 3,000 characters"),
    ],
    ids=["whole", "lines", "long-last-line", "one-line", "long-first-line"],
)
@pytest.mark.asyncio
async def test_long_output_keeps_its_first_and_last_characters_and_names_the_log_file(
    shell: Shell, lines: list[str], head: int, tail: int, omission: str | None
) -> None:
    text = "".join(f"{line}\n" for line in lines)
    call = shell.call({"command": "test"})
    process = await shell.started()
    process.write(text)
    process.exit(0)

    output = data(await shell.returned(call))["output"].split("\n")
    if omission is None:
        assert output == lines
        return
    shown_head, marker, shown_tail = output[:head], output[head], output[head + 1 :]
    # One marker says what was left out and names the file with all of it.
    assert re.fullmatch(rf"\[\.\.\. {omission} omitted; .*\]", marker), marker
    assert Path(named_log(marker)).read_bytes() == text.encode()
    assert len("\n".join(shown_head)) + len("\n".join(shown_tail)) <= 30_000
    assert len(shown_tail) == tail
    if head == 1 and len(lines[0]) > 2_000:
        assert shown_head == [lines[0][:2_000]]
    else:
        assert shown_head == lines[:head]
    if len(shown_tail) == 1 and len(lines[-1]) > len(shown_tail[0]):
        # The output's end, where errors and summaries are, stays in view.
        assert lines[-1].endswith(shown_tail[0])
    else:
        assert shown_tail == lines[-tail:]


# --- The wait and the background ---------------------------------------------


@pytest.mark.parametrize(
    ("timeout", "wait", "noted", "run"),
    [
        (None, SHELL_DEFAULT_WAIT_SECONDS, False, "prints"),
        (30, 30, False, "prints"),
        (0, SHELL_DEFAULT_WAIT_SECONDS, True, "prints"),
        (900, SHELL_MAX_WAIT_SECONDS, True, "prints"),
        # A program that reads the console directly waits silently on Windows.
        (None, SHELL_DEFAULT_WAIT_SECONDS, False, "silent-idle"),
        (30, 30, False, "silent-busy"),
        (None, SHELL_DEFAULT_WAIT_SECONDS, False, "silent-idle-posix"),
    ],
    ids=[
        "default",
        "timeout",
        "zero-is-omitted",
        "capped",
        "silent-and-idle",
        "silent-and-busy",
        "silent-and-idle-on-posix",
    ],
)
@pytest.mark.asyncio
async def test_a_command_still_running_when_the_wait_passes_continues_and_delivers(
    shell: Shell,
    monkeypatch: pytest.MonkeyPatch,
    timeout: int | None,
    wait: float,
    noted: bool,
    run: str,
) -> None:
    monkeypatch.setattr(shell_module, "_CONSOLE_READS_WAIT", not run.endswith("posix"))
    arguments: JsonObject = {"command": "slow build"}
    if timeout is not None:
        arguments["timeout"] = timeout
    call = shell.call(arguments)
    process = await shell.started()
    if run == "prints":
        process.write("step 1\n")
    elif run == "silent-busy":
        # Work without output: each look at the tree finds more CPU time.
        facts = process.tree.facts

        def working() -> ProcessTreeFacts:
            process.tree.cpu_seconds += 1.0
            return facts()

        monkeypatch.setattr(process.tree, "facts", working)

    result = data(await shell.returned(call, step=5 if wait > 60 else 0.5))
    command_id = result["command_id"]
    assert wait <= shell.clock.now < wait + 1
    assert result["status"] == "running"
    assert result.get("output") == ("step 1" if run == "prints" else None)
    assert ("notes" in result) is noted
    # The result names the exact call that stops the command once, and its log file.
    assert stop_calls(result["next"]) == [{"action": "stop", "id": command_id}]
    log_file(result["next"])
    # Only a silent command whose processes are idle on Windows may wait for the console.
    assert ("console input" in result["next"]) is (run == "silent-idle")
    # Nothing stops a command for taking long: it is listed and keeps running.
    await shell.commands.advance(3 * SHELL_MAX_WAIT_SECONDS, step=30)
    report = shell.manager.report(command_id)
    assert (report.ended, report.background) == (False, True)
    assert process.tree.terminations == 0
    assert [row.command_id for row in shell.manager.commands([OWNER])] == [command_id]

    process.exit(0)
    await shell.commands.finish(command_id)
    (submission,) = shell.sink.submissions
    assert submission["notice_id"] == f"command:{command_id}"
    assert background_command_statuses([note(submission["body"])]) == {command_id: "completed"}


@pytest.mark.parametrize(
    ("tools", "lines"),
    [(COMPANIONS, 3), (("terminal",), 5_000)],
    ids=["background_tasks-short-output", "without-background_tasks-long-output"],
)
@pytest.mark.asyncio
async def test_run_in_background_returns_at_once_and_delivers(
    shell: Shell, tools: tuple[str, ...], lines: int
) -> None:
    # A call in the background does not wait, also with a timeout.
    running = data(
        await shell.call({"command": "serve", "run_in_background": True, "timeout": 5}, tools=tools)
    )
    command_id = running["command_id"]
    assert (running["status"], shell.clock.now) == ("running", 0)
    assert "notes" not in running
    if tools == COMPANIONS:
        assert stop_calls(running["next"]) == [{"action": "stop", "id": command_id}]
    else:
        # Without background_tasks, the host shell stops it by its root process.
        pid = shell.manager.report(command_id).pid
        stop = f"taskkill /PID {pid} /T /F" if sys.platform == "win32" else f"kill -- -{pid}"
        assert f"stop it with the shell command {stop} once" in running["next"]
        assert BACKGROUND_TASKS_TOOL_NAME not in running["next"]
    path = log_file(running["next"])
    assert [row.command_id for row in shell.manager.commands([OWNER])] == [command_id]
    # The user sees that it continues.
    details = shell_detail_blocks({"command": "serve"}, tool_success(running))
    assert [block["level"] for block in details if block["type"] == "notice"] == ["info"]

    process = await shell.started()
    process.write("".join(f"line {number}\n" for number in range(lines)))
    await shell.commands.advance(30, step=5)
    assert not shell.manager.report(command_id).ended
    process.exit(0)
    await shell.commands.finish(command_id)
    body = shell.sink.bodies[0].splitlines()
    assert body[0].startswith(f"The background command {command_id} (serve) exited")
    # The delivery names the file with the complete output once: in its last line,
    # or in the marker where it leaves output out.
    assert sum(path in line for line in body) == 1
    if lines > 100:
        assert body[-1] == f"line {lines - 1}"
        assert any(line.startswith("[... ") and path in line for line in body)
    else:
        assert path in body[-1] and not body[-1].startswith("line ")
    assert Path(path).read_text(encoding="utf-8").splitlines()[-1] == f"line {lines - 1}"


@pytest.mark.asyncio
async def test_running_output_is_the_newest_lines_and_its_log_file_has_every_line(
    shell: Shell,
) -> None:
    call = shell.call({"command": "build"})
    process = await shell.started()
    process.write("".join(f"line {number}\n" for number in range(1, 151)))

    result = data(await shell.returned(call))
    lines = [f"line {number}" for number in range(1, 151)]
    assert result["status"] == "running"
    path = log_file(result["next"])
    marker, *newest = result["output"].splitlines()
    assert marker.startswith("[... 130 earlier lines omitted") and path in marker, marker
    assert newest == lines[-20:]
    assert Path(path).read_text(encoding="utf-8").splitlines() == lines
    # The user watched the output arrive while the call waited.
    assert shell.live_output[-1]["tool_call_id"] == "call-a"
    assert shell.live_output[-1]["command_id"] == result["command_id"]
    assert shell.live_output[-1]["output"].endswith("line 150")


@pytest.mark.parametrize(
    ("command", "stage"),
    [
        ("python -m unittest 2>&1 | Select-Object -Last 25", "Select-Object -Last 25"),
        # Select-Object -First passes each line on as it arrives.
        ("python -m unittest 2>&1 | Select-Object -First 25", None),
    ],
)
@pytest.mark.asyncio
async def test_output_a_pipe_stage_holds_back_is_explained_while_running_and_after_a_stop(
    shell: Shell, monkeypatch: pytest.MonkeyPatch, command: str, stage: str | None
) -> None:
    monkeypatch.setattr(shell_module, "_CONSOLE_READS_WAIT", True)
    # The command prints nothing before its pipe stage passes its output on.
    running = data(await shell.returned(shell.call({"command": command})))
    # A holding stage explains the silence: the program is not taken as waiting for
    # the console, and the log file is not called complete.
    assert ("console input" in running["next"]) is (stage is None)
    assert ("complete raw output" in running["next"]) is (stage is None)

    stopped_call = shell.call({"command": command})
    await shell.started(2)
    await eventually(lambda: len(shell.cancel_callbacks) == 2)
    stopping = shell.cancel_callbacks[1]()
    stopped = data(await shell.returned(stopped_call))
    await stopping

    assert (running["status"], stopped["status"]) == ("running", "stopped")
    # The output is the command's alone; only a stage that holds output back is
    # named, in what the Agent does next.
    assert "output" not in running and "output" not in stopped
    if stage is None:
        assert "piped into" not in running["next"] + stopped.get("hint", "")
    else:
        assert f"Output piped into {stage} appears only once" in running["next"]
        # The stop lost what the stage held.
        assert stage in stopped["hint"] and "lost" in stopped["hint"]


@pytest.mark.parametrize("request_kind", ["stop", "background", "turn-cancelled"])
@pytest.mark.asyncio
async def test_the_user_stops_the_command_or_moves_it_to_the_background(
    shell: Shell, request_kind: str
) -> None:
    call = shell.call({"command": "build"})
    process = await shell.started()
    process.write("compiling\n")
    await eventually(lambda: bool(shell.cancel_callbacks))
    await shell.commands.advance(12)
    command_id = shell.live_output[0]["command_id"]

    if request_kind == "stop":
        stopping = shell.cancel_callbacks[0]()
        result = data(await shell.returned(call, step=0.25))
        await stopping
        # The exit code of the kill is vBot's, not the command's: neither shows it.
        assert result == {
            "status": "stopped",
            "stopped_because": "the user stopped it after 12 seconds.",
            "output": "compiling",
        }
        notices = [
            block["text"]
            for block in shell_detail_blocks({"command": "build"}, tool_success(result))
            if block["type"] == "notice"
        ]
        assert notices == ["Stopped: the user stopped it after 12 seconds."]
    elif request_kind == "background":
        assert shell.background_callbacks[0]() is True
        result = data(await shell.returned(call))
        assert (result["status"], result["command_id"]) == ("running", command_id)
        assert stop_calls(result["next"]) == [{"action": "stop", "id": command_id}]
        # A second request finds it moved already, and a stop of the call no longer
        # reaches the command it handed to the background.
        assert shell.background_callbacks[0]() is False
        assert shell.cancel_callbacks[0]() is None
        process.exit(0)
    else:
        # The turn was cancelled with its call: the command is stopped with it.
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
    report = await shell.commands.finish(command_id)

    if request_kind == "background":
        assert report.stop_reason is None
        assert len(shell.sink.submissions) == 1
    else:
        assert report.stop_reason == ("user" if request_kind == "stop" else "run_cancelled")
        # The call's result shows the stop, or no turn waits for it: nothing is delivered.
        assert shell.sink.submissions == []


# --- Processes left running ----------------------------------------------------


@pytest.mark.parametrize(
    ("background", "tools"),
    [(False, COMPANIONS), (True, ())],
    ids=["result-with-background_tasks", "delivery-without-background_tasks"],
)
@pytest.mark.asyncio
async def test_processes_left_running_are_named_with_the_call_that_stops_them(
    shell: Shell, background: bool, tools: tuple[str, ...]
) -> None:
    call = shell.call({"command": "start-server", "run_in_background": background}, tools=tools)
    process = await shell.started()
    process.exit(0, leftovers=[RunningProcess(42, "server.exe")])

    if background:
        command_id = data(await call)["command_id"]
        await shell.commands.advance(5)
        (line,) = [
            line
            for line in shell.sink.bodies[0].splitlines()
            if line.startswith("Processes it started still run: server.exe (pid 42).")
        ]
        # Without background_tasks, the host shell stops them by their ids.
        stop = "Stop-Process -Id 42" if sys.platform == "win32" else "kill 42"
        assert f"Stop them with the shell command {stop} when" in line
    else:
        result = data(await shell.returned(call))
        command_id = result["command_id"]
        assert (result["status"], result["exit_code"]) == ("exited", 0)
        assert result["still_running"] == ["server.exe (pid 42)"]
        assert stop_calls(result["next"]) == [{"action": "stop", "id": command_id}]
        # The result showed the end: nothing more is delivered.
        assert shell.sink.submissions == []
    # They stay tied to the command, so the named call finds them.
    assert [row.command_id for row in shell.manager.commands([OWNER])] == [command_id]
    assert shell.manager.report(command_id).still_running == (RunningProcess(42, "server.exe"),)


# --- Hints and refusals ----------------------------------------------------------


_MISSING_MAKE = "bash: make: command not found\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "output", "exit_code", "expected"),
    [
        ("make", _MISSING_MAKE, 127, "`make`"),
        ("python -c \"print('$field')\"", "\n", 0, "`$field`"),
    ],
)
async def test_failed_command_gets_a_hint_in_its_result_and_its_delivery(
    shell: Shell,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    output: str,
    exit_code: int,
    expected: str,
) -> None:
    monkeypatch.setattr(shell_hints, "_POWERSHELL", True)
    call = shell.call({"command": command})
    process = await shell.started()
    process.write(output)
    process.exit(exit_code)

    result = await shell.returned(call)
    hint = data(result)["hint"]
    assert expected in hint
    assert data(result)["exit_code"] == exit_code
    # The user sees the hint as well.
    assert {"type": "notice", "level": "info", "text": f"Hint: {hint}"} in shell_detail_blocks(
        {"command": command}, result
    )

    command_id = data(await shell.call({"command": command, "run_in_background": True}))[
        "command_id"
    ]
    process = await shell.started(2)
    process.write(output)
    process.tree.exits = [ProgramExit("make.exe", exit_code)] if exit_code else []
    process.exit(exit_code)
    await shell.commands.finish(command_id)
    # The hint follows the status line, which the status fold reads.
    body = shell.sink.bodies[0]
    assert background_command_statuses([note(body)]) == {
        command_id: "failed" if exit_code else "completed"
    }
    assert body.splitlines()[1] == f"Hint: {hint}"
    # A single failed program with the exit code the status line names is not repeated.
    assert "Failed programs" not in body


@pytest.mark.asyncio
async def test_calls_refused_before_the_start_run_nothing(shell: Shell) -> None:
    tmp_path = shell.commands.cwd
    missing = await shell.call({"command": "ls", "workdir": "nope"})
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    a_file = await shell.call({"command": "ls", "workdir": "notes.txt"})
    secret = await shell.call({"command": "ls", "env_keys": ["UNKNOWN_SECRET_FOR_TEST"]})
    # No environment variable can hold a NUL character.
    nul = await shell.call({"command": "ls", "env": {"DATA": "a\x00b"}})
    gone = tmp_path / "gone"
    missing_cwd = await dispatch_as_executor(
        shell.registry,
        dataclasses.replace(shell.context(), cwd=gone),
        {"command": "ls"},
        [SHELL_TOOL_NAME],
    )

    assert missing["error"]["message"] == (
        f"{SHELL_MODEL_NAME} was not run: workdir {model_path(tmp_path / 'nope')} is not "
        "an existing directory. Pass an existing directory, or omit workdir to use the working "
        "directory."
    )
    assert a_file["error"]["message"] == (
        f"{SHELL_MODEL_NAME} was not run: workdir {model_path(tmp_path / 'notes.txt')} is "
        "a file, not a directory. Pass the directory it is in, or omit workdir to use the "
        "working directory."
    )
    # Omitting workdir cannot help when the working directory itself is missing.
    assert missing_cwd["error"]["message"] == (
        f"{SHELL_MODEL_NAME} was not run: the working directory {model_path(gone)} is not an "
        "existing directory. Pass an existing directory as workdir."
    )
    assert "not granted to this Agent" in secret["error"]["message"]
    assert nul["error"]["code"] == "invalid_arguments"
    assert shell.launcher.argv == []


@pytest.mark.parametrize(
    ("error", "retryable", "fragment"),
    [
        (
            PermissionError(errno.EACCES, "Zugriff verweigert"),
            False,
            "pass that directory as workdir",
        ),
        (OSError(errno.ENOMEM, "Nicht genug Speicher"), True, "out of memory or process resources"),
        (OSError(errno.ENOSPC, "Datenträger voll"), False, "Tell the user that the disk is full."),
        (OSError(errno.EIO, "E/A-Fehler"), True, f"(operating system error {errno.EIO})"),
        # The directory was removed after the call was checked.
        (FileNotFoundError(errno.ENOENT, "Pfad nicht gefunden"), False, "is not an existing"),
    ],
    ids=["access-denied", "out-of-resources", "disk-full", "other", "workdir-removed"],
)
@pytest.mark.asyncio
async def test_a_shell_that_cannot_start_names_the_state_instead_of_the_raw_error(
    shell: Shell, error: OSError, retryable: bool, fragment: str
) -> None:
    workdir = shell.commands.cwd / "build"
    workdir.mkdir()
    shell.launcher.gate = asyncio.Event()
    call = shell.call({"command": "make", "workdir": "build"})
    await eventually(lambda: bool(shell.launcher.argv), attempts=2000)
    if isinstance(error, FileNotFoundError):
        workdir.rmdir()
    shell.launcher.error = error
    shell.launcher.gate.set()

    failure = (await call)["error"]
    message = failure["message"]
    assert (failure["code"], failure["retryable"]) == ("command_not_started", retryable)
    assert message.startswith(f"{SHELL_MODEL_NAME} was not run: ")
    assert fragment in message
    # The host's own message names neither the state nor the next step.
    assert str(error.strerror) not in message
    if isinstance(error, PermissionError):
        assert model_path(workdir) in message
    # Out of resources, the Agent can free some by stopping its background commands.
    assert stop_calls(message) == ([{"action": "list"}] if error.errno == errno.ENOMEM else [])


@windows_only
@pytest.mark.parametrize(
    ("reason", "removed", "retryable", "fragment"),
    [
        ("request_unreadable", False, True, "could not pass the command to the shell"),
        ("workdir_unavailable", False, False, "pass that directory as workdir"),
        # The directory was removed after the call was checked.
        ("workdir_unavailable", True, False, "is no longer an existing directory"),
    ],
    ids=["request-unreadable", "workdir-unavailable", "workdir-removed"],
)
@pytest.mark.asyncio
async def test_a_shell_that_ran_nothing_fails_the_call(
    shell: Shell, reason: NotRunReason, removed: bool, retryable: bool, fragment: str
) -> None:
    # PowerShell's bootstrap exits with the request's own code and prints nothing.
    workdir = shell.commands.cwd / "build"
    workdir.mkdir()
    call = shell.call({"command": "make", "workdir": "build"})
    process = await shell.started()
    if removed:
        workdir.rmdir()
    exit_code = not_run_code(shell.launcher, reason)
    process.exit(exit_code)

    failure = (await shell.returned(call))["error"]
    message = failure["message"]
    assert (failure["code"], failure["retryable"]) == ("command_not_started", retryable)
    assert message.startswith(f"{SHELL_MODEL_NAME} was not run: ")
    assert fragment in message
    assert str(exit_code) not in message
    if reason == "workdir_unavailable":
        assert model_path(workdir) in message


@windows_only
@pytest.mark.asyncio
async def test_a_background_command_that_ran_nothing_delivers_why(shell: Shell) -> None:
    command_id = data(await shell.call({"command": "serve", "run_in_background": True}))[
        "command_id"
    ]
    exit_code = not_run_code(shell.launcher, "request_unreadable")
    (await shell.started()).exit(exit_code)
    await shell.commands.finish(command_id)

    body = shell.sink.bodies[0]
    assert body.startswith(f"The background command {command_id} (serve) did not run: ")
    assert str(exit_code) not in body
    assert background_command_statuses([note(body)]) == {command_id: "failed"}


@pytest.mark.asyncio
async def test_a_command_owns_every_exit_code_but_its_requests_own(shell: Shell) -> None:
    # A command that ran, printed nothing and exited with the bootstrap's fixed code,
    # or with another request's code, ran: only the code drawn for its request says
    # that PowerShell ran nothing.
    first = shell.call({"command": "tool"})
    (await shell.started()).exit(REQUEST_UNREADABLE_EXIT_CODE)
    assert data(await shell.returned(first))["exit_code"] == REQUEST_UNREADABLE_EXIT_CODE
    if sys.platform != "win32":
        return
    second = shell.call({"command": "tool"})
    other = not_run_code(shell.launcher, "request_unreadable", index=0)
    (await shell.started(2)).exit(other)
    assert data(await shell.returned(second))["exit_code"] == other


@pytest.mark.parametrize("companion", [True, False])
@pytest.mark.asyncio
async def test_command_over_the_running_command_limit_says_how_to_make_room(
    shell: Shell, companion: bool
) -> None:
    # The harness's manager runs at most three commands.
    for _ in range(3):
        assert data(await shell.call({"command": "serve", "run_in_background": True}))
    tools = COMPANIONS if companion else ("terminal",)

    refused = await shell.call({"command": "ls"}, tools=tools)

    assert (refused["error"]["code"], refused["error"]["retryable"]) == ("command_limit", True)
    message = refused["error"]["message"]
    assert message.startswith(
        f"{SHELL_MODEL_NAME} was not run: vBot already runs 3 commands, the most it runs at once."
    )
    assert stop_calls(message) == ([{"action": "list"}] if companion else [])
    assert len(shell.launcher.argv) == 3


@pytest.mark.asyncio
async def test_project_workdir_runs_in_the_project_directory(tmp_path: Path) -> None:
    directory = tmp_path / "repository"
    directory.mkdir()
    async with started_shell(tmp_path, projects=FakeProjects({"vbot": directory})) as shell:
        call = shell.call({"command": "ls", "workdir": "project:vbot"})
        (await shell.started()).exit(0)
        assert data(await shell.returned(call))["status"] == "exited"
        unknown = await shell.call({"command": "ls", "workdir": "project:other"})

    assert shell.launcher.cwds == [directory.resolve()]
    assert unknown["error"]["message"] == (
        f'{SHELL_MODEL_NAME} was not run: workdir "project:other" names no existing Project. '
        "Pass the Project's directory path instead."
    )


@pytest.mark.asyncio
async def test_update_handoff_token_is_claimable_exactly_while_the_command_runs(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    handoffs = UpdateHandoffs(data_dir)
    async with started_shell(tmp_path, update_handoffs=handoffs) as shell:
        result = data(await shell.call({"command": "vbot update", "run_in_background": True}))
        assert result["status"] == "running"
        process = await shell.started()
        token = launched(shell.launcher)[1]["VBOT_UPDATE_HANDOFF"]

        ticket = handoffs.mint(token)
        assert read_update_handoff_ticket(data_dir, ticket.ticket_id).acknowledged is False
        for callback in shell.persisted:
            callback()
        assert read_update_handoff_ticket(data_dir, ticket.ticket_id).acknowledged

        def released(candidate: str) -> bool:
            try:
                handoffs.mint(candidate)
            except UpdateHandoffUnavailableError:
                return True
            return False

        # Processes the command left running keep the token claimable.
        process.exit(0, leftovers=[RunningProcess(42, "vbot.exe")])
        await shell.commands.advance(5)
        assert shell.manager.report(result["command_id"]).ended
        assert not released(token)
        process.end_leftovers()
        await shell.commands.finish(result["command_id"])
        await eventually(lambda: released(token))

        # A command that cannot start says so and releases its token at once.
        shell.launcher.error = OSError("spawn unavailable")
        failed = await shell.call({"command": "vbot update"})
        assert failed["error"]["code"] == "command_not_started"
        assert failed["error"]["retryable"] is True
        assert released(launched(shell.launcher, -1)[1]["VBOT_UPDATE_HANDOFF"])


@pytest.mark.asyncio
async def test_statuses_of_background_commands_fold_from_results_and_deliveries(
    shell: Shell,
) -> None:
    call = shell.call({"command": "serve", "description": "Serve (dev)"})
    process = await shell.started()
    running = await shell.returned(call)
    command_id = data(running)["command_id"]
    process.exit(2)
    await shell.commands.finish(command_id)
    # A delivery named by its description shows the command as well.
    assert shell.sink.bodies[0].splitlines()[1] == "Command: serve"
    # The user stops a background command whose label has parentheses after a minute.
    stopped_id = data(
        await shell.call(
            {"command": "watch", "description": "Watch (a) b", "run_in_background": True}
        )
    )["command_id"]
    await shell.started(2)
    await shell.commands.advance(65, step=5)
    await shell.manager.stop(stopped_id, "user")
    await shell.commands.finish(stopped_id)
    # The shell's exit code can be unknown.
    unknown_end = dataclasses.replace(
        shell.manager.report(command_id), command_id="cmd_y", exit_code=None
    )

    def record(role: str, content: str, name: str | None = None) -> Any:
        return SimpleNamespace(role=role, content=content, name=name)

    result = record("tool", json.dumps(running), SHELL_TOOL_NAME)
    delivery = record("note", "Background results:\n" + shell.sink.bodies[0])
    stopped = record("note", "Background results:\n" + shell.sink.bodies[1])
    unknown = record("note", format_command_delivery(unknown_end))
    stopped_by_agent = record(
        "tool",
        json.dumps(tool_success({"status": "stopped", "command_id": "cmd_z"})),
        BACKGROUND_TASKS_TOOL_NAME,
    )
    not_run = record(
        "tool",
        json.dumps(tool_success({"status": "not_run", "command_id": "cmd_w"})),
        BACKGROUND_TASKS_TOOL_NAME,
    )

    assert background_command_statuses([result]) == {command_id: "running"}
    assert background_command_statuses(
        [result, delivery, stopped, unknown, stopped_by_agent, not_run]
    ) == {
        command_id: "failed",
        stopped_id: "stopped",
        "cmd_y": "failed",
        "cmd_z": "stopped",
        "cmd_w": "failed",
    }
    assert shell.manager.status(command_id) == "failed"
    assert shell.manager.status("cmd_unknown") is None


def test_definition_states_the_host_shell_and_its_detached_start() -> None:
    windows = sys.platform == "win32"
    background = SHELL_TOOL_PARAMETERS["properties"]["run_in_background"]["description"]

    assert ("Write PowerShell, not bash or cmd:" in SHELL_TOOL_DESCRIPTION) is windows
    assert ("Use bash syntax." in SHELL_TOOL_DESCRIPTION) is not windows
    assert f"instead of {'Start-Process' if windows else 'nohup'} or a trailing &." in background
    # The handler caps the wait; the schema names no maximum.
    assert "maximum" not in SHELL_TOOL_PARAMETERS["properties"]["timeout"]


# --- Real shells -------------------------------------------------------------------


_HOST_SHELL = shutil.which("pwsh" if sys.platform == "win32" else "bash")
real_shell_only = pytest.mark.skipif(_HOST_SHELL is None, reason="the host shell is not installed")


@contextlib.asynccontextmanager
async def real_shell(tmp_path: Path) -> AsyncIterator[ToolRegistry]:
    # Each command starts its own shell; the warm shell has a test of its own.
    manager = CommandManager(
        FakeSink(),
        temporary_files=TemporaryFileManager(tmp_path / "data"),
        launcher=PipeLauncher(warm=False),
    )
    manager.start()
    registry = ToolRegistry()
    register_shell_tool(registry, manager)
    try:
        yield registry
    finally:
        await manager.aclose()


async def run_real(tmp_path: Path, command: str, *, tools: tuple[str, ...] = ()) -> JsonObject:
    """Run *command* in the host shell; a command that waited for input fails the test."""
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
    async with real_shell(tmp_path) as registry:
        result = await asyncio.wait_for(
            # A program that waited for input would still run when the call returns.
            dispatch_as_executor(
                registry, context, {"command": command, "timeout": 10}, [SHELL_TOOL_NAME, *tools]
            ),
            timeout=60,
        )
    return data(result)


def _program(arguments: str) -> str:
    """The host shell's call of this Python with *arguments*."""
    if sys.platform == "win32":
        return f"& '{sys.executable}' {arguments}"
    return f"'{sys.executable}' {arguments}"


@real_shell_only
@pytest.mark.asyncio
async def test_real_shell_reports_exit_code_and_unicode_output(tmp_path: Path) -> None:
    # The exit code of the last program, which pwsh -Command alone reduces to 1.
    print_text = "Write-Output 'Grüße'" if sys.platform == "win32" else "printf 'Grüße\\n'"
    exit_three = _program('-c "import sys; sys.exit(3)"')
    command = f"{print_text}; {exit_three}"

    result = await run_real(tmp_path, command)

    assert (result["status"], result["exit_code"], result["output"]) == ("exited", 3, "Grüße")


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None, reason="Windows PowerShell 7"
)
@pytest.mark.asyncio
async def test_powershell_prompts_fail_at_once(tmp_path: Path) -> None:
    """User requirement: a command gets no input, so a PowerShell prompt fails at once."""
    for command in ("Read-Host 'Name'", "Get-Credential"):
        result = await run_real(tmp_path, command)

        assert result["status"] == "exited" and result["exit_code"] != 0
        # The hint names the parameter form instead of the prompt.
        assert "pass the value as a parameter" in result["hint"]


@real_shell_only
@pytest.mark.parametrize("terminal", [True, False])
@pytest.mark.asyncio
async def test_a_command_that_asks_for_input_fails_with_the_non_interactive_hint(
    tmp_path: Path, terminal: bool
) -> None:
    """User requirement: a program that asks for input fails at once, with the way out."""
    tools = ("terminal",) if terminal else ()
    result = await run_real(tmp_path, _program("-c \"input('Name: ')\""), tools=tools)

    assert result["status"] == "exited" and result["exit_code"] != 0
    assert "EOFError" in result["output"]
    assert result["hint"].startswith("The program asked for input, and commands get none.")
    # terminal is named only when the Agent can call it.
    assert ("terminal" in result["hint"]) is terminal
