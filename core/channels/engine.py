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
import time
from collections import OrderedDict, deque
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4
from weakref import WeakValueDictionary

from core.channels.adapter import (
    ChannelAccessRegistry,
    ConversationFacts,
    ConversationPointerStore,
    MessageFacts,
    PendingReplyStore,
    QuotedMessageFacts,
    ReplyPlanFacts,
    RouteFacts,
    RunButtonBindingRegistry,
    RunButtonClaim,
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
from core.runs import (
    WaitingWorkLimitError,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.channels.config import ChannelConfig
    from core.extensions.interactions import InteractionEvent
    from core.runs import Run
    from core.sessions import ChatSessionManager

from ._conversation_access import ChannelAccessPolicy
from ._conversation_content import (
    _FAILED_REPLY,
    _format_interaction_note,
    _format_observed_message,
    _interaction_retry_keyboard,
    _media_failure_reply,
    _restore_bound_interaction_event,
    _sender_tag,
)
from ._conversation_replies import ChannelReplies
from ._conversation_routing import ChannelSessionRouting, _session_address
from ._conversation_work import (
    ConversationTransport,
    _QueuedInboundMedia,
    _QueuedInboundMessage,
    _QueuedInternalPrompt,
    _QueuedObservedMessage,
    _QueuedPreparedCommand,
    _QueuedWork,
)
from ._observations import ChannelObservationInbox

_LOGGER = get_logger("channels.engine")

# Sent for admitted work that an ended engine left before any Run took it over.


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
        observation_inbox: ChannelObservationInbox,
        previous_observations: tuple[asyncio.Task[None], ...] = (),
        run_button_binding_registry: RunButtonBindingRegistry | None = None,
        access_registry: ChannelAccessRegistry | None = None,
    ) -> None:
        self._config = config
        self._trigger_service = trigger_service
        self._chat_sessions = chat_sessions
        # The running adapter's transport, and whether its platform connection is
        # up; replies wait for a connected transport.
        self._observation_inbox = observation_inbox
        self._previous_observations = previous_observations
        self._observation_recordings: set[asyncio.Task[None]] = set()
        self._has_observations = False
        # Names this engine on the replies it records; the Channel's next engine
        # sends the ones it left.
        self._owner = uuid4().hex
        self._started = False
        self._stopped = False
        # Relays and deliveries that outlive the call that started them.
        # Durable records of admitted work and the Runs that answer it; stop
        # lets them finish instead of cancelling either write.
        self._command_dispatcher = command_dispatcher
        self._run_button_binding_registry = run_button_binding_registry
        self._access = ChannelAccessPolicy(config, access_registry)
        self._conversation_pointers = conversation_pointers
        self._routing = ChannelSessionRouting(config, chat_sessions, conversation_pointers)
        self._replies = ChannelReplies(
            config,
            trigger_service,
            chat_sessions,
            pending_replies=pending_replies,
            run_button_binding_registry=run_button_binding_registry,
            access=self._access,
            owner=self._owner,
        )

        # One FIFO and worker per conversation while it has work; an idle
        # conversation retires both and the next item creates them again.
        self._ingress_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._chat_queues: dict[str, deque[_QueuedWork]] = {}
        self._chat_workers: dict[str, asyncio.Task[None]] = {}
        self._busy_reply_times: OrderedDict[str, float] = OrderedDict()
        self._bound_tap_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    @property
    def config(self) -> ChannelConfig:
        """The Channel configuration this engine serves."""
        return self._config

    @property
    def observation_owner(self) -> str:
        """Identify this engine's inbox rows so startup cannot overtake live FIFO work."""
        return self._owner

    def attach(self, transport: ConversationTransport) -> None:
        """Use ``transport`` for this Channel's platform I/O; replies wait until it connects."""
        self._replies.attach(transport)

    def set_connected(self, connected: bool) -> None:
        """Record whether the attached transport's platform connection is up."""
        self._replies.set_connected(connected)

    def start(self) -> None:
        """Begin sending the replies the Channel's earlier engines left owed."""
        if self._started or self._stopped:
            return
        self._started = True
        self._replies.start()

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
                prepared_command, self._replies.reply_surface(conversation.kind)
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
            if not await self._enqueue_chat_work(
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
                return await self._enqueue_observed_message(
                    conversation,
                    _format_observed_message(conversation, message_text),
                )
            return False

        if not await self._enqueue_chat_work(
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
        transport = self._replies.require_transport()
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
                    return await self._enqueue_observed_message(
                        conversation, notes[0], following_notes=tuple(notes[1:])
                    )
            return False

        if not await self._enqueue_chat_work(
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
        if await self._enqueue_chat_work(
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
            if await self._enqueue_chat_work(
                conversation.chat_id,
                _QueuedInternalPrompt(
                    conversation=conversation,
                    prompt=_format_interaction_note(conversation, event),
                    retry_keyboard=_interaction_retry_keyboard(event),
                ),
            ):
                return "enqueued"
            await self._reject_overflow(conversation)
            return "busy"

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
                admitted = await self._enqueue_chat_work(
                    conversation.chat_id,
                    _QueuedInternalPrompt(
                        conversation=conversation,
                        prompt=_format_interaction_note(conversation, restored_event),
                        route=RouteFacts(
                            agent_id=self._config.agent_id,
                            session_id=origin_session_id,
                        ),
                        binding_id=claim.binding.id,
                        retry_keyboard=_interaction_retry_keyboard(event),
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

    async def _enqueue_observed_message(
        self, conversation: ConversationFacts, note: str, *, following_notes: tuple[str, ...] = ()
    ) -> bool:
        if await self._enqueue_chat_work(
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

    async def _enqueue_chat_work(self, platform_target: str, queued: _QueuedWork) -> bool:
        # Durable admission yields; retain arrival order across those worker hops.
        lock = self._ingress_locks.setdefault(platform_target, asyncio.Lock())
        async with lock:
            return await self._admit_chat_work(platform_target, queued)

    async def _admit_chat_work(self, platform_target: str, queued: _QueuedWork) -> bool:
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

        observations = (
            (queued,)
            if isinstance(queued, _QueuedObservedMessage)
            else queued.observed_context
            if isinstance(queued, (_QueuedInboundMessage, _QueuedInboundMedia))
            else ()
        )
        if observations:
            self._has_observations = True
            recording = asyncio.create_task(
                self._observation_inbox.record(
                    self._owner, self._config, self._routing, observations
                )
            )
            self._observation_recordings.add(recording)
            try:
                await asyncio.shield(recording)
            except BaseException:
                self._trigger_service.release_waiting_work(admission)
                raise
            finally:
                if recording.done():
                    self._observation_recordings.discard(recording)
            if self._stopped:
                self._trigger_service.release_waiting_work(admission)
                return True

        queue = self._chat_queues.get(platform_target)
        if queue is None:
            queue = deque()
            self._chat_queues[platform_target] = queue

        pending = (
            None
            if isinstance(queued, _QueuedObservedMessage)
            else self._replies.owe_admitted(
                queued, self._routing._reply_plan_for(queued.conversation)
            )
        )
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

        Admission may await durable context recording. Queue append and worker
        selection do not await, nor does the empty check before retirement, so
        later admitted work joins this worker or starts the next one: one worker
        per conversation at a time, in arrival order.
        """
        try:
            for recovery in self._previous_observations:
                await asyncio.shield(recovery)
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
                await self._replies.settle_admitted(queued.pending)
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
                    await self._replies.send(reply_plan, _FAILED_REPLY)
                    return
                await self._chat_sessions.run_async(
                    self._routing._update_session_metadata,
                    route,
                    queued.conversation,
                    reply_plan,
                )
            await self._replies.trigger_and_relay(
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
        await self._observation_inbox.prepare(queued.delivery_id, self._routing)
        await self._observation_inbox.deliver(queued.delivery_id)

    async def _process_queued_message(self, queued: _QueuedInboundMessage) -> None:
        # Prepared commands have their own queued-work type; only plain messages
        # reach this path, so processing goes straight to trigger/relay.
        route, reply_plan = await self._routing._prepare_inbound_route_async(queued.conversation)
        content: str | list[ContentBlock] = queued.message.content
        if queued.conversation.kind == "group" and queued.raw_message is not None:
            try:
                transport = await self._replies.ready_transport()
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

        await self._replies.trigger_and_relay(
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
                transport = await self._replies.ready_transport()
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
            await self._replies.send(reply_plan, reply)

        if not content_blocks:
            return

        await self._replies.trigger_and_relay(
            route,
            reply_plan,
            content_blocks,
            conversation=queued.conversation,
            sender=self._access._sender_for(queued.conversation),
            waiting_work_admission=queued.admission,
            pending=queued.pending,
        )

    # -- Trigger / relay --------------------------------------------------------------

    async def relay_run(self, run: Run, reply_plan: ReplyPlanFacts) -> None:
        """Relay a background Run through the durable Channel reply lifecycle."""
        await self._replies.relay_background(run, reply_plan)

    # -- Owed replies -----------------------------------------------------------------

    # -- Sending ----------------------------------------------------------------------

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
        await self._replies.send_ingress(self._routing._reply_plan_for(conversation), _BUSY_REPLY)

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
        if unavailable.conversation == "group":
            text = f"The {unavailable.command} command is not available in group chats."
        else:
            text = (
                f"The {unavailable.command} command is not available through "
                f"{self._replies.platform_display_name()}."
            )
        await self._replies.send_ingress(reply_plan, text)

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
        send = self._replies.send_ingress if from_ingress else self._replies.send
        context = CommandExecutionContext(
            agent_id=route.agent_id,
            session_id=route.session_id,
            project_id=None,
            reply_surface=self._replies.reply_surface(conversation.kind, reply_plan),
            sender=self._access._sender_for(conversation),
        )
        try:
            if prepared.execution_mode == "serialized":
                async with self._replies.activity(reply_plan):
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
        send = self._replies.send_ingress if from_ingress else self._replies.send
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
                self._replies.spawn(self._replies.relay(command_run.run, reply_plan))
            else:
                await self._replies.relay(command_run.run, reply_plan)

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
        workers = list(self._chat_workers.values())
        self._chat_workers.clear()
        for queue in self._chat_queues.values():
            while queue:
                self._trigger_service.release_waiting_work(queue.popleft().admission)
        self._chat_queues.clear()
        self._busy_reply_times.clear()
        tasks = [*workers, *self._replies.begin_stop()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._replies.finish_stop()
        if self._observation_recordings:
            await asyncio.gather(*self._observation_recordings)
            self._observation_recordings.clear()
        # Preserve routes before config changes clear pointers or reassign the Agent.
        # This never takes a Session write lease, which shutdown's active Run holds.
        if self._has_observations:
            await self._observation_inbox.freeze(self._owner, self._routing)

    async def recover_observations(self) -> None:
        """Finish this ended engine's Notes after stop returned."""
        await self._observation_inbox.recover(self._owner)


__all__ = ["ChannelConversationEngine", "ConversationTransport"]
