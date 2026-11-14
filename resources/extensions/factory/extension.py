"""Factory orchestration, admission and owner-bound change publication."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Coroutine
from typing import Any

from core.extensions import ExtensionAPI
from core.extensions.operations import ExtensionHost
from core.tools import ToolContext, tool_success
from core.utils.logging import get_logger

from . import _workflows, agent_text
from ._values import text
from .store import (
    DATABASE_NAME,
    FINAL_STATES,
    MIGRATIONS,
    SCHEMA_SQL,
    FactoryError,
    FactoryStore,
)

type Json = dict[str, Any]
_LOGGER = get_logger("extensions.factory")
_STEP_FIELDS = ("status", "reason", "question", "note", "passes", "rounds", "child")
_WORKFLOW_STEP_FIELDS = (
    "id",
    "name",
    "kind",
    "after",
    "approval",
    "returns_to",
    "rounds",
    "workflow",
    "wait",
)

# Columns an earlier Factory filled from Git; the Agents now report such facts
# in their summaries.
_HIDDEN_FIELDS = frozenset({"runtime", "branch"})


def projection(card: Json) -> Json:
    """Decode provenance once; no transcript or private configuration on the Board."""
    value = {key: item for key, item in card.items() if key not in _HIDDEN_FIELDS}
    runtime = card.get("runtime", {})
    workflow = runtime.get("workflow")
    if isinstance(workflow, dict) and workflow.get("steps"):
        value["workflow"] = {
            "id": workflow["id"],
            "name": workflow["name"],
            "final_approval": workflow.get("final_approval", False),
            "steps": [
                {key: item[key] for key in _WORKFLOW_STEP_FIELDS if key in item}
                for item in workflow["steps"]
            ],
        }
        value["steps"] = {
            step_id: {key: state[key] for key in _STEP_FIELDS if state.get(key) not in (None, "")}
            for step_id, state in runtime.get("steps", {}).items()
        }
    for key in ("note", "legacy"):
        if runtime.get(key):
            value[key] = runtime[key]
    if blocker := runtime.get("blocker"):
        value["note"] = blocker.get("note", "")
    raw = card.get("origin_session", "")
    if raw:
        try:
            address = json.loads(raw)
        except ValueError, TypeError:
            address = None
        value["origin_session"] = address if isinstance(address, dict) else None
    else:
        value["origin_session"] = None
    return value


def attention(card: Json) -> str:
    """The first question or note a waiting Card has for the user."""
    runtime = card.get("runtime", {})
    blocker = runtime.get("blocker") or {}
    candidates = [blocker.get("note")]
    for step in (runtime.get("workflow") or {}).get("steps", []):
        state = runtime.get("steps", {}).get(step["id"], {})
        if state.get("status") in {"waiting", "approval"}:
            candidates += [state.get("question"), state.get("note")]
            if state["status"] == "approval":
                candidates.append(f"Step {step['name']} waits for your approval to start.")
    candidates += [runtime.get("note"), card.get("summary")]
    return next(
        (value.strip() for value in candidates if isinstance(value, str) and value.strip()), ""
    )


class FactoryExtension:
    """One serialized scheduler; execution and persistence stay behind this owner."""

    def __init__(self, api: ExtensionAPI) -> None:
        from ._execution import FactoryExecution
        from ._management import FactoryManagement
        from ._triggers import FactoryTriggers

        self.api = api
        self.host: ExtensionHost | None = None
        self.store: FactoryStore | None = None
        self.lock = asyncio.Lock()
        self.execution = FactoryExecution(self)
        self.management = FactoryManagement(self)
        self.triggers = FactoryTriggers(self)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._scheduler_task: asyncio.Task[Any] | None = None
        self._closing = False
        self._states: dict[str, str] = {}

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
        self._states = {card["id"]: card["state"] for card in await self.store.list_cards()}
        await self.execution.reconcile()
        if host.triggers is not None:
            await self.triggers.sync()
            host.triggers.set_handler(self.triggers.fire)
        await self._badge()
        await self.schedule()

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
        if not self._closing and (self._scheduler_task is None or self._scheduler_task.done()):
            self._scheduler_task = self.spawn(self.schedule())

    def limits(self) -> tuple[int, int]:
        config = self.api.get_config()

        def number(name: str, default: int, minimum: int) -> int:
            value = config.get(name, default)
            return (
                max(minimum, min(int(value), 10000))
                if isinstance(value, (int, float)) and value == value and abs(value) < float("inf")
                else default
            )

        return number("wip_limit", 2, 1), number("max_waiting", 0, 0)

    async def schedule(self) -> None:
        """Start queued Cards in Board order while the WIP limit has room."""
        async with self.lock:
            if self._closing or self.store is None:
                return
            limit, max_waiting = self.limits()
            for card in await self.store.list_cards(state="queued"):
                cards = await self.store.list_cards()
                if sum(self.execution.running(item) for item in cards) >= limit:
                    break
                if max_waiting and sum(item["state"] == "waiting" for item in cards) >= max_waiting:
                    break
                try:
                    await self.execution.start(await self.store.get_card(card["id"]))
                except Exception:
                    _LOGGER.exception("Factory admission failed (card=%s)", card["id"])
                    current = await self.store.get_card(card["id"])
                    runtime = {
                        **current["runtime"],
                        "blocker": {
                            "reason": "run_failed",
                            "note": agent_text.error_message("launch_failed"),
                        },
                    }
                    saved = await self.store.change(
                        card["id"],
                        {"state": "waiting", "waiting_reason": "run_failed", "runtime": runtime},
                        actor="factory",
                        event="admission_failed",
                    )
                    await self.changed(saved, current)

    async def config_changed(self, _previous: Json, _current: Json) -> None:
        await self.schedule()

    async def _badge(self) -> None:
        host = self.host
        if host is not None and host.set_page_badge is not None and self.store is not None:
            host.set_page_badge("board", await self.store.attention_count())

    async def changed(
        self, card: Json, previous: Json | None = None, *, actor: str = "factory"
    ) -> None:
        host = self.host
        if host is None or self._closing:
            return
        old_state = previous["state"] if previous is not None else self._states.get(card["id"])
        self._states[card["id"]] = card["state"]
        if host.publish_change is not None:
            host.publish_change("cards", [card["id"]], card["revision"])
        await self._badge()
        old_reason = previous.get("waiting_reason") if previous is not None else None
        if old_state != card["state"] or (
            card["state"] == "waiting" and old_reason not in (None, card["waiting_reason"])
        ):
            _LOGGER.info(
                "Factory Card changed (card=%s actor=%s state=%s revision=%d)",
                card["id"],
                actor,
                card["state"],
                card["revision"],
            )
            if card["state"] in FINAL_STATES and card.get("parent_id"):
                self.spawn(self.execution.child_finished(card["id"]))
            if card["state"] in {"waiting", "proposal"} and host.notify is not None:
                title = (
                    "Waiting for you: " if card["state"] == "waiting" else "Proposal: "
                ) + " ".join(card["title"].split())
                body = attention(card) or (
                    "This Card needs your attention. Open it on the Board."
                    if card["state"] == "waiting"
                    else "Review this proposed work on the Board."
                )
                try:
                    await host.notify(
                        title[:200],
                        str(body).splitlines()[0][:2000],
                        page_id="board",
                        route=f"/card/{card['id']}",
                        key=card["id"],
                    )
                except Exception:
                    _LOGGER.warning(
                        "Factory attention notification failed (card=%s)", card["id"], exc_info=True
                    )
        self.request_schedule()

    # Projects and Workflows

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

    async def workflow_snapshot(self, workflow_id: str) -> Json | None:
        """The saved Workflow a Card starts with, or None when it was deleted."""
        try:
            workflow: Json = await self.records().get_workflow(workflow_id)
        except FactoryError as error:
            if error.code == "workflow_not_found":
                return None
            raise
        return {
            key: value for key, value in workflow.items() if key not in {"revision", "updated_at"}
        }

    async def resolve_card_project(self, card: Json, workflow: Json) -> tuple[Json, str]:
        """The Project and working folder a starting Card works in, or why it has none.

        The Workflow's own Project wins; a Card without a Project works in a
        folder of its own.
        """
        project_id = workflow["project_id"] or card["project_id"]
        repository = card["runtime"].get("repository", "")
        if not project_id and repository:
            project_id, problem = await self.triggers.resolve_project(
                _workflows.REPOSITORY_PROJECT, workflow, repository
            )
            if problem:
                return {}, problem
        if project_id:
            try:
                project = await self.project(project_id)
            except FactoryError as error:
                return {}, str(error)
            return {"id": project_id, "cwd": project["cwd"]}, ""
        if any(step.get("source") == "team" for step in workflow["steps"]):
            return {}, (
                f"Workflow {workflow['name']!r} uses Project Team members but has no Project. "
                "Set the Workflow's Project, then retry the Card."
            )
        host = self.host
        assert host is not None
        base = host.state_dir or host.data_dir
        folder = base / "workspaces" / card["id"]
        folder.mkdir(parents=True, exist_ok=True)
        return {"id": "", "cwd": str(folder)}, ""

    # Card creation

    async def _new_card(
        self,
        workflow: Json,
        *,
        project_id: str,
        title: str,
        description: str,
        origin: str,
        runtime: Json | None = None,
        **values: Any,
    ) -> Json:
        if project_id:
            await self.project(project_id)
        card = await self.records().create_card(
            project_id,
            title,
            description,
            origin=origin,
            runtime=runtime or {},
            workflow_id=workflow["id"],
            **values,
        )
        await self.changed(card, actor="user" if origin in {"user", "operator"} else "factory")
        return {**card, "workflow_name": workflow["name"]}

    async def create_card(
        self, arguments: Json, *, origin: str, context: ToolContext | None = None
    ) -> Json:
        store = self.records()
        workflows = await store.list_workflows()
        enabled = [item for item in workflows if item["enabled"]]
        requested = arguments.get("workflow")
        if isinstance(requested, str) and requested.strip():
            workflow = _workflows.find(enabled, requested)
        elif enabled:
            workflow = enabled[0]
        else:
            raise FactoryError(
                "workflow_not_found",
                "The Factory has no enabled Workflow. Create or enable one on the Workflows page.",
            )
        project_id = arguments.get("project_id") or ""
        if not isinstance(project_id, str):
            raise FactoryError("missing_field", "project must be a Project id.")
        project_id = project_id.strip()
        if workflow["project_id"]:
            if project_id and project_id != workflow["project_id"]:
                raise FactoryError(
                    "project_mismatch",
                    f"Workflow {workflow['name']!r} always works in Project "
                    f"{workflow['project_id']!r}. Omit project or choose another Workflow.",
                )
            project_id = workflow["project_id"]
        address = (
            json.dumps(
                {
                    "project_id": context.project_id,
                    "agent_id": context.agent_id,
                    "session_id": context.session_id,
                }
            )
            if context is not None and context.session_id
            else ""
        )
        return await self._new_card(
            workflow,
            project_id=project_id,
            title=text(arguments, "title", maximum=200),
            description=text(arguments, "description"),
            origin=origin,
            origin_session=address,
            origin_run=context.run_id or "" if context is not None else "",
        )

    async def create_triggered(
        self,
        workflow: Json,
        trigger: Json,
        event: Json,
        *,
        title: str,
        project_id: str,
        repository: str,
    ) -> Json:
        """A Card for one trigger event; an unknown repository waits for the user at start."""
        description = trigger["task"].strip() or (
            f"Do what Workflow {workflow['name']!r} does for the event that created this Card."
        )
        try:
            return await self._new_card(
                workflow,
                project_id=project_id,
                title=title,
                description=description,
                origin="trigger",
                runtime={"repository": repository} if repository and not project_id else {},
                trigger_event=event,
            )
        except FactoryError as error:
            if error.code != "project_not_found":
                raise
            # The trigger names a deleted Project: the Card waits for the user instead.
            return await self._new_card(
                workflow,
                project_id="",
                title=title,
                description=description,
                origin="trigger",
                runtime={"repository": repository} if repository else {},
                trigger_event=event | {"missing_project": project_id},
            )

    async def create_child(self, card: Json, step: Json, earlier: list[Json]) -> Json:
        """Start the Card a workflow step runs, under the parent's Project unless fixed."""
        depth = int(card["runtime"].get("depth", 0)) + 1
        if depth > _workflows.MAX_DEPTH:
            raise FactoryError(
                "too_deep",
                f"Workflows start each other more than {_workflows.MAX_DEPTH} levels deep. "
                "Check the Workflows for a loop.",
            )
        try:
            workflow = await self.records().get_workflow(step["workflow"])
        except FactoryError as error:
            raise FactoryError(
                "workflow_not_found",
                f"Step {step['name']!r} starts Workflow {step['workflow']!r}, which no longer "
                "exists. Change the step, then retry.",
            ) from error
        parts = [step.get("task", "").strip(), card["description"].strip()]
        if earlier:
            parts.append(
                "Results of earlier steps:\n"
                + "\n\n".join(
                    f"{item['step_name']}: {item['result'] or item['summary']}" for item in earlier
                )
            )
        return await self._new_card(
            workflow,
            project_id=workflow["project_id"] or card["project_id"],
            title=f"{card['title']} · {step['name']}"[:200],
            description="\n\n".join(part for part in parts if part)[:20000],
            origin="workflow",
            runtime={"depth": depth},
            parent_id=card["id"],
        )

    async def discard_child(self, child_id: str) -> None:
        try:
            child = await self.records().get_card(child_id)
        except FactoryError as error:
            if error.code == "card_not_found":
                return
            raise
        if child["state"] not in FINAL_STATES:
            await self.execution.discard(child)

    # Tools

    async def public_tool(self, context: ToolContext, arguments: Json) -> Json:
        try:
            action = arguments["action"]
            if action == "create":
                host = self.host
                authority = (
                    await host.input_authority(context)
                    if host is not None
                    and host.input_authority is not None
                    and not context.subagent_session
                    and context.execution_owner is None
                    else "none"
                )
                origin = {"user": "user", "authorized_task": "automation"}.get(authority, "agent")
                async with self.lock:
                    card = await self.create_card(
                        {**arguments, "project_id": arguments.get("project") or ""},
                        origin=origin,
                        context=context,
                    )
                return agent_text.tool_result(
                    "proposal" if card["state"] == "proposal" else "created", card
                )
            if action == "list":
                cards = await self.records().list_cards(
                    arguments.get("project"), arguments.get("state")
                )
                return tool_success(
                    {
                        "cards": [
                            {
                                key: card[key]
                                for key in (
                                    "id",
                                    "project_id",
                                    "title",
                                    "state",
                                    "waiting_reason",
                                    "origin",
                                )
                            }
                            | (
                                {"running_steps": running}
                                if (running := self._running_steps(card))
                                else {}
                            )
                            for card in cards
                        ]
                    }
                )
            if action == "get":
                card = await self.records().get_card(text(arguments, "id"))
                return tool_success({"card": projection(card)})
            raise FactoryError("invalid_action", "Unknown action. Use create, list or get.")
        except FactoryError as error:
            return agent_text.refusal(error.code, str(error))
        except Exception:
            _LOGGER.exception("Factory public Tool failed (action=%s)", arguments.get("action"))
            return agent_text.unknown_outcome()

    @staticmethod
    def _running_steps(card: Json) -> list[str]:
        if card["state"] in FINAL_STATES:
            return []
        runtime = card["runtime"]
        return [
            step["name"]
            for step in (runtime.get("workflow") or {}).get("steps", [])
            if runtime.get("steps", {}).get(step["id"], {}).get("status") in {"running", "child"}
        ]

    async def private_tool(self, name: str, context: ToolContext, arguments: Json) -> Json:
        try:
            handlers = {
                "factory_submit_work": self.execution.submit_work,
                "factory_report_blocked": self.execution.report_blocked,
                "factory_request_changes": self.execution.request_changes,
            }
            async with self.lock:
                self.records()
                return await handlers[name](context, arguments)
        except FactoryError as error:
            return agent_text.refusal(error.code, str(error))
        except Exception:
            _LOGGER.exception(
                "Factory participant Tool failed (tool=%s run=%s)", name, context.run_id
            )
            return agent_text.unknown_outcome(participant=True)

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
        self._states.clear()
