"""Tests for TelegramChannelAdapter behavior."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.channels.adapter import ReplyPlanFacts
from core.channels.telegram import TELEGRAM_MESSAGE_LIMIT
from core.chat.commands import CommandFeedback, CommandOutcome
from tests.core.channels.telegram_test_support import (
    drain_chat_queue,
    make_adapter,
    make_command_dispatcher,
    make_completed_run,
    make_failed_run,
    make_update,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_to", [None, "42"])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_multipart_reply_retries_only_failed_chunk(
    tmp_path, monkeypatch, reply_to, exhausted
):
    from telegram.error import NetworkError

    adapter, _, _, bot = make_adapter(tmp_path, monkeypatch, allowed_chat_ids=[12345])
    monkeypatch.setattr("core.utils.retry.compute_retry_delay", lambda *a, **kw: (0, False))
    chunks = [letter * TELEGRAM_MESSAGE_LIMIT for letter in "abc"]
    failures = 0

    async def send(**payload):
        nonlocal failures
        if payload["text"] == chunks[1] and (exhausted or failures == 0):
            failures += 1
            raise NetworkError("test transport fault")

    bot.send_message.side_effect = send
    plan = ReplyPlanFacts(
        channel_id="tg-assistant",
        platform_target="12345",
        reply_to_message_id=reply_to,
        thread_id="17",
    )
    await adapter._engine._send_reply(plan, "".join(chunks))
    payloads = [call.kwargs for call in bot.send_message.await_args_list]
    assert [payload["text"] for payload in payloads] == (
        [chunks[0], *([chunks[1]] * 4)]
        if exhausted
        else [chunks[0], chunks[1], chunks[1], chunks[2]]
    )
    assert all(payload["message_thread_id"] == 17 for payload in payloads)
    assert all("reply_parameters" not in payload for payload in payloads[1:])
    assert ("reply_parameters" in payloads[0]) == (reply_to is not None)
    await adapter.stop()


@pytest.mark.asyncio
async def test_offset_saves_cannot_regress_when_threads_finish_out_of_order(tmp_path):
    import threading

    from core.channels import ChannelStorage

    storage = ChannelStorage(tmp_path)
    higher_saved = threading.Event()

    def save_lower():
        assert higher_saved.wait(timeout=5)
        storage.save_update_offset("tg-assistant", 7)

    def save_higher():
        try:
            storage.save_update_offset("tg-assistant", 8)
        finally:
            higher_saved.set()

    await asyncio.gather(asyncio.to_thread(save_lower), asyncio.to_thread(save_higher))
    assert ChannelStorage(tmp_path).load_update_offset("tg-assistant") == 8


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
    from core.channels import ChannelStorage

    storage = ChannelStorage(tmp_path)
    storage.save_update_offset("tg-assistant", 7)

    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
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
    await adapter.stop()


@pytest.mark.asyncio
async def test_duplicate_update_inside_one_session_is_claimed_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
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
    from core.channels import ChannelStorage

    storage = ChannelStorage(tmp_path)
    storage.save_update_offset("tg-assistant", 42)
    reloaded = ChannelStorage(tmp_path)
    assert reloaded.load_update_offset("tg-assistant") == 42

    state_path = tmp_path / "channels" / "tg-assistant" / "polling.json"
    state_path.write_text("{not json", encoding="utf-8")
    assert reloaded.load_update_offset("tg-assistant") == 0

    state_path.write_text('{"version": 99, "last_update_id": 1}', encoding="utf-8")
    assert reloaded.load_update_offset("tg-assistant") == 0
