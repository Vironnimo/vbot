"""Tests for sub-agent tools and batch tracking."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import core.chat as chat_api
import core.subagents.subagents as subagent_module
from core.agents import AgentNotFoundError
from core.chat import ChatMessage, ChatSessionManager
from core.runs import ActiveRunError, Run, RunNotFoundError
from core.subagents.subagents import (
    SubAgentBatchTracker,
    SubAgentCoordinator,
    _handle_subagent,
    _handle_subagent_result,
    _wait_for_subagent_result,
)
from core.tools.subagent import (
    SUBAGENT_RESULT_TOOL_NAME,
    SUBAGENT_TOOL_NAME,
    register_subagent_tools,
)
from core.tools.tools import ToolContext, ToolRegistry

pytestmark = pytest.mark.asyncio

JsonObject = dict[str, Any]
BACKGROUND_TASK_SETTLE_TICKS = 5


def make_context(
    *,
    agent_id: str = "parent",
    session_id: str = "parent-session",
    run_id: str = "parent-run",
    tool_name: str = SUBAGENT_TOOL_NAME,
    nesting_depth: int = 0,
    emit_hook: Any | None = None,
) -> ToolContext:
    return ToolContext(
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        tool_call_id="tool-call-one",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=Path("workspace"),
        app_root=Path("app"),
        data_root=Path("data"),
        emit_hook=emit_hook,
        nesting_depth=nesting_depth,
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
    ) -> Run:
        if self.error is not None:
            raise self.error
        self.calls.append((agent_id, message, session_id, internal))
        return Run(run_id="trigger-run", agent_id=agent_id, session_id=session_id or "new-session")


class FakeStorage:
    def __init__(self, settings: JsonObject | None = None) -> None:
        self.data_dir = Path("data")
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
        self.busy_sessions: dict[tuple[str, str], Run] = {}
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
    ) -> Run:
        del project_id
        if self.start_error is not None:
            raise self.start_error
        if (agent_id, session_id) in self.busy_sessions:
            raise ActiveRunError(f"session already has an active run: {session_id}")
        run = Run(
            run_id=f"sub-run-{len(self.started) + 1}",
            agent_id=agent_id,
            session_id=session_id,
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
    ) -> Any:
        del project_id
        future: asyncio.Future[Run] = asyncio.get_running_loop().create_future()
        item = SimpleNamespace(
            future=future,
            item_id=f"queued-item-{len(self.enqueued) + 1}",
        )
        run = Run(
            run_id=f"queued-sub-run-{len(self.enqueued) + 1}",
            agent_id=agent_id,
            session_id=session_id,
        )
        self.enqueued.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "executor": executor,
                "display_content": display_content,
                "internal": internal,
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

    def remove_queued(self, agent_id: str, session_id: str, item_id: str) -> bool:
        for record in list(self.enqueued):
            item = record["item"]
            if (
                record["agent_id"] != agent_id
                or record["session_id"] != session_id
                or item.item_id != item_id
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

    def active_run(self, *, agent_id: str, session_id: str) -> Run | None:
        return self.busy_sessions.get((agent_id, session_id))

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

    def run_executor(self, content: str, *, project_id: str | None = None) -> Any:
        del project_id
        return lambda run: self._execute_run(run, content)

    async def _execute_run(self, run: Run, content: str) -> ChatMessage:
        self.seen_depths.append(self._nesting_depth)
        return ChatMessage.assistant(model="openai/gpt-5.2", content=f"handled: {content}")


def make_runtime(
    tmp_path: Path, manager: FakeRunManager, settings: JsonObject | None = None
) -> Any:
    return SimpleNamespace(
        agents=FakeAgents(),
        chat_sessions=ChatSessionManager(tmp_path),
        chat_run_manager=manager,
        storage=FakeStorage(settings),
        streaming_chat_loop=FakeChatLoop(None, streaming=True),
    )


async def test_batch_tracker_triggers_once_when_all_sub_agents_complete() -> None:
    # Arrange
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "session-one", "run-one")
    tracker.register(parent_key, "worker", "session-two", "run-two")

    # Act
    tracker.on_sub_agent_complete(parent_key, "run-one", {"result": "First result"})
    await asyncio.sleep(0)
    tracker.on_sub_agent_complete(parent_key, "run-two", {"result": "Second result"})
    await asyncio.sleep(0)
    tracker.on_sub_agent_complete(parent_key, "run-two", {"result": "Second result again"})
    await asyncio.sleep(0)

    # Assert
    assert len(trigger_service.calls) == 1
    agent_id, message, session_id, internal = trigger_service.calls[0]
    assert agent_id == "parent"
    assert session_id == "parent-session"
    assert internal is True
    assert "Sub-agent batch completed." in message
    assert "### worker (session session-one) — completed" in message
    assert "First result" in message
    assert "### worker (session session-two) — completed" in message
    assert "Second result" in message


async def test_batch_tracker_delivers_complete_result_without_truncation() -> None:
    # Arrange
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "session-one", "run-one")
    long_result = "x" * 2000

    # Act
    tracker.on_sub_agent_complete(
        parent_key, "run-one", {"status": "completed", "result": long_result}
    )
    await asyncio.sleep(0)

    # Assert
    assert len(trigger_service.calls) == 1
    _agent_id, message, _session_id, _internal = trigger_service.calls[0]
    assert long_result in message


async def test_batch_tracker_surfaces_failure_note() -> None:
    # Arrange
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "session-one", "run-one")

    # Act
    tracker.on_sub_agent_complete(
        parent_key,
        "run-one",
        {"status": "failed", "result": None, "note": "boom"},
    )
    await asyncio.sleep(0)

    # Assert
    assert len(trigger_service.calls) == 1
    _agent_id, message, _session_id, _internal = trigger_service.calls[0]
    assert "### worker (session session-one) — failed" in message
    assert "boom" in message


async def test_batch_tracker_does_not_trigger_when_completed_item_was_fetched() -> None:
    # Arrange
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "session-one", "run-one")
    tracker.mark_fetched(parent_key, "session-one")

    # Act
    tracker.on_sub_agent_complete(parent_key, "run-one", {"result": "Already fetched"})
    await asyncio.sleep(0)

    # Assert
    assert trigger_service.calls == []
    assert tracker.spawn_count(parent_key) == 0


async def test_batch_tracker_logs_trigger_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    log_calls: list[tuple[Any, ...]] = []
    trigger_service = RecordingTriggerService()
    trigger_service.error = RuntimeError("trigger failed")
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "session-one", "run-one")
    monkeypatch.setattr(
        subagent_module._LOGGER, "error", lambda *args, **_kwargs: log_calls.append(args)
    )

    # Act
    tracker.on_sub_agent_complete(parent_key, "run-one", {"result": "done"})
    for _ in range(5):
        await asyncio.sleep(0)

    # Assert
    assert log_calls
    assert "Sub-agent batch completion trigger failed" in log_calls[0][1]
    assert str(log_calls[0][2]) == "trigger failed"


async def test_batch_tracker_prefers_most_recent_run_for_reused_session() -> None:
    # Arrange
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "shared-session", "run-one")
    tracker.register(parent_key, "worker", "shared-session", "run-two")

    # Act
    run_id = tracker.run_id_for_session(parent_key, "shared-session")

    # Assert
    assert run_id == "run-two"


async def test_register_subagent_tools_registers_both_public_tools() -> None:
    # Arrange
    registry = ToolRegistry()
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    coordinator = SubAgentCoordinator(SimpleNamespace(), trigger_service, batch_tracker=tracker)

    # Act
    register_subagent_tools(registry, coordinator)

    # Assert
    assert [tool.name for tool in registry.list_tools()] == [
        SUBAGENT_TOOL_NAME,
        SUBAGENT_RESULT_TOOL_NAME,
    ]


async def test_subagent_tool_enforces_depth_limit(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager, {"max_subagent_depth": 2})
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(nesting_depth=2)

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is False
    assert result["error"]["code"] == "subagent_depth_exceeded"
    assert manager.started == []


async def test_subagent_tool_enforces_per_turn_limit(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager, {"max_subagents_per_turn": 1})
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    tracker.register((context.agent_id, context.session_id, context.run_id), "worker", "s1", "r1")

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is False
    assert result["error"]["code"] == "subagent_limit_exceeded"
    assert manager.started == []


async def test_subagent_tool_validates_target_agent_before_creating_session(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    runtime.agents = FakeAgents({"parent"})
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "missing"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is False
    assert result["error"]["code"] == "agent_not_found"
    assert manager.started == []
    assert list((tmp_path / "agents").glob("missing")) == []


async def test_subagent_tool_creates_new_session_when_no_session_id_provided(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    existing_sessions = runtime.chat_sessions.list(context.agent_id)

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    new_session_id = result["data"]["session_id"]
    assert manager.started[0][1] == new_session_id
    assert len(runtime.chat_sessions.list(context.agent_id)) == len(existing_sessions) + 1


async def test_subagent_tool_marks_created_session_with_parent_metadata(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    metadata = runtime.chat_sessions.get_metadata(
        result["data"]["agent_id"], result["data"]["session_id"]
    )
    assert metadata["is_subagent_session"] is True
    assert metadata["subagent_parent"] == {
        "agent_id": context.agent_id,
        "session_id": context.session_id,
        "run_id": context.run_id,
        "tool_call_id": context.tool_call_id,
        "tool_call_index": context.tool_call_index,
        "project_id": None,
    }


async def test_subagent_tool_emits_session_started_before_blocking_result(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    emitted_events: list[tuple[str, JsonObject]] = []
    context = make_context(
        emit_hook=lambda event_type, payload: emitted_events.append((event_type, payload))
    )

    # Act
    task = asyncio.create_task(
        _handle_subagent(
            context,
            {"content": "spawn", "blocking": True},
            runtime=runtime,
            batch_tracker=tracker,
        )
    )
    await asyncio.sleep(0)

    # Assert
    assert manager.started
    session_id = manager.started[0][1]
    run = manager.started[0][3]
    assert emitted_events[:2] == [
        (
            subagent_module.SUBAGENT_SESSION_STARTED_EVENT,
            {
                "tool_call": {"id": "tool-call-one", "index": 0, "name": "subagent"},
                "data": {
                    "agent_id": "parent",
                    "session_id": session_id,
                    "status": "running",
                },
            },
        ),
        (
            subagent_module.SUBAGENT_SESSION_STARTED_EVENT,
            {
                "tool_call": {"id": "tool-call-one", "index": 0, "name": "subagent"},
                "data": {
                    "agent_id": "parent",
                    "session_id": session_id,
                    "run_id": run.id,
                    "status": "running",
                },
            },
        ),
    ]

    run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="done"))
    result = await task
    assert result["ok"] is True


async def test_subagent_tool_routes_into_existing_session_when_session_id_provided(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    runtime.chat_sessions.create(context.agent_id, session_id="existing-sub-session")
    runtime.chat_sessions.set_metadata(
        context.agent_id,
        "existing-sub-session",
        {"platform": "telegram"},
    )
    existing_session_ids = [session.id for session in runtime.chat_sessions.list(context.agent_id)]

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "session_id": "existing-sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["session_id"] == "existing-sub-session"
    assert manager.started[0][1] == "existing-sub-session"
    assert [
        session.id for session in runtime.chat_sessions.list(context.agent_id)
    ] == existing_session_ids
    metadata = runtime.chat_sessions.get_metadata(context.agent_id, "existing-sub-session")
    assert metadata["platform"] == "telegram"
    assert metadata["is_subagent_session"] is True
    assert metadata["subagent_parent"]["session_id"] == context.session_id


async def test_subagent_tool_rejects_nonexistent_session_id(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "session_id": "missing-sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is False
    assert result["error"]["code"] == "session_not_found"
    assert manager.started == []


async def test_subagent_tool_queues_busy_session_and_returns_running(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    runtime.chat_sessions.create(context.agent_id, session_id="busy-sub-session")
    manager.busy_sessions[(context.agent_id, "busy-sub-session")] = Run(
        run_id="busy-run",
        agent_id=context.agent_id,
        session_id="busy-sub-session",
    )

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "session_id": "busy-sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    assert result["data"]["session_id"] == "busy-sub-session"
    assert result["data"]["run_id"] == manager.enqueued[0]["run"].id
    assert manager.started == []
    assert len(manager.enqueued) == 1
    assert manager.enqueued[0]["display_content"] == "spawn"
    assert manager.enqueued[0]["internal"] is False


async def test_subagent_tool_queues_when_start_races_active_run(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    runtime.chat_sessions.create(context.agent_id, session_id="raced-sub-session")
    manager.start_error = ActiveRunError("session already has an active run")

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "session_id": "raced-sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    assert result["data"]["session_id"] == "raced-sub-session"
    assert result["data"]["run_id"] == manager.enqueued[0]["run"].id
    assert manager.started == []
    assert len(manager.enqueued) == 1


async def test_subagent_tool_returns_queued_without_waiting_for_busy_session_start(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    manager.hold_enqueued_starts = True
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    runtime.chat_sessions.create(context.agent_id, session_id="waiting-sub-session")
    manager.busy_sessions[(context.agent_id, "waiting-sub-session")] = Run(
        run_id="busy-run",
        agent_id=context.agent_id,
        session_id="waiting-sub-session",
    )

    # Act
    task = asyncio.create_task(
        _handle_subagent(
            context,
            {"content": "spawn", "session_id": "waiting-sub-session"},
            runtime=runtime,
            batch_tracker=tracker,
        )
    )
    await asyncio.sleep(0)

    # Assert
    assert len(manager.enqueued) == 1
    assert task.done() is True
    result = await task
    assert result["ok"] is True
    assert result["data"] == {
        "agent_id": "parent",
        "session_id": "waiting-sub-session",
        "queue_item_id": "queued-item-1",
        "status": "queued",
    }
    manager.remove_queued("parent", "waiting-sub-session", "queued-item-1")
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)


async def test_subagent_tool_counts_queued_run_against_per_turn_limit(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    manager.hold_enqueued_starts = True
    runtime = make_runtime(tmp_path, manager, {"max_subagents_per_turn": 1})
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    runtime.chat_sessions.create(context.agent_id, session_id="limited-sub-session")
    manager.busy_sessions[(context.agent_id, "limited-sub-session")] = Run(
        run_id="busy-run",
        agent_id=context.agent_id,
        session_id="limited-sub-session",
    )

    # Act
    first_result = await _handle_subagent(
        context,
        {"content": "spawn", "session_id": "limited-sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    second_result = await _handle_subagent(
        context,
        {"content": "spawn again", "session_id": "limited-sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert first_result["ok"] is True
    assert first_result["data"]["status"] == "queued"
    assert second_result["ok"] is False
    assert second_result["error"]["code"] == "subagent_limit_exceeded"
    assert len(manager.enqueued) == 1
    manager.remove_queued("parent", "limited-sub-session", "queued-item-1")
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)


async def test_parent_cancellation_removes_blocking_queued_subagent(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    manager.hold_enqueued_starts = True
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    parent_key = (context.agent_id, context.session_id, context.run_id)
    runtime.chat_sessions.create(context.agent_id, session_id="cancel-sub-session")
    manager.busy_sessions[(context.agent_id, "cancel-sub-session")] = Run(
        run_id="busy-run",
        agent_id=context.agent_id,
        session_id="cancel-sub-session",
    )

    # Act
    task = asyncio.create_task(
        _handle_subagent(
            context,
            {
                "content": "spawn",
                "session_id": "cancel-sub-session",
                "blocking": True,
            },
            runtime=runtime,
            batch_tracker=tracker,
        )
    )
    await asyncio.sleep(0)
    manager.parent_run.request_cancel()
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)

    # Assert
    assert task.done() is True
    assert manager.enqueued == []
    assert tracker.spawn_count(parent_key) == 0
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_parent_cancellation_does_not_remove_non_blocking_queued_subagent(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    manager.hold_enqueued_starts = True
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    parent_key = (context.agent_id, context.session_id, context.run_id)
    runtime.chat_sessions.create(context.agent_id, session_id="survive-sub-session")
    manager.busy_sessions[(context.agent_id, "survive-sub-session")] = Run(
        run_id="busy-run",
        agent_id=context.agent_id,
        session_id="survive-sub-session",
    )

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "session_id": "survive-sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    manager.parent_run.request_cancel()
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "queued"
    assert len(manager.enqueued) == 1
    assert tracker.spawn_count(parent_key) == 1
    manager.remove_queued("parent", "survive-sub-session", "queued-item-1")
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)


async def test_subagent_result_reports_queued_session(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    manager.hold_enqueued_starts = True
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    result_context = make_context(tool_name=SUBAGENT_RESULT_TOOL_NAME)
    runtime.chat_sessions.create(context.agent_id, session_id="queued-result-sub-session")
    manager.busy_sessions[(context.agent_id, "queued-result-sub-session")] = Run(
        run_id="busy-run",
        agent_id=context.agent_id,
        session_id="queued-result-sub-session",
    )

    spawn_result = await _handle_subagent(
        context,
        {"content": "spawn", "session_id": "queued-result-sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Act
    result = await _handle_subagent_result(
        result_context,
        {"agent_id": "parent", "session_id": "queued-result-sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert spawn_result["ok"] is True
    assert result["ok"] is True
    assert result["data"] == {
        "agent_id": "parent",
        "session_id": "queued-result-sub-session",
        "run_id": None,
        "queue_item_id": "queued-item-1",
        "status": "queued",
        "result": None,
        "usage": None,
    }
    manager.remove_queued("parent", "queued-result-sub-session", "queued-item-1")
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)


async def test_subagent_tool_blocking_waits_for_queued_run_to_complete(
    tmp_path: Path,
) -> None:
    # Arrange
    assistant = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content="queued finished",
        usage={"input_tokens": 2, "output_tokens": 3},
    )
    manager = FakeRunManager()
    manager.next_result = assistant
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    runtime.chat_sessions.create(context.agent_id, session_id="queued-blocking-sub-session")
    manager.busy_sessions[(context.agent_id, "queued-blocking-sub-session")] = Run(
        run_id="busy-run",
        agent_id=context.agent_id,
        session_id="queued-blocking-sub-session",
    )

    # Act
    result = await _handle_subagent(
        context,
        {
            "content": "spawn",
            "session_id": "queued-blocking-sub-session",
            "blocking": True,
        },
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["result"] == "queued finished"
    assert result["data"]["usage"] == {"input_tokens": 2, "output_tokens": 3}
    assert manager.started == []
    assert len(manager.enqueued) == 1


async def test_subagent_tool_rejects_parent_session_reuse(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "session_id": context.session_id},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert manager.started == []


async def test_subagent_tool_self_spawns_non_blocking_and_propagates_depth(
    tmp_path: Path,
) -> None:
    # Arrange
    FakeChatLoop.seen_depths = []
    FakeChatLoop.seen_streaming = []
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(nesting_depth=3)

    # Act
    result = await _handle_subagent(
        context,
        {"content": "do work"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    _agent_id, _session_id, executor, sub_run = manager.started[0]
    await executor(sub_run)
    sub_run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="done"))
    await asyncio.sleep(0)

    # Assert
    assert result["ok"] is True
    assert result["data"]["agent_id"] == "parent"
    assert result["data"]["status"] == "running"
    assert manager.started[0][0] == "parent"
    assert FakeChatLoop.seen_depths == [4]
    assert FakeChatLoop.seen_streaming == [True]


async def test_make_subagent_executor_inherits_live_run_loop_wiring() -> None:
    # Arrange
    resolver = object()
    compaction_service = object()
    parent_loop = chat_api.ChatLoop(
        cast(Any, SimpleNamespace()),
        streaming=True,
        attachment_resolver=cast(Any, resolver),
        compaction_service=cast(Any, compaction_service),
    )
    runtime = SimpleNamespace(streaming_chat_loop=parent_loop)

    # Act
    sub_loop, _executor = subagent_module._make_subagent_executor(
        cast(Any, runtime),
        "do work",
        make_context(nesting_depth=2),
    )

    # Assert
    assert sub_loop._attachment_resolver is resolver
    assert sub_loop._compaction_service is compaction_service
    assert sub_loop._streaming is True
    assert sub_loop._nesting_depth == 3


async def test_subagent_completion_tracker_logs_unexpected_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    log_calls: list[tuple[Any, ...]] = []
    manager = FakeRunManager()
    manager.next_result = ChatMessage.assistant(model="openai/gpt-5.2", content="done")
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    monkeypatch.setattr(
        tracker,
        "on_sub_agent_complete",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        subagent_module._LOGGER, "error", lambda *args, **_kwargs: log_calls.append(args)
    )

    # Act
    result = await _handle_subagent(
        context,
        {"content": "do work"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    for _ in range(5):
        await asyncio.sleep(0)

    # Assert
    assert result["ok"] is True
    assert log_calls
    assert "Sub-agent completion tracker failed" in log_calls[0][1]
    assert str(log_calls[0][2]) == "boom"


async def test_subagent_tool_propagates_parent_cancellation_for_blocking(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()

    # Act
    task = asyncio.create_task(
        _handle_subagent(
            context,
            {"content": "do work", "agent_id": "worker", "blocking": True},
            runtime=runtime,
            batch_tracker=tracker,
        )
    )
    await asyncio.sleep(0)
    sub_run = manager.started[0][3]
    manager.parent_run.request_cancel(reason="user")
    sub_run.mark_cancelled()
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)

    # Assert
    assert sub_run.cancel_requested is True
    assert sub_run.cancel_reason == "user"
    assert task.done() is True
    result = await task
    assert result["ok"] is True
    assert result["data"]["status"] == "cancelled"
    assert result["data"]["cancelled_by_user"] is True


async def test_subagent_tool_does_not_propagate_parent_cancellation_for_non_blocking(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()

    # Act
    result = await _handle_subagent(
        context,
        {"content": "do work", "agent_id": "worker"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    sub_run = manager.started[0][3]
    manager.parent_run.request_cancel()
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)

    # Assert
    assert result["ok"] is True
    assert sub_run.cancel_requested is False
    assert tracker.spawn_count((context.agent_id, context.session_id, context.run_id)) == 1


async def test_subagent_tool_blocking_waits_for_full_result(tmp_path: Path) -> None:
    # Arrange
    assistant = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content="finished",
        usage={"input_tokens": 1, "output_tokens": 2},
    )
    manager = FakeRunManager()
    manager.next_result = assistant
    runtime = make_runtime(tmp_path, manager)
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    context = make_context()

    # Act
    result = await _handle_subagent(
        context,
        {"content": "do work", "blocking": True},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["result"] == "finished"
    assert result["data"]["usage"] == {"input_tokens": 1, "output_tokens": 2}
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)
    assert trigger_service.calls == []


async def test_subagent_tool_blocking_timeout_completes_tracker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager, {"subagent_timeout_minutes": 1})
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()

    async def raise_timeout(_awaitable: Any, *, timeout: float | None = None) -> Any:
        _awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", raise_timeout)

    # Act
    result = await _handle_subagent(
        context,
        {"content": "do work", "blocking": True},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is False
    assert result["error"]["code"] == "subagent_timeout"
    assert manager.started[0][3].cancel_requested is True
    assert tracker.spawn_count((context.agent_id, context.session_id, context.run_id)) == 0
    trigger_service = tracker._trigger_service  # noqa: SLF001 - test observes tracker outcome.
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)
    assert trigger_service.calls == []


async def test_wait_for_subagent_result_converts_normal_failures_to_result_dict() -> None:
    # Arrange
    run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    run.mark_failed(RuntimeError("provider failed"))

    # Act
    result = await _wait_for_subagent_result(run)

    # Assert
    assert result["status"] == "failed"
    assert result["result"] == "provider failed"


async def test_wait_for_subagent_result_does_not_swallow_waiter_cancellation() -> None:
    # Arrange
    run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    waiter = asyncio.create_task(_wait_for_subagent_result(run))
    await asyncio.sleep(0)

    # Act
    waiter.cancel()

    # Assert
    with pytest.raises(asyncio.CancelledError):
        await waiter


async def test_wait_for_subagent_result_marks_user_cancelled_run() -> None:
    """A child run cancelled with reason='user' surfaces 'cancelled by user'."""
    # Arrange
    run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    run.request_cancel(reason="user")
    run.mark_cancelled()

    # Act
    result = await _wait_for_subagent_result(run)

    # Assert
    assert result["status"] == "cancelled"
    assert result["cancelled_by_user"] is True
    assert result["result"] == subagent_module.SUBAGENT_USER_CANCEL_MESSAGE


async def test_wait_for_subagent_result_marks_generic_cancellation_without_user_flag() -> None:
    """A child run cancelled without a reason does not get the user-cancel flag."""
    # Arrange
    run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    run.request_cancel()
    run.mark_cancelled()

    # Act
    result = await _wait_for_subagent_result(run)

    # Assert
    assert result["status"] == "cancelled"
    assert "cancelled_by_user" not in result
    assert result["result"] is None


async def test_subagent_tool_blocking_user_cancelled_result_includes_cancelled_by_user(
    tmp_path: Path,
) -> None:
    """A blocking sub-agent that the user cancels returns 'cancelled by user'."""
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    parent_key = (context.agent_id, context.session_id, context.run_id)

    task = asyncio.create_task(
        _handle_subagent(
            context,
            {"content": "do work", "agent_id": "worker", "blocking": True},
            runtime=runtime,
            batch_tracker=tracker,
        )
    )
    await asyncio.sleep(0)
    sub_run = manager.started[0][3]
    manager.parent_run.request_cancel(reason="user")
    sub_run.mark_cancelled()
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)

    # Act
    result = await task

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "cancelled"
    assert result["data"]["cancelled_by_user"] is True
    assert result["data"]["result"] == "Cancelled by the user"
    assert tracker.spawn_count(parent_key) == 0


async def test_subagent_result_reflects_user_cancelled_child(tmp_path: Path) -> None:
    """subagent_result on a user-cancelled child reports cancelled_by_user."""
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_RESULT_TOOL_NAME)
    parent_key = (context.agent_id, context.session_id, context.run_id)
    sub_run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    sub_run.request_cancel(reason="user")
    sub_run.mark_cancelled()
    manager.runs[sub_run.id] = sub_run
    tracker.register(parent_key, "worker", "sub-session", sub_run.id)

    # Act
    result = await _handle_subagent_result(
        context,
        {"agent_id": "worker", "session_id": "sub-session", "run_id": sub_run.id},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "cancelled"
    assert result["data"]["cancelled_by_user"] is True
    assert result["data"]["result"] == "Cancelled by the user"


async def test_subagent_result_falls_back_to_jsonl_when_run_is_missing(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    session = runtime.chat_sessions.create("worker", session_id="sub-session")
    session.append(ChatMessage.user("question"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="first"))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="final answer",
            usage={"input_tokens": 3, "output_tokens": 5},
        )
    )
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_RESULT_TOOL_NAME)
    tracker.register(
        (context.agent_id, context.session_id, context.run_id), "worker", "sub-session", "r1"
    )

    # Act
    result = await _handle_subagent_result(
        context,
        {"agent_id": "worker", "session_id": "sub-session", "run_id": "missing-run"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"] == {
        "agent_id": "worker",
        "session_id": "sub-session",
        "run_id": "missing-run",
        "status": "completed",
        "result": "final answer",
        "usage": {"input_tokens": 3, "output_tokens": 5},
    }


async def test_subagent_result_marks_live_run_fetched_before_wait_race(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    context = make_context(tool_name=SUBAGENT_RESULT_TOOL_NAME)
    parent_key = (context.agent_id, context.session_id, context.run_id)
    sub_run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    manager.runs[sub_run.id] = sub_run
    tracker.register(parent_key, "worker", "sub-session", sub_run.id)

    async def complete_after_fetch() -> None:
        while True:
            batch = tracker._batches[parent_key]  # noqa: SLF001 - test observes race state.
            if batch.entries[sub_run.id].fetched:
                break
            await asyncio.sleep(0)
        sub_run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="done"))
        tracker.on_sub_agent_complete(parent_key, sub_run.id, {"result": "done"})

    completion = asyncio.create_task(complete_after_fetch())

    # Act
    result = await _handle_subagent_result(
        context,
        {"agent_id": "worker", "session_id": "sub-session", "run_id": sub_run.id},
        runtime=runtime,
        batch_tracker=tracker,
    )
    await completion
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["result"] == "done"
    assert trigger_service.calls == []


async def test_subagent_result_without_run_id_resolves_live_run_from_tracker(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_RESULT_TOOL_NAME)
    parent_key = (context.agent_id, context.session_id, context.run_id)
    sub_run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    manager.runs[sub_run.id] = sub_run
    tracker.register(parent_key, "worker", "sub-session", sub_run.id)

    async def complete_run() -> None:
        await asyncio.sleep(0)
        sub_run.mark_completed(
            ChatMessage.assistant(
                model="openai/gpt-5.2",
                content="live answer",
                usage={"input_tokens": 13, "output_tokens": 17},
            )
        )

    completion = asyncio.create_task(complete_run())

    # Act
    result = await _handle_subagent_result(
        context,
        {"agent_id": "worker", "session_id": "sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    await completion

    # Assert
    assert result["ok"] is True
    assert result["data"] == {
        "agent_id": "worker",
        "session_id": "sub-session",
        "run_id": "sub-run",
        "status": "completed",
        "result": "live answer",
        "usage": {"input_tokens": 13, "output_tokens": 17},
    }


async def test_subagent_result_without_run_id_marks_fetched_before_batch_completion(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    context = make_context(tool_name=SUBAGENT_RESULT_TOOL_NAME)
    parent_key = (context.agent_id, context.session_id, context.run_id)
    sub_run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    manager.runs[sub_run.id] = sub_run
    tracker.register(parent_key, "worker", "sub-session", sub_run.id)

    async def complete_after_fetch() -> None:
        while True:
            batch = tracker._batches[parent_key]  # noqa: SLF001 - test observes race state.
            if batch.entries[sub_run.id].fetched:
                break
            await asyncio.sleep(0)
        sub_run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="done"))
        tracker.on_sub_agent_complete(parent_key, sub_run.id, {"result": "done"})

    completion = asyncio.create_task(complete_after_fetch())

    # Act
    result = await _handle_subagent_result(
        context,
        {"agent_id": "worker", "session_id": "sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    await completion
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["result"] == "done"
    assert trigger_service.calls == []


async def test_subagent_result_fetch_marks_only_requested_run_for_reused_session(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    context = make_context(tool_name=SUBAGENT_RESULT_TOOL_NAME)
    parent_key = (context.agent_id, context.session_id, context.run_id)
    tracker.register(parent_key, "worker", "shared-session", "run-old")
    tracker.register(parent_key, "worker", "shared-session", "run-new")

    old_run = Run(run_id="run-old", agent_id="worker", session_id="shared-session")
    old_run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="old answer"))
    manager.runs[old_run.id] = old_run
    tracker.on_sub_agent_complete(parent_key, "run-old", {"result": "old answer"})

    # Act
    result = await _handle_subagent_result(
        context,
        {"agent_id": "worker", "session_id": "shared-session", "run_id": "run-old"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    batch = tracker._batches[parent_key]  # noqa: SLF001 - test checks fetched disambiguation.
    fetched_after_old_fetch = {run_id: entry.fetched for run_id, entry in batch.entries.items()}
    tracker.on_sub_agent_complete(parent_key, "run-new", {"result": "new answer"})
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)

    # Assert
    assert result["ok"] is True
    assert fetched_after_old_fetch == {"run-old": True, "run-new": False}
    assert len(trigger_service.calls) == 1
    assert "### worker (session shared-session) — completed" in trigger_service.calls[0][1]
    assert "new answer" in trigger_service.calls[0][1]
    assert "old answer" not in trigger_service.calls[0][1]
    assert parent_key not in tracker._batches  # noqa: SLF001 - noted batch is pruned.


async def test_subagent_result_falls_back_to_jsonl_when_live_run_has_no_output(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    session = runtime.chat_sessions.create("worker", session_id="sub-session")
    session.append(ChatMessage.user("question"))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="jsonl answer",
            usage={"input_tokens": 7, "output_tokens": 11},
        )
    )
    sub_run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    sub_run.mark_completed(None)
    manager.runs[sub_run.id] = sub_run
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_RESULT_TOOL_NAME)

    # Act
    result = await _handle_subagent_result(
        context,
        {"agent_id": "worker", "session_id": "sub-session", "run_id": sub_run.id},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"] == {
        "agent_id": "worker",
        "session_id": "sub-session",
        "run_id": "sub-run",
        "status": "completed",
        "result": "jsonl answer",
        "usage": {"input_tokens": 7, "output_tokens": 11},
    }


async def test_subagent_result_failed_live_run_error_falls_back_to_jsonl_output(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    session = runtime.chat_sessions.create("worker", session_id="sub-session")
    session.append(ChatMessage.user("question"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="jsonl answer"))
    sub_run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    sub_run.mark_failed(RuntimeError("provider failed after persistence"))
    manager.runs[sub_run.id] = sub_run
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_RESULT_TOOL_NAME)

    # Act
    result = await _handle_subagent_result(
        context,
        {"agent_id": "worker", "session_id": "sub-session", "run_id": sub_run.id},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["result"] == "jsonl answer"


async def test_subagent_result_polls_jsonl_until_assistant_output_appears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    session = runtime.chat_sessions.create("worker", session_id="sub-session")
    session.append(ChatMessage.user("question"))
    sub_run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    sub_run.mark_failed(RuntimeError("provider failed after persistence"))
    manager.runs[sub_run.id] = sub_run
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_RESULT_TOOL_NAME)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def append_after_first_poll(delay_seconds: float) -> None:
        sleeps.append(delay_seconds)
        session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="late answer"))
        await real_sleep(0)

    monkeypatch.setattr(subagent_module.asyncio, "sleep", append_after_first_poll)

    # Act
    result = await _handle_subagent_result(
        context,
        {"agent_id": "worker", "session_id": "sub-session", "run_id": sub_run.id},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["result"] == "late answer"
    assert sleeps == [subagent_module.SESSION_RESULT_RETRY_DELAY_SECONDS]


async def test_subagent_result_reports_failed_when_jsonl_has_no_output(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    runtime.chat_sessions.create("worker", session_id="sub-session")
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_RESULT_TOOL_NAME)

    # Act
    result = await _handle_subagent_result(
        context,
        {"agent_id": "worker", "session_id": "sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "failed"
    assert result["data"]["result"] is None
    assert result["data"]["note"] == "No assistant output found in sub-agent session."


async def test_subagent_result_reports_failed_after_bounded_jsonl_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    runtime.chat_sessions.create("worker", session_id="sub-session")
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_RESULT_TOOL_NAME)
    sleeps: list[float] = []

    async def record_sleep(delay_seconds: float) -> None:
        sleeps.append(delay_seconds)

    monkeypatch.setattr(subagent_module.asyncio, "sleep", record_sleep)

    # Act
    result = await _handle_subagent_result(
        context,
        {"agent_id": "worker", "session_id": "sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "failed"
    assert result["data"]["result"] is None
    assert sleeps == [
        subagent_module.SESSION_RESULT_RETRY_DELAY_SECONDS,
        subagent_module.SESSION_RESULT_RETRY_DELAY_SECONDS,
    ]
