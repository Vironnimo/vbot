"""Accepted background context survives transport lifecycle without becoming a Run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.channels._observations import ChannelObservationInbox
from core.sessions import ChatSessionManager, SessionAddress

from .channels_test_support import make_service
from .engine_test_support import (
    SESSION_ID,
    HeldRuns,
    channel_state,
    drain,
    make_conversation,
    make_engine,
    make_new_only_dispatcher,
)

_ADDRESS = SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)


@asynccontextmanager
async def _held_session(sessions: ChatSessionManager) -> AsyncIterator[None]:
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        async with sessions.write_lock(_ADDRESS):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await entered.wait()
    try:
        yield
    finally:
        release.set()
        await holder


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("change", ["restart", "delete", "reassign"])
async def test_stopping_preserves_observed_batches_without_waiting_for_the_session_lock(
    tmp_path: Path,
    queued: bool,
    change: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = HeldRuns()
    engine, sessions, _, _ = make_engine(
        tmp_path, observe_unaddressed=True, trigger_run=runs.trigger
    )
    state = channel_state(tmp_path)
    monkeypatch.setattr(
        "core.channels._observations.utc_now_timestamp", lambda: "2026-10-15T12:00:00.000000Z"
    )
    sessions.get_or_create(_ADDRESS)
    async with _held_session(sessions):
        if queued:
            await engine.handle_inbound_text(
                make_conversation(kind="group", mentioned_bot=True), "hold"
            )
            await runs.wait_started()
        assert await engine.handle_inbound_media(
            make_conversation(kind="group", user_display_name="Alice"),
            (SimpleNamespace(caption="one"), SimpleNamespace(caption="two")),
        )
        assert await engine.handle_inbound_text(
            make_conversation(kind="group", user_display_name="Alice"), "later context"
        )
        # stop must finish while the active Run/other accessor still owns this lease.
        await asyncio.wait_for(engine.stop(), 1)
        assert sessions.get(_ADDRESS).load() == []
        if change == "delete":
            state.unregister(engine.config.id)
        elif change == "reassign":
            state.clear_agent_routes(engine.config.id)
            state.point_conversation(engine.config.id, SESSION_ID, "group", "another-session")
    runs.release()
    inbox = ChannelObservationInbox(state, sessions)
    assert len(await inbox.ids()) == 2
    await inbox.recover()
    await inbox.recover()
    assert [message.content for message in sessions.get(_ADDRESS).load()] == [
        "[channel-message] [Alice|50|member]: [media] one",
        "[channel-message] [Alice|50|member]: [media] two",
        "[channel-message] [Alice|50|member]: later context",
    ]
    assert await inbox.ids() == []


@pytest.mark.asyncio
async def test_recovery_deduplicates_a_committed_batch_after_acknowledgement_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, _, _ = make_engine(tmp_path, observe_unaddressed=True)
    inbox = ChannelObservationInbox(channel_state(tmp_path), sessions)
    sessions.get_or_create(_ADDRESS)
    async with _held_session(sessions):
        await engine.handle_inbound_text(make_conversation(kind="group"), "kept once")
        await engine.stop()
    original = inbox._settle

    async def fail_ack(_delivery_id: str) -> None:
        raise OSError("acknowledgement interrupted")

    monkeypatch.setattr(inbox, "_settle", fail_ack)
    await inbox.recover()
    assert len(sessions.get(_ADDRESS).load()) == 1
    assert len(await inbox.ids()) == 1
    monkeypatch.setattr(inbox, "_settle", original)
    await inbox.recover()
    assert len(sessions.get(_ADDRESS).load()) == 1
    assert await inbox.ids() == []


@pytest.mark.asyncio
async def test_observed_context_follows_a_preceding_new_command(tmp_path: Path) -> None:
    runs = HeldRuns()
    engine, sessions, _, _ = make_engine(
        tmp_path,
        observe_unaddressed=True,
        trigger_run=runs.trigger,
        admin_user_ids=["50"],
        command_dispatcher=make_new_only_dispatcher(),
    )
    try:
        await engine.handle_inbound_text(
            make_conversation(kind="group", mentioned_bot=True), "hold"
        )
        await runs.wait_started()
        await engine.handle_inbound_text(make_conversation(kind="group"), "/new")
        await engine.handle_inbound_text(make_conversation(kind="group"), "new context")
        runs.release()
        await drain(engine, 12345)
        target = channel_state(tmp_path).active_session_id(engine.config.id, SESSION_ID)
        assert target and target != SESSION_ID
        assert sessions.get(_ADDRESS).load() == []
        assert len(sessions.get(SessionAddress(None, "assistant", target)).load()) == 1
    finally:
        runs.release()
        await engine.stop()


@pytest.mark.asyncio
async def test_service_start_recovers_context_of_a_removed_channel(tmp_path: Path) -> None:
    engine, sessions, _, _ = make_engine(tmp_path, observe_unaddressed=True)
    sessions.get_or_create(_ADDRESS)
    async with _held_session(sessions):
        await engine.handle_inbound_text(make_conversation(kind="group"), "survives removal")
        await engine.stop()
    channel_state(tmp_path).unregister(engine.config.id)
    service = make_service(tmp_path, chat_sessions=sessions)
    try:
        service.start()
        await asyncio.gather(*service._observation_tasks)
        assert len(sessions.get(_ADDRESS).load()) == 1
    finally:
        await service.aclose()
        service.close()


@pytest.mark.asyncio
async def test_deleted_session_generation_is_never_recreated_for_old_context(
    tmp_path: Path,
) -> None:
    engine, sessions, _, _ = make_engine(tmp_path, observe_unaddressed=True)
    sessions.get_or_create(_ADDRESS)
    async with _held_session(sessions):
        await engine.handle_inbound_text(make_conversation(kind="group"), "obsolete generation")
        await engine.stop()
    sessions.delete(_ADDRESS)
    sessions.get_or_create(_ADDRESS)
    await engine.recover_observations()
    assert sessions.get(_ADDRESS).load() == []
