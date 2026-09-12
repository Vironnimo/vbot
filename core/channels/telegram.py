"""Telegram channel adapter implementation."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from core.attachments import AttachmentStore
from core.channels.adapter import (
    ChannelAccessRegistry,
    ChannelAdapter,
    ConversationFacts,
    DeniedChatFacts,
    DeniedChatLog,
    FileData,
    ReplyPlanFacts,
    RouteFacts,
    RunButtonBindingRegistry,
    UpdateOffsetStore,
)
from core.channels.config import ChannelConfig, ChannelConfigError, ChannelError
from core.channels.engine import ChannelConversationEngine
from core.extensions import (
    RUN_TRIGGER_PREFIX,
    InteractionButton,
    InteractionEvent,
    InteractionResponder,
)
from core.utils.logging import get_logger

from ._telegram_api import (
    _load_telegram_ext,
    _markup_to_buttons,
    _TelegramInteractionResponder,
)
from ._telegram_inbound import TelegramInboundBuffer
from ._telegram_messages import (
    TELEGRAM_MESSAGE_LIMIT,
    _compile_display_name_pattern,
    _extract_message_text,
    _is_integer,
    _parse_message_id,
    _parse_platform_target,
    _parse_start_command,
    _render_structured_message,
    _telegram_bot_display_name,
    _user_display_name,
    split_telegram_message,
)
from ._telegram_transport import TELEGRAM_CAPTION_LIMIT, TelegramTransport

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.chat.commands import CommandDispatcher
    from core.runs import Run
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("channels.telegram")

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
        access_registry: ChannelAccessRegistry | None = None,
        update_offset_store: UpdateOffsetStore | None = None,
    ) -> None:
        self._config = config
        self._transport = TelegramTransport(config.id, self._require_bot, attachment_store)
        # Persists a group→supergroup chat-id swap into the channel config
        # (ChannelService wires its storage update); None keeps the swap runtime-only.
        self._chat_migration_persister = chat_migration_persister
        # Routes a button tap to the extension registered for its callback prefix.
        # Reads the live extension registry (bound method), so an extension
        # reload/disable needs no channel re-wiring — the next tap uses the current
        # registry. None means taps are always acknowledged but never dispatched.
        self._interaction_dispatcher = interaction_dispatcher
        # Durable update-id watermark: Telegram redelivers unconfirmed updates
        # after a restart; the store lets the adapter skip already-processed ones.
        # None keeps dedup in-memory only (tests).
        self._update_offset_store = update_offset_store
        self._last_update_id = -1
        self._offset_save_tasks: set[asyncio.Task[None]] = set()
        self._engine = ChannelConversationEngine(
            config,
            trigger_service,
            chat_sessions,
            self._transport,
            command_dispatcher=command_dispatcher,
            run_button_binding_registry=run_button_binding_registry,
            access_registry=access_registry,
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
        self._bot_address_patterns: tuple[re.Pattern[str], ...] = ()
        self._inbound = TelegramInboundBuffer(
            config.id, self._engine.handle_inbound_text, self._engine.handle_inbound_media
        )

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
        # The bot's own identity feeds the addressing facts (@mention and visible-name
        # detection, reply-to-bot checks, /cmd@botname suffix parsing) for group gating.
        bot_user = await application.bot.get_me()
        self._set_bot_identity(bot_user)
        self._last_update_id = self._load_update_offset()
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
        # A graceful stop must not lose a watermark save: the next start would
        # otherwise replay already-processed updates as duplicate Runs.
        await self._await_offset_saves()

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
        """Send one outbound message and optional attachments or buttons."""
        await self._transport.send(
            message, platform_target, files=files, thread_id=thread_id, buttons=buttons
        )

    async def relay_run(self, run: Run, reply_plan: ReplyPlanFacts) -> None:
        """Relay one background Run through the composed conversation engine."""
        await self._engine.relay_run(run, reply_plan)

    # -- Inbound handlers -----------------------------------------------------------------

    def _load_update_offset(self) -> int:
        store = self._update_offset_store
        if store is None:
            return -1
        try:
            return store.load_update_offset(self._config.id)
        except Exception as error:
            _LOGGER.warning(
                "Cannot load Telegram polling offset (channel=%s), starting empty: %s",
                self._config.id,
                error,
            )
            return -1

    def _claim_update(self, update: Any) -> bool:
        """Claim one delivered update; False means it was already processed.

        Telegram redelivers every unconfirmed update after an adapter restart.
        The in-memory watermark claims instantly (PTB processes updates
        sequentially, so there is no concurrency to race); the durable save is
        offloaded so the handler never awaits disk I/O. A crash between the
        engine hand-off and the save can still redeliver that one message -
        at-most-once here would instead drop user messages on the same crash.
        """
        update_id = getattr(update, "update_id", None)
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            return True
        if update_id <= self._last_update_id:
            _LOGGER.debug(
                "Skipping re-delivered Telegram update (channel=%s update_id=%s)",
                self._config.id,
                update_id,
            )
            return False
        self._last_update_id = update_id
        self._schedule_offset_save(update_id)
        return True

    def _schedule_offset_save(self, update_id: int) -> None:
        store = self._update_offset_store
        if store is None:
            return
        task = asyncio.create_task(
            asyncio.to_thread(store.save_update_offset, self._config.id, update_id)
        )
        self._offset_save_tasks.add(task)
        task.add_done_callback(self._on_offset_saved)

    def _on_offset_saved(self, task: asyncio.Task[None]) -> None:
        self._offset_save_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOGGER.warning(
                "Cannot persist Telegram polling offset (channel=%s): %s",
                self._config.id,
                error,
            )

    async def _await_offset_saves(self) -> None:
        """Give pending watermark saves a bounded window during shutdown."""
        if self._offset_save_tasks:
            await asyncio.wait(list(self._offset_save_tasks), timeout=2)

    async def _handle_inbound_message(
        self,
        update: Any,
        _context: Any,
    ) -> None:
        if not self._claim_update(update):
            return

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
        message = getattr(update, "effective_message", None)
        if message is None:
            return

        # /start is Telegram's first-contact ritual in private chats; groups keep the
        # normal command/gating path (where an unknown /start is simply not addressed).
        if conversation.kind == "direct":
            start_payload = _parse_start_command(message_text)
            if start_payload is not None:
                await self._inbound._flush_pending_forward_comment(conversation.chat_id)
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
            await self._inbound._flush_pending_forward_comment(conversation.chat_id)
            await self._engine.handle_inbound_text(conversation, message_text)
            return

        await self._inbound._buffer_possible_forward_comment(conversation, message_text, message)

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
        if not self._claim_update(update):
            return

        conversation = self._conversation_facts(update)
        if conversation is None:
            return

        if not self._is_chat_allowed(conversation.chat_id):
            self._record_denied_inbound(conversation, update)
            return

        message = getattr(update, "effective_message", None)
        if message is None:
            return

        companion_text, conversation = await self._inbound._take_forward_comment_for_media(
            conversation, message
        )
        media_group_id = getattr(message, "media_group_id", None)
        if media_group_id is not None:
            self._inbound._buffer_album_message(
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
        if not self._claim_update(update):
            return

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

        await self._inbound._flush_pending_forward_comment(conversation.chat_id)
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
        if not self._claim_update(update):
            return

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

        await self._inbound._flush_pending_forward_comment(conversation.chat_id)
        # Same reply semantics as engine replies: group replies reference the message
        # they answer, and the reply follows the message into its forum topic.
        await self._transport.send_text(
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
        if not self._claim_update(update):
            # A redelivered tap was answered before the restart; answering again
            # would surface a stale toast for an already-consumed interaction.
            return

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
            access_scope_id=str(chat_id) if chat_id < 0 else None,
            thread_id=None,
            kind="group" if chat_id < 0 else "direct",
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
            access_scope_id=str(chat_id) if chat_id < 0 else None,
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
        self._transport.bot_id = self._bot_id

        address_patterns: list[re.Pattern[str]] = []
        username = getattr(bot_user, "username", None)
        if isinstance(username, str) and username.strip():
            self._bot_username = username.strip()
            address_patterns.append(
                re.compile(rf"@{re.escape(self._bot_username)}(?!\w)", re.IGNORECASE)
            )
        else:
            self._bot_username = None

        display_name = _telegram_bot_display_name(bot_user)
        if display_name is not None:
            address_patterns.append(_compile_display_name_pattern(display_name))
        self._bot_address_patterns = tuple(address_patterns)

    def _mentions_bot(self, message: Any) -> bool:
        # Regex over text and caption instead of entity offsets: Telegram entity offsets
        # are UTF-16 code units. Runtime-derived patterns cover both @username and the
        # visible bot name without mutating persisted mention_patterns configuration.
        for attribute_name in ("text", "caption"):
            value = getattr(message, attribute_name, None)
            if isinstance(value, str):
                for pattern in self._bot_address_patterns:
                    if pattern.search(value):
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

    # -- Lifecycle helpers ----------------------------------------------------------------

    async def _stop_workers(self) -> None:
        await self._inbound.stop()
        await self._engine.stop()

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


def _start_greeting_prompt(start_payload: str) -> str:
    if not start_payload:
        return _START_GREETING_PROMPT
    # Deep links (t.me/<bot>?start=<payload>) deliver a parameter worth surfacing.
    return f'{_START_GREETING_PROMPT} They arrived with the start parameter "{start_payload}".'


__all__ = [
    "TELEGRAM_CAPTION_LIMIT",
    "TELEGRAM_MESSAGE_LIMIT",
    "TelegramChannelAdapter",
    "split_telegram_message",
]
