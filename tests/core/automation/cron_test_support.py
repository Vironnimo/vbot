"""Shared fixtures and fakes for cron behavior tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from core.automation.cron import (
    CronService,
)
from core.calendar import CalendarService
from core.channels import ChannelNotFoundError


def make_service(
    tmp_path: Path,
    *,
    agent_resolver: Any = None,
    sessions: Any = None,
    tz: str | ZoneInfo | None = None,
    calendar: CalendarService | None = None,
) -> tuple[CronService, SimpleNamespace]:
    """A CronService with a mock trigger; with ``calendar``, bound to it both ways."""
    trigger_service = SimpleNamespace(trigger_run=AsyncMock())
    service = CronService(
        cast(Any, trigger_service),
        tmp_path,
        agent_resolver=agent_resolver,
        sessions=sessions,
        tz=tz,
        calendar=calendar,
    )
    if calendar is not None:
        calendar.bind_event_jobs(service)
    return service, trigger_service


@dataclass
class FakeChats:
    """Channels as Cron sees them: owners by Channel id, and what reached chats."""

    owners: dict[str, str] = field(default_factory=lambda: {"tg-main": "agent-one"})
    relayed: list[tuple[Any, str, str, str | None]] = field(default_factory=list)
    sent: list[tuple[str, str, str, str | None]] = field(default_factory=list)
    notes: list[tuple[str, str, str, str | None, str | None]] = field(default_factory=list)

    async def get_channel(self, channel_id: str) -> SimpleNamespace:
        if channel_id not in self.owners:
            raise ChannelNotFoundError(f"Channel not found: {channel_id}")
        return SimpleNamespace(agent_id=self.owners[channel_id])

    async def relay_run_to_chat(
        self, run: Any, channel_id: str, platform_target: str, *, thread_id: str | None = None
    ) -> None:
        self.relayed.append((run, channel_id, platform_target, thread_id))

    async def send(
        self, channel_id: str, message: str, platform_target: str, *, thread_id: str | None = None
    ) -> None:
        self.sent.append((channel_id, message, platform_target, thread_id))

    async def record_outbound_note(
        self,
        channel_id: str,
        platform_target: str,
        note: str,
        *,
        thread_id: str | None = None,
        skip_session_id: str | None = None,
    ) -> bool:
        self.notes.append((channel_id, platform_target, note, thread_id, skip_session_id))
        return True
