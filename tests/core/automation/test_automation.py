"""Tests for automation trigger run coordination."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from core.automation import TriggerService
from core.chat import ActiveRunError, Run

pytestmark = pytest.mark.asyncio


def make_run(run_id: str, agent_id: str = "coder", session_id: str = "session-one") -> Run:
    return Run(run_id=run_id, agent_id=agent_id, session_id=session_id)


async def test_trigger_run_creates_new_session_and_starts_run_immediately() -> None:
    # Arrange
    session = SimpleNamespace(id="new-session")
    runtime = SimpleNamespace(chat_sessions=SimpleNamespace(create=Mock(return_value=session)))
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(return_value=make_run("run-one", "coder", session.id))
    )
    chat_run_manager = Mock()
    trigger_service = TriggerService(
        cast(Any, chat_loop), cast(Any, chat_run_manager), cast(Any, runtime)
    )

    # Act
    run = await trigger_service.trigger_run("coder", "Start automated work")

    # Assert
    runtime.chat_sessions.create.assert_called_once_with("coder")
    chat_loop.start_run.assert_awaited_once_with(
        "coder",
        "Start automated work",
        session_id="new-session",
    )
    assert run.id == "run-one"


async def test_trigger_run_starts_existing_idle_session_immediately() -> None:
    # Arrange
    runtime = Mock()
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(return_value=make_run("run-one", "coder", "existing"))
    )
    chat_run_manager = Mock()
    trigger_service = TriggerService(
        cast(Any, chat_loop), cast(Any, chat_run_manager), cast(Any, runtime)
    )

    # Act
    run = await trigger_service.trigger_run("coder", "Continue", session_id="existing")

    # Assert
    chat_loop.start_run.assert_awaited_once_with("coder", "Continue", session_id="existing")
    chat_run_manager.active_run.assert_not_called()
    assert run.id == "run-one"


async def test_trigger_run_queues_busy_session_until_active_run_terminal_event() -> None:
    # Arrange
    active_run = make_run("active-run")
    queued_run = make_run("queued-run")
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=[ActiveRunError("active run"), queued_run])
    )
    chat_run_manager = SimpleNamespace(active_run=Mock(return_value=active_run))
    runtime = Mock()
    trigger_service = TriggerService(
        cast(Any, chat_loop), cast(Any, chat_run_manager), cast(Any, runtime)
    )

    # Act
    queued_task = asyncio.create_task(
        trigger_service.trigger_run("coder", "Queued message", session_id="session-one")
    )
    await asyncio.sleep(0)
    active_run.mark_completed("done")
    run = await queued_task

    # Assert
    assert run is queued_run
    assert chat_loop.start_run.await_args_list[0].args == ("coder", "Queued message")
    assert chat_loop.start_run.await_args_list[0].kwargs == {"session_id": "session-one"}
    assert chat_loop.start_run.await_args_list[1].args == ("coder", "Queued message")
    assert chat_loop.start_run.await_args_list[1].kwargs == {"session_id": "session-one"}
    chat_run_manager.active_run.assert_called_once_with(
        agent_id="coder",
        session_id="session-one",
    )


async def test_trigger_run_drains_multiple_queued_triggers_fifo() -> None:
    # Arrange
    active_run = make_run("active-run")
    first_queued_run = make_run("queued-run-one")
    second_queued_run = make_run("queued-run-two")
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(
            side_effect=[
                ActiveRunError("active run"),
                ActiveRunError("active run"),
                first_queued_run,
                second_queued_run,
            ]
        )
    )
    chat_run_manager = SimpleNamespace(active_run=Mock(return_value=active_run))
    runtime = Mock()
    trigger_service = TriggerService(
        cast(Any, chat_loop), cast(Any, chat_run_manager), cast(Any, runtime)
    )

    # Act
    first_task = asyncio.create_task(
        trigger_service.trigger_run("coder", "First queued", session_id="session-one")
    )
    await asyncio.sleep(0)
    second_task = asyncio.create_task(
        trigger_service.trigger_run("coder", "Second queued", session_id="session-one")
    )
    await asyncio.sleep(0)
    active_run.mark_completed("done")
    first_result = await first_task
    first_queued_run.mark_completed("done")
    second_result = await second_task

    # Assert
    assert first_result is first_queued_run
    assert second_result is second_queued_run
    drained_messages = [call.args[1] for call in chat_loop.start_run.await_args_list[2:]]
    assert drained_messages == ["First queued", "Second queued"]


async def test_trigger_run_preserves_queue_when_active_run_race_happens_during_drain() -> None:
    # Arrange
    active_run = make_run("active-run")
    competing_run = make_run("competing-run")
    queued_run = make_run("queued-run")
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(
            side_effect=[
                ActiveRunError("active run"),
                ActiveRunError("competing run became active"),
                queued_run,
            ]
        )
    )
    chat_run_manager = SimpleNamespace(active_run=Mock(side_effect=[active_run, competing_run]))
    runtime = Mock()
    trigger_service = TriggerService(
        cast(Any, chat_loop), cast(Any, chat_run_manager), cast(Any, runtime)
    )

    # Act
    queued_task = asyncio.create_task(
        trigger_service.trigger_run("coder", "Queued message", session_id="session-one")
    )
    await asyncio.sleep(0)
    active_run.mark_completed("done")
    await asyncio.sleep(0)
    competing_run.mark_completed("done")
    run = await queued_task

    # Assert
    assert run is queued_run
    drained_messages = [call.args[1] for call in chat_loop.start_run.await_args_list[1:]]
    assert drained_messages == ["Queued message", "Queued message"]
    chat_run_manager.active_run.assert_any_call(agent_id="coder", session_id="session-one")


async def test_subscriber_cancellation_releases_queued_trigger_waiters() -> None:
    # Arrange
    active_run = make_run("active-run")
    chat_loop = SimpleNamespace(start_run=AsyncMock(side_effect=[ActiveRunError("active run")]))
    chat_run_manager = SimpleNamespace(active_run=Mock(return_value=active_run))
    runtime = Mock()
    trigger_service = TriggerService(
        cast(Any, chat_loop), cast(Any, chat_run_manager), cast(Any, runtime)
    )

    # Act
    queued_task = asyncio.create_task(
        trigger_service.trigger_run("coder", "Queued message", session_id="session-one")
    )
    await asyncio.sleep(0)
    subscriber_task = next(iter(trigger_service._subscriber_tasks.values()))
    subscriber_task.cancel()
    await asyncio.sleep(0)

    # Assert
    with pytest.raises(asyncio.CancelledError):
        await queued_task
