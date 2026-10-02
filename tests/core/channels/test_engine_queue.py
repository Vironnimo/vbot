"""Channel engine: per-conversation FIFO, waiting-work limits and overflow replies."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.channels.engine as engine_module
from core.runs import ChatRunManager, Run, RunCancelledError
from core.sessions import SessionAddress

from .engine_test_support import (
    SESSION_ID,
    FakeTransport,
    HeldRuns,
    drain,
    make_completed_run,
    make_conversation,
    make_engine,
)

_LIMIT = engine_module.CHANNEL_WAITING_WORK_LIMIT


@pytest.mark.asyncio
async def test_messages_keep_arrival_order_and_an_idle_conversation_retires_its_worker(
    tmp_path: Path,
) -> None:
    earlier_tasks = asyncio.all_tasks()

    def conversation_workers() -> list[asyncio.Task[object]]:
        return [
            task
            for task in asyncio.all_tasks() - earlier_tasks
            if task.get_name().endswith(":chat-queue")
        ]

    runs = HeldRuns()
    engine, _sessions, _trigger, _transport = make_engine(tmp_path, trigger_run=runs.trigger)
    try:
        await engine.handle_inbound_text(make_conversation(), "first")
        await runs.wait_started()
        await engine.handle_inbound_text(make_conversation(), "second")
        await engine.handle_inbound_text(make_conversation(), "third")
        await asyncio.sleep(0)
        assert runs.contents == ["first"]
        assert len(conversation_workers()) == 1

        runs.release()
        await drain(engine, 12345)

        assert runs.contents == ["first", "second", "third"]
        assert conversation_workers() == []
        # The next message starts a new worker, which retires again once idle.
        await engine.handle_inbound_text(make_conversation(), "fourth")
        assert len(conversation_workers()) == 1
        await drain(engine, 12345)
        assert runs.contents == ["first", "second", "third", "fourth"]
        assert conversation_workers() == []
    finally:
        runs.release()
        await engine.stop()


@pytest.mark.asyncio
async def test_a_full_chat_queue_rejects_overflow_with_one_busy_reply(tmp_path: Path) -> None:
    waiting_work_manager = ChatRunManager(waiting_work_limit=16)
    runs = HeldRuns()
    media_builder = AsyncMock(return_value=[])
    transport = FakeTransport(media_builder=media_builder)
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=runs.trigger,
        transport=transport,
        waiting_work_manager=waiting_work_manager,
    )
    try:
        await engine.handle_inbound_text(make_conversation(), "running")
        await runs.wait_started()
        for index in range(_LIMIT):
            await engine.handle_inbound_text(make_conversation(), f"queued {index}")
        assert waiting_work_manager.waiting_work_count() == _LIMIT

        await engine.handle_inbound_text(make_conversation(), "overflow one")
        await engine.handle_inbound_text(make_conversation(), "overflow two")
        # Media overflow is rejected before anything is downloaded.
        await engine.handle_inbound_media(make_conversation(), (SimpleNamespace(caption="photo"),))

        media_builder.assert_not_awaited()
        # The busy reply is throttled per chat.
        assert transport.sent_texts == [engine_module._BUSY_REPLY]

        runs.release()
        await drain(engine, 12345)
        assert runs.contents == ["running", *(f"queued {index}" for index in range(_LIMIT))]
    finally:
        runs.release()
        await engine.stop()


@pytest.mark.asyncio
async def test_the_global_waiting_limit_rejects_followups_from_another_chat(
    tmp_path: Path,
) -> None:
    waiting_work_manager = ChatRunManager(waiting_work_limit=2)
    runs = HeldRuns()
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path,
        trigger_run=runs.trigger,
        waiting_work_manager=waiting_work_manager,
    )
    try:
        await engine.handle_inbound_text(make_conversation(chat_id=12345), "running one")
        await runs.wait_started(SESSION_ID)
        await engine.handle_inbound_text(make_conversation(chat_id=12345), "queued one")
        await engine.handle_inbound_text(make_conversation(chat_id=67890), "running two")
        await runs.wait_started("ch-tg-assistant-67890")
        await engine.handle_inbound_text(make_conversation(chat_id=67890), "queued two")
        assert waiting_work_manager.waiting_work_count() == 2

        await engine.handle_inbound_text(make_conversation(chat_id=12345), "global overflow")

        assert transport.sent_texts == [engine_module._BUSY_REPLY]
        runs.release()
        await drain(engine, 12345)
        await drain(engine, 67890)
        assert sorted(runs.contents) == ["queued one", "queued two", "running one", "running two"]
    finally:
        runs.release()
        await engine.stop()


@pytest.mark.asyncio
async def test_observed_message_waits_behind_active_channel_run(tmp_path: Path) -> None:
    runs = HeldRuns()
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=runs.trigger, observe_unaddressed=True
    )
    address = SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)

    def observed_notes() -> list[object]:
        return [
            message.content
            for message in chat_sessions.get(address).load()
            if message.role == "note"
            and isinstance(message.content, str)
            and message.content.startswith("[channel-message] ")
        ]

    try:
        await engine.handle_inbound_text(
            make_conversation(kind="group", mentioned_bot=True), "hello bot"
        )
        await runs.wait_started()
        await engine.handle_inbound_text(
            make_conversation(kind="group", user_display_name="Alice"), "side conversation"
        )
        await asyncio.sleep(0)
        assert observed_notes() == []

        runs.release()
        await drain(engine, 12345)

        assert observed_notes() == ["[channel-message] [Alice|50|member]: side conversation"]
    finally:
        runs.release()
        await engine.stop()


@pytest.mark.asyncio
async def test_removed_run_admission_keeps_channel_followups_and_releases_reservations(
    tmp_path: Path,
) -> None:
    waiting = ChatRunManager(waiting_work_limit=16)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def trigger(*_args: object, **_kwargs: object) -> Run:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            raise RunCancelledError("queued Run removed")
        return make_completed_run(output_text="next")

    engine, _, _, _ = make_engine(
        tmp_path, trigger_run=AsyncMock(side_effect=trigger), waiting_work_manager=waiting
    )
    try:
        await engine.handle_inbound_text(make_conversation(), "removed")
        await asyncio.wait_for(started.wait(), 5)
        await engine.handle_inbound_text(make_conversation(), "next")
        release.set()
        await drain(engine, 12345)
        assert calls == 2
        assert waiting.waiting_work_count() == 0
    finally:
        release.set()
        await engine.stop()
