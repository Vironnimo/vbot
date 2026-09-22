"""Cancellation ownership across Telegram coalescing and dispatch."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from core.channels._telegram_inbound import TelegramInboundBuffer
from core.channels.adapter import ConversationFacts


def conversation(message_id: str = "1") -> ConversationFacts:
    return ConversationFacts(
        platform="telegram",
        channel_id="telegram-test",
        chat_id="12345",
        user_id="50",
        kind="direct",
        message_id=message_id,
    )


@pytest.mark.asyncio
async def test_stop_drains_forward_comment_already_dispatching(monkeypatch):
    monkeypatch.setattr("core.channels._telegram_inbound._FORWARD_COMMENT_SETTLE_SECONDS", 0)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def dispatch(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    buffer = TelegramInboundBuffer("telegram-test", dispatch, AsyncMock())
    await buffer._buffer_possible_forward_comment(conversation(), "question", object())
    task = buffer._forward_comment_tasks["12345"]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await buffer.stop()
        assert cancelled.is_set()
        assert task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_album_item_does_not_cancel_dispatching_batch(monkeypatch):
    monkeypatch.setattr("core.channels._telegram_inbound._ALBUM_FLUSH_SECONDS", 0)
    entered = asyncio.Event()
    release = asyncio.Event()
    first, second = object(), object()
    delivered = []

    async def dispatch(facts, messages, **kwargs):
        if messages == (first,):
            entered.set()
            await release.wait()
        delivered.extend(messages)

    buffer = TelegramInboundBuffer("telegram-test", AsyncMock(), dispatch)
    buffer._buffer_album_message("album", conversation(), first)
    first_task = buffer._album_tasks["album"]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        buffer._buffer_album_message("album", conversation("2"), second)
        second_task = buffer._album_tasks["album"]
        release.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
        assert delivered == [first, second]
    finally:
        release.set()
        await buffer.stop()
