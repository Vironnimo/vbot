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
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from core.channels.adapter import (
    ChannelAccessRegistry,
    ConversationFacts,
    MessageFacts,
    QuotedMessageFacts,
    ReplyPlanFacts,
    RouteFacts,
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
from core.chat.messages import MessageSender, ReplySurface
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
from core.utils.logging import get_logger
from core.utils.retry import retry_async

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.channels.config import ChannelConfig
    from core.extensions.interactions import InteractionEvent
    from core.runs import Run
    from core.sessions import ChatSessionManager

from ._conversation_access import ChannelAccessPolicy
from ._conversation_content import (
    _assistant_output_interrupted,
    _combined_interrupted_output,
    _extract_assistant_output,
    _format_interaction_note,
    _format_observed_message,
    _media_failure_reply,
    _restore_bound_interaction_event,
    _sender_tag,
)
from ._conversation_routing import _CHANNEL_SESSION_WORKERS, ChannelSessionRouting, _session_address
from ._conversation_work import (
    ConversationTransport,
    _QueuedInboundMedia,
    _QueuedInboundMessage,
    _QueuedInternalPrompt,
    _QueuedObservedMessage,
    _QueuedPreparedCommand,
    _QueuedWork,
)

_LOGGER = get_logger("channels.engine")

_FAILED_REPLY = "Sorry, I couldn't complete that request. Please try again."
_CANCELLED_REPLY = "Sorry, this request was cancelled before completion."
_INTERRUPTED_REPLY = "Sorry, this request was interrupted before it could finish."
_EMPTY_ASSISTANT_REPLY = "I finished processing your message, but no reply text was produced."
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
        self._access = ChannelAccessPolicy(config, access_registry)
        self._routing = ChannelSessionRouting(config, chat_sessions)
        self._chat_queues: dict[str, asyncio.Queue[_QueuedWork]] = {}
        self._chat_workers: dict[str, asyncio.Task[None]] = {}
        self._busy_reply_times: OrderedDict[str, float] = OrderedDict()

    def prepare_inbound_route(
        self, conversation: ConversationFacts
    ) -> tuple[RouteFacts, ReplyPlanFacts]:
        """Resolve the active Session and update its Channel metadata."""
        return self._routing.prepare_inbound_route(conversation)

    def ensure_channel_session(self, conversation: ConversationFacts) -> RouteFacts:
        """Ensure an outbound Channel target has its routed Session."""
        return self._routing.ensure_channel_session(conversation)

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
    ) -> None:
        """Gate one inbound text and execute or enqueue a prepared command/message."""
        conversation = self._access._snapshot_group_sender(conversation)
        prepared_command = self._command_dispatcher.prepare(message_text)
        if prepared_command is not None:
            # Commands are inherently addressed; group commands are gated by sender
            # authorization instead of response mode. Authorization precedes both
            # availability projection and every command side effect.
            if conversation.kind == "group" and not self._access._command_sender_authorized(
                conversation
            ):
                _LOGGER.info(
                    "Channel command denied for member (channel=%s chat=%s user=%s)",
                    self._config.id,
                    conversation.chat_id,
                    conversation.user_id,
                )
                return
            reply_plan = self._routing._reply_plan_for(conversation)
            unavailable = self._command_dispatcher.unavailability(
                prepared_command, self._reply_surface(conversation.kind)
            )
            if unavailable is not None:
                await self._send_command_unavailability(reply_plan, unavailable)
                return
            if prepared_command.execution_mode == "immediate":
                route, reply_plan = await self._routing._prepare_inbound_route_async(conversation)
                await self._execute_prepared_command(
                    prepared_command,
                    conversation,
                    route,
                    reply_plan,
                    self._routing._derive_session_id(conversation),
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

        if not self._access.should_respond(conversation, (message_text,)):
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
        conversation = self._access._snapshot_group_sender(conversation)
        normalized_companion = companion_text.strip() if companion_text is not None else None
        if normalized_companion == "":
            normalized_companion = None
        caption_texts = tuple(self._transport.caption_text(message) for message in raw_messages)
        gating_texts = (
            *((normalized_companion,) if normalized_companion is not None else ()),
            *caption_texts,
        )
        if not self._access.should_respond(conversation, gating_texts):
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
        conversation = self._access._snapshot_group_sender(conversation)
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
        conversation = self._access._snapshot_group_sender(conversation)
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
        conversation = self._access._snapshot_group_sender(conversation)
        if not self._access._command_sender_authorized(conversation):
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
                self._routing._point_conversation_at_session,
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
            self._routing._restore_conversation_pointer,
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
            route, reply_plan = await self._routing._prepare_inbound_route_async(
                queued.conversation
            )
            await self._execute_prepared_command(
                queued.command,
                queued.conversation,
                route,
                reply_plan,
                self._routing._derive_session_id(queued.conversation),
            )
            return
        if isinstance(queued, _QueuedInboundMedia):
            await self._process_queued_media(queued)
            return
        if isinstance(queued, _QueuedInternalPrompt):
            route, reply_plan = await self._routing._prepare_inbound_route_async(
                queued.conversation
            )
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
        route, _reply_plan = await self._routing._prepare_inbound_route_async(queued.conversation)
        # Wait for any open tool cycle on this shared session (a Run via another
        # accessor) so the observed note lands after the cycle, never inside it.
        async with self._chat_sessions.write_lock(
            _session_address(route.agent_id, route.session_id)
        ):
            await _CHANNEL_SESSION_WORKERS.run(
                self._routing._append_session_note,
                route.agent_id,
                route.session_id,
                queued.note,
            )

    async def _process_queued_message(self, queued: _QueuedInboundMessage) -> None:
        # Prepared commands have their own queued-work type; only plain messages
        # reach this path, so processing goes straight to trigger/relay.
        route, reply_plan = await self._routing._prepare_inbound_route_async(queued.conversation)
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
            sender=self._access._sender_for(queued.conversation),
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

        quoted_conversation = self._access._snapshot_group_sender(
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
            sender=self._access._sender_for(queued.conversation),
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
        await self._send_reply(self._routing._reply_plan_for(conversation), _BUSY_REPLY)

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
                self._routing._apply_continuation_navigation,
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

    def _reply_surface(self, conversation_kind: Literal["direct", "group"]) -> ReplySurface:
        return ReplySurface.channel(
            platform=self._config.platform,
            platform_display_name=self._transport.platform_display_name,
            channel_id=self._config.id,
            conversation_kind=conversation_kind,
        )

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


__all__ = ["ChannelConversationEngine", "ConversationTransport"]
