"""Shared fixtures and builders for Telegram channel tests."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import core.channels.telegram as telegram_module
from core.attachments import AttachmentStore
from core.channels.channels import ChannelConfig
from core.channels.telegram import (
    TelegramChannelAdapter,
)
from core.chat import ReplySurface
from core.chat.commands import CommandOutcome, CommandUnavailability, PreparedCommand
from core.runs import ASSISTANT_OUTPUT_EVENT, Run, WaitingWorkAdmission
from core.sessions import ChatSessionManager

from .engine_test_support import MemoryChannelAccessRegistry

CHANNEL_REPLY_SURFACE = ReplySurface.channel(
    platform="telegram",
    platform_display_name="Telegram",
    channel_id="tg-assistant",
)
CHANNEL_GROUP_REPLY_SURFACE = ReplySurface.channel(
    platform="telegram",
    platform_display_name="Telegram",
    channel_id="tg-assistant",
    conversation_kind="group",
)


def make_config(
    *,
    dm_scope: str = "per_conversation",
    allowed_chat_ids: list[int] | None = None,
    response_mode: str = "mention",
    mention_patterns: list[str] | None = None,
    observe_unaddressed: bool = False,
) -> ChannelConfig:
    config = ChannelConfig(
        id="tg-assistant",
        platform="telegram",
        agent_id="assistant",
        dm_scope=dm_scope,
        allowed_chat_ids=cast(Any, list(allowed_chat_ids or [])),
        token_env_var="TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
        enabled=True,
        response_mode=response_mode,
        mention_patterns=list(mention_patterns or []),
        observe_unaddressed=observe_unaddressed,
    )
    config.validate()
    return config


def make_update(
    *,
    chat_id: int,
    user_id: int,
    text: str,
    message_id: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(
            text=text,
            message_id=message_id,
            message_thread_id=None,
        ),
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
    message_id: int | None = None,
    forward_origin: object | None = None,
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
            message_id=message_id,
            message_thread_id=None,
            forward_origin=forward_origin,
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


def make_migration_update(
    *,
    chat_id: int,
    migrate_to: int | None = None,
    migrate_from: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=50),
        effective_message=SimpleNamespace(
            text=None,
            migrate_to_chat_id=migrate_to,
            migrate_from_chat_id=migrate_from,
            message_thread_id=None,
        ),
    )


def make_callback_update(
    *,
    chat_id: int,
    user_id: int,
    data: str,
    callback_id: str = "cb1",
    inline_keyboard: list[list[Any]] | None = None,
    message_id: int = 777,
    text: str | None = "Shopping list",
    answer: AsyncMock | None = None,
) -> SimpleNamespace:
    """Build a fake callback_query update mirroring PTB's effective_* resolution.

    For a callback_query, PTB fills ``effective_message``/``effective_chat``/
    ``effective_user`` from the tapped message and the tapper, so the adapter
    reuses ``_conversation_facts`` on it exactly as for an inbound message.
    """
    reply_markup = (
        SimpleNamespace(inline_keyboard=inline_keyboard) if inline_keyboard is not None else None
    )
    message = SimpleNamespace(
        message_id=message_id,
        message_thread_id=None,
        is_topic_message=False,
        text=text,
        caption=None,
        reply_to_message=None,
        reply_markup=reply_markup,
        chat=SimpleNamespace(id=chat_id),
    )
    callback = SimpleNamespace(
        id=callback_id,
        data=data,
        from_user=SimpleNamespace(id=user_id, full_name="Tapper", username="tap"),
        message=message,
        answer=answer or AsyncMock(),
    )
    return SimpleNamespace(
        callback_query=callback,
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id, full_name="Tapper", username="tap"),
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


def make_command_dispatcher(
    *,
    result: CommandOutcome | None = None,
    argument: str | None = None,
    execution_mode: str | None = None,
    unavailable: CommandUnavailability | None = None,
) -> SimpleNamespace:
    prepared = (
        PreparedCommand(
            name=result.command,
            argument=argument,
            execution_mode=cast(
                Any,
                execution_mode
                or ("immediate" if result.command in {"help", "status", "stop"} else "serialized"),
            ),
        )
        if result is not None
        else None
    )
    return SimpleNamespace(
        prepare=Mock(side_effect=lambda text: prepared if text.strip().startswith("/") else None),
        unavailability=Mock(return_value=unavailable),
        execute=AsyncMock(return_value=result),
    )


def make_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dm_scope: str = "per_conversation",
    allowed_chat_ids: list[int] | None = None,
    response_mode: str = "mention",
    mention_patterns: list[str] | None = None,
    admin_user_ids: list[str] | None = None,
    observe_unaddressed: bool = False,
    bot_username: str | None = None,
    bot_display_name: str | None = None,
    bot_id: int | None = None,
    trigger_run: AsyncMock | None = None,
    compact_session: AsyncMock | None = None,
    credential_resolver: Callable[[str], str] | None = None,
    attachment_store: AttachmentStore | None = None,
    command_dispatcher: object | None = None,
    chat_migration_persister: Callable[[str, str], None] | None = None,
    update_offset_store: Any | None = None,
    set_process_token: bool = True,
) -> tuple[TelegramChannelAdapter, ChatSessionManager, AsyncMock, SimpleNamespace]:
    # Keep unit tests fast while preserving the production behavior: the task still
    # yields once, so a following forwarded-media handler can claim the pending text.
    monkeypatch.setattr(telegram_module, "_FORWARD_COMMENT_SETTLE_SECONDS", 0)
    if set_process_token:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", "test-token")
    else:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)

    chat_sessions = ChatSessionManager(tmp_path)
    trigger_mock = trigger_run or AsyncMock()

    async def trigger_with_admission(*args: Any, **kwargs: Any) -> Any:
        kwargs.pop("waiting_work_admission", None)
        return await trigger_mock(*args, **kwargs)

    trigger_service = SimpleNamespace(
        trigger_run=trigger_with_admission,
        compact_session=compact_session or AsyncMock(return_value="Context compacted."),
        # Synchronous like the real one (a bool, not a coroutine); idle by default.
        has_active_run=Mock(return_value=False),
        reserve_waiting_work=Mock(
            return_value=WaitingWorkAdmission(id="test-admission", scope="test:chat")
        ),
        release_waiting_work=Mock(return_value=True),
    )
    resolved_command_dispatcher = command_dispatcher or make_command_dispatcher()
    if hasattr(resolved_command_dispatcher, "chat_sessions"):
        resolved_command_dispatcher.chat_sessions = chat_sessions

    adapter = TelegramChannelAdapter(
        make_config(
            dm_scope=dm_scope,
            allowed_chat_ids=allowed_chat_ids,
            response_mode=response_mode,
            mention_patterns=mention_patterns,
            observe_unaddressed=observe_unaddressed,
        ),
        cast(Any, trigger_service),
        cast(Any, chat_sessions),
        credential_resolver or (lambda key: os.environ.get(key, "")),
        attachment_store=attachment_store,
        command_dispatcher=cast(Any, resolved_command_dispatcher),
        chat_migration_persister=chat_migration_persister,
        access_registry=MemoryChannelAccessRegistry(list(admin_user_ids or [])),
        update_offset_store=update_offset_store,
    )
    if bot_username is not None or bot_display_name is not None or bot_id is not None:
        adapter._set_bot_identity(
            SimpleNamespace(
                id=bot_id,
                username=bot_username,
                full_name=bot_display_name,
            )
        )

    bot = SimpleNamespace(
        send_message=AsyncMock(),
        send_photo=AsyncMock(),
        send_document=AsyncMock(),
        send_media_group=AsyncMock(),
        send_chat_action=AsyncMock(),
        get_file=AsyncMock(),
        answer_callback_query=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    adapter._application = SimpleNamespace(
        bot=bot,
        updater=None,
        stop=AsyncMock(),
        shutdown=AsyncMock(),
    )
    return adapter, chat_sessions, trigger_mock, bot


async def drain_chat_queue(adapter: TelegramChannelAdapter, chat_id: int) -> None:
    pending_flushes = list(adapter._forward_comment_tasks.values())
    if pending_flushes:
        await asyncio.gather(*pending_flushes)
    queue = adapter._engine._chat_queues.get(str(chat_id))
    if queue is None:
        await asyncio.sleep(0)
        return
    # Generous timeout: under xdist load a worker's first lazy import of the real
    # telegram package can eat well over a second before the queue drains.
    await asyncio.wait_for(queue.join(), timeout=5)


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

    class FakeInlineKeyboardButton:
        def __init__(self, *, text: str, callback_data: str) -> None:
            self.text = text
            self.callback_data = callback_data

    class FakeInlineKeyboardMarkup:
        def __init__(self, inline_keyboard: list[list[Any]]) -> None:
            self.inline_keyboard = inline_keyboard

    fake_telegram = SimpleNamespace(
        InputFile=FakeInputFile,
        InputMediaPhoto=FakeInputMediaPhoto,
        InputMediaDocument=FakeInputMediaDocument,
        ReplyParameters=FakeReplyParameters,
        InlineKeyboardButton=FakeInlineKeyboardButton,
        InlineKeyboardMarkup=FakeInlineKeyboardMarkup,
    )
    monkeypatch.setattr(telegram_module, "_load_telegram", lambda: fake_telegram)


def make_group_update(
    *,
    chat_id: int = -10001,
    user_id: int = 50,
    text: str | None = "hello",
    message_id: int | None = None,
    reply_to_user_id: int | None = None,
    reply_to_message: object | None = None,
    message_thread_id: int | None = None,
    is_topic_message: bool = False,
) -> SimpleNamespace:
    resolved_reply = reply_to_message
    if resolved_reply is None and reply_to_user_id is not None:
        resolved_reply = SimpleNamespace(from_user=SimpleNamespace(id=reply_to_user_id))
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(
            text=text,
            message_thread_id=message_thread_id,
            is_topic_message=is_topic_message,
            message_id=message_id,
            reply_to_message=resolved_reply,
        ),
    )
