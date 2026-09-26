"""Disposable real Tool dispatch; only child Model execution is a recorded fixture."""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.chat import ChatMessage
from core.database import write_bootstrap_marker
from core.projects import ResolutionAgentNotFoundError
from core.runs import ChatRunManager
from core.sessions import ChatSessionManager, SessionAddress
from core.storage import TemporaryFileManager
from core.subagents import SubAgentCoordinator
from core.tools._shell_arguments import normalize_shell_arguments
from core.tools.apply_patch import patch_targets, register_apply_patch_tool
from core.tools.bash import register_bash_tool
from core.tools.file_state import FileReadState
from core.tools.process_manager import ProcessManager
from core.tools.read import register_read_tool
from core.tools.search_files import interpret_search_call, register_search_files_tool
from core.tools.subagent import _render_subagent_prompt_block, register_subagent_tools
from core.tools.tools import ToolContext, ToolRegistry
from scripts.provider_probe.common import PROJECT_ROOT


class FixtureBoundaryError(ValueError):
    """Stop an observed out-of-scope choice, without teaching a replacement call."""


class FirstUseFixture:
    def __init__(self, root: Path, *, nested: bool = False, outside_cwd: bool = False):
        self.root = root.resolve()
        self.cwd = self.root / "workspace" if outside_cwd else self.root / "repo"
        self.repo = self.root / "repo"
        self.data = self.root / "state"
        for path in (self.cwd, self.repo, self.data):
            path.mkdir(parents=True, exist_ok=True)
        write_bootstrap_marker(self.data)
        self.sessions = ChatSessionManager(self.data)
        self.runs = ChatRunManager(persistence=self.sessions)
        self.temporary = TemporaryFileManager(self.data)
        self.processes = ProcessManager(temporary_files=self.temporary)
        self.received: list[dict[str, Any]] = []
        self.started_events: list[asyncio.Event] = []
        self.notices: list[dict[str, Any]] = []
        self.deliveries: list[asyncio.Future[None]] = []
        self.hold_children = False
        self.seed_result: dict[str, Any] = {}
        self.seed_run: Any = None
        self.trusted_execution_files: dict[str, bytes] = {}
        self.release = asyncio.Event()
        self.agents = {
            key: SimpleNamespace(
                id=key, name=key, project=None, tools={"subagent": {"allowed_agents": ["reviewer"]}}
            )
            for key in ("parent", "reviewer")
        }
        runtime = SimpleNamespace(
            agents=SimpleNamespace(list=lambda: list(self.agents.values())),
            projects=SimpleNamespace(list=lambda: []),
            agent_resolver=SimpleNamespace(
                resolve_agent=self.resolve_agent, resolve_agent_async=self.resolve_agent_async
            ),
            chat_sessions=self.sessions,
            chat_run_manager=self.runs,
            storage=SimpleNamespace(
                temporary_files=self.temporary, load_subagent_settings=lambda: {}
            ),
            streaming_chat_loop=self,
        )
        self.coordinator = SubAgentCoordinator(runtime, self, sessions=self.sessions)
        self.registry = ToolRegistry()
        register_search_files_tool(self.registry)
        register_subagent_tools(self.registry, self.coordinator)
        register_bash_tool(self.registry, self.processes)
        file_state = FileReadState()
        register_apply_patch_tool(self.registry, file_state=file_state)
        register_read_tool(
            self.registry,
            attachment_store=None,
            speech_service=None,
            file_state=file_state,
            speech_max_size_bytes=100000,
        )
        parent = self.sessions.create("parent")
        self.context = ToolContext(
            agent_id="parent",
            session_id=parent.id,
            run_id="fixture-parent",
            tool_call_id="fixture",
            tool_name="subagent",
            tool_call_index=0,
            workspace=self.cwd,
            cwd=self.cwd,
            vbot_root=PROJECT_ROOT,
            data_root=self.data,
            nesting_depth=int(nested),
            tool_settings={"subagent": {"allowed_agents": ["reviewer"]}},
        )

    def write(self, files: dict[str, str]) -> None:
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        self.trusted_execution_files = self.execution_files()

    def execution_files(self) -> dict[str, bytes]:
        """A shell fixture can execute only the original trusted seed directory."""
        paths = list(self.repo.rglob("*"))
        if any(path.is_symlink() for path in paths):
            raise FixtureBoundaryError("Shell execution fixture contains an untrusted link")
        return {
            path.relative_to(self.repo).as_posix(): path.read_bytes()
            for path in paths
            if path.is_file() and path.name != "check-result.txt"
        }

    def resolve_agent(self, project_id, agent_id, *, run_overrides=None):
        if project_id is not None or agent_id not in self.agents:
            raise ResolutionAgentNotFoundError(f"Agent not found: {agent_id}")
        return self.agents[agent_id]

    async def resolve_agent_async(self, project_id, agent_id, *, run_overrides=None):
        return self.resolve_agent(project_id, agent_id, run_overrides=run_overrides)

    def child_loop(self, *, nesting_depth):
        fixture = self

        def run_executor(content, *, agent_overrides=None):
            started = asyncio.Event()
            fixture.started_events.append(started)

            async def execute(run):
                fixture.received.append(
                    {
                        "agent_id": run.agent_id,
                        "session_id": run.session_id,
                        "run_id": run.id,
                        "content": content,
                        "depth": nesting_depth,
                        "model": getattr(agent_overrides, "model", None),
                        "thinking_effort": getattr(agent_overrides, "thinking_effort", None),
                    }
                )
                started.set()
                if fixture.hold_children:
                    await fixture.release.wait()
                message = ChatMessage.assistant(
                    model="fixture/child", content="Fixture review completed: CHECK-42."
                )
                session = fixture.sessions.get(
                    SessionAddress(
                        project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
                    )
                )
                # A real child Run and Session receive the delegation. Its Model's
                # reasoning/review quality is deliberately outside this interface test.
                session.append(message)
                return message

            return execute

        return SimpleNamespace(run_executor=run_executor)

    def submit_completion(self, *args, **kwargs):
        future = asyncio.get_running_loop().create_future()
        self.notices.append(kwargs)
        self.deliveries.append(future)
        return future

    def cancel_completion(self, *args, **kwargs):
        return True

    def system_prompt(self) -> str:
        block = _render_subagent_prompt_block(
            SimpleNamespace(
                agent=self.agents["parent"],
                agent_project_id=None,
                nesting_depth=self.context.nesting_depth,
            ),
            self.coordinator,
        )
        return "\n\n".join(
            [
                f"Working directory: {self.cwd.as_posix()}\n"
                f"Operating system: {'Windows' if sys.platform == 'win32' else 'Linux'}",
                (PROJECT_ROOT / "resources/prompts/tools.md").read_text(encoding="utf-8"),
                block,
            ]
        )

    def inside(self, value: str) -> bool:
        return (self.cwd / value).resolve().is_relative_to(self.root)

    async def dispatch(self, call: dict[str, Any]):
        name, arguments = call["name"], call["arguments"]
        if name == "search_files":
            query = interpret_search_call(arguments)
            if any(not self.inside(path) for path in query["paths"] or [str(self.cwd)]):
                raise FixtureBoundaryError(
                    "Search selected roots outside the disposable repository"
                )
        elif name == "read":
            if not isinstance(arguments.get("path"), str) or not self.inside(arguments["path"]):
                raise FixtureBoundaryError("Read selected a path outside the disposable repository")
        elif name == "apply_patch":
            if any(not self.inside(path) for path in patch_targets(arguments)):
                raise FixtureBoundaryError("Edit selected a path outside the disposable repository")
        elif name == "bash":
            shell_arguments = normalize_shell_arguments(arguments)
            command = shell_arguments.get("command", "")
            if shell_arguments.get("env") or shell_arguments.get("env_keys"):
                raise FixtureBoundaryError("Shell fixture does not allow environment changes")
            # The same execution boundary applies to every task. A valid shell
            # alternative is executed, then graded by its evidence/effect, not
            # rejected merely because a dedicated Tool would be preferable.
            check_command = re.fullmatch(
                r"""(?:python|python3)(?:\.exe)?\s+["']?(?:\.[/\\])?check\.py["']?"""
                r"""(?:;\s*(?:(?:echo|Write-Output)\s+)?["']?"""
                r"""[\w :=-]*\$LASTEXITCODE["']?)?""",
                command,
                re.I,
            )
            read_command = re.fullmatch(
                r"(?:Get-Content\s+(?:(?:-LiteralPath|-Path)\s+)?|cat\s+)"
                r"(?:README\.md|src[/\\]recipes\.py)(?:\s+-Raw)?",
                command,
                re.I,
            )
            if not (check_command or read_command):
                raise FixtureBoundaryError(
                    "Shell command is outside the fixture's executable allowlist"
                )
            if (self.cwd / (shell_arguments.get("workdir") or ".")).resolve() != self.repo:
                raise FixtureBoundaryError("Shell selected a directory outside the fixture")
            if check_command and self.execution_files() != self.trusted_execution_files:
                raise FixtureBoundaryError("Shell executable fixture was changed after seeding")
        context = replace(self.context, tool_name=name, tool_call_id=call.get("id", "fixture"))
        previously_started = len(self.started_events)
        result = await self.registry.dispatch(context, arguments)
        if name == "subagent" and result["ok"] and result["data"].get("status") == "running":
            for started in self.started_events[previously_started:]:
                await asyncio.wait_for(started.wait(), timeout=10)
        await asyncio.sleep(0)
        return result

    async def close(self):
        self.release.set()
        await self.runs.aclose()
        # Drain the completion watchers scheduled by real coordinator dispatch.
        await asyncio.sleep(0)
        for future in self.deliveries:
            future.cancel()
        self.processes.stop()
        self.sessions.close()
