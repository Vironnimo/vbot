"""Sub-agent batch tracking and completion notification tests."""

from __future__ import annotations

from .subagent_test_support import (
    Any,
    RecordingTriggerService,
    SubAgentBatchTracker,
    asyncio,
    pytest,
    subagent_module,
)

pytestmark = pytest.mark.asyncio


async def test_batch_tracker_reports_open_identity_parent_and_child_references() -> None:
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    tracker.reserve_slot(("parent", "session", "run"), 2, project_id=None)
    tracker.register(
        ("project-parent", "session", "run"),
        "child",
        "child-session",
        "child-run",
        project_id=None,
    )
    tracker.reserve_slot(
        ("qualified-parent", "session", "run"),
        2,
        project_id="vbot",
    )
    tracker.register_reserved(
        ("qualified-parent", "session", "run"),
        "qualified-child",
        "qualified-session",
        "qualified-run",
        project_id="vbot",
    )

    assert tracker.references_identity_agent("parent") is True
    assert tracker.references_identity_agent("child") is True
    assert tracker.references_identity_agent("qualified-parent") is False
    assert tracker.references_identity_agent("qualified-child") is False
    assert tracker.references_identity_agent("missing") is False


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
    assert "Sub-agent batch complete." in message
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
