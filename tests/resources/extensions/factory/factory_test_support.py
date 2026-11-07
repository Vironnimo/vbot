"""Registered Factory Tools and operations with deterministic participant admission."""

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
from resources.extensions.factory._values import SETUP_DEFAULTS
from resources.extensions.factory._workflows import templates
from resources.extensions.factory.extension import FactoryExtension
from resources.extensions.factory.store import FactoryStore

type Json = dict[str, Any]


def implement_workflow() -> Json:
    """Implement, then Review: the Workflow the operation contracts run Cards with."""
    workflow = templates()[0]
    implement, review, integrate = workflow["steps"]
    implement.update(source="agent", agent="worker")
    review.update(source="agent", agent="reviewer")
    integrate.update(source="agent", agent="worker")
    return workflow


def drafts_workflow() -> Json:
    """Three independent drafts, a merge and a review: a document-only Workflow."""
    workflow = templates()[2]
    for step in workflow["steps"]:
        step.update(source="agent", agent="reviewer" if step["returns_to"] else "worker")
    return workflow


class FakeHost:
    """Only canonical input authority and presentation effects cross this test seam."""

    def __init__(self, directory: Path) -> None:
        self.authority: Literal["user", "authorized_task", "none"] = "none"
        self.inspect_prompt: Callable[[Any, str | None], Awaitable[Json]] | None = None
        self.authority_calls: list[ToolContext] = []
        self.published: list[tuple[str, list[str], int]] = []
        self.badges: list[tuple[str, int]] = []
        self.notifications: list[Json] = []
        self.catalog_data: Json = {
            "projects": [
                {
                    "id": project_id,
                    "cwd": str(directory / project_id),
                    "agent_profiles": [{"id": "worker"}, {"id": "reviewer"}],
                }
                for project_id in ("project", "other", "disabled")
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


class FakeExecution:
    """Admit one phase and persist its state, without Sessions, Models or Git."""

    def __init__(self, store: FactoryStore) -> None:
        self.store = store
        self.calls: list[tuple[str, str, str]] = []
        self.start_gate: tuple[asyncio.Event, asyncio.Event] | None = None

    async def _admit(self, action: str, card: Json, state: str, comment: str = "") -> None:
        self.calls.append((action, card["id"], comment))
        if action == "start" and self.start_gate is not None:
            entered, released = self.start_gate
            entered.set()
            await released.wait()
        runtime = dict(card["runtime"])
        runtime.pop("pending_action", None)
        await self.store.change(
            card["id"],
            {"state": state, "waiting_reason": "", "runtime": runtime},
            actor="factory",
            event=f"test_{action}",
        )

    async def start(self, card: Json) -> None:
        await self._admit("start", card, "working")

    async def accept(self, card: Json) -> None:
        await self._admit("accept", card, "working")

    async def send_back(self, card: Json, comment: str, step: str = "") -> None:
        await self._admit("send_back", card, "working", comment)

    async def retry(self, card: Json) -> None:
        await self._admit("retry", card, "working")


@dataclass
class FactoryHarness:
    api: ExtensionAPI
    declarations: ExtensionDeclarations
    tools: ToolRegistry
    service: FactoryExtension
    store: FactoryStore
    host: FakeHost
    execution: FakeExecution
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
            tool_name="factory_card",
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
            return tool_failure_for_exception("factory_card", error)

    async def operation(self, name: str, **arguments: Any) -> Json:
        return await self.api.operations.invoke(name, arguments)

    async def settle(self) -> None:
        """Drain this service's unfinished Tasks, never the worker's whole Event Loop."""
        while tasks := [task for task in self.service._tasks if not task.done()]:
            await asyncio.gather(*tasks)

    async def setup(self, project_id: str = "project", **changes: Any) -> Json:
        return await self.store.save_setup(
            {
                **SETUP_DEFAULTS,
                "project_id": project_id,
                "workflows": [implement_workflow()],
                "default_workflow": "implement",
                **changes,
            }
        )

    async def card(
        self,
        state: str = "queued",
        *,
        project_id: str = "project",
        waiting_reason: str = "",
    ) -> Json:
        card = await self.store.create_card(
            project_id,
            "Objective",
            "Acceptance criteria",
            origin="agent" if state == "proposal" else "operator",
            runtime={"cwd": str(self.directory / project_id)},
            workflow_id="implement",
        )
        if card["state"] != state or waiting_reason:
            card = await self.store.change(
                card["id"],
                {"state": state, "waiting_reason": waiting_reason},
                actor="factory",
                event="test_seed",
            )
        return card


async def build_factory(directory: Path, store: FactoryStore) -> FactoryHarness:
    declarations = ExtensionDeclarations()
    config: Json = {"wip_limit": 2, "max_waiting": 0}
    api = ExtensionAPI(
        "factory", declarations, config={}, logger=None, config_provider=lambda: dict(config)
    )
    register(api)
    handler = next(tool.handler for tool in declarations.tools if tool.name == "factory_card")
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
    execution = FakeExecution(store)
    service.host = cast(ExtensionHost, host)
    service.store = store
    cast(Any, service).execution = execution
    harness = FactoryHarness(
        api, declarations, tools, service, store, host, execution, config, directory
    )
    await harness.setup()
    return harness
