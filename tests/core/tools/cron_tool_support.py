"""A real CronService behind the registered cron Tool, called as a Run calls it."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.automation.cron import CronJob, CronService
from core.providers.adapter import tool_result_text
from core.tools.cron import CRON_TOOL_NAME, register_cron_tool
from core.tools.tools import ToolContext, ToolRegistry, tool_failure
from tests.core.automation.cron_test_support import make_service

SERVER_ZONE = "Europe/Berlin"


@dataclass
class CronTool:
    registry: ToolRegistry
    service: CronService
    trigger: SimpleNamespace
    workspace: Path

    def call(self, arguments: Any, *, project_id: str | None = None) -> tuple[dict[str, Any], str]:
        """Dispatch like the Tool executor; return the envelope and the text the Model reads."""
        context = ToolContext(
            agent_id="agent-one",
            session_id="session-one",
            run_id="run-one",
            tool_call_id="call-one",
            tool_name=CRON_TOOL_NAME,
            tool_call_index=0,
            workspace=self.workspace,
            vbot_root=self.workspace,
            data_root=self.workspace,
            project_id=project_id,
        )
        try:
            envelope = asyncio.run(self.registry.dispatch(context, arguments, [CRON_TOOL_NAME]))
        except ValueError as error:
            envelope = tool_failure("invalid_arguments", str(error))
        text = str(tool_result_text(json.dumps(envelope, ensure_ascii=False)))
        return envelope, text

    def jobs(self) -> list[CronJob]:
        return self.service.list_jobs()

    def only_job(self) -> CronJob:
        [job] = self.jobs()
        return job


def cron_tool(tmp_path: Path, *, tz: str = SERVER_ZONE, agent_resolver: Any = None) -> CronTool:
    service, trigger = make_service(tmp_path, agent_resolver=agent_resolver, tz=tz)
    registry = ToolRegistry()
    register_cron_tool(registry, service)
    return CronTool(registry, service, trigger, tmp_path)
