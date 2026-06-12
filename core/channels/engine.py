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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from core.attachments import AttachmentTooLargeError, AttachmentTypeNotAllowedError
from core.channels.adapter import (
    ConversationFacts,
    MessageFacts,
    ReplyPlanFacts,
    RouteFacts,
)
from core.chat.commands import CommandAction, CommandDispatcher, CommandHandled
from core.chat.content_blocks import ContentBlock
from core.runs import (
    ASSISTANT_OUTPUT_EVENT,
    RUN_CANCELLED_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.channels.channels import ChannelConfig
    from core.runs import Run, RunEvent
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("channels.engine")

_FAILED_REPLY = "Sorry, I couldn't complete that request. Please try again."
_CANCELLED_REPLY = "Sorry, this request was cancelled before completion."
_EMPTY_ASSISTANT_REPLY = "I finished processing your message, but no reply text was produced."
_UNSUPPORTED_FILE_REPLY = "Sorry, this file type isn't supported yet."
_FILE_TOO_LARGE_REPLY = "Sorry, this file is too large to process."
_MEDIA_FAILED_REPLY = "Sorry, I couldn't process the attached file. Please try again."
_SYSTEM_REMINDER_TEMPLATE = (
    "This session is receiving messages via {platform} "
    "(channel: {channel_id}, chat: {chat_id}).\n"
    "Respond in a style appropriate for {platform} messaging."
)


class ConversationTransport(Protocol):
    """Platform I/O surface the engine drives.

    The adapter implements this; the engine stays free of platform libraries. Raw platform
    messages are opaque to the engine and only ``build_media_blocks`` understands them.
    """

    @property
    def platform_display_name(self) -> str:
        """Human-facing platform name used verbatim in reply and reminder text."""

    async def send_text(self, platform_target: str, text: str) -> None:
        """Deliver one outbound text reply to a platform target."""

    def activity_indicator(
        self, platform_target: str
    ) -> contextlib.AbstractAsyncContextManager[None]:
        """Show a best-effort activity indicator for a target until the block exits."""

    async def build_media_blocks(self, raw_message: Any) -> list[ContentBlock]:
        """Convert one raw platform message into canonical content blocks."""


@dataclass(slots=True, frozen=True)
class _QueuedInboundMessage:
    route: RouteFacts
    reply_plan: ReplyPlanFacts
    message: MessageFacts
    command_checked: bool = False


@dataclass(slots=True, frozen=True)
class _QueuedCommandAction:
    route: RouteFacts
    reply_plan: ReplyPlanFacts
    action: CommandAction


@dataclass(slots=True, frozen=True)
class _QueuedInboundMedia:
    route: RouteFacts
    reply_plan: ReplyPlanFacts
    # Raw platform messages; conversion to content blocks happens in the per-conversation
    # worker via the transport so the adapter's update pipeline never blocks.
    messages: tuple[Any, ...]


_QueuedWork = _QueuedInboundMessage | _QueuedCommandAction | _QueuedInboundMedia


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
        self._chat_queues: dict[str, asyncio.Queue[_QueuedWork]] = {}
        self._chat_workers: dict[str, asyncio.Task[None]] = {}

    # -- Inbound entry points ---------------------------------------------------------

    async def handle_inbound_text(
        self,
        conversation: ConversationFacts,
        message_text: str,
    ) -> None:
        """Route, eagerly command-dispatch, and enqueue one inbound text message."""
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
        ):
            return

        self._enqueue_chat_work(
            reply_plan.platform_target,
            _QueuedInboundMessage(
                route=route,
                reply_plan=reply_plan,
                message=MessageFacts(content=message_text),
                command_checked=True,
            ),
        )

    def enqueue_media(
        self,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        messages: tuple[Any, ...],
    ) -> None:
        """Enqueue inbound media (one message or a buffered album) for worker processing."""
        self._enqueue_chat_work(
            reply_plan.platform_target,
            _QueuedInboundMedia(route=route, reply_plan=reply_plan, messages=messages),
        )

    def prepare_inbound_route(
        self,
        conversation: ConversationFacts,
    ) -> tuple[RouteFacts, ReplyPlanFacts]:
        """Ensure the routed Session exists and refresh its channel metadata."""
        route = self._ensure_channel_session(conversation)
        reply_plan = ReplyPlanFacts(
            channel_id=self._config.id,
            platform_target=conversation.chat_id,
        )
        self._update_session_metadata(route, conversation, reply_plan)
        return route, reply_plan

    def ensure_channel_session(self, conversation: ConversationFacts) -> RouteFacts:
        """Ensure the Session mirroring a conversation exists with channel context."""
        return self._ensure_channel_session(conversation)

    # -- Session routing / metadata ---------------------------------------------------

    def _ensure_channel_session(self, conversation: ConversationFacts) -> RouteFacts:
        route = self._route_facts(conversation)
        is_new_session = not self._session_exists(route)
        session = self._chat_sessions.get_or_create(route.agent_id, route.session_id)
        if is_new_session:
            session.add_note(
                _SYSTEM_REMINDER_TEMPLATE.format(
                    platform=self._transport.platform_display_name,
                    channel_id=self._config.id,
                    chat_id=conversation.chat_id,
                )
            )
        return route

    def _route_facts(self, conversation: ConversationFacts) -> RouteFacts:
        return RouteFacts(
            agent_id=self._config.agent_id,
            session_id=self._derive_session_id(conversation),
        )

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
        self._chat_sessions.set_metadata(route.agent_id, route.session_id, metadata)

    # -- Queue / workers --------------------------------------------------------------

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
        if isinstance(queued, _QueuedCommandAction):
            await self._handle_command_action(queued.action, queued.route, queued.reply_plan)
            return
        if isinstance(queued, _QueuedInboundMedia):
            await self._process_queued_media(queued)
            return
        await self._process_queued_message(queued)

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
            ):
                return

        await self._trigger_and_relay(queued.route, queued.reply_plan, queued.message.content)

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
            await self._transport.send_text(queued.reply_plan.platform_target, reply)

        if not content_blocks:
            return

        await self._trigger_and_relay(queued.route, queued.reply_plan, content_blocks)

    # -- Trigger / relay --------------------------------------------------------------

    async def _trigger_and_relay(
        self,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        content: str | list[ContentBlock],
    ) -> None:
        try:
            run = await self._trigger_service.trigger_run(
                route.agent_id,
                content,
                route.session_id,
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
            await self._transport.send_text(reply_plan.platform_target, _FAILED_REPLY)
            return

        await self._relay_run_events(run, reply_plan.platform_target)

    async def _relay_run_events(self, run: Run, platform_target: str) -> None:
        assistant_text: str | None = None
        reply: str | None = None

        async with self._transport.activity_indicator(platform_target):
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
            await self._transport.send_text(platform_target, reply)

    # -- Command actions --------------------------------------------------------------

    async def _handle_dispatch_result(
        self,
        dispatch_result: object,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        *,
        defer_actions: bool,
    ) -> bool:
        if isinstance(dispatch_result, CommandHandled):
            reply = dispatch_result.reply
            if isinstance(reply, str) and reply.strip():
                await self._transport.send_text(reply_plan.platform_target, reply)
            return True

        if isinstance(dispatch_result, CommandAction):
            if defer_actions:
                # Command actions can run long (compact = model call, retry = full Run
                # relay). The adapter feeds updates sequentially, so they must not be
                # awaited in the update handler; the per-conversation worker owns slow work.
                self._enqueue_chat_work(
                    reply_plan.platform_target,
                    _QueuedCommandAction(
                        route=route, reply_plan=reply_plan, action=dispatch_result
                    ),
                )
            else:
                await self._handle_command_action(dispatch_result, route, reply_plan)
            return True

        return False

    async def _handle_command_action(
        self,
        command_action: CommandAction,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
    ) -> None:
        platform = self._transport.platform_display_name
        match command_action.name:
            case "compact":
                try:
                    async with self._transport.activity_indicator(reply_plan.platform_target):
                        reply = await self._trigger_service.compact_session(
                            route.agent_id,
                            route.session_id,
                        )
                except Exception as error:
                    self._log_command_action_failure(command_action.name, route, reply_plan, error)
                    reply = _FAILED_REPLY
                await self._transport.send_text(reply_plan.platform_target, reply)
            case "new_session":
                await self._transport.send_text(
                    reply_plan.platform_target,
                    f"Starting a new session is not available from {platform} channels yet.",
                )
            case "retry_last_turn":
                try:
                    run = await self._trigger_service.retry_run(
                        route.agent_id,
                        route.session_id,
                    )
                except Exception as error:
                    self._log_command_action_failure(command_action.name, route, reply_plan, error)
                    await self._transport.send_text(reply_plan.platform_target, _FAILED_REPLY)
                    return
                await self._relay_run_events(run, reply_plan.platform_target)
            case _:
                # Recognized commands without a channel implementation (e.g. /handoff)
                # must reply instead of silently swallowing the message.
                await self._transport.send_text(
                    reply_plan.platform_target,
                    f"This command is not available from {platform} channels yet.",
                )

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
