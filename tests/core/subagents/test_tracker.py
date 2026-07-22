"""Tests for the in-memory sub-agent batch tracker."""

from __future__ import annotations

import asyncio

import pytest

from core.chat import ChatSessionManager
from core.subagents.subagents import SubAgentBatchTracker
from core.subagents.tracker import _entry_result_text, _entry_status, _SubAgentEntry

pytestmark = pytest.mark.asyncio


class RecordingTriggerService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, bool, str | None]] = []
        self.error: BaseException | None = None
        self.defer_input_persisted = False
        self.input_persisted_hooks: list[object] = []

    async def trigger_run(
        self,
        agent_id: str,
        message: str,
        session_id: str | None = None,
        *,
        internal: bool = False,
        project_id: str | None = None,
        input_persisted_hook: object | None = None,
    ) -> object:
        if self.error is not None:
            raise self.error
        self.calls.append((agent_id, message, session_id, internal, project_id))
        if callable(input_persisted_hook):
            self.input_persisted_hooks.append(input_persisted_hook)
            if not self.defer_input_persisted:
                input_persisted_hook()
        return object()


def _completed_entry(result: dict[str, object]) -> _SubAgentEntry:
    return _SubAgentEntry(
        agent_id="worker",
        session_id="session-one",
        run_id="run-one",
        complete=True,
        result=dict(result),
    )


async def test_entry_status_returns_cancelled_by_user_for_user_cancelled_entry() -> None:
    # Arrange
    entry = _completed_entry(
        {
            "status": "cancelled",
            "result": "Cancelled by the user",
            "cancelled_by_user": True,
        }
    )

    # Act
    status = _entry_status(entry)

    # Assert
    assert status == "cancelled by user"


async def test_entry_status_returns_cancelled_for_generic_cancellation() -> None:
    # Arrange
    entry = _completed_entry({"status": "cancelled", "result": None})

    # Act
    status = _entry_status(entry)

    # Assert
    assert status == "cancelled"


async def test_entry_result_text_uses_user_cancel_message_when_flag_set() -> None:
    # Arrange
    entry = _completed_entry(
        {
            "status": "cancelled",
            "result": "Cancelled by the user",
            "cancelled_by_user": True,
        }
    )

    # Act
    text = _entry_result_text(entry)

    # Assert
    assert text == "Cancelled by the user"


async def test_batch_completion_message_marks_user_cancelled_entry_in_note() -> None:
    # Arrange
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "session-one", "run-one")
    tracker.on_sub_agent_complete(
        parent_key,
        "run-one",
        {
            "status": "cancelled",
            "result": "Cancelled by the user",
            "cancelled_by_user": True,
        },
    )
    await asyncio.sleep(0)

    # Assert
    assert len(trigger_service.calls) == 1
    message = trigger_service.calls[0][1]
    assert "### worker (session session-one) — cancelled by user" in message
    assert "Cancelled by the user" in message


async def test_batch_completion_message_includes_activity_file_when_available() -> None:
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(
        parent_key,
        "worker",
        "session-one",
        "run-one",
        activity_file="C:/data/temp/subagents/run-one.md",
    )

    tracker.on_sub_agent_complete(parent_key, "run-one", {"result": "done"})
    await asyncio.sleep(0)

    assert "Activity file: C:/data/temp/subagents/run-one.md" in trigger_service.calls[0][1]


async def test_batch_completion_message_keeps_generic_cancellation_wording() -> None:
    # Arrange
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "session-one", "run-one")
    tracker.on_sub_agent_complete(
        parent_key,
        "run-one",
        {"status": "cancelled", "result": None},
    )
    await asyncio.sleep(0)

    # Assert
    assert len(trigger_service.calls) == 1
    message = trigger_service.calls[0][1]
    assert "### worker (session session-one) — cancelled" in message
    assert "cancelled by user" not in message


async def test_batch_is_pruned_after_completion_note_for_unfetched_entries() -> None:
    # Arrange: a non-blocking batch whose entries are never fetched via
    # subagent_result (the standard flow — the completion note embeds the
    # results and forbids re-fetching). Regression test for handoff3 B4.
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "session-one", "run-one")
    tracker.register(parent_key, "worker", "session-two", "run-two")

    # Act
    tracker.on_sub_agent_complete(parent_key, "run-one", {"result": "first output"})
    tracker.on_sub_agent_complete(parent_key, "run-two", {"result": "second output"})
    await asyncio.sleep(0)

    # Assert: the note was sent and the batch no longer leaks in memory.
    assert len(trigger_service.calls) == 1
    assert "first output" in trigger_service.calls[0][1]
    assert "second output" in trigger_service.calls[0][1]
    assert parent_key not in tracker._batches  # noqa: SLF001 - leak regression check.


async def test_background_result_is_read_only_after_parent_note_persists(tmp_path) -> None:
    trigger_service = RecordingTriggerService()
    trigger_service.defer_input_persisted = True
    sessions = ChatSessionManager(tmp_path)
    sessions.create("worker", session_id="session-one")
    sessions.record_terminal_run(
        "worker",
        "session-one",
        "run-one",
        "completed",
        "2026-07-22T10:00:00+00:00",
    )
    tracker = SubAgentBatchTracker(trigger_service, sessions=sessions)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "session-one", "run-one")

    tracker.on_sub_agent_complete(parent_key, "run-one", {"result": "done"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert sessions.list_with_metadata("worker")[0]["has_unread_completion"] is True
    assert len(trigger_service.input_persisted_hooks) == 1
    input_persisted_hook = trigger_service.input_persisted_hooks[0]
    assert callable(input_persisted_hook)
    input_persisted_hook()

    assert sessions.list_with_metadata("worker")[0]["has_unread_completion"] is False


async def test_background_delivery_failure_leaves_child_unread(tmp_path) -> None:
    trigger_service = RecordingTriggerService()
    trigger_service.error = RuntimeError("parent unavailable")
    sessions = ChatSessionManager(tmp_path)
    sessions.create("worker", session_id="session-one")
    sessions.record_terminal_run(
        "worker",
        "session-one",
        "run-one",
        "completed",
        "2026-07-22T10:00:00+00:00",
    )
    tracker = SubAgentBatchTracker(trigger_service, sessions=sessions)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "session-one", "run-one")

    tracker.on_sub_agent_complete(parent_key, "run-one", {"result": "done"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert sessions.list_with_metadata("worker")[0]["has_unread_completion"] is True


async def test_completion_trigger_carries_parent_project_id() -> None:
    # Arrange: a project (config) agent reserves its batch under a project, so the
    # batch-completion trigger must continue the parent under that same project
    # rather than falling through to the identity path. Regression for the
    # "Agent not found: orchestrator" failure on a project sub-agent batch.
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("orchestrator", "parent-session", "parent-run")
    assert tracker.reserve_slot(parent_key, max_count=8, project_id="vbot")
    tracker.register_reserved(parent_key, "worker", "session-one", "run-one")

    # Act
    tracker.on_sub_agent_complete(parent_key, "run-one", {"result": "output"})
    await asyncio.sleep(0)

    # Assert: exactly one trigger, carrying the parent run's project_id.
    assert len(trigger_service.calls) == 1
    assert trigger_service.calls[0][0] == "orchestrator"
    assert trigger_service.calls[0][4] == "vbot"


async def test_completion_trigger_keeps_none_project_for_identity_parent() -> None:
    # Arrange: an identity parent (no project) must keep the legacy global layout —
    # the trigger carries project_id None, unchanged from before projects existed.
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    assert tracker.reserve_slot(parent_key, max_count=8)
    tracker.register_reserved(parent_key, "worker", "session-one", "run-one")

    # Act
    tracker.on_sub_agent_complete(parent_key, "run-one", {"result": "output"})
    await asyncio.sleep(0)

    # Assert
    assert len(trigger_service.calls) == 1
    assert trigger_service.calls[0][4] is None


async def test_batch_with_fetched_entries_prunes_without_second_note() -> None:
    # Arrange: one entry already fetched via subagent_result, one not. The note
    # must only embed the unfetched entry, and the batch is dropped afterwards.
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "session-one", "run-one")
    tracker.register(parent_key, "worker", "session-two", "run-two")
    tracker.on_sub_agent_complete(parent_key, "run-one", {"result": "first output"})
    tracker.mark_fetched(parent_key, "session-one", "run-one")

    # Act
    tracker.on_sub_agent_complete(parent_key, "run-two", {"result": "second output"})
    await asyncio.sleep(0)

    # Assert
    assert len(trigger_service.calls) == 1
    assert "first output" not in trigger_service.calls[0][1]
    assert "second output" in trigger_service.calls[0][1]
    assert parent_key not in tracker._batches  # noqa: SLF001 - leak regression check.


async def test_remove_queued_fires_completion_when_siblings_already_finished() -> None:
    # Arrange: sibling B completes first (no note yet — queued A still open), then
    # queued A's item is removed (chat.queue_remove). The removal is the last event
    # that can complete the batch, so it must deliver B's result and drop the batch.
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "session-b", "run-b")
    tracker.register_queued(parent_key, "worker", "session-a", "queue-item-a")
    tracker.on_sub_agent_complete(parent_key, "run-b", {"result": "b output"})
    assert trigger_service.calls == []

    # Act
    tracker.remove_queued(parent_key, "queue-item-a")
    await asyncio.sleep(0)

    # Assert: the promised automatic delivery fired and nothing leaks.
    assert len(trigger_service.calls) == 1
    assert "b output" in trigger_service.calls[0][1]
    assert parent_key not in tracker._batches  # noqa: SLF001 - leak regression check.


async def test_remove_queued_stays_silent_while_siblings_still_run() -> None:
    # Arrange: removing queued A while live sibling B is still running must not
    # notify — B's own completion event fires the note later, exactly once.
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "session-b", "run-b")
    tracker.register_queued(parent_key, "worker", "session-a", "queue-item-a")

    # Act
    tracker.remove_queued(parent_key, "queue-item-a")
    tracker.on_sub_agent_complete(parent_key, "run-b", {"result": "b output"})
    await asyncio.sleep(0)

    # Assert
    assert len(trigger_service.calls) == 1
    assert "b output" in trigger_service.calls[0][1]
    assert parent_key not in tracker._batches  # noqa: SLF001 - leak regression check.


async def test_remove_queued_only_entry_prunes_batch_without_note() -> None:
    # Arrange: the batch's single entry is the queued item being removed — the
    # batch just empties out; an empty batch never notifies.
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register_queued(parent_key, "worker", "session-a", "queue-item-a")

    # Act
    tracker.remove_queued(parent_key, "queue-item-a")
    await asyncio.sleep(0)

    # Assert
    assert trigger_service.calls == []
    assert parent_key not in tracker._batches  # noqa: SLF001 - leak regression check.
