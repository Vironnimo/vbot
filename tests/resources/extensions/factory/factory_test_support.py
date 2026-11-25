"""Registered Factory Tools and operations with deterministic test host."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import MethodType
from typing import Any, Literal, cast

from core.extensions import ExtensionAPI, ExtensionRecord, ExtensionRegistry
from core.extensions.extensions import ExtensionDeclarations
from core.extensions.notifications import notification_text
from core.extensions.operations import ExtensionHost
from core.permissions import PermissionBoundary
from core.runs import RunKind
from core.tools import ToolContext, ToolRegistry, tool_failure_for_exception
from core.tools.contracts import ToolContractError
from resources.extensions.factory import register
from resources.extensions.factory._public_tool import PUBLIC_TOOL_NAME
from resources.extensions.factory.extension import FactoryExtension
from resources.extensions.factory.store import FactoryStore

type Json = dict[str, Any]


class FakeTriggers:
    """The host's Extension triggers as the Factory sees them, without Cron."""

    def __init__(self) -> None:
        self.items: dict[str, Json] = {}
        self.handler: Callable[[Json], Awaitable[None]] | None = None
        self.secrets: dict[str, str] = {}

    def set_handler(self, handler: Callable[[Json], Awaitable[None]] | None) -> None:
        self.handler = handler

    def check_schedule(self, schedule: str) -> None:
        if schedule.startswith("bad"):
            raise ValueError("Use a cron expression such as 0 9 * * *")

    def list(self) -> list[Json]:
        return [copy.deepcopy(item) for item in self.items.values()]

    async def create(self, key: str, *, name: str, schedule: str, enabled: bool) -> Json:
        trigger_id = f"trg{len(self.secrets) + 1}"
        webhook = schedule == "webhook"
        self.items[key] = {
            "id": trigger_id,
            "key": key,
            "name": name,
            "schedule": schedule,
            "schedule_type": "webhook" if webhook else "cron",
            "enabled": enabled,
            "status": "active" if enabled else "paused",
            "next_fire_at": None,
            "last_fired_at": None,
            "last_outcome": None,
            "last_error": None,
            "webhook": {"url": f"http://vbot/hooks/{trigger_id}", "path": trigger_id}
            if webhook
            else None,
        }
        self.secrets[trigger_id] = f"secret-{trigger_id}"
        return copy.deepcopy(self.items[key])

    def _by_id(self, trigger_id: str) -> Json:
        return next(item for item in self.items.values() if item["id"] == trigger_id)

    async def update(self, trigger_id: str, **values: Any) -> Json:
        self._by_id(trigger_id).update(values)
        return copy.deepcopy(self._by_id(trigger_id))

    async def delete(self, trigger_id: str) -> None:
        self.items.pop(self._by_id(trigger_id)["key"])

    async def webhook_secret(self, trigger_id: str) -> str:
        return self.secrets[trigger_id]

    async def rotate_webhook_secret(self, trigger_id: str) -> str:
        self.secrets[trigger_id] += "-rotated"
        return self.secrets[trigger_id]


class FakeHost:
    """Only canonical input authority and presentation effects cross this test seam."""

    def __init__(self, directory: Path) -> None:
        self.authority: Literal["user", "authorized_task", "none"] = "none"
        self.inspect_prompt: Callable[[Any, str | None], Awaitable[Json]] | None = None
        self.authority_calls: list[ToolContext] = []
        self.published: list[tuple[str, list[str], int]] = []
        self.badges: list[tuple[str, int]] = []
        self.notifications: list[Json] = []
        self.triggers: FakeTriggers | None = FakeTriggers()
        self.state_dir = directory / "state"
        self.data_dir = directory / "data"
        self.catalog_data: Json = {
            "tools": [],
            "tool_activations": [],
            "projects": [
                {
                    "id": project_id,
                    "cwd": str(directory / project_id),
                    "agent_profiles": [{"id": "worker"}, {"id": "reviewer"}],
                }
                for project_id in ("project", "other")
            ],
            "agents": [{"id": "worker"}, {"id": "reviewer"}],
            "models": [{"id": "test/worker"}, {"id": "test/reviewer"}],
        }

    async def catalog(self) -> Json:
        return copy.deepcopy(self.catalog_data)

    async def input_authority(
        self, context: ToolContext
    ) -> Literal["user", "authorized_task", "none"]:
        self.authority_calls.append(context)
        return self.authority

    def publish_change(self, resource: str, ids: list[str], revision: int) -> None:
        self.published.append((resource, list(ids), revision))

    def set_page_badge(self, page_id: str, count: int) -> None:
        self.badges.append((page_id, count))

    async def notify(self, title: str, body: str, **destination: Any) -> None:
        notification_text(title, name="title", maximum=200)
        notification_text(body, name="body", maximum=2000, multiline=True)
        self.notifications.append({"title": title, "body": body, **destination})


@dataclass
class FactoryHarness:
    api: ExtensionAPI
    declarations: ExtensionDeclarations
    tools: ToolRegistry
    service: FactoryExtension
    store: FactoryStore
    host: FakeHost
    config: Json
    directory: Path

    def context(self, **changes: Any) -> ToolContext:
        context = ToolContext(
            agent_id="creator",
            session_id="source-session",
            project_id="source-project",
            working_project_id="project",
            run_id="source-run",
            tool_call_id="factory-call",
            tool_name=PUBLIC_TOOL_NAME,
            tool_call_index=0,
            workspace=self.directory,
            vbot_root=self.directory,
            data_root=self.directory,
            run_kind=RunKind.USER,
            permissions=PermissionBoundary.UNRESTRICTED,
        )
        return replace(context, **changes)

    async def call(self, arguments: Json, *, context: ToolContext | None = None) -> Json:
        try:
            return await self.tools.dispatch(context or self.context(), arguments)
        except ToolContractError as error:
            return tool_failure_for_exception(PUBLIC_TOOL_NAME, error)

    async def operation(self, name: str, /, **arguments: Any) -> Json:
        return await self.api.operations.invoke(name, arguments)

    async def settle(self) -> None:
        """Drain this service's unfinished Tasks, never the worker's whole Event Loop."""
        while tasks := [task for task in self.service._tasks if not task.done()]:
            await asyncio.gather(*tasks)


async def build_factory(directory: Path, store: FactoryStore) -> FactoryHarness:
    declarations = ExtensionDeclarations()
    config: Json = {"wip_limit": 2}
    api = ExtensionAPI(
        "factory", declarations, config={}, logger=None, config_provider=lambda: dict(config)
    )
    register(api)
    handler = next(tool.handler for tool in declarations.tools if tool.name == PUBLIC_TOOL_NAME)
    assert isinstance(handler, MethodType)
    service = handler.__self__
    assert isinstance(service, FactoryExtension)
    registry = ExtensionRegistry()
    registry._records.append(
        ExtensionRecord(
            "factory", directory, directory / "extension.py", "loaded", declarations=declarations
        )
    )
    tools = ToolRegistry()
    registry.apply_tools(tools)
    host = FakeHost(directory)
    service.host = cast(ExtensionHost, host)
    service.store = store
    return FactoryHarness(api, declarations, tools, service, store, host, config, directory)
