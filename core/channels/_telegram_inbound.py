"""Telegram album and forwarding-comment coalescing."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

from core.channels.adapter import (
    ConversationFacts,
)
from core.utils.logging import get_logger

from ._telegram_messages import _parse_message_id

_LOGGER = get_logger("channels.telegram")
_ALBUM_FLUSH_SECONDS = 0.5
_FORWARD_COMMENT_SETTLE_SECONDS = 0.5


@dataclass(slots=True, frozen=True)
class _PendingForwardComment:
    conversation: ConversationFacts
    text: str
    message_id: int
    raw_message: Any


class TelegramInboundBuffer:
    """Coalesce albums and adjacent forwarding comments before dispatching one turn."""

    def __init__(
        self,
        channel_id: str,
        text_handler: Callable[..., Awaitable[None]],
        media_handler: Callable[..., Awaitable[None]],
    ) -> None:
        self._channel_id = channel_id
        self._text_handler = text_handler
        self._media_handler = media_handler
        self._album_buffers: dict[str, list[Any]] = {}
        self._album_conversations: dict[str, ConversationFacts] = {}
        self._album_companion_texts: dict[str, str] = {}
        self._album_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_forward_comments: dict[str, _PendingForwardComment] = {}
        self._forward_comment_tasks: dict[str, asyncio.Task[None]] = {}

    def _buffer_album_message(
        self,
        album_id: str,
        conversation: ConversationFacts,
        message: Any,
        *,
        companion_text: str | None = None,
    ) -> None:
        existing_messages = self._album_buffers.get(album_id)
        if existing_messages is not None:
            existing_messages.append(message)
            # A username/name address or reply-to-bot on any item addresses the whole album.
            buffered_conversation = self._album_conversations[album_id]
            self._album_conversations[album_id] = replace(
                buffered_conversation,
                mentioned_bot=buffered_conversation.mentioned_bot or conversation.mentioned_bot,
                is_reply_to_bot=(
                    buffered_conversation.is_reply_to_bot or conversation.is_reply_to_bot
                ),
            )
        else:
            self._album_buffers[album_id] = [message]
            self._album_conversations[album_id] = conversation
        if companion_text is not None:
            self._album_companion_texts[album_id] = companion_text
        self._restart_album_flush(album_id)

    def _restart_album_flush(self, album_id: str) -> None:
        # The flush window counts from the last buffered item, so slow album delivery
        # does not split one album into multiple Runs.
        existing_task = self._album_tasks.get(album_id)
        if existing_task is not None:
            existing_task.cancel()

        task = asyncio.create_task(
            self._flush_album(album_id),
            name=f"telegram:{self._channel_id}:album:{album_id}",
        )
        self._album_tasks[album_id] = task
        task.add_done_callback(partial(self._on_album_task_done, album_id))

    async def _flush_album(self, album_id: str) -> None:
        await asyncio.sleep(_ALBUM_FLUSH_SECONDS)

        messages = self._album_buffers.pop(album_id, [])
        conversation = self._album_conversations.pop(album_id, None)
        companion_text = self._album_companion_texts.pop(album_id, None)
        if not messages or conversation is None:
            return

        await self._media_handler(
            conversation,
            tuple(messages),
            companion_text=companion_text,
        )

    def _on_album_task_done(self, album_id: str, task: asyncio.Task[None]) -> None:
        if self._album_tasks.get(album_id) is task:
            self._album_tasks.pop(album_id, None)

        if task.cancelled():
            return

        error = task.exception()
        if error is None:
            return

        # Losing an entire inbound album is not "expected"; carry the traceback so the
        # dropped media is diagnosable beyond str(error).
        _LOGGER.warning(
            "Telegram album flush failed (channel=%s album=%s): %s",
            self._channel_id,
            album_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )

    async def _buffer_possible_forward_comment(
        self,
        conversation: ConversationFacts,
        message_text: str,
        raw_message: Any,
    ) -> None:
        message_id = _parse_message_id(conversation.message_id)
        if message_id is None:
            await self._text_handler(
                conversation,
                message_text,
                raw_message=raw_message,
            )
            return

        await self._flush_pending_forward_comment(conversation.chat_id)
        self._pending_forward_comments[conversation.chat_id] = _PendingForwardComment(
            conversation=conversation,
            text=message_text,
            message_id=message_id,
            raw_message=raw_message,
        )
        task = asyncio.create_task(
            self._flush_forward_comment_after_delay(conversation.chat_id),
            name=f"telegram:{self._channel_id}:forward-comment:{conversation.chat_id}",
        )
        self._forward_comment_tasks[conversation.chat_id] = task
        task.add_done_callback(partial(self._on_forward_comment_task_done, conversation.chat_id))

    async def _take_forward_comment_for_media(
        self,
        conversation: ConversationFacts,
        message: Any,
    ) -> tuple[str | None, ConversationFacts]:
        pending = self._pending_forward_comments.get(conversation.chat_id)
        if pending is None:
            return None, conversation

        message_id = _parse_message_id(conversation.message_id)
        is_matching_forward = (
            getattr(message, "forward_origin", None) is not None
            and message_id == pending.message_id + 1
            and conversation.user_id == pending.conversation.user_id
            and conversation.thread_id == pending.conversation.thread_id
        )
        if not is_matching_forward:
            await self._flush_pending_forward_comment(conversation.chat_id)
            return None, conversation

        self._pending_forward_comments.pop(conversation.chat_id, None)
        self._cancel_forward_comment_task(conversation.chat_id)
        merged_conversation = replace(
            conversation,
            mentioned_bot=(conversation.mentioned_bot or pending.conversation.mentioned_bot),
            is_reply_to_bot=(conversation.is_reply_to_bot or pending.conversation.is_reply_to_bot),
        )
        return pending.text, merged_conversation

    async def _flush_forward_comment_after_delay(self, chat_id: str) -> None:
        await asyncio.sleep(_FORWARD_COMMENT_SETTLE_SECONDS)
        await self._flush_pending_forward_comment(chat_id)

    async def _flush_pending_forward_comment(self, chat_id: str) -> None:
        pending = self._pending_forward_comments.pop(chat_id, None)
        self._cancel_forward_comment_task(chat_id)
        if pending is not None:
            await self._text_handler(
                pending.conversation,
                pending.text,
                raw_message=pending.raw_message,
            )

    def _cancel_forward_comment_task(self, chat_id: str) -> None:
        task = self._forward_comment_tasks.pop(chat_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _on_forward_comment_task_done(self, chat_id: str, task: asyncio.Task[None]) -> None:
        if self._forward_comment_tasks.get(chat_id) is task:
            self._forward_comment_tasks.pop(chat_id, None)

        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        _LOGGER.warning(
            "Telegram forward-comment flush failed (channel=%s target=%s): %s",
            self._channel_id,
            chat_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )

    async def stop(self) -> None:
        album_tasks = list(self._album_tasks.values())
        forward_comment_tasks = list(self._forward_comment_tasks.values())
        self._album_tasks.clear()
        self._album_buffers.clear()
        self._album_conversations.clear()
        self._album_companion_texts.clear()
        self._forward_comment_tasks.clear()
        self._pending_forward_comments.clear()
        for task in (*album_tasks, *forward_comment_tasks):
            task.cancel()

        background_tasks = [*album_tasks, *forward_comment_tasks]
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
