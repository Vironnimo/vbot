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
