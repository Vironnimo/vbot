"""Tests for automation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, call

import pytest

from core.automation import TriggerService
from core.chat import MessageSender, ReplySurface
from core.runs import ActiveRunError, ChatRunManager, Run

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]


def make_run(run_id: str, agent_id: str = "coder", session_id: str = "session-one") -> Run:
    return Run(run_id=run_id, agent_id=agent_id, session_id=session_id)


def make_queued_item(run: Run | None = None) -> SimpleNamespace:
    future: asyncio.Future[Run] = asyncio.get_running_loop().create_future()
    if run is not None:
        future.set_result(run)
    return SimpleNamespace(future=future)


@pytest.mark.parametrize("project_id", [None, "vbot"])
async def test_trigger_run_without_session_starts_in_a_new_session(project_id: str | None) -> None:
    runtime = SimpleNamespace(chat_sessions=SimpleNamespace(create=Mock()))
    chat_loop = SimpleNamespace(
        start_run_in_new_session=AsyncMock(return_value=make_run("run-one", "coder", "new"))
    )
    trigger_service = TriggerService(cast(Any, chat_loop), cast(Any, Mock()), cast(Any, runtime))

    run = await trigger_service.trigger_run("coder", "Start automated work", project_id=project_id)

    # The chat loop creates the Session (under the project anchor when scoped).
    runtime.chat_sessions.create.assert_not_called()
    chat_loop.start_run_in_new_session.assert_awaited_once_with(
        "coder",
        "Start automated work",
        sender=None,
        reply_surface=None,
        project_id=project_id,
    )
    assert run.id == "run-one"


@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_new_session_trigger_releases_its_waiting_work_reservation(
    internal: bool, outcome: str
) -> None:
    manager = ChatRunManager(waiting_work_limit=1)
    run = make_run("run-one", "coder", "new-session")
    admission_error = RuntimeError("Admission failed")
    abort = asyncio.CancelledError()
    starter = AsyncMock(
        return_value=run,
        side_effect=admission_error
        if outcome == "error"
        else abort
        if outcome == "cancel"
        else None,
    )
    service = TriggerService(
        cast(Any, SimpleNamespace(start_run_in_new_session=starter)), manager, cast(Any, Mock())
    )
    reservation = service.reserve_waiting_work(scope="producer", scope_limit=1)
    try:
        if outcome == "success":
            assert (
                await service.trigger_run(
                    "coder", "Work", internal=internal, waiting_work_admission=reservation
                )
                is run
            )
        else:
            with pytest.raises(RuntimeError if outcome == "error" else asyncio.CancelledError):
                await service.trigger_run(
                    "coder", "Work", internal=internal, waiting_work_admission=reservation
                )
        assert manager.waiting_work_count() == 0
        following = service.reserve_waiting_work(scope="producer", scope_limit=1)
        service.release_waiting_work(following)
    finally:
        await manager.aclose()


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
        "coder",
        "Continue",
        session_id="existing",
        sender=None,
        reply_surface=None,
        project_id=None,
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
        reply_surface=None,
        project_id=None,
    )
    chat_loop.start_run.assert_not_awaited()
    assert run.id == "run-streaming"


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
        reply_surface=None,
        project_id=None,
    )
    chat_loop.queue_run.assert_awaited_once_with(
        "coder",
        "Queued message",
        session_id="session-one",
        sender=None,
        reply_surface=None,
        project_id=None,
    )

    queued_item.future.set_result(queued_run)
    run = await queued_task

    # Assert
    assert run is queued_run
    chat_run_manager.active_run.assert_not_called()


def _input_persisted() -> None:
    return None


_SENDER = MessageSender(id="50", display_name="Alice")
_SURFACE = ReplySurface.channel(
    platform="telegram", platform_display_name="Telegram", channel_id="tg-main"
)
_VISIBLE = {"sender": None, "reply_surface": None, "project_id": None}
_INTERNAL = {"internal": True, "reply_surface": None, "project_id": None}


@pytest.mark.parametrize(
    ("options", "forwarded"),
    [
        pytest.param({"sender": _SENDER}, {**_VISIBLE, "sender": _SENDER}, id="sender"),
        pytest.param(
            {"reply_surface": _SURFACE}, {**_VISIBLE, "reply_surface": _SURFACE}, id="reply-surface"
        ),
        pytest.param({"internal": True}, _INTERNAL, id="internal"),
        pytest.param(
            {"internal": True, "input_persisted_hook": _input_persisted},
            {**_INTERNAL, "input_persisted_hook": _input_persisted},
            id="input-persisted-hook",
        ),
        pytest.param(
            {"internal": True, "contributes_to_agent_activity": False},
            {**_INTERNAL, "contributes_to_agent_activity": False},
            id="no-agent-activity",
        ),
        pytest.param(
            {"internal": True, "max_tool_iterations": 60},
            {**_INTERNAL, "max_tool_iterations": 60},
            id="tool-iteration-limit",
        ),
    ],
)
async def test_trigger_run_forwards_options_to_start_and_to_the_queue(
    options: dict[str, Any], forwarded: dict[str, Any]
) -> None:
    queued_run = make_run("queued-run")
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("active run")),
        queue_run=AsyncMock(return_value=make_queued_item(queued_run)),
    )
    chat_run_manager = Mock()
    trigger_service = TriggerService(
        cast(Any, chat_loop), cast(Any, chat_run_manager), cast(Any, Mock())
    )

    run = await trigger_service.trigger_run("coder", "Work", session_id="session-one", **options)

    assert run is queued_run
    chat_loop.start_run.assert_awaited_once_with(
        "coder", "Work", session_id="session-one", **forwarded
    )
    chat_loop.queue_run.assert_awaited_once_with(
        "coder", "Work", session_id="session-one", **forwarded
    )
    chat_run_manager.active_run.assert_not_called()


@pytest.mark.parametrize("active", [True, False])
async def test_has_active_run_reports_the_session_state(active: bool) -> None:
    chat_run_manager = Mock()
    chat_run_manager.active_run = Mock(
        return_value=make_run("active-run", "coder", "session-one") if active else None
    )
    trigger_service = TriggerService(
        cast(Any, SimpleNamespace()), cast(Any, chat_run_manager), cast(Any, Mock())
    )

    assert trigger_service.has_active_run("coder", "session-one") is active
    chat_run_manager.active_run.assert_called_once_with(
        agent_id="coder", session_id="session-one", project_id=None
    )


async def test_compact_session_delegates_to_the_command_chat_loop() -> None:
    chat_loop = SimpleNamespace(compact_session=AsyncMock(return_value="Context compacted."))
    trigger_chat_loop = SimpleNamespace(compact_session=AsyncMock())
    trigger_service = TriggerService(
        cast(Any, chat_loop),
        cast(Any, Mock()),
        cast(Any, Mock()),
        trigger_chat_loop=cast(Any, trigger_chat_loop),
    )

    reply = await trigger_service.compact_session("coder", "session-one")
    # A /compact in a project chat keeps its instruction and project scope.
    await trigger_service.compact_session(
        "coder", "session-one", "keep the API design", project_id="proj"
    )

    assert reply == "Context compacted."
    assert chat_loop.compact_session.await_args_list == [
        call("coder", "session-one", None, project_id=None),
        call("coder", "session-one", "keep the API design", project_id="proj"),
    ]
    trigger_chat_loop.compact_session.assert_not_awaited()


async def test_start_compaction_run_delegates_to_command_chat_loop() -> None:
    run = object()
    chat_loop = SimpleNamespace(start_compaction_run=AsyncMock(return_value=run))
    trigger_service = TriggerService(
        cast(Any, chat_loop),
        cast(Any, Mock()),
        cast(Any, Mock()),
    )

    result = await trigger_service.start_compaction_run(
        "coder",
        "session-one",
        "keep the API design",
        project_id="proj",
    )

    chat_loop.start_compaction_run.assert_awaited_once_with(
        "coder",
        "session-one",
        "keep the API design",
        project_id="proj",
    )
    assert result is run


@pytest.mark.parametrize("cancel_producer", [False, True])
async def test_removed_queue_item_does_not_cancel_its_producer_task(cancel_producer) -> None:
    from core.runs import RunCancelledError

    item = make_queued_item()
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("busy")),
        queue_run=AsyncMock(return_value=item),
    )
    service = TriggerService(cast(Any, chat_loop), Mock(), Mock())
    task = asyncio.create_task(service.trigger_run("coder", "queued", session_id="session-one"))
    await asyncio.sleep(0)
    if cancel_producer:
        task.cancel()
    else:
        item.future.cancel()
    with pytest.raises(asyncio.CancelledError if cancel_producer else RunCancelledError):
        await task
    assert task.cancelled() is cancel_producer
