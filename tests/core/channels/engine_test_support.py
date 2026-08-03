"""Shared transport, builders, and dependencies for Channel engine tests."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import core.channels.engine as engine_module
from core.attachments import AttachmentTooLargeError, AttachmentTypeNotAllowedError
from core.channels.adapter import (
    ChannelAccessRegistry,
    ConversationFacts,
    MessageFacts,
    QuotedMessageFacts,
    ReplyPlanFacts,
    RouteFacts,
    RunButtonBindingRegistry,
)
from core.channels.channels import ChannelConfig
from core.channels.engine import ChannelConversationEngine
from core.chat import MessageSender, ReplySurface
from core.chat.commands import (
    CommandFeedback,
    CommandNavigation,
    CommandOutcome,
    CommandRun,
    CommandUnavailability,
    PreparedCommand,
)
from core.chat.content_blocks import ContentBlock, MediaBlock, TextBlock
from core.chat.messages import GroupRole
from core.extensions.interactions import InteractionButton, InteractionEvent
from core.runs import (
    ASSISTANT_OUTPUT_EVENT,
    ChatRunManager,
    Run,
    RunInterruptedError,
    RunKind,
    WaitingWorkAdmission,
)
from core.sessions import ChatSessionManager

SESSION_ID = "ch-tg-assistant-12345"
CHANNEL_REPLY_SURFACE = ReplySurface.channel(
    platform="telegram",
    platform_display_name="Telegram",
    channel_id="tg-assistant",
)
CHANNEL_GROUP_REPLY_SURFACE = ReplySurface.channel(
    platform="telegram",
    platform_display_name="Telegram",
    channel_id="tg-assistant",
    conversation_kind="group",
)


class MemoryChannelAccessRegistry(ChannelAccessRegistry):
    """Small live access registry for adapter/engine unit tests."""

    def __init__(self, admin_user_ids: list[str] | None = None) -> None:
        self.admin_user_ids = set(admin_user_ids or [])
        self.participants: dict[str, dict[str, str]] = {}

    def snapshot_participant_role(
        self,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
        display_name: str,
    ) -> GroupRole:
        del channel_id
        self.participants.setdefault(access_scope_id, {})[user_id] = display_name
        return self.role_for("", access_scope_id, user_id)

    def role_for(self, channel_id: str, access_scope_id: str, user_id: str) -> GroupRole:
        del channel_id, access_scope_id
        return "admin" if user_id in self.admin_user_ids else "member"


class FakeTransport:
    """Minimal ConversationTransport for engine tests; records outbound text."""

    platform_display_name = "Telegram"

    def __init__(
        self,
        *,
        media_builder: Callable[[Any], Awaitable[list[ContentBlock]]] | None = None,
        quoted_builder: Callable[[Any], Awaitable[QuotedMessageFacts | None]] | None = None,
    ) -> None:
        self.sent: list[tuple[str, str]] = []
        self.sent_reply_targets: list[str | None] = []
        self.sent_thread_ids: list[str | None] = []
        self.activity_targets: list[str] = []
        self.activity_thread_ids: list[str | None] = []
        self._media_builder = media_builder
        self._quoted_builder = quoted_builder

    async def send_text(
        self,
        platform_target: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        self.sent.append((platform_target, text))
        self.sent_reply_targets.append(reply_to_message_id)
        self.sent_thread_ids.append(thread_id)

    @contextlib.asynccontextmanager
    async def activity_indicator(
        self, platform_target: str, thread_id: str | None = None
    ) -> AsyncIterator[None]:
        self.activity_targets.append(platform_target)
        self.activity_thread_ids.append(thread_id)
        yield

    async def build_media_blocks(self, raw_message: Any) -> list[ContentBlock]:
        if self._media_builder is None:
            raise AssertionError("media builder not configured for this test")
        return await self._media_builder(raw_message)

    async def build_quoted_message(self, raw_message: Any) -> QuotedMessageFacts | None:
        if self._quoted_builder is None:
            return None
        return await self._quoted_builder(raw_message)

    def caption_text(self, raw_message: Any) -> str | None:
        return getattr(raw_message, "caption", None)

    @property
    def sent_texts(self) -> list[str]:
        return [text for _target, text in self.sent]


def make_config(
    *,
    dm_scope: str = "per_conversation",
    response_mode: str = "mention",
    mention_patterns: list[str] | None = None,
    observe_unaddressed: bool = False,
) -> ChannelConfig:
    return ChannelConfig(
        id="tg-assistant",
        platform="telegram",
        agent_id="assistant",
        dm_scope=dm_scope,
        allowed_chat_ids=["12345"],
        token_env_var="TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
        enabled=True,
        response_mode=response_mode,
        mention_patterns=list(mention_patterns or []),
        observe_unaddressed=observe_unaddressed,
    )


def make_conversation(
    *,
    chat_id: int = 12345,
    user_id: int | str = 50,
    kind: str = "direct",
    user_display_name: str | None = None,
    message_id: str | None = None,
    thread_id: str | None = None,
    mentioned_bot: bool = False,
    is_reply_to_bot: bool = False,
) -> ConversationFacts:
    return ConversationFacts(
        platform="telegram",
        channel_id="tg-assistant",
        chat_id=str(chat_id),
        user_id=str(user_id),
        access_scope_id=str(chat_id) if kind == "group" else None,
        thread_id=thread_id,
        kind=cast(Any, kind),
        user_display_name=user_display_name,
        message_id=message_id,
        mentioned_bot=mentioned_bot,
        is_reply_to_bot=is_reply_to_bot,
    )


def command_outcome(
    command: str,
    text: str,
    *,
    kind: str = "notice",
) -> CommandOutcome:
    return CommandOutcome(
        command=command,
        feedback=CommandFeedback(kind=cast(Any, kind), text=text),
    )


def make_command_dispatcher(
    *,
    result: CommandOutcome | None = None,
    argument: str | None = None,
    execution_mode: str | None = None,
    unavailable: CommandUnavailability | None = None,
) -> SimpleNamespace:
    prepared = (
        PreparedCommand(
            name=result.command,
            argument=argument,
            execution_mode=cast(
                Any,
                execution_mode
                or ("immediate" if result.command in {"help", "status", "stop"} else "serialized"),
            ),
        )
        if result is not None
        else None
    )
    return SimpleNamespace(
        prepare=Mock(side_effect=lambda text: prepared if text.strip().startswith("/") else None),
        unavailability=Mock(return_value=unavailable),
        execute=AsyncMock(return_value=result),
    )


def make_new_only_dispatcher() -> SimpleNamespace:
    """Dispatcher whose core execution creates the preferred `/new` Session."""

    dispatcher = SimpleNamespace(chat_sessions=None)

    def prepare(text: str) -> PreparedCommand | None:
        if text.strip() != "/new":
            return None
        return PreparedCommand(
            name="new",
            argument=None,
            execution_mode="serialized",
            accepts_preferred_session_id=True,
        )

    async def execute(_prepared: PreparedCommand, context: Any) -> CommandOutcome:
        if dispatcher.chat_sessions is None:
            raise AssertionError("new-only dispatcher was not bound to ChatSessionManager")
        session = dispatcher.chat_sessions.create(
            context.agent_id,
            session_id=context.preferred_new_session_id,
        )
        return CommandOutcome(
            command="new",
            feedback=CommandFeedback(kind="notice", text=f"New session started: {session.id}"),
            facts={"session_id": session.id},
            navigation=CommandNavigation(
                kind="continue_in_session",
                agent_id=context.agent_id,
                session_id=session.id,
            ),
        )

    dispatcher.prepare = Mock(side_effect=prepare)
    dispatcher.unavailability = Mock(return_value=None)
    dispatcher.execute = AsyncMock(side_effect=execute)
    return dispatcher


def make_completed_run(*, output_text: str, session_id: str = SESSION_ID) -> Run:
    run = Run(run_id="run-completed", agent_id="assistant", session_id=session_id)
    run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": output_text}})
    run.mark_completed("ok")
    return run


def make_empty_completed_run(*, session_id: str = SESSION_ID) -> Run:
    run = Run(run_id="run-empty", agent_id="assistant", session_id=session_id)
    run.mark_completed("ok")
    return run


def make_failed_run(*, message: str, session_id: str = SESSION_ID) -> Run:
    run = Run(run_id="run-failed", agent_id="assistant", session_id=session_id)
    run.mark_failed(RuntimeError(message))
    return run


def make_cancelled_run(*, session_id: str = SESSION_ID) -> Run:
    run = Run(run_id="run-cancelled", agent_id="assistant", session_id=session_id)
    run.mark_cancelled()
    return run


def make_interrupted_run(*, output_text: str | None, session_id: str = SESSION_ID) -> Run:
    run = Run(run_id="run-interrupted", agent_id="assistant", session_id=session_id)
    if output_text is not None:
        run.emit(
            ASSISTANT_OUTPUT_EVENT,
            {"message": {"content": output_text, "interrupted": True}},
        )
    run.mark_interrupted(RunInterruptedError("network", result=output_text))
    return run


def make_engine(
    tmp_path: Path,
    *,
    dm_scope: str = "per_conversation",
    response_mode: str = "mention",
    mention_patterns: list[str] | None = None,
    admin_user_ids: list[str] | None = None,
    observe_unaddressed: bool = False,
    trigger_run: AsyncMock | None = None,
    compact_session: AsyncMock | None = None,
    has_active_run: Mock | None = None,
    command_dispatcher: object | None = None,
    transport: FakeTransport | None = None,
    waiting_work_manager: ChatRunManager | None = None,
    run_button_binding_registry: RunButtonBindingRegistry | None = None,
    access_registry: ChannelAccessRegistry | None = None,
) -> tuple[ChannelConversationEngine, ChatSessionManager, AsyncMock, FakeTransport]:
    chat_sessions = ChatSessionManager(tmp_path)
    trigger_mock = trigger_run or AsyncMock()

    async def trigger_with_admission(*args: Any, **kwargs: Any) -> Any:
        admission = kwargs.pop("waiting_work_admission", None)
        if waiting_work_manager is not None and isinstance(admission, WaitingWorkAdmission):
            waiting_work_manager.release_waiting_work(admission)
        return await trigger_mock(*args, **kwargs)

    admission_ids = count()

    def reserve_waiting_work(*, scope: str, scope_limit: int) -> WaitingWorkAdmission:
        if waiting_work_manager is not None:
            return waiting_work_manager.reserve_waiting_work(
                scope=scope,
                scope_limit=scope_limit,
            )
        del scope_limit
        return WaitingWorkAdmission(id=f"admission-{next(admission_ids)}", scope=scope)

    def release_waiting_work(admission: WaitingWorkAdmission | None) -> bool:
        if waiting_work_manager is not None and admission is not None:
            return waiting_work_manager.release_waiting_work(admission)
        return True

    trigger_service = SimpleNamespace(
        trigger_run=trigger_with_admission,
        compact_session=compact_session or AsyncMock(return_value="Context compacted."),
        # Synchronous on purpose: the real has_active_run returns a bool, not a
        # coroutine. An AsyncMock would return a truthy coroutine -> always "busy".
        has_active_run=has_active_run or Mock(return_value=False),
        reserve_waiting_work=reserve_waiting_work,
        release_waiting_work=release_waiting_work,
    )
    resolved_transport = transport or FakeTransport()
    resolved_dispatcher = command_dispatcher or make_command_dispatcher()
    if hasattr(resolved_dispatcher, "chat_sessions"):
        resolved_dispatcher.chat_sessions = chat_sessions
    engine = ChannelConversationEngine(
        make_config(
            dm_scope=dm_scope,
            response_mode=response_mode,
            mention_patterns=mention_patterns,
            observe_unaddressed=observe_unaddressed,
        ),
        cast(Any, trigger_service),
        cast(Any, chat_sessions),
        cast(Any, resolved_transport),
        command_dispatcher=cast(Any, resolved_dispatcher),
        run_button_binding_registry=run_button_binding_registry,
        access_registry=access_registry
        or cast(
            ChannelAccessRegistry,
            MemoryChannelAccessRegistry(list(admin_user_ids or [])),
        ),
    )
    return engine, chat_sessions, trigger_mock, resolved_transport


async def drain(engine: ChannelConversationEngine, platform_target: int) -> None:
    queue = engine._chat_queues.get(str(platform_target))
    if queue is None:
        await asyncio.sleep(0)
        return
    # Generous timeout: xdist load can delay the worker task noticeably.
    await asyncio.wait_for(queue.join(), timeout=5)


def assert_member_trigger(
    trigger_mock: AsyncMock,
    *args: Any,
    sender: MessageSender,
    reply_surface: ReplySurface = CHANNEL_GROUP_REPLY_SURFACE,
) -> Callable[[str], str | None]:
    """Assert the stable member Run inputs and return its live denial resolver."""
    assert trigger_mock.await_count == 1
    awaited_args = trigger_mock.await_args
    assert awaited_args is not None
    assert awaited_args.args == args
    kwargs = dict(awaited_args.kwargs)
    assert kwargs.pop("sender") == sender
    assert kwargs.pop("reply_surface") == reply_surface
    assert kwargs.pop("tool_restriction") == ("web_search", "web_fetch")
    assert kwargs.pop("run_kind") is RunKind.CHANNEL
    resolver = kwargs.pop("tool_denial_resolver")
    assert callable(resolver)
    assert kwargs == {}
    return cast(Callable[[str], str | None], resolver)


__all__ = [
    "asyncio",
    "contextlib",
    "logging",
    "AsyncIterator",
    "Awaitable",
    "Callable",
    "count",
    "Path",
    "SimpleNamespace",
    "Any",
    "cast",
    "AsyncMock",
    "Mock",
    "pytest",
    "engine_module",
    "AttachmentTooLargeError",
    "AttachmentTypeNotAllowedError",
    "ConversationFacts",
    "MessageFacts",
    "QuotedMessageFacts",
    "ReplyPlanFacts",
    "RouteFacts",
    "ChannelConfig",
    "ChannelConversationEngine",
    "MessageSender",
    "ReplySurface",
    "CommandFeedback",
    "CommandNavigation",
    "CommandOutcome",
    "CommandRun",
    "CommandUnavailability",
    "PreparedCommand",
    "ContentBlock",
    "MediaBlock",
    "TextBlock",
    "InteractionButton",
    "InteractionEvent",
    "ASSISTANT_OUTPUT_EVENT",
    "ChatRunManager",
    "Run",
    "RunKind",
    "WaitingWorkAdmission",
    "ChatSessionManager",
    "SESSION_ID",
    "CHANNEL_REPLY_SURFACE",
    "CHANNEL_GROUP_REPLY_SURFACE",
    "MemoryChannelAccessRegistry",
    "FakeTransport",
    "make_config",
    "make_conversation",
    "make_command_dispatcher",
    "command_outcome",
    "make_new_only_dispatcher",
    "make_completed_run",
    "make_empty_completed_run",
    "make_failed_run",
    "make_cancelled_run",
    "make_interrupted_run",
    "make_engine",
    "drain",
    "assert_member_trigger",
]
