"""Discord Gateway callback ownership during shutdown."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tests.core.channels.discord_helpers import FakeChannel, make_adapter, make_message

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


@pytest.mark.asyncio
async def test_stop_drains_backfill_and_waiting_gateway_callbacks(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    adapter, _sessions, _trigger, client = make_adapter(
        tmp_path, target=channel, allowed_chat_ids=[100]
    )
    history_started = asyncio.Event()
    release_history = asyncio.Event()
    history_cancelled = asyncio.Event()

    async def history(**_kwargs: Any) -> Any:
        history_started.set()
        try:
            await release_history.wait()
        except asyncio.CancelledError:
            history_cancelled.set()
            raise
        if False:
            yield None

    channel.history = history  # type: ignore[method-assign]
    admitted = AsyncMock()
    adapter._engine.handle_inbound_text = admitted  # type: ignore[method-assign]
    message = make_message(
        channel,
        message_id=200,
        author_id=50,
        content="hello <@999>",
        mentions=[SimpleNamespace(id=999)],
    )
    first = asyncio.create_task(adapter._handle_inbound_message(message))
    await history_started.wait()
    second = asyncio.create_task(adapter._handle_inbound_message(message))
    await asyncio.sleep(0)

    try:
        await adapter.stop()
        assert first.done() and second.done()
        assert history_cancelled.is_set()
        admitted.assert_not_awaited()
        client.close.assert_awaited_once()
        assert adapter._message_locks == {}
    finally:
        release_history.set()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_gateway_callback_after_stop_cannot_admit_work(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=None, recipient_id=50)
    adapter, _sessions, _trigger, _client = make_adapter(
        tmp_path, target=channel, allowed_chat_ids=[100]
    )
    admitted = AsyncMock()
    adapter._engine.handle_inbound_text = admitted  # type: ignore[method-assign]

    await adapter.stop()
    await adapter._handle_inbound_message(
        make_message(channel, message_id=200, author_id=50, content="hello")
    )

    admitted.assert_not_awaited()
