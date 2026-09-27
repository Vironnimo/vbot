"""Shared builders for Telegram adapter tests."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import core.channels._telegram_inbound as telegram_inbound
import core.channels._telegram_transport as telegram_transport
from core.attachments import AttachmentStore
from core.channels.adapter import ChannelAccessRegistry, RunButtonBindingRegistry
from core.channels.telegram import TelegramChannelAdapter
from core.chat import CommandDispatcher, CommandFeedback
from core.chat.commands import CommandOutcome
from core.chat.messages import GroupRole
from core.database import write_bootstrap_marker
from core.extensions import InteractionEvent, InteractionResponder
from core.runs import ChatRunManager
from core.sessions import ChatSessionManager

from .engine_test_support import (
    QUEUE_DRAIN_TIMEOUT_SECONDS,
    MemoryChannelAccessRegistry,
    channel_state,
    make_command_dispatcher,
    make_config,
    make_trigger_service,
)

# PNG signature bytes: the attachment store sniffs them as image/png.
PNG_BYTES = b"\x89PNG\r\n\x1a\nIMG"


class ManualClock:
    """Controlled time for the Telegram settle, album and typing windows.

    The inbound buffer and the transport sleep through their module's ``asyncio``
    name and have no sleep seam, so the clock swaps that name for a proxy whose
    ``sleep`` waits until :meth:`advance` passes its deadline. Everything else in
    those modules, and every other module, keeps the real asyncio and real time.
    """

    def __init__(self) -> None:
        self._now = 0.0
        self._timers: list[tuple[float, asyncio.Future[None]]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proxy = _AsyncioWithSleep(self._sleep)
        monkeypatch.setattr(telegram_inbound, "asyncio", proxy)
        monkeypatch.setattr(telegram_transport, "asyncio", proxy)

    async def _sleep(self, delay: float) -> None:
        deadline = asyncio.get_running_loop().create_future()
        self._timers.append((self._now + delay, deadline))
        await deadline

    async def advance(self, seconds: float) -> None:
        """Move time forward and let every sleep that became due resume.

        Newly created tasks first reach their sleep, so a window opened just
        before the call counts from the current time.
        """
        await _run_ready_callbacks()
        self._now += seconds
        due = [deadline for when, deadline in self._timers if when <= self._now]
        self._timers = [(when, deadline) for when, deadline in self._timers if when > self._now]
        for deadline in due:
            if not deadline.done():
                deadline.set_result(None)
        await _run_ready_callbacks()


async def _run_ready_callbacks() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class _AsyncioWithSleep:
    def __init__(self, sleep: Callable[[float], Awaitable[None]]) -> None:
        self.sleep = sleep

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)


class GatedAccessRegistry(MemoryChannelAccessRegistry):
    """Access registry whose first group-sender lookup waits until released.

    Group messages look their sender up before any engine work, so the first
    lookup holds that dispatch in flight. With *error*, every lookup raises it.
    """

    def __init__(self, *, error: Exception | None = None) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._error = error

    async def snapshot_participant_role(
        self, channel_id: str, access_scope_id: str, user_id: str, display_name: str
    ) -> GroupRole:
        if self._error is not None:
            raise self._error
        if not self.entered.is_set():
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return await super().snapshot_participant_role(
            channel_id, access_scope_id, user_id, display_name
        )


def make_workflow_dispatcher() -> CommandDispatcher:
    """Return the real Chat command dispatcher with two Extension commands.

    ``/workflow <argument>`` is serialized behind the conversation's work and
    ``/ping`` answers at once, like the built-in ``/stop``.
    """
    dispatcher = CommandDispatcher(ChatRunManager())
    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Run the workflow.",
        handler=lambda _context, argument: CommandOutcome(
            command="workflow",
            feedback=CommandFeedback(kind="notice", text=f"Workflow {argument} complete."),
        ),
    )
    dispatcher.register_extension_command(
        "workflow_ext",
        name="ping",
        description="Answer at once.",
        handler=lambda _context, _argument: CommandOutcome(
            command="ping",
            feedback=CommandFeedback(kind="notice", text="pong"),
        ),
        argument="none",
        execution_mode="immediate",
    )
    return dispatcher


def make_update(
    *,
    chat_id: int = 12345,
    user_id: int = 50,
    text: str | None = "hello",
    message_id: int | None = None,
    update_id: int | None = None,
    chat_title: str | None = None,
    user_full_name: str | None = None,
    user_username: str | None = None,
    message_thread_id: int | None = None,
    is_topic_message: bool = False,
    reply_to_user_id: int | None = None,
    reply_to_message: object | None = None,
) -> SimpleNamespace:
    """Build a text update shaped like PTB's ``effective_*`` view of one message."""
    if reply_to_message is None and reply_to_user_id is not None:
        reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=reply_to_user_id))
    return SimpleNamespace(
        update_id=update_id,
        effective_chat=SimpleNamespace(id=chat_id, title=chat_title),
        effective_user=SimpleNamespace(
            id=user_id, full_name=user_full_name, username=user_username
        ),
        effective_message=SimpleNamespace(
            text=text,
            message_id=message_id,
            message_thread_id=message_thread_id,
            is_topic_message=is_topic_message,
            reply_to_message=reply_to_message,
        ),
    )


def make_media_update(
    *,
    chat_id: int = 12345,
    user_id: int = 50,
    caption: str | None = None,
    media_group_id: str | None = None,
    message_id: int | None = None,
    forward_origin: object | None = None,
    user_full_name: str | None = None,
    **media: object,
) -> SimpleNamespace:
    """Build a media update; *media* names the Telegram payload, e.g. ``photo=``."""
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id, full_name=user_full_name),
        effective_message=SimpleNamespace(
            text=None,
            caption=caption,
            media_group_id=media_group_id,
            message_id=message_id,
            message_thread_id=None,
            forward_origin=forward_origin,
            **media,
        ),
    )


def photo(file_unique_id: str) -> list[SimpleNamespace]:
    """Return a Telegram photo payload (its size variants) for one picture."""
    return [SimpleNamespace(file_id=f"photo-{file_unique_id}", file_unique_id=file_unique_id)]


def telegram_file(payload: bytes, *, file_size: int | None = None) -> SimpleNamespace:
    """Return what ``bot.get_file`` resolves: size metadata and a lazy body download."""
    return SimpleNamespace(
        file_size=file_size,
        download_as_bytearray=AsyncMock(return_value=bytearray(payload)),
    )


def make_migration_update(
    *,
    chat_id: int,
    migrate_to: int | None = None,
    migrate_from: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=50),
        effective_message=SimpleNamespace(
            text=None,
            migrate_to_chat_id=migrate_to,
            migrate_from_chat_id=migrate_from,
            message_thread_id=None,
        ),
    )


def make_callback_update(
    *,
    chat_id: int = 12345,
    user_id: int = 50,
    data: str,
    callback_id: str = "cb1",
    inline_keyboard: list[list[Any]] | None = None,
    message_id: int = 777,
    text: str | None = "Shopping list",
    answer: AsyncMock | None = None,
) -> SimpleNamespace:
    """Build a callback_query update mirroring PTB's effective_* resolution.

    For a callback_query, PTB fills ``effective_message``/``effective_chat``/
    ``effective_user`` from the tapped message and the tapper.
    """
    reply_markup = (
        SimpleNamespace(inline_keyboard=inline_keyboard) if inline_keyboard is not None else None
    )
    message = SimpleNamespace(
        message_id=message_id,
        message_thread_id=None,
        is_topic_message=False,
        text=text,
        caption=None,
        reply_to_message=None,
        reply_markup=reply_markup,
        chat=SimpleNamespace(id=chat_id),
    )
    callback = SimpleNamespace(
        id=callback_id,
        data=data,
        from_user=SimpleNamespace(id=user_id, full_name="Tapper", username="tap"),
        message=message,
        answer=answer or AsyncMock(),
    )
    return SimpleNamespace(
        callback_query=callback,
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id, full_name="Tapper", username="tap"),
    )


def make_bot() -> SimpleNamespace:
    """Return a recording stand-in for the PTB Bot the adapter calls."""
    return SimpleNamespace(
        send_message=AsyncMock(),
        send_photo=AsyncMock(),
        send_document=AsyncMock(),
        send_media_group=AsyncMock(),
        send_chat_action=AsyncMock(),
        get_file=AsyncMock(),
        answer_callback_query=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


def make_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dm_scope: str = "per_conversation",
    allowed_chat_ids: list[int] | None = None,
    response_mode: str = "mention",
    admin_user_ids: list[str] | None = None,
    bot_username: str | None = None,
    bot_display_name: str | None = None,
    bot_id: int | None = None,
    trigger_run: AsyncMock | None = None,
    credential_resolver: Callable[[str], str] | None = None,
    attachment_store: AttachmentStore | None = None,
    command_dispatcher: object | None = None,
    chat_migration_persister: Callable[[str, str], None] | None = None,
    interaction_dispatcher: (
        Callable[[InteractionEvent, InteractionResponder], Awaitable[bool]] | None
    ) = None,
    run_button_binding_registry: RunButtonBindingRegistry | None = None,
    access_registry: ChannelAccessRegistry | None = None,
    update_offset_store: Any | None = None,
    set_process_token: bool = True,
    clock: ManualClock | None = None,
    running: bool = True,
) -> tuple[TelegramChannelAdapter, ChatSessionManager, AsyncMock, SimpleNamespace]:
    """Build an adapter on real Sessions, Channel state and engine.

    ``running`` attaches the returned bot as if ``start()`` had run; the bot
    identity arguments stand in for its ``get_me`` answer. Without a *clock*
    the forwarding-comment settle window is zero, so held text flushes on the
    next loop pass; with one, the production windows wait for the clock.
    """
    if clock is None:
        monkeypatch.setattr(telegram_inbound, "_FORWARD_COMMENT_SETTLE_SECONDS", 0)
    else:
        clock.install(monkeypatch)
    if set_process_token:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", "test-token")
    else:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)

    if not (tmp_path / "data-store.json").exists():
        write_bootstrap_marker(tmp_path)
    chat_sessions = ChatSessionManager(tmp_path)
    trigger_mock = trigger_run or AsyncMock()

    config = make_config(dm_scope=dm_scope, response_mode=response_mode)
    config.allowed_chat_ids = [str(chat_id) for chat_id in allowed_chat_ids or ()]
    config.validate()

    adapter = TelegramChannelAdapter(
        config,
        cast(Any, make_trigger_service(trigger_mock)),
        cast(Any, chat_sessions),
        credential_resolver or (lambda key: os.environ.get(key, "")),
        attachment_store=attachment_store,
        command_dispatcher=cast(Any, command_dispatcher or make_command_dispatcher()),
        conversation_pointers=channel_state(tmp_path),
        chat_migration_persister=chat_migration_persister,
        interaction_dispatcher=interaction_dispatcher,
        run_button_binding_registry=run_button_binding_registry,
        access_registry=access_registry or MemoryChannelAccessRegistry(admin_user_ids),
        update_offset_store=update_offset_store,
    )
    if bot_username is not None or bot_display_name is not None or bot_id is not None:
        adapter._set_bot_identity(
            SimpleNamespace(id=bot_id, username=bot_username, full_name=bot_display_name)
        )

    bot = make_bot()
    if running:
        adapter._application = SimpleNamespace(
            bot=bot,
            updater=None,
            stop=AsyncMock(),
            shutdown=AsyncMock(),
        )
    return adapter, chat_sessions, trigger_mock, bot


async def drain_chat_queue(adapter: TelegramChannelAdapter, chat_id: int) -> None:
    """Wait for held text and album dispatches, then for the chat's engine queue."""
    while pending := [task for task in adapter._inbound._tasks if not task.done()]:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=QUEUE_DRAIN_TIMEOUT_SECONDS,
        )
    queue = adapter._engine._chat_queues.get(str(chat_id))
    if queue is None:
        await asyncio.sleep(0)
        return
    await asyncio.wait_for(queue.join(), timeout=QUEUE_DRAIN_TIMEOUT_SECONDS)


def sent_texts(bot: SimpleNamespace) -> list[str]:
    return [call.kwargs["text"] for call in bot.send_message.await_args_list]
