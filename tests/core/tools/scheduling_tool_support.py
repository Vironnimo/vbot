"""The scheduling Tools against real services, called through production dispatch as a Run calls.

``cron_tool`` puts a real CronService behind the registered cron Tool and ``calendar_tool`` a
real CalendarService behind the calendar Tool; ``clock_at`` fixes the time a module reads.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast, override

from core.automation.cron import CronJob, CronService
from core.calendar import CalendarEvent, CalendarService
from core.providers.adapter import tool_result_text
from core.tools.calendar import CALENDAR_TOOL_NAME, register_calendar_tool
from core.tools.cron import CRON_TOOL_NAME, register_cron_tool
from core.tools.tools import ToolContext, ToolRegistry
from tests.core.automation.cron_test_support import make_service
from tests.core.tools.tools_test_support import dispatch_as_executor

SERVER_ZONE = "Europe/Berlin"
JOB_PROMPT = "Lint the wiki and report broken links."
DENTIST_START = "2030-01-10T15:00"
WEEKLY_MONDAY = {"freq": "weekly", "by_weekday": ["mo"]}


@dataclass
class _DispatchedTool:
    registry: ToolRegistry
    workspace: Path
    reference_lock: asyncio.Lock

    tool_name: ClassVar[str]

    def call(self, arguments: Any, *, project_id: str | None = None) -> tuple[dict[str, Any], str]:
        """Dispatch like the Tool executor; return the envelope and the text the Model reads."""
        return asyncio.run(self.call_async(arguments, project_id=project_id))

    async def call_async(
        self, arguments: Any, *, project_id: str | None = None
    ) -> tuple[dict[str, Any], str]:
        """:meth:`call` on the running Event Loop."""
        context = ToolContext(
            agent_id="agent-one",
            session_id="session-one",
            run_id="run-one",
            tool_call_id="call-one",
            tool_name=self.tool_name,
            tool_call_index=0,
            workspace=self.workspace,
            vbot_root=self.workspace,
            data_root=self.workspace,
            project_id=project_id,
        )
        envelope = await dispatch_as_executor(self.registry, context, arguments)
        text = str(tool_result_text(json.dumps(envelope, ensure_ascii=False)))
        return envelope, text


@dataclass
class CronTool(_DispatchedTool):
    service: CronService
    trigger: SimpleNamespace

    tool_name: ClassVar[str] = CRON_TOOL_NAME

    def jobs(self) -> list[CronJob]:
        return self.service.list_jobs()

    def only_job(self) -> CronJob:
        [job] = self.jobs()
        return job

    def add_job(self, **fields: Any) -> str:
        """Create a job running ``JOB_PROMPT`` every 2h through the Tool; return its id."""
        envelope, text = self.call(
            {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", **fields}
        )
        assert envelope["ok"] is True, text
        return str(envelope["data"]["id"])

    def created(self, arguments: Any) -> tuple[CronJob, str]:
        """Dispatch a call that must succeed; return the only job and the text."""
        envelope, text = self.call(arguments)
        assert envelope["ok"] is True, text
        return self.only_job(), text

    def refused(self, arguments: Any) -> str:
        """Dispatch a call that must fail without touching any job; return its message."""
        before = [job.to_dict() for job in self.jobs()]
        envelope, text = self.call(arguments)
        assert envelope["ok"] is False, text
        assert envelope["error"]["code"] == "invalid_arguments"
        assert [job.to_dict() for job in self.jobs()] == before
        message = str(envelope["error"]["message"])
        assert message.startswith("cron was not run: ")
        return message


@dataclass
class CalendarTool(_DispatchedTool):
    service: CalendarService

    tool_name: ClassVar[str] = CALENDAR_TOOL_NAME

    def events(self) -> list[CalendarEvent]:
        return self.service.list_events()

    def only_event(self) -> CalendarEvent:
        [event] = self.events()
        return event

    def actions(self) -> list[dict[str, Any]]:
        return self.service.actions.list_actions()

    def add_dentist(self) -> str:
        """Create the one-hour event "Dentist" at ``DENTIST_START``; return its id."""
        return self.service.create_event(title="Dentist", start=DENTIST_START).id

    def add_weekly(self) -> str:
        """Create the event "Weekly", Mondays at 09:00 from 2030-01-07; return its id."""
        return self.service.create_event(
            title="Weekly", start="2030-01-07T09:00", rrule=WEEKLY_MONDAY
        ).id


def cron_tool(tmp_path: Path, *, tz: str = SERVER_ZONE, agent_resolver: Any = None) -> CronTool:
    service, trigger = make_service(tmp_path, agent_resolver=agent_resolver, tz=tz)
    registry = ToolRegistry()
    reference_lock = asyncio.Lock()
    register_cron_tool(registry, service, reference_lock=reference_lock)
    return CronTool(
        registry=registry,
        workspace=tmp_path,
        reference_lock=reference_lock,
        service=service,
        trigger=trigger,
    )


def calendar_tool(tmp_path: Path, *, tz: str = SERVER_ZONE) -> CalendarTool:
    service = CalendarService(tmp_path, tz=tz)
    registry = ToolRegistry()
    reference_lock = asyncio.Lock()
    register_calendar_tool(registry, service, reference_lock=reference_lock)
    return CalendarTool(
        registry=registry, workspace=tmp_path, reference_lock=reference_lock, service=service
    )


def clock_at(moment: datetime) -> type[datetime]:
    """A ``datetime`` whose ``now`` is ``moment``, to patch into a module that reads the clock."""

    class Clock(datetime):
        @classmethod
        @override
        def now(cls, tz: tzinfo | None = None) -> Clock:
            return cast(Clock, moment.astimezone(tz))

    return Clock
