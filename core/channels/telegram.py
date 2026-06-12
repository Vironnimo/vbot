"""Telegram channel adapter implementation."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from functools import partial
from importlib import import_module
from typing import TYPE_CHECKING, Any, TypeGuard

from core.attachments import AttachmentStore
from core.channels.adapter import (
    ChannelAdapter,
    ConversationFacts,
    FileData,
    ReplyPlanFacts,
    RouteFacts,
)
from core.channels.channels import ChannelConfig, ChannelConfigError, ChannelError
from core.channels.engine import ChannelConversationEngine
from core.chat.content_blocks import ContentBlock, FileBlock, MediaBlock, TextBlock
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.chat.commands import CommandDispatcher
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("channels.telegram")

TELEGRAM_MESSAGE_LIMIT = 4096
# Album items arrive as separate updates; the flush window restarts with each new item.
_ALBUM_FLUSH_SECONDS = 0.5
# Telegram chat actions expire after ~5 s, so the indicator is refreshed on a shorter cycle.
_TYPING_ACTION = "typing"
_TYPING_REFRESH_SECONDS = 4.0
_UNSUPPORTED_MESSAGE_TYPE_REPLY = "Sorry, this message type isn't supported yet."


class TelegramChannelAdapter(ChannelAdapter):
    """Telegram long-polling adapter for bidirectional channel messaging."""

    platform = "telegram"
    platform_display_name = "Telegram"

    def __init__(
        self,
        config: ChannelConfig,
        trigger_service: TriggerService,
        chat_sessions: ChatSessionManager,
        credential_resolver: Callable[[str], str],
        attachment_store: AttachmentStore | None = None,
        *,
        command_dispatcher: CommandDispatcher,
    ) -> None:
        self._config = config
        self._attachment_store = attachment_store
        self._engine = ChannelConversationEngine(
            config,
            trigger_service,
            chat_sessions,
            self,
            command_dispatcher=command_dispatcher,
        )

        token = credential_resolver(config.token_env_var)
        if not isinstance(token, str) or not token.strip():
            raise ChannelConfigError(
                f"Missing Telegram token in environment variable: {config.token_env_var}"
            )
        self._token = token.strip()

        self._application: Any | None = None
        self._stop_event = asyncio.Event()
        self._allowed_chat_ids = frozenset(config.allowed_chat_ids)
        self._album_buffers: dict[str, list[Any]] = {}
        self._album_routes: dict[str, RouteFacts] = {}
        self._album_reply_plans: dict[str, ReplyPlanFacts] = {}
        self._album_conversations: dict[str, ConversationFacts] = {}
        self._album_tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        """Start Telegram long-polling and wait until stop is requested."""
        if self._application is not None:
            await self._stop_event.wait()
            return

        telegram_ext = _load_telegram_ext()
        application = telegram_ext.Application.builder().token(self._token).build()
        for handler in self._build_message_handlers(telegram_ext):
            application.add_handler(handler)
        self._application = application
        self._stop_event.clear()

        await application.initialize()
        await application.bot.delete_webhook(drop_pending_updates=False)
        await application.start()

        updater = application.updater
        if updater is None:
            raise ChannelError("Telegram updater is unavailable")

        await updater.start_polling()
        _LOGGER.info("Telegram adapter started (channel=%s)", self._config.id)
        await self._stop_event.wait()

    def _build_message_handlers(self, telegram_ext: Any) -> list[Any]:
        # UpdateType.MESSAGE restricts handlers to new messages: edited messages must not
        # trigger new Runs, and channel posts are out of scope for chat routing.
        new_messages_only = telegram_ext.filters.UpdateType.MESSAGE
        media_message_types = (
            telegram_ext.filters.PHOTO
            | telegram_ext.filters.Document.ALL
            | telegram_ext.filters.VOICE
            | telegram_ext.filters.AUDIO
            | telegram_ext.filters.VIDEO
            | telegram_ext.filters.VIDEO_NOTE
        )
        # Animations carry a backward-compat `document` field and normally hit the media
        # handler first; the ANIMATION filter here only catches them if Telegram ever
        # stops setting that field.
        unsupported_message_types = (
            telegram_ext.filters.ANIMATION | telegram_ext.filters.Sticker.ALL
        )
        return [
            telegram_ext.MessageHandler(
                telegram_ext.filters.TEXT & new_messages_only,
                self._handle_inbound_message,
            ),
            telegram_ext.MessageHandler(
                media_message_types & new_messages_only,
                self._handle_inbound_media,
            ),
            telegram_ext.MessageHandler(
                unsupported_message_types & new_messages_only,
                self._handle_unsupported_message_type,
            ),
        ]

    async def stop(self) -> None:
        """Stop polling, cancel engine workers and album tasks, and release resources."""
        self._stop_event.set()
        await self._stop_workers()

        application = self._application
        self._application = None
        if application is None:
            return

        updater = application.updater
        if updater is not None:
            await self._run_lifecycle_step(updater.stop, "updater.stop")
        await self._run_lifecycle_step(application.stop, "application.stop")
        await self._run_lifecycle_step(application.shutdown, "application.shutdown")

    async def send(
        self,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
    ) -> None:
        """Send one outbound message and/or file payloads to Telegram."""
        bot = self._require_bot()
        chat_id = _parse_platform_target(platform_target)
        normalized_message = _normalize_optional_message(message)
        normalized_files = list(files or [])

        if normalized_files:
            await self._send_files(bot, chat_id, normalized_files, caption=normalized_message)
            return

        if normalized_message is None:
            raise ChannelConfigError("at least one of message or files must be provided")

        for chunk in split_telegram_message(normalized_message, TELEGRAM_MESSAGE_LIMIT):
            await bot.send_message(chat_id=chat_id, text=chunk)

    async def _send_files(
        self,
        bot: Any,
        chat_id: int,
        files: list[FileData],
        *,
        caption: str | None,
    ) -> None:
        if not files:
            return

        await self._send_file_batch(bot, chat_id, files, caption=caption)

    async def _send_single_file(
        self,
        bot: Any,
        chat_id: int,
        file_data: FileData,
        *,
        caption: str | None,
    ) -> None:
        telegram = _load_telegram()
        input_file = telegram.InputFile(file_data.data, filename=file_data.filename)

        if _is_image_media_type(file_data.media_type):
            payload: dict[str, Any] = {"chat_id": chat_id, "photo": input_file}
            if caption is not None:
                payload["caption"] = caption
            await bot.send_photo(**payload)
            return

        payload = {"chat_id": chat_id, "document": input_file}
        if caption is not None:
            payload["caption"] = caption
        await bot.send_document(**payload)

    async def _send_file_batch(
        self,
        bot: Any,
        chat_id: int,
        files: list[FileData],
        *,
        caption: str | None,
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
    ) -> None:
        if not files:
            return
        if len(files) == 1:
            await self._send_single_file(bot, chat_id, files[0], caption=caption)
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

        await bot.send_media_group(chat_id=chat_id, media=media_items)

    # -- ConversationTransport ------------------------------------------------------------

    async def send_text(self, platform_target: str, text: str) -> None:
        """Deliver one outbound text reply (engine transport callback)."""
        await self.send(text, platform_target)

    def activity_indicator(
        self, platform_target: str
    ) -> contextlib.AbstractAsyncContextManager[None]:
        """Telegram typing indicator as the engine's activity-indicator callback."""
        return self._typing_indicator(platform_target)

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
        if record.media_type.startswith("image/"):
            blocks.append(
                MediaBlock(
                    type="media",
                    attachment_id=record.id,
                    filename=record.filename,
                    media_type=record.media_type,
                )
            )
            return blocks

        if record.media_type.startswith("text/"):
            blocks.append(TextBlock(type="text", text=record.text_content or ""))
            return blocks

        blocks.append(
            FileBlock(
                type="file",
                attachment_id=record.id,
                filename=record.filename,
                media_type=record.media_type,
            )
        )
        return blocks

    async def _build_audio_video_block(self, message: Any) -> MediaBlock | None:
        """Store one voice/audio/video/video-note payload and return its media block."""
        media_sources: tuple[tuple[str, Any], ...] = (
            ("voice", _default_voice_filename),
            ("audio", _default_audio_filename),
            ("video", _default_video_filename),
            ("video_note", _default_video_note_filename),
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

        bot = self._require_bot()
        telegram_file = await bot.get_file(file_id)
        payload = await telegram_file.download_as_bytearray()
        return attachment_store.store(filename, bytes(payload))

    # -- Inbound handlers -----------------------------------------------------------------

    async def _handle_inbound_message(
        self,
        update: Any,
        _context: Any,
    ) -> None:
        conversation = self._conversation_facts(update)
        if conversation is None:
            return

        if not self._is_chat_allowed(int(conversation.chat_id)):
            return

        message_text = _extract_message_text(update)
        if message_text is None:
            return

        await self._engine.handle_inbound_text(conversation, message_text)

    async def _handle_inbound_media(self, update: Any, _context: Any) -> None:
        conversation = self._conversation_facts(update)
        if conversation is None:
            return

        if not self._is_chat_allowed(int(conversation.chat_id)):
            return

        message = getattr(update, "effective_message", None)
        if message is None:
            return

        route, reply_plan = self._engine.prepare_inbound_route(conversation)
        media_group_id = getattr(message, "media_group_id", None)
        if media_group_id is not None:
            self._buffer_album_message(
                str(media_group_id), route, reply_plan, conversation, message
            )
            return

        self._engine.enqueue_media(route, reply_plan, (message,), conversation=conversation)

    async def _handle_unsupported_message_type(self, update: Any, _context: Any) -> None:
        """Reply to allowed chats that this message type cannot be processed yet."""
        conversation = self._conversation_facts(update)
        if conversation is None:
            return

        if not self._is_chat_allowed(int(conversation.chat_id)):
            return

        await self.send(_UNSUPPORTED_MESSAGE_TYPE_REPLY, conversation.chat_id)

    def ensure_outbound_session(self, platform_target: str) -> RouteFacts:
        """Ensure the Session mirroring an outbound Telegram chat exists with channel context."""
        return self._engine.ensure_channel_session(
            self._conversation_facts_for_target(platform_target)
        )

    def _conversation_facts_for_target(self, platform_target: str) -> ConversationFacts:
        chat_id = _parse_platform_target(platform_target)
        # Telegram private chats use chat_id == user_id, and group chats (negative ids) ignore
        # dm_scope, so the chat id alone determines the routed session for a proactive send.
        return ConversationFacts(
            platform=self.platform,
            channel_id=self._config.id,
            chat_id=str(chat_id),
            user_id=str(chat_id),
            thread_id=None,
            kind="group" if chat_id < 0 else "direct",
        )

    # -- Album buffering ------------------------------------------------------------------

    def _buffer_album_message(
        self,
        album_id: str,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        conversation: ConversationFacts,
        message: Any,
    ) -> None:
        existing_messages = self._album_buffers.get(album_id)
        if existing_messages is not None:
            existing_messages.append(message)
        else:
            self._album_buffers[album_id] = [message]
            self._album_routes[album_id] = route
            self._album_reply_plans[album_id] = reply_plan
            self._album_conversations[album_id] = conversation
        self._restart_album_flush(album_id)

    def _restart_album_flush(self, album_id: str) -> None:
        # The flush window counts from the last buffered item, so slow album delivery
        # does not split one album into multiple Runs.
        existing_task = self._album_tasks.get(album_id)
        if existing_task is not None:
            existing_task.cancel()

        task = asyncio.create_task(
            self._flush_album(album_id),
            name=f"telegram:{self._config.id}:album:{album_id}",
        )
        self._album_tasks[album_id] = task
        task.add_done_callback(partial(self._on_album_task_done, album_id))

    async def _flush_album(self, album_id: str) -> None:
        await asyncio.sleep(_ALBUM_FLUSH_SECONDS)

        messages = self._album_buffers.pop(album_id, [])
        route = self._album_routes.pop(album_id, None)
        reply_plan = self._album_reply_plans.pop(album_id, None)
        conversation = self._album_conversations.pop(album_id, None)
        if not messages or route is None or reply_plan is None or conversation is None:
            return

        self._engine.enqueue_media(route, reply_plan, tuple(messages), conversation=conversation)

    def _on_album_task_done(self, album_id: str, task: asyncio.Task[None]) -> None:
        if self._album_tasks.get(album_id) is task:
            self._album_tasks.pop(album_id, None)

        if task.cancelled():
            return

        error = task.exception()
        if error is None:
            return

        _LOGGER.warning(
            "Telegram album flush failed (channel=%s album=%s): %s",
            self._config.id,
            album_id,
            error,
        )

    # -- Update parsing -------------------------------------------------------------------

    def _conversation_facts(self, update: Any) -> ConversationFacts | None:
        message = getattr(update, "effective_message", None)
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        if message is None or chat is None or user is None:
            return None

        chat_id = getattr(chat, "id", None)
        user_id = getattr(user, "id", None)
        if not (_is_integer(chat_id) and _is_integer(user_id)):
            return None

        thread_id_raw = getattr(message, "message_thread_id", None)
        thread_id = str(thread_id_raw) if thread_id_raw is not None else None

        return ConversationFacts(
            platform=self.platform,
            channel_id=self._config.id,
            chat_id=str(chat_id),
            user_id=str(user_id),
            thread_id=thread_id,
            # Telegram group chats are identified by negative chat ids.
            kind="group" if chat_id < 0 else "direct",
            user_display_name=_user_display_name(user),
        )

    def _is_chat_allowed(self, chat_id: int) -> bool:
        # D8: empty allowed_chat_ids means deny all inbound chats.
        return chat_id in self._allowed_chat_ids

    # -- Typing indicator -----------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _typing_indicator(self, platform_target: str) -> AsyncIterator[None]:
        """Show Telegram's "typing" indicator for the chat until the block exits."""
        task = asyncio.create_task(
            self._keep_typing(platform_target),
            name=f"telegram:{self._config.id}:typing:{platform_target}",
        )
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _keep_typing(self, platform_target: str) -> None:
        try:
            bot = self._require_bot()
            chat_id = _parse_platform_target(platform_target)
        except (ChannelError, ChannelConfigError):
            return

        while True:
            try:
                await bot.send_chat_action(chat_id=chat_id, action=_TYPING_ACTION)
            except Exception as error:
                # Best-effort cosmetic indicator: stop quietly if the API call fails.
                _LOGGER.debug(
                    "Telegram typing indicator stopped (channel=%s target=%s): %s",
                    self._config.id,
                    platform_target,
                    error,
                )
                return
            await asyncio.sleep(_TYPING_REFRESH_SECONDS)

    # -- Lifecycle helpers ----------------------------------------------------------------

    async def _stop_workers(self) -> None:
        album_tasks = list(self._album_tasks.values())
        self._album_tasks.clear()
        self._album_buffers.clear()
        self._album_routes.clear()
        self._album_reply_plans.clear()
        self._album_conversations.clear()
        for task in album_tasks:
            task.cancel()

        await self._engine.stop()

        if album_tasks:
            await asyncio.gather(*album_tasks, return_exceptions=True)

    async def _run_lifecycle_step(self, operation: Any, label: str) -> None:
        try:
            await operation()
        except RuntimeError:
            return
        except Exception as error:
            _LOGGER.warning(
                "Telegram adapter lifecycle step failed (%s channel=%s): %s",
                label,
                self._config.id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _require_bot(self) -> Any:
        application = self._application
        if application is None:
            raise ChannelError(f"Telegram channel is not running: {self._config.id}")
        return application.bot


def split_telegram_message(message: str, max_chars: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split one message into Telegram-size chunks."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not message:
        return []
    return [message[start : start + max_chars] for start in range(0, len(message), max_chars)]


def _normalize_optional_message(message: str | None) -> str | None:
    if message is None:
        return None
    if not isinstance(message, str) or not message.strip():
        raise ChannelConfigError("message must be a non-empty string when provided")
    return message.strip()


def _extract_message_text(update: Any) -> str | None:
    message = getattr(update, "effective_message", None)
    text = getattr(message, "text", None)
    if not isinstance(text, str):
        return None
    if not text.strip():
        return None
    return text


def _user_display_name(user: Any) -> str | None:
    # full_name is derived from first_name (Bot-API-mandatory) + optional last_name;
    # username is optional and unset for many accounts.
    full_name = getattr(user, "full_name", None)
    if isinstance(full_name, str) and full_name.strip():
        return full_name.strip()

    username = getattr(user, "username", None)
    if isinstance(username, str) and username.strip():
        return username.strip()
    return None


def _extract_caption(message: Any) -> str | None:
    caption = getattr(message, "caption", None)
    if not isinstance(caption, str):
        return None
    caption = caption.strip()
    return caption or None


def _default_photo_filename(file_unique_id: object) -> str:
    if isinstance(file_unique_id, str) and file_unique_id.strip():
        return f"telegram-photo-{file_unique_id.strip()}.jpg"
    return "telegram-photo.jpg"


def _default_document_filename(document: Any) -> str:
    filename = getattr(document, "file_name", None)
    if isinstance(filename, str) and filename.strip():
        return filename.strip()

    file_unique_id = getattr(document, "file_unique_id", None)
    if isinstance(file_unique_id, str) and file_unique_id.strip():
        return f"telegram-document-{file_unique_id.strip()}"
    return "telegram-document"


def _media_filename(media_object: Any, prefix: str, extension: str) -> str:
    filename = getattr(media_object, "file_name", None)
    if isinstance(filename, str) and filename.strip():
        return filename.strip()

    file_unique_id = getattr(media_object, "file_unique_id", None)
    if isinstance(file_unique_id, str) and file_unique_id.strip():
        return f"{prefix}-{file_unique_id.strip()}{extension}"
    return f"{prefix}{extension}"


def _default_voice_filename(voice: Any) -> str:
    return _media_filename(voice, "telegram-voice", ".ogg")


def _default_audio_filename(audio: Any) -> str:
    return _media_filename(audio, "telegram-audio", "")


def _default_video_filename(video: Any) -> str:
    return _media_filename(video, "telegram-video", ".mp4")


def _default_video_note_filename(video_note: Any) -> str:
    return _media_filename(video_note, "telegram-video-note", ".mp4")


def _is_image_media_type(media_type: str) -> bool:
    return isinstance(media_type, str) and media_type.startswith("image/")


def _parse_platform_target(platform_target: str) -> int:
    try:
        chat_id = int(platform_target)
    except (TypeError, ValueError) as error:
        raise ChannelConfigError("platform_target must be an integer chat id") from error
    return chat_id


def _load_telegram_ext() -> Any:
    try:
        return import_module("telegram.ext")
    except ModuleNotFoundError as error:
        raise ChannelError(
            "python-telegram-bot is required for Telegram channels; install server dependencies"
        ) from error


def _load_telegram() -> Any:
    try:
        return import_module("telegram")
    except ModuleNotFoundError as error:
        raise ChannelError(
            "python-telegram-bot is required for Telegram channels; install server dependencies"
        ) from error


def _is_integer(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = ["TELEGRAM_MESSAGE_LIMIT", "TelegramChannelAdapter", "split_telegram_message"]
