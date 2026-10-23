"""The shell Tool harness its tests share: the Tool on a real ``CommandManager``, fake processes."""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import AsyncIterator, Callable, Container
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.permissions import PermissionBoundary
from core.projects import ProjectNotFoundError
from core.secrets.agent_secrets import AgentSecrets
from core.subagents import SubAgentTree
from core.tools.background_tasks import BACKGROUND_TASKS_TOOL_NAME
from core.tools.command_manager import CommandManager
from core.tools.scratch_areas import ScratchAreas
from core.tools.shell import (
    SHELL_TOOL_NAME,
    register_shell_tool,
)
from core.tools.tools import JsonObject, ToolContext, ToolRegistry
from core.tools.update_handoff import (
    UpdateHandoffs,
)
from tests.core.permissions.command_check_support import (
    FakeAnalyzer,
    fake_checker,
)
from tests.core.tools.command_manager_helpers import (
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
        secrets: AgentSecrets | None = None,
        analyzer: FakeAnalyzer | None = None,
        permissions: PermissionBoundary = PermissionBoundary.UNRESTRICTED,
        credential_names: Container[str] = frozenset(),
        subagents: SubAgentTree | None = None,
    ) -> None:
        self.commands = commands
        self.registry = ToolRegistry()
        self.checker = fake_checker(analyzer or FakeAnalyzer())
        self.permissions = permissions
        register_shell_tool(
            self.registry,
            commands.manager,
            credential_resolver=lambda name: f"value-of-{name}",
            credential_names=lambda: credential_names,
            update_handoffs=update_handoffs,
            projects=projects,  # type: ignore[arg-type]
            scratch_areas=scratch_areas,
            secrets=secrets,
            command_checker=self.checker,
            subagents=subagents,
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
            permissions=self.permissions,
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


def data(result: JsonObject) -> JsonObject:
    assert result["ok"] is True, result
    payload: JsonObject = result["data"]
    return payload


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
