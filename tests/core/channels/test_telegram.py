"""Tests for TelegramChannelAdapter behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from core.channels.adapter import ConversationFacts
from core.channels.channels import ChannelConfig, ChannelConfigError
from core.channels.telegram import TELEGRAM_MESSAGE_LIMIT, TelegramChannelAdapter
from core.chat.chat import ChatSessionManager
from core.chat.runs import ASSISTANT_OUTPUT_EVENT, Run


def make_config(
    *,
    dm_scope: str = "per_conversation",
    allowed_chat_ids: list[int] | None = None,
) -> ChannelConfig:
    return ChannelConfig(
        id="tg-assistant",
        platform="telegram",
        agent_id="assistant",
        dm_scope=dm_scope,
        allowed_chat_ids=list(allowed_chat_ids or []),
        token_env_var="TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
        enabled=True,
    )


def make_update(*, chat_id: int, user_id: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(text=text, message_thread_id=None),
    )


def make_completed_run(*, session_id: str, output_text: str) -> Run:
    run = Run(run_id="run-completed", agent_id="assistant", session_id=session_id)
    run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": output_text}})
    run.mark_completed("ok")
    return run


def make_failed_run(*, session_id: str, message: str) -> Run:
    run = Run(run_id="run-failed", agent_id="assistant", session_id=session_id)
    run.mark_failed(RuntimeError(message))
    return run


def make_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dm_scope: str = "per_conversation",
    allowed_chat_ids: list[int] | None = None,
    trigger_run: AsyncMock | None = None,
) -> tuple[TelegramChannelAdapter, ChatSessionManager, AsyncMock, SimpleNamespace]:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", "test-token")
    chat_sessions = ChatSessionManager(tmp_path)
    trigger_mock = trigger_run or AsyncMock()
    trigger_service = SimpleNamespace(trigger_run=trigger_mock)

    adapter = TelegramChannelAdapter(
        make_config(dm_scope=dm_scope, allowed_chat_ids=allowed_chat_ids),
        cast(Any, trigger_service),
        cast(Any, chat_sessions),
        runtime=SimpleNamespace(),
    )

    bot = SimpleNamespace(send_message=AsyncMock())
    adapter._application = SimpleNamespace(
        bot=bot,
        updater=None,
        stop=AsyncMock(),
        shutdown=AsyncMock(),
    )
    return adapter, chat_sessions, trigger_mock, bot


async def drain_chat_queue(adapter: TelegramChannelAdapter, chat_id: int) -> None:
    queue = adapter._chat_queues.get(str(chat_id))
    if queue is None:
        await asyncio.sleep(0)
        return
    await asyncio.wait_for(queue.join(), timeout=1)


@pytest.mark.parametrize(
    ("dm_scope", "chat_id", "user_id", "expected"),
    [
        ("per_conversation", 12345, 987, "ch-tg-assistant-12345"),
        ("main", 12345, 987, "ch-tg-assistant-main"),
        ("per_peer", 12345, 987, "ch-tg-assistant-u987"),
        ("per_account_channel_peer", 12345, 987, "ch-tg-assistant-12345-u987"),
        ("main", -10001, 987, "ch-tg-assistant--10001"),
    ],
)
def test_derive_session_id(dm_scope: str, chat_id: int, user_id: int, expected: str) -> None:
    adapter = TelegramChannelAdapter.__new__(TelegramChannelAdapter)
    adapter._config = make_config(dm_scope=dm_scope, allowed_chat_ids=[chat_id])

    session_id = TelegramChannelAdapter._derive_session_id(
        adapter,
        ConversationFacts(
            platform="telegram",
            channel_id="tg-assistant",
            chat_id=str(chat_id),
            user_id=str(user_id),
            thread_id=None,
        ),
    )

    assert session_id == expected


def test_constructor_requires_token_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)

    with pytest.raises(ChannelConfigError, match="Missing Telegram token"):
        TelegramChannelAdapter(
            make_config(allowed_chat_ids=[12345]),
            trigger_service=cast(Any, SimpleNamespace(trigger_run=AsyncMock())),
            chat_sessions=cast(Any, ChatSessionManager(tmp_path)),
            runtime=SimpleNamespace(),
        )


@pytest.mark.asyncio
async def test_allowed_chat_ids_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=99999, user_id=50, text="hi"),
        SimpleNamespace(),
    )
    await asyncio.sleep(0)

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_system_reminder_written_once_for_new_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        side_effect=[
            make_completed_run(session_id=session_id, output_text="first"),
            make_completed_run(session_id=session_id, output_text="second"),
        ]
    )
    adapter, chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
    )

    update = make_update(chat_id=12345, user_id=50, text="hello")
    await adapter._handle_inbound_message(update, SimpleNamespace())
    await drain_chat_queue(adapter, 12345)

    await adapter._handle_inbound_message(update, SimpleNamespace())
    await drain_chat_queue(adapter, 12345)

    session = chat_sessions.get("assistant", session_id)
    notes = [message for message in session.load() if message.role == "note"]
    metadata = chat_sessions.get_metadata("assistant", session_id)

    assert len(notes) == 1
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
    }
    await adapter.stop()


@pytest.mark.asyncio
async def test_completed_run_forwards_final_assistant_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="final reply")
    )
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

    bot.send_message.assert_awaited_once_with(chat_id=12345, text="final reply")
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_splits_message_at_telegram_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    payload = "x" * (TELEGRAM_MESSAGE_LIMIT * 2 + 9)
    await adapter.send(payload, "12345")

    chunks = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert [len(chunk) for chunk in chunks] == [TELEGRAM_MESSAGE_LIMIT, TELEGRAM_MESSAGE_LIMIT, 9]
    await adapter.stop()


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
    assert "failed" in error_text.lower()
    assert "boom" in error_text
    await adapter.stop()
