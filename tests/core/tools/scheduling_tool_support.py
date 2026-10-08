"""The scheduling Tools against real services, called through production dispatch as a Run calls.

``scheduling_tools`` registers the calendar and cron Tools in one registry over a
CalendarService and a CronService bound to each other, as the Runtime does;
``calendar_tool`` and ``cron_tool`` return one of them. ``clock_at`` fixes the time
a module reads.
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
BOTH_TOOLS = (CALENDAR_TOOL_NAME, CRON_TOOL_NAME)


@dataclass
class _DispatchedTool:
    registry: ToolRegistry
    workspace: Path
    reference_lock: asyncio.Lock
    # The Tools the Run offers the Agent; results name only these.
    offered: tuple[str, ...]

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
        envelope = await dispatch_as_executor(self.registry, context, arguments, self.offered)
        text = str(tool_result_text(json.dumps(envelope, ensure_ascii=False)))
        return envelope, text

    def succeeded(self, arguments: Any) -> str:
        """Dispatch a call that must succeed; return the text the Model reads."""
        envelope, text = self.call(arguments)
        assert envelope["ok"] is True, text
        return text


@dataclass
class CronTool(_DispatchedTool):
    service: CronService
    trigger: SimpleNamespace
    calendar: CalendarService

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
        text = self.succeeded(arguments)
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
    cron: CronService

    tool_name: ClassVar[str] = CALENDAR_TOOL_NAME

    def events(self) -> list[CalendarEvent]:
        return self.service.list_events()

    def only_event(self) -> CalendarEvent:
        [event] = self.events()
        return event

    def refused(self, arguments: Any) -> str:
        """Dispatch a call that must fail without changing the calendar; return its message."""
        before = [event.to_dict() for event in self.events()]
        envelope, text = self.call(arguments)
        assert envelope["ok"] is False, text
        assert envelope["error"]["code"] == "invalid_arguments", text
        assert [event.to_dict() for event in self.events()] == before
        message = str(envelope["error"]["message"])
        assert message.startswith("calendar was not run: "), message
        return message


def scheduling_tools(
    tmp_path: Path,
    *,
    tz: str = SERVER_ZONE,
    agent_resolver: Any = None,
    offered: tuple[str, ...] = BOTH_TOOLS,
) -> tuple[CalendarTool, CronTool]:
    """The calendar and cron Tools over bound services, registered as the Runtime does."""
    calendar = CalendarService(tmp_path, tz=tz)
    cron, trigger = make_service(tmp_path, agent_resolver=agent_resolver, tz=tz, calendar=calendar)
    registry = ToolRegistry()
    reference_lock = asyncio.Lock()
    register_cron_tool(registry, cron, reference_lock=reference_lock)
    register_calendar_tool(registry, calendar, reference_lock=reference_lock, cron_service=cron)
    shared: dict[str, Any] = {
        "registry": registry,
        "workspace": tmp_path,
        "reference_lock": reference_lock,
        "offered": offered,
    }
    return (
        CalendarTool(**shared, service=calendar, cron=cron),
        CronTool(**shared, service=cron, trigger=trigger, calendar=calendar),
    )


def cron_tool(tmp_path: Path, *, tz: str = SERVER_ZONE, agent_resolver: Any = None) -> CronTool:
    return scheduling_tools(tmp_path, tz=tz, agent_resolver=agent_resolver)[1]


def calendar_tool(
    tmp_path: Path, *, tz: str = SERVER_ZONE, cron_offered: bool = True
) -> CalendarTool:
    offered = BOTH_TOOLS if cron_offered else (CALENDAR_TOOL_NAME,)
    return scheduling_tools(tmp_path, tz=tz, offered=offered)[0]


def clock_at(moment: datetime) -> type[datetime]:
    """A ``datetime`` whose ``now`` is ``moment``, to patch into a module that reads the clock."""

    class Clock(datetime):
        @classmethod
        @override
        def now(cls, tz: tzinfo | None = None) -> Clock:
            return cast(Clock, moment.astimezone(tz))

    return Clock
