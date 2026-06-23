"""Platform-neutral conversation engine for channel adapters.

The engine owns everything about a channel conversation that is not specific to one
messaging platform: per-conversation queueing and worker serialization, slash-command
dispatch handling, run trigger/relay, and session routing/metadata. A `ChannelAdapter`
composes one engine in its ``__init__`` and delegates to it; raw platform messages flow
through the engine as opaque values and are converted to canonical content blocks by the
injected `ConversationTransport`.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from core.attachments import AttachmentTooLargeError, AttachmentTypeNotAllowedError
from core.channels.adapter import (
    ConversationFacts,
    MessageFacts,
    ReplyPlanFacts,
    RouteFacts,
    channel_system_reminder,
)
from core.chat.commands import CommandAction, CommandDispatcher, CommandHandled
from core.chat.content_blocks import ContentBlock
from core.chat.errors import ChatSessionError
from core.chat.messages import MessageSender
from core.runs import (
    ASSISTANT_OUTPUT_EVENT,
    RUN_CANCELLED_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
)
from core.sessions.sessions import CHANNEL_MESSAGE_NOTE_PREFIX, SESSION_ID_PATTERN
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.channels.channels import ChannelConfig
    from core.runs import Run, RunEvent
    from core.sessions import ChatSession, ChatSessionManager

_LOGGER = get_logger("channels.engine")

_FAILED_REPLY = "Sorry, I couldn't complete that request. Please try again."
_CANCELLED_REPLY = "Sorry, this request was cancelled before completion."
_EMPTY_ASSISTANT_REPLY = "I finished processing your message, but no reply text was produced."
_UNSUPPORTED_FILE_REPLY = "Sorry, this file type isn't supported yet."
_FILE_TOO_LARGE_REPLY = "Sorry, this file is too large to process."
_MEDIA_FAILED_REPLY = "Sorry, I couldn't process the attached file. Please try again."
_SENDER_TAG_UNSAFE_CHARACTERS = str.maketrans("", "", "[]|\r\n")

# Metadata-sidecar key on a conversation anchor that points at the chat's currently
# active session (the "Wegweiser" pointer). Absent = the anchor itself is the session.
ACTIVE_SESSION_METADATA_KEY = "active_session_id"
_NEW_SESSION_STARTED_REPLY = (
    "Started a new session. Your previous conversation has been saved and is still available."
)
# Mirrors the WebUI /new refusal so the behavior reads the same across accessors.
_NEW_SESSION_BUSY_REPLY = "A new session can be started after the current run finishes."


class ConversationTransport(Protocol):
    """Platform I/O surface the engine drives.

    The adapter implements this; the engine stays free of platform libraries. Raw platform
    messages are opaque to the engine and only ``build_media_blocks`` understands them.
    """

    @property
    def platform_display_name(self) -> str:
        """Human-facing platform name used verbatim in reply and reminder text."""

    async def send_text(
        self,
        platform_target: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> None:
        """Deliver one outbound text reply, optionally referencing a platform message."""

    def activity_indicator(
        self, platform_target: str
    ) -> contextlib.AbstractAsyncContextManager[None]:
        """Show a best-effort activity indicator for a target until the block exits."""

    async def build_media_blocks(self, raw_message: Any) -> list[ContentBlock]:
        """Convert one raw platform message into canonical content blocks."""

    def caption_text(self, raw_message: Any) -> str | None:
        """Extract caption text from one raw platform message for gating checks."""


@dataclass(slots=True, frozen=True)
class _QueuedInboundMessage:
    route: RouteFacts
    reply_plan: ReplyPlanFacts
    message: MessageFacts
    command_checked: bool = False
    sender: MessageSender | None = None


@dataclass(slots=True, frozen=True)
class _QueuedCommandAction:
    route: RouteFacts
    reply_plan: ReplyPlanFacts
    action: CommandAction
    # The stable conversation anchor captured when the command was received, so a
    # deferred /new sets its pointer on the anchor and not on the resolved active
    # session (route.session_id may already be a pointer target).
    conversation_key: str | None = None


@dataclass(slots=True, frozen=True)
class _QueuedInboundMedia:
    route: RouteFacts
    reply_plan: ReplyPlanFacts
    # Raw platform messages; conversion to content blocks happens in the per-conversation
    # worker via the transport so the adapter's update pipeline never blocks.
    messages: tuple[Any, ...]
    sender: MessageSender | None = None


@dataclass(slots=True, frozen=True)
class _QueuedObservedMessage:
    conversation: ConversationFacts
    note: str


_QueuedWork = (
    _QueuedInboundMessage | _QueuedCommandAction | _QueuedInboundMedia | _QueuedObservedMessage
)


class ChannelConversationEngine:
    """Platform-neutral conversation behavior shared by channel adapters."""

    def __init__(
        self,
        config: ChannelConfig,
        trigger_service: TriggerService,
        chat_sessions: ChatSessionManager,
        transport: ConversationTransport,
        *,
        command_dispatcher: CommandDispatcher,
    ) -> None:
        self._config = config
        self._trigger_service = trigger_service
        self._chat_sessions = chat_sessions
        self._transport = transport
        self._command_dispatcher = command_dispatcher
        self._owner_user_ids = frozenset(config.owner_user_ids)
        # Config validation guarantees the patterns compile.
        self._mention_patterns = tuple(
            re.compile(pattern, re.IGNORECASE) for pattern in config.mention_patterns
        )
        self._chat_queues: dict[str, asyncio.Queue[_QueuedWork]] = {}
        self._chat_workers: dict[str, asyncio.Task[None]] = {}

    # -- Inbound entry points ---------------------------------------------------------

    async def handle_inbound_text(
        self,
        conversation: ConversationFacts,
        message_text: str,
    ) -> None:
        """Gate, route, eagerly command-dispatch, and enqueue one inbound text message."""
        if conversation.kind == "group" and self._command_dispatcher.recognizes(message_text):
            # Commands are inherently addressed; they are gated by sender authorization
            # instead of response mode. The check must run before dispatch() because
            # dispatch executes handler side effects (e.g. /stop cancels a Run).
            if not self._command_sender_authorized(conversation):
                _LOGGER.info(
                    "Channel command denied for non-owner (channel=%s chat=%s user=%s)",
                    self._config.id,
                    conversation.chat_id,
                    conversation.user_id,
                )
                return
        elif not self.should_respond(conversation, (message_text,)):
            if self._config.observe_unaddressed and conversation.kind == "group":
                self._enqueue_observed_message(
                    conversation,
                    _format_observed_message(conversation, message_text),
                )
                _LOGGER.debug(
                    "Channel group message not addressed; observed (channel=%s chat=%s)",
                    self._config.id,
                    conversation.chat_id,
                )
                return
            _LOGGER.debug(
                "Channel group message not addressed; dropped (channel=%s chat=%s)",
                self._config.id,
                conversation.chat_id,
            )
            return

        route, reply_plan = self.prepare_inbound_route(conversation)

        command_result = self._command_dispatcher.dispatch(
            route.agent_id,
            route.session_id,
            message_text,
        )
        if await self._handle_dispatch_result(
            command_result,
            route,
            reply_plan,
            defer_actions=True,
            conversation_key=self._derive_session_id(conversation),
        ):
            return

        self._enqueue_chat_work(
            reply_plan.platform_target,
            _QueuedInboundMessage(
                route=route,
                reply_plan=reply_plan,
                message=MessageFacts(content=message_text),
                command_checked=True,
                sender=self._sender_for(conversation),
            ),
        )

    async def handle_inbound_media(
        self,
        conversation: ConversationFacts,
        raw_messages: tuple[Any, ...],
    ) -> None:
        """Gate, route, and enqueue inbound media (one message or a buffered album)."""
        gating_texts = tuple(self._transport.caption_text(message) for message in raw_messages)
        if not self.should_respond(conversation, gating_texts):
            if self._config.observe_unaddressed and conversation.kind == "group":
                for caption in gating_texts:
                    body = (
                        f"[media] {caption}"
                        if caption is not None and caption != ""
                        else "[media message]"
                    )
                    self._enqueue_observed_message(
                        conversation,
                        _format_observed_message(conversation, body),
                    )
                _LOGGER.debug(
                    "Channel group media not addressed; observed (channel=%s chat=%s count=%s)",
                    self._config.id,
                    conversation.chat_id,
                    len(raw_messages),
                )
                return
            _LOGGER.debug(
                "Channel group media not addressed; dropped (channel=%s chat=%s)",
                self._config.id,
                conversation.chat_id,
            )
            return

        route, reply_plan = self.prepare_inbound_route(conversation)
        self._enqueue_chat_work(
            reply_plan.platform_target,
            _QueuedInboundMedia(
                route=route,
                reply_plan=reply_plan,
                messages=tuple(raw_messages),
                sender=self._sender_for(conversation),
            ),
        )

    def observe_inbound_text(
        self,
        conversation: ConversationFacts,
        message_text: str,
    ) -> None:
        """Queue platform-acquired context without starting a Run.

        Discord uses this for bounded history backfill before an addressed group
        message. Passive live observation still flows through ``handle_inbound_text``.
        """
        self._enqueue_observed_message(
            conversation,
            _format_observed_message(conversation, message_text),
        )

    def prepare_inbound_route(
        self,
        conversation: ConversationFacts,
    ) -> tuple[RouteFacts, ReplyPlanFacts]:
        """Ensure the routed Session exists and refresh its channel metadata."""
        route, _session = self._ensure_channel_session(conversation)
        reply_plan = ReplyPlanFacts(
            channel_id=self._config.id,
            platform_target=conversation.chat_id,
            # Group replies reference the triggering message so it is clear which
            # message the bot answers; DM replies stay plain.
            reply_to_message_id=(conversation.message_id if conversation.kind == "group" else None),
        )
        self._update_session_metadata(route, conversation, reply_plan)
        return route, reply_plan

    def ensure_channel_session(self, conversation: ConversationFacts) -> RouteFacts:
        """Ensure the Session mirroring a conversation exists with channel context."""
        route, _session = self._ensure_channel_session(conversation)
        # Proactive (outbound-only) sessions get the same channel sidecar metadata as inbound
        # ones, so a channel_send-created session is recognizable as a channel session and has
        # a last_reply_target before any inbound message arrives. No participant is recorded:
        # an outbound target has no real sender.
        self._update_session_metadata(
            route,
            conversation,
            ReplyPlanFacts(channel_id=self._config.id, platform_target=conversation.chat_id),
            track_participant=False,
        )
        return route

    # -- Gating -------------------------------------------------------------------------

    def should_respond(
        self,
        conversation: ConversationFacts,
        gating_texts: Sequence[str | None] = (),
    ) -> bool:
        """Decide whether one inbound non-command message may trigger a Run.

        Direct conversations always respond. Group conversations respond in
        ``response_mode: "all"``, or in ``"mention"`` mode when the message is addressed:
        platform bot mention, reply to a bot message, or a ``mention_patterns`` wake-word
        match against the supplied texts (message text or media captions).
        """
        if conversation.kind != "group":
            return True
        if self._config.response_mode == "all":
            return True
        if conversation.mentioned_bot or conversation.is_reply_to_bot:
            return True
        return self._matches_mention_patterns(gating_texts)

    def _matches_mention_patterns(self, gating_texts: Sequence[str | None]) -> bool:
        for text in gating_texts:
            if not isinstance(text, str):
                continue
            for pattern in self._mention_patterns:
                if pattern.search(text):
                    return True
        return False

    def _command_sender_authorized(self, conversation: ConversationFacts) -> bool:
        # DM commands are always authorized: the chat allowlist already identifies the
        # sender, and commands act on that sender's own session. Owner gating protects
        # the shared group session; an empty owner list denies all group commands
        # (consistent with allowed_chat_ids deny-all semantics).
        if conversation.kind != "group":
            return True
        return conversation.user_id in self._owner_user_ids

    def _sender_for(self, conversation: ConversationFacts) -> MessageSender | None:
        # Sender identity is group-only in v1; DM turns stay unattributed.
        if conversation.kind != "group":
            return None
        return MessageSender(
            id=conversation.user_id,
            display_name=conversation.user_display_name or conversation.user_id,
        )

    # -- Session routing / metadata ---------------------------------------------------

    def _ensure_channel_session(
        self,
        conversation: ConversationFacts,
    ) -> tuple[RouteFacts, ChatSession]:
        route = self._route_facts(conversation)
        is_new_session = not self._session_exists(route)
        session = self._chat_sessions.get_or_create(route.agent_id, route.session_id)
        if is_new_session:
            session.add_note(
                channel_system_reminder(
                    platform_display_name=self._transport.platform_display_name,
                    channel_id=self._config.id,
                    chat_id=conversation.chat_id,
                )
            )
        return route, session

    def _route_facts(self, conversation: ConversationFacts) -> RouteFacts:
        # _derive_session_id yields the stable conversation anchor. The active
        # session may have been moved off that anchor by /new (the "Wegweiser"
        # pointer), so route through the pointer instead of straight to the anchor.
        conversation_key = self._derive_session_id(conversation)
        return RouteFacts(
            agent_id=self._config.agent_id,
            session_id=self._resolve_active_session_id(self._config.agent_id, conversation_key),
        )

    def _resolve_active_session_id(self, agent_id: str, conversation_key: str) -> str:
        """Follow a conversation anchor's pointer to its currently active session.

        ``/new`` stores an ``active_session_id`` pointer in the anchor's metadata
        sidecar and creates a fresh session as the live one. With no pointer the
        anchor *is* the session, so a channel that never ran ``/new`` routes
        exactly as before (no migration, no legacy branch — the default state).
        """
        try:
            metadata = self._chat_sessions.get_metadata(agent_id, conversation_key)
        except ChatSessionError:
            # Anchor session does not exist yet -> nothing has moved off it.
            return conversation_key
        active = metadata.get(ACTIVE_SESSION_METADATA_KEY)
        # Single hop: the pointer always names the newest session directly. A
        # deleted target is fine -- get_or_create re-creates it empty downstream,
        # keeping the current conversation fresh rather than reviving old history.
        if isinstance(active, str) and active:
            return active
        return conversation_key

    def _derive_session_id(self, conversation: ConversationFacts) -> str:
        # Group conversations share one session keyed by chat id and ignore dm_scope.
        if conversation.kind == "group":
            return f"ch-{self._config.id}-{conversation.chat_id}"

        scope = self._config.dm_scope
        if scope == "main":
            return f"ch-{self._config.id}-main"
        if scope == "per_peer":
            return f"ch-{self._config.id}-u{conversation.user_id}"
        if scope == "per_account_channel_peer":
            return f"ch-{self._config.id}-{conversation.chat_id}-u{conversation.user_id}"
        return f"ch-{self._config.id}-{conversation.chat_id}"

    def _session_exists(self, route: RouteFacts) -> bool:
        return self._chat_sessions.exists(route.agent_id, route.session_id)

    def _update_session_metadata(
        self,
        route: RouteFacts,
        conversation: ConversationFacts,
        reply_plan: ReplyPlanFacts,
        *,
        track_participant: bool = True,
    ) -> None:
        metadata = self._chat_sessions.get_metadata(route.agent_id, route.session_id)
        metadata.update(
            {
                "source_channel_id": self._config.id,
                "platform": conversation.platform,
                "platform_conv_id": conversation.chat_id,
                "last_reply_target": {
                    "channel_id": reply_plan.channel_id,
                    "platform_target": reply_plan.platform_target,
                },
            }
        )
        if track_participant and conversation.kind == "group":
            participants = metadata.get("participants")
            if not isinstance(participants, dict):
                participants = {}
            participants[conversation.user_id] = {
                "display_name": conversation.user_display_name or conversation.user_id,
                "last_seen_at": datetime.now(UTC).isoformat(),
            }
            metadata["participants"] = participants
        self._chat_sessions.set_metadata(route.agent_id, route.session_id, metadata)

    # -- Queue / workers --------------------------------------------------------------

    def _enqueue_observed_message(self, conversation: ConversationFacts, note: str) -> None:
        self._enqueue_chat_work(
            conversation.chat_id,
            _QueuedObservedMessage(conversation=conversation, note=note),
        )

    def _enqueue_chat_work(self, platform_target: str, queued: _QueuedWork) -> None:
        queue = self._chat_queues.get(platform_target)
        if queue is None:
            queue = asyncio.Queue()
            self._chat_queues[platform_target] = queue

        queue.put_nowait(queued)

        worker = self._chat_workers.get(platform_target)
        if worker is None or worker.done():
            worker = asyncio.create_task(
                self._run_chat_queue(platform_target, queue),
                name=f"channel:{self._config.id}:{platform_target}",
            )
            self._chat_workers[platform_target] = worker

    async def _run_chat_queue(
        self,
        platform_target: str,
        queue: asyncio.Queue[_QueuedWork],
    ) -> None:
        try:
            while True:
                queued = await queue.get()
                try:
                    await self._process_queued_work(queued)
                except Exception as error:
                    _LOGGER.error(
                        "Channel inbound processing failed (channel=%s target=%s): %s",
                        self._config.id,
                        platform_target,
                        error,
                        exc_info=(type(error), error, error.__traceback__),
                    )
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise
        finally:
            current = self._chat_workers.get(platform_target)
            if current is asyncio.current_task():
                self._chat_workers.pop(platform_target, None)

    async def _process_queued_work(self, queued: _QueuedWork) -> None:
        if isinstance(queued, _QueuedObservedMessage):
            await self._process_queued_observed_message(queued)
            return
        if isinstance(queued, _QueuedCommandAction):
            await self._handle_command_action(
                queued.action,
                queued.route,
                queued.reply_plan,
                queued.conversation_key,
            )
            return
        if isinstance(queued, _QueuedInboundMedia):
            await self._process_queued_media(queued)
            return
        await self._process_queued_message(queued)

    async def _process_queued_observed_message(self, queued: _QueuedObservedMessage) -> None:
        route, session = self._ensure_channel_session(queued.conversation)
        reply_plan = ReplyPlanFacts(
            channel_id=self._config.id,
            platform_target=queued.conversation.chat_id,
        )
        self._update_session_metadata(route, queued.conversation, reply_plan)
        # Wait for any open tool cycle on this shared session (a Run via another
        # accessor) so the observed note lands after the cycle, never inside it.
        async with self._chat_sessions.write_lock(route.agent_id, route.session_id):
            session.add_note(queued.note)

    async def _process_queued_message(self, queued: _QueuedInboundMessage) -> None:
        command_text = _command_text_from_content(queued.message.content)
        if command_text is not None and not queued.command_checked:
            dispatch_result = self._command_dispatcher.dispatch(
                queued.route.agent_id,
                queued.route.session_id,
                command_text,
            )
            if await self._handle_dispatch_result(
                dispatch_result,
                queued.route,
                queued.reply_plan,
                defer_actions=False,
                # Block content never command-dispatches and text commands are
                # eagerly checked, so this path produces no new_session action
                # today; the route.session_id fallback in _start_new_session stays
                # correct if it ever does.
                conversation_key=None,
            ):
                return

        await self._trigger_and_relay(
            queued.route,
            queued.reply_plan,
            queued.message.content,
            sender=queued.sender,
        )

    async def _process_queued_media(self, queued: _QueuedInboundMedia) -> None:
        # Per-message handling: one failing album item must not drop its siblings,
        # and every failure produces user-visible feedback instead of silence.
        content_blocks: list[ContentBlock] = []
        failure_replies: list[str] = []
        for message in queued.messages:
            try:
                content_blocks.extend(await self._transport.build_media_blocks(message))
            except Exception as error:
                _LOGGER.warning(
                    "Channel inbound media processing failed (channel=%s target=%s): %s",
                    self._config.id,
                    queued.reply_plan.platform_target,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
                failure_replies.append(_media_failure_reply(error))

        for reply in dict.fromkeys(failure_replies):
            await self._send_reply(queued.reply_plan, reply)

        if not content_blocks:
            return

        await self._trigger_and_relay(
            queued.route,
            queued.reply_plan,
            content_blocks,
            sender=queued.sender,
        )

    # -- Trigger / relay --------------------------------------------------------------

    async def _trigger_and_relay(
        self,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        content: str | list[ContentBlock],
        *,
        sender: MessageSender | None = None,
    ) -> None:
        try:
            run = await self._trigger_service.trigger_run(
                route.agent_id,
                content,
                route.session_id,
                sender=sender,
            )
        except Exception as error:
            _LOGGER.error(
                "Channel trigger run failed (channel=%s agent=%s session=%s target=%s): %s",
                reply_plan.channel_id,
                route.agent_id,
                route.session_id,
                reply_plan.platform_target,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            await self._send_reply(reply_plan, _FAILED_REPLY)
            return

        await self._relay_run_events(run, reply_plan)

    async def _relay_run_events(self, run: Run, reply_plan: ReplyPlanFacts) -> None:
        assistant_text: str | None = None
        reply: str | None = None

        async with self._transport.activity_indicator(reply_plan.platform_target):
            async for event in run.subscribe():
                if event.type == ASSISTANT_OUTPUT_EVENT:
                    extracted = _extract_assistant_output(event)
                    if extracted is not None:
                        assistant_text = extracted
                    continue

                if event.type == RUN_COMPLETED_EVENT:
                    reply = assistant_text or _EMPTY_ASSISTANT_REPLY
                    break

                if event.type == RUN_FAILED_EVENT:
                    reply = _FAILED_REPLY
                    break

                if event.type == RUN_CANCELLED_EVENT:
                    reply = _CANCELLED_REPLY
                    break

        if reply is not None:
            await self._send_reply(reply_plan, reply)

    async def _send_reply(self, reply_plan: ReplyPlanFacts, text: str) -> None:
        await self._transport.send_text(
            reply_plan.platform_target,
            text,
            reply_to_message_id=reply_plan.reply_to_message_id,
        )

    # -- Command actions --------------------------------------------------------------

    async def _handle_dispatch_result(
        self,
        dispatch_result: object,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        *,
        defer_actions: bool,
        conversation_key: str | None = None,
    ) -> bool:
        if isinstance(dispatch_result, CommandHandled):
            reply = dispatch_result.reply
            if isinstance(reply, str) and reply.strip():
                await self._send_reply(reply_plan, reply)
            return True

        if isinstance(dispatch_result, CommandAction):
            if defer_actions:
                # Command actions can run long (compact = model call, retry = full Run
                # relay). The adapter feeds updates sequentially, so they must not be
                # awaited in the update handler; the per-conversation worker owns slow work.
                self._enqueue_chat_work(
                    reply_plan.platform_target,
                    _QueuedCommandAction(
                        route=route,
                        reply_plan=reply_plan,
                        action=dispatch_result,
                        conversation_key=conversation_key,
                    ),
                )
            else:
                await self._handle_command_action(
                    dispatch_result, route, reply_plan, conversation_key
                )
            return True

        return False

    async def _handle_command_action(
        self,
        command_action: CommandAction,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        conversation_key: str | None = None,
    ) -> None:
        platform = self._transport.platform_display_name
        match command_action.name:
            case "compact":
                try:
                    async with self._transport.activity_indicator(reply_plan.platform_target):
                        reply = await self._trigger_service.compact_session(
                            route.agent_id,
                            route.session_id,
                            command_action.argument,
                        )
                except Exception as error:
                    self._log_command_action_failure(command_action.name, route, reply_plan, error)
                    reply = _FAILED_REPLY
                await self._send_reply(reply_plan, reply)
            case "new_session":
                await self._start_new_session(route, reply_plan, conversation_key)
            case "retry_last_turn":
                try:
                    run = await self._trigger_service.retry_run(
                        route.agent_id,
                        route.session_id,
                    )
                except Exception as error:
                    self._log_command_action_failure(command_action.name, route, reply_plan, error)
                    await self._send_reply(reply_plan, _FAILED_REPLY)
                    return
                await self._relay_run_events(run, reply_plan)
            case _:
                # Recognized commands without a channel implementation (e.g. /handoff)
                # must reply instead of silently swallowing the message.
                await self._send_reply(
                    reply_plan,
                    f"This command is not available from {platform} channels yet.",
                )

    async def _start_new_session(
        self,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        conversation_key: str | None,
    ) -> None:
        """Start a fresh channel session and point this chat's anchor at it.

        The previous session is left untouched (saved, still searchable); only the
        anchor's ``active_session_id`` pointer moves, so all later traffic for this
        chat routes into the new session. Mirrors the WebUI ``/new``, including the
        refusal while a run is active.
        """
        if self._trigger_service.has_active_run(route.agent_id, route.session_id):
            await self._send_reply(reply_plan, _NEW_SESSION_BUSY_REPLY)
            return

        # conversation_key is the stable anchor; route.session_id is only the
        # currently-active session (used for the run guard above). Falling back to
        # route.session_id is correct before the first /new, when they are equal.
        anchor = conversation_key or route.session_id
        new_session_id = self._create_fresh_channel_session(route.agent_id, anchor, reply_plan)
        self._set_active_session_pointer(route.agent_id, anchor, new_session_id)
        await self._send_reply(reply_plan, _NEW_SESSION_STARTED_REPLY)

    def _create_fresh_channel_session(
        self,
        agent_id: str,
        anchor: str,
        reply_plan: ReplyPlanFacts,
    ) -> str:
        """Create and tag a brand-new channel session, returning its id.

        The id is anchored to the conversation for readability/grouping in the
        sessions list, falling back to a bare uuid when the anchored form would
        exceed the session-id length contract. The fresh session gets the one-time
        channel reminder note and the base channel sidecar metadata (no
        participants), so it is recognizable as a channel session immediately.
        """
        candidate = f"{anchor}-{uuid4().hex}"
        new_session_id = candidate if SESSION_ID_PATTERN.fullmatch(candidate) else uuid4().hex
        session = self._chat_sessions.get_or_create(agent_id, new_session_id)
        session.add_note(
            channel_system_reminder(
                platform_display_name=self._transport.platform_display_name,
                channel_id=self._config.id,
                chat_id=reply_plan.platform_target,
            )
        )
        self._chat_sessions.set_metadata(
            agent_id,
            new_session_id,
            {
                "source_channel_id": self._config.id,
                "platform": self._config.platform,
                "platform_conv_id": reply_plan.platform_target,
                "last_reply_target": {
                    "channel_id": reply_plan.channel_id,
                    "platform_target": reply_plan.platform_target,
                },
            },
        )
        return new_session_id

    def _set_active_session_pointer(self, agent_id: str, anchor: str, new_session_id: str) -> None:
        """Point the conversation anchor at the newest session (single hop).

        Read-modify-write on the anchor's sidecar so the anchor's other channel
        metadata is preserved. The anchor normally already exists (it was the
        active session); the get_or_create is a defensive floor for the rare case
        where it does not.
        """
        try:
            metadata = self._chat_sessions.get_metadata(agent_id, anchor)
        except ChatSessionError:
            self._chat_sessions.get_or_create(agent_id, anchor)
            metadata = {}
        metadata[ACTIVE_SESSION_METADATA_KEY] = new_session_id
        self._chat_sessions.set_metadata(agent_id, anchor, metadata)

    def _log_command_action_failure(
        self,
        action_name: str,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        error: Exception,
    ) -> None:
        _LOGGER.error(
            "Channel command action failed (action=%s channel=%s agent=%s session=%s "
            "target=%s): %s",
            action_name,
            reply_plan.channel_id,
            route.agent_id,
            route.session_id,
            reply_plan.platform_target,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )

    # -- Lifecycle --------------------------------------------------------------------

    async def stop(self) -> None:
        """Cancel all per-conversation workers and await their cancellation."""
        workers = list(self._chat_workers.values())
        self._chat_workers.clear()
        self._chat_queues.clear()
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)


def _command_text_from_content(content: str | list[ContentBlock]) -> str | None:
    if isinstance(content, str):
        return content
    return None


def _format_observed_message(conversation: ConversationFacts, text: str) -> str:
    display_name = _sanitize_sender_tag_part(conversation.user_display_name or conversation.user_id)
    sender_id = _sanitize_sender_tag_part(conversation.user_id)
    return f"{CHANNEL_MESSAGE_NOTE_PREFIX}{display_name} ({sender_id}): {text}"


def _sanitize_sender_tag_part(value: str) -> str:
    sanitized = value.translate(_SENDER_TAG_UNSAFE_CHARACTERS).strip()
    return sanitized or "unknown"


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


def _media_failure_reply(error: Exception) -> str:
    """Map a media-ingest failure to user-facing reply text without leaking internals."""
    if isinstance(error, AttachmentTypeNotAllowedError):
        return _UNSUPPORTED_FILE_REPLY
    if isinstance(error, AttachmentTooLargeError):
        return _FILE_TOO_LARGE_REPLY
    return _MEDIA_FAILED_REPLY


__all__ = ["ChannelConversationEngine", "ConversationTransport"]
