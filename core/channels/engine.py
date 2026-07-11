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
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, replace
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
    WaitingWorkAdmission,
    WaitingWorkLimitError,
)
from core.sessions.sessions import CHANNEL_MESSAGE_NOTE_PREFIX, SESSION_ID_PATTERN
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.channels.channels import ChannelConfig
    from core.extensions.interactions import InteractionEvent
    from core.runs import Run, RunEvent
    from core.sessions import ChatSession, ChatSessionManager

_LOGGER = get_logger("channels.engine")

_FAILED_REPLY = "Sorry, I couldn't complete that request. Please try again."
_CANCELLED_REPLY = "Sorry, this request was cancelled before completion."
_EMPTY_ASSISTANT_REPLY = "I finished processing your message, but no reply text was produced."
_UNSUPPORTED_FILE_REPLY = "Sorry, this file type isn't supported yet."
_FILE_TOO_LARGE_REPLY = "Sorry, this file is too large to process."
_MEDIA_FAILED_REPLY = "Sorry, I couldn't process the attached file. Please try again."
_BUSY_REPLY = "I'm busy with earlier messages. Please try again shortly."
_SENDER_TAG_UNSAFE_CHARACTERS = str.maketrans("", "", "[]|\r\n")

CHANNEL_WAITING_WORK_LIMIT = 8
BUSY_REPLY_COOLDOWN_SECONDS = 30
BUSY_REPLY_TRACKING_LIMIT = 512

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
        thread_id: str | None = None,
    ) -> None:
        """Deliver one outbound text reply, optionally referencing a message/thread."""

    def activity_indicator(
        self,
        platform_target: str,
        thread_id: str | None = None,
    ) -> contextlib.AbstractAsyncContextManager[None]:
        """Show a best-effort activity indicator for a target until the block exits."""

    async def build_media_blocks(self, raw_message: Any) -> list[ContentBlock]:
        """Convert one raw platform message into canonical content blocks."""

    def caption_text(self, raw_message: Any) -> str | None:
        """Extract caption text from one raw platform message for gating checks."""


# Queued work carries the ConversationFacts, not a resolved route: the routed session
# is resolved in the per-conversation worker at processing time. Resolving at enqueue
# time would pin messages to a session that a queued /new ahead of them is about to
# move off the conversation anchor (observed messages always resolved late already).
@dataclass(slots=True, frozen=True)
class _QueuedInboundMessage:
    conversation: ConversationFacts
    message: MessageFacts
    admission: WaitingWorkAdmission | None = None


@dataclass(slots=True, frozen=True)
class _QueuedCommandAction:
    conversation: ConversationFacts
    action: CommandAction
    admission: WaitingWorkAdmission | None = None


@dataclass(slots=True, frozen=True)
class _QueuedInboundMedia:
    conversation: ConversationFacts
    # Raw platform messages; conversion to content blocks happens in the per-conversation
    # worker via the transport so the adapter's update pipeline never blocks.
    messages: tuple[Any, ...]
    admission: WaitingWorkAdmission | None = None


@dataclass(slots=True, frozen=True)
class _QueuedObservedMessage:
    conversation: ConversationFacts
    note: str
    admission: WaitingWorkAdmission | None = None


@dataclass(slots=True, frozen=True)
class _QueuedInternalPrompt:
    conversation: ConversationFacts
    prompt: str
    admission: WaitingWorkAdmission | None = None


_QueuedWork = (
    _QueuedInboundMessage
    | _QueuedCommandAction
    | _QueuedInboundMedia
    | _QueuedObservedMessage
    | _QueuedInternalPrompt
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
        self._busy_reply_times: OrderedDict[str, float] = OrderedDict()

    # -- Inbound entry points ---------------------------------------------------------

    async def handle_inbound_text(
        self,
        conversation: ConversationFacts,
        message_text: str,
    ) -> None:
        """Gate, eagerly command-dispatch, and enqueue one inbound text message."""
        if self._command_dispatcher.recognizes(message_text):
            # Commands are inherently addressed; group commands are gated by sender
            # authorization instead of response mode. The check must run before
            # dispatch() because dispatch executes handler side effects (e.g. /stop
            # cancels a Run). Commands dispatch eagerly against the session that is
            # active on arrival, so /stop can cancel a Run a queued item waits on.
            if conversation.kind == "group" and not self._command_sender_authorized(conversation):
                _LOGGER.info(
                    "Channel command denied for non-owner (channel=%s chat=%s user=%s)",
                    self._config.id,
                    conversation.chat_id,
                    conversation.user_id,
                )
                return
            route, reply_plan = self.prepare_inbound_route(conversation)
            command_result = self._command_dispatcher.dispatch(
                route.agent_id,
                route.session_id,
                message_text,
            )
            if await self._handle_dispatch_result(
                command_result, conversation, route, reply_plan, defer_actions=True
            ):
                return

        if not self.should_respond(conversation, (message_text,)):
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

        if not self._enqueue_chat_work(
            conversation.chat_id,
            _QueuedInboundMessage(
                conversation=conversation,
                message=MessageFacts(content=message_text),
            ),
        ):
            await self._reject_overflow(conversation)

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

        if not self._enqueue_chat_work(
            conversation.chat_id,
            _QueuedInboundMedia(
                conversation=conversation,
                messages=tuple(raw_messages),
            ),
        ):
            await self._reject_overflow(conversation)

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

    async def trigger_internal_reply(self, conversation: ConversationFacts, prompt: str) -> bool:
        """Queue an internal note-driven Run whose reply goes back to the conversation.

        Platform rituals such as Telegram's ``/start`` use this: the prompt is
        persisted as a kernel-internal note (never a visible user message), the model
        acts on it, and its reply is relayed like any other channel answer.
        """
        if self._enqueue_chat_work(
            conversation.chat_id,
            _QueuedInternalPrompt(conversation=conversation, prompt=prompt),
        ):
            return True
        await self._reject_overflow(conversation)
        return False

    async def trigger_interaction_reply(
        self,
        conversation: ConversationFacts,
        event: InteractionEvent,
    ) -> bool:
        """Wake the agent from a run-triggering button tap (reserved ``run:`` prefix).

        Enqueues an internal note-driven Run (the same path as
        :meth:`trigger_internal_reply`) carrying the tap context — the tapped
        button plus the message's current keyboard state — so the agent can act on
        it and confirm in the chat. Group taps are gated by ``owner_user_ids``
        exactly like group commands. Returns ``True`` when the tap was authorized
        and enqueued, ``False`` when a non-owner group tap was logged and dropped;
        the adapter has already acked the tap, and closes the keyboard only on a
        ``True`` (an unauthorized tapper must not close the shared message).
        """
        if not self._command_sender_authorized(conversation):
            _LOGGER.info(
                "Run-triggering tap denied for non-owner (channel=%s chat=%s user=%s)",
                self._config.id,
                conversation.chat_id,
                conversation.user_id,
            )
            return False
        return await self.trigger_internal_reply(
            conversation, _format_interaction_note(conversation, event)
        )

    def prepare_inbound_route(
        self,
        conversation: ConversationFacts,
    ) -> tuple[RouteFacts, ReplyPlanFacts]:
        """Ensure the routed Session exists and refresh its channel metadata."""
        route, _session = self._ensure_channel_session(conversation)
        reply_plan = self._reply_plan_for(conversation)
        self._update_session_metadata(route, conversation, reply_plan)
        return route, reply_plan

    def _reply_plan_for(self, conversation: ConversationFacts) -> ReplyPlanFacts:
        """Build a reply target without creating or changing a Session."""
        return ReplyPlanFacts(
            channel_id=self._config.id,
            platform_target=conversation.chat_id,
            # Group replies reference the triggering message so it is clear which
            # message the bot answers; DM replies stay plain.
            reply_to_message_id=(conversation.message_id if conversation.kind == "group" else None),
            # Replies follow the message into its thread/topic where the platform
            # models topics inside one chat (Telegram forum topics).
            thread_id=conversation.thread_id,
        )

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

    async def migrate_group_conversation(self, old_chat_id: str, new_chat_id: str) -> bool:
        """Repoint a group conversation anchor after a platform chat-id migration.

        Some platforms change a group's chat id in place (Telegram: group →
        supergroup upgrade). The old anchor's active session keeps the full
        history; the new chat id's anchor gets an ``active_session_id`` pointer at
        it (single hop), the session's channel sidecar moves to the new chat id
        (so proactive sends target the live chat), and a note tells the model.
        Returns False when the old conversation has no session to bridge.
        """
        agent_id = self._config.agent_id
        old_anchor = self._group_conversation_key(old_chat_id)
        active_session_id = self._resolve_active_session_id(agent_id, old_anchor)
        if not self._chat_sessions.exists(agent_id, active_session_id):
            return False

        new_anchor = self._group_conversation_key(new_chat_id)
        self._set_active_session_pointer(agent_id, new_anchor, active_session_id)
        conversation = ConversationFacts(
            platform=self._config.platform,
            channel_id=self._config.id,
            chat_id=new_chat_id,
            user_id=new_chat_id,
            kind="group",
        )
        self._update_session_metadata(
            RouteFacts(agent_id=agent_id, session_id=active_session_id),
            conversation,
            ReplyPlanFacts(channel_id=self._config.id, platform_target=new_chat_id),
            track_participant=False,
        )
        async with self._chat_sessions.write_lock(agent_id, active_session_id):
            session = self._chat_sessions.get_or_create(agent_id, active_session_id)
            session.add_note(
                f"This group chat was migrated by the platform to a new chat id "
                f"(old: {old_chat_id}, new: {new_chat_id}). The conversation continues here."
            )
        return True

    def _derive_session_id(self, conversation: ConversationFacts) -> str:
        # Group conversations share one session keyed by chat id and ignore dm_scope.
        if conversation.kind == "group":
            return self._group_conversation_key(conversation.chat_id)

        scope = self._config.dm_scope
        if scope == "main":
            return f"ch-{self._config.id}-main"
        if scope == "per_peer":
            return f"ch-{self._config.id}-u{conversation.user_id}"
        if scope == "per_account_channel_peer":
            return f"ch-{self._config.id}-{conversation.chat_id}-u{conversation.user_id}"
        return f"ch-{self._config.id}-{conversation.chat_id}"

    def _group_conversation_key(self, chat_id: str) -> str:
        return f"ch-{self._config.id}-{chat_id}"

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
        last_reply_target: dict[str, Any] = {
            "channel_id": reply_plan.channel_id,
            "platform_target": reply_plan.platform_target,
        }
        # The thread key is present only while the conversation lives in a topic; a
        # later non-topic message rewrites the dict without it (last-target semantics).
        if reply_plan.thread_id is not None:
            last_reply_target["thread_id"] = reply_plan.thread_id
        metadata.update(
            {
                "source_channel_id": self._config.id,
                "platform": conversation.platform,
                "platform_conv_id": conversation.chat_id,
                "last_reply_target": last_reply_target,
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
        if self._enqueue_chat_work(
            conversation.chat_id,
            _QueuedObservedMessage(conversation=conversation, note=note),
        ):
            return
        _LOGGER.warning(
            "Observed channel context rejected by queue limit (channel=%s target=%s)",
            self._config.id,
            conversation.chat_id,
        )

    def _enqueue_chat_work(self, platform_target: str, queued: _QueuedWork) -> bool:
        """Admit one channel item before it reaches the per-chat FIFO.

        The Run manager owns the bounded waiting-work accounting. This ingress
        FIFO retains only already-admitted items so it can preserve a channel's
        arrival order and defer media downloads until after admission.
        """
        try:
            admission = self._trigger_service.reserve_waiting_work(
                scope=self._waiting_scope(platform_target),
                scope_limit=CHANNEL_WAITING_WORK_LIMIT,
            )
        except WaitingWorkLimitError:
            return False

        queue = self._chat_queues.get(platform_target)
        if queue is None:
            queue = asyncio.Queue()
            self._chat_queues[platform_target] = queue

        queue.put_nowait(replace(queued, admission=admission))

        worker = self._chat_workers.get(platform_target)
        if worker is None or worker.done():
            worker = asyncio.create_task(
                self._run_chat_queue(platform_target, queue),
                name=f"channel:{self._config.id}:{platform_target}",
            )
            self._chat_workers[platform_target] = worker
        return True

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
                    self._trigger_service.release_waiting_work(queued.admission)
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
            # Deferred actions re-resolve at processing time like every other queued
            # item: a /new that ran ahead of this action in the queue has moved the
            # pointer by now, and e.g. /compact must act on the now-active session.
            self._trigger_service.release_waiting_work(queued.admission)
            route, reply_plan = self.prepare_inbound_route(queued.conversation)
            await self._handle_command_action(
                queued.action,
                route,
                reply_plan,
                self._derive_session_id(queued.conversation),
            )
            return
        if isinstance(queued, _QueuedInboundMedia):
            await self._process_queued_media(queued)
            return
        if isinstance(queued, _QueuedInternalPrompt):
            route, reply_plan = self.prepare_inbound_route(queued.conversation)
            await self._trigger_and_relay(
                route,
                reply_plan,
                queued.prompt,
                internal=True,
                waiting_work_admission=queued.admission,
            )
            return
        await self._process_queued_message(queued)

    async def _process_queued_observed_message(self, queued: _QueuedObservedMessage) -> None:
        self._trigger_service.release_waiting_work(queued.admission)
        route, session = self._ensure_channel_session(queued.conversation)
        reply_plan = ReplyPlanFacts(
            channel_id=self._config.id,
            platform_target=queued.conversation.chat_id,
            thread_id=queued.conversation.thread_id,
        )
        self._update_session_metadata(route, queued.conversation, reply_plan)
        # Wait for any open tool cycle on this shared session (a Run via another
        # accessor) so the observed note lands after the cycle, never inside it.
        async with self._chat_sessions.write_lock(route.agent_id, route.session_id):
            session.add_note(queued.note)

    async def _process_queued_message(self, queued: _QueuedInboundMessage) -> None:
        # Commands were already dispatched eagerly on arrival; only plain messages
        # reach this queue, so processing goes straight to trigger/relay.
        route, reply_plan = self.prepare_inbound_route(queued.conversation)
        await self._trigger_and_relay(
            route,
            reply_plan,
            queued.message.content,
            sender=self._sender_for(queued.conversation),
            waiting_work_admission=queued.admission,
        )

    async def _process_queued_media(self, queued: _QueuedInboundMedia) -> None:
        route, reply_plan = self.prepare_inbound_route(queued.conversation)
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
                    reply_plan.platform_target,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
                failure_replies.append(_media_failure_reply(error))

        for reply in dict.fromkeys(failure_replies):
            await self._send_reply(reply_plan, reply)

        if not content_blocks:
            return

        await self._trigger_and_relay(
            route,
            reply_plan,
            content_blocks,
            sender=self._sender_for(queued.conversation),
            waiting_work_admission=queued.admission,
        )

    # -- Trigger / relay --------------------------------------------------------------

    async def _trigger_and_relay(
        self,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        content: str | list[ContentBlock],
        *,
        sender: MessageSender | None = None,
        internal: bool = False,
        waiting_work_admission: WaitingWorkAdmission | None = None,
    ) -> None:
        _LOGGER.info(
            "Channel message routed (channel=%s target=%s agent=%s session=%s%s)",
            reply_plan.channel_id,
            reply_plan.platform_target,
            route.agent_id,
            route.session_id,
            " internal" if internal else "",
        )
        try:
            # An internal run persists the content as a kernel note instead of a
            # visible user message; it never carries a sender.
            if internal:
                if waiting_work_admission is None:
                    run = await self._trigger_service.trigger_run(
                        route.agent_id,
                        content,
                        route.session_id,
                        internal=True,
                    )
                else:
                    run = await self._trigger_service.trigger_run(
                        route.agent_id,
                        content,
                        route.session_id,
                        internal=True,
                        waiting_work_admission=waiting_work_admission,
                    )
            else:
                if waiting_work_admission is None:
                    run = await self._trigger_service.trigger_run(
                        route.agent_id,
                        content,
                        route.session_id,
                        sender=sender,
                    )
                else:
                    run = await self._trigger_service.trigger_run(
                        route.agent_id,
                        content,
                        route.session_id,
                        sender=sender,
                        waiting_work_admission=waiting_work_admission,
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

        async with self._transport.activity_indicator(
            reply_plan.platform_target, reply_plan.thread_id
        ):
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
            thread_id=reply_plan.thread_id,
        )

    def _waiting_scope(self, platform_target: str) -> str:
        return f"{self._config.id}:{platform_target}"

    async def _reject_overflow(self, conversation: ConversationFacts) -> None:
        """Log one rejected inbound item and send a throttled busy reply."""
        _LOGGER.warning(
            "Channel inbound work rejected by queue limit (channel=%s target=%s)",
            self._config.id,
            conversation.chat_id,
        )
        if not self._should_send_busy_reply(conversation.chat_id):
            _LOGGER.debug(
                "Channel busy reply throttled (channel=%s target=%s)",
                self._config.id,
                conversation.chat_id,
            )
            return
        await self._send_reply(self._reply_plan_for(conversation), _BUSY_REPLY)

    def _should_send_busy_reply(self, platform_target: str) -> bool:
        now = time.monotonic()
        last_reply_at = self._busy_reply_times.get(platform_target)
        if last_reply_at is not None and now - last_reply_at < BUSY_REPLY_COOLDOWN_SECONDS:
            self._busy_reply_times.move_to_end(platform_target)
            return False

        self._busy_reply_times[platform_target] = now
        self._busy_reply_times.move_to_end(platform_target)
        if len(self._busy_reply_times) > BUSY_REPLY_TRACKING_LIMIT:
            self._busy_reply_times.popitem(last=False)
        return True

    # -- Command actions --------------------------------------------------------------

    async def _handle_dispatch_result(
        self,
        dispatch_result: object,
        conversation: ConversationFacts,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        *,
        defer_actions: bool,
    ) -> bool:
        if isinstance(dispatch_result, CommandHandled):
            reply = dispatch_result.reply
            if isinstance(reply, str) and reply.strip():
                await self._send_reply(reply_plan, reply)
            return True

        if isinstance(dispatch_result, CommandAction):
            if defer_actions:
                # Command actions can run long (compact = model call, continue = full Run
                # relay). The adapter feeds updates sequentially, so they must not be
                # awaited in the update handler; the per-conversation worker owns slow work.
                if not self._enqueue_chat_work(
                    conversation.chat_id,
                    _QueuedCommandAction(
                        conversation=conversation,
                        action=dispatch_result,
                    ),
                ):
                    await self._reject_overflow(conversation)
            else:
                await self._handle_command_action(
                    dispatch_result, route, reply_plan, self._derive_session_id(conversation)
                )
            return True

        return False

    async def _handle_command_action(
        self,
        command_action: CommandAction,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        conversation_key: str,
    ) -> None:
        platform = self._transport.platform_display_name
        match command_action.name:
            case "compact":
                try:
                    async with self._transport.activity_indicator(
                        reply_plan.platform_target, reply_plan.thread_id
                    ):
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
            case "continue":
                try:
                    run = await self._trigger_service.continue_run(
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
        conversation_key: str,
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
        # currently-active session (used for the run guard above).
        new_session_id = self._create_fresh_channel_session(
            route.agent_id, conversation_key, reply_plan
        )
        self._set_active_session_pointer(route.agent_id, conversation_key, new_session_id)
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
        for queue in self._chat_queues.values():
            while not queue.empty():
                queued = queue.get_nowait()
                self._trigger_service.release_waiting_work(queued.admission)
                queue.task_done()
        self._chat_queues.clear()
        self._busy_reply_times.clear()
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)


def _format_observed_message(conversation: ConversationFacts, text: str) -> str:
    display_name = _sanitize_sender_tag_part(conversation.user_display_name or conversation.user_id)
    sender_id = _sanitize_sender_tag_part(conversation.user_id)
    return f"{CHANNEL_MESSAGE_NOTE_PREFIX}{display_name} ({sender_id}): {text}"


def _format_interaction_note(conversation: ConversationFacts, event: InteractionEvent) -> str:
    """Render the neutral kernel note for a run-triggering button tap.

    Content-agnostic: it calls out the tapped button and then lists every current
    button's label and callback data verbatim in row order, so the agent can read
    the whole keyboard state — e.g. which items a skill marked ✅ — from the note
    alone, with no server-side store. Any glyph or id convention inside the labels
    or data is the skill's interpretation, never the engine's.
    """
    lines = [
        "A channel button was tapped and is asking you to act on it.",
        f'Tapped button: "{_tapped_button_label(event)}" ({event.data})',
    ]
    if conversation.kind == "group":
        lines.append(f"Tapped by: {conversation.user_display_name or conversation.user_id}")
    lines.append("Current buttons on the message (top to bottom, left to right):")
    lines.extend(f'- "{button.label}" ({button.data})' for row in event.buttons for button in row)
    lines.append("Act on the current button state, then confirm in this chat.")
    return "\n".join(lines)


def _tapped_button_label(event: InteractionEvent) -> str:
    """The label of the button whose data was tapped, or the raw data as fallback."""
    for row in event.buttons:
        for button in row:
            if button.data == event.data:
                return button.label
    return event.data


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
