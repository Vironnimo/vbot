"""Tests for automation trigger run coordination."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from core.automation import TriggerService
from core.chat import MessageSender
from core.runs import ActiveRunError, Run

pytestmark = pytest.mark.asyncio


def make_run(run_id: str, agent_id: str = "coder", session_id: str = "session-one") -> Run:
    return Run(run_id=run_id, agent_id=agent_id, session_id=session_id)


def make_queued_item(run: Run | None = None) -> SimpleNamespace:
    future: asyncio.Future[Run] = asyncio.get_running_loop().create_future()
    if run is not None:
        future.set_result(run)
    return SimpleNamespace(future=future)


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
        sender=None,
    )
    assert run.id == "run-one"


async def test_trigger_run_starts_existing_idle_session_immediately() -> None:
    # Arrange
    runtime = Mock()
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(return_value=make_run("run-one", "coder", "existing")),
        queue_run=AsyncMock(),
    )
    chat_run_manager = Mock()
    trigger_service = TriggerService(
        cast(Any, chat_loop), cast(Any, chat_run_manager), cast(Any, runtime)
    )

    # Act
    run = await trigger_service.trigger_run("coder", "Continue", session_id="existing")

    # Assert
    chat_loop.start_run.assert_awaited_once_with(
        "coder", "Continue", session_id="existing", sender=None
    )
    chat_loop.queue_run.assert_not_awaited()
    chat_run_manager.active_run.assert_not_called()
    assert run.id == "run-one"


async def test_trigger_run_uses_trigger_chat_loop_when_provided() -> None:
    # Arrange
    runtime = Mock()
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(),
        queue_run=AsyncMock(),
    )
    trigger_chat_loop = SimpleNamespace(
        start_run=AsyncMock(return_value=make_run("run-streaming", "coder", "existing")),
        queue_run=AsyncMock(),
    )
    chat_run_manager = Mock()
    trigger_service = TriggerService(
        cast(Any, chat_loop),
        cast(Any, chat_run_manager),
        cast(Any, runtime),
        trigger_chat_loop=cast(Any, trigger_chat_loop),
    )

    # Act
    run = await trigger_service.trigger_run("coder", "Continue", session_id="existing")

    # Assert
    trigger_chat_loop.start_run.assert_awaited_once_with(
        "coder",
        "Continue",
        session_id="existing",
        sender=None,
    )
    chat_loop.start_run.assert_not_awaited()
    assert run.id == "run-streaming"


async def test_trigger_run_can_start_internal_run_without_visible_user_turn() -> None:
    # Arrange
    runtime = Mock()
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(return_value=make_run("run-one", "coder", "existing")),
        queue_run=AsyncMock(),
    )
    chat_run_manager = Mock()
    trigger_service = TriggerService(
        cast(Any, chat_loop), cast(Any, chat_run_manager), cast(Any, runtime)
    )

    # Act
    run = await trigger_service.trigger_run(
        "coder",
        "Sub-agent batch completed.",
        session_id="existing",
        internal=True,
    )

    # Assert
    chat_loop.start_run.assert_awaited_once_with(
        "coder",
        "Sub-agent batch completed.",
        session_id="existing",
        internal=True,
    )
    chat_loop.queue_run.assert_not_awaited()
    assert run.id == "run-one"


async def test_trigger_run_queues_busy_session_until_active_run_terminal_event() -> None:
    # Arrange
    queued_run = make_run("queued-run")
    queued_item = make_queued_item()
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    chat_run_manager = Mock()
    runtime = Mock()
    trigger_service = TriggerService(
        cast(Any, chat_loop), cast(Any, chat_run_manager), cast(Any, runtime)
    )

    # Act
    queued_task = asyncio.create_task(
        trigger_service.trigger_run("coder", "Queued message", session_id="session-one")
    )
    await asyncio.sleep(0)

    assert queued_task.done() is False
    chat_loop.start_run.assert_awaited_once_with(
        "coder",
        "Queued message",
        session_id="session-one",
        sender=None,
    )
    chat_loop.queue_run.assert_awaited_once_with(
        "coder",
        "Queued message",
        session_id="session-one",
        sender=None,
    )

    queued_item.future.set_result(queued_run)
    run = await queued_task

    # Assert
    assert run is queued_run
    chat_run_manager.active_run.assert_not_called()


async def test_trigger_run_preserves_internal_flag_when_queued() -> None:
    # Arrange
    queued_run = make_run("queued-run")
    queued_item = make_queued_item(queued_run)
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    chat_run_manager = Mock()
    runtime = Mock()
    trigger_service = TriggerService(
        cast(Any, chat_loop), cast(Any, chat_run_manager), cast(Any, runtime)
    )

    # Act
    run = await trigger_service.trigger_run(
        "coder",
        "Sub-agent batch completed.",
        session_id="session-one",
        internal=True,
    )

    # Assert
    assert run is queued_run
    chat_loop.start_run.assert_awaited_once_with(
        "coder",
        "Sub-agent batch completed.",
        session_id="session-one",
        internal=True,
    )
    chat_loop.queue_run.assert_awaited_once_with(
        "coder",
        "Sub-agent batch completed.",
        session_id="session-one",
        internal=True,
    )


async def test_trigger_run_queues_via_chat_run_manager_when_session_is_busy() -> None:
    # Arrange
    queued_run = make_run("queued-run")
    queued_item = make_queued_item(queued_run)
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    chat_run_manager = Mock()
    runtime = Mock()
    trigger_service = TriggerService(
        cast(Any, chat_loop), cast(Any, chat_run_manager), cast(Any, runtime)
    )

    # Act
    run = await trigger_service.trigger_run("coder", "Queued message", session_id="session-one")

    # Assert
    assert run is queued_run
    chat_loop.start_run.assert_awaited_once_with(
        "coder",
        "Queued message",
        session_id="session-one",
        sender=None,
    )
    chat_loop.queue_run.assert_awaited_once_with(
        "coder",
        "Queued message",
        session_id="session-one",
        sender=None,
    )
    chat_run_manager.active_run.assert_not_called()


async def test_trigger_run_forwards_sender_to_start_run() -> None:
    # Arrange
    sender = MessageSender(id="50", display_name="Alice")
    runtime = Mock()
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(return_value=make_run("run-one", "coder", "existing")),
        queue_run=AsyncMock(),
    )
    trigger_service = TriggerService(cast(Any, chat_loop), cast(Any, Mock()), cast(Any, runtime))

    # Act
    run = await trigger_service.trigger_run(
        "coder", "Group message", session_id="existing", sender=sender
    )

    # Assert
    chat_loop.start_run.assert_awaited_once_with(
        "coder",
        "Group message",
        session_id="existing",
        sender=sender,
    )
    chat_loop.queue_run.assert_not_awaited()
    assert run.id == "run-one"


async def test_trigger_run_forwards_sender_when_queued() -> None:
    # Arrange
    sender = MessageSender(id="50", display_name="Alice")
    queued_run = make_run("queued-run")
    queued_item = make_queued_item(queued_run)
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    trigger_service = TriggerService(cast(Any, chat_loop), cast(Any, Mock()), cast(Any, Mock()))

    # Act
    run = await trigger_service.trigger_run(
        "coder", "Group message", session_id="session-one", sender=sender
    )

    # Assert
    assert run is queued_run
    chat_loop.queue_run.assert_awaited_once_with(
        "coder",
        "Group message",
        session_id="session-one",
        sender=sender,
    )


async def test_compact_session_delegates_to_command_chat_loop() -> None:
    # Arrange
    chat_loop = SimpleNamespace(compact_session=AsyncMock(return_value="Context compacted."))
    trigger_chat_loop = SimpleNamespace(compact_session=AsyncMock())
    trigger_service = TriggerService(
        cast(Any, chat_loop),
        cast(Any, Mock()),
        cast(Any, Mock()),
        trigger_chat_loop=cast(Any, trigger_chat_loop),
    )

    # Act
    reply = await trigger_service.compact_session("coder", "session-one")

    # Assert
    chat_loop.compact_session.assert_awaited_once_with("coder", "session-one")
    trigger_chat_loop.compact_session.assert_not_awaited()
    assert reply == "Context compacted."
