"""Telegram channel adapter implementation."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from functools import partial
from importlib import import_module
from typing import TYPE_CHECKING, Any

from core.attachments import AttachmentStore
from core.channels.adapter import (
    ChannelAdapter,
    ConversationFacts,
    FileData,
    MessageFacts,
    ReplyPlanFacts,
    RouteFacts,
)
from core.channels.channels import ChannelConfig, ChannelConfigError, ChannelError
from core.chat.content_blocks import ContentBlock, FileBlock, MediaBlock, TextBlock
from core.chat.runs import (
    ASSISTANT_OUTPUT_EVENT,
    RUN_CANCELLED_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.chat.chat import ChatSessionManager
    from core.chat.runs import Run, RunEvent

_LOGGER = get_logger("channels.telegram")

TELEGRAM_MESSAGE_LIMIT = 4096
_FAILED_REPLY = "Sorry, I couldn't complete that request. Please try again."
_CANCELLED_REPLY = "Sorry, this request was cancelled before completion."
_EMPTY_ASSISTANT_REPLY = "I finished processing your message, but no reply text was produced."
_SYSTEM_REMINDER_TEMPLATE = (
    "This session is receiving messages via Telegram "
    "(channel: {channel_id}, chat: {chat_id}).\n"
    "Respond in a style appropriate for Telegram messaging."
)


@dataclass(slots=True, frozen=True)
class _QueuedInboundMessage:
    route: RouteFacts
    reply_plan: ReplyPlanFacts
    message: MessageFacts


class TelegramChannelAdapter(ChannelAdapter):
    """Telegram long-polling adapter for bidirectional channel messaging."""

    platform = "telegram"

    def __init__(
        self,
        config: ChannelConfig,
        trigger_service: TriggerService,
        chat_sessions: ChatSessionManager,
        runtime: object,
        attachment_store: AttachmentStore | None = None,
    ) -> None:
        self._config = config
        self._trigger_service = trigger_service
        self._chat_sessions = chat_sessions
        self._runtime = runtime
        self._attachment_store = attachment_store

        token = _resolve_channel_token(config.token_env_var, runtime)
        if not isinstance(token, str) or not token.strip():
            raise ChannelConfigError(
                f"Missing Telegram token in environment variable: {config.token_env_var}"
            )
        self._token = token.strip()

        self._application: Any | None = None
        self._stop_event = asyncio.Event()
        self._allowed_chat_ids = frozenset(config.allowed_chat_ids)
        self._chat_queues: dict[str, asyncio.Queue[_QueuedInboundMessage]] = {}
        self._chat_workers: dict[str, asyncio.Task[None]] = {}
        self._album_buffers: dict[str, list[ContentBlock]] = {}
        self._album_routes: dict[str, RouteFacts] = {}
        self._album_reply_plans: dict[str, ReplyPlanFacts] = {}
        self._album_tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        """Start Telegram long-polling and wait until stop is requested."""
        if self._application is not None:
            await self._stop_event.wait()
            return

        telegram_ext = _load_telegram_ext()
        application = telegram_ext.Application.builder().token(self._token).build()
        application.add_handler(
            telegram_ext.MessageHandler(telegram_ext.filters.TEXT, self._handle_inbound_message)
        )
        application.add_handler(
            telegram_ext.MessageHandler(
                telegram_ext.filters.PHOTO | telegram_ext.filters.Document.ALL,
                self._handle_inbound_media,
            )
        )
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

    async def stop(self) -> None:
        """Stop polling, cancel per-chat workers, and release Telegram resources."""
        self._stop_event.set()
        await self._stop_chat_workers()

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

    async def _handle_inbound_message(
        self,
        update: Any,
        _context: Any,
    ) -> None:
        conversation = self._conversation_facts(update)
        if conversation is None:
            return

        chat_id = int(conversation.chat_id)
        if not self._is_chat_allowed(chat_id):
            return

        message_text = _extract_message_text(update)
        if message_text is None:
            return

        route, reply_plan = self._prepare_inbound_route(conversation)

        self._enqueue_chat_message(
            conversation.chat_id,
            _QueuedInboundMessage(
                route=route,
                reply_plan=reply_plan,
                message=MessageFacts(content=message_text),
            ),
        )

    async def _handle_inbound_media(self, update: Any, _context: Any) -> None:
        conversation = self._conversation_facts(update)
        if conversation is None:
            return

        chat_id = int(conversation.chat_id)
        if not self._is_chat_allowed(chat_id):
            return

        message = getattr(update, "effective_message", None)
        if message is None:
            return

        try:
            content_blocks = await self._build_media_message_blocks(message)
        except Exception as error:
            _LOGGER.warning(
                "Telegram inbound media processing failed (channel=%s chat=%s): %s",
                self._config.id,
                conversation.chat_id,
                error,
            )
            return

        if not content_blocks:
            return

        route, reply_plan = self._prepare_inbound_route(conversation)
        media_group_id = getattr(message, "media_group_id", None)
        if media_group_id is not None:
            self._buffer_album_message(
                str(media_group_id),
                conversation.chat_id,
                route,
                reply_plan,
                content_blocks,
            )
            return

        self._enqueue_chat_message(
            conversation.chat_id,
            _QueuedInboundMessage(
                route=route,
                reply_plan=reply_plan,
                message=MessageFacts(content=content_blocks),
            ),
        )

    def _prepare_inbound_route(
        self,
        conversation: ConversationFacts,
    ) -> tuple[RouteFacts, ReplyPlanFacts]:
        route = self._route_facts(conversation)
        is_new_session = not self._session_exists(route)
        session = self._chat_sessions.get_or_create(route.agent_id, route.session_id)
        if is_new_session:
            session.add_note(
                _SYSTEM_REMINDER_TEMPLATE.format(
                    channel_id=self._config.id,
                    chat_id=conversation.chat_id,
                )
            )

        reply_plan = ReplyPlanFacts(
            channel_id=self._config.id,
            platform_target=conversation.chat_id,
        )
        self._update_session_metadata(route, conversation, reply_plan)
        return route, reply_plan

    async def _build_media_message_blocks(self, message: Any) -> list[ContentBlock]:
        blocks: list[ContentBlock] = []

        caption = _extract_caption(message)
        if caption is not None:
            blocks.append(TextBlock(type="text", text=caption))

        photo_items = getattr(message, "photo", None)
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

        document = getattr(message, "document", None)
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

    async def _store_inbound_attachment(self, *, file_id: str, filename: str) -> Any:
        attachment_store = self._attachment_store
        if attachment_store is None:
            raise ChannelError("Attachment store is not configured for Telegram channels")

        bot = self._require_bot()
        telegram_file = await bot.get_file(file_id)
        payload = await telegram_file.download_as_bytearray()
        return attachment_store.store(filename, bytes(payload))

    def _buffer_album_message(
        self,
        album_id: str,
        chat_id: str,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        blocks: list[ContentBlock],
    ) -> None:
        existing_blocks = self._album_buffers.get(album_id)
        if existing_blocks is not None:
            existing_blocks.extend(blocks)
            return

        self._album_buffers[album_id] = list(blocks)
        self._album_routes[album_id] = route
        self._album_reply_plans[album_id] = reply_plan
        task = asyncio.create_task(
            self._flush_album(album_id, chat_id),
            name=f"telegram:{self._config.id}:album:{album_id}",
        )
        self._album_tasks[album_id] = task
        task.add_done_callback(partial(self._on_album_task_done, album_id))

    async def _flush_album(self, album_id: str, chat_id: str) -> None:
        await asyncio.sleep(0.5)

        blocks = self._album_buffers.pop(album_id, [])
        route = self._album_routes.pop(album_id, None)
        reply_plan = self._album_reply_plans.pop(album_id, None)
        if not blocks or route is None or reply_plan is None:
            return

        self._enqueue_chat_message(
            chat_id,
            _QueuedInboundMessage(
                route=route,
                reply_plan=reply_plan,
                message=MessageFacts(content=blocks),
            ),
        )

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

    def _conversation_facts(self, update: Any) -> ConversationFacts | None:
        message = getattr(update, "effective_message", None)
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        if message is None or chat is None or user is None:
            return None

        chat_id = getattr(chat, "id", None)
        user_id = getattr(user, "id", None)
        if not _is_integer(chat_id) or not _is_integer(user_id):
            return None

        thread_id_raw = getattr(message, "message_thread_id", None)
        thread_id = str(thread_id_raw) if thread_id_raw is not None else None

        return ConversationFacts(
            platform=self.platform,
            channel_id=self._config.id,
            chat_id=str(chat_id),
            user_id=str(user_id),
            thread_id=thread_id,
        )

    def _route_facts(self, conversation: ConversationFacts) -> RouteFacts:
        return RouteFacts(
            agent_id=self._config.agent_id,
            session_id=self._derive_session_id(conversation),
        )

    def _derive_session_id(self, conversation: ConversationFacts) -> str:
        chat_id = int(conversation.chat_id)
        if chat_id < 0:
            return f"ch-{self._config.id}-{conversation.chat_id}"

        scope = self._config.dm_scope
        if scope == "main":
            return f"ch-{self._config.id}-main"
        if scope == "per_peer":
            return f"ch-{self._config.id}-u{conversation.user_id}"
        if scope == "per_account_channel_peer":
            return f"ch-{self._config.id}-{conversation.chat_id}-u{conversation.user_id}"
        return f"ch-{self._config.id}-{conversation.chat_id}"

    def _is_chat_allowed(self, chat_id: int) -> bool:
        # D8: empty allowed_chat_ids means deny all inbound chats.
        return chat_id in self._allowed_chat_ids

    def _session_exists(self, route: RouteFacts) -> bool:
        sessions_dir = self._chat_sessions.sessions_dir(route.agent_id)
        session_path = sessions_dir / f"{route.session_id}.jsonl"
        return session_path.exists()

    def _update_session_metadata(
        self,
        route: RouteFacts,
        conversation: ConversationFacts,
        reply_plan: ReplyPlanFacts,
    ) -> None:
        metadata = self._chat_sessions.get_metadata(route.agent_id, route.session_id)
        metadata.update(
            {
                "source_channel_id": self._config.id,
                "platform": self.platform,
                "platform_conv_id": conversation.chat_id,
                "last_reply_target": {
                    "channel_id": reply_plan.channel_id,
                    "platform_target": reply_plan.platform_target,
                },
            }
        )
        self._chat_sessions.set_metadata(route.agent_id, route.session_id, metadata)

    def _enqueue_chat_message(self, chat_id: str, queued: _QueuedInboundMessage) -> None:
        queue = self._chat_queues.get(chat_id)
        if queue is None:
            queue = asyncio.Queue()
            self._chat_queues[chat_id] = queue

        queue.put_nowait(queued)

        worker = self._chat_workers.get(chat_id)
        if worker is None or worker.done():
            worker = asyncio.create_task(
                self._run_chat_queue(chat_id, queue),
                name=f"telegram:{self._config.id}:{chat_id}",
            )
            self._chat_workers[chat_id] = worker

    async def _run_chat_queue(
        self,
        chat_id: str,
        queue: asyncio.Queue[_QueuedInboundMessage],
    ) -> None:
        try:
            while True:
                queued = await queue.get()
                try:
                    await self._process_queued_message(queued)
                except Exception as error:
                    _LOGGER.error(
                        "Telegram inbound processing failed (channel=%s chat=%s): %s",
                        self._config.id,
                        chat_id,
                        error,
                        exc_info=(type(error), error, error.__traceback__),
                    )
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise
        finally:
            current = self._chat_workers.get(chat_id)
            if current is asyncio.current_task():
                self._chat_workers.pop(chat_id, None)

    async def _process_queued_message(self, queued: _QueuedInboundMessage) -> None:
        try:
            run = await self._trigger_service.trigger_run(
                queued.route.agent_id,
                queued.message.content,
                queued.route.session_id,
            )
        except Exception:
            await self.send(_format_failed_reply(), queued.reply_plan.platform_target)
            return

        await self._relay_run_events(run, queued.reply_plan.platform_target)

    async def _relay_run_events(self, run: Run, platform_target: str) -> None:
        assistant_text: str | None = None

        async for event in run.subscribe():
            if event.type == ASSISTANT_OUTPUT_EVENT:
                extracted = _extract_assistant_output(event)
                if extracted is not None:
                    assistant_text = extracted
                continue

            if event.type == RUN_COMPLETED_EVENT:
                await self.send(assistant_text or _EMPTY_ASSISTANT_REPLY, platform_target)
                return

            if event.type == RUN_FAILED_EVENT:
                await self.send(_format_failed_reply(), platform_target)
                return

            if event.type == RUN_CANCELLED_EVENT:
                await self.send(_CANCELLED_REPLY, platform_target)
                return

    async def _stop_chat_workers(self) -> None:
        workers = list(self._chat_workers.values())
        album_tasks = list(self._album_tasks.values())
        self._chat_workers.clear()
        self._chat_queues.clear()
        self._album_tasks.clear()
        self._album_buffers.clear()
        self._album_routes.clear()
        self._album_reply_plans.clear()
        for worker in workers:
            worker.cancel()
        for task in album_tasks:
            task.cancel()

        pending = [*workers, *album_tasks]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

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


def _is_image_media_type(media_type: str) -> bool:
    return isinstance(media_type, str) and media_type.startswith("image/")


def _extract_assistant_output(event: RunEvent) -> str | None:
    payload = event.payload
    if not isinstance(payload, dict):
        return None

    message = payload.get("message")
    if not isinstance(message, dict):
        return None

    content = message.get("content")
    if not isinstance(content, str):
        return None

    content = content.strip()
    return content or None


def _format_failed_reply() -> str:
    return _FAILED_REPLY


def _resolve_channel_token(token_env_var: str, runtime: object) -> str:
    resolver = getattr(runtime, "resolve_environment_credential", None)
    if callable(resolver):
        try:
            resolved = resolver(token_env_var)
        except Exception:
            resolved = ""
        if isinstance(resolved, str):
            return resolved

    return os.environ.get(token_env_var, "")


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


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = ["TELEGRAM_MESSAGE_LIMIT", "TelegramChannelAdapter", "split_telegram_message"]
