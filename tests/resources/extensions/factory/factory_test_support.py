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
from resources.extensions.factory._execution import FactoryExecution
from resources.extensions.factory._workflows import normalize, templates
from resources.extensions.factory.extension import FactoryExtension
from resources.extensions.factory.store import FactoryStore

type Json = dict[str, Any]


def template(template_id: str, **changes: Any) -> Json:
    """A starter Workflow with Agents chosen: reviewers judge, workers do the rest."""
    workflow = next(item for item in templates() if item["id"] == template_id)
    for step in workflow["steps"]:
        step["agent"] = "reviewer" if step["returns_to"] else "worker"
    return {**workflow, **changes}


def implement_workflow(**changes: Any) -> Json:
    """Implement, Review, then Integrate after approval, in Project ``project``."""
    return template("implement", **{"project_id": "project", **changes})


def drafts_workflow(**changes: Any) -> Json:
    """Three parallel drafts, a merge and a review: a Workflow without a Project."""
    return template("drafts", **changes)


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


class FakeExecution:
    """Record user actions and admit Cards without Sessions or Models.

    A started Card runs its first step, so it occupies a WIP slot like a real one.
    """

    running = staticmethod(FactoryExecution.running)
    group_ids = staticmethod(FactoryExecution.group_ids)

    def __init__(self, store: FactoryStore) -> None:
        self.store = store
        self.calls: list[tuple[str, str, str]] = []
        self.start_gate: tuple[asyncio.Event, asyncio.Event] | None = None

    async def budget_status(self, card: Json, step_id: str, **kwargs: Any) -> Json:
        return {}

    async def _record(self, action: str, card: Json, detail: str = "", **patch: Any) -> Json:
        self.calls.append((action, card["id"], detail))
        return cast(
            Json,
            await self.store.change(card["id"], patch, actor="user", event=f"test_{action}"),
        )

    async def start(self, card: Json) -> Json:
        self.calls.append(("start", card["id"], ""))
        if self.start_gate is not None:
            entered, released = self.start_gate
            entered.set()
            await released.wait()
        runtime = {
            **card["runtime"],
            "workflow": {"id": card["workflow_id"], "name": "Workflow", "steps": [{"id": "do"}]},
            "steps": {"do": {"status": "running"}},
        }
        return cast(
            Json,
            await self.store.change(
                card["id"],
                {"state": "working", "runtime": runtime},
                actor="factory",
                event="test_start",
            ),
        )

    async def accept(self, card: Json, step: str = "") -> Json:
        return await self._record("accept", card, step, state="done")

    async def send_back(self, card: Json, comment: str, step: str = "") -> Json:
        return await self._record("send_back", card, f"{step}:{comment}", state="working")

    async def answer(self, card: Json, text: str, step: str = "") -> Json:
        return await self._record("answer", card, f"{step}:{text}", state="working")

    async def retry(self, card: Json, step: str = "") -> Json:
        return await self._record("retry", card, step, state="working")

    async def discard(self, card: Json) -> Json:
        return await self._record("discard", card, state="discarded")

    async def delete(self, card: Json) -> Json:
        self.calls.append(("delete", card["id"], ""))
        return {"id": card["id"], "pending": True}


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

    async def operation(self, name: str, /, **arguments: Any) -> Json:
        return await self.api.operations.invoke(name, arguments)

    async def settle(self) -> None:
        """Drain this service's unfinished Tasks, never the worker's whole Event Loop."""
        while tasks := [task for task in self.service._tasks if not task.done()]:
            await asyncio.gather(*tasks)

    async def workflow(self, definition: Json | None = None) -> Json:
        """Save a Workflow directly, without the operation's catalog checks."""
        normalized = normalize(definition or implement_workflow())
        return cast(Json, await self.store.save_workflow(normalized, expected_revision=None))

    async def card(
        self,
        state: str = "queued",
        *,
        project_id: str = "project",
        waiting_reason: str = "",
        runtime: Json | None = None,
    ) -> Json:
        card = await self.store.create_card(
            project_id,
            "Objective",
            "Acceptance criteria",
            origin="agent" if state == "proposal" else "operator",
            workflow_id="implement",
        )
        patch: Json = {}
        if state == "working" and runtime is None:
            runtime = {"steps": {"do": {"status": "running"}}}
        if runtime is not None:
            patch["runtime"] = {**card["runtime"], **runtime}
        if card["state"] != state or waiting_reason:
            patch.update(state=state, waiting_reason=waiting_reason)
        if patch:
            card = await self.store.change(card["id"], patch, actor="factory", event="test_seed")
        return cast(Json, card)


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
    await harness.workflow()
    return harness
