"""Platform-neutral conversation engine for channel adapters.

The engine owns everything about a channel conversation that is not specific to one
messaging platform: per-conversation queueing and worker serialization, neutral command
projection, run trigger/relay, and session routing/metadata. The Channel service keeps
one engine per Channel across adapter restarts and hands it to each adapter it starts;
the running adapter attaches its `ConversationTransport`, and raw platform messages flow
through the engine as opaque values that only that transport converts to canonical
content blocks.

Every reply a conversation is owed (a Run's answer, or a notice for admitted work no
Run took over) is recorded durably before its answer exists and sent at most once, by
this engine or, after it ended, by the Channel's next one.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Coroutine, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4
from weakref import WeakValueDictionary

from core.channels.adapter import (
    ChannelAccessRegistry,
    ConversationFacts,
    ConversationPointerStore,
    MessageFacts,
    PendingReply,
    PendingReplyStore,
    QuotedMessageFacts,
    ReplyPlanFacts,
    RouteFacts,
    RunButtonBindingRegistry,
    RunButtonClaim,
    parse_bound_run_callback_data,
)
from core.channels.config import ChannelError
from core.chat.commands import (
    CommandDispatcher,
    CommandExecutionContext,
    CommandOutcome,
    CommandUnavailability,
    PreparedCommand,
)
from core.chat.content_blocks import ContentBlock, TextBlock
from core.chat.messages import MessageSender, ReplySurface
from core.runs import (
    ASSISTANT_OUTPUT_EVENT,
    COMPACTION_COMPLETED_EVENT,
    USER_MESSAGE_EVENT,
    RunCancelledError,
    RunKind,
    WaitingWorkAdmission,
    WaitingWorkLimitError,
)
from core.utils.logging import get_logger
from core.utils.retry import retry_async
from core.utils.timestamps import utc_now_timestamp

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.channels.config import ChannelConfig
    from core.extensions.interactions import InteractionEvent
    from core.runs import Run
    from core.sessions import ChatSessionManager

from ._conversation_access import ChannelAccessPolicy
from ._conversation_content import (
    _CANCELLED_REPLY,
    _FAILED_REPLY,
    _INTERRUPTED_REPLY,
    RunReply,
    _format_interaction_note,
    _format_observed_message,
    _media_failure_reply,
    _restore_bound_interaction_event,
    _sender_tag,
)
from ._conversation_routing import ChannelSessionRouting, _session_address
from ._conversation_work import (
    ConversationTransport,
    _PendingItem,
    _QueuedInboundMedia,
    _QueuedInboundMessage,
    _QueuedInternalPrompt,
    _QueuedObservedMessage,
    _QueuedPreparedCommand,
    _QueuedWork,
)

_LOGGER = get_logger("channels.engine")

# Sent for admitted work that an ended engine left before any Run took it over.
_UNANSWERED_MESSAGE_REPLY = (
    "Sorry, I was interrupted before I could answer your last message. Please send it again."
)
_UNANSWERED_MESSAGES_REPLY = (
    "Sorry, I was interrupted before I could answer your last messages. Please send them again."
)
_UNANSWERED_TAP_REPLY = "If you tapped a button, please tap it again."
_UNANSWERED_TAP_ONLY_REPLY = (
    "Sorry, I was interrupted before I could act on your button tap. Please tap it again."
)
_BUSY_REPLY = "I'm busy with earlier messages. Please try again shortly."
_QUOTED_MESSAGE_PREFIX = "[quoted-message]"
_QUOTED_MESSAGE_UNAVAILABLE = "[quoted-message unavailable]"

CHANNEL_WAITING_WORK_LIMIT = 8
BUSY_REPLY_COOLDOWN_SECONDS = 30
BUSY_REPLY_TRACKING_LIMIT = 512

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


class ChannelConversationEngine:
    """Platform-neutral conversation behavior shared by channel adapters."""

    def __init__(
        self,
        config: ChannelConfig,
        trigger_service: TriggerService,
        chat_sessions: ChatSessionManager,
        *,
        command_dispatcher: CommandDispatcher,
        conversation_pointers: ConversationPointerStore,
        pending_replies: PendingReplyStore,
        run_button_binding_registry: RunButtonBindingRegistry | None = None,
        access_registry: ChannelAccessRegistry | None = None,
    ) -> None:
        self._config = config
        self._trigger_service = trigger_service
        self._chat_sessions = chat_sessions
        # The running adapter's transport, and whether its platform connection is
        # up; replies wait for a connected transport.
        self._transport: ConversationTransport | None = None
        self._connected = asyncio.Event()
        self._pending_replies = pending_replies
        # Names this engine on the replies it records; the Channel's next engine
        # sends the ones it left.
        self._owner = uuid4().hex
        self._started = False
        self._stopped = False
        # Relays and deliveries that outlive the call that started them.
        self._tasks: set[asyncio.Task[Any]] = set()
        # Durable records of admitted work; stop lets them finish instead of
        # cancelling them, so no admitted item loses its notice.
        self._recordings: set[asyncio.Task[bool]] = set()
        self._command_dispatcher = command_dispatcher
        self._run_button_binding_registry = run_button_binding_registry
        self._access = ChannelAccessPolicy(config, access_registry)
        self._conversation_pointers = conversation_pointers
        self._routing = ChannelSessionRouting(config, chat_sessions, conversation_pointers)
        # One FIFO and worker per conversation while it has work; an idle
        # conversation retires both and the next item creates them again.
        self._chat_queues: dict[str, deque[_QueuedWork]] = {}
        self._chat_workers: dict[str, asyncio.Task[None]] = {}
        self._busy_reply_times: OrderedDict[str, float] = OrderedDict()
        self._bound_tap_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    @property
    def config(self) -> ChannelConfig:
        """The Channel configuration this engine serves."""
        return self._config

    def attach(self, transport: ConversationTransport) -> None:
        """Use ``transport`` for this Channel's platform I/O; replies wait until it connects."""
        self._transport = transport
        self._connected.clear()

    def set_connected(self, connected: bool) -> None:
        """Record whether the attached transport's platform connection is up."""
        if connected and self._transport is not None and not self._stopped:
            self._connected.set()
        else:
            self._connected.clear()

    def start(self) -> None:
        """Begin sending the replies the Channel's earlier engines left owed."""
        if self._started or self._stopped:
            return
        self._started = True
        self._spawn(self._resume_pending_replies())

    async def ensure_channel_session(self, conversation: ConversationFacts) -> RouteFacts:
        """Ensure an outbound Channel target has its routed Session."""
        return await self._routing.ensure_channel_session(conversation)

    async def migrate_group_conversation(self, old_chat_id: str, new_chat_id: str) -> bool:
        """Preserve the active Session across a platform group-id migration."""
        return await self._routing.migrate_group_conversation(old_chat_id, new_chat_id)

    def should_respond(
        self, conversation: ConversationFacts, gating_texts: Sequence[str | None] = ()
    ) -> bool:
        """Apply configured response eligibility to this conversation."""
        return self._access.should_respond(conversation, gating_texts)

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
        observed_context: Sequence[tuple[ConversationFacts, str]] = (),
    ) -> bool:
        """Gate and admit text with optional bounded context; report admission."""
        conversation = await self._access._snapshot_group_sender(conversation)
        prepared_command = self._command_dispatcher.prepare(message_text)
        if prepared_command is not None:
            # Commands are inherently addressed; group commands are gated by sender
            # authorization instead of response mode. Authorization precedes both
            # availability projection and every command side effect.
            if conversation.kind == "group" and not self._access._command_sender_authorized(
                conversation
            ):
                _LOGGER.debug(
                    "Channel command denied for member (channel=%s)",
                    self._config.id,
                )
                return False
            reply_plan = self._routing._reply_plan_for(conversation)
            unavailable = self._command_dispatcher.unavailability(
                prepared_command, self._reply_surface(conversation.kind)
            )
            if unavailable is not None:
                await self._send_command_unavailability(reply_plan, unavailable)
                return True
            if prepared_command.execution_mode == "immediate":
                route, reply_plan = await self._routing._prepare_inbound_route_async(conversation)
                await self._execute_prepared_command(
                    prepared_command,
                    conversation,
                    route,
                    reply_plan,
                    self._routing._derive_session_id(conversation),
                    from_ingress=True,
                )
                return True
            if not self._enqueue_chat_work(
                conversation.chat_id,
                _QueuedPreparedCommand(
                    conversation=conversation,
                    command=prepared_command,
                ),
            ):
                await self._reject_overflow(conversation)
                return False
            return True

        if not self._access.should_respond(conversation, (message_text,)):
            if self._config.observe_unaddressed and conversation.kind == "group":
                return self._enqueue_observed_message(
                    conversation,
                    _format_observed_message(conversation, message_text),
                )
            return False

        if not self._enqueue_chat_work(
            conversation.chat_id,
            _QueuedInboundMessage(
                conversation=conversation,
                message=MessageFacts(content=message_text),
                raw_message=raw_message,
                observed_context=await self._snapshot_observed_context(observed_context),
            ),
        ):
            await self._reject_overflow(conversation)
            return False
        return True

    async def handle_inbound_media(
        self,
        conversation: ConversationFacts,
        raw_messages: tuple[Any, ...],
        *,
        companion_text: str | None = None,
        observed_context: Sequence[tuple[ConversationFacts, str]] = (),
    ) -> bool:
        """Gate and admit media with optional bounded context; report admission."""
        conversation = await self._access._snapshot_group_sender(conversation)
        normalized_companion = companion_text.strip() if companion_text is not None else None
        if normalized_companion == "":
            normalized_companion = None
        transport = self._require_transport()
        caption_texts = tuple(transport.caption_text(message) for message in raw_messages)
        gating_texts = (
            *((normalized_companion,) if normalized_companion is not None else ()),
            *caption_texts,
        )
        if not self._access.should_respond(conversation, gating_texts):
            if self._config.observe_unaddressed and conversation.kind == "group":
                notes = []
                for caption in gating_texts:
                    body = (
                        f"[media] {caption}"
                        if caption is not None and caption != ""
                        else "[media message]"
                    )
                    notes.append(_format_observed_message(conversation, body))
                if notes:
                    return self._enqueue_observed_message(
                        conversation, notes[0], following_notes=tuple(notes[1:])
                    )
            return False

        if not self._enqueue_chat_work(
            conversation.chat_id,
            _QueuedInboundMedia(
                conversation=conversation,
                messages=tuple(raw_messages),
                companion_text=normalized_companion,
                observed_context=await self._snapshot_observed_context(observed_context),
            ),
        ):
            await self._reject_overflow(conversation)
            return False
        return True

    async def _snapshot_observed_context(
        self, context: Sequence[tuple[ConversationFacts, str]]
    ) -> tuple[_QueuedObservedMessage, ...]:
        observed = []
        for conversation, text in context:
            conversation = await self._access._snapshot_group_sender(conversation)
            observed.append(
                _QueuedObservedMessage(
                    conversation=conversation,
                    note=_format_observed_message(conversation, text),
                )
            )
        return tuple(observed)

    async def trigger_internal_reply(self, conversation: ConversationFacts, prompt: str) -> bool:
        """Queue an internal note-driven Run whose reply goes back to the conversation.

        Platform rituals such as Telegram's ``/start`` use this: the prompt is
        persisted as a kernel-internal note (never a visible user message), the model
        acts on it, and its reply is relayed like any other channel answer.
        """
        conversation = await self._access._snapshot_group_sender(conversation)
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
        FIFO as following messages. Unbound ``run:<payload>`` buttons, sent without
        a Run origin (for example from a Project Session), route to the Channel's
        current active Session.
        """
        conversation = await self._access._snapshot_group_sender(conversation)
        if not self._access._command_sender_authorized(conversation):
            _LOGGER.debug(
                "Run-triggering tap denied for member (channel=%s)",
                self._config.id,
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
        # Bindings share an anchor, including DM scopes that span platform chats.
        # Hold ownership through compensation so an older aborted tap cannot undo
        # a later admitted tap to the same Session. Waiters retain the lock; the
        # weak index retires idle anchors without splitting a waiting cohort.
        anchor = self._routing._derive_session_id(conversation)
        lock = self._bound_tap_locks.get(anchor)
        if lock is None:
            lock = asyncio.Lock()
            self._bound_tap_locks[anchor] = lock
        async with lock:
            binding_id, button_index = parsed_binding
            claim: RunButtonClaim | None = None
            pointed = False
            previous_session_id: str | None = None
            terminal = False
            admitted = False

            def claim_binding() -> None:
                nonlocal claim
                claim = registry.claim_run_button_binding(
                    self._config.id,
                    binding_id,
                    platform_target=conversation.chat_id,
                    thread_id=conversation.thread_id,
                )

            def point_at_origin(origin_session_id: str) -> None:
                nonlocal pointed, previous_session_id
                previous_session_id = self._routing._point_conversation_at_session(
                    conversation, origin_session_id
                )
                pointed = True

            async def rollback() -> None:
                assert claim is not None and claim.binding is not None
                binding = claim.binding
                try:
                    if pointed:
                        await self._conversation_pointers.run_async(
                            self._routing._restore_conversation_pointer,
                            conversation,
                            previous_session_id,
                            expected_session_id=binding.origin_session_id,
                        )
                finally:
                    await registry.run_async(
                        registry.restore_run_button_binding, self._config.id, binding.id
                    )

            try:
                # Each step runs on its own database's pool and keeps its
                # compensation state inside the worker: cancellation waits for a
                # started step to settle, but deliberately does not return its result.
                await registry.run_async(claim_binding)
                assert claim is not None
                if claim.status == "consumed":
                    return "already_handled"
                if claim.status != "claimed" or claim.binding is None:
                    return "unavailable"
                origin_session_id = claim.binding.origin_session_id
                restored_event = _restore_bound_interaction_event(
                    claim.binding, event, button_index
                )
                if restored_event is None or not await self._chat_sessions.run_async(
                    self._chat_sessions.exists,
                    _session_address(self._config.agent_id, origin_session_id),
                ):
                    terminal = True
                    return "unavailable"
                await self._conversation_pointers.run_async(point_at_origin, origin_session_id)
                admitted = self._enqueue_chat_work(
                    conversation.chat_id,
                    _QueuedInternalPrompt(
                        conversation=conversation,
                        prompt=_format_interaction_note(conversation, restored_event),
                        route=RouteFacts(
                            agent_id=self._config.agent_id,
                            session_id=origin_session_id,
                        ),
                        binding_id=claim.binding.id,
                    ),
                )
                if admitted:
                    return "enqueued"
            finally:
                if (
                    not admitted
                    and not terminal
                    and claim is not None
                    and claim.status == "claimed"
                ):
                    cleanup = asyncio.create_task(rollback())
                    cancelled = False
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            cancelled = True
                    cleanup.result()
                    if cancelled:
                        raise asyncio.CancelledError

        await self._reject_overflow(conversation)
        return "busy"

    # -- Queue / workers --------------------------------------------------------------

    def _enqueue_observed_message(
        self, conversation: ConversationFacts, note: str, *, following_notes: tuple[str, ...] = ()
    ) -> bool:
        if self._enqueue_chat_work(
            conversation.chat_id,
            _QueuedObservedMessage(
                conversation=conversation, note=note, following_notes=following_notes
            ),
        ):
            return True
        _LOGGER.warning(
            "Observed channel context rejected by queue limit (channel=%s)",
            self._config.id,
        )
        return False

    def _enqueue_chat_work(self, platform_target: str, queued: _QueuedWork) -> bool:
        """Admit one channel item before it reaches the per-chat FIFO.

        The Run manager owns the bounded waiting-work accounting. This ingress
        FIFO retains only already-admitted items so it can preserve a channel's
        arrival order and defer media downloads until after admission. Every
        admitted item that expects an answer is recorded as owed at once, so an
        engine that ends before a Run took it over leaves its notice behind.
        """
        if self._stopped:
            return False
        try:
            admission = self._trigger_service.reserve_waiting_work(
                scope=self._waiting_scope(platform_target),
                scope_limit=CHANNEL_WAITING_WORK_LIMIT,
            )
        except WaitingWorkLimitError:
            return False

        queue = self._chat_queues.get(platform_target)
        if queue is None:
            queue = deque()
            self._chat_queues[platform_target] = queue

        pending = None if isinstance(queued, _QueuedObservedMessage) else self._owe_admitted(queued)
        queue.append(replace(queued, admission=admission, pending=pending))

        worker = self._chat_workers.get(platform_target)
        if worker is None or worker.done():
            worker = asyncio.create_task(
                self._run_chat_queue(platform_target, queue),
                name=f"channel:{self._config.id}:chat-queue",
            )
            self._chat_workers[platform_target] = worker
        return True

    async def _run_chat_queue(
        self,
        platform_target: str,
        queue: deque[_QueuedWork],
    ) -> None:
        """Process one conversation's FIFO, then retire with it once it is empty.

        Enqueueing is synchronous, and nothing awaits between the empty check and
        the retirement, so an item arriving later either joins this queue while
        the worker runs or finds no worker and starts a new one: one worker per
        conversation at a time, in arrival order.
        """
        try:
            while queue:
                queued = queue.popleft()
                try:
                    await self._process_queued_work(queued)
                except Exception as error:
                    _LOGGER.error(
                        "Channel inbound processing failed (channel=%s): %s",
                        self._config.id,
                        error,
                        exc_info=(type(error), error, error.__traceback__),
                    )
                finally:
                    self._trigger_service.release_waiting_work(queued.admission)
                # Handled without a Run taking the item over: its notice is not owed.
                # A cancelled worker leaves it owed for the Channel's next engine.
                await self._settle_admitted(queued.pending)
        finally:
            if self._chat_workers.get(platform_target) is asyncio.current_task():
                self._chat_workers.pop(platform_target, None)
                # A worker that ended abnormally leaves its items to the next one.
                if not queue and self._chat_queues.get(platform_target) is queue:
                    self._chat_queues.pop(platform_target, None)

    async def _process_queued_work(self, queued: _QueuedWork) -> None:
        if isinstance(queued, (_QueuedInboundMessage, _QueuedInboundMedia)):
            # Bounded history belongs to the addressed turn, not independent work
            # that could fill its capacity budget before the turn itself is admitted.
            for observed in queued.observed_context:
                await self._process_queued_observed_message(observed)
        if isinstance(queued, _QueuedObservedMessage):
            await self._process_queued_observed_message(queued)
            return
        if isinstance(queued, _QueuedPreparedCommand):
            # Serialized commands re-resolve at processing time like every other
            # queued item: an earlier navigation may have changed the active Session.
            self._trigger_service.release_waiting_work(queued.admission)
            route, reply_plan = await self._routing._prepare_inbound_route_async(
                queued.conversation
            )
            await self._execute_prepared_command(
                queued.command,
                queued.conversation,
                route,
                reply_plan,
                self._routing._derive_session_id(queued.conversation),
                from_ingress=False,
            )
            return
        if isinstance(queued, _QueuedInboundMedia):
            await self._process_queued_media(queued)
            return
        if isinstance(queued, _QueuedInternalPrompt):
            if queued.route is None:
                route, reply_plan = await self._routing._prepare_inbound_route_async(
                    queued.conversation
                )
            else:
                route = queued.route
                reply_plan = self._routing._reply_plan_for(queued.conversation)
                exists = await self._chat_sessions.run_async(
                    self._chat_sessions.exists,
                    _session_address(route.agent_id, route.session_id),
                )
                if not exists:
                    await self._send_reply(reply_plan, _FAILED_REPLY)
                    return
                await self._chat_sessions.run_async(
                    self._routing._update_session_metadata,
                    route,
                    queued.conversation,
                    reply_plan,
                )
            await self._trigger_and_relay(
                route,
                reply_plan,
                queued.prompt,
                conversation=queued.conversation,
                internal=True,
                waiting_work_admission=queued.admission,
                pending=queued.pending,
            )
            return
        await self._process_queued_message(queued)

    async def _process_queued_observed_message(self, queued: _QueuedObservedMessage) -> None:
        self._trigger_service.release_waiting_work(queued.admission)
        route, _reply_plan = await self._routing._prepare_inbound_route_async(queued.conversation)
        # Wait for any open tool cycle on this shared session (a Run via another
        # accessor) so the observed note lands after the cycle, never inside it.
        async with self._chat_sessions.write_lock(
            _session_address(route.agent_id, route.session_id)
        ):
            for note in (queued.note, *queued.following_notes):
                await self._chat_sessions.run_async(
                    self._routing._append_session_note,
                    route.agent_id,
                    route.session_id,
                    note,
                )

    async def _process_queued_message(self, queued: _QueuedInboundMessage) -> None:
        # Prepared commands have their own queued-work type; only plain messages
        # reach this path, so processing goes straight to trigger/relay.
        route, reply_plan = await self._routing._prepare_inbound_route_async(queued.conversation)
        content: str | list[ContentBlock] = queued.message.content
        if queued.conversation.kind == "group" and queued.raw_message is not None:
            try:
                transport = await self._ready_transport()
                quoted = await transport.build_quoted_message(queued.raw_message)
            except Exception as error:
                _LOGGER.warning(
                    "Channel quoted attachment processing failed (channel=%s): %s",
                    self._config.id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
                quoted = QuotedMessageFacts(
                    user_id=None,
                    user_display_name=None,
                    content=None,
                )
            if quoted is not None:
                content = await self._content_with_quoted_message(queued, quoted)

        await self._trigger_and_relay(
            route,
            reply_plan,
            content,
            conversation=queued.conversation,
            sender=self._access._sender_for(queued.conversation),
            waiting_work_admission=queued.admission,
            pending=queued.pending,
        )

    async def _content_with_quoted_message(
        self,
        queued: _QueuedInboundMessage,
        quoted: QuotedMessageFacts,
    ) -> list[ContentBlock]:
        blocks: list[ContentBlock] = [TextBlock(type="text", text=queued.message.content)]
        if quoted.content is None or quoted.user_id is None:
            blocks.append(TextBlock(type="text", text=_QUOTED_MESSAGE_UNAVAILABLE))
            return blocks

        quoted_conversation = await self._access._snapshot_group_sender(
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
        quoted_sender = self._access._sender_for(quoted_conversation)
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
        route, reply_plan = await self._routing._prepare_inbound_route_async(queued.conversation)
        # Per-message handling: one failing album item must not drop its siblings,
        # and every failure produces user-visible feedback instead of silence.
        content_blocks: list[ContentBlock] = []
        if queued.companion_text is not None:
            content_blocks.append(TextBlock(type="text", text=queued.companion_text))
        failure_replies: list[str] = []
        for message in queued.messages:
            try:
                transport = await self._ready_transport()
                content_blocks.extend(await transport.build_media_blocks(message))
            except Exception as error:
                _LOGGER.warning(
                    "Channel inbound media processing failed (channel=%s): %s",
                    self._config.id,
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
            sender=self._access._sender_for(queued.conversation),
            waiting_work_admission=queued.admission,
            pending=queued.pending,
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
        waiting_work_admission: WaitingWorkAdmission | None,
        pending: _PendingItem | None = None,
    ) -> None:
        tool_restriction, tool_denial_resolver = self._access._tool_access_for(conversation)
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
        except RunCancelledError:
            # The queued turn was removed (for example from the WebUI Queue) or
            # its Run was cancelled before it started.
            await self._answer_admitted(pending, reply_plan, _CANCELLED_REPLY)
            return
        except Exception as error:
            _LOGGER.error(
                "Channel trigger run failed (channel=%s agent=%s): %s",
                reply_plan.channel_id,
                route.agent_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            await self._answer_admitted(pending, reply_plan, _FAILED_REPLY)
            return

        await self._relay_run(run, reply_plan, pending)

    async def _answer_admitted(
        self, pending: _PendingItem | None, reply_plan: ReplyPlanFacts, text: str
    ) -> None:
        """Send the reply an admitted item gets without a Run, in place of its owed record."""
        reply_id: str | None = None
        if pending is not None:
            pending.taken = True
            if await asyncio.shield(pending.recorded):
                reply_id = pending.reply_id
        await self._deliver((reply_id,), reply_plan, text)

    async def _relay_run(
        self, run: Run, reply_plan: ReplyPlanFacts, pending: _PendingItem | None = None
    ) -> None:
        """Owe the Run's answer durably, wait for it, and send it once."""
        reply_id = await self._owe_run_reply(run, reply_plan, pending)
        reply = await self._await_run_reply(run, reply_plan)
        await self._deliver((reply_id,), reply_plan, reply)

    async def relay_run(self, run: Run, reply_plan: ReplyPlanFacts) -> None:
        """Relay an admitted background Run with the normal Channel reply semantics.

        Returns once the Run ended. Its answer is owed durably first, so it reaches
        the chat once the Channel is connected, by this engine or its successor.
        """
        reply_id = await self._owe_run_reply(run, reply_plan, None)
        reply = await self._await_run_reply(run, reply_plan)
        if not self._spawn(self._deliver((reply_id,), reply_plan, reply)):
            _LOGGER.debug(
                "Channel engine ended before a background reply; its next engine sends it "
                "(channel=%s run=%s)",
                self._config.id,
                run.id,
            )

    async def _await_run_reply(self, run: Run, reply_plan: ReplyPlanFacts) -> str:
        """Follow the Run to its end, then project its complete durable history."""
        projection = RunReply()
        async with self._activity(reply_plan):
            async for event in run.subscribe():
                if event.type == ASSISTANT_OUTPUT_EVENT:
                    projection.observe_output(event.payload.get("message"))
                elif event.type == USER_MESSAGE_EVENT:
                    projection.observe_input()
                elif event.type == COMPACTION_COMPLETED_EVENT:
                    projection.observe_compaction()
            # Replay is bounded, and a lagging subscription may end before the
            # Run does. Neither can decide which parts of its answer survived.
            with contextlib.suppress(Exception):
                await run.wait()
        status, reason = run.status.value, run.cancel_reason
        return await self._history_reply(
            RouteFacts(agent_id=run.agent_id, session_id=run.session_id),
            run.id,
            fallback=projection.settle(status, reason),
            status=status,
            reason=reason,
        )

    async def _history_reply(
        self,
        route: RouteFacts | None,
        run_id: str | None,
        *,
        fallback: str = _INTERRUPTED_REPLY,
        status: str = "interrupted",
        reason: str | None = None,
    ) -> str:
        """Project exact Run history; observed live output is the unavailable-history fallback."""
        if route is None or run_id is None:
            return fallback
        try:
            session = await self._chat_sessions.get_async(
                _session_address(route.agent_id, route.session_id)
            )
            messages = await session.load_run_messages_async(run_id)
        except Exception as error:
            _LOGGER.warning(
                "Channel reply history unavailable (channel=%s run=%s): %s",
                self._config.id,
                run_id,
                error,
            )
            return fallback
        if not messages:
            # A standalone in-memory Run has no Session history. A storage
            # failure may also have prevented any of its output from persisting.
            return fallback
        projection = RunReply()
        for message in messages:
            if message.role == "assistant":
                projection.observe_output(message.to_dict())
            elif message.role == "user":
                projection.observe_input()
            elif message.role == "compaction_checkpoint":
                projection.observe_compaction()
            elif message.role == "run_summary" and message.run_id == run_id:
                status, reason = message.status or status, message.completion_reason
        # Without a summary use the known live outcome, or interruption when
        # the process that ran it is gone.
        return projection.settle(status, reason)

    # -- Owed replies -----------------------------------------------------------------

    def _owe_admitted(self, queued: _QueuedWork) -> _PendingItem:
        """Record durably that an admitted item is owed an answer, without blocking ingress."""
        reply = PendingReply(
            id=uuid4().hex,
            reply_plan=self._routing._reply_plan_for(queued.conversation),
            owner=self._owner,
            created_at=utc_now_timestamp(),
            binding_id=queued.binding_id if isinstance(queued, _QueuedInternalPrompt) else None,
        )
        recorded = asyncio.create_task(
            self._owe(reply), name=f"channel:{self._config.id}:owe-reply"
        )
        self._recordings.add(recorded)
        recorded.add_done_callback(self._recordings.discard)
        return _PendingItem(reply_id=reply.id, created_at=reply.created_at, recorded=recorded)

    async def _owe_run_reply(
        self, run: Run, reply_plan: ReplyPlanFacts, pending: _PendingItem | None
    ) -> str | None:
        """Record the Run's answer as owed; an admitted item's notice becomes that answer."""
        reply_id = uuid4().hex
        created_at = utc_now_timestamp()
        if pending is not None:
            pending.taken = True
            if not await asyncio.shield(pending.recorded):
                return None
            reply_id, created_at = pending.reply_id, pending.created_at
        reply = PendingReply(
            id=reply_id,
            reply_plan=reply_plan,
            owner=self._owner,
            created_at=created_at,
            route=RouteFacts(agent_id=run.agent_id, session_id=run.session_id),
            run_id=run.id,
        )
        return reply_id if await self._owe(reply) else None

    async def _owe(self, reply: PendingReply) -> bool:
        try:
            await self._pending_replies.run_async(
                self._pending_replies.owe_reply, self._config.id, reply
            )
        except Exception as error:
            # The reply is still sent while this engine runs, only not after it ends.
            _LOGGER.warning(
                "Channel reply not recorded (channel=%s): %s",
                self._config.id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            return False
        return True

    async def _settle_admitted(self, pending: _PendingItem | None) -> None:
        if pending is None or pending.taken:
            return
        if await asyncio.shield(pending.recorded):
            await self._update_pending(self._pending_replies.settle_reply, pending.reply_id)

    async def _resume_pending_replies(self) -> None:
        """Send the replies the Channel's earlier engines left owed, each at most once."""
        try:
            replies, dropped = await self._pending_replies.run_async(
                self._pending_replies.take_pending_replies, self._config.id, self._owner
            )
        except Exception as error:
            _LOGGER.error(
                "Channel owed replies unavailable (channel=%s): %s",
                self._config.id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            return
        if dropped:
            _LOGGER.warning(
                "Channel replies dropped: a send was in flight when vBot stopped, or they "
                "were owed too long (channel=%s count=%d)",
                self._config.id,
                dropped,
            )
        unanswered: dict[tuple[str, str | None], list[PendingReply]] = {}
        for reply in replies:
            if reply.run_id is None:
                plan = reply.reply_plan
                unanswered.setdefault((plan.platform_target, plan.thread_id), []).append(reply)
            else:
                self._spawn(self._resume_run_reply(reply))
        for group in unanswered.values():
            self._spawn(self._resume_unanswered(group))

    async def _resume_run_reply(self, reply: PendingReply) -> None:
        run_id = reply.run_id
        assert run_id is not None
        run = self._trigger_service.running_run(run_id)
        text = (
            await self._await_run_reply(run, reply.reply_plan)
            if run is not None
            else await self._history_reply(reply.route, reply.run_id)
        )
        await self._deliver((reply.id,), reply.reply_plan, text)

    async def _resume_unanswered(self, replies: list[PendingReply]) -> None:
        """Ask a conversation once to resend the work an ended engine never answered."""
        taps = [reply for reply in replies if reply.binding_id is not None]
        registry = self._run_button_binding_registry
        if registry is not None:
            for reply in taps:
                # The claimed button works again for the requested tap.
                try:
                    await registry.run_async(
                        registry.restore_run_button_binding, self._config.id, reply.binding_id
                    )
                except Exception as error:
                    _LOGGER.warning(
                        "Run-button binding not restored (channel=%s): %s",
                        self._config.id,
                        error,
                    )
        messages = len(replies) - len(taps)
        if messages == 0:
            text = _UNANSWERED_TAP_ONLY_REPLY
        else:
            text = _UNANSWERED_MESSAGE_REPLY if messages == 1 else _UNANSWERED_MESSAGES_REPLY
            if taps:
                text = f"{text} {_UNANSWERED_TAP_REPLY}"
        await self._deliver(tuple(reply.id for reply in replies), replies[-1].reply_plan, text)

    async def _update_pending(self, update: Any, reply_id: str) -> bool:
        try:
            await self._pending_replies.run_async(update, self._config.id, reply_id)
        except Exception as error:
            _LOGGER.warning(
                "Channel owed reply not updated (channel=%s): %s",
                self._config.id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            return False
        return True

    async def _claim(self, reply_ids: tuple[str | None, ...]) -> tuple[str, ...] | None:
        """Claim the recorded replies for one send; None when none of them is still owed."""
        recorded = tuple(reply_id for reply_id in reply_ids if reply_id is not None)
        if not recorded:
            return ()
        claimed: list[str] = []
        for reply_id in recorded:
            try:
                if await self._pending_replies.run_async(
                    self._pending_replies.claim_reply, self._config.id, reply_id
                ):
                    claimed.append(reply_id)
            except Exception as error:
                # Without a claim the reply is still sent once by this engine.
                _LOGGER.warning(
                    "Channel owed reply not claimed (channel=%s): %s", self._config.id, error
                )
                return ()
        # Gone: the Channel was deleted or reset, or the reply was sent already.
        return tuple(claimed) if claimed else None

    # -- Sending ----------------------------------------------------------------------

    async def _deliver(
        self, reply_ids: tuple[str | None, ...], reply_plan: ReplyPlanFacts, text: str | None
    ) -> None:
        """Send one reply at most once over the connected transport, then forget it.

        The send waits while the Channel is disconnected. A failure that shows the
        reply did not leave because the adapter went away waits for the next
        connection; any other failure loses the reply, logged. A send cut off
        mid-flight leaves its claim, so no later engine sends it again.
        """
        if text is None:
            for reply_id in reply_ids:
                if reply_id is not None:
                    await self._update_pending(self._pending_replies.settle_reply, reply_id)
            return
        while True:
            transport = await self._ready_transport()
            claimed = await self._claim(reply_ids)
            if claimed is None:
                return
            try:
                await retry_async(
                    transport.send_text,
                    reply_plan.platform_target,
                    text,
                    reply_to_message_id=reply_plan.reply_to_message_id,
                    thread_id=reply_plan.thread_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                unsent = isinstance(error, ChannelError) and not error.possibly_delivered
                if unsent and not self._is_connected(transport) and not self._stopped:
                    for reply_id in claimed:
                        await self._update_pending(self._pending_replies.release_reply, reply_id)
                    continue
                _log_failed_reply(reply_plan, error)
            for reply_id in claimed:
                await self._update_pending(self._pending_replies.settle_reply, reply_id)
            return

    async def _send_reply(self, reply_plan: ReplyPlanFacts, text: str) -> None:
        """Send a reply that is not owed durably, waiting while the Channel is disconnected."""
        await self._deliver((None,), reply_plan, text)

    async def _send_ingress_reply(self, reply_plan: ReplyPlanFacts, text: str) -> None:
        """Send a reply from the adapter's own inbound handling, without waiting.

        The handling adapter is the attached transport; waiting for a later one
        would hold the adapter's shutdown.
        """
        transport = self._transport
        if transport is None or self._stopped:
            return
        try:
            await retry_async(
                transport.send_text,
                reply_plan.platform_target,
                text,
                reply_to_message_id=reply_plan.reply_to_message_id,
                thread_id=reply_plan.thread_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_failed_reply(reply_plan, error)

    def _require_transport(self) -> ConversationTransport:
        transport = self._transport
        if transport is None:
            raise ChannelError(f"Channel has no running adapter: {self._config.id}")
        return transport

    async def _ready_transport(self) -> ConversationTransport:
        """Wait until the attached transport is connected and return it."""
        while True:
            await self._connected.wait()
            transport = self._transport
            if transport is not None and self._connected.is_set():
                return transport

    def _is_connected(self, transport: ConversationTransport) -> bool:
        return self._transport is transport and self._connected.is_set()

    @contextlib.asynccontextmanager
    async def _activity(self, reply_plan: ReplyPlanFacts) -> AsyncIterator[None]:
        """Show the connected transport's activity indicator; nothing while disconnected."""
        transport = self._transport
        if transport is None or not self._connected.is_set():
            yield
            return
        async with transport.activity_indicator(reply_plan.platform_target, reply_plan.thread_id):
            yield

    def _spawn(self, work: Coroutine[Any, Any, Any]) -> bool:
        """Run engine-owned work that stop cancels; refused once the engine stopped."""
        if self._stopped:
            work.close()
            return False
        task = asyncio.create_task(work, name=f"channel:{self._config.id}:reply")
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return True

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOGGER.error(
                "Channel reply work failed (channel=%s): %s",
                self._config.id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _waiting_scope(self, platform_target: str) -> str:
        return f"{self._config.id}:{platform_target}"

    async def _reject_overflow(self, conversation: ConversationFacts) -> None:
        """Log one rejected inbound item and send a throttled busy reply."""
        _LOGGER.warning(
            "Channel inbound work rejected by queue limit (channel=%s)",
            self._config.id,
        )
        if self._stopped or not self._should_send_busy_reply(conversation.chat_id):
            return
        await self._send_ingress_reply(self._routing._reply_plan_for(conversation), _BUSY_REPLY)

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
        await self._send_ingress_reply(
            reply_plan,
            f"The {unavailable.command} command is not available through "
            f"{self._platform_display_name()}.",
        )

    async def _execute_prepared_command(
        self,
        prepared: PreparedCommand,
        conversation: ConversationFacts,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        conversation_key: str,
        *,
        from_ingress: bool,
    ) -> None:
        """Run a Command and project its outcome.

        From the adapter's inbound handling (``from_ingress``) replies do not wait
        for a connection and Command Runs relay in the background.
        """
        send = self._send_ingress_reply if from_ingress else self._send_reply
        context = CommandExecutionContext(
            agent_id=route.agent_id,
            session_id=route.session_id,
            project_id=None,
            reply_surface=self._reply_surface(conversation.kind),
        )
        try:
            if prepared.execution_mode == "serialized":
                async with self._activity(reply_plan):
                    outcome = await self._command_dispatcher.execute(prepared, context)
            else:
                outcome = await self._command_dispatcher.execute(prepared, context)
            await self._project_command_outcome(
                outcome,
                conversation,
                reply_plan,
                conversation_key,
                from_ingress=from_ingress,
            )
        except Exception as error:
            self._log_command_failure(prepared.name, route, reply_plan, error)
            await send(reply_plan, _FAILED_REPLY)

    async def _project_command_outcome(
        self,
        outcome: CommandOutcome,
        conversation: ConversationFacts,
        reply_plan: ReplyPlanFacts,
        conversation_key: str,
        *,
        from_ingress: bool,
    ) -> None:
        send = self._send_ingress_reply if from_ingress else self._send_reply
        continued = False
        navigation = outcome.navigation
        if navigation is not None and navigation.kind in {"continue_in_session", "new_session"}:
            if navigation.agent_id != self._config.agent_id or navigation.project_id is not None:
                raise ValueError(
                    "Channel continuation navigation must stay on its configured Agent"
                )
            if navigation.kind == "new_session":
                # The conversation's next message creates the Session (``/new``).
                await self._routing._move_to_new_session_async(conversation, conversation_key)
            else:
                route = RouteFacts(agent_id=navigation.agent_id, session_id=navigation.session_id)
                await self._routing._apply_continuation_navigation_async(
                    route,
                    conversation,
                    reply_plan,
                    conversation_key,
                )
            await send(reply_plan, _NEW_SESSION_STARTED_REPLY)
            continued = True

        if not continued and outcome.feedback is not None and outcome.feedback.text.strip():
            await send(reply_plan, outcome.feedback.text)
        for command_run in outcome.runs:
            if from_ingress:
                self._spawn(self._relay_run(command_run.run, reply_plan))
            else:
                await self._relay_run(command_run.run, reply_plan)

    def _reply_surface(self, conversation_kind: Literal["direct", "group"]) -> ReplySurface:
        return ReplySurface.channel(
            platform=self._config.platform,
            platform_display_name=self._platform_display_name(),
            channel_id=self._config.id,
            conversation_kind=conversation_kind,
        )

    def _platform_display_name(self) -> str:
        transport = self._transport
        return transport.platform_display_name if transport is not None else self._config.platform

    def _log_command_failure(
        self,
        command_name: str,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        error: Exception,
    ) -> None:
        _LOGGER.error(
            "Channel command failed (command=%s channel=%s agent=%s): %s",
            command_name,
            reply_plan.channel_id,
            route.agent_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )

    # -- Lifecycle --------------------------------------------------------------------

    async def stop(self) -> None:
        """End this engine: cancel its workers and relays, keeping every owed reply.

        Queued items release their admission. What the engine still owed (the
        answers of Runs it relayed, and notices for admitted work no Run took
        over) stays recorded for the Channel's next engine.
        """
        self._stopped = True
        self._connected.clear()
        workers = list(self._chat_workers.values())
        self._chat_workers.clear()
        for queue in self._chat_queues.values():
            while queue:
                self._trigger_service.release_waiting_work(queue.popleft().admission)
        self._chat_queues.clear()
        self._busy_reply_times.clear()
        tasks = [*workers, *self._tasks]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._recordings:
            await asyncio.gather(*self._recordings, return_exceptions=True)


def _log_failed_reply(reply_plan: ReplyPlanFacts, error: Exception) -> None:
    """Log a reply whose send failed for good, saying whether part of it reached the chat."""
    if isinstance(error, ChannelError) and error.possibly_delivered:
        message = "Channel reply incomplete: part of it may have reached the chat"
    else:
        message = "Channel reply lost after retries"
    _LOGGER.error(
        "%s (channel=%s attempts=%s): %s",
        message,
        reply_plan.channel_id,
        getattr(error, "attempts_made", None),
        error,
    )


__all__ = ["ChannelConversationEngine", "ConversationTransport"]
