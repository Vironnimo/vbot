"""Tests for automation completion."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import core.automation.automation as automation_module
from core.automation import TriggerService
from core.chat import ChatSession, ChatSessionError, ChatSessionManager, ReplySurface
from core.runs import (
    ChatRunManager,
    Run,
    RunAdmission,
    RunAdmissionBlockedError,
    RunExecutionOwner,
    RunKind,
)
from core.sessions import SessionAddress

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]


class _CompletionChatLoop:
    def __init__(self, run_manager: ChatRunManager) -> None:
        self._run_manager = run_manager
        self.messages: list[str] = []
        self.reply_surfaces: list[ReplySurface | None] = []
        self.start_attempts = 0

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
        self.start_attempts += 1

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


class _OwnerAdmission:
    """Admission double for owned completions: only open owners may receive work.

    Mirrors the group owner's contract: a group stops admitting its owners before
    it closes its resources, and a closed epoch or replaced generation never
    becomes admissible again.
    """

    def __init__(self, *owners: RunExecutionOwner) -> None:
        self.open = set(owners)
        self.checked: list[tuple[SessionAddress, RunExecutionOwner]] = []

    def __call__(self, address: SessionAddress, owner: RunExecutionOwner) -> None:
        self.checked.append((address, owner))
        if owner not in self.open:
            raise RunAdmissionBlockedError("This Session is no longer available.")


def _owned_starter(manager: ChatRunManager, started: list[str]) -> Callable[..., Any]:
    async def start(
        address: SessionAddress,
        owner: RunExecutionOwner,
        content: str,
        _notice_ids: tuple[str, ...],
        on_persisted: Callable[[], None],
    ) -> Run:
        async def executor(_run: Run) -> str:
            started.append(content)
            on_persisted()
            return content

        return await manager.start(address, executor, admission=RunAdmission(owner=owner))

    return start


async def _user_cancelled_origin(manager: ChatRunManager) -> Run:
    release = asyncio.Event()

    async def executor(_run: Run) -> str:
        await release.wait()
        return "unused"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"), executor
    )
    return await manager.cancel(run.id, reason="user")


_ADDRESS = SessionAddress(project_id=None, agent_id="coder", session_id="session-one")


async def _active_origin(manager: ChatRunManager) -> tuple[Run, asyncio.Event]:
    """Start the origin Run on the Session; it ends once the event is set."""
    release = asyncio.Event()

    async def executor(_run: Run) -> str:
        await release.wait()
        return "parent complete"

    return await manager.start(_ADDRESS, executor), release


def _completion_service(
    tmp_path: Path, manager: ChatRunManager
) -> tuple[TriggerService, _CompletionChatLoop, ChatSession]:
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="session-one")
    loop = _CompletionChatLoop(manager)
    return TriggerService(cast(Any, loop), manager, Mock(), sessions=sessions), loop, session


def _submit(service: TriggerService, notice_id: str, body: str, origin_run_id: str) -> Any:
    return service.submit_completion(
        "coder", "session-one", notice_id=notice_id, origin_run_id=origin_run_id, body=body
    )


def _notes(session: ChatSession) -> list[str]:
    return [
        message.content
        for message in session.load()
        if message.role == "note" and isinstance(message.content, str)
    ]


_LIVE_OWNER = RunExecutionOwner("swarm", "group", "peer", "generation", "epoch")


@pytest.mark.parametrize("origin_user_cancelled", [False, True])
async def test_owned_completion_for_an_inadmissible_owner_is_rejected_at_submission(
    tmp_path: Path, origin_user_cancelled: bool
) -> None:
    # A user-cancelled origin would persist the result without a Run, bypassing
    # Run admission; the submission check rejects it before any state is kept.
    manager = ChatRunManager()
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="session-one")
    loop = _CompletionChatLoop(manager)
    service = TriggerService(cast(Any, loop), manager, Mock(), sessions=sessions)
    admission = _OwnerAdmission(_LIVE_OWNER)
    stale_owner = RunExecutionOwner("swarm", "group", "peer", "generation", "closed-epoch")
    started: list[str] = []
    service.set_owned_completion_starter(_owned_starter(manager, started))
    service.set_owned_completion_validator(admission)
    origin_id = (await _user_cancelled_origin(manager)).id if origin_user_cancelled else "origin"

    delivery = service.submit_completion(
        "coder",
        "session-one",
        notice_id="stale",
        origin_run_id=origin_id,
        body="stale owned result",
        execution_owner=stale_owner,
    )

    assert delivery.cancelled()
    assert [owner for _address, owner in admission.checked] == [stale_owner]
    assert service.has_execution_work(stale_owner) is False
    for _ in range(3):
        await asyncio.sleep(0)
    assert _notes(session) == []
    assert started == []
    assert loop.messages == []
    await service.aclose()
    await manager.aclose()
    sessions.close()


@pytest.mark.parametrize("origin_user_cancelled", [False, True])
async def test_owned_completion_for_a_live_owner_is_delivered(
    tmp_path: Path, origin_user_cancelled: bool
) -> None:
    manager = ChatRunManager()
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="session-one")
    loop = _CompletionChatLoop(manager)
    service = TriggerService(cast(Any, loop), manager, Mock(), sessions=sessions)
    admission = _OwnerAdmission(_LIVE_OWNER)
    started: list[str] = []
    service.set_owned_completion_starter(_owned_starter(manager, started))
    service.set_owned_completion_validator(admission)
    origin_id = (await _user_cancelled_origin(manager)).id if origin_user_cancelled else "origin"

    delivery = service.submit_completion(
        "coder",
        "session-one",
        notice_id="live",
        origin_run_id=origin_id,
        body="live owned result",
        execution_owner=_LIVE_OWNER,
    )
    await asyncio.wait_for(delivery, 2)

    address = SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
    assert (address, _LIVE_OWNER) in admission.checked
    if origin_user_cancelled:
        # The cancelled origin gets a note without waking the Agent.
        assert started == []
        assert any("live owned result" in note for note in _notes(session))
    else:
        assert len(started) == 1
        assert "live owned result" in started[0]
    assert loop.messages == []
    await service.aclose()
    await manager.aclose()
    sessions.close()


@pytest.mark.parametrize("origin_user_cancelled", [False, True])
async def test_owned_completion_whose_owner_goes_stale_is_not_written_to_the_session(
    tmp_path: Path, origin_user_cancelled: bool
) -> None:
    # Admitted while live, but the owner goes stale before delivery. Its follow-up
    # Run admission fails terminally instead of retrying a Run that can never be
    # admitted, and the Session-write fallback for a user-cancelled origin
    # re-checks the owner instead of bypassing admission.
    admission = _OwnerAdmission(_LIVE_OWNER)

    def validate_run_admission(address: SessionAddress, run_admission: RunAdmission) -> None:
        # Runtime wires the same group check into Run admission.
        if run_admission.owner is not None:
            admission(address, run_admission.owner)

    manager = ChatRunManager(admission_validator=validate_run_admission)
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="session-one")
    loop = _CompletionChatLoop(manager)
    service = TriggerService(cast(Any, loop), manager, Mock(), sessions=sessions)
    started: list[str] = []
    service.set_owned_completion_starter(_owned_starter(manager, started))
    service.set_owned_completion_validator(admission)
    origin_id = (await _user_cancelled_origin(manager)).id if origin_user_cancelled else "origin"

    delivery = service.submit_completion(
        "coder",
        "session-one",
        notice_id="stale-later",
        origin_run_id=origin_id,
        body="stale later result",
        execution_owner=_LIVE_OWNER,
    )
    admission.open.clear()

    with pytest.raises(RunAdmissionBlockedError):
        await asyncio.wait_for(delivery, 2)
    assert _notes(session) == []
    assert started == []
    assert service.has_execution_work(_LIVE_OWNER) is False
    await service.aclose()
    await manager.aclose()
    sessions.close()


async def test_closing_execution_groups_leaves_no_per_group_state(tmp_path: Path) -> None:
    manager = ChatRunManager()
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="session-one")
    service = TriggerService(
        cast(Any, _CompletionChatLoop(manager)), manager, Mock(), sessions=sessions
    )
    owners = [
        RunExecutionOwner("swarm", f"group-{index}", "peer", "generation", f"epoch-{index}")
        for index in range(3)
    ]
    admission = _OwnerAdmission(*owners)
    service.set_owned_completion_starter(_owned_starter(manager, []))
    service.set_owned_completion_validator(admission)
    coordinator = service._completion_delivery

    def state_sizes() -> dict[str, int]:
        return {
            name: len(value)
            for name, value in vars(coordinator).items()
            if isinstance(value, (dict, set, list))
        }

    baseline = state_sizes()
    deliveries = [
        service.submit_completion(
            "coder",
            "session-one",
            notice_id=f"owned-{owner.group_id}",
            origin_run_id="origin",
            body="owned result",
            execution_owner=owner,
        )
        for owner in owners
    ]
    for owner in owners:
        # The group owner stops admitting its owners before closing resources.
        admission.open.discard(owner)
        await service.close_execution_group(owner.extension, owner.group_id, owner.epoch)

    assert all(delivery.cancelled() for delivery in deliveries)
    for _ in range(20):
        if state_sizes() == baseline:
            break
        await asyncio.sleep(0)
    assert state_sizes() == baseline
    assert not any(service.has_execution_work(owner) for owner in owners)
    await service.aclose()
    await manager.aclose()
    sessions.close()


async def test_owned_completion_close_discards_only_matching_notice(tmp_path):
    manager = ChatRunManager()
    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="session-one")
    loop = _CompletionChatLoop(manager)
    service = TriggerService(loop, manager, Mock(), sessions=sessions)
    owner = RunExecutionOwner("swarm", "group", "peer", "generation", "epoch")
    admission = _OwnerAdmission(owner)
    service.set_owned_completion_validator(admission)
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
    admission.open.discard(owner)
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
    # Only owned submissions are checked; ordinary results never consult it.
    assert [checked for _address, checked in admission.checked] == [owner, owner]
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
    service.set_owned_completion_validator(_OwnerAdmission(owner))
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
        "load",
        Mock(side_effect=AssertionError("completion delivery must read only the latest note")),
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


@pytest.mark.parametrize("waiting_for", ["active_run", "blocked_admission"])
async def test_completion_delivery_aclose_persists_pending_results_and_rejects_later_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waiting_for: str
) -> None:
    # A server stop, an update's included, closes completion delivery: a result
    # still waiting at shutdown must survive the restart as a System Reminder.
    run_manager = ChatRunManager()
    origin_run: Run | None = None
    origin_release = asyncio.Event()
    backoff_waits: list[float] = []
    guards = AsyncExitStack()

    async def hold_backoff(delay: float) -> None:
        backoff_waits.append(delay)
        await asyncio.Event().wait()

    monkeypatch.setattr(automation_module, "_sleep", hold_backoff)
    if waiting_for == "active_run":
        origin_run, origin_release = await _active_origin(run_manager)
    else:
        await guards.enter_async_context(run_manager.session_admission_guard(_ADDRESS))
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
    trigger_service.set_owned_completion_validator(_OwnerAdmission(_LIVE_OWNER))
    origin_id = origin_run.id if origin_run is not None else "origin"
    pending = _submit(trigger_service, "bash:pending-at-shutdown", "pending result", origin_id)
    owned = trigger_service.submit_completion(
        "coder",
        "session-one",
        notice_id="owned:pending-at-shutdown",
        origin_run_id=origin_id,
        body="owned result",
        execution_owner=_LIVE_OWNER,
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if origin_run is not None or backoff_waits:
            break
    assert backoff_waits == ([] if origin_run is not None else [0.25])

    await trigger_service.aclose()

    await asyncio.wait_for(pending, timeout=1)
    notes = _notes(session)
    assert len(notes) == 1
    assert "pending result" in notes[0]
    # An owned result is withdrawn: its owner closed before completion delivery.
    assert owned.cancelled()
    assert "owned result" not in notes[0]
    assert completion_loop.messages == []
    late = _submit(trigger_service, "bash:after-shutdown", "late result", origin_id)
    assert late.cancelled()
    origin_release.set()
    await guards.aclose()
    await run_manager.aclose()
    sessions.close()


async def test_blocked_completion_admission_backs_off_until_admission_reopens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Run Admission Guard blocks new Runs and announces no end, so a blocked
    # follow-up retries with a bounded backoff instead of spinning.
    manager = ChatRunManager()
    service, loop, _session = _completion_service(tmp_path, manager)
    delays: list[float] = []
    guards = AsyncExitStack()

    async def record_wait(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 9:
            await guards.aclose()
        await asyncio.sleep(0)

    monkeypatch.setattr(automation_module, "_sleep", record_wait)
    await guards.enter_async_context(manager.session_admission_guard(_ADDRESS))

    delivery = _submit(service, "bash:blocked", "finished while guarded", "origin")
    await asyncio.wait_for(delivery, timeout=1)

    assert delays == [0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert loop.start_attempts == len(delays) + 1
    assert len(loop.messages) == 1
    assert "finished while guarded" in loop.messages[0]
    await service.aclose()
    await manager.aclose()


async def test_closing_during_a_reminder_write_persists_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Closing cancels the delivery worker mid-append; the append still lands and
    # counts as delivered, so closing does not write the same results again.
    manager = ChatRunManager()
    service, _loop, session = _completion_service(tmp_path, manager)
    origin = await _user_cancelled_origin(manager)
    entered = threading.Event()
    release = threading.Event()
    original_add_note = ChatSession.add_note

    def held_add_note(chat_session: ChatSession, content: str, **options: Any) -> None:
        entered.set()
        release.wait(5)
        original_add_note(chat_session, content, **options)

    monkeypatch.setattr(ChatSession, "add_note", held_add_note)
    delivery = _submit(service, "bash:closing", "written while closing", origin.id)
    assert await asyncio.to_thread(entered.wait, 5)

    closing = asyncio.create_task(service.aclose())
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(closing, timeout=5)

    await asyncio.wait_for(delivery, timeout=1)
    notes = _notes(session)
    assert len(notes) == 1
    assert "written while closing" in notes[0]
    await manager.aclose()


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
    await asyncio.wait_for(delivery, timeout=5)

    completion_loop.start_run.assert_awaited_once()
    persisted.assert_called_once_with()
    notes = _notes(sessions.get(_ADDRESS))
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

    def add_note_with_transient_failure(session: ChatSession, content: str, **options: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ChatSessionError("temporary append failure")
        original_add_note(session, content, **options)

    monkeypatch.setattr(ChatSession, "add_note", add_note_with_transient_failure)
    delays: list[float] = []

    async def record_wait(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(automation_module, "_sleep", record_wait)

    delivery = trigger_service.submit_completion(
        "coder",
        "session-one",
        notice_id="bash:retry",
        origin_run_id="origin-run",
        body="retry this result",
    )
    await asyncio.wait_for(delivery, timeout=5)

    assert attempts == 2
    assert delays == [0.25]
    notes = _notes(sessions.get(_ADDRESS))
    assert len(notes) == 1
    assert "retry this result" in notes[0]


async def test_results_ready_at_run_end_coalesce_and_later_ones_get_a_new_delivery(
    tmp_path: Path,
) -> None:
    manager = ChatRunManager()
    origin, release = await _active_origin(manager)
    service, loop, _session = _completion_service(tmp_path, manager)

    first = _submit(service, "bash:one", "### Bash process — completed\nfirst", origin.id)
    second = _submit(service, "subagent:one", "### Sub-Agent worker — completed\nsecond", origin.id)
    await asyncio.sleep(0)
    assert loop.messages == []
    release.set()
    await origin.wait()
    later = _submit(service, "bash:later", "finished later", origin.id)
    await asyncio.wait_for(asyncio.gather(first, second, later), timeout=1)

    # Everything ready at the end of the Run arrives in one follow-up, in order.
    assert len(loop.messages) == 2
    assert loop.messages[0].index("first") < loop.messages[0].index("second")
    assert "finished later" not in loop.messages[0]
    assert "finished later" in loop.messages[1]


async def test_completion_delivery_joins_active_run_at_next_request_boundary(
    tmp_path: Path,
) -> None:
    manager = ChatRunManager()
    origin, release = await _active_origin(manager)
    service, loop, session = _completion_service(tmp_path, manager)
    delivery = _submit(service, "bash:in-run", "finished during the active run", origin.id)

    assert service.deliver_background_completions(origin, session) is True
    await asyncio.wait_for(delivery, timeout=1)

    notes = _notes(session)
    assert len(notes) == 1
    assert "finished during the active run" in notes[0]
    release.set()
    await origin.wait()
    await asyncio.sleep(0)
    assert loop.messages == []


async def test_cancelled_pending_notice_does_not_start_empty_follow_up(
    tmp_path: Path,
) -> None:
    manager = ChatRunManager()
    origin, release = await _active_origin(manager)
    service, loop, _session = _completion_service(tmp_path, manager)

    delivery = _submit(service, "bash:manually-fetched", "already delivered manually", origin.id)
    assert service.cancel_completion("coder", "session-one", notice_id="bash:manually-fetched")
    assert delivery.cancelled()
    release.set()
    await origin.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert loop.messages == []


async def test_user_cancelled_origin_gets_its_completions_as_notes_without_a_run(
    tmp_path: Path,
) -> None:
    manager = ChatRunManager()
    origin, _release = await _active_origin(manager)
    service, loop, session = _completion_service(tmp_path, manager)
    pending = _submit(service, "bash:cancelled-parent", "completed before cancellation", origin.id)

    await manager.cancel(origin.id, reason="user")
    await asyncio.wait_for(pending, timeout=1)
    # A completion arriving after the cancellation takes the same path.
    late = _submit(service, "bash:late-cancelled-parent", "completed after cancellation", origin.id)
    await asyncio.wait_for(late, timeout=1)

    assert loop.messages == []
    notes = _notes(session)
    assert any("completed before cancellation" in note for note in notes)
    assert any("completed after cancellation" in note for note in notes)
