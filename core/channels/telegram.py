"""Telegram channel adapter implementation."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any

from core.channels.adapter import (
    ChannelAdapter,
    ConversationFacts,
    MessageFacts,
    ReplyPlanFacts,
    RouteFacts,
)
from core.channels.channels import ChannelConfig, ChannelConfigError, ChannelError
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
_FAILED_REPLY_PREFIX = "Sorry, your request failed"
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
    ) -> None:
        self._config = config
        self._trigger_service = trigger_service
        self._chat_sessions = chat_sessions
        self._runtime = runtime

        token = os.environ.get(config.token_env_var)
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

    async def send(self, message: str, platform_target: str) -> None:
        """Send one outbound message to Telegram, chunked to platform limits."""
        bot = self._require_bot()
        chat_id = _parse_platform_target(platform_target)
        for chunk in split_telegram_message(message, TELEGRAM_MESSAGE_LIMIT):
            await bot.send_message(chat_id=chat_id, text=chunk)

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

        self._enqueue_chat_message(
            conversation.chat_id,
            _QueuedInboundMessage(
                route=route,
                reply_plan=reply_plan,
                message=MessageFacts(text=message_text),
            ),
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
                queued.message.text,
                queued.route.session_id,
            )
        except Exception as error:
            await self.send(_format_failed_reply(str(error)), queued.reply_plan.platform_target)
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
                error_text = _extract_error_text(event)
                await self.send(_format_failed_reply(error_text), platform_target)
                return

            if event.type == RUN_CANCELLED_EVENT:
                await self.send(_CANCELLED_REPLY, platform_target)
                return

    async def _stop_chat_workers(self) -> None:
        workers = list(self._chat_workers.values())
        self._chat_workers.clear()
        self._chat_queues.clear()
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

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


def _extract_message_text(update: Any) -> str | None:
    message = getattr(update, "effective_message", None)
    text = getattr(message, "text", None)
    if not isinstance(text, str):
        return None
    if not text.strip():
        return None
    return text


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


def _extract_error_text(event: RunEvent) -> str:
    payload = event.payload
    if not isinstance(payload, dict):
        return ""

    error = payload.get("error")
    if not isinstance(error, str):
        return ""

    return error.strip()


def _format_failed_reply(detail: str) -> str:
    if detail:
        return f"{_FAILED_REPLY_PREFIX}: {detail}."
    return f"{_FAILED_REPLY_PREFIX}."


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


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = ["TELEGRAM_MESSAGE_LIMIT", "TelegramChannelAdapter", "split_telegram_message"]
