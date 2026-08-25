"""Tests for TelegramChannelAdapter behavior."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.chat.commands import CommandFeedback, CommandOutcome
from tests.core.channels.telegram_test_support import (
    drain_chat_queue,
    make_adapter,
    make_command_dispatcher,
    make_failed_run,
    make_update,
)


@pytest.mark.asyncio
async def test_failed_run_sends_error_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(return_value=make_failed_run(session_id=session_id, message="boom"))
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="hello"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    bot.send_message.assert_awaited_once()
    error_text = bot.send_message.await_args.kwargs["text"]
    assert "try again" in error_text.lower()
    assert "boom" not in error_text
    await adapter.stop()


@pytest.mark.asyncio
async def test_trigger_run_exception_does_not_leak_internal_error_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    trigger_mock = AsyncMock(side_effect=RuntimeError("internal stack trace"))
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
    )
    caplog.set_level(logging.ERROR, logger="vbot.channels.engine")

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="hello"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert "internal stack trace" not in sent_text
    log_records = [
        record
        for record in caplog.records
        if record.message.startswith("Channel trigger run failed")
    ]
    assert len(log_records) == 1
    assert log_records[0].exc_info is not None
    assert "tg-assistant" in log_records[0].message
    assert "ch-tg-assistant-12345" in log_records[0].message
    await adapter.stop()


@pytest.mark.asyncio
async def test_compact_command_exception_is_logged_with_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    command_dispatcher = make_command_dispatcher(
        result=CommandOutcome(
            command="compact",
            feedback=CommandFeedback(kind="notice", text="unused"),
        )
    )
    command_dispatcher.execute.side_effect = RuntimeError("compact failed")
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
    )
    caplog.set_level(logging.ERROR, logger="vbot.channels.engine")

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/compact"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert "compact failed" not in sent_text
    log_records = [
        record for record in caplog.records if record.message.startswith("Channel command failed")
    ]
    assert len(log_records) == 1
    assert log_records[0].exc_info is not None
    assert "command=compact" in log_records[0].message
    assert "ch-tg-assistant-12345" in log_records[0].message
    await adapter.stop()


@pytest.mark.asyncio
async def test_redelivered_update_is_skipped_via_persisted_watermark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telegram redelivers unconfirmed updates after a restart; only new ids run."""
    from core.channels.channels import ChannelStorage

    storage = ChannelStorage(tmp_path)
    storage.save_update_offset("tg-assistant", 7)

    trigger_mock = AsyncMock()
    adapter, _chat_sessions, _trigger, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        update_offset_store=storage,
    )
    # What start() does before polling begins.
    adapter._last_update_id = adapter._load_update_offset()

    redelivered = make_update(chat_id=12345, user_id=50, text="hello")
    redelivered.update_id = 7
    fresh = make_update(chat_id=12345, user_id=50, text="hello again")
    fresh.update_id = 8

    await adapter._handle_inbound_message(redelivered, SimpleNamespace())
    trigger_mock.assert_not_awaited()

    await adapter._handle_inbound_message(fresh, SimpleNamespace())
    await drain_chat_queue(adapter, 12345)
    trigger_mock.assert_awaited_once()

    await adapter._await_offset_saves()
    assert storage.load_update_offset("tg-assistant") == 8


@pytest.mark.asyncio
async def test_duplicate_update_inside_one_session_is_claimed_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trigger_mock = AsyncMock()
    adapter, _chat_sessions, _trigger, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
    )

    update = make_update(chat_id=12345, user_id=50, text="hello")
    update.update_id = 3

    await adapter._handle_inbound_message(update, SimpleNamespace())
    await drain_chat_queue(adapter, 12345)
    await adapter._handle_inbound_message(update, SimpleNamespace())

    assert trigger_mock.await_count == 1
    await adapter.stop()


def test_polling_state_survives_storage_reload_and_degrades_on_corruption(
    tmp_path: Path,
) -> None:
    from core.channels.channels import ChannelStorage

    storage = ChannelStorage(tmp_path)
    storage.save_update_offset("tg-assistant", 42)
    reloaded = ChannelStorage(tmp_path)
    assert reloaded.load_update_offset("tg-assistant") == 42

    state_path = tmp_path / "channels" / "tg-assistant" / "polling.json"
    state_path.write_text("{not json", encoding="utf-8")
    assert reloaded.load_update_offset("tg-assistant") == 0

    state_path.write_text('{"version": 99, "last_update_id": 1}', encoding="utf-8")
    assert reloaded.load_update_offset("tg-assistant") == 0
