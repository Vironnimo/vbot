"""Discord channel adapter implementation."""

from __future__ import annotations

import asyncio
import contextlib
import io
from collections.abc import AsyncIterator, Callable, Sequence
from importlib import import_module
from typing import TYPE_CHECKING, Any

from core.attachments import AttachmentStore
from core.channels.adapter import (
    ChannelAdapter,
    ConversationFacts,
    FileData,
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

_LOGGER = get_logger("channels.discord")

DISCORD_MESSAGE_LIMIT = 2000
_DISCORD_FILE_BATCH_LIMIT = 10
_HISTORY_BACKFILL_LIMIT = 50


class DiscordChannelAdapter(ChannelAdapter):
    """Discord Gateway adapter for bidirectional channel messaging."""

    platform = "discord"
    platform_display_name = "Discord"

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
        self._command_dispatcher = command_dispatcher
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
                f"Missing Discord token in environment variable: {config.token_env_var}"
            )
        self._token = token.strip()

        self._client: Any | None = None
        self._bot_id: str | None = None
        self._allowed_chat_ids = frozenset(config.allowed_chat_ids)
        self._message_locks: dict[str, asyncio.Lock] = {}
        self._backfilled_message_ids: dict[str, set[str]] = {}
        self._known_conversations: dict[str, ConversationFacts] = {}

    async def start(self) -> None:
        """Connect to Discord's Gateway and process messages until stopped."""
        if self._client is not None:
            raise ChannelError(f"Discord channel is already running: {self._config.id}")

        discord = _load_discord()
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        async def on_ready() -> None:
            bot_user = client.user
            bot_id = getattr(bot_user, "id", None)
            self._bot_id = str(bot_id) if _is_snowflake(bot_id) else None
            _LOGGER.info(
                "Discord adapter started (channel=%s bot_id=%s)",
                self._config.id,
                self._bot_id or "unknown",
            )

        async def on_message(message: Any) -> None:
            await self._handle_inbound_message(message)

        client.event(on_ready)
        client.event(on_message)
        self._client = client
        await client.start(self._token)

    async def stop(self) -> None:
        """Stop engine workers and close the Discord Gateway connection."""
        await self._engine.stop()
        self._message_locks.clear()
        self._backfilled_message_ids.clear()
        self._known_conversations.clear()

        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.close()
        except Exception as error:
            _LOGGER.warning(
                "Discord adapter close failed (channel=%s): %s",
                self._config.id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def send(
        self,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
    ) -> None:
        """Send one outbound message and/or file payloads to a Discord channel."""
        target = await self._resolve_target(platform_target)
        await self._send_payloads(target, message, list(files or []))
        self._backfilled_message_ids.pop(platform_target, None)

    def ensure_outbound_session(self, platform_target: str) -> RouteFacts:
        """Ensure the Session mirroring a cached Discord target exists."""
        target_id = _parse_platform_target(platform_target)
        normalized_target = str(target_id)
        conversation = self._known_conversations.get(normalized_target)
        if conversation is None:
            client = self._require_client()
            target = client.get_channel(target_id)
            if target is None:
                raise ChannelConfigError(
                    "Discord platform_target is not cached; send to it before recording "
                    "outbound context"
                )
            conversation = self._conversation_facts_for_target(target)
            self._remember_conversation(conversation)
        return self._engine.ensure_channel_session(conversation)

    # -- ConversationTransport --------------------------------------------------------

    async def send_text(
        self,
        platform_target: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> None:
        """Deliver one engine reply, referencing the triggering group message."""
        target = await self._resolve_target(platform_target)
        reference = self._reply_reference(target, reply_to_message_id)
        await self._send_payloads(target, text, [], reference=reference)
        self._backfilled_message_ids.pop(platform_target, None)

    def activity_indicator(
        self, platform_target: str
    ) -> contextlib.AbstractAsyncContextManager[None]:
        """Discord typing indicator as the engine activity callback."""
        return self._typing_indicator(platform_target)

    def caption_text(self, raw_message: Any) -> str | None:
        """Expose Discord message content as the media caption for gating."""
        return _message_content(raw_message)

    async def build_media_blocks(self, raw_message: Any) -> list[ContentBlock]:
        """Convert Discord attachments into canonical content blocks."""
        blocks: list[ContentBlock] = []
        content = _message_content(raw_message)
        if content is not None:
            blocks.append(TextBlock(type="text", text=content))

        attachments = _message_attachments(raw_message)
        for attachment in attachments:
            record = await self._store_inbound_attachment(attachment)
            if record.media_type.startswith(("image/", "audio/", "video/")):
                blocks.append(
                    MediaBlock(
                        type="media",
                        attachment_id=record.id,
                        filename=record.filename,
                        media_type=record.media_type,
                    )
                )
            elif record.media_type.startswith("text/"):
                blocks.append(TextBlock(type="text", text=record.text_content or ""))
            else:
                blocks.append(
                    FileBlock(
                        type="file",
                        attachment_id=record.id,
                        filename=record.filename,
                        media_type=record.media_type,
                    )
                )
        return blocks

    # -- Inbound handling -------------------------------------------------------------

    async def _handle_inbound_message(self, message: Any) -> None:
        author = getattr(message, "author", None)
        if author is None or bool(getattr(author, "bot", False)):
            return

        conversation = self._conversation_facts(message)
        if conversation is None or not self._is_message_allowed(message, conversation):
            return

        content = _message_content(message)
        attachments = _message_attachments(message)
        if content is None and not attachments:
            return

        self._remember_conversation(conversation)
        lock = self._message_locks.setdefault(conversation.chat_id, asyncio.Lock())
        async with lock:
            should_backfill = self._should_backfill(conversation, content)
            if should_backfill:
                await self._backfill_history(message)

            if attachments:
                await self._engine.handle_inbound_media(conversation, (message,))
            elif content is not None:
                await self._engine.handle_inbound_text(conversation, content)

            if should_backfill and conversation.message_id is not None:
                self._seen_message_ids(conversation.chat_id).add(conversation.message_id)

    def _should_backfill(
        self,
        conversation: ConversationFacts,
        content: str | None,
    ) -> bool:
        if conversation.kind != "group":
            return False
        if self._config.response_mode != "mention" or self._config.observe_unaddressed:
            return False
        if content is not None and self._command_dispatcher.recognizes(content):
            return False
        return self._engine.should_respond(conversation, (content,))

    async def _backfill_history(self, triggering_message: Any) -> None:
        channel = getattr(triggering_message, "channel", None)
        target_id = _snowflake_string(getattr(channel, "id", None))
        if channel is None or target_id is None:
            return

        seen_ids = self._seen_message_ids(target_id)
        observed: list[tuple[ConversationFacts, str]] = []
        bot_id = self._effective_bot_id()
        try:
            history = channel.history(
                limit=_HISTORY_BACKFILL_LIMIT,
                before=triggering_message,
                oldest_first=False,
            )
            async for message in history:
                author_id = _snowflake_string(getattr(getattr(message, "author", None), "id", None))
                if bot_id is not None and author_id == bot_id:
                    break

                message_id = _snowflake_string(getattr(message, "id", None))
                if message_id is None or message_id in seen_ids:
                    continue

                body = _observed_message_body(message)
                conversation = self._conversation_facts(message)
                if body is None or conversation is None:
                    continue
                observed.append((conversation, body))
        except Exception as error:
            _LOGGER.warning(
                "Discord history backfill failed (channel=%s target=%s): %s",
                self._config.id,
                target_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            return

        for conversation, body in reversed(observed):
            self._engine.observe_inbound_text(conversation, body)
            if conversation.message_id is not None:
                seen_ids.add(conversation.message_id)

    def _conversation_facts(self, message: Any) -> ConversationFacts | None:
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        chat_id = _snowflake_string(getattr(channel, "id", None))
        user_id = _snowflake_string(getattr(author, "id", None))
        if chat_id is None or user_id is None:
            return None

        guild = getattr(message, "guild", None)
        parent_id = _snowflake_string(getattr(channel, "parent_id", None))
        message_id = _snowflake_string(getattr(message, "id", None))
        return ConversationFacts(
            platform=self.platform,
            channel_id=self._config.id,
            chat_id=chat_id,
            user_id=user_id,
            thread_id=chat_id if parent_id is not None else None,
            kind="direct" if guild is None else "group",
            user_display_name=_user_display_name(author),
            message_id=message_id,
            mentioned_bot=self._mentions_bot(message),
            is_reply_to_bot=self._is_reply_to_bot(message),
        )

    def _conversation_facts_for_target(self, target: Any) -> ConversationFacts:
        chat_id = _snowflake_string(getattr(target, "id", None))
        if chat_id is None:
            raise ChannelConfigError("Discord target does not have a valid channel id")

        guild = getattr(target, "guild", None)
        parent_id = _snowflake_string(getattr(target, "parent_id", None))
        user_id = chat_id
        if guild is None:
            recipient = getattr(target, "recipient", None)
            recipient_id = _snowflake_string(getattr(recipient, "id", None))
            if recipient_id is not None:
                user_id = recipient_id

        return ConversationFacts(
            platform=self.platform,
            channel_id=self._config.id,
            chat_id=chat_id,
            user_id=user_id,
            thread_id=chat_id if parent_id is not None else None,
            kind="direct" if guild is None else "group",
        )

    def _is_message_allowed(
        self,
        message: Any,
        conversation: ConversationFacts,
    ) -> bool:
        if conversation.chat_id in self._allowed_chat_ids:
            return True
        parent_id = _snowflake_string(getattr(getattr(message, "channel", None), "parent_id", None))
        return parent_id is not None and parent_id in self._allowed_chat_ids

    def _mentions_bot(self, message: Any) -> bool:
        bot_id = self._effective_bot_id()
        if bot_id is None:
            return False
        for mentioned_user in getattr(message, "mentions", ()) or ():
            mentioned_id = _snowflake_string(getattr(mentioned_user, "id", None))
            if mentioned_id == bot_id:
                return True
        return bot_id in {str(raw_id) for raw_id in (getattr(message, "raw_mentions", ()) or ())}

    def _is_reply_to_bot(self, message: Any) -> bool:
        bot_id = self._effective_bot_id()
        if bot_id is None:
            return False
        reference = getattr(message, "reference", None)
        if reference is None:
            return False
        replied_message = getattr(reference, "resolved", None)
        if replied_message is None:
            replied_message = getattr(reference, "cached_message", None)
        replied_author = getattr(replied_message, "author", None)
        replied_author_id = _snowflake_string(getattr(replied_author, "id", None))
        return replied_author_id == bot_id

    # -- Outbound helpers -------------------------------------------------------------

    async def _resolve_target(self, platform_target: str) -> Any:
        target_id = _parse_platform_target(platform_target)
        client = self._require_client()
        target = client.get_channel(target_id)
        if target is None:
            try:
                target = await client.fetch_channel(target_id)
            except Exception as error:
                raise ChannelConfigError(
                    f"Cannot resolve Discord platform_target {platform_target}: {error}"
                ) from error

        if not callable(getattr(target, "send", None)):
            raise ChannelConfigError(
                f"Discord platform_target is not a message channel: {platform_target}"
            )
        self._remember_conversation(self._conversation_facts_for_target(target))
        return target

    async def _send_payloads(
        self,
        target: Any,
        message: str | None,
        files: list[FileData],
        *,
        reference: Any | None = None,
    ) -> None:
        normalized_message = _normalize_optional_message(message)
        if normalized_message is None and not files:
            raise ChannelConfigError("at least one of message or files must be provided")

        chunks = (
            split_discord_message(normalized_message, DISCORD_MESSAGE_LIMIT)
            if normalized_message is not None
            else []
        )
        file_batches = [
            files[start : start + _DISCORD_FILE_BATCH_LIMIT]
            for start in range(0, len(files), _DISCORD_FILE_BATCH_LIMIT)
        ]
        send_count = max(len(chunks), len(file_batches))
        discord = _load_discord()

        for index in range(send_count):
            payload: dict[str, Any] = {}
            if index < len(chunks):
                payload["content"] = chunks[index]

            discord_files: list[Any] = []
            if index < len(file_batches):
                discord_files = [
                    discord.File(
                        io.BytesIO(file_data.data),
                        filename=file_data.filename,
                    )
                    for file_data in file_batches[index]
                ]
                payload["files"] = discord_files

            if index == 0 and reference is not None:
                payload["reference"] = reference
                payload["mention_author"] = False

            try:
                await target.send(**payload)
            finally:
                for discord_file in discord_files:
                    close = getattr(discord_file, "close", None)
                    if callable(close):
                        close()

    def _reply_reference(self, target: Any, reply_to_message_id: str | None) -> Any | None:
        if reply_to_message_id is None:
            return None
        try:
            message_id = _parse_platform_target(reply_to_message_id)
            partial_message = target.get_partial_message(message_id)
            return partial_message.to_reference(fail_if_not_exists=False)
        except (AttributeError, ChannelConfigError, TypeError, ValueError) as error:
            _LOGGER.debug(
                "Ignoring invalid Discord reply target (channel=%s target=%r): %s",
                self._config.id,
                reply_to_message_id,
                error,
            )
            return None

    @contextlib.asynccontextmanager
    async def _typing_indicator(self, platform_target: str) -> AsyncIterator[None]:
        try:
            target = await self._resolve_target(platform_target)
            indicator = target.typing()
            await indicator.__aenter__()
        except Exception as error:
            _LOGGER.debug(
                "Discord typing indicator unavailable (channel=%s target=%s): %s",
                self._config.id,
                platform_target,
                error,
            )
            yield
            return

        try:
            yield
        finally:
            try:
                await indicator.__aexit__(None, None, None)
            except Exception as error:
                _LOGGER.debug(
                    "Discord typing indicator close failed (channel=%s target=%s): %s",
                    self._config.id,
                    platform_target,
                    error,
                )

    async def _store_inbound_attachment(self, attachment: Any) -> Any:
        attachment_store = self._attachment_store
        if attachment_store is None:
            raise ChannelError("Attachment store is not configured for Discord channels")

        filename = getattr(attachment, "filename", None)
        if not isinstance(filename, str) or not filename.strip():
            attachment_id = _snowflake_string(getattr(attachment, "id", None)) or "attachment"
            filename = f"discord-{attachment_id}"
        payload = await attachment.read()
        return attachment_store.store(filename.strip(), bytes(payload))

    # -- State helpers ----------------------------------------------------------------

    def _effective_bot_id(self) -> str | None:
        if self._bot_id is not None:
            return self._bot_id
        client = self._client
        user_id = getattr(getattr(client, "user", None), "id", None)
        return _snowflake_string(user_id)

    def _remember_conversation(self, conversation: ConversationFacts) -> None:
        self._known_conversations[conversation.chat_id] = ConversationFacts(
            platform=conversation.platform,
            channel_id=conversation.channel_id,
            chat_id=conversation.chat_id,
            user_id=conversation.user_id,
            thread_id=conversation.thread_id,
            kind=conversation.kind,
            user_display_name=conversation.user_display_name,
        )

    def _seen_message_ids(self, platform_target: str) -> set[str]:
        return self._backfilled_message_ids.setdefault(platform_target, set())

    def _require_client(self) -> Any:
        client = self._client
        if client is None:
            raise ChannelError(f"Discord channel is not running: {self._config.id}")
        return client


def split_discord_message(message: str, max_chars: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split one message into Discord-size chunks."""
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


def _message_content(message: Any) -> str | None:
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def _message_attachments(message: Any) -> tuple[Any, ...]:
    attachments = getattr(message, "attachments", None)
    if not isinstance(attachments, Sequence):
        return ()
    return tuple(attachments)


def _observed_message_body(message: Any) -> str | None:
    parts: list[str] = []
    content = _message_content(message)
    if content is not None:
        parts.append(content)

    for attachment in _message_attachments(message):
        filename = getattr(attachment, "filename", None)
        if isinstance(filename, str) and filename.strip():
            parts.append(f"[media] {filename.strip()}")
        else:
            parts.append("[media message]")

    if not parts:
        return None
    return "\n".join(parts)


def _user_display_name(user: Any) -> str | None:
    for attribute_name in ("display_name", "global_name", "name"):
        value = getattr(user, attribute_name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_platform_target(platform_target: str) -> int:
    try:
        target_id = int(platform_target)
    except (TypeError, ValueError) as error:
        raise ChannelConfigError("platform_target must be a Discord channel id") from error
    if target_id <= 0:
        raise ChannelConfigError("platform_target must be a positive Discord channel id")
    return target_id


def _snowflake_string(value: object) -> str | None:
    if not _is_snowflake(value):
        return None
    return str(value)


def _is_snowflake(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _load_discord() -> Any:
    try:
        return import_module("discord")
    except ModuleNotFoundError as error:
        raise ChannelError(
            "discord.py is required for Discord channels; install server dependencies"
        ) from error


__all__ = ["DISCORD_MESSAGE_LIMIT", "DiscordChannelAdapter", "split_discord_message"]
