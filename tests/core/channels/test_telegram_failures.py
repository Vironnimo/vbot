"""Tests for TelegramChannelAdapter behavior."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.chat.commands import CommandAction
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
    compact_mock = AsyncMock(side_effect=RuntimeError("compact failed"))
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="compact"))
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        compact_session=compact_mock,
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
        record
        for record in caplog.records
        if record.message.startswith("Channel command action failed")
    ]
    assert len(log_records) == 1
    assert log_records[0].exc_info is not None
    assert "action=compact" in log_records[0].message
    assert "ch-tg-assistant-12345" in log_records[0].message
    await adapter.stop()


@pytest.mark.asyncio
async def test_continue_command_exception_is_logged_with_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    continue_mock = AsyncMock(side_effect=RuntimeError("continue failed"))
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="continue"))
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        continue_run=continue_mock,
        command_dispatcher=command_dispatcher,
    )
    caplog.set_level(logging.ERROR, logger="vbot.channels.engine")

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/continue"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert "continue failed" not in sent_text
    log_records = [
        record
        for record in caplog.records
        if record.message.startswith("Channel command action failed")
    ]
    assert len(log_records) == 1
    assert log_records[0].exc_info is not None
    assert "action=continue" in log_records[0].message
    assert "ch-tg-assistant-12345" in log_records[0].message
    await adapter.stop()
