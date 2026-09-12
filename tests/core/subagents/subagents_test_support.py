"""Shared fixtures and fakes for subagents behavior tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.agents import AgentNotFoundError
from core.chat import ChatMessage, ChatSessionManager
from core.projects import AgentResolutionError
from core.runs import (
    DEFAULT_RUN_ADMISSION,
    ActiveRunError,
    Run,
    RunAdmission,
    RunNotFoundError,
)
from core.sessions import SessionAddress
from core.storage import TemporaryFileManager
from core.subagents.subagents import _handle_subagent as _handle_subagent_impl
from core.subagents.tracker import SubAgentBatchTracker
from core.tools.tools import ToolContext

JsonObject = dict[str, Any]


SUBAGENT_TOOL_NAME = "subagent"


def _address(
    agent_id: str,
    session_id: str,
    project_id: str | None = None,
) -> SessionAddress:
    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)


async def _handle_subagent(
    context: ToolContext,
    arguments: JsonObject,
    *,
    runtime: Any,
    batch_tracker: SubAgentBatchTracker,
) -> JsonObject:
    canonical = {"action": "run", **arguments}
    return await _handle_subagent_impl(
        context,
        canonical,
        runtime=runtime,
        batch_tracker=batch_tracker,
    )


def make_context(
    *,
    agent_id: str = "parent",
    session_id: str = "parent-session",
    run_id: str = "parent-run",
    project_id: str | None = None,
    nesting_depth: int = 0,
    emit_hook: Any | None = None,
    allowed_agents: list[str] | None = None,
    result_persisted_hook: Any | None = None,
) -> ToolContext:
    return ToolContext(
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        tool_call_id="tool-call-one",
        tool_name=SUBAGENT_TOOL_NAME,
        tool_call_index=0,
        workspace=Path("workspace"),
        vbot_root=Path("app"),
        data_root=Path("data"),
        project_id=project_id,
        nesting_depth=nesting_depth,
        emit_hook=emit_hook,
        result_persisted_hook=result_persisted_hook,
        tool_settings=(
            None if allowed_agents is None else {"subagent": {"allowed_agents": allowed_agents}}
        ),
    )


class RecordingTriggerService:
    def __init__(self) -> None:
        self.completion_deliveries: dict[str, asyncio.Future[None]] = {}

    async def trigger_run(
        self,
        agent_id: str,
        message: str,
        session_id: str | None = None,
        *,
        internal: bool = False,
        project_id: str | None = None,
    ) -> Run:
        return Run(run_id="trigger-run", agent_id=agent_id, session_id=session_id or "new")

    def submit_completion(
        self,
        agent_id: str,
        session_id: str,
        *,
        notice_id: str,
        origin_run_id: str,
        body: str,
        project_id: str | None = None,
        on_persisted: Any | None = None,
        execution_owner: object | None = None,
    ) -> asyncio.Future[None]:
        del agent_id, session_id, origin_run_id, body, project_id, on_persisted
        delivery: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.completion_deliveries[notice_id] = delivery
        return delivery

    def cancel_completion(
        self,
        agent_id: str,
        session_id: str,
        *,
        notice_id: str,
        project_id: str | None = None,
    ) -> bool:
        del agent_id, session_id, project_id
        delivery = self.completion_deliveries.pop(notice_id, None)
        if delivery is None:
            return False
        if not delivery.done():
            delivery.cancel()
        return True


class FakeStorage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.temporary_files = TemporaryFileManager(data_dir)

    def load_subagent_settings(self) -> JsonObject:
        return {}


class FakeAgents:
    def __init__(self, agent_ids: set[str] | None = None) -> None:
        self._agent_ids = agent_ids or {"parent", "worker"}

    def get(self, agent_id: str) -> SimpleNamespace:
        if agent_id not in self._agent_ids:
            raise AgentNotFoundError(f"Agent not found: {agent_id}")
        return SimpleNamespace(id=agent_id)


class FakeAgentResolver:
    """Resolver seam for the subagent target validation.

    Resolves the target under the parent run's project — identity or project,
    both delegate to the same known-id set here — and raises
    :class:`AgentResolutionError` for an unknown target, matching how the real
    resolver fails an off-Team / unknown-agent spawn. Records the
    ``(project_id, agent_id)`` it was asked to resolve so a test can prove the
    child inherits the parent's project.
    """

    def __init__(self, agents: FakeAgents) -> None:
        self._agents = agents
        self.calls: list[tuple[str | None, str]] = []

    def resolve_agent(
        self,
        project_id: str | None,
        agent_id: str,
        *,
        run_overrides: Any | None = None,
    ) -> SimpleNamespace:
        del run_overrides
        self.calls.append((project_id, agent_id))
        try:
            return self._agents.get(agent_id)
        except AgentNotFoundError as error:
            raise AgentResolutionError(str(error)) from error


class FakeRunManager:
    """Run manager that records the project_id passed to start/enqueue."""

    def __init__(self) -> None:
        self.parent_run = Run(run_id="parent-run", agent_id="parent", session_id="parent-session")
        self.started: list[dict[str, Any]] = []
        self.runs: dict[str, Run] = {self.parent_run.id: self.parent_run}
        self.busy_sessions: dict[tuple[str, str], Run] = {}

    async def start(
        self,
        address: SessionAddress,
        executor: Any,
        *,
        admission: RunAdmission = DEFAULT_RUN_ADMISSION,
    ) -> Run:
        agent_id = address.agent_id
        session_id = address.session_id
        if (agent_id, session_id) in self.busy_sessions:
            raise ActiveRunError(f"session already has an active run: {session_id}")
        run = Run(
            run_id=f"sub-run-{len(self.started) + 1}",
            agent_id=agent_id,
            session_id=session_id,
            project_id=address.project_id,
            working_project_id=admission.working_project_id,
            run_kind=admission.run_kind,
            work_id=admission.work_id,
        )
        self.started.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "executor": executor,
                "project_id": address.project_id,
                "working_project_id": admission.working_project_id,
                "run_kind": admission.run_kind,
                "work_id": admission.work_id,
                "execution_owner": admission.owner,
                "run": run,
            }
        )
        self.runs[run.id] = run
        return run

    def get(self, run_id: str) -> Run:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise RunNotFoundError(f"run not found: {run_id}") from exc

    def active_run(
        self, *, agent_id: str, session_id: str, project_id: str | None = None
    ) -> Run | None:
        return self.busy_sessions.get((agent_id, session_id))

    def list_queued(
        self, agent_id: str, session_id: str, *, project_id: str | None = None
    ) -> list[Any]:
        return []


class FakeChildLoop:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def child_loop(self, *, nesting_depth: int) -> FakeChildLoop:
        del nesting_depth
        return self

    def run_executor(self, content: str, *, agent_overrides: Any | None = None) -> Any:
        # The project anchor rides ``run.project_id`` (set by the run manager
        # from the project_id passed to start/enqueue), not the executor closure.
        del agent_overrides

        async def _execute(run: Run) -> ChatMessage:
            return ChatMessage.assistant(model="openai/gpt-5.2", content=f"handled: {content}")

        return _execute


def make_runtime(
    tmp_path: Path, manager: FakeRunManager, *, agent_ids: set[str] | None = None
) -> Any:
    child_loop = FakeChildLoop(None)
    agents = FakeAgents(agent_ids)
    return SimpleNamespace(
        agents=agents,
        agent_resolver=FakeAgentResolver(agents),
        chat_sessions=ChatSessionManager(tmp_path),
        chat_run_manager=manager,
        storage=FakeStorage(tmp_path),
        streaming_chat_loop=child_loop,
    )
