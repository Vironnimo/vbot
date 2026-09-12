"""Process manager: notifications behavior."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from typing import Any

import pytest

from core.tools.process_manager import (
    PROCESS_BUFFER_CAP_BYTES,
    PROCESS_TERMINAL_OUTPUT_CAP_CHARS,
    ProcessManager,
)
from tests.core.tools.process_manager_helpers import (
    AGENT_A,
    SCOPE_A,
)
from tests.core.tools.process_manager_helpers import (
    manager as manager,
)


class TerminalNotificationRecorder:
    """Collects terminal notifications with an awaitable first-receipt event."""

    def __init__(self) -> None:
        self.notifications: list[dict[str, Any]] = []

    def __call__(self, notification: dict[str, Any]) -> None:
        self.notifications.append(notification)

    async def wait_for_notification(self, count: int = 1) -> None:
        for _ in range(200):
            if len(self.notifications) >= count:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(
            f"expected {count} terminal notification(s), got {len(self.notifications)}"
        )


async def _await_terminal(manager: ProcessManager, process_id: str) -> None:
    """Let the wait task settle so the terminal notification has fired."""
    tracked = manager.get_process(process_id, AGENT_A)
    if tracked.wait_task is not None:
        await tracked.wait_task


@pytest.mark.asyncio
async def test_terminal_notification_fires_once_for_backgrounded_exit(
    manager: ProcessManager,
) -> None:
    recorder = TerminalNotificationRecorder()
    manager.add_terminal_callback(recorder)
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "print('background done')"],
        env=None,
        cwd=None,
    )
    manager.mark_backgrounded(process_id, AGENT_A)

    await _await_terminal(manager, process_id)
    await recorder.wait_for_notification()

    assert len(recorder.notifications) == 1
    notification = recorder.notifications[0]
    assert notification["process_id"] == process_id
    assert notification["agent_id"] == AGENT_A
    assert notification["status"] == "completed"
    assert notification["exit_code"] == 0
    assert notification["cancelled_by_user"] is False
    assert "background done" in notification["output"]
    assert notification["started_at"]
    assert notification["finished_at"]
    assert datetime.fromisoformat(notification["started_at"]) <= datetime.fromisoformat(
        notification["finished_at"]
    )


@pytest.mark.asyncio
async def test_terminal_notification_skips_foreground_processes(
    manager: ProcessManager,
) -> None:
    recorder = TerminalNotificationRecorder()
    manager.add_terminal_callback(recorder)
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "print('foreground done')"],
        env=None,
        cwd=None,
    )

    await _await_terminal(manager, process_id)
    await asyncio.sleep(0.05)

    assert recorder.notifications == []


@pytest.mark.asyncio
async def test_terminal_notification_fires_for_killed_process(
    manager: ProcessManager,
) -> None:
    recorder = TerminalNotificationRecorder()
    manager.add_terminal_callback(recorder)
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )
    manager.mark_backgrounded(process_id, AGENT_A)

    await manager.kill(process_id, AGENT_A)
    await _await_terminal(manager, process_id)
    await recorder.wait_for_notification()

    notification = recorder.notifications[0]
    assert notification["status"] == "killed"
    assert notification["finished_at"]


@pytest.mark.asyncio
async def test_terminal_notification_output_tail_is_capped() -> None:
    manager = ProcessManager(buffer_cap_bytes=PROCESS_BUFFER_CAP_BYTES, sweep_interval_seconds=3600)
    try:
        recorder = TerminalNotificationRecorder()
        manager.add_terminal_callback(recorder)
        script = "import sys; sys.stdout.write('x' * 40000); sys.stdout.flush()"
        process_id = await manager.spawn(
            SCOPE_A, AGENT_A, [sys.executable, "-c", script], env=None, cwd=None
        )
        manager.mark_backgrounded(process_id, AGENT_A)

        await _await_terminal(manager, process_id)
        await recorder.wait_for_notification()

        notification = recorder.notifications[0]
        assert len(notification["output"]) == PROCESS_TERMINAL_OUTPUT_CAP_CHARS
        assert notification["output"] == "x" * PROCESS_TERMINAL_OUTPUT_CAP_CHARS
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_terminal_notification_survives_a_failing_callback(
    manager: ProcessManager,
) -> None:
    def broken_callback(notification: dict[str, Any]) -> None:
        raise RuntimeError("bridge exploded")

    recorder = TerminalNotificationRecorder()
    manager.add_terminal_callback(broken_callback)
    manager.add_terminal_callback(recorder)
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "print('notified')"],
        env=None,
        cwd=None,
    )
    manager.mark_backgrounded(process_id, AGENT_A)

    await _await_terminal(manager, process_id)
    await recorder.wait_for_notification()

    assert recorder.notifications[0]["process_id"] == process_id


@pytest.mark.asyncio
async def test_terminal_callback_unsubscribe_stops_delivery(
    manager: ProcessManager,
) -> None:
    recorder = TerminalNotificationRecorder()
    unsubscribe = manager.add_terminal_callback(recorder)
    unsubscribe()
    process_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "print('unsubscribed')"],
        env=None,
        cwd=None,
    )
    manager.mark_backgrounded(process_id, AGENT_A)

    await _await_terminal(manager, process_id)
    await asyncio.sleep(0.05)

    assert recorder.notifications == []
