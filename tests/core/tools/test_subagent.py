"""Tests for sub-agent tools and batch tracking."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import core.chat as chat_api
from core.chat import ChatMessage, ChatSessionManager, Run, RunNotFoundError
from core.tools.subagent import (
    SUBAGENT_RESULT_TOOL_NAME,
    SUBAGENT_TOOL_NAME,
    SubAgentBatchTracker,
    _handle_subagent,
    _handle_subagent_result,
    _wait_for_subagent_result,
    register_subagent_tools,
)
from core.tools.tools import ToolContext, ToolRegistry

pytestmark = pytest.mark.asyncio

JsonObject = dict[str, Any]


def make_context(
    *,
    agent_id: str = "parent",
    session_id: str = "parent-session",
    run_id: str = "parent-run",
    tool_name: str = SUBAGENT_TOOL_NAME,
    nesting_depth: int = 0,
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
        nesting_depth=nesting_depth,
    )


class RecordingTriggerService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def trigger_run(self, agent_id: str, message: str, session_id: str | None = None) -> Run:
        self.calls.append((agent_id, message, session_id))
        return Run(run_id="trigger-run", agent_id=agent_id, session_id=session_id or "new-session")


class FakeStorage:
    def __init__(self, settings: JsonObject | None = None) -> None:
        self.data_dir = Path("data")
        self._settings = settings or {}

    def load_subagent_settings(self) -> JsonObject:
        return dict(self._settings)


class FakeRunManager:
    def __init__(self, parent_run: Run | None = None) -> None:
        self.parent_run = parent_run or Run(
            run_id="parent-run",
            agent_id="parent",
            session_id="parent-session",
        )
        self.started: list[tuple[str, str, Any, Run]] = []
        self.runs: dict[str, Run] = {self.parent_run.id: self.parent_run}
        self.next_result: Any | None = None
        self.next_error: BaseException | None = None

    async def start(self, *, agent_id: str, session_id: str, executor: Any) -> Run:
        run = Run(
            run_id=f"sub-run-{len(self.started) + 1}",
            agent_id=agent_id,
            session_id=session_id,
        )
        self.started.append((agent_id, session_id, executor, run))
        self.runs[run.id] = run
        if self.next_error is not None:
            asyncio.create_task(self._fail_next(run, self.next_error))
        elif self.next_result is not None:
            asyncio.create_task(self._complete_next(run, self.next_result))
        return run

    def get(self, run_id: str) -> Run:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise RunNotFoundError(f"run not found: {run_id}") from exc

    async def _complete_next(self, run: Run, result: Any) -> None:
        await asyncio.sleep(0)
        run.mark_completed(result)

    async def _fail_next(self, run: Run, error: BaseException) -> None:
        await asyncio.sleep(0)
        run.mark_failed(error)


class FakeChatLoop:
    seen_depths: list[int] = []

    def __init__(self, runtime: Any, *, streaming: bool = False) -> None:
        self._runtime = runtime
        self._streaming = streaming
        self._nesting_depth = 0

    async def _execute_run(self, run: Run, content: str) -> ChatMessage:
        self.seen_depths.append(self._nesting_depth)
        return ChatMessage.assistant(model="openai/gpt-5.2", content=f"handled: {content}")


def make_runtime(
    tmp_path: Path, manager: FakeRunManager, settings: JsonObject | None = None
) -> Any:
    return SimpleNamespace(
        chat_sessions=ChatSessionManager(tmp_path),
        chat_run_manager=manager,
        storage=FakeStorage(settings),
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
    agent_id, message, session_id = trigger_service.calls[0]
    assert agent_id == "parent"
    assert session_id == "parent-session"
    assert "Sub-agent batch completed." in message
    assert "- worker/session-one: First result" in message
    assert "- worker/session-two: Second result" in message


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
    assert tracker.spawn_count(parent_key) == 1


async def test_register_subagent_tools_registers_both_public_tools() -> None:
    # Arrange
    registry = ToolRegistry()
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)

    # Act
    register_subagent_tools(registry, SimpleNamespace(), trigger_service, tracker)

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


async def test_subagent_tool_self_spawns_non_blocking_and_propagates_depth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    FakeChatLoop.seen_depths = []
    monkeypatch.setattr(chat_api, "ChatLoop", FakeChatLoop)
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


async def test_subagent_tool_propagates_parent_cancellation(tmp_path: Path) -> None:
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
    await asyncio.sleep(0)

    # Assert
    assert result["ok"] is True
    assert sub_run.cancel_requested is True


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
    tracker = SubAgentBatchTracker(RecordingTriggerService())
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
