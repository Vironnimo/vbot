"""Shared fixtures and fakes for cron behavior tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from core.automation.cron import (
    CronService,
)
from core.calendar import CalendarService


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
