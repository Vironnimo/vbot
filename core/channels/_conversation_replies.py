"""Durable Channel answers and their transport lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Coroutine
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from core.channels.adapter import (
    ConversationFacts,
    PendingReply,
    PendingReplyStore,
    ReplyPlanFacts,
    RouteFacts,
    RunButtonBindingRegistry,
)
from core.channels.config import ChannelError
from core.chat.content_blocks import ContentBlock
from core.chat.messages import MessageSender, ReplySurface
from core.runs import (
    ASSISTANT_OUTPUT_EVENT,
    COMPACTION_COMPLETED_EVENT,
    USER_MESSAGE_EVENT,
    RunCancelledError,
    RunKind,
    WaitingWorkAdmission,
)
from core.utils.logging import get_logger
from core.utils.retry import retry_async, sleep_for_retry
from core.utils.timestamps import utc_now_timestamp

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.channels.config import ChannelConfig
    from core.extensions.interactions import InteractionButton
    from core.runs import Run
    from core.sessions import ChatSessionManager
from ._conversation_access import ChannelAccessPolicy
from ._conversation_content import (
    _CANCELLED_REPLY,
    _FAILED_REPLY,
    _INTERRUPTED_REPLY,
    RunReply,
    _retry_buttons,
)
from ._conversation_routing import _session_address
from ._conversation_work import (
    ConversationTransport,
    _PendingItem,
    _QueuedInternalPrompt,
    _QueuedWork,
)

_UNANSWERED_MESSAGE_REPLY = (
    "Sorry, I was interrupted before I could answer your last message. Please send it again."
)
_UNANSWERED_MESSAGES_REPLY = (
    "Sorry, I was interrupted before I could answer your last messages. Please send them again."
)
_UNANSWERED_TAP_ONLY_REPLY = (
    "Sorry, I was interrupted before I could act on your button tap. Please tap it again."
)
_UNANSWERED_TAP_UNAVAILABLE_REPLY = (
    "Sorry, I was interrupted before I could act on your button tap. Please send the request again."
)

_LOGGER = get_logger("channels.engine")


class ChannelReplies:
    """Own transport connection, owed answers, relay tasks and at-most-once delivery."""

    def __init__(
        self,
        config: ChannelConfig,
        trigger_service: TriggerService,
        chat_sessions: ChatSessionManager,
        *,
        pending_replies: PendingReplyStore,
        run_button_binding_registry: RunButtonBindingRegistry | None,
        access: ChannelAccessPolicy,
        owner: str,
    ) -> None:
        self._config = config
        self._trigger_service = trigger_service
        self._chat_sessions = chat_sessions
        self._pending_replies = pending_replies
        self._run_button_binding_registry = run_button_binding_registry
        self._access = access
        self._owner = owner
        self._transport: ConversationTransport | None = None
        self._connected = asyncio.Event()
        self._stopped = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._recordings: set[asyncio.Task[bool]] = set()

    def attach(self, transport: ConversationTransport) -> None:
        self._transport = transport
        self._connected.clear()

    def set_connected(self, connected: bool) -> None:
        if connected and self._transport is not None and not self._stopped:
            self._connected.set()
        else:
            self._connected.clear()

    def start(self) -> None:
        self.spawn(self._resume_pending_replies())

    def begin_stop(self) -> tuple[asyncio.Task[Any], ...]:
        self._stopped = True
        self._connected.clear()
        return tuple(self._tasks)

    async def finish_stop(self) -> None:
        if self._recordings:
            await asyncio.gather(*self._recordings, return_exceptions=True)

    async def trigger_and_relay(
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
        reply_surface = self.reply_surface(conversation.kind, reply_plan)
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
            await self.answer_admitted(pending, reply_plan, _CANCELLED_REPLY)
            return
        except Exception as error:
            _LOGGER.error(
                "Channel trigger run failed (channel=%s agent=%s): %s",
                reply_plan.channel_id,
                route.agent_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            await self.answer_admitted(pending, reply_plan, _FAILED_REPLY)
            return

        await self.relay(run, reply_plan, pending)

    async def answer_admitted(
        self, pending: _PendingItem | None, reply_plan: ReplyPlanFacts, text: str
    ) -> None:
        """Send the reply an admitted item gets without a Run, in place of its owed record."""
        reply_id: str | None = None
        if pending is not None:
            pending.taken = True
            if await asyncio.shield(pending.recorded):
                reply_id = pending.reply_id
        await self._deliver((reply_id,), reply_plan, text)

    async def relay(
        self, run: Run, reply_plan: ReplyPlanFacts, pending: _PendingItem | None = None
    ) -> None:
        """Owe the Run's answer durably, wait for it, and send it once."""
        reply_id = await self._owe_run_reply(run, reply_plan, pending)
        reply = await self._await_run_reply(run, reply_plan)
        await self._deliver((reply_id,), reply_plan, reply)

    async def relay_background(self, run: Run, reply_plan: ReplyPlanFacts) -> None:
        """Relay an admitted background Run with the normal Channel reply semantics.

        Returns once the Run ended. Its answer is owed durably first, so it reaches
        the chat once the Channel is connected, by this engine or its successor.
        """
        reply_id = await self._owe_run_reply(run, reply_plan, None)
        reply = await self._await_run_reply(run, reply_plan)
        if not self.spawn(self._deliver((reply_id,), reply_plan, reply)):
            _LOGGER.debug(
                "Channel engine ended before a background reply; its next engine sends it "
                "(channel=%s run=%s)",
                self._config.id,
                run.id,
            )

    async def _await_run_reply(self, run: Run, reply_plan: ReplyPlanFacts) -> str:
        """Follow the Run to its end, then project its complete durable history."""
        projection = RunReply()
        async with self.activity(reply_plan):
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

    def owe_admitted(self, queued: _QueuedWork, reply_plan: ReplyPlanFacts) -> _PendingItem:
        """Record durably that an admitted item is owed an answer, without blocking ingress."""
        reply = PendingReply(
            id=uuid4().hex,
            reply_plan=reply_plan,
            owner=self._owner,
            created_at=utc_now_timestamp(),
            binding_id=queued.binding_id if isinstance(queued, _QueuedInternalPrompt) else None,
            retry_keyboard=(
                queued.retry_keyboard if isinstance(queued, _QueuedInternalPrompt) else None
            ),
        )
        recorded = self._record_reply(reply)
        return _PendingItem(reply_id=reply.id, created_at=reply.created_at, recorded=recorded)

    async def _owe_run_reply(
        self, run: Run, reply_plan: ReplyPlanFacts, pending: _PendingItem | None
    ) -> str | None:
        """Record the Run's answer as owed; an admitted item's notice becomes that answer."""
        reply_id = uuid4().hex
        created_at = utc_now_timestamp()
        if pending is not None:
            pending.taken = True
            reply_id, created_at = pending.reply_id, pending.created_at
        reply = PendingReply(
            id=reply_id,
            reply_plan=reply_plan,
            owner=self._owner,
            created_at=created_at,
            route=RouteFacts(agent_id=run.agent_id, session_id=run.session_id),
            run_id=run.id,
        )
        recorded = self._record_reply(reply, after=pending.recorded if pending else None)
        return reply_id if await asyncio.shield(recorded) else None

    def _record_reply(
        self, reply: PendingReply, *, after: asyncio.Task[bool] | None = None
    ) -> asyncio.Task[bool]:
        """Keep admission and Run writes ordered and alive until stop drains them."""

        async def record() -> bool:
            if after is not None and not await after:
                return False
            return await self._owe(reply)

        recorded = asyncio.create_task(record(), name=f"channel:{self._config.id}:owe-reply")
        self._recordings.add(recorded)
        recorded.add_done_callback(self._recordings.discard)
        return recorded

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

    async def settle_admitted(self, pending: _PendingItem | None) -> None:
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
                self.spawn(self._resume_run_reply(reply))
        for group in unanswered.values():
            self.spawn(self._resume_unanswered(group))

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
        """Reissue each interrupted tap's keyboard; group ordinary messages into one notice."""
        messages = []
        registry = self._run_button_binding_registry
        for reply in replies:
            if reply.binding_id is None and reply.retry_keyboard is None:
                messages.append(reply)
                continue
            restored = reply.binding_id is None
            if registry is not None and reply.binding_id is not None:
                try:
                    restored = await registry.run_async(
                        registry.restore_run_button_binding, self._config.id, reply.binding_id
                    )
                except Exception as error:
                    _LOGGER.warning(
                        "Run-button binding not restored (channel=%s): %s",
                        self._config.id,
                        error,
                    )
            buttons = _retry_buttons(reply.retry_keyboard, reply.binding_id) if restored else None
            # Telegram removed the accepted keyboard. Releasing its binding alone
            # cannot make it usable; each retry message carries its own keyboard.
            text = _UNANSWERED_TAP_ONLY_REPLY if buttons else _UNANSWERED_TAP_UNAVAILABLE_REPLY
            await self._deliver((reply.id,), reply.reply_plan, text, buttons=buttons)
        if messages:
            text = _UNANSWERED_MESSAGE_REPLY if len(messages) == 1 else _UNANSWERED_MESSAGES_REPLY
            await self._deliver(
                tuple(reply.id for reply in messages), messages[-1].reply_plan, text
            )

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
        for attempt in range(3):
            claimed: list[str] = []
            try:
                for reply_id in recorded:
                    if await self._pending_replies.run_async(
                        self._pending_replies.claim_reply, self._config.id, reply_id
                    ):
                        claimed.append(reply_id)
            except Exception:
                # No transport send began. Release the known claims of a
                # grouped notice so the retry can claim the whole group again.
                for reply_id in claimed:
                    await self._update_pending(self._pending_replies.release_reply, reply_id)
                if attempt == 2:
                    raise
                _LOGGER.debug(
                    "Channel reply claim failed; retrying (channel=%s attempt=%d)",
                    self._config.id,
                    attempt + 1,
                    exc_info=True,
                )
                await sleep_for_retry(attempt)
            else:
                # Gone: deleted/reset Channel, or replies already claimed or sent.
                return tuple(claimed) if claimed else None
        raise AssertionError("claim retry did not settle")

    async def _deliver(
        self,
        reply_ids: tuple[str | None, ...],
        reply_plan: ReplyPlanFacts,
        text: str | None,
        *,
        buttons: list[list[InteractionButton]] | None = None,
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
            transport = await self.ready_transport()
            try:
                claimed = await self._claim(reply_ids)
            except Exception:
                # Sending without a durable claim would leave an owed row that
                # a later engine sends again. Keep it for that engine instead.
                _LOGGER.warning(
                    "Channel reply deferred: its send claim could not be recorded (channel=%s)",
                    self._config.id,
                    exc_info=True,
                )
                return
            if claimed is None:
                return
            try:
                send_kwargs: dict[str, Any] = {
                    "reply_to_message_id": reply_plan.reply_to_message_id,
                    "thread_id": reply_plan.thread_id,
                }
                if buttons is not None:
                    send_kwargs["buttons"] = buttons
                await retry_async(
                    transport.send_text,
                    reply_plan.platform_target,
                    text,
                    **send_kwargs,
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

    async def send(self, reply_plan: ReplyPlanFacts, text: str) -> None:
        """Send a reply that is not owed durably, waiting while the Channel is disconnected."""
        await self._deliver((None,), reply_plan, text)

    async def send_ingress(self, reply_plan: ReplyPlanFacts, text: str) -> None:
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

    async def remove_inbound(self, platform_target: str, message_id: str | None) -> bool:
        """Remove the user's message where the platform allows; never raises."""
        transport = self._transport
        if transport is None or self._stopped or message_id is None:
            return False
        try:
            return await transport.delete_message(platform_target, message_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(
                "Channel could not remove an inbound message (channel=%s): %s",
                self._config.id,
                type(error).__name__,
            )
            return False

    def require_transport(self) -> ConversationTransport:
        transport = self._transport
        if transport is None:
            raise ChannelError(f"Channel has no running adapter: {self._config.id}")
        return transport

    async def ready_transport(self) -> ConversationTransport:
        """Wait until the attached transport is connected and return it."""
        while True:
            await self._connected.wait()
            transport = self._transport
            if transport is not None and self._connected.is_set():
                return transport

    def _is_connected(self, transport: ConversationTransport) -> bool:
        return self._transport is transport and self._connected.is_set()

    @contextlib.asynccontextmanager
    async def activity(self, reply_plan: ReplyPlanFacts) -> AsyncIterator[None]:
        """Show the connected transport's activity indicator; nothing while disconnected."""
        transport = self._transport
        if transport is None or not self._connected.is_set():
            yield
            return
        async with transport.activity_indicator(reply_plan.platform_target, reply_plan.thread_id):
            yield

    def spawn(self, work: Coroutine[Any, Any, Any]) -> bool:
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

    def reply_surface(
        self,
        conversation_kind: Literal["direct", "group"],
        reply_plan: ReplyPlanFacts | None = None,
    ) -> ReplySurface:
        return ReplySurface.channel(
            platform=self._config.platform,
            platform_display_name=self.platform_display_name(),
            channel_id=self._config.id,
            conversation_kind=conversation_kind,
            platform_target=reply_plan.platform_target if reply_plan is not None else None,
            thread_id=reply_plan.thread_id if reply_plan is not None else None,
        )

    def platform_display_name(self) -> str:
        transport = self._transport
        return transport.platform_display_name if transport is not None else self._config.platform


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
