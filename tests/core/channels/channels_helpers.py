"""Shared fixtures and fakes for channels behavior tests."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from core.attachments import AttachmentStore
from core.channels import (
    ChannelAdapter,
    ChannelConfig,
    ChannelService,
)
from core.channels.adapter import (
    FileData,
    ReplyPlanFacts,
    RouteFacts,
)
from core.extensions import InteractionButton
from core.runs import Run
from core.sessions import ChatSessionManager


class AgentStoreStub:
    def __init__(self, *, known_agent_ids: set[str] | None = None) -> None:
        self._known_agent_ids = set(known_agent_ids or {"assistant"})

    def get(self, agent_id: str) -> SimpleNamespace:
        if agent_id not in self._known_agent_ids:
            raise KeyError(agent_id)
        return SimpleNamespace(id=agent_id)


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
    platform = "telegram"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.sent_messages: list[tuple[str | None, str]] = []
        self.sent_buttons: list[list[list[InteractionButton]] | None] = []
        self.relayed_runs: list[tuple[Run, ReplyPlanFacts]] = []

    async def start(self) -> None:
        self.started.set()
        await asyncio.Future()

    async def stop(self) -> None:
        self.stopped.set()

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

    async def relay_run(self, run: Run, reply_plan: ReplyPlanFacts) -> None:
        self.relayed_runs.append((run, reply_plan))

    def ensure_outbound_session(self, platform_target: str) -> RouteFacts:
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

    async def start(self) -> None:
        self._events.append(f"start:{self.label}")
        self.started.set()
        await asyncio.Future()

    async def stop(self) -> None:
        self._events.append(f"stop:{self.label}:begin")
        await self._stop_gate.wait()
        self._events.append(f"stop:{self.label}:end")
        self.stopped.set()

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

    def ensure_outbound_session(self, platform_target: str) -> RouteFacts:
        raise NotImplementedError


async def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise TimeoutError("Timed out waiting for condition")
        await asyncio.sleep(0)
