"""Bounded Discord history travels with the addressed turn's admission."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.attachments import AttachmentStore
from core.runs import ASSISTANT_OUTPUT_EVENT, ChatRunManager, Run
from core.sessions import SessionAddress
from tests.core.channels.discord_helpers import (
    FakeAttachment,
    FakeChannel,
    make_adapter,
    make_message,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


@pytest.mark.asyncio
@pytest.mark.parametrize("with_attachment", [False, True])
@pytest.mark.parametrize("initially_busy", [False, True])
async def test_full_history_does_not_displace_trigger_or_disappear_on_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_attachment: bool, initially_busy: bool
) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    channel.history_messages = [
        make_message(channel, message_id=index, author_id=50, content=f"context {index}")
        for index in range(50, 0, -1)
    ]
    trigger = AsyncMock()
    adapter, sessions, _trigger, _client = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
        attachment_store=AttachmentStore(tmp_path),
        trigger_run=trigger,
    )
    waiting = ChatRunManager(waiting_work_limit=1)
    monkeypatch.setattr(
        adapter._engine._trigger_service, "reserve_waiting_work", waiting.reserve_waiting_work
    )
    monkeypatch.setattr(
        adapter._engine._trigger_service,
        "release_waiting_work",
        lambda admission: (
            waiting.release_waiting_work(admission) if admission is not None else False
        ),
    )
    observed: list[str] = []

    async def completed_run(_agent_id: str, _content: Any, session_id: str, **_kwargs: Any) -> Run:
        observed.extend(
            message.content
            for message in sessions.get(SessionAddress(None, "assistant", session_id)).load()
            if message.role == "note" and isinstance(message.content, str)
        )
        run = Run(run_id="test-run", agent_id="assistant", session_id=session_id)
        run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": "answer"}})
        run.mark_completed("answer")
        return run

    trigger.side_effect = completed_run
    message = make_message(
        channel,
        message_id=200,
        author_id=50,
        content="question <@999>",
        mentions=[SimpleNamespace(id=999)],
        attachments=[FakeAttachment(300, "file.txt", b"context file")] if with_attachment else [],
    )
    try:
        if initially_busy:
            held = waiting.reserve_waiting_work(scope="busy", scope_limit=1)
            await adapter._handle_inbound_message(message)
            trigger.assert_not_awaited()
            assert waiting.waiting_work_count() == 1
            assert not sessions.exists(SessionAddress(None, "assistant", "ch-dc-assistant-100"))
            if with_attachment:
                message.attachments[0].read.assert_not_awaited()
            waiting.release_waiting_work(held)

        await adapter._handle_inbound_message(message)
        queue = adapter._engine._chat_queues.get("100")
        if queue is not None:
            await queue.join()
        trigger.assert_awaited_once()
        assert observed == [
            f"[channel-message] [Alice|50|member]: context {index}" for index in range(1, 51)
        ]
        assert waiting.waiting_work_count() == 0
    finally:
        await adapter.stop()
        await waiting.aclose()


@pytest.mark.asyncio
async def test_stop_releases_one_admission_for_pending_history_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    channel.history_messages = [
        make_message(channel, message_id=index, author_id=50, content=f"context {index}")
        for index in range(50, 0, -1)
    ]
    adapter, _sessions, trigger, _client = make_adapter(
        tmp_path, target=channel, allowed_chat_ids=[100]
    )
    waiting = ChatRunManager(waiting_work_limit=32)
    monkeypatch.setattr(
        adapter._engine._trigger_service, "reserve_waiting_work", waiting.reserve_waiting_work
    )
    monkeypatch.setattr(
        adapter._engine._trigger_service,
        "release_waiting_work",
        lambda admission: (
            waiting.release_waiting_work(admission) if admission is not None else False
        ),
    )
    processing = asyncio.Event()

    async def pause_processing(_queued: Any) -> None:
        processing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(adapter._engine, "_process_queued_work", pause_processing)
    try:
        await adapter._handle_inbound_message(
            make_message(
                channel,
                message_id=200,
                author_id=50,
                content="question <@999>",
                mentions=[SimpleNamespace(id=999)],
            )
        )
        await processing.wait()
        assert waiting.waiting_work_count() == 1
        await adapter.stop()
        assert waiting.waiting_work_count() == 0
        trigger.assert_not_awaited()
    finally:
        await adapter.stop()
        await waiting.aclose()


@pytest.mark.asyncio
async def test_pending_triggers_share_history_without_duplicate_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    channel.history_messages = [
        make_message(channel, message_id=index, author_id=50, content=f"context {index}")
        for index in range(3, 0, -1)
    ]
    trigger = AsyncMock()
    adapter, sessions, _trigger, _client = make_adapter(
        tmp_path, target=channel, allowed_chat_ids=[100], trigger_run=trigger
    )
    release_worker = asyncio.Event()
    process = adapter._engine._process_queued_work

    async def pause_processing(queued: Any) -> None:
        await release_worker.wait()
        await process(queued)

    async def completed_run(_agent: str, _content: Any, session_id: str, **_kwargs: Any) -> Run:
        run = Run(
            run_id=f"test-run-{trigger.await_count}", agent_id="assistant", session_id=session_id
        )
        run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": "answer"}})
        run.mark_completed("answer")
        return run

    trigger.side_effect = completed_run
    monkeypatch.setattr(adapter._engine, "_process_queued_work", pause_processing)
    first = make_message(
        channel,
        message_id=200,
        author_id=50,
        content="first <@999>",
        mentions=[SimpleNamespace(id=999)],
    )
    try:
        await adapter._handle_inbound_message(first)
        channel.history_messages.insert(0, first)
        await adapter._handle_inbound_message(
            make_message(
                channel,
                message_id=201,
                author_id=50,
                content="second <@999>",
                mentions=[SimpleNamespace(id=999)],
            )
        )
        release_worker.set()
        await adapter._engine._chat_queues["100"].join()
        assert trigger.await_count == 2
        notes = [
            message.content
            for message in sessions.get(
                SessionAddress(None, "assistant", "ch-dc-assistant-100")
            ).load()
            if message.role == "note"
        ]
        assert notes == [
            f"[channel-message] [Alice|50|member]: context {index}" for index in range(1, 4)
        ]
    finally:
        release_worker.set()
        await adapter.stop()
