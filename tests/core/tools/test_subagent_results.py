"""Sub-agent result retrieval, live-Run races, and Session-history fallback tests."""

from __future__ import annotations

from .subagent_test_support import (
    BACKGROUND_TASK_SETTLE_TICKS,
    SUBAGENT_RESULT_TOOL_NAME,
    ChatMessage,
    FakeRunManager,
    Path,
    RecordingTriggerService,
    Run,
    SubAgentBatchTracker,
    _handle_subagent_result,
    asyncio,
    make_context,
    make_runtime,
    pytest,
    subagent_module,
)

pytestmark = pytest.mark.asyncio


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
