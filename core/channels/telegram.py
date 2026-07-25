"""Telegram channel adapter implementation."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, replace
from functools import partial
from importlib import import_module
from typing import TYPE_CHECKING, Any, TypeGuard

from core.attachments import AttachmentStore
from core.channels.adapter import (
    ChannelAdapter,
    ConversationFacts,
    DeniedChatFacts,
    DeniedChatLog,
    FileData,
    RouteFacts,
    RunButtonBindingRegistry,
    content_blocks_for_attachment,
)
from core.channels.channels import ChannelConfig, ChannelConfigError, ChannelError
from core.channels.engine import ChannelConversationEngine
from core.chat.content_blocks import ContentBlock, MediaBlock, TextBlock
from core.extensions import (
    RUN_TRIGGER_PREFIX,
    InteractionButton,
    InteractionEvent,
    InteractionResponder,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.chat.commands import CommandDispatcher
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("channels.telegram")

TELEGRAM_MESSAGE_LIMIT = 4096
# File captions are capped far below the text limit. Both limits count UTF-16 code units.
TELEGRAM_CAPTION_LIMIT = 1024
# Album items arrive as separate updates; the flush window restarts with each new item.
_ALBUM_FLUSH_SECONDS = 0.5
# Telegram sends a comment entered while forwarding as a separate message immediately
# before the forwarded item. Hold eligible text briefly so the two transport updates can
# become the single user turn shown by the Telegram client.
_FORWARD_COMMENT_SETTLE_SECONDS = 0.5
# Telegram chat actions expire after ~5 s, so the indicator is refreshed on a shorter cycle.
_TYPING_ACTION = "typing"
_TYPING_REFRESH_SECONDS = 4.0
_UNSUPPORTED_MESSAGE_TYPE_REPLY = "Sorry, this message type isn't supported yet."
# Telegram's first-contact ritual: every user's first DM to a bot is the /start command.
# It is translated into an internal note-driven Run so the agent greets in its own
# voice instead of the model receiving a literal "/start" user message.
_START_GREETING_PROMPT = (
    "The user has just opened this chat with Telegram's /start command. "
    "Greet them briefly in your own voice and let them know how you can help."
)
_CHAT_MIGRATED_REPLY = (
    "This group was upgraded by Telegram and has a new chat id. "
    "I've updated my configuration; the conversation continues here."
)
_INTERACTION_ALREADY_HANDLED_REPLY = "This action was already handled."
_INTERACTION_UNAVAILABLE_REPLY = "This action is no longer available."
# Retries for a send that Telegram answers with RetryAfter (flood control), honoring the
# server-provided delay — the project convention of max 3 retries for transient errors.
_SEND_MAX_RETRIES = 3


@dataclass(slots=True, frozen=True)
class _PendingForwardComment:
    conversation: ConversationFacts
    text: str
    message_id: int


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
        chat_migration_persister: Callable[[str, str], None] | None = None,
        interaction_dispatcher: (
            Callable[[InteractionEvent, InteractionResponder], Awaitable[bool]] | None
        ) = None,
        run_button_binding_registry: RunButtonBindingRegistry | None = None,
    ) -> None:
        self._config = config
        self._attachment_store = attachment_store
        # Persists a group→supergroup chat-id swap into the channel config
        # (ChannelService wires its storage update); None keeps the swap runtime-only.
        self._chat_migration_persister = chat_migration_persister
        # Routes a button tap to the extension registered for its callback prefix.
        # Reads the live extension registry (bound method), so an extension
        # reload/disable needs no channel re-wiring — the next tap uses the current
        # registry. None means taps are always acknowledged but never dispatched.
        self._interaction_dispatcher = interaction_dispatcher
        self._engine = ChannelConversationEngine(
            config,
            trigger_service,
            chat_sessions,
            self,
            command_dispatcher=command_dispatcher,
            run_button_binding_registry=run_button_binding_registry,
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
        self._denied_chat_log = DeniedChatLog()
        self._bot_id: int | None = None
        self._bot_username: str | None = None
        self._bot_mention_pattern: re.Pattern[str] | None = None
        self._album_buffers: dict[str, list[Any]] = {}
        self._album_conversations: dict[str, ConversationFacts] = {}
        self._album_companion_texts: dict[str, str] = {}
        self._album_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_forward_comments: dict[str, _PendingForwardComment] = {}
        self._forward_comment_tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        """Start Telegram long-polling and wait until stop is requested."""
        if self._application is not None:
            await self._stop_event.wait()
            return

        telegram_ext = _load_telegram_ext()
        application = self._build_application(telegram_ext)
        for handler in self._build_message_handlers(telegram_ext):
            application.add_handler(handler)
        self._application = application
        self._stop_event.clear()

        await application.initialize()
        # The bot's own identity feeds the addressing facts (@mention detection,
        # reply-to-bot checks, /cmd@botname suffix parsing) for group gating.
        bot_user = await application.bot.get_me()
        self._set_bot_identity(bot_user)
        await application.bot.delete_webhook(drop_pending_updates=False)
        await application.start()

        updater = application.updater
        if updater is None:
            raise ChannelError("Telegram updater is unavailable")

        await updater.start_polling()
        _LOGGER.info("Telegram adapter started (channel=%s)", self._config.id)
        await self._stop_event.wait()

    def _build_application(self, telegram_ext: Any) -> Any:
        # AIORateLimiter paces outbound calls against Telegram's flood limits (~30 msg/s
        # overall, 20 msg/min per group) so multi-chunk replies and media groups do not
        # trip flood control, and retries a send Telegram answers with RetryAfter. Without
        # it a rate-limit error mid-reply loses the remaining chunks.
        rate_limiter = telegram_ext.AIORateLimiter(max_retries=_SEND_MAX_RETRIES)
        return (
            telegram_ext.Application.builder().token(self._token).rate_limiter(rate_limiter).build()
        )

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
            | telegram_ext.filters.ANIMATION
        )
        # Location (incl. venue), contact, and poll messages are rendered as bracketed
        # text for the model instead of being dropped.
        structured_message_types = (
            telegram_ext.filters.LOCATION | telegram_ext.filters.CONTACT | telegram_ext.filters.POLL
        )
        # Everything else that is a real user message (stickers, dice, games, ...) gets
        # the polite unsupported-type reply instead of silence. Status updates (member
        # joined, pinned message, ...) are service noise, not user messages, and stay
        # excluded — migration status updates have their own handler above.
        unsupported_message_types = ~(
            telegram_ext.filters.TEXT
            | media_message_types
            | structured_message_types
            | telegram_ext.filters.StatusUpdate.ALL
        )
        return [
            # Migration service messages first: a group→supergroup upgrade changes the
            # chat id in place and would otherwise silently kill the allowlist match.
            telegram_ext.MessageHandler(
                telegram_ext.filters.StatusUpdate.MIGRATE & new_messages_only,
                self._handle_chat_migration,
            ),
            telegram_ext.MessageHandler(
                telegram_ext.filters.TEXT & new_messages_only,
                self._handle_inbound_message,
            ),
            telegram_ext.MessageHandler(
                media_message_types & new_messages_only,
                self._handle_inbound_media,
            ),
            telegram_ext.MessageHandler(
                structured_message_types & new_messages_only,
                self._handle_inbound_structured_message,
            ),
            telegram_ext.MessageHandler(
                unsupported_message_types & new_messages_only,
                self._handle_unsupported_message_type,
            ),
            # A button tap is a distinct update type (callback_query), not a message,
            # so this handler is additive and never wrapped with UpdateType.MESSAGE.
            telegram_ext.CallbackQueryHandler(self._handle_callback_query),
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
            with _telegram_error_boundary(self._config.id):
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

        with _telegram_error_boundary(self._config.id):
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
            await bot.send_message(**payload)

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

    # -- ConversationTransport ------------------------------------------------------------

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

        with _telegram_error_boundary(self._config.id):
            # Only the first chunk references the replied-to message; every chunk
            # carries the topic, so a multi-part reply stays in the forum topic
            # instead of falling into the General topic.
            for index, chunk in enumerate(
                split_telegram_message(normalized_message, TELEGRAM_MESSAGE_LIMIT)
            ):
                payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
                if message_thread_id is not None:
                    payload["message_thread_id"] = message_thread_id
                if index == 0:
                    payload["reply_parameters"] = reply_parameters
                await bot.send_message(**payload)

    def _build_reply_parameters(self, reply_to_message_id: str | None) -> Any | None:
        if reply_to_message_id is None:
            return None
        try:
            message_id = int(reply_to_message_id)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "Ignoring non-integer reply target message id (channel=%s): %r",
                self._config.id,
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

        bot = self._require_bot()
        # get_file fetches only metadata (size + path), not the body; checking the reported
        # size here refuses an oversized file before download_as_bytearray pulls it into
        # memory. store() re-checks as a backstop for when Telegram omits the size.
        telegram_file = await bot.get_file(file_id)
        reported_size = getattr(telegram_file, "file_size", None)
        attachment_store.ensure_within_limit(
            reported_size if isinstance(reported_size, int) else None
        )

        # The pre-check already bounded this to <= the store limit, so converting the
        # downloaded bytearray to immutable bytes copies only validated, in-limit data.
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

        if not self._is_chat_allowed(conversation.chat_id):
            self._record_denied_inbound(conversation, update)
            return

        message_text = _extract_message_text(update)
        if message_text is None:
            return

        message_text = self._strip_bot_command_suffix(message_text)

        # /start is Telegram's first-contact ritual in private chats; groups keep the
        # normal command/gating path (where an unknown /start is simply not addressed).
        if conversation.kind == "direct":
            start_payload = _parse_start_command(message_text)
            if start_payload is not None:
                await self._flush_pending_forward_comment(conversation.chat_id)
                await self._engine.trigger_internal_reply(
                    conversation, _start_greeting_prompt(start_payload)
                )
                return

        # Commands must retain their immediate semantics. Normal text is held
        # for one short settle window because Telegram represents a comment entered in
        # the forwarding UI as a plain message immediately before the forwarded media.
        has_message_id = _parse_message_id(conversation.message_id) is not None
        if (
            has_message_id
            and message_text.startswith("/")
            and self._engine.is_command(message_text)
        ):
            await self._flush_pending_forward_comment(conversation.chat_id)
            await self._engine.handle_inbound_text(conversation, message_text)
            return

        await self._buffer_possible_forward_comment(conversation, message_text)

    def _strip_bot_command_suffix(self, text: str) -> str:
        # Telegram group clients send commands as `/cmd@botusername`. Strip the suffix
        # only when it addresses this bot; `/cmd@otherbot` stays unchanged and is never
        # treated as our command.
        username = self._bot_username
        if not username or not text.startswith("/"):
            return text

        first_token, separator, remainder = text.partition(" ")
        command, at_sign, suffix = first_token.partition("@")
        if not at_sign or suffix.casefold() != username.casefold():
            return text
        return command + separator + remainder

    async def _handle_inbound_media(self, update: Any, _context: Any) -> None:
        conversation = self._conversation_facts(update)
        if conversation is None:
            return

        if not self._is_chat_allowed(conversation.chat_id):
            self._record_denied_inbound(conversation, update)
            return

        message = getattr(update, "effective_message", None)
        if message is None:
            return

        companion_text, conversation = await self._take_forward_comment_for_media(
            conversation, message
        )
        media_group_id = getattr(message, "media_group_id", None)
        if media_group_id is not None:
            self._buffer_album_message(
                str(media_group_id),
                conversation,
                message,
                companion_text=companion_text,
            )
            return

        await self._engine.handle_inbound_media(
            conversation,
            (message,),
            companion_text=companion_text,
        )

    async def _handle_inbound_structured_message(self, update: Any, _context: Any) -> None:
        """Route a location/contact/poll message as rendered text into the engine."""
        conversation = self._conversation_facts(update)
        if conversation is None:
            return

        if not self._is_chat_allowed(conversation.chat_id):
            self._record_denied_inbound(conversation, update)
            return

        message = getattr(update, "effective_message", None)
        if message is None:
            return

        rendered_text = _render_structured_message(message)
        if rendered_text is None:
            return

        await self._flush_pending_forward_comment(conversation.chat_id)
        await self._engine.handle_inbound_text(conversation, rendered_text)

    async def _handle_chat_migration(self, update: Any, _context: Any) -> None:
        """Adopt the new chat id when Telegram upgrades a group to a supergroup.

        The upgrade changes the chat id in place; without this the allowlist stops
        matching and the bot silently goes dead in that chat. The allowlist swap is
        applied to the running adapter, persisted into the channel config, and the
        conversation anchor is bridged so the session history continues seamlessly.
        """
        message = getattr(update, "effective_message", None)
        chat = getattr(update, "effective_chat", None)
        if message is None or chat is None:
            return
        chat_id = getattr(chat, "id", None)
        if not _is_integer(chat_id):
            return

        migrate_to = getattr(message, "migrate_to_chat_id", None)
        migrate_from = getattr(message, "migrate_from_chat_id", None)
        if _is_integer(migrate_to):
            old_chat_id, new_chat_id = str(chat_id), str(migrate_to)
        elif _is_integer(migrate_from):
            old_chat_id, new_chat_id = str(migrate_from), str(chat_id)
        else:
            return

        # Authorization: only an allowlisted old chat carries its allowance over.
        # Telegram announces the migration twice (a service message in the old chat
        # and one in the new); the first one processed swaps the allowlist and the
        # second finds the old id gone and is a no-op.
        if old_chat_id not in self._allowed_chat_ids:
            return

        self._allowed_chat_ids = frozenset(
            new_chat_id if allowed == old_chat_id else allowed for allowed in self._allowed_chat_ids
        )
        _LOGGER.info(
            "Telegram chat migrated to supergroup (channel=%s old=%s new=%s)",
            self._config.id,
            old_chat_id,
            new_chat_id,
        )

        persister = self._chat_migration_persister
        if persister is not None:
            try:
                persister(old_chat_id, new_chat_id)
            except Exception as error:
                _LOGGER.error(
                    "Cannot persist migrated chat id (channel=%s old=%s new=%s): %s",
                    self._config.id,
                    old_chat_id,
                    new_chat_id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

        try:
            await self._engine.migrate_group_conversation(old_chat_id, new_chat_id)
        except Exception as error:
            _LOGGER.error(
                "Cannot bridge migrated chat conversation (channel=%s old=%s new=%s): %s",
                self._config.id,
                old_chat_id,
                new_chat_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

        try:
            await self.send(_CHAT_MIGRATED_REPLY, new_chat_id)
        except ChannelError as error:
            # Best-effort courtesy note; the migration itself already succeeded.
            _LOGGER.warning(
                "Cannot confirm chat migration in new chat (channel=%s new=%s): %s",
                self._config.id,
                new_chat_id,
                error,
            )

    async def _handle_unsupported_message_type(self, update: Any, _context: Any) -> None:
        """Reply to allowed chats that this message type cannot be processed yet."""
        conversation = self._conversation_facts(update)
        if conversation is None:
            return

        if not self._is_chat_allowed(conversation.chat_id):
            self._record_denied_inbound(conversation, update)
            return

        # Unaddressed group messages are dropped, so an unsupported-type reply would be
        # spam (e.g. for every sticker in a group). Same gating decision as real media.
        if not self._engine.should_respond(conversation):
            return

        await self._flush_pending_forward_comment(conversation.chat_id)
        # Same reply semantics as engine replies: group replies reference the message
        # they answer, and the reply follows the message into its forum topic.
        await self.send_text(
            conversation.chat_id,
            _UNSUPPORTED_MESSAGE_TYPE_REPLY,
            reply_to_message_id=(conversation.message_id if conversation.kind == "group" else None),
            thread_id=conversation.thread_id,
        )

    async def _handle_callback_query(self, update: Any, _context: Any) -> None:
        """Turn a Telegram button tap (callback_query) into a dispatched interaction.

        Extension-owned taps stay deterministic and in-process. The reserved
        ``run:`` prefix instead enters the conversation engine and its per-chat FIFO
        because it deliberately wakes the agent. Identity and the allowlist gate
        reuse the same inbound plumbing as messages. Every tap is acknowledged
        exactly once — by the Run path, extension handler, or fallback here — so the
        tapper's spinner always stops.
        """
        callback = getattr(update, "callback_query", None)
        if callback is None:
            return

        data = getattr(callback, "data", None)
        message = getattr(callback, "message", None)
        conversation = self._conversation_facts(update)
        if (
            not isinstance(data, str)
            or not data
            or message is None
            or conversation is None
            or conversation.message_id is None
        ):
            await self._best_effort_ack(callback)
            return

        if not self._is_chat_allowed(conversation.chat_id):
            self._record_denied_inbound(conversation, update)
            await self._best_effort_ack(callback)
            return

        responder = _TelegramInteractionResponder(
            self._require_bot(),
            callback_id=str(getattr(callback, "id", "")),
            chat_id=int(conversation.chat_id),
            message_id=int(conversation.message_id),
            channel_id=self._config.id,
        )
        inline_keyboard = getattr(getattr(message, "reply_markup", None), "inline_keyboard", None)
        text_value = getattr(message, "text", None)
        event = InteractionEvent(
            platform=self.platform,
            channel_id=self._config.id,
            chat_id=conversation.chat_id,
            user_id=conversation.user_id,
            message_id=conversation.message_id,
            data=data,
            buttons=_markup_to_buttons(inline_keyboard),
            text=text_value if isinstance(text_value, str) else None,
            user_display_name=conversation.user_display_name,
            thread_id=conversation.thread_id,
        )

        if data.split(":", 1)[0] == RUN_TRIGGER_PREFIX:
            # A reserved-prefix tap wakes the agent instead of an extension. Bound
            # buttons first claim their durable origin and repoint the conversation;
            # legacy buttons keep the current Channel route. Only accepted or terminal
            # taps close the keyboard, while busy/denied taps remain retryable.
            try:
                outcome = await self._engine.trigger_interaction_reply(conversation, event)
            except Exception as error:
                _LOGGER.error(
                    "Telegram Run-button handling failed (channel=%s): %s",
                    self._config.id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
                with contextlib.suppress(ChannelError):
                    await responder.answer(_INTERACTION_UNAVAILABLE_REPLY, alert=True)
                return

            answer_text = None
            answer_alert = False
            close_keyboard = outcome == "enqueued"
            if outcome == "already_handled":
                answer_text = _INTERACTION_ALREADY_HANDLED_REPLY
                close_keyboard = True
            elif outcome == "unavailable":
                answer_text = _INTERACTION_UNAVAILABLE_REPLY
                answer_alert = True
                close_keyboard = True
            with contextlib.suppress(ChannelError):
                await responder.answer(answer_text, alert=answer_alert)
            if close_keyboard:
                with contextlib.suppress(ChannelError):
                    await responder.edit(buttons=[])
            return

        if self._interaction_dispatcher is not None:
            await self._interaction_dispatcher(event, responder)
        if not responder.answered:
            with contextlib.suppress(ChannelError):
                await responder.answer()

    async def _best_effort_ack(self, callback: Any) -> None:
        """Silently acknowledge a tap; a Bot API failure here is logged, never fatal."""
        try:
            await callback.answer()
        except Exception as error:
            _LOGGER.debug(
                "Telegram callback ack failed (channel=%s): %s",
                self._config.id,
                error,
            )

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
        conversation: ConversationFacts,
        message: Any,
        *,
        companion_text: str | None = None,
    ) -> None:
        existing_messages = self._album_buffers.get(album_id)
        if existing_messages is not None:
            existing_messages.append(message)
            # An @mention or reply-to-bot on any album item addresses the whole album.
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
            name=f"telegram:{self._config.id}:album:{album_id}",
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

        await self._engine.handle_inbound_media(
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
            self._config.id,
            album_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )

    # -- Forward-comment buffering -------------------------------------------------------

    async def _buffer_possible_forward_comment(
        self,
        conversation: ConversationFacts,
        message_text: str,
    ) -> None:
        message_id = _parse_message_id(conversation.message_id)
        if message_id is None:
            await self._engine.handle_inbound_text(conversation, message_text)
            return

        await self._flush_pending_forward_comment(conversation.chat_id)
        self._pending_forward_comments[conversation.chat_id] = _PendingForwardComment(
            conversation=conversation,
            text=message_text,
            message_id=message_id,
        )
        task = asyncio.create_task(
            self._flush_forward_comment_after_delay(conversation.chat_id),
            name=f"telegram:{self._config.id}:forward-comment:{conversation.chat_id}",
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
            await self._engine.handle_inbound_text(pending.conversation, pending.text)

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
            self._config.id,
            chat_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
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

        # message_thread_id is only a topic when is_topic_message is set: in non-forum
        # supergroups Telegram also fills it for plain reply threads, and sending a
        # message_thread_id back into a non-forum group fails with "thread not found".
        thread_id_raw = getattr(message, "message_thread_id", None)
        is_topic_message = bool(getattr(message, "is_topic_message", False))
        thread_id = str(thread_id_raw) if is_topic_message and _is_integer(thread_id_raw) else None

        message_id_raw = getattr(message, "message_id", None)
        message_id = str(message_id_raw) if _is_integer(message_id_raw) else None

        return ConversationFacts(
            platform=self.platform,
            channel_id=self._config.id,
            chat_id=str(chat_id),
            user_id=str(user_id),
            thread_id=thread_id,
            # Telegram group chats are identified by negative chat ids.
            kind="group" if chat_id < 0 else "direct",
            user_display_name=_user_display_name(user),
            message_id=message_id,
            mentioned_bot=self._mentions_bot(message),
            is_reply_to_bot=self._is_reply_to_bot(message),
        )

    def _set_bot_identity(self, bot_user: Any) -> None:
        bot_id = getattr(bot_user, "id", None)
        self._bot_id = bot_id if _is_integer(bot_id) else None

        username = getattr(bot_user, "username", None)
        if isinstance(username, str) and username.strip():
            self._bot_username = username.strip()
            self._bot_mention_pattern = re.compile(
                rf"@{re.escape(self._bot_username)}\b", re.IGNORECASE
            )
        else:
            self._bot_username = None
            self._bot_mention_pattern = None

    def _mentions_bot(self, message: Any) -> bool:
        # Word-boundary regex over text and caption instead of entity offsets: Telegram
        # entity offsets are UTF-16 code units, and the regex catches the same practical
        # @botusername mentions without that conversion.
        pattern = self._bot_mention_pattern
        if pattern is None:
            return False
        for attribute_name in ("text", "caption"):
            value = getattr(message, attribute_name, None)
            if isinstance(value, str) and pattern.search(value):
                return True
        return False

    def _is_reply_to_bot(self, message: Any) -> bool:
        if self._bot_id is None:
            return False
        replied_message = getattr(message, "reply_to_message", None)
        if replied_message is None:
            return False
        replied_user = getattr(replied_message, "from_user", None)
        replied_user_id = getattr(replied_user, "id", None)
        return _is_integer(replied_user_id) and replied_user_id == self._bot_id

    def _is_chat_allowed(self, chat_id: str) -> bool:
        # D8: empty allowed_chat_ids means deny all inbound chats.
        return chat_id in self._allowed_chat_ids

    def denied_chats(self) -> list[DeniedChatFacts]:
        return self._denied_chat_log.entries()

    def _record_denied_inbound(self, conversation: ConversationFacts, update: Any) -> None:
        """Record an allowlist-denied inbound message for status/discovery surfaces.

        The first denial per chat logs at info so operators can find the chat id
        without any tooling; repeats stay at debug to keep a chatty denied chat
        from flooding the log.
        """
        display_name = self._denied_chat_display_name(conversation, update)
        is_new_chat = self._denied_chat_log.record(
            chat_id=conversation.chat_id,
            kind=conversation.kind,
            display_name=display_name,
        )
        log = _LOGGER.info if is_new_chat else _LOGGER.debug
        log(
            "Inbound Telegram message from chat not in allowlist "
            "(channel=%s chat=%s kind=%s name=%s); chat id recorded in channel status",
            self._config.id,
            conversation.chat_id,
            conversation.kind,
            display_name or "unknown",
        )

    def _denied_chat_display_name(
        self,
        conversation: ConversationFacts,
        update: Any,
    ) -> str | None:
        if conversation.kind == "group":
            title = getattr(getattr(update, "effective_chat", None), "title", None)
            if isinstance(title, str) and title.strip():
                return title.strip()
            return None
        return conversation.user_display_name

    # -- Typing indicator -----------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _typing_indicator(
        self, platform_target: str, thread_id: str | None = None
    ) -> AsyncIterator[None]:
        """Show Telegram's "typing" indicator for the chat until the block exits."""
        task = asyncio.create_task(
            self._keep_typing(platform_target, thread_id),
            name=f"telegram:{self._config.id}:typing:{platform_target}",
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
                    self._config.id,
                    platform_target,
                    error,
                )
                return
            await asyncio.sleep(_TYPING_REFRESH_SECONDS)

    # -- Lifecycle helpers ----------------------------------------------------------------

    async def _stop_workers(self) -> None:
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

        await self._engine.stop()

        background_tasks = [*album_tasks, *forward_comment_tasks]
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)

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


class _TelegramInteractionResponder:
    """Concrete :class:`InteractionResponder` for one Telegram ``callback_query``.

    Owns the reply channel for a single tap: acknowledge it (stop the tapper's
    spinner) and optionally edit the tapped message's text and/or keyboard. Bot
    API calls are wrapped in the adapter's :func:`_telegram_error_boundary`, so a
    failed edit or ack surfaces as a ``ChannelError`` rather than an unwrapped
    PTB error. ``answered`` lets the adapter guarantee a single fallback ack.
    """

    def __init__(
        self,
        bot: Any,
        *,
        callback_id: str,
        chat_id: int,
        message_id: int,
        channel_id: str,
    ) -> None:
        self._bot = bot
        self._callback_id = callback_id
        self._chat_id = chat_id
        self._message_id = message_id
        self._channel_id = channel_id
        self.answered = False

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        with _telegram_error_boundary(self._channel_id):
            await self._bot.answer_callback_query(
                callback_query_id=self._callback_id, text=text, show_alert=alert
            )
        self.answered = True

    async def edit(
        self,
        *,
        text: str | None = None,
        buttons: list[list[InteractionButton]] | None = None,
    ) -> None:
        # An empty keyboard means "remove the inline keyboard": Telegram clears it
        # only for reply_markup=None, not for an empty InlineKeyboardMarkup.
        markup = _buttons_to_markup(buttons) if buttons else None
        with _telegram_error_boundary(self._channel_id):
            if text is not None:
                await self._bot.edit_message_text(
                    text=text,
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                    reply_markup=markup,
                )
            elif buttons is not None:
                await self._bot.edit_message_reply_markup(
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                    reply_markup=markup,
                )


def _buttons_to_markup(rows: list[list[InteractionButton]]) -> Any:
    """Render neutral button rows into a PTB ``InlineKeyboardMarkup``."""
    telegram = _load_telegram()
    keyboard = [
        [
            telegram.InlineKeyboardButton(text=button.label, callback_data=button.data)
            for button in row
        ]
        for row in rows
    ]
    return telegram.InlineKeyboardMarkup(keyboard)


def _markup_to_buttons(inline_keyboard: Any) -> tuple[tuple[InteractionButton, ...], ...]:
    """Read a PTB ``inline_keyboard`` (rows of buttons) into neutral button rows.

    Returns an empty tuple when the message carries no keyboard. Each button's
    ``.text`` becomes the neutral label and its ``.callback_data`` the neutral
    data; a button missing either is skipped (only tappable buttons round-trip).
    """
    if not inline_keyboard:
        return ()
    rows: list[tuple[InteractionButton, ...]] = []
    for row in inline_keyboard:
        buttons: list[InteractionButton] = []
        for button in row:
            label = getattr(button, "text", None)
            data = getattr(button, "callback_data", None)
            if isinstance(label, str) and isinstance(data, str):
                buttons.append(InteractionButton(label=label, data=data))
        rows.append(tuple(buttons))
    return tuple(rows)


def split_telegram_message(message: str, max_chars: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split one message into Telegram-size chunks measured in UTF-16 code units.

    Telegram counts its length limits in UTF-16 code units, not Unicode code points, so an
    astral-plane character (most emoji) counts as two. Splitting on Python's code-point
    slicing would let an emoji-heavy chunk exceed the wire limit and fail with BadRequest,
    so chunk boundaries are placed by UTF-16 length and never inside a character.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not message:
        return []

    chunks: list[str] = []
    chunk_start = 0
    chunk_units = 0
    for index, character in enumerate(message):
        units = _utf16_units(character)
        if chunk_units + units > max_chars and index > chunk_start:
            chunks.append(message[chunk_start:index])
            chunk_start = index
            chunk_units = 0
        chunk_units += units
    chunks.append(message[chunk_start:])
    return chunks


def _utf16_units(character: str) -> int:
    # Astral-plane characters encode as a UTF-16 surrogate pair (2 units); everything in
    # the Basic Multilingual Plane is a single unit. Telegram counts in these units.
    return 2 if ord(character) > 0xFFFF else 1


def _utf16_length(text: str) -> int:
    return sum(_utf16_units(character) for character in text)


def _normalize_optional_message(message: str | None) -> str | None:
    if message is None:
        return None
    if not isinstance(message, str) or not message.strip():
        raise ChannelConfigError("message must be a non-empty string when provided")
    return message.strip()


def _parse_start_command(text: str) -> str | None:
    """Return the /start deep-link payload ("" when bare), or None for other text."""
    stripped = text.strip()
    if stripped == "/start":
        return ""
    if stripped.startswith("/start "):
        return stripped[len("/start ") :].strip()
    return None


def _start_greeting_prompt(start_payload: str) -> str:
    if not start_payload:
        return _START_GREETING_PROMPT
    # Deep links (t.me/<bot>?start=<payload>) deliver a parameter worth surfacing.
    return f'{_START_GREETING_PROMPT} They arrived with the start parameter "{start_payload}".'


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


def _render_structured_message(message: Any) -> str | None:
    """Render a location/contact/poll payload as bracketed text for the model.

    These message types carry structured data instead of downloadable media; a compact
    text rendering keeps their content usable in the conversation without a separate
    content-block type.
    """
    venue = getattr(message, "venue", None)
    if venue is not None:
        return _render_venue(venue)
    location = getattr(message, "location", None)
    if location is not None:
        return _render_location(location)
    contact = getattr(message, "contact", None)
    if contact is not None:
        return _render_contact(contact)
    poll = getattr(message, "poll", None)
    if poll is not None:
        return _render_poll(poll)
    return None


def _location_coordinates(location: Any) -> str | None:
    latitude = getattr(location, "latitude", None)
    longitude = getattr(location, "longitude", None)
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None
    return f"latitude {latitude}, longitude {longitude}"


def _render_location(location: Any) -> str | None:
    coordinates = _location_coordinates(location)
    if coordinates is None:
        return None
    return f"[location shared] {coordinates}"


def _render_venue(venue: Any) -> str | None:
    details = ", ".join(
        value.strip()
        for value in (getattr(venue, "title", None), getattr(venue, "address", None))
        if isinstance(value, str) and value.strip()
    )
    coordinates = _location_coordinates(getattr(venue, "location", None))
    if details and coordinates:
        return f"[location shared] {details} ({coordinates})"
    if details:
        return f"[location shared] {details}"
    if coordinates:
        return f"[location shared] {coordinates}"
    return None


def _render_contact(contact: Any) -> str | None:
    name = " ".join(
        value.strip()
        for value in (getattr(contact, "first_name", None), getattr(contact, "last_name", None))
        if isinstance(value, str) and value.strip()
    )
    phone_raw = getattr(contact, "phone_number", None)
    phone = phone_raw.strip() if isinstance(phone_raw, str) and phone_raw.strip() else None
    if not name and phone is None:
        return None
    if phone is None:
        return f"[contact shared] {name}"
    if not name:
        return f"[contact shared] phone: {phone}"
    return f"[contact shared] {name}, phone: {phone}"


def _render_poll(poll: Any) -> str | None:
    question = getattr(poll, "question", None)
    if not isinstance(question, str) or not question.strip():
        return None
    lines = [f"[poll] {question.strip()}"]
    for option in getattr(poll, "options", ()) or ():
        text = getattr(option, "text", None)
        if isinstance(text, str) and text.strip():
            lines.append(f"- {text.strip()}")
    return "\n".join(lines)


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


def _default_animation_filename(animation: Any) -> str:
    # Telegram converts GIFs to MP4 animations; a real GIF still sniffs as image/gif
    # in the attachment store regardless of this fallback extension.
    return _media_filename(animation, "telegram-animation", ".mp4")


def _is_image_media_type(media_type: str) -> bool:
    return isinstance(media_type, str) and media_type.startswith("image/")


def _parse_platform_target(platform_target: str) -> int:
    try:
        chat_id = int(platform_target)
    except (TypeError, ValueError) as error:
        raise ChannelConfigError("platform_target must be an integer chat id") from error
    return chat_id


def _parse_thread_id(thread_id: str | None) -> int | None:
    if thread_id is None:
        return None
    try:
        return int(thread_id)
    except (TypeError, ValueError) as error:
        raise ChannelConfigError("thread_id must be an integer Telegram topic id") from error


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


def _load_telegram_error() -> Any:
    try:
        return import_module("telegram.error")
    except ModuleNotFoundError as error:
        raise ChannelError(
            "python-telegram-bot is required for Telegram channels; install server dependencies"
        ) from error


@contextlib.contextmanager
def _telegram_error_boundary(channel_id: str) -> Iterator[None]:
    """Translate python-telegram-bot send errors into ChannelError at the adapter boundary.

    PTB raises ``telegram.error.TelegramError`` (e.g. ``BadRequest``) when the Bot API rejects
    a send. Callers such as the ``channel_send`` tool and the engine relay only handle the
    ChannelError family, so an unwrapped PTB error would surface as an unexpected exception
    instead of a clean failure.
    """
    telegram_error = _load_telegram_error()
    try:
        yield
    except telegram_error.TelegramError as error:
        raise ChannelError(f"Telegram send failed (channel={channel_id}): {error}") from error


def _is_integer(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_message_id(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


__all__ = [
    "TELEGRAM_CAPTION_LIMIT",
    "TELEGRAM_MESSAGE_LIMIT",
    "TelegramChannelAdapter",
    "split_telegram_message",
]
