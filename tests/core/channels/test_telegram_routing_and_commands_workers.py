"""Telegram routing and commands: workers behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import core.channels._telegram_inbound as telegram_inbound
from core.attachments import AttachmentStore, AttachmentTooLargeError
from core.chat.commands import (
    CommandFeedback,
    CommandOutcome,
    PreparedCommand,
)
from core.chat.content_blocks import MediaBlock
from core.runs import Run
from tests.core.channels.telegram_test_support import (
    drain_chat_queue,
    make_adapter,
    make_command_dispatcher,
    make_completed_run,
    make_photo_update,
    make_update,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

_ASYNC_COORDINATION_TIMEOUT_SECONDS = 10.0


@pytest.mark.asyncio
async def test_compact_action_runs_in_worker_and_keeps_handler_unblocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_started = asyncio.Event()
    release_compact = asyncio.Event()

    def prepare(text: str) -> PreparedCommand | None:
        if text == "/compact":
            return PreparedCommand(name="compact", argument=None, execution_mode="serialized")
        if text == "/stop":
            return PreparedCommand(name="stop", argument=None, execution_mode="immediate")
        return None

    async def execute(prepared: PreparedCommand, _context: Any) -> CommandOutcome:
        if prepared.name == "compact":
            compact_started.set()
            await release_compact.wait()
            return CommandOutcome(
                command="compact",
                feedback=CommandFeedback(kind="notice", text="Context compacted."),
            )
        return CommandOutcome(
            command="stop",
            feedback=CommandFeedback(kind="notice", text="Run cancelled."),
        )

    command_dispatcher = SimpleNamespace(
        prepare=prepare,
        unavailability=lambda _prepared, _surface: None,
        execute=AsyncMock(side_effect=execute),
    )
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
    )

    await asyncio.wait_for(
        adapter._handle_inbound_message(
            make_update(chat_id=12345, user_id=50, text="/compact"),
            SimpleNamespace(),
        ),
        timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS,
    )
    await asyncio.wait_for(compact_started.wait(), timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/stop"),
        SimpleNamespace(),
    )
    await asyncio.sleep(0)
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="Run cancelled.")

    release_compact.set()
    await drain_chat_queue(adapter, 12345)

    sent_texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert sent_texts == ["Run cancelled.", "Context compacted."]
    trigger_mock.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_media_download_runs_in_worker_not_in_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    release_download = asyncio.Event()

    async def slow_download() -> bytearray:
        await release_download.wait()
        return bytearray(b"\x89PNG\r\n\x1a\nIMG")

    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(side_effect=slow_download)
    )

    await asyncio.wait_for(
        adapter._handle_inbound_media(
            make_photo_update(
                chat_id=12345,
                user_id=50,
                file_id="photo-1",
                file_unique_id="uniq-1",
            ),
            SimpleNamespace(),
        ),
        timeout=1,
    )
    trigger_mock.assert_not_awaited()

    release_download.set()
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert isinstance(blocks[0], MediaBlock)
    await adapter.stop()


@pytest.mark.asyncio
async def test_oversized_inbound_media_rejected_before_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path, max_size_bytes=8)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        attachment_store=attachment_store,
    )
    download_mock = AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nIMG"))
    # get_file returns metadata (including file_size) only; the body is a separate fetch.
    bot.get_file.return_value = SimpleNamespace(
        file_size=1_000_000,
        download_as_bytearray=download_mock,
    )
    raw_message = make_photo_update(
        chat_id=12345,
        user_id=50,
        file_id="photo-1",
        file_unique_id="uniq-1",
    ).effective_message

    with pytest.raises(AttachmentTooLargeError):
        await adapter._transport.build_media_blocks(raw_message)

    # The oversized file is refused on its reported size; the body is never downloaded.
    download_mock.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_album_flush_window_resets_per_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_inbound, "_ALBUM_FLUSH_SECONDS", 0.15)
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    bot.get_file.side_effect = [
        SimpleNamespace(
            download_as_bytearray=AsyncMock(
                return_value=bytearray(b"\x89PNG\r\n\x1a\n" + bytes([index]))
            )
        )
        for index in range(3)
    ]

    # Items spaced inside the window but with a cumulative span beyond it: without the
    # per-item reset the album would flush after item 2 and split into two Runs.
    for index in range(3):
        await adapter._handle_inbound_media(
            make_photo_update(
                chat_id=12345,
                user_id=50,
                file_id=f"photo-{index}",
                file_unique_id=f"uniq-{index}",
                media_group_id="album-1",
            ),
            SimpleNamespace(),
        )
        await asyncio.sleep(0.1)

    await asyncio.sleep(0.2)
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 3
    await adapter.stop()


@pytest.mark.asyncio
async def test_stop_command_is_eagerly_dispatched_while_chat_worker_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    command_dispatcher = make_command_dispatcher(
        result=CommandOutcome(
            command="stop",
            feedback=CommandFeedback(kind="notice", text="Run cancelled."),
        )
    )
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=session_id)
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        command_dispatcher=command_dispatcher,
    )

    relay_started = asyncio.Event()
    release_relay = asyncio.Event()

    async def block_relay(_run: Run, _platform_target: str) -> None:
        relay_started.set()
        await release_relay.wait()

    monkeypatch.setattr(
        adapter._engine,
        "_relay_run_events",
        AsyncMock(side_effect=block_relay),
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="hello"),
        SimpleNamespace(),
    )
    await asyncio.wait_for(relay_started.wait(), timeout=1)

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/stop"),
        SimpleNamespace(),
    )
    await asyncio.sleep(0)

    # Plain text never touches the dispatcher; the command dispatched eagerly while
    # the worker was still blocked relaying the first message's run.
    command_dispatcher.execute.assert_awaited_once()
    assert trigger_mock.await_count == 1
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="Run cancelled.")

    release_relay.set()
    await drain_chat_queue(adapter, 12345)
    await adapter.stop()


@pytest.mark.asyncio
async def test_non_command_text_still_queues_while_chat_worker_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    command_dispatcher = make_command_dispatcher()
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=session_id)
    )
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        command_dispatcher=command_dispatcher,
    )

    relay_started = asyncio.Event()
    release_relay = asyncio.Event()

    async def block_relay(_run: Run, _platform_target: str) -> None:
        relay_started.set()
        await release_relay.wait()

    monkeypatch.setattr(
        adapter._engine,
        "_relay_run_events",
        AsyncMock(side_effect=block_relay),
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="hello"),
        SimpleNamespace(),
    )
    await asyncio.wait_for(relay_started.wait(), timeout=1)

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="still queued"),
        SimpleNamespace(),
    )
    await asyncio.sleep(0)

    # Plain text is queued without any dispatcher involvement.
    command_dispatcher.execute.assert_not_awaited()
    assert trigger_mock.await_count == 1

    queue = adapter._engine._chat_queues.get("12345")
    assert queue is not None
    assert queue.qsize() == 1

    release_relay.set()
    await drain_chat_queue(adapter, 12345)
    await adapter.stop()
