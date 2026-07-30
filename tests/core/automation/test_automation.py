"""Tests for automation trigger run coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from core.automation import TriggerService
from core.chat import ChatSessionManager, MessageSender, ReplySurface
from core.runs import ActiveRunError, ChatRunManager, Run

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
    runtime = SimpleNamespace(chat_sessions=SimpleNamespace(create=Mock()))
    chat_loop = SimpleNamespace(
        start_run_in_new_session=AsyncMock(return_value=make_run("run-one", "coder", "new-session"))
    )
    chat_run_manager = Mock()
    trigger_service = TriggerService(
        cast(Any, chat_loop), cast(Any, chat_run_manager), cast(Any, runtime)
    )

    # Act
    run = await trigger_service.trigger_run("coder", "Start automated work")

    # Assert
    runtime.chat_sessions.create.assert_not_called()
    chat_loop.start_run_in_new_session.assert_awaited_once_with(
        "coder",
        "Start automated work",
        sender=None,
        reply_surface=None,
        project_id=None,
    )
    assert run.id == "run-one"


async def test_trigger_run_scopes_new_session_and_run_to_project() -> None:
    # Arrange
    runtime = SimpleNamespace(chat_sessions=SimpleNamespace(create=Mock()))
    chat_loop = SimpleNamespace(
        start_run_in_new_session=AsyncMock(
            return_value=make_run("run-one", "builder", "proj-session")
        )
    )
    trigger_service = TriggerService(cast(Any, chat_loop), cast(Any, Mock()), cast(Any, runtime))

    # Act
    run = await trigger_service.trigger_run("builder", "Run project work", project_id="vbot")

    # Assert: the auto-session is created under the project anchor and the run is
    # project-scoped (cwd = repo, project files in the prompt downstream).
    runtime.chat_sessions.create.assert_not_called()
    chat_loop.start_run_in_new_session.assert_awaited_once_with(
        "builder",
        "Run project work",
        sender=None,
        reply_surface=None,
        project_id="vbot",
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
        reply_surface=None,
        project_id=None,
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
        reply_surface=None,
        project_id=None,
    )
    chat_loop.queue_run.assert_awaited_once_with(
        "coder",
        "Sub-agent batch completed.",
        session_id="session-one",
        internal=True,
        reply_surface=None,
        project_id=None,
    )


async def test_trigger_run_forwards_input_persistence_hook_when_queued() -> None:
    queued_run = make_run("queued-run")
    queued_item = make_queued_item(queued_run)
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    trigger_service = TriggerService(cast(Any, chat_loop), cast(Any, Mock()), cast(Any, Mock()))
    input_persisted_hook = Mock()

    run = await trigger_service.trigger_run(
        "coder",
        "Sub-agent batch completed.",
        session_id="session-one",
        internal=True,
        input_persisted_hook=input_persisted_hook,
    )

    assert run is queued_run
    chat_loop.start_run.assert_awaited_once_with(
        "coder",
        "Sub-agent batch completed.",
        session_id="session-one",
        internal=True,
        reply_surface=None,
        project_id=None,
        input_persisted_hook=input_persisted_hook,
    )
    chat_loop.queue_run.assert_awaited_once_with(
        "coder",
        "Sub-agent batch completed.",
        session_id="session-one",
        internal=True,
        reply_surface=None,
        project_id=None,
        input_persisted_hook=input_persisted_hook,
    )


async def test_trigger_run_forwards_agent_activity_policy_when_queued() -> None:
    queued_run = make_run("queued-run")
    queued_item = make_queued_item(queued_run)
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    trigger_service = TriggerService(cast(Any, chat_loop), cast(Any, Mock()), cast(Any, Mock()))

    run = await trigger_service.trigger_run(
        "coder",
        "System work",
        session_id="session-one",
        internal=True,
        contributes_to_agent_activity=False,
    )

    assert run is queued_run
    chat_loop.start_run.assert_awaited_once_with(
        "coder",
        "System work",
        session_id="session-one",
        internal=True,
        reply_surface=None,
        project_id=None,
        contributes_to_agent_activity=False,
    )
    chat_loop.queue_run.assert_awaited_once_with(
        "coder",
        "System work",
        session_id="session-one",
        internal=True,
        reply_surface=None,
        project_id=None,
        contributes_to_agent_activity=False,
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
        reply_surface=None,
        project_id=None,
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
        reply_surface=None,
        project_id=None,
    )


async def test_trigger_run_forwards_reply_surface_to_start_and_queue() -> None:
    surface = ReplySurface.channel(
        platform="telegram",
        platform_display_name="Telegram",
        channel_id="tg-main",
    )
    queued_run = make_run("queued-run")
    queued_item = make_queued_item(queued_run)
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    trigger_service = TriggerService(cast(Any, chat_loop), cast(Any, Mock()), cast(Any, Mock()))

    run = await trigger_service.trigger_run(
        "coder",
        "Channel message",
        session_id="session-one",
        reply_surface=surface,
    )

    assert run is queued_run
    chat_loop.start_run.assert_awaited_once_with(
        "coder",
        "Channel message",
        session_id="session-one",
        sender=None,
        reply_surface=surface,
        project_id=None,
    )
    chat_loop.queue_run.assert_awaited_once_with(
        "coder",
        "Channel message",
        session_id="session-one",
        sender=None,
        reply_surface=surface,
        project_id=None,
    )


async def test_has_active_run_reports_active_run_presence() -> None:
    # Arrange
    active = make_run("active-run", "coder", "session-one")
    chat_run_manager = Mock()
    chat_run_manager.active_run = Mock(return_value=active)
    trigger_service = TriggerService(
        cast(Any, SimpleNamespace()), cast(Any, chat_run_manager), cast(Any, Mock())
    )

    # Act
    result = trigger_service.has_active_run("coder", "session-one")

    # Assert
    assert result is True
    chat_run_manager.active_run.assert_called_once_with(
        agent_id="coder", session_id="session-one", project_id=None
    )


async def test_has_active_run_reports_idle_session() -> None:
    # Arrange
    chat_run_manager = Mock()
    chat_run_manager.active_run = Mock(return_value=None)
    trigger_service = TriggerService(
        cast(Any, SimpleNamespace()), cast(Any, chat_run_manager), cast(Any, Mock())
    )

    # Act / Assert
    assert trigger_service.has_active_run("coder", "session-one") is False


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
    chat_loop.compact_session.assert_awaited_once_with(
        "coder", "session-one", None, project_id=None
    )
    trigger_chat_loop.compact_session.assert_not_awaited()
    assert reply == "Context compacted."


async def test_compact_session_forwards_instruction_to_command_chat_loop() -> None:
    # Arrange
    chat_loop = SimpleNamespace(compact_session=AsyncMock(return_value="Context compacted."))
    trigger_service = TriggerService(
        cast(Any, chat_loop),
        cast(Any, Mock()),
        cast(Any, Mock()),
    )

    # Act
    await trigger_service.compact_session("coder", "session-one", "keep the API design")

    # Assert
    chat_loop.compact_session.assert_awaited_once_with(
        "coder", "session-one", "keep the API design", project_id=None
    )


async def test_compact_session_forwards_project_id_to_command_chat_loop() -> None:
    # A /compact issued in a project chat must carry the project scope down to the
    # chat loop, not collapse to the identity session.
    chat_loop = SimpleNamespace(compact_session=AsyncMock(return_value="Context compacted."))
    trigger_service = TriggerService(
        cast(Any, chat_loop),
        cast(Any, Mock()),
        cast(Any, Mock()),
    )

    await trigger_service.compact_session("coder", "session-one", project_id="proj")

    chat_loop.compact_session.assert_awaited_once_with(
        "coder", "session-one", None, project_id="proj"
    )


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


class _CompletionChatLoop:
    def __init__(self, run_manager: ChatRunManager) -> None:
        self._run_manager = run_manager
        self.messages: list[str] = []

    async def start_run(
        self,
        agent_id: str,
        content: str,
        *,
        session_id: str,
        internal: bool,
        project_id: str | None,
        input_persisted_hook: Callable[[], None],
    ) -> Run:
        assert internal is True

        async def executor(_run: Run) -> str:
            self.messages.append(content)
            input_persisted_hook()
            return content

        return await self._run_manager.start(
            agent_id=agent_id,
            session_id=session_id,
            executor=executor,
            project_id=project_id,
        )


async def test_completion_delivery_coalesces_every_result_ready_before_run_end(
    tmp_path: Path,
) -> None:
    run_manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_executor(_run: Run) -> str:
        await active_release.wait()
        return "parent complete"

    parent_run = await run_manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=active_executor,
        project_id=None,
    )
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="session-one")
    completion_loop = _CompletionChatLoop(run_manager)
    trigger_service = TriggerService(
        cast(Any, completion_loop),
        run_manager,
        cast(Any, Mock()),
        trigger_chat_loop=cast(Any, completion_loop),
        sessions=sessions,
    )

    first = trigger_service.submit_completion(
        "coder",
        "session-one",
        notice_id="bash:one",
        origin_run_id=parent_run.id,
        body="### Bash process — completed\nfirst",
    )
    second = trigger_service.submit_completion(
        "coder",
        "session-one",
        notice_id="subagent:one",
        origin_run_id=parent_run.id,
        body="### Sub-Agent worker — completed\nsecond",
    )
    await asyncio.sleep(0)
    assert completion_loop.messages == []

    active_release.set()
    await parent_run.wait()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

    assert len(completion_loop.messages) == 1
    message = completion_loop.messages[0]
    assert message.startswith(
        "Automatic completion delivery — this is not a new user request. Do not restart or "
        "repeat work merely because this report arrived. Re-evaluate the original user goal "
        "and current system state before taking further action.\n\nResults:"
    )
    assert "first" in message
    assert "second" in message


async def test_completion_finishing_after_boundary_uses_later_delivery(tmp_path: Path) -> None:
    run_manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_executor(_run: Run) -> str:
        await active_release.wait()
        return "parent complete"

    parent_run = await run_manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=active_executor,
        project_id=None,
    )
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="session-one")
    completion_loop = _CompletionChatLoop(run_manager)
    trigger_service = TriggerService(
        cast(Any, completion_loop),
        run_manager,
        cast(Any, Mock()),
        trigger_chat_loop=cast(Any, completion_loop),
        sessions=sessions,
    )

    ready = trigger_service.submit_completion(
        "coder",
        "session-one",
        notice_id="bash:ready",
        origin_run_id=parent_run.id,
        body="ready at boundary",
    )
    active_release.set()
    await parent_run.wait()

    later = trigger_service.submit_completion(
        "coder",
        "session-one",
        notice_id="bash:later",
        origin_run_id=parent_run.id,
        body="finished later",
    )
    await asyncio.wait_for(asyncio.gather(ready, later), timeout=1)

    assert len(completion_loop.messages) == 2
    assert "ready at boundary" in completion_loop.messages[0]
    assert "finished later" not in completion_loop.messages[0]
    assert "finished later" in completion_loop.messages[1]


async def test_cancelled_pending_notice_does_not_start_empty_follow_up(
    tmp_path: Path,
) -> None:
    run_manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_executor(_run: Run) -> str:
        await active_release.wait()
        return "parent complete"

    parent_run = await run_manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=active_executor,
        project_id=None,
    )
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="session-one")
    completion_loop = _CompletionChatLoop(run_manager)
    trigger_service = TriggerService(
        cast(Any, completion_loop),
        run_manager,
        cast(Any, Mock()),
        trigger_chat_loop=cast(Any, completion_loop),
        sessions=sessions,
    )

    delivery = trigger_service.submit_completion(
        "coder",
        "session-one",
        notice_id="bash:manually-fetched",
        origin_run_id=parent_run.id,
        body="already delivered manually",
    )
    assert trigger_service.cancel_completion(
        "coder",
        "session-one",
        notice_id="bash:manually-fetched",
    )
    assert delivery.cancelled()

    active_release.set()
    await parent_run.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert completion_loop.messages == []


async def test_user_cancel_persists_pending_completion_without_new_run(tmp_path: Path) -> None:
    run_manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_executor(_run: Run) -> str:
        await active_release.wait()
        return "unused"

    parent_run = await run_manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=active_executor,
        project_id=None,
    )
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="session-one")
    completion_loop = _CompletionChatLoop(run_manager)
    trigger_service = TriggerService(
        cast(Any, completion_loop),
        run_manager,
        cast(Any, Mock()),
        trigger_chat_loop=cast(Any, completion_loop),
        sessions=sessions,
    )
    pending = trigger_service.submit_completion(
        "coder",
        "session-one",
        notice_id="bash:cancelled-parent",
        origin_run_id=parent_run.id,
        body="completed before cancellation",
    )

    await run_manager.cancel(parent_run.id, reason="user")
    await asyncio.wait_for(pending, timeout=1)

    assert completion_loop.messages == []
    notes = [
        message.content
        for message in sessions.get("coder", "session-one").load()
        if message.role == "note" and isinstance(message.content, str)
    ]
    assert any("completed before cancellation" in note for note in notes)

    late = trigger_service.submit_completion(
        "coder",
        "session-one",
        notice_id="bash:late-cancelled-parent",
        origin_run_id=parent_run.id,
        body="completed after cancellation",
    )
    await asyncio.wait_for(late, timeout=1)
    assert completion_loop.messages == []


async def test_completion_from_already_cancelled_origin_does_not_start_run(
    tmp_path: Path,
) -> None:
    run_manager = ChatRunManager()

    async def active_executor(_run: Run) -> str:
        await asyncio.Event().wait()
        return "unused"

    parent_run = await run_manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=active_executor,
        project_id=None,
    )
    await run_manager.cancel(parent_run.id, reason="user")

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="session-one")
    completion_loop = _CompletionChatLoop(run_manager)
    trigger_service = TriggerService(
        cast(Any, completion_loop),
        run_manager,
        cast(Any, Mock()),
        trigger_chat_loop=cast(Any, completion_loop),
        sessions=sessions,
    )

    late = trigger_service.submit_completion(
        "coder",
        "session-one",
        notice_id="bash:late-only",
        origin_run_id=parent_run.id,
        body="finished after cancellation",
    )
    await asyncio.wait_for(late, timeout=1)

    assert completion_loop.messages == []
    notes = [
        message.content
        for message in sessions.get("coder", "session-one").load()
        if message.role == "note" and isinstance(message.content, str)
    ]
    assert any("finished after cancellation" in note for note in notes)
