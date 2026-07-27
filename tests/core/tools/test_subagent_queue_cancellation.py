"""Sub-agent queue admission, cancellation, and queued result tests."""

from __future__ import annotations

from .subagent_test_support import (
    BACKGROUND_TASK_SETTLE_TICKS,
    SUBAGENT_RESULT_TOOL_NAME,
    ActiveRunError,
    ChatMessage,
    FakeRunManager,
    Path,
    RecordingTriggerService,
    Run,
    SubAgentBatchTracker,
    _handle_subagent,
    _handle_subagent_result,
    asyncio,
    make_context,
    make_runtime,
    pytest,
)

pytestmark = pytest.mark.asyncio


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
        {
            "content": "spawn",
            "agent_id": context.agent_id,
            "session_id": "busy-sub-session",
        },
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
        {
            "content": "spawn",
            "agent_id": context.agent_id,
            "session_id": "raced-sub-session",
        },
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
            {
                "content": "spawn",
                "agent_id": context.agent_id,
                "session_id": "waiting-sub-session",
            },
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
    activity_file = result["data"]["activity_file"]
    assert isinstance(activity_file, str)
    assert Path(activity_file).exists()
    assert result["data"] == {
        "agent_id": "parent",
        "session_id": "waiting-sub-session",
        "queue_item_id": "queued-item-1",
        "status": "queued",
        "activity_file": activity_file,
        "activity_note": (
            "Current Sub-Agent activity is available at "
            f"{activity_file}. Read this file if the Sub-Agent's status or progress becomes "
            "relevant."
        ),
    }
    manager.remove_queued("parent", "waiting-sub-session", "queued-item-1", project_id=None)
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
        {
            "content": "spawn",
            "agent_id": context.agent_id,
            "session_id": "limited-sub-session",
        },
        runtime=runtime,
        batch_tracker=tracker,
    )
    second_result = await _handle_subagent(
        context,
        {
            "content": "spawn again",
            "agent_id": context.agent_id,
            "session_id": "limited-sub-session",
        },
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert first_result["ok"] is True
    assert first_result["data"]["status"] == "queued"
    assert second_result["ok"] is False
    assert second_result["error"]["code"] == "subagent_limit_exceeded"
    assert len(manager.enqueued) == 1
    manager.remove_queued("parent", "limited-sub-session", "queued-item-1", project_id=None)
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)


async def test_parent_cancellation_removes_foreground_queued_subagent(tmp_path: Path) -> None:
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
                "agent_id": context.agent_id,
                "session_id": "cancel-sub-session",
                "background": False,
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


async def test_parent_cancellation_does_not_remove_background_queued_subagent(
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
        {
            "content": "spawn",
            "agent_id": context.agent_id,
            "session_id": "survive-sub-session",
        },
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
    manager.remove_queued("parent", "survive-sub-session", "queued-item-1", project_id=None)
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
        {
            "content": "spawn",
            "agent_id": context.agent_id,
            "session_id": "queued-result-sub-session",
        },
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
    activity_file = spawn_result["data"]["activity_file"]
    assert "activity_note" not in result["data"]
    assert result["data"] == {
        "agent_id": "parent",
        "session_id": "queued-result-sub-session",
        "run_id": None,
        "queue_item_id": "queued-item-1",
        "status": "queued",
        "result": None,
        "usage": None,
        "activity_file": activity_file,
    }
    manager.remove_queued("parent", "queued-result-sub-session", "queued-item-1", project_id=None)
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)


async def test_qualified_subagent_queue_and_result_keep_target_project(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    manager.hold_enqueued_starts = True
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(project_id=None)
    result_context = make_context(
        tool_name=SUBAGENT_RESULT_TOOL_NAME,
        project_id=None,
    )
    runtime.chat_sessions.create("worker", session_id="qualified-queued", project_id="vbot")
    manager.busy_sessions[("worker", "qualified-queued")] = Run(
        run_id="busy-project-run",
        agent_id="worker",
        session_id="qualified-queued",
        project_id="vbot",
    )

    spawn_result = await _handle_subagent(
        context,
        {
            "content": "spawn",
            "agent_id": "worker@vbot",
            "session_id": "qualified-queued",
        },
        runtime=runtime,
        batch_tracker=tracker,
    )
    result = await _handle_subagent_result(
        result_context,
        {"agent_id": "worker@vbot", "session_id": "qualified-queued"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert spawn_result["ok"] is True
    assert spawn_result["data"]["project_id"] == "vbot"
    assert manager.enqueued[0]["project_id"] == "vbot"
    assert result["ok"] is True
    activity_file = spawn_result["data"]["activity_file"]
    assert result["data"] == {
        "agent_id": "worker",
        "project_id": "vbot",
        "session_id": "qualified-queued",
        "run_id": None,
        "queue_item_id": "queued-item-1",
        "status": "queued",
        "result": None,
        "usage": None,
        "activity_file": activity_file,
    }
    manager.remove_queued(
        "worker",
        "qualified-queued",
        "queued-item-1",
        project_id="vbot",
    )
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)


async def test_subagent_tool_foreground_waits_for_queued_run_to_complete(
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
    runtime.chat_sessions.create(context.agent_id, session_id="queued-foreground-sub-session")
    manager.busy_sessions[(context.agent_id, "queued-foreground-sub-session")] = Run(
        run_id="busy-run",
        agent_id=context.agent_id,
        session_id="queued-foreground-sub-session",
    )

    # Act
    result = await _handle_subagent(
        context,
        {
            "content": "spawn",
            "agent_id": context.agent_id,
            "session_id": "queued-foreground-sub-session",
            "background": False,
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
