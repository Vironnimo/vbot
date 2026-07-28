"""Shared runtime doubles, helpers, and imports for sub-agent tool tests."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import core.chat as chat_api
import core.subagents.subagents as subagent_module
from core.agents import AgentNotFoundError
from core.chat import ChatMessage, ChatSessionManager
from core.projects import AgentResolutionError
from core.runs import ActiveRunError, Run, RunCancelledError, RunNotFoundError
from core.storage import TemporaryFileManager
from core.subagents.subagents import (
    SubAgentBatchTracker,
    SubAgentCoordinator,
    _handle_subagent_status,
    _wait_for_subagent_result,
)
from core.subagents.subagents import (
    _handle_subagent as _handle_subagent_impl,
)
from core.tools.subagent import (
    SUBAGENT_TOOL_NAME,
    register_subagent_tools,
)
from core.tools.tools import ToolContext, ToolRegistry

__all__ = [
    "asyncio",
    "Path",
    "SimpleNamespace",
    "Any",
    "cast",
    "pytest",
    "chat_api",
    "subagent_module",
    "AgentNotFoundError",
    "ChatMessage",
    "ChatSessionManager",
    "AgentResolutionError",
    "ActiveRunError",
    "Run",
    "RunCancelledError",
    "RunNotFoundError",
    "SubAgentBatchTracker",
    "SubAgentCoordinator",
    "_handle_subagent",
    "_handle_subagent_result",
    "_wait_for_subagent_result",
    "SUBAGENT_TOOL_NAME",
    "register_subagent_tools",
    "ToolContext",
    "ToolRegistry",
    "JsonObject",
    "BACKGROUND_TASK_SETTLE_TICKS",
    "make_context",
    "RecordingTriggerService",
    "FakeStorage",
    "FakeAgents",
    "FakeAgentResolver",
    "FakeRunManager",
    "FakeChatLoop",
    "make_runtime",
]


pytestmark = pytest.mark.asyncio

JsonObject = dict[str, Any]
BACKGROUND_TASK_SETTLE_TICKS = 5


async def _handle_subagent(
    context: ToolContext,
    arguments: JsonObject,
    *,
    runtime: Any,
    batch_tracker: SubAgentBatchTracker,
) -> JsonObject:
    """Call the canonical handler while keeping behavioral tests compact."""
    canonical = {"action": "run", **arguments}
    return await _handle_subagent_impl(
        context,
        canonical,
        runtime=runtime,
        batch_tracker=batch_tracker,
    )


async def _handle_subagent_result(
    context: ToolContext,
    arguments: JsonObject,
    *,
    runtime: Any,
    batch_tracker: SubAgentBatchTracker,
) -> JsonObject:
    """Exercise the status action for result-oriented coordinator tests."""
    return await _handle_subagent_status(
        context,
        {"action": "status", **arguments},
        runtime=runtime,
        batch_tracker=batch_tracker,
    )


def make_context(
    *,
    agent_id: str = "parent",
    session_id: str = "parent-session",
    run_id: str = "parent-run",
    tool_name: str = SUBAGENT_TOOL_NAME,
    nesting_depth: int = 0,
    emit_hook: Any | None = None,
    project_id: str | None = None,
) -> ToolContext:
    return ToolContext(
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        tool_call_id="tool-call-one",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=Path("workspace"),
        vbot_root=Path("app"),
        data_root=Path("data"),
        emit_hook=emit_hook,
        nesting_depth=nesting_depth,
        project_id=project_id,
    )


class RecordingTriggerService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, bool]] = []
        self.error: BaseException | None = None

    async def trigger_run(
        self,
        agent_id: str,
        message: str,
        session_id: str | None = None,
        *,
        internal: bool = False,
        project_id: str | None = None,
    ) -> Run:
        if self.error is not None:
            raise self.error
        self.calls.append((agent_id, message, session_id, internal))
        return Run(run_id="trigger-run", agent_id=agent_id, session_id=session_id or "new-session")


class FakeStorage:
    def __init__(self, data_dir: Path, settings: JsonObject | None = None) -> None:
        self.data_dir = data_dir
        self.temporary_files = TemporaryFileManager(data_dir)
        self._settings = settings or {}

    def load_subagent_settings(self) -> JsonObject:
        return dict(self._settings)


class FakeAgents:
    def __init__(self, agent_ids: set[str] | None = None) -> None:
        self._agent_ids = agent_ids or {"parent", "worker"}

    def get(self, agent_id: str) -> SimpleNamespace:
        if agent_id not in self._agent_ids:
            raise AgentNotFoundError(f"Agent not found: {agent_id}")
        return SimpleNamespace(id=agent_id)


class FakeAgentResolver:
    """Resolver seam used by sub-agent target validation.

    Delegates to ``FakeAgents`` and re-raises an unknown target as
    :class:`AgentResolutionError`, matching the real resolver's failure surface.
    """

    def __init__(self, agents: FakeAgents) -> None:
        self._agents = agents
        self.calls: list[tuple[str | None, str, Any | None]] = []

    def resolve_agent(
        self,
        project_id: str | None,
        agent_id: str,
        *,
        run_overrides: Any | None = None,
    ) -> SimpleNamespace:
        self.calls.append((project_id, agent_id, run_overrides))
        try:
            return self._agents.get(agent_id)
        except AgentNotFoundError as error:
            raise AgentResolutionError(str(error)) from error


class FakeRunManager:
    def __init__(self, parent_run: Run | None = None) -> None:
        self.parent_run = parent_run or Run(
            run_id="parent-run",
            agent_id="parent",
            session_id="parent-session",
        )
        self.started: list[tuple[str, str, Any, Run]] = []
        self.enqueued: list[dict[str, Any]] = []
        self.hold_enqueued_starts = False
        self._pending_enqueued_starts: list[tuple[SimpleNamespace, Run]] = []
        self.runs: dict[str, Run] = {self.parent_run.id: self.parent_run}
        self.busy_sessions: dict[
            tuple[str, str] | tuple[str | None, str, str],
            Run,
        ] = {}
        self.start_error: BaseException | None = None
        self.next_result: Any | None = None
        self.next_error: BaseException | None = None

    async def start(
        self,
        *,
        agent_id: str,
        session_id: str,
        executor: Any,
        project_id: str | None = None,
        working_project_id: str | None = None,
    ) -> Run:
        if self.start_error is not None:
            raise self.start_error
        if (agent_id, session_id) in self.busy_sessions:
            raise ActiveRunError(f"session already has an active run: {session_id}")
        run = Run(
            run_id=f"sub-run-{len(self.started) + 1}",
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
            working_project_id=working_project_id,
        )
        self.started.append((agent_id, session_id, executor, run))
        self.runs[run.id] = run
        self._schedule_terminal_state(run)
        return run

    async def enqueue(
        self,
        *,
        agent_id: str,
        session_id: str,
        executor: Any,
        display_content: str = "",
        internal: bool = False,
        project_id: str | None = None,
        working_project_id: str | None = None,
    ) -> Any:
        future: asyncio.Future[Run] = asyncio.get_running_loop().create_future()
        item = SimpleNamespace(
            future=future,
            item_id=f"queued-item-{len(self.enqueued) + 1}",
        )
        run = Run(
            run_id=f"queued-sub-run-{len(self.enqueued) + 1}",
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
            working_project_id=working_project_id,
        )
        self.enqueued.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "executor": executor,
                "display_content": display_content,
                "internal": internal,
                "project_id": project_id,
                "working_project_id": working_project_id,
                "item": item,
                "run": run,
            }
        )
        self.runs[run.id] = run
        if self.hold_enqueued_starts:
            self._pending_enqueued_starts.append((item, run))
        else:
            future.set_result(run)
            self._schedule_terminal_state(run)
        return item

    def remove_queued(
        self, agent_id: str, session_id: str, item_id: str, *, project_id: str | None = None
    ) -> bool:
        for record in list(self.enqueued):
            item = record["item"]
            if (
                record["agent_id"] != agent_id
                or record["session_id"] != session_id
                or item.item_id != item_id
                or item.future.done()
            ):
                continue
            self.enqueued.remove(record)
            self._pending_enqueued_starts = [
                pending for pending in self._pending_enqueued_starts if pending[0] is not item
            ]
            if not item.future.done():
                item.future.cancel()
            return True
        return False

    def release_next_enqueued_start(self) -> Run:
        item, run = self._pending_enqueued_starts.pop(0)
        item.future.set_result(run)
        self._schedule_terminal_state(run)
        return run

    def get(self, run_id: str) -> Run:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise RunNotFoundError(f"run not found: {run_id}") from exc

    async def cancel(self, run_id: str, reason: str | None = None) -> Run:
        run = self.get(run_id)
        run.request_cancel(reason=reason)
        if run.status.value == "running":
            run.mark_cancelled()
        with suppress(RunCancelledError):
            await run.wait()
        return run

    def active_run(
        self, *, agent_id: str, session_id: str, project_id: str | None = None
    ) -> Run | None:
        return self.busy_sessions.get((project_id, agent_id, session_id)) or self.busy_sessions.get(
            (agent_id, session_id)
        )

    def list_queued(
        self, agent_id: str, session_id: str, *, project_id: str | None = None
    ) -> list[Any]:
        return [
            record["item"]
            for record in self.enqueued
            if record["agent_id"] == agent_id
            and record["session_id"] == session_id
            and record["project_id"] == project_id
            and not record["item"].future.done()
        ]

    def _schedule_terminal_state(self, run: Run) -> None:
        if self.next_error is not None:
            asyncio.create_task(self._fail_next(run, self.next_error))
        elif self.next_result is not None:
            asyncio.create_task(self._complete_next(run, self.next_result))

    async def _complete_next(self, run: Run, result: Any) -> None:
        await asyncio.sleep(0)
        run.mark_completed(result)

    async def _fail_next(self, run: Run, error: BaseException) -> None:
        await asyncio.sleep(0)
        run.mark_failed(error)


class FakeChatLoop:
    seen_depths: list[int] = []
    seen_streaming: list[bool] = []
    seen_agent_overrides: list[Any | None] = []

    def __init__(
        self,
        runtime: Any,
        *,
        streaming: bool = False,
        attachment_resolver: Any | None = None,
        compaction_service: Any | None = None,
    ) -> None:
        self._runtime = runtime
        self._streaming = streaming
        self._attachment_resolver = attachment_resolver
        self._compaction_service = compaction_service
        self._nesting_depth = 0

    def child_loop(self, *, nesting_depth: int) -> FakeChatLoop:
        child = FakeChatLoop(
            self._runtime,
            streaming=self._streaming,
            attachment_resolver=self._attachment_resolver,
            compaction_service=self._compaction_service,
        )
        child._nesting_depth = nesting_depth
        self.seen_streaming.append(child._streaming)
        return child

    def run_executor(
        self,
        content: str,
        *,
        agent_overrides: Any | None = None,
    ) -> Any:
        self.seen_agent_overrides.append(agent_overrides)
        return lambda run: self._execute_run(run, content)

    async def _execute_run(self, run: Run, content: str) -> ChatMessage:
        self.seen_depths.append(self._nesting_depth)
        return ChatMessage.assistant(model="openai/gpt-5.2", content=f"handled: {content}")


def make_runtime(
    tmp_path: Path, manager: FakeRunManager, settings: JsonObject | None = None
) -> Any:
    agents = FakeAgents()
    return SimpleNamespace(
        agents=agents,
        agent_resolver=FakeAgentResolver(agents),
        chat_sessions=ChatSessionManager(tmp_path),
        chat_run_manager=manager,
        storage=FakeStorage(tmp_path, settings),
        streaming_chat_loop=FakeChatLoop(None, streaming=True),
    )
