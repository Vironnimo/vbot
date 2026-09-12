"""Telegram outbound delivery, media conversion and typing activity."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

from core.attachments import AttachmentStore
from core.channels.adapter import (
    FileData,
    QuotedMessageFacts,
    content_blocks_for_attachment,
)
from core.channels.config import ChannelConfigError, ChannelError
from core.chat.content_blocks import ContentBlock, MediaBlock, TextBlock
from core.extensions import (
    InteractionButton,
)
from core.utils.logging import get_logger
from core.utils.retry import retry_async

from ._telegram_api import _buttons_to_markup, _load_telegram, _telegram_error_boundary
from ._telegram_messages import (
    TELEGRAM_MESSAGE_LIMIT,
    _default_animation_filename,
    _default_audio_filename,
    _default_document_filename,
    _default_photo_filename,
    _default_video_filename,
    _default_video_note_filename,
    _default_voice_filename,
    _extract_caption,
    _is_image_media_type,
    _normalize_optional_message,
    _parse_platform_target,
    _parse_thread_id,
    _telegram_message_author,
    _telegram_message_has_media,
    _utf16_length,
    split_telegram_message,
)

_LOGGER = get_logger("channels.telegram")
TELEGRAM_CAPTION_LIMIT = 1024
_TYPING_ACTION = "typing"
_TYPING_REFRESH_SECONDS = 4.0
_INBOUND_MEDIA_RETRY_INITIAL_SECONDS = 1.0


class TelegramTransport:
    """Own Telegram delivery and attachment conversion using the active bot client."""

    platform_display_name = "Telegram"

    def __init__(
        self, channel_id: str, bot: Callable[[], Any], attachment_store: AttachmentStore | None
    ) -> None:
        self._channel_id = channel_id
        self._require_bot = bot
        self._attachment_store = attachment_store
        self.bot_id: int | None = None

    async def send(
        self,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
        thread_id: str | None = None,
        buttons: list[list[InteractionButton]] | None = None,
    ) -> None:
        """Send one outbound message and/or file payloads to Telegram.

        ``buttons`` attaches an inline keyboard to the message (the final text
        chunk). A keyboard cannot ride on a media group, so combining ``buttons``
        with ``files`` is rejected rather than silently dropping the keyboard.
        """
        bot = self._require_bot()
        chat_id = _parse_platform_target(platform_target)
        message_thread_id = _parse_thread_id(thread_id)
        normalized_message = _normalize_optional_message(message)
        normalized_files = list(files or [])
        reply_markup = _buttons_to_markup(buttons) if buttons else None

        if normalized_files:
            if reply_markup is not None:
                raise ChannelConfigError(
                    "interactive buttons cannot be combined with file attachments"
                )
            with _telegram_error_boundary(self._channel_id):
                await self._send_with_files(
                    bot,
                    chat_id,
                    normalized_message,
                    normalized_files,
                    message_thread_id=message_thread_id,
                )
            return

        if normalized_message is None:
            raise ChannelConfigError("at least one of message or files must be provided")

        with _telegram_error_boundary(self._channel_id):
            await self._send_text_chunks(
                bot,
                chat_id,
                normalized_message,
                message_thread_id=message_thread_id,
                reply_markup=reply_markup,
            )

    async def _send_text_chunks(
        self,
        bot: Any,
        chat_id: int,
        message: str,
        *,
        message_thread_id: int | None = None,
        reply_markup: Any = None,
        reply_parameters: Any = None,
    ) -> None:
        # A keyboard belongs on the final visible message, so it rides only on the
        # last chunk of a split reply.
        chunks = split_telegram_message(message, TELEGRAM_MESSAGE_LIMIT)
        last_index = len(chunks) - 1
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if message_thread_id is not None:
                payload["message_thread_id"] = message_thread_id
            if reply_markup is not None and index == last_index:
                payload["reply_markup"] = reply_markup
            if reply_parameters is not None and index == 0:
                payload["reply_parameters"] = reply_parameters

            async def send_chunk(payload: dict[str, Any] = payload) -> None:
                with _telegram_error_boundary(self._channel_id):
                    await bot.send_message(**payload)

            try:
                await retry_async(send_chunk)
            except ChannelError as error:
                # This chunk exhausted its retries. Retrying the whole transport
                # call would duplicate all earlier, acknowledged chunks.
                error.retryable = False
                raise

    async def _send_with_files(
        self,
        bot: Any,
        chat_id: int,
        message: str | None,
        files: list[FileData],
        *,
        message_thread_id: int | None = None,
    ) -> None:
        # Telegram caps file captions at TELEGRAM_CAPTION_LIMIT UTF-16 units, far below the
        # 4096 text limit. A caption that fits rides along on the first file; a longer message
        # is delivered as standalone text first (so nothing is dropped) and the files go out
        # uncaptioned.
        if message is not None and _utf16_length(message) > TELEGRAM_CAPTION_LIMIT:
            await self._send_text_chunks(bot, chat_id, message, message_thread_id=message_thread_id)
            await self._send_files(
                bot, chat_id, files, caption=None, message_thread_id=message_thread_id
            )
            return
        await self._send_files(
            bot, chat_id, files, caption=message, message_thread_id=message_thread_id
        )

    async def _send_files(
        self,
        bot: Any,
        chat_id: int,
        files: list[FileData],
        *,
        caption: str | None,
        message_thread_id: int | None = None,
    ) -> None:
        if not files:
            return

        await self._send_file_batch(
            bot, chat_id, files, caption=caption, message_thread_id=message_thread_id
        )

    async def _send_single_file(
        self,
        bot: Any,
        chat_id: int,
        file_data: FileData,
        *,
        caption: str | None,
        message_thread_id: int | None = None,
    ) -> None:
        telegram = _load_telegram()
        input_file = telegram.InputFile(file_data.data, filename=file_data.filename)

        if _is_image_media_type(file_data.media_type):
            payload: dict[str, Any] = {"chat_id": chat_id, "photo": input_file}
            if caption is not None:
                payload["caption"] = caption
            if message_thread_id is not None:
                payload["message_thread_id"] = message_thread_id
            await bot.send_photo(**payload)
            return

        payload = {"chat_id": chat_id, "document": input_file}
        if caption is not None:
            payload["caption"] = caption
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        await bot.send_document(**payload)

    async def _send_file_batch(
        self,
        bot: Any,
        chat_id: int,
        files: list[FileData],
        *,
        caption: str | None,
        message_thread_id: int | None = None,
    ) -> None:
        image_files: list[FileData] = []
        doc_files: list[FileData] = []
        for file_data in files:
            if _is_image_media_type(file_data.media_type):
                image_files.append(file_data)
            else:
                doc_files.append(file_data)

        caption_pending = caption
        for partition, is_image in ((image_files, True), (doc_files, False)):
            if not partition:
                continue

            for start in range(0, len(partition), 10):
                batch = partition[start : start + 10]
                await self._send_homogeneous_batch(
                    bot,
                    chat_id,
                    batch,
                    caption=caption_pending,
                    is_image=is_image,
                    message_thread_id=message_thread_id,
                )
                caption_pending = None

    async def _send_homogeneous_batch(
        self,
        bot: Any,
        chat_id: int,
        files: list[FileData],
        *,
        caption: str | None,
        is_image: bool,
        message_thread_id: int | None = None,
    ) -> None:
        if not files:
            return
        if len(files) == 1:
            await self._send_single_file(
                bot, chat_id, files[0], caption=caption, message_thread_id=message_thread_id
            )
            return

        telegram = _load_telegram()
        media_items: list[Any] = []

        for index, file_data in enumerate(files):
            item_caption = caption if index == 0 else None
            input_file = telegram.InputFile(file_data.data, filename=file_data.filename)
            if is_image:
                media_items.append(telegram.InputMediaPhoto(media=input_file, caption=item_caption))
            else:
                media_items.append(
                    telegram.InputMediaDocument(media=input_file, caption=item_caption)
                )

        payload: dict[str, Any] = {"chat_id": chat_id, "media": media_items}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        await bot.send_media_group(**payload)

    async def send_text(
        self,
        platform_target: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        """Deliver one outbound text reply (engine transport callback)."""
        reply_parameters = self._build_reply_parameters(reply_to_message_id)
        if reply_parameters is None:
            await self.send(text, platform_target, thread_id=thread_id)
            return

        bot = self._require_bot()
        chat_id = _parse_platform_target(platform_target)
        message_thread_id = _parse_thread_id(thread_id)
        normalized_message = _normalize_optional_message(text)
        if normalized_message is None:
            raise ChannelConfigError("at least one of message or files must be provided")

        with _telegram_error_boundary(self._channel_id):
            await self._send_text_chunks(
                bot,
                chat_id,
                normalized_message,
                message_thread_id=message_thread_id,
                reply_parameters=reply_parameters,
            )

    def _build_reply_parameters(self, reply_to_message_id: str | None) -> Any | None:
        if reply_to_message_id is None:
            return None
        try:
            message_id = int(reply_to_message_id)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "Ignoring non-integer reply target message id (channel=%s): %r",
                self._channel_id,
                reply_to_message_id,
            )
            return None
        telegram = _load_telegram()
        # allow_sending_without_reply keeps the reply deliverable when the original
        # message was deleted in the meantime.
        return telegram.ReplyParameters(message_id=message_id, allow_sending_without_reply=True)

    def caption_text(self, raw_message: Any) -> str | None:
        """Expose the Telegram caption for engine-side gating checks."""
        return _extract_caption(raw_message)

    def activity_indicator(
        self,
        platform_target: str,
        thread_id: str | None = None,
    ) -> contextlib.AbstractAsyncContextManager[None]:
        """Telegram typing indicator as the engine's activity-indicator callback."""
        return self._typing_indicator(platform_target, thread_id)

    async def build_media_blocks(self, raw_message: Any) -> list[ContentBlock]:
        """Convert one raw Telegram message into canonical content blocks."""
        blocks: list[ContentBlock] = []

        caption = _extract_caption(raw_message)
        if caption is not None:
            blocks.append(TextBlock(type="text", text=caption))

        photo_items = getattr(raw_message, "photo", None)
        if isinstance(photo_items, (list, tuple)) and photo_items:
            largest_photo = photo_items[-1]
            file_id = getattr(largest_photo, "file_id", None)
            if not isinstance(file_id, str) or not file_id.strip():
                return blocks

            file_unique_id = getattr(largest_photo, "file_unique_id", None)
            filename = _default_photo_filename(file_unique_id)
            record = await self._store_inbound_attachment(file_id=file_id, filename=filename)
            blocks.append(
                MediaBlock(
                    type="media",
                    attachment_id=record.id,
                    filename=record.filename,
                    media_type=record.media_type,
                )
            )
            return blocks

        audio_video_block = await self._build_audio_video_block(raw_message)
        if audio_video_block is not None:
            blocks.append(audio_video_block)
            return blocks

        document = getattr(raw_message, "document", None)
        if document is None:
            return blocks

        file_id = getattr(document, "file_id", None)
        if not isinstance(file_id, str) or not file_id.strip():
            return blocks

        filename = _default_document_filename(document)
        record = await self._store_inbound_attachment(file_id=file_id, filename=filename)
        # A document is classified by its sniffed type, not by Telegram's delivery channel:
        # an MP3/MP4 sent as a "file" becomes a media attachment like any voice/video message.
        blocks.extend(content_blocks_for_attachment(record))
        return blocks

    async def build_quoted_message(self, raw_message: Any) -> QuotedMessageFacts | None:
        """Download attachment content from the Telegram message being replied to."""
        replied_message = getattr(raw_message, "reply_to_message", None)
        if replied_message is None:
            return None

        author = _telegram_message_author(replied_message)
        if author is None:
            return QuotedMessageFacts(
                user_id=None,
                user_display_name=None,
                content=None,
            )
        user_id, display_name = author
        if self.bot_id is not None and user_id == str(self.bot_id):
            return None
        if not _telegram_message_has_media(replied_message):
            return None

        blocks = await self.build_media_blocks(replied_message)
        if not any(not isinstance(block, TextBlock) for block in blocks):
            return QuotedMessageFacts(
                user_id=user_id,
                user_display_name=display_name,
                content=None,
            )
        return QuotedMessageFacts(
            user_id=user_id,
            user_display_name=display_name,
            content=blocks,
        )

    async def _build_audio_video_block(self, message: Any) -> MediaBlock | None:
        """Store one voice/audio/video/video-note/animation payload and return its block.

        Animations must be resolved here, before the document fallback: Telegram sets a
        backward-compat ``document`` field on animation messages, and the animation's own
        metadata (filename, unique id) is the better source.
        """
        media_sources: tuple[tuple[str, Any], ...] = (
            ("voice", _default_voice_filename),
            ("audio", _default_audio_filename),
            ("video", _default_video_filename),
            ("video_note", _default_video_note_filename),
            ("animation", _default_animation_filename),
        )
        for attribute_name, default_filename_builder in media_sources:
            media_object = getattr(message, attribute_name, None)
            if media_object is None:
                continue

            file_id = getattr(media_object, "file_id", None)
            if not isinstance(file_id, str) or not file_id.strip():
                return None

            filename = default_filename_builder(media_object)
            record = await self._store_inbound_attachment(file_id=file_id, filename=filename)
            return MediaBlock(
                type="media",
                attachment_id=record.id,
                filename=record.filename,
                media_type=record.media_type,
            )
        return None

    async def _store_inbound_attachment(self, *, file_id: str, filename: str) -> Any:
        attachment_store = self._attachment_store
        if attachment_store is None:
            raise ChannelError("Attachment store is not configured for Telegram channels")

        payload = await retry_async(
            self._download_inbound_attachment,
            file_id,
            attachment_store,
            initial_delay=_INBOUND_MEDIA_RETRY_INITIAL_SECONDS,
        )
        return attachment_store.store(filename, bytes(payload))

    async def _download_inbound_attachment(
        self,
        file_id: str,
        attachment_store: AttachmentStore,
    ) -> bytearray:
        """Fetch one Telegram file, translating transient API faults for retry."""
        bot = self._require_bot()
        # get_file fetches only metadata (size + path), not the body; checking the reported
        # size here refuses an oversized file before download_as_bytearray pulls it into
        # memory. store() re-checks as a backstop for when Telegram omits the size.
        with _telegram_error_boundary(self._channel_id):
            telegram_file = await bot.get_file(file_id)
        reported_size = getattr(telegram_file, "file_size", None)
        attachment_store.ensure_within_limit(
            reported_size if isinstance(reported_size, int) else None
        )

        # The pre-check already bounded this to <= the store limit, so converting the
        # downloaded bytearray to immutable bytes copies only validated, in-limit data.
        with _telegram_error_boundary(self._channel_id):
            return cast(bytearray, await telegram_file.download_as_bytearray())

    @contextlib.asynccontextmanager
    async def _typing_indicator(
        self, platform_target: str, thread_id: str | None = None
    ) -> AsyncIterator[None]:
        """Show Telegram's "typing" indicator for the chat until the block exits."""
        task = asyncio.create_task(
            self._keep_typing(platform_target, thread_id),
            name=f"telegram:{self._channel_id}:typing:{platform_target}",
        )
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _keep_typing(self, platform_target: str, thread_id: str | None = None) -> None:
        try:
            bot = self._require_bot()
            chat_id = _parse_platform_target(platform_target)
            message_thread_id = _parse_thread_id(thread_id)
        except (ChannelError, ChannelConfigError):
            return

        payload: dict[str, Any] = {"chat_id": chat_id, "action": _TYPING_ACTION}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        while True:
            try:
                await bot.send_chat_action(**payload)
            except Exception as error:
                # Best-effort cosmetic indicator: stop quietly if the API call fails.
                _LOGGER.debug(
                    "Telegram typing indicator stopped (channel=%s target=%s): %s",
                    self._channel_id,
                    platform_target,
                    error,
                )
                return
            await asyncio.sleep(_TYPING_REFRESH_SECONDS)
