"""Tests for TelegramChannelAdapter behavior."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import core.channels.engine as engine_module
import core.channels.telegram as telegram_module
from core.attachments import AttachmentStore
from core.channels.adapter import (
    FileData,
    ReplyPlanFacts,
    RouteFacts,
)
from core.channels.channels import ChannelConfig, ChannelConfigError
from core.channels.telegram import TELEGRAM_MESSAGE_LIMIT, TelegramChannelAdapter
from core.chat import MessageSender
from core.chat.commands import CommandAction, CommandHandled, NotACommand
from core.chat.content_blocks import FileBlock, MediaBlock, TextBlock
from core.runs import ASSISTANT_OUTPUT_EVENT, Run
from core.sessions import ChatSessionManager


def make_config(
    *,
    dm_scope: str = "per_conversation",
    allowed_chat_ids: list[int] | None = None,
    response_mode: str = "mention",
    mention_patterns: list[str] | None = None,
    owner_user_ids: list[str] | None = None,
) -> ChannelConfig:
    return ChannelConfig(
        id="tg-assistant",
        platform="telegram",
        agent_id="assistant",
        dm_scope=dm_scope,
        allowed_chat_ids=list(allowed_chat_ids or []),
        token_env_var="TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
        enabled=True,
        response_mode=response_mode,
        mention_patterns=list(mention_patterns or []),
        owner_user_ids=list(owner_user_ids or []),
    )


def make_update(*, chat_id: int, user_id: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(text=text, message_thread_id=None),
    )


def make_photo_update(
    *,
    chat_id: int,
    user_id: int,
    file_id: str,
    file_unique_id: str,
    caption: str | None = None,
    media_group_id: str | None = None,
    user_full_name: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id, full_name=user_full_name),
        effective_message=SimpleNamespace(
            text=None,
            caption=caption,
            photo=[SimpleNamespace(file_id=file_id, file_unique_id=file_unique_id)],
            document=None,
            media_group_id=media_group_id,
            message_thread_id=None,
        ),
    )


def make_document_update(
    *,
    chat_id: int,
    user_id: int,
    file_id: str,
    file_unique_id: str,
    file_name: str,
    caption: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(
            text=None,
            caption=caption,
            photo=None,
            document=SimpleNamespace(
                file_id=file_id,
                file_unique_id=file_unique_id,
                file_name=file_name,
            ),
            media_group_id=None,
            message_thread_id=None,
        ),
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


def make_command_dispatcher(*, result: object | None = None) -> SimpleNamespace:
    dispatch_result = NotACommand() if result is None else result
    return SimpleNamespace(
        dispatch=Mock(return_value=dispatch_result),
        # Mirrors the real dispatcher closely enough for adapter tests: slash-prefixed
        # text counts as a recognized command.
        recognizes=Mock(side_effect=lambda text: text.strip().startswith("/")),
    )


def make_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dm_scope: str = "per_conversation",
    allowed_chat_ids: list[int] | None = None,
    response_mode: str = "mention",
    mention_patterns: list[str] | None = None,
    owner_user_ids: list[str] | None = None,
    bot_username: str | None = None,
    bot_id: int | None = None,
    trigger_run: AsyncMock | None = None,
    retry_run: AsyncMock | None = None,
    compact_session: AsyncMock | None = None,
    credential_resolver: Callable[[str], str] | None = None,
    attachment_store: AttachmentStore | None = None,
    command_dispatcher: object | None = None,
    set_process_token: bool = True,
) -> tuple[TelegramChannelAdapter, ChatSessionManager, AsyncMock, SimpleNamespace]:
    if set_process_token:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", "test-token")
    else:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)

    chat_sessions = ChatSessionManager(tmp_path)
    trigger_mock = trigger_run or AsyncMock()
    trigger_service = SimpleNamespace(
        trigger_run=trigger_mock,
        retry_run=retry_run or AsyncMock(),
        compact_session=compact_session or AsyncMock(return_value="Context compacted."),
    )
    resolved_command_dispatcher = command_dispatcher or make_command_dispatcher()

    adapter = TelegramChannelAdapter(
        make_config(
            dm_scope=dm_scope,
            allowed_chat_ids=allowed_chat_ids,
            response_mode=response_mode,
            mention_patterns=mention_patterns,
            owner_user_ids=owner_user_ids,
        ),
        cast(Any, trigger_service),
        cast(Any, chat_sessions),
        credential_resolver or (lambda key: os.environ.get(key, "")),
        attachment_store=attachment_store,
        command_dispatcher=cast(Any, resolved_command_dispatcher),
    )
    if bot_username is not None or bot_id is not None:
        adapter._set_bot_identity(SimpleNamespace(id=bot_id, username=bot_username))

    bot = SimpleNamespace(
        send_message=AsyncMock(),
        send_photo=AsyncMock(),
        send_document=AsyncMock(),
        send_media_group=AsyncMock(),
        send_chat_action=AsyncMock(),
        get_file=AsyncMock(),
    )
    adapter._application = SimpleNamespace(
        bot=bot,
        updater=None,
        stop=AsyncMock(),
        shutdown=AsyncMock(),
    )
    return adapter, chat_sessions, trigger_mock, bot


async def drain_chat_queue(adapter: TelegramChannelAdapter, chat_id: int) -> None:
    queue = adapter._engine._chat_queues.get(str(chat_id))
    if queue is None:
        await asyncio.sleep(0)
        return
    await asyncio.wait_for(queue.join(), timeout=1)


def install_fake_telegram_media(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeInputFile:
        def __init__(self, data: bytes, *, filename: str | None = None) -> None:
            self.data = data
            self.filename = filename

    class FakeInputMediaPhoto:
        def __init__(self, media: FakeInputFile, caption: str | None = None) -> None:
            self.media = media
            self.caption = caption

    class FakeInputMediaDocument:
        def __init__(self, media: FakeInputFile, caption: str | None = None) -> None:
            self.media = media
            self.caption = caption

    class FakeReplyParameters:
        def __init__(self, message_id: int, *, allow_sending_without_reply: bool = False) -> None:
            self.message_id = message_id
            self.allow_sending_without_reply = allow_sending_without_reply

    fake_telegram = SimpleNamespace(
        InputFile=FakeInputFile,
        InputMediaPhoto=FakeInputMediaPhoto,
        InputMediaDocument=FakeInputMediaDocument,
        ReplyParameters=FakeReplyParameters,
    )
    monkeypatch.setattr(telegram_module, "_load_telegram", lambda: fake_telegram)


@pytest.mark.parametrize(
    ("chat_id", "expected_kind"),
    [(12345, "direct"), (-10001, "group")],
)
def test_conversation_facts_classifies_kind_by_chat_id_sign(
    chat_id: int, expected_kind: str
) -> None:
    adapter = TelegramChannelAdapter.__new__(TelegramChannelAdapter)
    adapter._config = make_config(allowed_chat_ids=[chat_id])
    adapter._bot_id = None
    adapter._bot_username = None
    adapter._bot_mention_pattern = None

    conversation = adapter._conversation_facts(make_update(chat_id=chat_id, user_id=50, text="hi"))

    assert conversation is not None
    assert conversation.kind == expected_kind


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        (SimpleNamespace(id=50, full_name="Alice Example", username="alice"), "Alice Example"),
        (SimpleNamespace(id=50, full_name="  ", username="alice"), "alice"),
        (SimpleNamespace(id=50, username="alice"), "alice"),
        (SimpleNamespace(id=50, full_name=None, username=None), None),
        (SimpleNamespace(id=50), None),
    ],
)
def test_conversation_facts_display_name_chain(user: SimpleNamespace, expected: str | None) -> None:
    adapter = TelegramChannelAdapter.__new__(TelegramChannelAdapter)
    adapter._config = make_config(allowed_chat_ids=[12345])
    adapter._bot_id = None
    adapter._bot_username = None
    adapter._bot_mention_pattern = None
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=12345),
        effective_user=user,
        effective_message=SimpleNamespace(text="hi", message_thread_id=None),
    )

    conversation = adapter._conversation_facts(update)

    assert conversation is not None
    assert conversation.user_display_name == expected


@pytest.mark.asyncio
async def test_negative_chat_id_routes_to_shared_group_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant--10001"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        dm_scope="main",
        allowed_chat_ids=[-10001],
        response_mode="all",
        trigger_run=trigger_mock,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=-10001, user_id=50, text="hello"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    # Group chats ignore dm_scope and share one session keyed by the chat id.
    assert chat_sessions.exists("assistant", session_id)
    trigger_mock.assert_awaited_once_with(
        "assistant",
        "hello",
        session_id,
        sender=MessageSender(id="50", display_name="50"),
    )
    await adapter.stop()


def test_constructor_requires_token_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)

    with pytest.raises(ChannelConfigError, match="Missing Telegram token"):
        TelegramChannelAdapter(
            make_config(allowed_chat_ids=[12345]),
            trigger_service=cast(Any, SimpleNamespace(trigger_run=AsyncMock())),
            chat_sessions=cast(Any, ChatSessionManager(tmp_path)),
            credential_resolver=lambda key: os.environ.get(key, ""),
            command_dispatcher=cast(Any, make_command_dispatcher()),
        )


def test_constructor_resolves_token_through_injected_credential_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)

    adapter = TelegramChannelAdapter(
        make_config(allowed_chat_ids=[12345]),
        trigger_service=cast(Any, SimpleNamespace(trigger_run=AsyncMock())),
        chat_sessions=cast(Any, ChatSessionManager(tmp_path)),
        credential_resolver=lambda _key: "runtime-token",
        command_dispatcher=cast(Any, make_command_dispatcher()),
    )

    assert adapter._token == "runtime-token"


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
async def test_typing_indicator_refreshes_chat_action_and_stops_after_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    async with adapter._typing_indicator("12345"):
        await asyncio.sleep(0.05)

    bot.send_chat_action.assert_awaited_with(chat_id=12345, action="typing")
    awaited_during_block = bot.send_chat_action.await_count
    assert awaited_during_block >= 1

    await asyncio.sleep(0.05)
    assert bot.send_chat_action.await_count == awaited_during_block
    await adapter.stop()


@pytest.mark.asyncio
async def test_ensure_outbound_session_creates_session_with_channel_reminder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    route = adapter.ensure_outbound_session("12345")

    assert route.agent_id == "assistant"
    assert route.session_id == "ch-tg-assistant-12345"
    session = chat_sessions.get("assistant", "ch-tg-assistant-12345")
    notes = [message for message in session.load() if message.role == "note"]
    assert len(notes) == 1
    assert "Telegram" in (notes[0].content or "")
    await adapter.stop()


@pytest.mark.asyncio
async def test_ensure_outbound_session_reuses_existing_session_without_extra_reminder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    adapter.ensure_outbound_session("12345")
    adapter.ensure_outbound_session("12345")

    session = chat_sessions.get("assistant", "ch-tg-assistant-12345")
    notes = [message for message in session.load() if message.role == "note"]
    assert len(notes) == 1
    await adapter.stop()


@pytest.mark.asyncio
async def test_ensure_outbound_session_rejects_non_integer_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    with pytest.raises(ChannelConfigError, match="platform_target must be an integer chat id"):
        adapter.ensure_outbound_session("not-a-chat-id")
    await adapter.stop()


@pytest.mark.asyncio
async def test_plain_text_command_is_dispatched_before_trigger_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandHandled(reply="Run cancelled."))
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/stop"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    command_dispatcher.dispatch.assert_called_once_with(
        "assistant", "ch-tg-assistant-12345", "/stop"
    )
    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="Run cancelled.")
    await adapter.stop()


@pytest.mark.asyncio
async def test_compact_command_action_replies_without_trigger_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_mock = AsyncMock(return_value="Context compacted.")
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="compact"))
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        compact_session=compact_mock,
        command_dispatcher=command_dispatcher,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/compact"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    compact_mock.assert_awaited_once_with("assistant", "ch-tg-assistant-12345")
    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="Context compacted.")
    await adapter.stop()


@pytest.mark.asyncio
async def test_new_command_action_reports_channel_limitation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="new_session"))
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/new"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        chat_id=12345,
        text="Starting a new session is not available from Telegram channels yet.",
    )
    await adapter.stop()


@pytest.mark.asyncio
async def test_retry_command_action_retries_and_relays_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    retry_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="retried reply")
    )
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="retry_last_turn"))
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        retry_run=retry_mock,
        command_dispatcher=command_dispatcher,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/retry"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    retry_mock.assert_awaited_once_with("assistant", session_id)
    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="retried reply")
    await adapter.stop()


@pytest.mark.asyncio
async def test_handoff_command_action_reports_channel_limitation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_command_dispatcher(
        result=CommandAction(name="handoff", argument=None)
    )
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/handoff"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        chat_id=12345,
        text="This command is not available from Telegram channels yet.",
    )
    await adapter.stop()


@pytest.mark.asyncio
async def test_message_handlers_ignore_edited_messages_and_channel_posts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telegram = pytest.importorskip("telegram")
    telegram_ext = pytest.importorskip("telegram.ext")
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    def make_real_message(**content: Any) -> Any:
        return telegram.Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=telegram.Chat(id=12345, type="private"),
            from_user=telegram.User(id=50, first_name="A", is_bot=False),
            **content,
        )

    text_handler, media_handler, unsupported_handler = adapter._build_message_handlers(telegram_ext)
    text_message = make_real_message(text="hi")
    photo_message = make_real_message(
        photo=[telegram.PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)]
    )
    voice_message = make_real_message(
        voice=telegram.Voice(file_id="v", file_unique_id="vu", duration=2)
    )
    sticker_message = make_real_message(
        sticker=telegram.Sticker(
            file_id="s",
            file_unique_id="su",
            width=512,
            height=512,
            is_animated=False,
            is_video=False,
            type="regular",
        )
    )

    assert text_handler.check_update(telegram.Update(update_id=1, message=text_message))
    assert not text_handler.check_update(telegram.Update(update_id=2, edited_message=text_message))
    assert not text_handler.check_update(telegram.Update(update_id=3, channel_post=text_message))
    assert media_handler.check_update(telegram.Update(update_id=4, message=photo_message))
    assert not media_handler.check_update(
        telegram.Update(update_id=5, edited_message=photo_message)
    )
    assert media_handler.check_update(telegram.Update(update_id=6, message=voice_message))
    assert unsupported_handler.check_update(telegram.Update(update_id=7, message=sticker_message))
    assert not unsupported_handler.check_update(telegram.Update(update_id=8, message=voice_message))
    assert not unsupported_handler.check_update(
        telegram.Update(update_id=9, edited_message=sticker_message)
    )
    await adapter.stop()


@pytest.mark.asyncio
async def test_compact_action_runs_in_worker_and_keeps_handler_unblocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_started = asyncio.Event()
    release_compact = asyncio.Event()

    async def slow_compact(_agent_id: str, _session_id: str) -> str:
        compact_started.set()
        await release_compact.wait()
        return "Context compacted."

    command_dispatcher = make_command_dispatcher()
    command_dispatcher.dispatch.side_effect = [
        CommandAction(name="compact"),
        CommandHandled(reply="Run cancelled."),
    ]
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        compact_session=AsyncMock(side_effect=slow_compact),
        command_dispatcher=command_dispatcher,
    )

    await asyncio.wait_for(
        adapter._handle_inbound_message(
            make_update(chat_id=12345, user_id=50, text="/compact"),
            SimpleNamespace(),
        ),
        timeout=1,
    )
    await asyncio.wait_for(compact_started.wait(), timeout=1)

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
async def test_album_flush_window_resets_per_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_module, "_ALBUM_FLUSH_SECONDS", 0.15)
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
    command_dispatcher = make_command_dispatcher()
    command_dispatcher.dispatch.side_effect = [
        NotACommand(),
        CommandHandled(reply="Run cancelled."),
    ]
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

    first_call = command_dispatcher.dispatch.call_args_list[0]
    second_call = command_dispatcher.dispatch.call_args_list[1]
    assert first_call.args == ("assistant", session_id, "hello")
    assert second_call.args == ("assistant", session_id, "/stop")
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
    command_dispatcher.dispatch.side_effect = [
        NotACommand(),
        NotACommand(),
    ]
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

    first_call = command_dispatcher.dispatch.call_args_list[0]
    second_call = command_dispatcher.dispatch.call_args_list[1]
    assert first_call.args == ("assistant", session_id, "hello")
    assert second_call.args == ("assistant", session_id, "still queued")
    assert trigger_mock.await_count == 1

    queue = adapter._engine._chat_queues.get("12345")
    assert queue is not None
    assert queue.qsize() == 1

    release_relay.set()
    await drain_chat_queue(adapter, 12345)
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
async def test_send_single_image_file_uses_send_photo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    await adapter.send(
        "caption",
        "12345",
        files=[FileData(filename="image.png", media_type="image/png", data=b"img-bytes")],
    )

    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["chat_id"] == 12345
    assert bot.send_photo.await_args.kwargs["caption"] == "caption"
    bot.send_document.assert_not_awaited()
    bot.send_media_group.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_mixed_single_image_and_document_sends_separate_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    await adapter.send(
        "batch caption",
        "12345",
        files=[
            FileData(filename="a.png", media_type="image/png", data=b"a"),
            FileData(filename="b.pdf", media_type="application/pdf", data=b"%PDF"),
        ],
    )

    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["chat_id"] == 12345
    assert bot.send_photo.await_args.kwargs["caption"] == "batch caption"

    bot.send_document.assert_awaited_once()
    assert bot.send_document.await_args.kwargs["chat_id"] == 12345
    assert "caption" not in bot.send_document.await_args.kwargs

    bot.send_media_group.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_two_images_uses_single_homogeneous_media_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    await adapter.send(
        "batch caption",
        "12345",
        files=[
            FileData(filename="a.png", media_type="image/png", data=b"a"),
            FileData(filename="b.jpg", media_type="image/jpeg", data=b"b"),
        ],
    )

    bot.send_media_group.assert_awaited_once()
    media = bot.send_media_group.await_args.kwargs["media"]
    assert len(media) == 2
    assert {type(item).__name__ for item in media} == {"FakeInputMediaPhoto"}
    assert media[0].caption == "batch caption"
    assert media[1].caption is None
    bot.send_photo.assert_not_awaited()
    bot.send_document.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_mixed_batches_caption_only_on_first_item_of_first_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    await adapter.send(
        "batch caption",
        "12345",
        files=[
            FileData(filename="a.png", media_type="image/png", data=b"a"),
            FileData(filename="b.jpg", media_type="image/jpeg", data=b"b"),
            FileData(filename="c.pdf", media_type="application/pdf", data=b"c"),
            FileData(filename="d.pdf", media_type="application/pdf", data=b"d"),
        ],
    )

    assert bot.send_media_group.await_count == 2
    first_batch = bot.send_media_group.await_args_list[0].kwargs["media"]
    second_batch = bot.send_media_group.await_args_list[1].kwargs["media"]

    assert {type(item).__name__ for item in first_batch} == {"FakeInputMediaPhoto"}
    assert {type(item).__name__ for item in second_batch} == {"FakeInputMediaDocument"}

    assert first_batch[0].caption == "batch caption"
    assert all(item.caption is None for item in first_batch[1:])
    assert all(item.caption is None for item in second_batch)

    bot.send_photo.assert_not_awaited()
    bot.send_document.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_inbound_photo_stores_attachment_and_triggers_media_block(
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

    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nIMG"))
    )

    await adapter._handle_inbound_media(
        make_photo_update(
            chat_id=12345,
            user_id=50,
            file_id="photo-1",
            file_unique_id="uniq-1",
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    trigger_args = await_args.args
    assert trigger_args[0] == "assistant"
    assert trigger_args[2] == session_id
    blocks = trigger_args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    assert isinstance(blocks[0], MediaBlock)
    stored = attachment_store.get(blocks[0].attachment_id)
    assert stored.media_type == "image/png"
    await adapter.stop()


@pytest.mark.asyncio
async def test_inbound_pdf_document_triggers_file_block(
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

    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"%PDF-1.7\n"))
    )

    await adapter._handle_inbound_media(
        make_document_update(
            chat_id=12345,
            user_id=50,
            file_id="doc-1",
            file_unique_id="docuniq-1",
            file_name="report.pdf",
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    assert isinstance(blocks[0], FileBlock)
    assert blocks[0].media_type == "application/pdf"
    await adapter.stop()


@pytest.mark.asyncio
async def test_inbound_text_document_triggers_text_block_with_content(
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

    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"hello from text file"))
    )

    await adapter._handle_inbound_media(
        make_document_update(
            chat_id=12345,
            user_id=50,
            file_id="doc-2",
            file_unique_id="docuniq-2",
            file_name="notes.txt",
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    assert isinstance(blocks[0], TextBlock)
    assert blocks[0].text == "hello from text file"
    await adapter.stop()


@pytest.mark.asyncio
async def test_disallowed_document_type_replies_instead_of_silent_drop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    trigger_mock = AsyncMock()
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    # Non-UTF8 binary without a known signature sniffs to octet-stream -> rejected.
    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"\xff\xfe\xfdbinary"))
    )

    await adapter._handle_inbound_media(
        make_document_update(
            chat_id=12345,
            user_id=50,
            file_id="doc-3",
            file_unique_id="docuniq-3",
            file_name="archive.zip",
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        chat_id=12345,
        text="Sorry, this file type isn't supported yet.",
    )
    await adapter.stop()


@pytest.mark.asyncio
async def test_album_with_one_failing_item_keeps_siblings_and_reports_failure(
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

    bot.get_file.side_effect = [
        SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nok"))
        ),
        SimpleNamespace(
            download_as_bytearray=AsyncMock(side_effect=RuntimeError("download failed"))
        ),
    ]

    queued = engine_module._QueuedInboundMedia(
        route=RouteFacts(agent_id="assistant", session_id=session_id),
        reply_plan=ReplyPlanFacts(channel_id="tg-assistant", platform_target="12345"),
        messages=(
            make_photo_update(
                chat_id=12345,
                user_id=50,
                file_id="photo-ok",
                file_unique_id="uniq-ok",
            ).effective_message,
            make_photo_update(
                chat_id=12345,
                user_id=50,
                file_id="photo-broken",
                file_unique_id="uniq-broken",
            ).effective_message,
        ),
    )

    await adapter._engine._process_queued_media(queued)

    sent_texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert sent_texts == [
        "Sorry, I couldn't process the attached file. Please try again.",
        "ok",
    ]
    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    assert isinstance(blocks[0], MediaBlock)
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attribute_name", "payload", "expected_media_type", "expected_filename"),
    [
        ("voice", b"OggS\x00\x02opus", "audio/ogg", "telegram-voice-vu-1.ogg"),
        ("audio", b"ID3\x04\x00mp3", "audio/mpeg", "telegram-audio-vu-1"),
        ("video", b"\x00\x00\x00\x18ftypisom", "video/mp4", "telegram-video-vu-1.mp4"),
        (
            "video_note",
            b"\x00\x00\x00\x18ftypisom",
            "video/mp4",
            "telegram-video-note-vu-1.mp4",
        ),
    ],
)
async def test_inbound_audio_video_message_triggers_media_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attribute_name: str,
    payload: bytes,
    expected_media_type: str,
    expected_filename: str,
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

    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(payload))
    )

    media_object = SimpleNamespace(file_id="media-1", file_unique_id="vu-1", file_name=None)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=12345),
        effective_user=SimpleNamespace(id=50),
        effective_message=SimpleNamespace(
            text=None,
            caption="check this",
            photo=None,
            document=None,
            media_group_id=None,
            message_thread_id=None,
            **{attribute_name: media_object},
        ),
    )

    await adapter._handle_inbound_media(update, SimpleNamespace())
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 2
    assert isinstance(blocks[0], TextBlock)
    assert blocks[0].text == "check this"
    assert isinstance(blocks[1], MediaBlock)
    assert blocks[1].media_type == expected_media_type
    assert blocks[1].filename == expected_filename
    await adapter.stop()


@pytest.mark.asyncio
async def test_unsupported_message_type_replies_for_allowed_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    voice_update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=12345),
        effective_user=SimpleNamespace(id=50),
        effective_message=SimpleNamespace(text=None, message_thread_id=None),
    )

    await adapter._handle_unsupported_message_type(voice_update, SimpleNamespace())

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        chat_id=12345,
        text="Sorry, this message type isn't supported yet.",
    )
    await adapter.stop()


@pytest.mark.asyncio
async def test_unsupported_message_type_ignores_disallowed_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    voice_update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=99999),
        effective_user=SimpleNamespace(id=50),
        effective_message=SimpleNamespace(text=None, message_thread_id=None),
    )

    await adapter._handle_unsupported_message_type(voice_update, SimpleNamespace())

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_album_messages_are_buffered_into_single_trigger_run(
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

    bot.get_file.side_effect = [
        SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nA"))
        ),
        SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nB"))
        ),
    ]

    await adapter._handle_inbound_media(
        make_photo_update(
            chat_id=12345,
            user_id=50,
            file_id="photo-a",
            file_unique_id="uniq-a",
            media_group_id="album-1",
        ),
        SimpleNamespace(),
    )
    await adapter._handle_inbound_media(
        make_photo_update(
            chat_id=12345,
            user_id=50,
            file_id="photo-b",
            file_unique_id="uniq-b",
            media_group_id="album-1",
        ),
        SimpleNamespace(),
    )

    await asyncio.sleep(0.6)
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 2
    assert isinstance(blocks[0], MediaBlock)
    assert isinstance(blocks[1], MediaBlock)
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_album_carries_sender_into_trigger_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant--10001"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        response_mode="all",
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    bot.get_file.side_effect = [
        SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nA"))
        ),
        SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nB"))
        ),
    ]

    await adapter._handle_inbound_media(
        make_photo_update(
            chat_id=-10001,
            user_id=50,
            file_id="photo-a",
            file_unique_id="uniq-a",
            media_group_id="album-1",
            user_full_name="Alice Example",
        ),
        SimpleNamespace(),
    )
    await adapter._handle_inbound_media(
        make_photo_update(
            chat_id=-10001,
            user_id=50,
            file_id="photo-b",
            file_unique_id="uniq-b",
            media_group_id="album-1",
            user_full_name="Alice Example",
        ),
        SimpleNamespace(),
    )

    await asyncio.sleep(0.6)
    await drain_chat_queue(adapter, -10001)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    assert await_args.kwargs["sender"] == MessageSender(id="50", display_name="Alice Example")
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
async def test_retry_command_exception_is_logged_with_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    retry_mock = AsyncMock(side_effect=RuntimeError("retry failed"))
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="retry_last_turn"))
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        retry_run=retry_mock,
        command_dispatcher=command_dispatcher,
    )
    caplog.set_level(logging.ERROR, logger="vbot.channels.engine")

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/retry"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert "retry failed" not in sent_text
    log_records = [
        record
        for record in caplog.records
        if record.message.startswith("Channel command action failed")
    ]
    assert len(log_records) == 1
    assert log_records[0].exc_info is not None
    assert "action=retry_last_turn" in log_records[0].message
    assert "ch-tg-assistant-12345" in log_records[0].message
    await adapter.stop()


def make_group_update(
    *,
    chat_id: int = -10001,
    user_id: int = 50,
    text: str | None = "hello",
    message_id: int | None = None,
    reply_to_user_id: int | None = None,
) -> SimpleNamespace:
    reply_to_message = None
    if reply_to_user_id is not None:
        reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=reply_to_user_id))
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(
            text=text,
            message_thread_id=None,
            message_id=message_id,
            reply_to_message=reply_to_message,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/stop@MyBot", "/stop"),
        ("/stop@mybot", "/stop"),
        ("/handoff@MyBot coder", "/handoff coder"),
        ("/stop@OtherBot", "/stop@OtherBot"),
        ("/stop", "/stop"),
        ("hello @MyBot", "hello @MyBot"),
    ],
)
async def test_strip_bot_command_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    expected: str,
) -> None:
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        bot_username="MyBot",
        bot_id=999,
    )

    assert adapter._strip_bot_command_suffix(text) == expected
    await adapter.stop()


@pytest.mark.asyncio
async def test_dm_command_with_own_bot_suffix_is_dispatched_stripped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandHandled(reply="Run cancelled."))
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/stop@MyBot"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    command_dispatcher.dispatch.assert_called_once_with(
        "assistant", "ch-tg-assistant-12345", "/stop"
    )
    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="Run cancelled.")
    await adapter.stop()


@pytest.mark.asyncio
async def test_command_addressed_to_other_bot_is_not_stripped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    command_dispatcher = make_command_dispatcher()
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        command_dispatcher=command_dispatcher,
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/stop@OtherBot"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    command_dispatcher.dispatch.assert_called_once_with("assistant", session_id, "/stop@OtherBot")
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_message_without_mention_is_dropped_in_mention_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_inbound_message(
        make_group_update(text="just chatting"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    assert not chat_sessions.exists("assistant", "ch-tg-assistant--10001")
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_message_with_bot_mention_triggers_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant--10001"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        trigger_run=trigger_mock,
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_inbound_message(
        make_group_update(text="hi @MyBot, are you there?"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    trigger_mock.assert_awaited_once()
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_reply_to_bot_message_triggers_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant--10001"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        trigger_run=trigger_mock,
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_inbound_message(
        make_group_update(text="what did you mean?", reply_to_user_id=999),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    trigger_mock.assert_awaited_once()
    await adapter.stop()


@pytest.mark.asyncio
async def test_mentions_bot_checks_caption_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        bot_username="MyBot",
        bot_id=999,
    )

    assert adapter._mentions_bot(SimpleNamespace(text=None, caption="@MyBot look at this"))
    assert not adapter._mentions_bot(SimpleNamespace(text=None, caption="@MyBotty look"))
    assert not adapter._mentions_bot(SimpleNamespace(text=None, caption=None))
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_reply_uses_reply_parameters_on_first_chunk_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    session_id = "ch-tg-assistant--10001"
    long_reply = "x" * (TELEGRAM_MESSAGE_LIMIT + 5)
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text=long_reply)
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        response_mode="all",
        trigger_run=trigger_mock,
    )

    await adapter._handle_inbound_message(
        make_group_update(text="hello", message_id=777),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    assert bot.send_message.await_count == 2
    first_call = bot.send_message.await_args_list[0]
    second_call = bot.send_message.await_args_list[1]
    assert first_call.kwargs["reply_parameters"].message_id == 777
    assert first_call.kwargs["reply_parameters"].allow_sending_without_reply is True
    assert "reply_parameters" not in second_call.kwargs
    await adapter.stop()


@pytest.mark.asyncio
async def test_dm_reply_is_sent_without_reply_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
    )

    update = make_update(chat_id=12345, user_id=50, text="hello")
    update.effective_message.message_id = 555
    await adapter._handle_inbound_message(update, SimpleNamespace())
    await drain_chat_queue(adapter, 12345)

    bot.send_message.assert_awaited_once_with(chat_id=12345, text="ok")
    await adapter.stop()


@pytest.mark.asyncio
async def test_unsupported_message_type_in_group_is_silent_when_not_addressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_unsupported_message_type(
        make_group_update(text=None),
        SimpleNamespace(),
    )

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_unsupported_message_type_in_group_replies_when_addressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_unsupported_message_type(
        make_group_update(text=None, reply_to_user_id=999),
        SimpleNamespace(),
    )

    bot.send_message.assert_awaited_once_with(
        chat_id=-10001,
        text="Sorry, this message type isn't supported yet.",
    )
    await adapter.stop()
