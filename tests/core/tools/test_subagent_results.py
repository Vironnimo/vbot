"""Sub-agent result retrieval, live-Run races, and Session-history fallback tests."""

from __future__ import annotations

from .subagent_test_support import (
    BACKGROUND_TASK_SETTLE_TICKS,
    SUBAGENT_TOOL_NAME,
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

TERMINAL_TIMING = {
    "started_at": "2026-07-24T10:00:00+00:00",
    "completed_at": "2026-07-24T10:00:01+00:00",
    "duration_ms": 1000,
}
WORK_ID = "sub_work"


async def test_subagent_result_reflects_user_cancelled_child(tmp_path: Path) -> None:
    """A status snapshot reports user cancellation."""
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_TOOL_NAME)
    parent_key = (context.agent_id, context.session_id, context.run_id)
    sub_run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    sub_run.request_cancel(reason="user")
    sub_run.mark_cancelled()
    manager.runs[sub_run.id] = sub_run
    tracker.register(parent_key, "worker", "sub-session", sub_run.id, work_id=WORK_ID)

    # Act
    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "cancelled"
    assert result["data"]["cancelled_by_user"] is True
    assert result["data"]["result"] == "Cancelled by the user"


async def test_subagent_result_marks_preserved_partial_as_interrupted(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_TOOL_NAME)
    parent_key = (context.agent_id, context.session_id, context.run_id)
    sub_run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    sub_run.mark_completed(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="I am about to write the plan.",
            interrupted=True,
            interruption_cause="timeout",
        )
    )
    manager.runs[sub_run.id] = sub_run
    tracker.register(parent_key, "worker", "sub-session", sub_run.id, work_id=WORK_ID)

    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["interrupted"] is True
    assert result["data"]["interruption_cause"] == "timeout"
    assert result["data"]["note"] == (
        "Result is partial: the Sub-Agent Run was interrupted by timeout. Continue the "
        "same Session by passing both agent_id and session_id from this result to subagent."
    )


async def test_subagent_result_preserves_interruption_details_from_jsonl(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    session = runtime.chat_sessions.create("worker", session_id="sub-session")
    session.append(ChatMessage.user("write the plan"))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="I am about to write the plan.",
            interrupted=True,
            interruption_cause="timeout",
        )
    )
    session.append(
        ChatMessage.run_summary(
            run_id="missing-run",
            status="completed",
            timing=TERMINAL_TIMING,
        )
    )
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_TOOL_NAME)
    tracker.register(
        (context.agent_id, context.session_id, context.run_id),
        "worker",
        "sub-session",
        "missing-run",
        work_id=WORK_ID,
    )

    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    assert result["data"]["interrupted"] is True
    assert result["data"]["interruption_cause"] == "timeout"


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
    session.append(
        ChatMessage.run_summary(
            run_id="missing-run",
            status="completed",
            timing=TERMINAL_TIMING,
        )
    )
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_TOOL_NAME)
    tracker.register(
        (context.agent_id, context.session_id, context.run_id),
        "worker",
        "sub-session",
        "missing-run",
        work_id=WORK_ID,
    )

    # Act
    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"] == {
        "id": WORK_ID,
        "agent_id": "worker",
        "session_id": "sub-session",
        "status": "completed",
        "result": "final answer",
        "usage": {"input_tokens": 3, "output_tokens": 5},
        "activity_file": None,
    }


async def test_subagent_result_returns_running_without_fetching_or_waiting(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    context = make_context(tool_name=SUBAGENT_TOOL_NAME)
    parent_key = (context.agent_id, context.session_id, context.run_id)
    sub_run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    manager.runs[sub_run.id] = sub_run
    tracker.register(parent_key, "worker", "sub-session", sub_run.id, work_id=WORK_ID)

    # Act
    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
        runtime=runtime,
        batch_tracker=tracker,
    )
    fetched = tracker._batches[parent_key].entries[WORK_ID].fetched  # noqa: SLF001
    sub_run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="done"))
    tracker.on_sub_agent_complete(parent_key, sub_run.id, {"result": "done"})
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    assert result["data"]["id"] == WORK_ID
    assert result["data"]["result"] is None
    assert fetched is False
    assert len(trigger_service.calls) == 1
    assert "done" in trigger_service.calls[0][1]


async def test_subagent_status_resolves_live_run_from_stable_id(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_TOOL_NAME)
    parent_key = (context.agent_id, context.session_id, context.run_id)
    sub_run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    manager.runs[sub_run.id] = sub_run
    tracker.register(parent_key, "worker", "sub-session", sub_run.id, work_id=WORK_ID)

    # Act
    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"] == {
        "id": WORK_ID,
        "agent_id": "worker",
        "session_id": "sub-session",
        "status": "running",
        "result": None,
        "usage": None,
        "activity_file": None,
    }


async def test_subagent_result_from_later_parent_run_resolves_active_session_run(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(
        tool_name=SUBAGENT_TOOL_NAME,
        run_id="later-parent-run",
    )
    sub_run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    manager.runs[sub_run.id] = sub_run
    manager.busy_sessions[("worker", "sub-session")] = sub_run
    session = runtime.chat_sessions.create("worker", session_id="sub-session")
    session.append(ChatMessage.user("do the work"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Still working."))
    tracker.register(
        (context.agent_id, context.session_id, "parent-run"),
        "worker",
        "sub-session",
        sub_run.id,
        work_id=WORK_ID,
    )

    # Act
    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["id"] == WORK_ID
    assert result["data"]["status"] == "running"
    assert result["data"]["result"] is None


async def test_subagent_result_from_later_parent_run_resolves_queued_session_run(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    manager.hold_enqueued_starts = True
    queued_item = await manager.enqueue(
        agent_id="worker",
        session_id="sub-session",
        executor=lambda run: run,
    )
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(
        tool_name=SUBAGENT_TOOL_NAME,
        run_id="later-parent-run",
    )
    tracker.register_queued(
        (context.agent_id, context.session_id, "parent-run"),
        "worker",
        "sub-session",
        queued_item.item_id,
        work_id=WORK_ID,
    )

    # Act
    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["id"] == WORK_ID
    assert result["data"]["status"] == "queued"
    assert "queue_item_id" not in result["data"]
    assert "run_id" not in result["data"]


async def test_subagent_result_fetch_marks_only_requested_run_for_reused_session(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    trigger_service = RecordingTriggerService()
    trigger_service.defer_input_persisted = True
    tracker = SubAgentBatchTracker(trigger_service)
    context = make_context(tool_name=SUBAGENT_TOOL_NAME)
    parent_key = (context.agent_id, context.session_id, context.run_id)
    tracker.register(
        parent_key,
        "worker",
        "shared-session",
        "run-old",
        work_id="sub_old",
    )
    tracker.register(
        parent_key,
        "worker",
        "shared-session",
        "run-new",
        work_id="sub_new",
    )

    old_run = Run(run_id="run-old", agent_id="worker", session_id="shared-session")
    old_run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="old answer"))
    manager.runs[old_run.id] = old_run
    tracker.on_sub_agent_complete(parent_key, "run-old", {"result": "old answer"})

    # Act
    result = await _handle_subagent_result(
        context,
        {"id": "sub_old"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    batch = tracker._batches[parent_key]  # noqa: SLF001 - test checks fetched disambiguation.
    fetched_after_old_fetch = {work_id: entry.fetched for work_id, entry in batch.entries.items()}
    trigger_service.defer_input_persisted = False
    tracker.on_sub_agent_complete(parent_key, "run-new", {"result": "new answer"})
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)

    # Assert
    assert result["ok"] is True
    assert fetched_after_old_fetch == {"sub_old": True, "sub_new": False}
    assert len(trigger_service.calls) == 2
    assert trigger_service.cancelled_notice_ids == ["subagent:parent-run:sub_old"]
    assert (
        "### Sub-Agent worker (id sub_new, session shared-session) — completed"
        in trigger_service.calls[1][1]
    )
    assert "new answer" in trigger_service.calls[1][1]
    assert "old answer" not in trigger_service.calls[1][1]
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
    session.append(
        ChatMessage.run_summary(
            run_id="sub-run",
            status="completed",
            timing=TERMINAL_TIMING,
        )
    )
    sub_run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    sub_run.mark_completed(None)
    manager.runs[sub_run.id] = sub_run
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_TOOL_NAME)
    tracker.register(
        (context.agent_id, context.session_id, context.run_id),
        "worker",
        "sub-session",
        sub_run.id,
        work_id=WORK_ID,
    )

    # Act
    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"] == {
        "id": WORK_ID,
        "agent_id": "worker",
        "session_id": "sub-session",
        "status": "completed",
        "result": "jsonl answer",
        "usage": {"input_tokens": 7, "output_tokens": 11},
        "activity_file": None,
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
    session.append(
        ChatMessage.run_summary(
            run_id="sub-run",
            status="failed",
            timing=TERMINAL_TIMING,
        )
    )
    sub_run = Run(run_id="sub-run", agent_id="worker", session_id="sub-session")
    sub_run.mark_failed(RuntimeError("provider failed after persistence"))
    manager.runs[sub_run.id] = sub_run
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_TOOL_NAME)
    tracker.register(
        (context.agent_id, context.session_id, context.run_id),
        "worker",
        "sub-session",
        sub_run.id,
        work_id=WORK_ID,
    )

    # Act
    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "failed"
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
    context = make_context(tool_name=SUBAGENT_TOOL_NAME)
    tracker.register(
        (context.agent_id, context.session_id, context.run_id),
        "worker",
        "sub-session",
        sub_run.id,
        work_id=WORK_ID,
    )
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def append_after_first_poll(delay_seconds: float) -> None:
        sleeps.append(delay_seconds)
        session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="late answer"))
        session.append(
            ChatMessage.run_summary(
                run_id="sub-run",
                status="failed",
                timing=TERMINAL_TIMING,
            )
        )
        await real_sleep(0)

    monkeypatch.setattr(subagent_module.asyncio, "sleep", append_after_first_poll)

    # Act
    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "failed"
    assert result["data"]["result"] == "late answer"
    assert sleeps == [subagent_module.SESSION_RESULT_RETRY_DELAY_SECONDS]


async def test_subagent_result_does_not_complete_from_intermediate_assistant_output(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    session = runtime.chat_sessions.create("worker", session_id="sub-session")
    session.append(ChatMessage.user("question"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Still reading."))
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_TOOL_NAME)
    tracker.register(
        (context.agent_id, context.session_id, context.run_id),
        "worker",
        "sub-session",
        "sub-run",
        work_id=WORK_ID,
    )

    # Act
    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "failed"
    assert result["data"]["result"] is None
    assert result["data"]["note"] == "No terminal Run summary found in sub-agent session."


async def test_subagent_result_ignores_prior_terminal_run_when_new_output_is_unfinished(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    session = runtime.chat_sessions.create("worker", session_id="sub-session")
    session.append(ChatMessage.user("first question"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="First answer."))
    session.append(
        ChatMessage.run_summary(
            run_id="first-run",
            status="completed",
            timing=TERMINAL_TIMING,
        )
    )
    session.append(ChatMessage.user("continue"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Still working."))
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_TOOL_NAME)
    tracker.register(
        (context.agent_id, context.session_id, context.run_id),
        "worker",
        "sub-session",
        "second-run",
        work_id=WORK_ID,
    )

    # Act
    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "failed"
    assert result["data"]["id"] == WORK_ID
    assert "run_id" not in result["data"]
    assert result["data"]["result"] is None


async def test_subagent_result_reports_failed_when_jsonl_has_no_output(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    runtime.chat_sessions.create("worker", session_id="sub-session")
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_TOOL_NAME)
    tracker.register(
        (context.agent_id, context.session_id, context.run_id),
        "worker",
        "sub-session",
        "sub-run",
        work_id=WORK_ID,
    )

    # Act
    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["status"] == "failed"
    assert result["data"]["result"] is None
    assert result["data"]["note"] == "No terminal Run summary found in sub-agent session."


async def test_subagent_result_reports_failed_after_bounded_jsonl_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    runtime.chat_sessions.create("worker", session_id="sub-session")
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(tool_name=SUBAGENT_TOOL_NAME)
    tracker.register(
        (context.agent_id, context.session_id, context.run_id),
        "worker",
        "sub-session",
        "sub-run",
        work_id=WORK_ID,
    )
    sleeps: list[float] = []

    async def record_sleep(delay_seconds: float) -> None:
        sleeps.append(delay_seconds)

    monkeypatch.setattr(subagent_module.asyncio, "sleep", record_sleep)

    # Act
    result = await _handle_subagent_result(
        context,
        {"id": WORK_ID},
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
