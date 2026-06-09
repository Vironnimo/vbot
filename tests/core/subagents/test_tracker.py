"""Tests for the in-memory sub-agent batch tracker."""

from __future__ import annotations

import asyncio

import pytest

from core.subagents.subagents import SubAgentBatchTracker
from core.subagents.tracker import _entry_result_text, _entry_status

pytestmark = pytest.mark.asyncio


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
    ) -> object:
        if self.error is not None:
            raise self.error
        self.calls.append((agent_id, message, session_id, internal))
        return object()


async def test_entry_status_returns_cancelled_by_user_for_user_cancelled_entry() -> None:
    # Arrange
    tracker = SubAgentBatchTracker(RecordingTriggerService())
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
    entry = tracker._batches[parent_key].entries["run-one"]  # noqa: SLF001 - test inspects entry.

    # Act
    status = _entry_status(entry)

    # Assert
    assert status == "cancelled by user"


async def test_entry_status_returns_cancelled_for_generic_cancellation() -> None:
    # Arrange
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "session-one", "run-one")
    tracker.on_sub_agent_complete(
        parent_key,
        "run-one",
        {"status": "cancelled", "result": None},
    )
    await asyncio.sleep(0)
    entry = tracker._batches[parent_key].entries["run-one"]  # noqa: SLF001 - test inspects entry.

    # Act
    status = _entry_status(entry)

    # Assert
    assert status == "cancelled"


async def test_entry_result_text_uses_user_cancel_message_when_flag_set() -> None:
    # Arrange
    tracker = SubAgentBatchTracker(RecordingTriggerService())
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
    entry = tracker._batches[parent_key].entries["run-one"]  # noqa: SLF001 - test inspects entry.

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
