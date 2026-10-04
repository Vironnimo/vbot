"""The shell Tool: results, hand-off, delivery, stops, environment and definitions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from core.storage.temp_files import TemporaryFileManager
from core.tools._terminal_process_tree import ProgramExit, RunningProcess
from core.tools.shell import (
    SHELL_HANDOFF_SECONDS,
    SHELL_TOOL_DESCRIPTION,
    SHELL_TOOL_NAME,
    SHELL_TOOL_PARAMETERS,
    background_command_statuses,
    project_shell_tool_definitions,
    register_shell_tool,
)
from core.tools.terminal_manager import TerminalManager, TerminalRenderHost
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure_for_exception
from core.tools.update_handoff import (
    UpdateHandoffs,
    UpdateHandoffUnavailableError,
    read_update_handoff_ticket,
)
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


class Shell:
    """The shell Tool over a TerminalManager with fake terminals, trees and time."""

    def __init__(self, tmp_path: Path, *, update_handoffs: UpdateHandoffs | None = None) -> None:
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


@pytest.mark.asyncio
async def test_finished_command_reports_output_exit_code_failed_programs_and_environment(
    shell: Shell,
) -> None:
    call = shell.call({"command": "build --all", "env_keys": ["API_TOKEN"], "env": {"MODE": "ci"}})
    adapter, tree = await shell.started()
    adapter.emit("compiling\r\ndone\r\n")
    tree.exits = (ProgramExit("python.exe", 5),)
    tree.shell_exits(1)

    result = data(await call)
    assert result == {
        "status": "exited",
        "exit_code": 1,
        "output": "compiling\ndone",
        "failed_programs": ["python.exe exited with code 5"],
    }
    argv, cwd, env, _rows, _columns = shell.factory.calls[0]
    assert argv[-1].endswith("build --all")
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
    assert result["status"] == "running"
    assert result["output"] == "Name:"
    assert "probably waiting for input" in result["next"]
    assert ("arrives as a new message" in result["next"]) is (depth == 0)
    assert result["terminal_id"] in [info.terminal_id for info in shell.manager.list_terminals()]

    tree.shell_exits(0)
    await eventually(lambda: shell.manager.command_report(result["terminal_id"]).exited)
    await asyncio.sleep(0)
    assert len(shell.bodies()) == (1 if depth == 0 else 0)


@pytest.mark.asyncio
async def test_long_command_is_handed_off_at_depth_zero_and_its_result_delivered(
    shell: Shell,
) -> None:
    # Without the terminal Tool, quiet commands are not handed off as idle.
    call = shell.call({"command": "sleep"}, terminal=False)
    _adapter, tree = await shell.started()
    await shell.run_clock(call, until=SHELL_HANDOFF_SECONDS + 5)

    result = data(await call)
    assert result["status"] == "running"
    assert shell.clock.now >= SHELL_HANDOFF_SECONDS
    assert result["next"].startswith("The command continues in the background.")

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


@pytest.mark.asyncio
async def test_timeout_stops_the_command_and_says_how_to_let_it_finish(shell: Shell) -> None:
    call = shell.call({"command": "hang", "timeout": 5})
    adapter, tree = await shell.started()
    await shell.run_clock(call, until=5)
    await eventually(lambda: "\x03" in adapter.writes)
    tree.shell_exits(1)

    result = data(await call)
    assert result["status"] == "stopped"
    assert result["stopped_because"].startswith("it was still running at the 5-second timeout")
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
        assert result["next"].startswith("The user moved the command to the background.")
        tree.shell_exits(0)
        await eventually(lambda: bool(shell.bodies()))


@pytest.mark.parametrize("depth", [0, 1])
@pytest.mark.asyncio
async def test_background_mode_returns_at_once_but_not_in_a_subagent(
    shell: Shell, depth: int
) -> None:
    result = await shell.call({"command": "serve", "mode": "background"}, depth=depth)

    if depth == 0:
        assert data(result)["status"] == "running"
        assert len(shell.manager.list_terminals()) == 1
    else:
        assert result["error"]["code"] == "background_unavailable_in_subagent"
        assert shell.factory.calls == []


@pytest.mark.asyncio
async def test_processes_left_running_are_reported_and_listed(shell: Shell) -> None:
    call = shell.call({"command": "start-server"})
    _adapter, tree = await shell.started()
    tree.shell_exits(0, survivors=(RunningProcess(42, "server.exe"),))

    result = data(await call)
    assert result["still_running"] == ["server.exe (pid 42)"]
    assert f"terminal kill on {result['terminal_id']}" in result["next"]
    assert result["terminal_id"] in [info.terminal_id for info in shell.manager.list_terminals()]


@pytest.mark.asyncio
async def test_missing_workdir_and_ungranted_credentials_run_nothing(shell: Shell) -> None:
    missing = await shell.call({"command": "ls", "workdir": "nope"})
    secret = await shell.call({"command": "ls", "env_keys": ["UNKNOWN_SECRET_FOR_TEST"]})

    assert "is not an existing directory" in missing["error"]["message"]
    assert "not granted to this Agent" in secret["error"]["message"]
    assert shell.factory.calls == []


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
            {"read", "terminal"},
            "after 90 seconds, or waiting for input, continues as a terminal",
            True,
        ),
        (0, {"read"}, "after 90 seconds continues in the background", True),
        (1, {"read", "terminal"}, "A command waiting for input continues as a terminal", False),
        (1, {"read"}, None, False),
    ],
)
def test_definition_fits_the_session_depth_and_offered_tools(
    depth: int, offered: set[str], continuation: str | None, has_mode: bool
) -> None:
    shell_definition: JsonObject = {
        "name": SHELL_TOOL_NAME,
        "description": SHELL_TOOL_DESCRIPTION,
        "parameters": SHELL_TOOL_PARAMETERS,
    }
    definitions: list[JsonObject] = [
        shell_definition,
        *({"name": name} for name in sorted(offered)),
    ]

    projected = project_shell_tool_definitions(definitions, nesting_depth=depth)[0]

    description = projected["description"]
    assert "use read to read files" in description
    assert "apply_patch" not in description
    if continuation is None:
        assert "continues" not in description
    else:
        assert continuation in description
    assert ("mode" in projected["parameters"]["properties"]) is has_mode


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
    command = (
        "Write-Output 'Grüße'; exit 3" if sys.platform == "win32" else "printf 'Grüße\\n'; exit 3"
    )
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
