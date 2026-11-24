"""Factory orchestration, admission and owner-bound change publication."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine
from typing import Any, cast

from core.extensions import ExtensionAPI
from core.extensions.operations import ExtensionHost
from core.tools import ToolContext, tool_failure
from core.utils.logging import get_logger

from ._job_agent_text import JOB_PRIVATE_TOOLS
from .store import (
    DATABASE_NAME,
    MIGRATIONS,
    SCHEMA_SQL,
    FactoryError,
    FactoryStore,
)

type Json = dict[str, Any]
_LOGGER = get_logger("extensions.factory")


class FactoryExtension:
    """One serialized scheduler; execution and persistence stay behind this owner."""

    def __init__(self, api: ExtensionAPI) -> None:
        from ._job_execution import FactoryJobExecution
        from ._job_management import FactoryJobManagement
        from ._job_tools import FactoryJobTools
        from ._job_triggers import FactoryJobTriggers
        from ._management import FactoryManagement
        from ._native_execution import NativeFactoryExecution
        from ._native_management import NativeFactoryManagement
        from ._public_tool import FactoryPublicTool

        self.api = api
        self.host: ExtensionHost | None = None
        self.store: FactoryStore | None = None
        self.lock = asyncio.Lock()
        self.native = NativeFactoryExecution(self)
        self.native_management = NativeFactoryManagement(self)
        self.management = FactoryManagement(self)
        self.job_triggers = FactoryJobTriggers(self)
        self.job_execution = FactoryJobExecution(self)
        self.job_tools = FactoryJobTools(self)
        self.job_management = FactoryJobManagement(self)
        self.public_tool_handler = FactoryPublicTool(self)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._scheduler_task: asyncio.Task[Any] | None = None
        self._schedule_requested = False
        self._closing = False

    def records(self) -> FactoryStore:
        if self.store is None or self.host is None or self._closing:
            raise FactoryError(
                "factory_unavailable",
                "The Factory is unavailable. Wait for its Extension to load, then retry.",
            )
        return self.store

    async def start(self, host: ExtensionHost) -> None:
        if host.open_database is None or host.temporary_agents is None:
            raise RuntimeError("Factory requires the owner-bound Extension host")
        self._closing = False
        self.host = host
        self.store = FactoryStore(await host.open_database(DATABASE_NAME, SCHEMA_SQL, MIGRATIONS))
        await self.native_management.identities.acknowledge_memberships()
        await self.native.reconcile()
        await self.job_execution.reconcile()
        await self._archive_orphaned_groups()
        if host.triggers is not None:
            await self.job_triggers.sync()
            host.triggers.set_handler(self.job_triggers.fire)
        await self._badge()
        await self.schedule()

    async def _archive_orphaned_groups(self) -> None:
        host = self.host
        if host is None or host.temporary_agents is None:
            return
        groups = host.temporary_agents
        if not hasattr(groups, "groups"):
            return
        active_native = await self.native.group_ids()
        after = ""
        while True:
            page = await groups.groups(after=after, limit=100)
            if not page:
                break
            for group_id in page:
                if self.job_execution.owns(group_id) or group_id in active_native:
                    continue
                with contextlib.suppress(Exception):
                    await groups.close_group(group_id)
                with contextlib.suppress(Exception):
                    await groups.archive_group(group_id)
            if len(page) < 100:
                break
            after = page[-1]

    def spawn(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name="factory-work")
        self._tasks.add(task)

        def settled(completed: asyncio.Task[Any]) -> None:
            self._tasks.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                _LOGGER.error("Factory background work failed", exc_info=completed.exception())

        task.add_done_callback(settled)
        return task

    def request_schedule(self) -> None:
        if self._closing:
            return
        self._schedule_requested = True
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = self.spawn(self._run_schedule())

    async def _run_schedule(self) -> None:
        while self._schedule_requested and not self._closing:
            self._schedule_requested = False
            await self.schedule()

    def wip_limit(self) -> int:
        config = self.api.get_config()
        value = config.get("wip_limit", 2)
        return (
            max(1, min(int(value), 10000))
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value == value
            and abs(value) < float("inf")
            else 2
        )

    async def schedule(self) -> None:
        """Schedule native Leader chat and queued Job Runs within the WIP limit."""
        async with self.lock:
            if self._closing or self.store is None:
                return
            limit = self.wip_limit()
            await self.native.schedule(limit)
            await self.job_execution.schedule(limit)

    def _runtime_owner(self, context: Any) -> Any:
        group_id = context.binding.group_id
        if self.job_execution.owns(group_id):
            return self.job_execution
        return self.native.sessions

    async def before_request(self, context: Any) -> Any:
        return await self._runtime_owner(context).before_request(context)

    async def before_model_request(self, context: Any) -> None:
        await self._runtime_owner(context).before_model_request(context)

    async def before_tool_call(
        self, context: Any, *, tool_name: str, tool_call_id: str
    ) -> str | None:
        return cast(
            str | None,
            await self._runtime_owner(context).before_tool_call(
                context, tool_name=tool_name, tool_call_id=tool_call_id
            ),
        )

    async def acknowledge_delivery(self, context: Any, delivery: Any) -> None:
        await self._runtime_owner(context).acknowledge_delivery(context, delivery)

    async def run_finished(self, context: Any, *, outcome: str) -> None:
        await self._runtime_owner(context).run_finished(context, outcome=outcome)

    async def quiesce(self, reason: str = "extension") -> None:
        self.native.quiescing = True
        await self.job_execution.quiesce(reason)

    async def config_changed(self, _previous: Json, _current: Json) -> None:
        await self.schedule()

    async def _badge(self) -> None:
        host = self.host
        if host is not None and host.set_page_badge is not None and self.store is not None:
            host.set_page_badge("board", await self.job_execution.attention_count())

    async def _projects(self) -> list[Json]:
        host = self.host
        assert host is not None and host.catalog is not None
        projects: list[Json] = (await host.catalog())["projects"]
        return projects

    async def project(self, project_id: str) -> Json:
        found = next((item for item in await self._projects() if item["id"] == project_id), None)
        if found is None:
            raise FactoryError(
                "project_not_found",
                f"Project {project_id!r} does not exist. Choose an existing Project.",
            )
        return found

    async def public_tool(self, context: ToolContext, arguments: Json) -> Json:
        return await self.public_tool_handler.handle(context, arguments)

    async def private_tool(self, name: str, context: ToolContext, arguments: Json) -> Json:
        if name in JOB_PRIVATE_TOOLS:
            return await self.job_tools.handle(name, context, arguments)
        if name in {"factory_settings", "factory_members"}:
            return await self.native_management.private_tool(name, context, arguments)
        return tool_failure("unknown_tool", f"Unknown Factory tool: {name}.")

    async def close(self) -> None:
        self._closing = True
        host = self.host
        if host is not None and host.triggers is not None:
            # A retired registration's triggers are released by the host.
            with contextlib.suppress(Exception):
                host.triggers.set_handler(None)
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.store = None
        self.host = None
