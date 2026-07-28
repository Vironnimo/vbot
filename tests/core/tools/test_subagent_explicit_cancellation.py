"""Parent-Agent cancellation of exact owned Sub-Agent work."""

from __future__ import annotations

from .subagent_test_support import (
    BACKGROUND_TASK_SETTLE_TICKS,
    FakeRunManager,
    JsonObject,
    Path,
    RecordingTriggerService,
    Run,
    SubAgentBatchTracker,
    _handle_subagent,
    asyncio,
    make_context,
    make_runtime,
    pytest,
    subagent_module,
)

pytestmark = pytest.mark.asyncio


async def test_later_parent_run_can_cancel_its_surviving_background_child(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    original_context = make_context()

    spawned = await _handle_subagent(
        original_context,
        {"content": "keep working"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    child_run = manager.started[0][3]

    manager.parent_run.request_cancel()
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)

    assert child_run.status.value == "running"
    assert child_run.cancel_requested is False

    emitted_events: list[tuple[str, JsonObject]] = []
    later_context = make_context(
        run_id="parent-run-two",
        emit_hook=lambda event_type, payload: emitted_events.append((event_type, payload)),
    )
    cancelled = await _handle_subagent(
        later_context,
        {"action": "cancel", "id": spawned["data"]["id"]},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert cancelled == {
        "ok": True,
        "error": None,
        "data": {
            "id": spawned["data"]["id"],
            "agent_id": "parent",
            "session_id": spawned["data"]["session_id"],
            "status": "cancelled",
        },
        "artifacts": [],
    }
    assert child_run.status.value == "cancelled"
    assert child_run.cancel_reason == subagent_module.PARENT_AGENT_CANCEL_REASON
    assert emitted_events == [
        (
            subagent_module.SUBAGENT_STATUS_CHANGED_EVENT,
            {
                "tool_call": {
                    "id": "tool-call-one",
                    "index": 0,
                    "name": "subagent",
                },
                "data": {
                    **cancelled["data"],
                    "run_id": child_run.id,
                },
            },
        )
    ]


async def test_parent_cannot_cancel_another_parent_sessions_child(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    spawned = await _handle_subagent(
        make_context(),
        {"content": "private child"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    child_run = manager.started[0][3]

    result = await _handle_subagent(
        make_context(session_id="different-parent-session", run_id="different-parent-run"),
        {"action": "cancel", "id": spawned["data"]["id"]},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "subagent_not_owned"
    assert child_run.status.value == "running"
    child_run.mark_cancelled()


async def test_parent_can_remove_its_exact_queued_child(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    manager.hold_enqueued_starts = True
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    target_session_id = "queued-child-session"
    runtime.chat_sessions.create("parent", session_id=target_session_id)
    manager.busy_sessions[("parent", target_session_id)] = Run(
        run_id="busy-run",
        agent_id="parent",
        session_id=target_session_id,
    )

    spawned = await _handle_subagent(
        make_context(),
        {
            "content": "wait in queue",
            "agent_id": "parent",
            "session_id": target_session_id,
        },
        runtime=runtime,
        batch_tracker=tracker,
    )
    emitted_events: list[tuple[str, JsonObject]] = []
    result = await _handle_subagent(
        make_context(
            run_id="parent-run-two",
            emit_hook=lambda event_type, payload: emitted_events.append((event_type, payload)),
        ),
        {"action": "cancel", "id": spawned["data"]["id"]},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    assert result["data"] == {
        "id": spawned["data"]["id"],
        "agent_id": "parent",
        "session_id": target_session_id,
        "status": "cancelled",
    }
    assert manager.list_queued("parent", target_session_id, project_id=None) == []
    assert emitted_events[-1][0] == subagent_module.SUBAGENT_STATUS_CHANGED_EVENT
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)


async def test_queue_handle_cancels_the_same_child_after_it_starts(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    manager.hold_enqueued_starts = True
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    target_session_id = "drained-child-session"
    runtime.chat_sessions.create("parent", session_id=target_session_id)
    manager.busy_sessions[("parent", target_session_id)] = Run(
        run_id="busy-run",
        agent_id="parent",
        session_id=target_session_id,
    )
    spawned = await _handle_subagent(
        make_context(),
        {
            "content": "start later",
            "agent_id": "parent",
            "session_id": target_session_id,
        },
        runtime=runtime,
        batch_tracker=tracker,
    )
    started_run = manager.release_next_enqueued_start()
    for _ in range(BACKGROUND_TASK_SETTLE_TICKS):
        await asyncio.sleep(0)

    result = await _handle_subagent(
        make_context(run_id="parent-run-two"),
        {"action": "cancel", "id": spawned["data"]["id"]},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    assert result["data"]["id"] == spawned["data"]["id"]
    assert "queue_item_id" not in result["data"]
    assert "run_id" not in result["data"]
    assert started_run.status.value == "cancelled"


@pytest.mark.parametrize(
    "id_value",
    (
        None,
        "",
    ),
)
async def test_cancel_requires_public_id(
    tmp_path: Path,
    id_value: str | None,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())

    result = await subagent_module._handle_subagent(
        make_context(),
        {"action": "cancel", "id": id_value},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
