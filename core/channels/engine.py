"""Platform-neutral conversation engine for channel adapters.

The engine owns everything about a channel conversation that is not specific to one
messaging platform: per-conversation queueing and worker serialization, neutral command
projection, run trigger/relay, and session routing/metadata. A `ChannelAdapter`
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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from core.attachments import AttachmentTooLargeError, AttachmentTypeNotAllowedError
from core.channels.adapter import (
    ChannelAccessRegistry,
    ConversationFacts,
    MessageFacts,
    QuotedMessageFacts,
    ReplyPlanFacts,
    RouteFacts,
    RunButtonBinding,
    RunButtonBindingRegistry,
    parse_bound_run_callback_data,
)
from core.chat.commands import (
    CommandDispatcher,
    CommandExecutionContext,
    CommandOutcome,
    CommandUnavailability,
    PreparedCommand,
)
from core.chat.content_blocks import ContentBlock, TextBlock
from core.chat.errors import ChatSessionError
from core.chat.messages import GroupRole, MessageSender, ReplySurface
from core.runs import (
    ASSISTANT_OUTPUT_EVENT,
    COMPACTION_COMPLETED_EVENT,
    RUN_CANCELLED_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
    RUN_INTERRUPTED_EVENT,
    RunKind,
    WaitingWorkAdmission,
    WaitingWorkLimitError,
)
from core.sessions.sessions import (
    CHANNEL_MESSAGE_NOTE_PREFIX,
    SessionAddress,
)
from core.utils.logging import get_logger
from core.utils.retry import retry_async
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.channels.channels import ChannelConfig
    from core.extensions.interactions import InteractionEvent
    from core.runs import Run, RunEvent
    from core.sessions import ChatSession, ChatSessionManager

_LOGGER = get_logger("channels.engine")
_CHANNEL_SESSION_WORKERS = BoundedWorkerPool(name="channel-session", max_workers=4)

_FAILED_REPLY = "Sorry, I couldn't complete that request. Please try again."


def _session_address(agent_id: str, session_id: str) -> SessionAddress:
    """Address one Channel Session (channels always run on Identity Agents)."""
    return SessionAddress(project_id=None, agent_id=agent_id, session_id=session_id)


_CANCELLED_REPLY = "Sorry, this request was cancelled before completion."
_INTERRUPTED_REPLY = "Sorry, this request was interrupted before it could finish."
_EMPTY_ASSISTANT_REPLY = "I finished processing your message, but no reply text was produced."
_UNSUPPORTED_FILE_REPLY = "Sorry, this file type isn't supported yet."
_FILE_TOO_LARGE_REPLY = "Sorry, this file is too large to process."
_MEDIA_FAILED_REPLY = "Sorry, I couldn't process the attached file. Please try again."
_MEDIA_DOWNLOAD_FAILED_REPLY = (
    "Sorry, the messaging platform couldn't download the attached file after several "
    "attempts. Please resend it."
)
_BUSY_REPLY = "I'm busy with earlier messages. Please try again shortly."
_MEMBER_TOOL_NAMES = ("web_search", "web_fetch")
_MEMBER_TOOL_DENIAL = (
    "Tool access denied: the current sender is a group member. "
    "Group members may use only web_search and web_fetch."
)
_QUOTED_MESSAGE_PREFIX = "[quoted-message]"
_QUOTED_MESSAGE_UNAVAILABLE = "[quoted-message unavailable]"
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
InteractionTriggerStatus = Literal[
    "enqueued",
    "denied",
    "busy",
    "already_handled",
    "unavailable",
]


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

    async def build_quoted_message(self, raw_message: Any) -> QuotedMessageFacts | None:
        """Resolve attachment content from the message referenced by ``raw_message``.

        Called only after the triggering message passed response gating and Queue
        admission, so adapters may perform metadata fetches and attachment downloads.
        """

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
    # The platform trigger stays opaque until the worker may resolve a replied-to
    # attachment after response gating and waiting-work admission.
    raw_message: Any | None = None
    admission: WaitingWorkAdmission | None = None


@dataclass(slots=True, frozen=True)
class _QueuedPreparedCommand:
    conversation: ConversationFacts
    command: PreparedCommand
    admission: WaitingWorkAdmission | None = None


@dataclass(slots=True, frozen=True)
class _QueuedInboundMedia:
    conversation: ConversationFacts
    # Raw platform messages; conversion to content blocks happens in the per-conversation
    # worker via the transport so the adapter's update pipeline never blocks.
    messages: tuple[Any, ...]
    # Some platforms deliver a user's comment and the media it introduces as separate
    # transport messages. Adapters may reunite that comment with the media before queueing.
    companion_text: str | None = None
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
    | _QueuedPreparedCommand
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
        run_button_binding_registry: RunButtonBindingRegistry | None = None,
        access_registry: ChannelAccessRegistry | None = None,
    ) -> None:
        self._config = config
        self._trigger_service = trigger_service
        self._chat_sessions = chat_sessions
        self._transport = transport
        self._command_dispatcher = command_dispatcher
        self._run_button_binding_registry = run_button_binding_registry
        self._access_registry = access_registry
        # Config validation guarantees the patterns compile.
        self._mention_patterns = tuple(
            re.compile(pattern, re.IGNORECASE) for pattern in config.mention_patterns
        )
        self._chat_queues: dict[str, asyncio.Queue[_QueuedWork]] = {}
        self._chat_workers: dict[str, asyncio.Task[None]] = {}
        self._busy_reply_times: OrderedDict[str, float] = OrderedDict()

    # -- Inbound entry points ---------------------------------------------------------

    def is_command(self, message_text: str) -> bool:
        """Return whether Chat recognizes text as a live Command without executing it."""
        return self._command_dispatcher.prepare(message_text) is not None

    async def handle_inbound_text(
        self,
        conversation: ConversationFacts,
        message_text: str,
        *,
        raw_message: Any | None = None,
    ) -> None:
        """Gate one inbound text and execute or enqueue a prepared command/message."""
        conversation = self._snapshot_group_sender(conversation)
        prepared_command = self._command_dispatcher.prepare(message_text)
        if prepared_command is not None:
            # Commands are inherently addressed; group commands are gated by sender
            # authorization instead of response mode. Authorization precedes both
            # availability projection and every command side effect.
            if conversation.kind == "group" and not self._command_sender_authorized(conversation):
                _LOGGER.info(
                    "Channel command denied for member (channel=%s chat=%s user=%s)",
                    self._config.id,
                    conversation.chat_id,
                    conversation.user_id,
                )
                return
            reply_plan = self._reply_plan_for(conversation)
            unavailable = self._command_dispatcher.unavailability(
                prepared_command, self._reply_surface(conversation.kind)
            )
            if unavailable is not None:
                await self._send_command_unavailability(reply_plan, unavailable)
                return
            if prepared_command.execution_mode == "immediate":
                route, reply_plan = await self._prepare_inbound_route_async(conversation)
                await self._execute_prepared_command(
                    prepared_command,
                    conversation,
                    route,
                    reply_plan,
                    self._derive_session_id(conversation),
                )
                return
            if not self._enqueue_chat_work(
                conversation.chat_id,
                _QueuedPreparedCommand(
                    conversation=conversation,
                    command=prepared_command,
                ),
            ):
                await self._reject_overflow(conversation)
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
                raw_message=raw_message,
            ),
        ):
            await self._reject_overflow(conversation)

    async def handle_inbound_media(
        self,
        conversation: ConversationFacts,
        raw_messages: tuple[Any, ...],
        *,
        companion_text: str | None = None,
    ) -> None:
        """Gate, route, and enqueue inbound media (one message or a buffered album)."""
        conversation = self._snapshot_group_sender(conversation)
        normalized_companion = companion_text.strip() if companion_text is not None else None
        if normalized_companion == "":
            normalized_companion = None
        caption_texts = tuple(self._transport.caption_text(message) for message in raw_messages)
        gating_texts = (
            *((normalized_companion,) if normalized_companion is not None else ()),
            *caption_texts,
        )
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
                companion_text=normalized_companion,
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
        conversation = self._snapshot_group_sender(conversation)
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
        conversation = self._snapshot_group_sender(conversation)
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
    ) -> InteractionTriggerStatus:
        """Wake the agent from a run-triggering button tap (reserved ``run:`` prefix).

        A ``channel_send``-bound tap atomically claims its durable origin, points
        the Channel conversation at that Session, then enters the same per-chat
        FIFO as following messages. Legacy unbound ``run:<payload>`` buttons keep
        routing to the Channel's current active Session.
        """
        conversation = self._snapshot_group_sender(conversation)
        if not self._command_sender_authorized(conversation):
            _LOGGER.info(
                "Run-triggering tap denied for member (channel=%s chat=%s user=%s)",
                self._config.id,
                conversation.chat_id,
                conversation.user_id,
            )
            return "denied"

        parsed_binding = parse_bound_run_callback_data(event.data)
        if parsed_binding is None:
            enqueued = await self.trigger_internal_reply(
                conversation, _format_interaction_note(conversation, event)
            )
            return "enqueued" if enqueued else "busy"

        registry = self._run_button_binding_registry
        if registry is None:
            return "unavailable"
        binding_id, button_index = parsed_binding
        claim = await _CHANNEL_SESSION_WORKERS.run(
            registry.claim_run_button_binding,
            self._config.id,
            binding_id,
            platform_target=conversation.chat_id,
            thread_id=conversation.thread_id,
        )
        if claim.status == "consumed":
            return "already_handled"
        if claim.status != "claimed" or claim.binding is None:
            return "unavailable"

        binding = claim.binding
        restored_event = _restore_bound_interaction_event(binding, event, button_index)
        origin_exists = await _CHANNEL_SESSION_WORKERS.run(
            self._chat_sessions.exists,
            _session_address(self._config.agent_id, binding.origin_session_id),
        )
        if restored_event is None or not origin_exists:
            return "unavailable"

        try:
            previous_anchor_metadata = await _CHANNEL_SESSION_WORKERS.run(
                self._point_conversation_at_session,
                conversation,
                binding.origin_session_id,
            )
        except Exception:
            await _CHANNEL_SESSION_WORKERS.run(
                registry.restore_run_button_binding,
                self._config.id,
                binding.id,
            )
            raise
        queued = self._enqueue_chat_work(
            conversation.chat_id,
            _QueuedInternalPrompt(
                conversation=conversation,
                prompt=_format_interaction_note(conversation, restored_event),
            ),
        )
        if queued:
            return "enqueued"

        await _CHANNEL_SESSION_WORKERS.run(
            self._restore_conversation_pointer,
            conversation,
            previous_anchor_metadata,
        )
        await _CHANNEL_SESSION_WORKERS.run(
            registry.restore_run_button_binding,
            self._config.id,
            binding.id,
        )
        await self._reject_overflow(conversation)
        return "busy"

    def prepare_inbound_route(
        self,
        conversation: ConversationFacts,
    ) -> tuple[RouteFacts, ReplyPlanFacts]:
        """Ensure the routed Session exists and refresh its channel metadata."""
        route, _session = self._ensure_channel_session(conversation)
        reply_plan = self._reply_plan_for(conversation)
        self._update_session_metadata(route, conversation, reply_plan)
        return route, reply_plan

    async def _prepare_inbound_route_async(
        self,
        conversation: ConversationFacts,
    ) -> tuple[RouteFacts, ReplyPlanFacts]:
        return await _CHANNEL_SESSION_WORKERS.run(self.prepare_inbound_route, conversation)

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
        # Proactive (outbound-only) Sessions get the same channel metadata as inbound
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
        # DM commands retain their existing behavior. Group Commands use the same
        # immutable ingress role snapshot as messages and reserved Run buttons.
        if conversation.kind != "group":
            return True
        return conversation.sender_role == "admin"

    def _sender_for(self, conversation: ConversationFacts) -> MessageSender | None:
        # Sender identity is group-only in v1; DM turns stay unattributed.
        if conversation.kind != "group":
            return None
        return MessageSender(
            id=conversation.user_id,
            display_name=conversation.user_display_name or conversation.user_id,
            role=conversation.sender_role or "member",
        )

    def _snapshot_group_sender(self, conversation: ConversationFacts) -> ConversationFacts:
        """Persist and freeze one group sender's role at Channel ingress."""
        if conversation.kind != "group" or conversation.sender_role is not None:
            return conversation
        access_scope_id = conversation.access_scope_id or conversation.chat_id
        registry = self._access_registry
        role: GroupRole = (
            registry.snapshot_participant_role(
                self._config.id,
                access_scope_id,
                conversation.user_id,
                conversation.user_display_name or conversation.user_id,
            )
            if registry is not None
            else "member"
        )
        return replace(
            conversation,
            access_scope_id=access_scope_id,
            sender_role=role,
        )

    def _tool_access_for(
        self,
        conversation: ConversationFacts,
    ) -> tuple[Sequence[str] | None, Callable[[str], str | None] | None]:
        """Return the admission ceiling and live pre-dispatch denial resolver."""
        if conversation.kind != "group":
            return None, None
        admitted_role = conversation.sender_role or "member"
        access_scope_id = conversation.access_scope_id or conversation.chat_id
        user_id = conversation.user_id
        registry = self._access_registry

        def denial_resolver(tool_name: str) -> str | None:
            if tool_name in _MEMBER_TOOL_NAMES:
                return None
            current_role = (
                registry.role_for(self._config.id, access_scope_id, user_id)
                if registry is not None
                else admitted_role
            )
            if admitted_role != "admin" or current_role != "admin":
                return _MEMBER_TOOL_DENIAL
            return None

        restriction: Sequence[str] | None = (
            _MEMBER_TOOL_NAMES if admitted_role == "member" else None
        )
        return restriction, denial_resolver

    # -- Session routing / metadata ---------------------------------------------------

    def _ensure_channel_session(
        self,
        conversation: ConversationFacts,
    ) -> tuple[RouteFacts, ChatSession]:
        route = self._route_facts(conversation)
        session = self._chat_sessions.get_or_create(
            _session_address(route.agent_id, route.session_id)
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
            metadata = self._chat_sessions.get_metadata(
                _session_address(agent_id, conversation_key)
            )
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
        prepared = await _CHANNEL_SESSION_WORKERS.run(
            self._prepare_group_migration,
            old_chat_id,
            new_chat_id,
        )
        if prepared is None:
            return False
        agent_id, active_session_id = prepared
        async with self._chat_sessions.write_lock(_session_address(agent_id, active_session_id)):
            await _CHANNEL_SESSION_WORKERS.run(
                self._append_session_note,
                agent_id,
                active_session_id,
                f"This group chat was migrated by the platform to a new chat id "
                f"(old: {old_chat_id}, new: {new_chat_id}). The conversation continues here.",
            )
        return True

    def _prepare_group_migration(
        self,
        old_chat_id: str,
        new_chat_id: str,
    ) -> tuple[str, str] | None:
        agent_id = self._config.agent_id
        old_anchor = self._group_conversation_key(old_chat_id)
        active_session_id = self._resolve_active_session_id(agent_id, old_anchor)
        if not self._chat_sessions.exists(_session_address(agent_id, active_session_id)):
            return None

        new_anchor = self._group_conversation_key(new_chat_id)
        self._set_active_session_pointer(agent_id, new_anchor, active_session_id)
        conversation = ConversationFacts(
            platform=self._config.platform,
            channel_id=self._config.id,
            chat_id=new_chat_id,
            user_id=new_chat_id,
            access_scope_id=new_chat_id,
            kind="group",
        )
        self._update_session_metadata(
            RouteFacts(agent_id=agent_id, session_id=active_session_id),
            conversation,
            ReplyPlanFacts(channel_id=self._config.id, platform_target=new_chat_id),
            track_participant=False,
        )
        return agent_id, active_session_id

    def _append_session_note(self, agent_id: str, session_id: str, note: str) -> None:
        session = self._chat_sessions.get_or_create(_session_address(agent_id, session_id))
        session.add_note(note)

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

    def _update_session_metadata(
        self,
        route: RouteFacts,
        conversation: ConversationFacts,
        reply_plan: ReplyPlanFacts,
        *,
        track_participant: bool = True,
    ) -> None:
        address = _session_address(route.agent_id, route.session_id)
        last_reply_target: dict[str, Any] = {
            "channel_id": reply_plan.channel_id,
            "platform_target": reply_plan.platform_target,
        }
        # The thread key is present only while the conversation lives in a topic; a
        # later non-topic message rewrites the dict without it (last-target semantics).
        if reply_plan.thread_id is not None:
            last_reply_target["thread_id"] = reply_plan.thread_id

        def update(metadata: dict[str, Any]) -> None:
            metadata.update(
                {
                    "source_channel_id": self._config.id,
                    "platform": conversation.platform,
                    "platform_conv_id": conversation.chat_id,
                    "conversation_kind": conversation.kind,
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

        self._chat_sessions.mutate_metadata(address, update)

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
        if isinstance(queued, _QueuedPreparedCommand):
            # Serialized commands re-resolve at processing time like every other
            # queued item: an earlier navigation may have changed the active Session.
            self._trigger_service.release_waiting_work(queued.admission)
            route, reply_plan = await self._prepare_inbound_route_async(queued.conversation)
            await self._execute_prepared_command(
                queued.command,
                queued.conversation,
                route,
                reply_plan,
                self._derive_session_id(queued.conversation),
            )
            return
        if isinstance(queued, _QueuedInboundMedia):
            await self._process_queued_media(queued)
            return
        if isinstance(queued, _QueuedInternalPrompt):
            route, reply_plan = await self._prepare_inbound_route_async(queued.conversation)
            await self._trigger_and_relay(
                route,
                reply_plan,
                queued.prompt,
                conversation=queued.conversation,
                internal=True,
                waiting_work_admission=queued.admission,
            )
            return
        await self._process_queued_message(queued)

    async def _process_queued_observed_message(self, queued: _QueuedObservedMessage) -> None:
        self._trigger_service.release_waiting_work(queued.admission)
        route, _reply_plan = await self._prepare_inbound_route_async(queued.conversation)
        # Wait for any open tool cycle on this shared session (a Run via another
        # accessor) so the observed note lands after the cycle, never inside it.
        async with self._chat_sessions.write_lock(
            _session_address(route.agent_id, route.session_id)
        ):
            await _CHANNEL_SESSION_WORKERS.run(
                self._append_session_note,
                route.agent_id,
                route.session_id,
                queued.note,
            )

    async def _process_queued_message(self, queued: _QueuedInboundMessage) -> None:
        # Prepared commands have their own queued-work type; only plain messages
        # reach this path, so processing goes straight to trigger/relay.
        route, reply_plan = await self._prepare_inbound_route_async(queued.conversation)
        content: str | list[ContentBlock] = queued.message.content
        failure_reply: str | None = None
        if queued.conversation.kind == "group" and queued.raw_message is not None:
            try:
                quoted = await self._transport.build_quoted_message(queued.raw_message)
            except Exception as error:
                _LOGGER.warning(
                    "Channel quoted attachment processing failed (channel=%s target=%s): %s",
                    self._config.id,
                    reply_plan.platform_target,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
                quoted = QuotedMessageFacts(
                    user_id=None,
                    user_display_name=None,
                    content=None,
                )
                failure_reply = _media_failure_reply(error)
            if quoted is not None:
                content = self._content_with_quoted_message(queued, quoted)

        if failure_reply is not None:
            await self._send_reply(reply_plan, failure_reply)
        await self._trigger_and_relay(
            route,
            reply_plan,
            content,
            conversation=queued.conversation,
            sender=self._sender_for(queued.conversation),
            waiting_work_admission=queued.admission,
        )

    def _content_with_quoted_message(
        self,
        queued: _QueuedInboundMessage,
        quoted: QuotedMessageFacts,
    ) -> list[ContentBlock]:
        if not isinstance(queued.message.content, str):
            raise AssertionError("queued inbound text message must contain text")
        blocks: list[ContentBlock] = [TextBlock(type="text", text=queued.message.content)]
        if quoted.content is None or quoted.user_id is None:
            blocks.append(TextBlock(type="text", text=_QUOTED_MESSAGE_UNAVAILABLE))
            return blocks

        quoted_conversation = self._snapshot_group_sender(
            replace(
                queued.conversation,
                user_id=quoted.user_id,
                user_display_name=quoted.user_display_name,
                sender_role=None,
                message_id=None,
                mentioned_bot=False,
                is_reply_to_bot=False,
            )
        )
        quoted_sender = self._sender_for(quoted_conversation)
        if quoted_sender is None:
            raise AssertionError("quoted group message must have a sender")
        blocks.append(
            TextBlock(
                type="text",
                text=f"{_QUOTED_MESSAGE_PREFIX} {_sender_tag(quoted_sender)}:",
            )
        )
        blocks.extend(quoted.content)
        return blocks

    async def _process_queued_media(self, queued: _QueuedInboundMedia) -> None:
        route, reply_plan = await self._prepare_inbound_route_async(queued.conversation)
        # Per-message handling: one failing album item must not drop its siblings,
        # and every failure produces user-visible feedback instead of silence.
        content_blocks: list[ContentBlock] = []
        if queued.companion_text is not None:
            content_blocks.append(TextBlock(type="text", text=queued.companion_text))
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
            conversation=queued.conversation,
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
        conversation: ConversationFacts,
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
        tool_restriction, tool_denial_resolver = self._tool_access_for(conversation)
        tool_access_kwargs: dict[str, Any] = {}
        if tool_restriction is not None:
            tool_access_kwargs["tool_restriction"] = tool_restriction
        if tool_denial_resolver is not None:
            tool_access_kwargs["tool_denial_resolver"] = tool_denial_resolver
        reply_surface = self._reply_surface(conversation.kind)
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
                        reply_surface=reply_surface,
                        run_kind=RunKind.CHANNEL,
                        **tool_access_kwargs,
                    )
                else:
                    run = await self._trigger_service.trigger_run(
                        route.agent_id,
                        content,
                        route.session_id,
                        internal=True,
                        reply_surface=reply_surface,
                        run_kind=RunKind.CHANNEL,
                        **tool_access_kwargs,
                        waiting_work_admission=waiting_work_admission,
                    )
            else:
                if waiting_work_admission is None:
                    run = await self._trigger_service.trigger_run(
                        route.agent_id,
                        content,
                        route.session_id,
                        sender=sender,
                        reply_surface=reply_surface,
                        run_kind=RunKind.CHANNEL,
                        **tool_access_kwargs,
                    )
                else:
                    run = await self._trigger_service.trigger_run(
                        route.agent_id,
                        content,
                        route.session_id,
                        sender=sender,
                        reply_surface=reply_surface,
                        run_kind=RunKind.CHANNEL,
                        **tool_access_kwargs,
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
        interrupted_segments: list[str] = []
        compaction_completed = False
        reply: str | None = None

        async with self._transport.activity_indicator(
            reply_plan.platform_target, reply_plan.thread_id
        ):
            async for event in run.subscribe():
                if event.type == ASSISTANT_OUTPUT_EVENT:
                    is_interrupted = _assistant_output_interrupted(event)
                    extracted = _extract_assistant_output(
                        event,
                        preserve_whitespace=is_interrupted or bool(interrupted_segments),
                    )
                    if extracted is not None:
                        if is_interrupted or interrupted_segments:
                            interrupted_segments.append(extracted)
                        else:
                            assistant_text = extracted
                    continue

                if event.type == COMPACTION_COMPLETED_EVENT:
                    compaction_completed = True
                    continue

                if event.type == RUN_COMPLETED_EVENT:
                    reply = (
                        _combined_interrupted_output(interrupted_segments)
                        or assistant_text
                        or ("Context compacted." if compaction_completed else None)
                        or _EMPTY_ASSISTANT_REPLY
                    )
                    break

                if event.type == RUN_FAILED_EVENT:
                    reply = _FAILED_REPLY
                    break

                if event.type == RUN_CANCELLED_EVENT:
                    reply = _CANCELLED_REPLY
                    break

                if event.type == RUN_INTERRUPTED_EVENT:
                    reply = (
                        _combined_interrupted_output(interrupted_segments)
                        or assistant_text
                        or _INTERRUPTED_REPLY
                    )
                    break

        if reply is not None:
            await self._send_reply(reply_plan, reply)

    async def relay_run(self, run: Run, reply_plan: ReplyPlanFacts) -> None:
        """Relay an admitted background Run using the normal Channel reply semantics."""
        await self._relay_run_events(run, reply_plan)

    async def _send_reply(self, reply_plan: ReplyPlanFacts, text: str) -> None:
        """Deliver an engine reply, retrying transient transport failures.

        Retries honor the adapter's retryable classification (network blips,
        rate limits) with the shared backoff policy. When retries are
        exhausted the answer is genuinely lost - log it at error level so a
        dropped reply is visible instead of surfacing as generic queue noise.
        """
        try:
            await retry_async(
                self._transport.send_text,
                reply_plan.platform_target,
                text,
                reply_to_message_id=reply_plan.reply_to_message_id,
                thread_id=reply_plan.thread_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.error(
                "Channel reply lost after retries (channel=%s target=%s thread=%s attempts=%s): %s",
                reply_plan.channel_id,
                reply_plan.platform_target,
                reply_plan.thread_id,
                getattr(error, "attempts_made", None),
                error,
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

    # -- Slash Commands --------------------------------------------------------------

    async def _send_command_unavailability(
        self, reply_plan: ReplyPlanFacts, unavailable: CommandUnavailability
    ) -> None:
        await self._send_reply(
            reply_plan,
            f"The {unavailable.command} command is not available through "
            f"{self._transport.platform_display_name}.",
        )

    async def _execute_prepared_command(
        self,
        prepared: PreparedCommand,
        conversation: ConversationFacts,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        conversation_key: str,
    ) -> None:
        context = CommandExecutionContext(
            agent_id=route.agent_id,
            session_id=route.session_id,
            project_id=None,
            reply_surface=self._reply_surface(conversation.kind),
        )
        try:
            if prepared.execution_mode == "serialized":
                async with self._transport.activity_indicator(
                    reply_plan.platform_target, reply_plan.thread_id
                ):
                    outcome = await self._command_dispatcher.execute(prepared, context)
            else:
                outcome = await self._command_dispatcher.execute(prepared, context)
            await self._project_command_outcome(
                outcome,
                conversation,
                reply_plan,
                conversation_key,
            )
        except Exception as error:
            self._log_command_failure(prepared.name, route, reply_plan, error)
            await self._send_reply(reply_plan, _FAILED_REPLY)

    async def _project_command_outcome(
        self,
        outcome: CommandOutcome,
        conversation: ConversationFacts,
        reply_plan: ReplyPlanFacts,
        conversation_key: str,
    ) -> None:
        continued = False
        navigation = outcome.navigation
        if navigation is not None and navigation.kind == "continue_in_session":
            if navigation.agent_id != self._config.agent_id or navigation.project_id is not None:
                raise ValueError(
                    "Channel continuation navigation must stay on its configured Agent"
                )
            route = RouteFacts(agent_id=navigation.agent_id, session_id=navigation.session_id)
            await _CHANNEL_SESSION_WORKERS.run(
                self._apply_continuation_navigation,
                route,
                conversation,
                reply_plan,
                conversation_key,
            )
            await self._send_reply(reply_plan, _NEW_SESSION_STARTED_REPLY)
            continued = True

        if not continued and outcome.feedback is not None and outcome.feedback.text.strip():
            await self._send_reply(reply_plan, outcome.feedback.text)
        for command_run in outcome.runs:
            await self._relay_run_events(command_run.run, reply_plan)

    def _apply_continuation_navigation(
        self,
        route: RouteFacts,
        conversation: ConversationFacts,
        reply_plan: ReplyPlanFacts,
        conversation_key: str,
    ) -> None:
        self._update_session_metadata(
            route,
            conversation,
            reply_plan,
            track_participant=False,
        )
        self._set_active_session_pointer(
            route.agent_id,
            conversation_key,
            route.session_id,
        )

    def _reply_surface(self, conversation_kind: Literal["direct", "group"]) -> ReplySurface:
        return ReplySurface.channel(
            platform=self._config.platform,
            platform_display_name=self._transport.platform_display_name,
            channel_id=self._config.id,
            conversation_kind=conversation_kind,
        )

    def _set_active_session_pointer(self, agent_id: str, anchor: str, new_session_id: str) -> None:
        """Point the conversation anchor at the newest session (single hop).

        Read-modify-write on the anchor's sidecar so the anchor's other channel
        metadata is preserved. The anchor normally already exists (it was the
        active session); the get_or_create is a defensive floor for the rare case
        where it does not.
        """
        address = _session_address(agent_id, anchor)
        self._chat_sessions.get_or_create(address)
        self._chat_sessions.mutate_metadata(
            address,
            lambda metadata: metadata.__setitem__(ACTIVE_SESSION_METADATA_KEY, new_session_id),
        )

    def _point_conversation_at_session(
        self,
        conversation: ConversationFacts,
        session_id: str,
    ) -> dict[str, Any]:
        """Persist a new active Session and return the exact prior anchor metadata."""
        anchor = self._derive_session_id(conversation)
        address = _session_address(self._config.agent_id, anchor)
        self._chat_sessions.get_or_create(address)
        previous, _updated = self._chat_sessions.mutate_metadata_with_previous(
            address,
            lambda metadata: metadata.__setitem__(ACTIVE_SESSION_METADATA_KEY, session_id),
        )
        return previous

    def _restore_conversation_pointer(
        self,
        conversation: ConversationFacts,
        metadata: dict[str, Any],
    ) -> None:
        """Restore the anchor when a bound tap could not enter the Queue."""
        anchor = self._derive_session_id(conversation)
        self._chat_sessions.set_metadata(_session_address(self._config.agent_id, anchor), metadata)

    def _log_command_failure(
        self,
        command_name: str,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        error: Exception,
    ) -> None:
        _LOGGER.error(
            "Channel command failed (command=%s channel=%s agent=%s session=%s target=%s): %s",
            command_name,
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
    role = conversation.sender_role or "member"
    return f"{CHANNEL_MESSAGE_NOTE_PREFIX}[{display_name}|{sender_id}|{role}]: {text}"


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
        lines.append(
            "Tapped by: "
            f"[{_sanitize_sender_tag_part(conversation.user_display_name or conversation.user_id)}"
            f"|{_sanitize_sender_tag_part(conversation.user_id)}"
            f"|{conversation.sender_role or 'member'}]"
        )
    lines.append("Current buttons on the message (top to bottom, left to right):")
    lines.extend(f'- "{button.label}" ({button.data})' for row in event.buttons for button in row)
    lines.append("Act on the current button state, then confirm in this chat.")
    return "\n".join(lines)


def _restore_bound_interaction_event(
    binding: RunButtonBinding,
    event: InteractionEvent,
    tapped_index: int,
) -> InteractionEvent | None:
    """Hide the private binding envelope and restore the agent-authored ``run:*`` data."""
    if tapped_index >= len(binding.original_button_data):
        return None

    restored_rows = []
    for row in event.buttons:
        restored_row = []
        for button in row:
            parsed = parse_bound_run_callback_data(button.data)
            if parsed is None or parsed[0] != binding.id:
                restored_row.append(button)
                continue
            button_index = parsed[1]
            if button_index >= len(binding.original_button_data):
                return None
            restored_row.append(replace(button, data=binding.original_button_data[button_index]))
        restored_rows.append(tuple(restored_row))

    return replace(
        event,
        data=binding.original_button_data[tapped_index],
        buttons=tuple(restored_rows),
    )


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


def _sender_tag(sender: MessageSender) -> str:
    return (
        f"[{_sanitize_sender_tag_part(sender.display_name)}"
        f"|{_sanitize_sender_tag_part(sender.id)}|{sender.role}]"
    )


def _extract_assistant_output(event: RunEvent, *, preserve_whitespace: bool = False) -> str | None:
    payload = event.payload
    if not isinstance(payload, dict):
        return None

    message = payload.get("message")
    if not isinstance(message, dict):
        return None

    content = message.get("content")
    if not isinstance(content, str):
        return None

    if not content.strip():
        return None
    return content if preserve_whitespace else content.strip()


def _assistant_output_interrupted(event: RunEvent) -> bool:
    message = event.payload.get("message")
    return isinstance(message, dict) and message.get("interrupted") is True


def _combined_interrupted_output(segments: list[str]) -> str | None:
    # These are consecutive fragments of one visible answer across internal
    # Model boundaries. Preserve their bytes instead of inventing separators;
    # the continuation Model owns any required whitespace or Markdown break.
    return "".join(segments) if segments else None


def _media_failure_reply(error: Exception) -> str:
    """Map a media-ingest failure to user-facing reply text without leaking internals."""
    if isinstance(error, AttachmentTypeNotAllowedError):
        return _UNSUPPORTED_FILE_REPLY
    if isinstance(error, AttachmentTooLargeError):
        return _FILE_TOO_LARGE_REPLY
    if getattr(error, "retryable", False) is True:
        return _MEDIA_DOWNLOAD_FAILED_REPLY
    return _MEDIA_FAILED_REPLY


__all__ = ["ChannelConversationEngine", "ConversationTransport"]
