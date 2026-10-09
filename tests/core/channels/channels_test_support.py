"""Shared fixtures and fakes for channels behavior tests."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override

import pytest

from core.attachments import AttachmentStore
from core.channels import (
    ChannelAdapter,
    ChannelConfig,
    ChannelService,
    ChannelStorage,
)
from core.channels.adapter import (
    FileData,
    RouteFacts,
)
from core.extensions import InteractionButton
from core.sessions import ChatSessionManager


class AgentStoreStub:
    """The user's Agents ``known_agent_ids`` plus the built-in Librarian."""

    def __init__(self, *, known_agent_ids: set[str] | None = None) -> None:
        self._known_agent_ids = set(known_agent_ids or {"assistant"})

    def get(self, agent_id: str) -> SimpleNamespace:
        if agent_id == "librarian":
            return SimpleNamespace(id=agent_id, name="Librarian", builtin="librarian")
        if agent_id not in self._known_agent_ids:
            raise KeyError(agent_id)
        return SimpleNamespace(id=agent_id, name=agent_id, builtin=None)


def make_service(
    tmp_path: Path,
    *,
    known_agent_ids: set[str] | None = None,
    attachment_store: AttachmentStore | None = None,
    chat_sessions: ChatSessionManager | None = None,
) -> ChannelService:
    return ChannelService(
        cast(Any, SimpleNamespace()),
        cast(Any, chat_sessions or SimpleNamespace()),
        agent_store=cast(Any, AgentStoreStub(known_agent_ids=known_agent_ids)),
        data_root=tmp_path,
        credential_resolver=lambda key: os.environ.get(key, ""),
        attachment_store=attachment_store,
        command_dispatcher=cast(Any, SimpleNamespace(prepare=lambda _content: None)),
    )


def make_config(
    channel_id: str = "tg-assistant",
    *,
    enabled: bool = True,
    allowed_chat_ids: list[int | str] | None = None,
    platform: str = "telegram",
) -> ChannelConfig:
    token_env_var = (
        "DISCORD_BOT_TOKEN_DC_ASSISTANT"
        if platform == "discord"
        else "TELEGRAM_BOT_TOKEN_TG_ASSISTANT"
    )
    return ChannelConfig(
        id=channel_id,
        platform=platform,
        agent_id="assistant",
        dm_scope="per_conversation",
        allowed_chat_ids=cast(Any, list(allowed_chat_ids or [])),
        token_env_var=token_env_var,
        enabled=enabled,
    )


class BlockingAdapter(ChannelAdapter):
    """Runs until stopped; also the transport its Channel's engine replies over."""

    platform = "telegram"
    platform_display_name = "Telegram"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.sent_messages: list[tuple[str | None, str]] = []
        self.sent_buttons: list[list[list[InteractionButton]] | None] = []
        # (platform_target, thread_id, text) of each engine reply.
        self.replies: list[tuple[str, str | None, str]] = []

    @override
    async def start(self) -> None:
        self._report_connected()
        self.started.set()
        await asyncio.Future()

    @override
    async def stop(self) -> None:
        self.stopped.set()

    @override
    async def send(
        self,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
        thread_id: str | None = None,
        buttons: list[list[InteractionButton]] | None = None,
    ) -> None:
        self.sent_messages.append((message, platform_target))
        self.sent_buttons.append(buttons)

    async def send_text(
        self,
        platform_target: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
        buttons: list[list[InteractionButton]] | None = None,
    ) -> None:
        del reply_to_message_id
        self.replies.append((platform_target, thread_id, text))
        self.sent_buttons.append(buttons)

    def activity_indicator(
        self, platform_target: str, thread_id: str | None = None
    ) -> contextlib.AbstractAsyncContextManager[None]:
        del platform_target, thread_id
        return contextlib.nullcontext()

    @override
    async def ensure_outbound_session(
        self, platform_target: str, *, thread_id: str | None = None
    ) -> RouteFacts:
        return RouteFacts(agent_id="assistant", session_id=f"ch-blocking-{platform_target}")


class DelayedStopAdapter(ChannelAdapter):
    platform = "telegram"

    def __init__(
        self,
        *,
        label: str,
        stop_gate: asyncio.Event,
        events: list[str],
    ) -> None:
        self.label = label
        self._stop_gate = stop_gate
        self._events = events
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    @override
    async def start(self) -> None:
        self._events.append(f"start:{self.label}")
        self.started.set()
        await asyncio.Future()

    @override
    async def stop(self) -> None:
        self._events.append(f"stop:{self.label}:begin")
        await self._stop_gate.wait()
        self._events.append(f"stop:{self.label}:end")
        self.stopped.set()

    @override
    async def send(
        self,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
        thread_id: str | None = None,
        buttons: list[list[InteractionButton]] | None = None,
    ) -> None:
        return

    @override
    async def ensure_outbound_session(
        self, platform_target: str, *, thread_id: str | None = None
    ) -> RouteFacts:
        raise NotImplementedError


async def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise TimeoutError("Timed out waiting for condition")
        await asyncio.sleep(0)


async def start_with_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: BlockingAdapter,
    *,
    chat_sessions: ChatSessionManager | None = None,
) -> ChannelService:
    """Save one enabled Channel and start it with ``adapter`` as its platform adapter."""
    config = make_config(enabled=True)
    ChannelStorage(tmp_path).save(config)
    service = make_service(tmp_path, chat_sessions=chat_sessions)
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)
    service.start_channel(config.id)
    await asyncio.wait_for(adapter.started.wait(), timeout=5)
    return service
