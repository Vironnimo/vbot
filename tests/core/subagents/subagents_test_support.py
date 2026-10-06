"""A dispatch harness for the Sub-Agent Tools over real Sessions and a real Run manager.

Every call goes through the registered Tool the way the Tool executor runs an
Agent's call: argument normalization, contract validation, then the
coordinator. Sessions, the Run manager, the Sub-Agent links and forwarding are
real; the Agent resolver, the completion coordinator and the Chat loop that
executes a Sub-Agent's turn are doubles.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest_asyncio

from core.agents import AgentNotFoundError
from core.chat import ChatMessage, ChatSessionManager
from core.database import write_bootstrap_marker
from core.projects import (
    AgentOverrides,
    AgentResolver,
    ModelConfigurationError,
    ResolutionAgentNotFoundError,
)
from core.runs import ChatRunManager, Run, RunAdmission, RunKind, RunStatus
from core.sessions import SessionAddress
from core.storage import TemporaryFileManager
from core.subagents import SubAgentCoordinator
from core.subagents.links import find_link
from core.tools.availability import MESSAGE_PARENT_TOOL_NAME
from core.tools.subagent import SUBAGENT_TOOL_NAME, register_subagent_tools
from core.tools.tools import ToolContext, ToolRegistry, tool_failure_for_exception

JsonObject = dict[str, Any]


def address(agent_id: str, session_id: str, project_id: str | None = None) -> SessionAddress:
    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)


@dataclass
class Notice:
    """One section submitted to a Parent Session through the completion coordinator."""

    notice_id: str
    target: SessionAddress
    origin_run_id: str
    body: str
    wake: bool
    execution_owner: Any | None


class RecordingTriggerService:
    """Completion delivery that records each section and persists it at once."""

    def __init__(self) -> None:
        self.notices: list[Notice] = []

    def submit_completion(
        self,
        agent_id: str,
        session_id: str,
        *,
        notice_id: str,
        origin_run_id: str,
        body: str,
        project_id: str | None = None,
        on_persisted: Callable[[], None] | None = None,
        execution_owner: Any | None = None,
        wake: bool = True,
    ) -> asyncio.Future[None]:
        self.notices.append(
            Notice(
                notice_id,
                address(agent_id, session_id, project_id),
                origin_run_id,
                body,
                wake,
                execution_owner,
            )
        )
        if on_persisted is not None:
            on_persisted()
        delivery: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        delivery.set_result(None)
        return delivery

    def to(self, target: SessionAddress) -> list[Notice]:
        return [notice for notice in self.notices if notice.target == target]


class FakeAgents:
    """The roster ``agent_ids``, plus the built-in Librarian that resolves but is never listed."""

    def __init__(self, agent_ids: set[str]) -> None:
        self._agent_ids = agent_ids

    def get(self, agent_id: str) -> SimpleNamespace:
        if agent_id == "librarian":
            return SimpleNamespace(id=agent_id, builtin="librarian")
        if agent_id not in self._agent_ids:
            raise AgentNotFoundError(f"Agent not found: {agent_id}")
        return SimpleNamespace(id=agent_id)

    def list(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(id=agent_id, name=agent_id) for agent_id in sorted(self._agent_ids)]


class FakeAgentResolver:
    """Resolves every known Agent; Session overrides and working Projects are the real resolver's.

    ``projects`` holds the ids of the Projects that exist.
    """

    def __init__(self, agents: FakeAgents, sessions: ChatSessionManager) -> None:
        self._agents = agents
        self.unusable_models: set[str] = set()
        # Agent id -> the resolution error its resolution raises.
        self.failures: dict[str, Exception] = {}
        self.projects: set[str] = set()
        self._overrides = AgentResolver(
            cast(Any, agents),
            cast(Any, SimpleNamespace(exists=self.projects.__contains__)),
            cast(Any, self),
            dict,
            sessions=sessions,
        )

    def require_configured(self, model: str) -> None:
        if model in self.unusable_models:
            raise ModelConfigurationError(f"model is not usable in this instance: {model}")

    async def resolve_agent_async(
        self, project_id: str | None, agent_id: str, *, session_id: str | None = None
    ) -> SimpleNamespace:
        if agent_id in self.failures:
            raise self.failures[agent_id]
        try:
            agent = self._agents.get(agent_id)
        except AgentNotFoundError as error:
            raise ResolutionAgentNotFoundError(str(error)) from error
        if session_id is None:
            return agent
        overrides = await self.session_overrides_async(address(agent_id, session_id, project_id))
        return SimpleNamespace(**{**vars(agent), **overrides.agent_changes()})

    async def require_model_configured_async(self, model: str) -> None:
        self.require_configured(model)

    async def session_overrides_async(self, target: SessionAddress) -> AgentOverrides:
        return await self._overrides.session_overrides_async(target)

    def update_session_overrides(
        self, target: SessionAddress, changes: dict[str, Any]
    ) -> AgentOverrides:
        return self._overrides.update_session_overrides(target, changes)

    async def session_working_project_async(self, target: SessionAddress) -> str | None:
        return await self._overrides.session_working_project_async(target)


@dataclass
class Turn:
    """One turn the Chat loop double was asked to run in a Sub-Agent Session."""

    content: str
    parent_agent_input: bool
    run: Run | None = None


class ScriptedLoop:
    """Chat loop double: a turn answers ``answer to <content>`` unless held or scripted."""

    def __init__(self) -> None:
        self.turns: list[Turn] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.answers: dict[str, str] = {}

    def hold(self, content: str) -> asyncio.Event:
        """Keep every turn for *content* running until the returned event is set."""
        return self.gates.setdefault(content, asyncio.Event())

    def run_executor(
        self,
        content: str,
        *,
        reply_surface: Any = None,
        temporary_parent_binding: Any = None,
        parent_agent_input: bool = False,
    ) -> Callable[[Run], Any]:
        del reply_surface, temporary_parent_binding
        turn = Turn(content, parent_agent_input)
        self.turns.append(turn)

        async def execute(run: Run) -> ChatMessage:
            turn.run = run
            gate = self.gates.get(content)
            if gate is not None:
                await gate.wait()
            return ChatMessage.assistant(
                model="fixture", content=self.answers.get(content, f"answer to {content}")
            )

        return execute


class FakeStorage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.temporary_files = TemporaryFileManager(data_dir)
        self.settings: JsonObject = {}

    def load_subagent_settings(self) -> JsonObject:
        return dict(self.settings)


class SubAgentHarness:
    """The registered Sub-Agent Tools over real Sessions and a real Run manager."""

    def __init__(self, data_dir: Path) -> None:
        if not (data_dir / "data-store.json").exists():
            write_bootstrap_marker(data_dir)
        self.sessions = ChatSessionManager(data_dir)
        self.manager = ChatRunManager(persistence=self.sessions)
        self.loop = ScriptedLoop()
        self.storage = FakeStorage(data_dir)
        agents = FakeAgents({"parent", "worker"})
        self.resolver = FakeAgentResolver(agents, self.sessions)
        self.runtime = SimpleNamespace(
            agents=agents,
            agent_resolver=self.resolver,
            projects=SimpleNamespace(list=list),
            chat_sessions=self.sessions,
            chat_run_manager=self.manager,
            storage=self.storage,
            streaming_chat_loop=self.loop,
            terminal_manager=None,
        )
        self.triggers = RecordingTriggerService()
        self.coordinator = SubAgentCoordinator(cast(Any, self.runtime), self.triggers)
        self.coordinator.install(self.manager)
        self.registry = ToolRegistry()
        register_subagent_tools(self.registry, self.coordinator)
        self.parent = self.sessions.create("parent").address
        self.events: list[tuple[str, JsonObject]] = []

    def context(
        self,
        session: SessionAddress | None = None,
        *,
        tool_name: str = SUBAGENT_TOOL_NAME,
        run_id: str = "parent-run",
        allowed_agents: list[str] | None = None,
        execution_owner: Any | None = None,
        working_project_id: str | None = None,
    ) -> ToolContext:
        session = session or self.parent
        # Chat grants message_parent to Sub-Agent Sessions; the Tool itself checks the link.
        grants = (MESSAGE_PARENT_TOOL_NAME,) if tool_name == MESSAGE_PARENT_TOOL_NAME else ()
        return ToolContext(
            agent_id=session.agent_id,
            session_id=session.session_id,
            project_id=session.project_id,
            run_id=run_id,
            tool_call_id="tool-call-one",
            tool_name=tool_name,
            tool_call_index=0,
            workspace=Path("workspace"),
            vbot_root=Path("app"),
            data_root=Path("data"),
            execution_owner=execution_owner,
            working_project_id=working_project_id,
            session_tool_grants=grants,
            emit_hook=self._record_event,
            tool_settings=(
                None if allowed_agents is None else {"subagent": {"allowed_agents": allowed_agents}}
            ),
        )

    async def call(
        self,
        arguments: JsonObject,
        session: SessionAddress | None = None,
        *,
        tool_name: str = SUBAGENT_TOOL_NAME,
        **context_options: Any,
    ) -> JsonObject:
        """Dispatch one Agent call and return what the Agent receives."""
        context = self.context(session, tool_name=tool_name, **context_options)
        try:
            return cast(JsonObject, await self.registry.dispatch(context, arguments))
        except Exception as error:  # The Tool executor's failure envelope.
            return tool_failure_for_exception(context.tool_name, error)

    async def message_parent(self, content: str, session: SessionAddress) -> JsonObject:
        return await self.call({"content": content}, session, tool_name=MESSAGE_PARENT_TOOL_NAME)

    async def spawn(
        self, task: str = "review", session: SessionAddress | None = None, **arguments: Any
    ) -> JsonObject:
        """Start a Sub-Agent successfully and return the result data."""
        result = await self.call(
            {"description": f"Do {task}", "content": task, **arguments}, session
        )
        assert result["ok"], result
        return cast(JsonObject, result["data"])

    def subagent_session(self, subagent_id: str) -> SessionAddress:
        link = find_link(self.sessions, subagent_id)
        assert link is not None, subagent_id
        return link.session

    async def finished(self, content: str) -> None:
        """Release the held turns for *content* and let their answers be forwarded."""
        self.loop.hold(content).set()
        await self.settle()

    async def start_turn(
        self, session: SessionAddress, content: str, *, kind: RunKind = RunKind.SYSTEM
    ) -> Run:
        """Start a turn in *session* the way a delivery or the user would."""
        return await self.manager.start(
            session,
            self.loop.run_executor(content),
            admission=RunAdmission(run_kind=kind),
        )

    async def settle(self) -> None:
        """Wait until no Run is active and every forwarding task has finished."""
        for _ in range(400):
            await asyncio.sleep(0.005)
            active = [run for run in self.manager.active_runs() if run.status is RunStatus.RUNNING]
            if not active and not self.coordinator._forwarding.followed_runs():  # noqa: SLF001
                break
        await self.coordinator.drain_activity()

    async def until(self, condition: Callable[[], bool]) -> None:
        for _ in range(400):
            if condition():
                return
            await asyncio.sleep(0.005)
        raise AssertionError("condition not reached")

    async def close(self) -> None:
        for gate in self.loop.gates.values():
            gate.set()
        await self.manager.aclose()
        await self.settle()
        self.sessions.close()

    async def _record_event(self, event_type: str, payload: JsonObject) -> None:
        self.events.append((event_type, payload))


@pytest_asyncio.fixture
async def harness(tmp_path: Path, current_format_data_directory: None) -> AsyncIterator[Any]:
    del current_format_data_directory
    subject = SubAgentHarness(tmp_path)
    try:
        yield subject
    finally:
        await subject.close()
