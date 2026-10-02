"""Runtime doubles and a dispatch harness for ``subagent`` Tool behavior tests.

Every call goes through the registered ``subagent`` Tool the way the Tool
executor runs an Agent's call: argument normalization, contract validation and
then the coordinator. The Run manager, Agent resolver, completion delivery and
chat loop are doubles; Sessions and the batch tracker are real.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
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
from core.runs import (
    DEFAULT_RUN_ADMISSION,
    ActiveRunError,
    ChatRunManager,
    Run,
    RunAdmission,
    RunCancelledError,
    RunNotFoundError,
    RunStatus,
)
from core.sessions import SessionAddress
from core.storage import TemporaryFileManager
from core.subagents import SubAgentBatchTracker, SubAgentCoordinator
from core.subagents._constants import SUBAGENT_ACTIVITY_NOTE_TEMPLATE
from core.tools.subagent import SUBAGENT_TOOL_NAME, register_subagent_tools
from core.tools.tools import ToolContext, ToolRegistry, tool_failure_for_exception

JsonObject = dict[str, Any]

# Loop turns that let completion watchers and delivery callbacks run.
SETTLE_TICKS = 5
PARENT = ("parent", "parent-session")


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


def address(agent_id: str, session_id: str, project_id: str | None = None) -> SessionAddress:
    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)


def activity_path_from_note(note: str) -> str:
    """Extract the concrete activity path from an ``activity_note`` sentence."""
    before, after = SUBAGENT_ACTIVITY_NOTE_TEMPLATE.split("{path}")
    assert note.startswith(before), note
    assert note.endswith(after), note
    return note[len(before) : len(note) - len(after)]


def done(content: str = "done", **fields: Any) -> ChatMessage:
    return ChatMessage.assistant(model="fixture", content=content, **fields)


@dataclass
class Notice:
    """One automatic completion notice submitted for a Parent Session."""

    notice_id: str
    agent_id: str
    session_id: str
    project_id: str | None
    body: str
    execution_owner: Any | None


class RecordingTriggerService:
    """Completion delivery that records notices and persists them on demand."""

    def __init__(self) -> None:
        self.notices: list[Notice] = []
        self.deliveries: dict[str, asyncio.Future[None]] = {}
        self.cancelled_notice_ids: list[str] = []
        self.error: BaseException | None = None
        self.defer_persistence = False
        self._unpersisted: dict[str, Callable[[], None]] = {}

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
        execution_owner: Any | None = None,
    ) -> asyncio.Future[None]:
        assert origin_run_id
        delivery: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.deliveries[notice_id] = delivery
        if self.error is not None:
            delivery.set_exception(self.error)
            return delivery
        self.notices.append(
            Notice(notice_id, agent_id, session_id, project_id, body, execution_owner)
        )

        def persist() -> None:
            if callable(on_persisted):
                on_persisted()
            if not delivery.done():
                delivery.set_result(None)

        if self.defer_persistence:
            self._unpersisted[notice_id] = persist
        else:
            persist()
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
        self.cancelled_notice_ids.append(notice_id)
        self._unpersisted.pop(notice_id, None)
        delivery = self.deliveries.pop(notice_id, None)
        if delivery is None:
            return False
        if not delivery.done():
            delivery.cancel()
        return True

    def persist(self) -> None:
        """Persist every deferred notice in its Parent Session."""
        pending = list(self._unpersisted.values())
        self._unpersisted.clear()
        for persist in pending:
            persist()

    @property
    def bodies(self) -> list[str]:
        return [notice.body for notice in self.notices]


class FakeAgents:
    def __init__(self, agent_ids: set[str] | None = None) -> None:
        self._agent_ids = agent_ids or {"parent", "worker"}

    def get(self, agent_id: str) -> SimpleNamespace:
        if agent_id not in self._agent_ids:
            raise AgentNotFoundError(f"Agent not found: {agent_id}")
        return SimpleNamespace(id=agent_id)

    def list(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(id=agent_id, name=agent_id) for agent_id in sorted(self._agent_ids)]


class FakeModelChecker:
    """Every Model can run except those in ``unusable``."""

    def __init__(self) -> None:
        self.unusable: set[str] = set()

    def require_configured(self, model: str) -> None:
        if model in self.unusable:
            raise ModelConfigurationError(f"model is not usable in this instance: {model}")


class FakeAgentResolver:
    """Resolves every known Agent in any scope and records each request.

    Session Agent overrides are the real resolver's, stored in the harness's real
    Sessions, and a resolution that names its Session applies them after checking
    their Model, as the real resolver does.
    """

    def __init__(self, agents: FakeAgents, sessions: ChatSessionManager) -> None:
        self._agents = agents
        self.models = FakeModelChecker()
        self._overrides = AgentResolver(
            cast(Any, agents), cast(Any, None), cast(Any, self.models), dict, sessions=sessions
        )
        self.calls: list[tuple[str | None, str, str | None]] = []

    async def resolve_agent_async(
        self, project_id: str | None, agent_id: str, *, session_id: str | None = None
    ) -> SimpleNamespace:
        self.calls.append((project_id, agent_id, session_id))
        try:
            agent = self._agents.get(agent_id)
        except AgentNotFoundError as error:
            raise ResolutionAgentNotFoundError(str(error)) from error
        if session_id is None:
            return agent
        overrides = await self.session_overrides_async(address(agent_id, session_id, project_id))
        if overrides.model is not None:
            self.models.require_configured(overrides.model)
        return SimpleNamespace(**{**vars(agent), **overrides.agent_changes()})

    async def require_model_configured_async(self, model: str) -> None:
        self.models.require_configured(model)

    async def session_overrides_async(self, target: SessionAddress) -> AgentOverrides:
        return await self._overrides.session_overrides_async(target)

    def update_session_overrides(
        self, target: SessionAddress, changes: dict[str, Any]
    ) -> AgentOverrides:
        return self._overrides.update_session_overrides(target, changes)


class RaisingResolver:
    """Resolver whose every lookup fails with ``error``."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls: list[tuple[str | None, str, str | None]] = []

    async def resolve_agent_async(
        self, project_id: str | None, agent_id: str, *, session_id: str | None = None
    ) -> Any:
        self.calls.append((project_id, agent_id, session_id))
        raise self.error


@dataclass
class Child:
    """One child Run the coordinator started or queued."""

    run: Run
    executor: Any
    admission: RunAdmission
    item: Any = None  # The Queue item of queued work.
    display_content: str = ""

    async def task(self) -> str:
        """Return the task text this child's executor was built for."""
        received = await self.executor(self.run)
        return str(received.content).removeprefix("handled: ")


class FakeRunManager:
    """Run manager whose child Runs stay running until a test settles them.

    A Session is busy while its active Run is running: ``make_busy`` marks one,
    and a released Queue item becomes its Session's active Run.
    """

    def __init__(self) -> None:
        self.parent_run = Run(run_id="parent-run", agent_id="parent", session_id="parent-session")
        self.runs: dict[str, Run] = {self.parent_run.id: self.parent_run}
        self.started: list[Child] = []
        self.enqueued: list[Child] = []
        self.hold_enqueued_starts = False
        self.start_error: BaseException | None = None
        self.next_result: Any | None = None
        self.held: list[Child] = []
        self.forgotten: list[Run] = []
        self._active: dict[tuple[str | None, str, str], Run] = {}

    def make_busy(
        self,
        agent_id: str,
        session_id: str,
        *,
        project_id: str | None = None,
        work_id: str | None = None,
    ) -> Run:
        run = Run(
            run_id=f"busy-{session_id}",
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
            work_id=work_id,
        )
        self.runs[run.id] = run
        self._active[(project_id, agent_id, session_id)] = run
        return run

    async def start(
        self,
        target: SessionAddress,
        executor: Any,
        *,
        admission: RunAdmission = DEFAULT_RUN_ADMISSION,
    ) -> Run:
        if self.start_error is not None:
            raise self.start_error
        if self._active_run(target) is not None:
            raise ActiveRunError(f"session already has an active run: {target.session_id}")
        run = self._run(f"sub-run-{len(self.started) + 1}", target, admission)
        self.started.append(Child(run, executor, admission))
        self._finish_with_next_result(run)
        return run

    async def enqueue(
        self,
        target: SessionAddress,
        executor: Any,
        *,
        display_content: str = "",
        internal: bool = False,
        admission: RunAdmission = DEFAULT_RUN_ADMISSION,
    ) -> Any:
        assert internal is False
        future: asyncio.Future[Run] = asyncio.get_running_loop().create_future()
        item = SimpleNamespace(
            future=future, item_id=f"queued-item-{len(self.enqueued) + 1}", admission=admission
        )
        run = self._run(f"queued-sub-run-{len(self.enqueued) + 1}", target, admission)
        child = Child(run, executor, admission, item=item, display_content=display_content)
        self.enqueued.append(child)
        if self.hold_enqueued_starts:
            self.held.append(child)
        else:
            self._admit(child)
        return item

    def release_next_enqueued_start(self) -> Run:
        """Start the oldest held Queue item as its Session's active Run."""
        child = self.held.pop(0)
        self._admit(child)
        return child.run

    def fail_next_enqueued_start(self, error: BaseException) -> None:
        """Let the oldest held Queue item fail its admission."""
        self.held.pop(0).item.future.set_exception(error)

    def remove_queued(
        self, agent_id: str, session_id: str, item_id: str, *, project_id: str | None = None
    ) -> bool:
        for child in self.queued_children(agent_id, session_id, project_id=project_id):
            if child.item.item_id == item_id:
                self.held.remove(child)
                child.item.future.cancel()
                return True
        return False

    def get(self, run_id: str) -> Run:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise RunNotFoundError(f"run not found: {run_id}") from exc

    def forget(self, run_id: str) -> None:
        """Drop a Run from the manager, as the manager does with released Runs."""
        self.forgotten.append(self.runs.pop(run_id))

    async def cancel(
        self, run_id: str, reason: str | None = None, *, initiator: str | None = None
    ) -> Run:
        run = self.get(run_id)
        run.request_cancel(reason=reason, initiator=initiator)
        if run.status is RunStatus.RUNNING:
            run.mark_cancelled()
        with suppress(RunCancelledError):
            await run.wait()
        return run

    def active_run(
        self, *, agent_id: str, session_id: str, project_id: str | None = None
    ) -> Run | None:
        return self._active_run(address(agent_id, session_id, project_id))

    def queued_children(
        self, agent_id: str, session_id: str, *, project_id: str | None = None
    ) -> list[Child]:
        return [
            child
            for child in self.held
            if (child.run.project_id, child.run.agent_id, child.run.session_id)
            == (project_id, agent_id, session_id)
        ]

    def list_queued(
        self, agent_id: str, session_id: str, *, project_id: str | None = None
    ) -> list[Any]:
        return [
            child.item
            for child in self.queued_children(agent_id, session_id, project_id=project_id)
        ]

    def _active_run(self, target: SessionAddress) -> Run | None:
        run = self._active.get((target.project_id, target.agent_id, target.session_id))
        return run if run is not None and run.status is RunStatus.RUNNING else None

    def _admit(self, child: Child) -> None:
        run = child.run
        self._active[(run.project_id, run.agent_id, run.session_id)] = run
        child.item.future.set_result(run)
        self._finish_with_next_result(run)

    def _run(self, run_id: str, target: SessionAddress, admission: RunAdmission) -> Run:
        run = Run(
            run_id=run_id,
            agent_id=target.agent_id,
            session_id=target.session_id,
            project_id=target.project_id,
            working_project_id=admission.working_project_id,
            run_kind=admission.run_kind,
            work_id=admission.work_id,
            execution_owner=admission.owner,
        )
        self.runs[run.id] = run
        return run

    def _finish_with_next_result(self, run: Run) -> None:
        if self.next_result is None:
            return

        async def complete() -> None:
            await asyncio.sleep(0)
            run.mark_completed(self.next_result)

        asyncio.create_task(complete())


class FakeChatLoop:
    """Streaming loop double that records how each child task was built."""

    def __init__(self, *, nesting_depth: int = 0, tasks: dict[str, Any] | None = None) -> None:
        self.nesting_depth = nesting_depth
        # Task text -> the nesting depth it was built with.
        self.tasks: dict[str, SimpleNamespace] = {} if tasks is None else tasks

    def child_loop(self, *, nesting_depth: int) -> FakeChatLoop:
        return FakeChatLoop(nesting_depth=nesting_depth, tasks=self.tasks)

    def run_executor(self, content: str) -> Any:
        self.tasks[content] = SimpleNamespace(nesting_depth=self.nesting_depth)

        async def execute(run: Run) -> ChatMessage:
            del run
            return ChatMessage.assistant(model="fixture", content=f"handled: {content}")

        return execute


class ExecutorLoop:
    """Chat loop double whose every child Run runs ``executor``."""

    def __init__(self, executor: Callable[[Run], Any]) -> None:
        self._executor = executor

    def child_loop(self, *, nesting_depth: int) -> ExecutorLoop:
        del nesting_depth
        return self

    def run_executor(self, content: str) -> Any:
        del content
        return self._executor


class FakeStorage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.temporary_files = TemporaryFileManager(data_dir)
        self.settings: JsonObject = {}

    def load_subagent_settings(self) -> JsonObject:
        return dict(self.settings)


class SubAgentHarness:
    """The registered ``subagent`` Tool over runtime doubles and real Sessions."""

    def __init__(self, data_dir: Path, *, manager: Any | None = None) -> None:
        if not (data_dir / "data-store.json").exists():
            write_bootstrap_marker(data_dir)
        self.manager = manager if manager is not None else FakeRunManager()
        self.loop = FakeChatLoop()
        self.storage = FakeStorage(data_dir)
        self.sessions = ChatSessionManager(data_dir)
        self.runtime = SimpleNamespace(
            projects=SimpleNamespace(list=list),
            chat_sessions=self.sessions,
            chat_run_manager=self.manager,
            storage=self.storage,
            streaming_chat_loop=self.loop,
        )
        self.use_agents({"parent", "worker"})
        self.triggers = RecordingTriggerService()
        self.tracker = SubAgentBatchTracker(self.triggers, sessions=self.sessions)
        self.coordinator = SubAgentCoordinator(
            cast(Any, self.runtime), self.triggers, batch_tracker=self.tracker
        )
        self.registry = ToolRegistry()
        register_subagent_tools(self.registry, self.coordinator)

    def use_agents(self, agent_ids: set[str]) -> None:
        agents = FakeAgents(agent_ids)
        self.runtime.agents = agents
        self.runtime.agent_resolver = FakeAgentResolver(agents, self.sessions)

    @property
    def resolver_calls(self) -> list[tuple[str | None, str]]:
        return [call[:2] for call in self.runtime.agent_resolver.calls]

    def stored_overrides(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> JsonObject | None:
        """Return the Agent overrides one child Session stores in its metadata."""
        return cast(
            "JsonObject | None",
            self.sessions.metadata_value(
                address(agent_id, session_id, project_id), "agent_overrides"
            ),
        )

    async def call(
        self, arguments: JsonObject, context: ToolContext | None = None, **context_options: Any
    ) -> JsonObject:
        """Dispatch one Agent call and return what the Agent receives."""
        context = context or make_context(**context_options)
        try:
            return cast(JsonObject, await self.registry.dispatch(context, arguments))
        except Exception as error:  # The Tool executor's failure envelope.
            return tool_failure_for_exception(context.tool_name, error)

    def call_in_background(
        self, arguments: JsonObject, context: ToolContext | None = None, **context_options: Any
    ) -> asyncio.Task[JsonObject]:
        """Start a call that waits inline, such as a nested foreground delegation."""
        return asyncio.create_task(self.call(arguments, context, **context_options))

    async def spawn(
        self, arguments: JsonObject, context: ToolContext | None = None, **context_options: Any
    ) -> JsonObject:
        """Delegate successfully and return the result data."""
        result = await self.call(arguments, context, **context_options)
        assert result["ok"], result
        return cast(JsonObject, result["data"])

    async def started(self, count: int = 1) -> list[Child]:
        """Return the started children once ``count`` exist.

        A spawn opens its child Session on the Session database's worker pool,
        so the child Run starts a few loop turns after the call began.
        """
        return await self._wait_for(lambda: self.manager.started, count)

    async def enqueued(self, count: int = 1) -> list[Child]:
        return await self._wait_for(lambda: self.manager.enqueued, count)

    async def settle(self) -> None:
        for _ in range(SETTLE_TICKS):
            await asyncio.sleep(0)

    def owned(self, work_id: str, *, project_id: str | None = None) -> Any:
        """Return the tracked entry for ``work_id`` in the default Parent Session."""
        owned = self.tracker.owned_entry(*PARENT, project_id, work_id)
        return None if owned is None else owned[1]

    def owned_work(self, *, project_id: str | None = None) -> list[str]:
        return [entry.work_id for _, entry in self.tracker.owned_entries(*PARENT, project_id)]

    def unread(self, agent_id: str = "worker", project_id: str | None = None) -> bool:
        summaries = self.sessions.list_summaries(agent_id, project_id=project_id)
        return bool(summaries[0]["has_unread_completion"])

    async def close(self) -> None:
        if isinstance(self.manager, ChatRunManager):
            await self.manager.aclose()
        else:
            for child in self.manager.held:
                child.item.future.cancel()
            self.manager.held.clear()
            for run in [*self.manager.runs.values(), *self.manager.forgotten]:
                if run.status is RunStatus.RUNNING:
                    run.mark_completed(done())
            await self.settle()
        await self.coordinator.drain_activity()
        for delivery in self.triggers.deliveries.values():
            delivery.cancel()
        self.sessions.close()

    @staticmethod
    async def _wait_for(children: Callable[[], list[Child]], count: int) -> list[Child]:
        for _ in range(500):
            if len(children()) >= count:
                return list(children())
            await asyncio.sleep(0.01)
        raise AssertionError(f"expected {count} child Run(s), got {len(children())}")


@pytest_asyncio.fixture
async def harness(tmp_path: Path, current_format_data_directory: None) -> AsyncIterator[Any]:
    del current_format_data_directory
    subject = SubAgentHarness(tmp_path)
    try:
        yield subject
    finally:
        await subject.close()
