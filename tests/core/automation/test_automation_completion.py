"""Tests for automation completion."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import core.automation.automation as automation_module
from core.automation import TriggerService
from core.chat import ChatSession, ChatSessionError, ChatSessionManager, ReplySurface
from core.runs import ChatRunManager, Run, RunAdmission, RunExecutionOwner, RunKind
from core.sessions import SessionAddress
from core.subagents import SubAgentBatchTracker

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]


class _CompletionChatLoop:
    def __init__(self, run_manager: ChatRunManager) -> None:
        self._run_manager = run_manager
        self.messages: list[str] = []
        self.reply_surfaces: list[ReplySurface | None] = []

    async def start_run(
        self,
        agent_id: str,
        content: str,
        *,
        session_id: str,
        internal: bool,
        reply_surface: ReplySurface | None,
        project_id: str | None,
        input_persisted_hook: Callable[[], None],
        run_kind: RunKind,
    ) -> Run:
        assert internal is True
        assert run_kind is RunKind.SYSTEM

        async def executor(_run: Run) -> str:
            self.messages.append(content)
            self.reply_surfaces.append(reply_surface)
            input_persisted_hook()
            return content

        return await self._run_manager.start(
            SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id),
            executor,
            admission=RunAdmission(run_kind=run_kind),
        )


async def test_owned_completion_close_discards_only_matching_notice(tmp_path):
    manager = ChatRunManager()
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="session-one")
    loop = _CompletionChatLoop(manager)
    service = TriggerService(loop, manager, Mock(), sessions=sessions)
    owner = RunExecutionOwner("swarm", "group", "peer", "generation", "epoch")
    owned = service.submit_completion(
        "coder",
        "session-one",
        notice_id="owned",
        origin_run_id="origin",
        body="owned result",
        execution_owner=owner,
    )
    ordinary = service.submit_completion(
        "coder",
        "session-one",
        notice_id="ordinary",
        origin_run_id="other",
        body="ordinary result",
    )
    await service.close_execution_group("swarm", "group", "epoch")
    assert owned.cancelled()
    await asyncio.wait_for(ordinary, 2)
    assert len(loop.messages) == 1
    assert "ordinary result" in loop.messages[0]
    assert "owned result" not in loop.messages[0]
    late = service.submit_completion(
        "coder",
        "session-one",
        notice_id="late",
        origin_run_id="origin",
        body="late owned result",
        execution_owner=owner,
    )
    assert late.cancelled()
    await service.aclose()
    await manager.aclose()
    sessions.close()


async def test_owned_completion_admission_failure_cannot_fallback_to_session_write(tmp_path):
    manager = ChatRunManager()
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="session-one")
    loop = _CompletionChatLoop(manager)
    service = TriggerService(loop, manager, Mock(), sessions=sessions)
    owner = RunExecutionOwner("swarm", "group", "peer", "generation", "epoch")
    service.set_owned_completion_starter(AsyncMock(side_effect=ValueError("fixture rejection")))
    delivery = service.submit_completion(
        "coder",
        "session-one",
        notice_id="owned",
        origin_run_id="origin",
        body="owned result",
        execution_owner=owner,
    )
    with pytest.raises(ValueError):
        await asyncio.wait_for(delivery, 2)
    assert session.load() == []
    assert loop.messages == []
    await service.aclose()
    await manager.aclose()
    sessions.close()


async def test_idle_completion_relays_through_latest_channel_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_manager = ChatRunManager()
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="session-one")
    surface = ReplySurface.channel(
        platform="telegram",
        platform_display_name="Telegram",
        channel_id="tg-main",
    )
    session.add_note(surface.to_note_content())
    monkeypatch.setattr(
        ChatSession,
        "load_async",
        AsyncMock(side_effect=AssertionError("completion delivery must read only the latest note")),
    )
    completion_loop = _CompletionChatLoop(run_manager)
    trigger_service = TriggerService(
        cast(Any, completion_loop),
        run_manager,
        cast(Any, Mock()),
        trigger_chat_loop=cast(Any, completion_loop),
        sessions=sessions,
    )

    async def relay(run: Run, reply_surface: ReplySurface) -> None:
        assert reply_surface == surface
        await run.wait()

    relay_mock = AsyncMock(side_effect=relay)
    trigger_service.set_completion_run_relay(relay_mock)

    delivered = trigger_service.submit_completion(
        "coder",
        "session-one",
        notice_id="terminal:one",
        origin_run_id="origin-run",
        body="terminal finished",
    )
    await asyncio.wait_for(delivered, timeout=1)
    for _ in range(10):
        if relay_mock.await_count:
            break
        await asyncio.sleep(0)

    assert completion_loop.reply_surfaces == [surface]
    relay_mock.assert_awaited_once()
    await trigger_service.aclose()


async def test_idle_completion_does_not_send_webui_surface_to_channel_relay(
    tmp_path: Path,
) -> None:
    run_manager = ChatRunManager()
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="session-one")
    session.add_note(ReplySurface.webui().to_note_content())
    completion_loop = _CompletionChatLoop(run_manager)
    trigger_service = TriggerService(
        cast(Any, completion_loop),
        run_manager,
        cast(Any, Mock()),
        trigger_chat_loop=cast(Any, completion_loop),
        sessions=sessions,
    )
    relay_mock = AsyncMock()
    trigger_service.set_completion_run_relay(relay_mock)

    delivered = trigger_service.submit_completion(
        "coder",
        "session-one",
        notice_id="terminal:webui",
        origin_run_id="origin-run",
        body="terminal finished",
    )
    await asyncio.wait_for(delivered, timeout=1)

    relay_mock.assert_not_awaited()
    assert completion_loop.reply_surfaces == [ReplySurface.webui()]
    await trigger_service.aclose()


async def test_completion_delivery_aclose_cancels_workers_and_pending_notices(
    tmp_path: Path,
) -> None:
    run_manager = ChatRunManager()
    active_started = asyncio.Event()
    active_release = asyncio.Event()

    async def active_executor(_run: Run) -> str:
        active_started.set()
        await active_release.wait()
        return "done"

    parent_run = await run_manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        active_executor,
    )
    await active_started.wait()
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
        notice_id="bash:pending-at-shutdown",
        origin_run_id=parent_run.id,
        body="pending result",
    )
    await asyncio.sleep(0)

    await trigger_service.aclose()

    assert pending.cancelled()
    late = trigger_service.submit_completion(
        "coder",
        "session-one",
        notice_id="bash:after-shutdown",
        origin_run_id=parent_run.id,
        body="late result",
    )
    assert late.cancelled()
    active_release.set()
    assert await parent_run.wait() == "done"


async def test_completion_start_failure_persists_system_reminder_without_run(
    tmp_path: Path,
) -> None:
    run_manager = ChatRunManager()
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="session-one")
    completion_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=RuntimeError("provider unavailable"))
    )
    persisted = Mock()
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
        notice_id="bash:fallback",
        origin_run_id="origin-run",
        body="background command finished",
        on_persisted=persisted,
    )
    await asyncio.wait_for(delivery, timeout=1)

    completion_loop.start_run.assert_awaited_once()
    persisted.assert_called_once_with()
    notes = [
        message.content
        for message in sessions.get(
            SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
        ).load()
        if message.role == "note" and isinstance(message.content, str)
    ]
    assert len(notes) == 1
    assert "background command finished" in notes[0]


async def test_completion_fallback_retries_transient_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_manager = ChatRunManager()
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="session-one")
    completion_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=RuntimeError("provider unavailable"))
    )
    trigger_service = TriggerService(
        cast(Any, completion_loop),
        run_manager,
        cast(Any, Mock()),
        trigger_chat_loop=cast(Any, completion_loop),
        sessions=sessions,
    )
    original_add_note = ChatSession.add_note
    attempts = 0

    def add_note_with_transient_failure(session: ChatSession, content: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ChatSessionError("temporary append failure")
        original_add_note(session, content)

    monkeypatch.setattr(ChatSession, "add_note", add_note_with_transient_failure)
    monkeypatch.setattr(
        automation_module,
        "_COMPLETION_PERSIST_RETRY_INITIAL_SECONDS",
        0.0,
    )

    delivery = trigger_service.submit_completion(
        "coder",
        "session-one",
        notice_id="bash:retry",
        origin_run_id="origin-run",
        body="retry this result",
    )
    await asyncio.wait_for(delivery, timeout=1)

    assert attempts == 2
    notes = [
        message.content
        for message in sessions.get(
            SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
        ).load()
        if message.role == "note" and isinstance(message.content, str)
    ]
    assert len(notes) == 1
    assert "retry this result" in notes[0]


async def test_completion_fallback_prunes_persisted_subagent_batch(tmp_path: Path) -> None:
    run_manager = ChatRunManager()
    sessions = ChatSessionManager(tmp_path)
    sessions.create("parent", session_id="parent-session")
    completion_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=RuntimeError("provider unavailable"))
    )
    trigger_service = TriggerService(
        cast(Any, completion_loop),
        run_manager,
        cast(Any, Mock()),
        trigger_chat_loop=cast(Any, completion_loop),
        sessions=sessions,
    )
    tracker = SubAgentBatchTracker(trigger_service)
    parent_key = ("parent", "parent-session", "parent-run")
    tracker.register(parent_key, "worker", "child-session", "child-run")

    tracker.on_sub_agent_complete(parent_key, "child-run", {"result": "finished work"})
    for _ in range(10):
        if not tracker.references_identity_agent("parent"):
            break
        await asyncio.sleep(0)

    assert tracker.references_identity_agent("parent") is False
    notes = [
        message.content
        for message in sessions.get(
            SessionAddress(project_id=None, agent_id="parent", session_id="parent-session")
        ).load()
        if message.role == "note" and isinstance(message.content, str)
    ]
    assert len(notes) == 1
    assert "finished work" in notes[0]


async def test_completion_delivery_coalesces_every_result_ready_before_run_end(
    tmp_path: Path,
) -> None:
    run_manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_executor(_run: Run) -> str:
        await active_release.wait()
        return "parent complete"

    parent_run = await run_manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        active_executor,
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
    assert "first" in message
    assert "second" in message
    assert message.index("first") < message.index("second")


async def test_completion_delivery_joins_active_run_at_next_request_boundary(
    tmp_path: Path,
) -> None:
    run_manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_executor(_run: Run) -> str:
        await active_release.wait()
        return "parent complete"

    parent_run = await run_manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        active_executor,
    )
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="session-one")
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
        notice_id="bash:in-run",
        origin_run_id=parent_run.id,
        body="finished during the active run",
    )

    assert trigger_service.deliver_background_completions(parent_run, session) is True
    await asyncio.wait_for(delivery, timeout=1)

    notes = [
        message.content
        for message in session.load()
        if message.role == "note" and isinstance(message.content, str)
    ]
    assert len(notes) == 1
    assert "finished during the active run" in notes[0]

    active_release.set()
    await parent_run.wait()
    await asyncio.sleep(0)
    assert completion_loop.messages == []


async def test_completion_finishing_after_boundary_uses_later_delivery(tmp_path: Path) -> None:
    run_manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_executor(_run: Run) -> str:
        await active_release.wait()
        return "parent complete"

    parent_run = await run_manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        active_executor,
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
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        active_executor,
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
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        active_executor,
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
        for message in sessions.get(
            SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
        ).load()
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
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        active_executor,
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
        for message in sessions.get(
            SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
        ).load()
        if message.role == "note" and isinstance(message.content, str)
    ]
    assert any("finished after cancellation" in note for note in notes)
