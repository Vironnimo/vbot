"""A real CalendarService behind the registered calendar Tool, called as a Run calls it."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.calendar import CalendarEvent, CalendarService
from core.providers.adapter import tool_result_text
from core.tools.calendar import CALENDAR_TOOL_NAME, register_calendar_tool
from core.tools.tools import ToolContext, ToolRegistry, tool_failure

SERVER_ZONE = "Europe/Berlin"


@dataclass
class CalendarTool:
    registry: ToolRegistry
    service: CalendarService
    workspace: Path

    def call(self, arguments: Any, *, project_id: str | None = None) -> tuple[dict[str, Any], str]:
        """Dispatch like the Tool executor; return the envelope and the text the Model reads."""
        context = ToolContext(
            agent_id="agent-one",
            session_id="session-one",
            run_id="run-one",
            tool_call_id="call-one",
            tool_name=CALENDAR_TOOL_NAME,
            tool_call_index=0,
            workspace=self.workspace,
            vbot_root=self.workspace,
            data_root=self.workspace,
            project_id=project_id,
        )
        try:
            envelope = asyncio.run(self.registry.dispatch(context, arguments, [CALENDAR_TOOL_NAME]))
        except ValueError as error:
            envelope = tool_failure("invalid_arguments", str(error))
        text = str(tool_result_text(json.dumps(envelope, ensure_ascii=False)))
        return envelope, text

    def events(self) -> list[CalendarEvent]:
        return self.service.list_events()

    def only_event(self) -> CalendarEvent:
        [event] = self.events()
        return event

    def actions(self) -> list[dict[str, Any]]:
        return self.service.actions.list_actions()


def calendar_tool(tmp_path: Path, *, tz: str = SERVER_ZONE) -> CalendarTool:
    service = CalendarService(tmp_path, tz=tz)
    registry = ToolRegistry()
    register_calendar_tool(registry, service)
    return CalendarTool(registry, service, tmp_path)
