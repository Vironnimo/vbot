"""Sub-agent foreground, background, depth, waiting, and cancellation-result tests."""

from __future__ import annotations

from tests.core.chat.chat_loop_support import build_chat_loop

from .subagent_test_support import (
    BACKGROUND_TASK_SETTLE_TICKS,
    Any,
    ChatMessage,
    FakeChatLoop,
    FakeRunManager,
    Path,
    RecordingTriggerService,
    Run,
    SimpleNamespace,
    SubAgentBatchTracker,
    _handle_subagent,
    _wait_for_subagent_result,
    asyncio,
    cast,
    make_context,
    make_runtime,
    pytest,
    subagent_module,
)

pytestmark = pytest.mark.asyncio


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


async def test_subagent_tool_self_spawns_background_and_propagates_depth(
    tmp_path: Path,
) -> None:
    # Background only happens at the top level (depth 0); a depth >= 1 spawn is
    # forced to the foreground. A top-level self-spawn returns "running" and the
    # child loop carries depth + 1.
    # Arrange
    FakeChatLoop.seen_depths = []
    FakeChatLoop.seen_streaming = []
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(nesting_depth=0)

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
    assert FakeChatLoop.seen_depths == [1]
    assert FakeChatLoop.seen_streaming == [True]


async def test_subagent_tool_forces_foreground_at_depth(tmp_path: Path) -> None:
    # A sub-agent (depth >= 1) cannot park background work, so an omitted background
    # flag is forced to the foreground: the tool returns the finished child result
    # payload (not a "running" descriptor) and tags it with the forced-foreground note.
    assistant = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content="child done",
        usage={"input_tokens": 1, "output_tokens": 2},
    )
    manager = FakeRunManager()
    manager.next_result = assistant
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(nesting_depth=1)

    # Act
    result = await _handle_subagent(
        context,
        {"content": "do work"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["result"] == "child done"
    assert result["data"]["spawn_note"] == subagent_module.FORCED_FOREGROUND_NOTE
    # The forced-foreground child is fully fetched, so the batch drains with no note.
    assert tracker.spawn_count((context.agent_id, context.session_id, context.run_id)) == 0


async def test_subagent_tool_forces_foreground_at_depth_when_background_true(
    tmp_path: Path,
) -> None:
    # An explicit background=true at depth is overridden too, not just an omitted flag.
    assistant = ChatMessage.assistant(model="openai/gpt-5.2", content="child done")
    manager = FakeRunManager()
    manager.next_result = assistant
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(nesting_depth=1)

    # Act
    result = await _handle_subagent(
        context,
        {"content": "do work", "background": True},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["spawn_note"] == subagent_module.FORCED_FOREGROUND_NOTE


async def test_subagent_tool_explicit_foreground_at_depth_has_no_forced_note(
    tmp_path: Path,
) -> None:
    # Explicit foreground at depth is honored as-is, with no forced-foreground note:
    # the caller already asked to wait, nothing was overridden.
    assistant = ChatMessage.assistant(model="openai/gpt-5.2", content="child done")
    manager = FakeRunManager()
    manager.next_result = assistant
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(nesting_depth=1)

    # Act
    result = await _handle_subagent(
        context,
        {"content": "do work", "background": False},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert "spawn_note" not in result["data"]


async def test_subagent_tool_top_level_background_keeps_running_descriptor(
    tmp_path: Path,
) -> None:
    # Regression: at depth 0 an omitted background flag stays in the background and
    # returns the "running" descriptor with no forced-foreground note.
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(nesting_depth=0)

    # Act
    result = await _handle_subagent(
        context,
        {"content": "do work"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    assert "spawn_note" not in result["data"]
    activity_file = result["data"]["activity_file"]
    assert result["data"]["activity_note"] == (
        "Current Sub-Agent activity is available at "
        f"{activity_file}. Read this file if the Sub-Agent's status or progress becomes relevant."
    )
    # Settle the background completion tracker task before the loop closes.
    sub_run = manager.started[0][3]
    sub_run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="done"))
    await asyncio.sleep(0)


async def test_make_subagent_executor_inherits_live_run_loop_wiring() -> None:
    # Arrange
    resolver = object()
    compaction_service = object()
    parent_loop = build_chat_loop(
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


async def test_subagent_tool_propagates_parent_cancellation_for_foreground(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()

    # Act
    task = asyncio.create_task(
        _handle_subagent(
            context,
            {"content": "do work", "agent_id": "worker", "background": False},
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


async def test_subagent_tool_does_not_propagate_parent_cancellation_for_background(
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


async def test_subagent_tool_foreground_waits_for_full_result(tmp_path: Path) -> None:
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
        {"content": "do work", "background": False},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["result"] == "finished"
    assert result["data"]["usage"] == {"input_tokens": 1, "output_tokens": 2}
    activity_file = result["data"]["activity_file"]
    assert result["data"]["activity_note"] == (
        "Current Sub-Agent activity is available at "
        f"{activity_file}. Read this file if the Sub-Agent's status or progress becomes relevant."
    )
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)
    assert trigger_service.calls == []


async def test_subagent_tool_foreground_timeout_completes_tracker(
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
        {"content": "do work", "background": False},
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


async def test_subagent_tool_foreground_user_cancelled_result_includes_cancelled_by_user(
    tmp_path: Path,
) -> None:
    """A foreground sub-agent that the user cancels returns 'cancelled by user'."""
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    parent_key = (context.agent_id, context.session_id, context.run_id)

    task = asyncio.create_task(
        _handle_subagent(
            context,
            {"content": "do work", "agent_id": "worker", "background": False},
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
