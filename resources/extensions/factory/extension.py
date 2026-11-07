"""Factory orchestration, admission and owner-bound change publication."""

from __future__ import annotations

import asyncio
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
    ACTIVE_STATES,
    DATABASE_NAME,
    FINAL_STATES,
    MIGRATIONS,
    SCHEMA_SQL,
    FactoryError,
    FactoryStore,
)

type Json = dict[str, Any]
_LOGGER = get_logger("extensions.factory")


def _current_step(runtime: Json) -> Json | None:
    """The current step of a started Card's Workflow snapshot."""
    workflow = runtime.get("workflow")
    if not isinstance(workflow, dict) or not workflow.get("steps"):
        return None
    steps = workflow["steps"]
    current = runtime.get("step")
    found: Json = next((item for item in steps if item["id"] == current), steps[0])
    return found


# Columns an earlier Factory filled from Git; the Agents now report such facts
# in their summaries.
_HIDDEN_FIELDS = frozenset(
    {
        "runtime",
        "round",
        "branch",
        "base",
        "base_sha",
        "head_sha",
        "checks",
        "recommendation",
        "integration_reference",
    }
)


def projection(card: Json) -> Json:
    """Decode provenance once; no transcript or private configuration on the Board."""
    value = {key: item for key, item in card.items() if key not in _HIDDEN_FIELDS}
    runtime = card.get("runtime", {})
    if current := _current_step(runtime):
        workflow = runtime["workflow"]
        value["workflow"] = {
            "id": workflow["id"],
            "name": workflow["name"],
            "steps": [
                {
                    key: item[key]
                    for key in ("id", "name", "after", "approval", "returns_to", "rounds")
                }
                for item in workflow["steps"]
            ],
        }
        value["step"] = {"id": current["id"], "name": current["name"]}
        value["done_steps"] = [
            item["id"] for item in workflow["steps"] if item["id"] in runtime.get("done", [])
        ]
    if pending := runtime.get("pending_action"):
        value["queued_action"] = pending["action"]
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


class FactoryExtension:
    """One serialized scheduler; execution and persistence stay behind this owner."""

    def __init__(self, api: ExtensionAPI) -> None:
        from ._execution import FactoryExecution
        from ._management import FactoryManagement

        self.api = api
        self.host: ExtensionHost | None = None
        self.store: FactoryStore | None = None
        self.lock = asyncio.Lock()
        self.execution = FactoryExecution(self)
        self.management = FactoryManagement(self)
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
        async with self.lock:
            if self._closing or self.store is None:
                return
            limit, max_waiting = self.limits()
            cards = await self.store.list_cards()
            if max_waiting and sum(card["state"] == "waiting" for card in cards) >= max_waiting:
                return
            available = limit - sum(card["state"] in ACTIVE_STATES for card in cards)
            enabled = {
                setup["project_id"] for setup in await self.store.list_setups() if setup["enabled"]
            }
            for card in cards:
                if available <= 0:
                    break
                if card["state"] != "queued" or card["project_id"] not in enabled:
                    continue
                pending = card["runtime"].get("pending_action", {})
                try:
                    action = pending.get("action")
                    if action == "accept":
                        await self.execution.accept(card)
                    elif action == "send_back":
                        await self.execution.send_back(
                            card,
                            pending.get("comment", ""),
                            pending.get("step") or pending.get("stage", ""),
                        )
                    elif action == "retry":
                        await self.execution.retry(card)
                    else:
                        await self.execution.start(card)
                except Exception:
                    _LOGGER.exception("Factory admission failed (card=%s)", card["id"])
                    current = await self.store.get_card(card["id"])
                    runtime = {
                        **current["runtime"],
                        "note": "The participant could not start. "
                        "Retry the Card after checking its Session and Project setup.",
                    }
                    saved = await self.store.change(
                        card["id"],
                        {"state": "waiting", "waiting_reason": "run_failed", "runtime": runtime},
                        actor="factory",
                        event="admission_failed",
                    )
                    await self.changed(saved, current)
                current_cards = await self.store.list_cards()
                available = limit - sum(item["state"] in ACTIVE_STATES for item in current_cards)
                if (
                    max_waiting
                    and sum(item["state"] == "waiting" for item in current_cards) >= max_waiting
                ):
                    break

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
        if old_state != card["state"]:
            _LOGGER.info(
                "Factory Card changed (card=%s actor=%s state=%s revision=%d)",
                card["id"],
                actor,
                card["state"],
                card["revision"],
            )
            if card["state"] in {"waiting", "proposal"} and host.notify is not None:
                title = (
                    "Waiting for you: " if card["state"] == "waiting" else "Proposal: "
                ) + " ".join(card["title"].split())
                body = next(
                    (
                        value.strip()
                        for value in (
                            card["runtime"].get("question"),
                            card["runtime"].get("note"),
                            card.get("summary"),
                        )
                        if isinstance(value, str) and value.strip()
                    ),
                    "This Card needs your attention. Open it on the Board."
                    if card["state"] == "waiting"
                    else "Review this proposed work on the Board.",
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

    async def create_card(
        self, arguments: Json, *, origin: str, context: ToolContext | None = None
    ) -> Json:
        project_id = text(arguments, "project_id")
        store = self.records()
        setups = await store.list_setups()
        enabled = [setup["project_id"] for setup in setups if setup["enabled"]]
        if project_id not in enabled:
            choices = ", ".join(enabled) or "(none; configure a Project in Setups first)"
            raise FactoryError(
                "project_not_enabled",
                f"Project {project_id!r} has no enabled Factory setup. Use one of: {choices}.",
            )
        host = self.host
        assert host is not None and host.catalog is not None
        catalog = await host.catalog()
        project = next((item for item in catalog["projects"] if item["id"] == project_id), None)
        if project is None:
            raise FactoryError(
                "project_not_found",
                "The Project is unavailable. Choose an existing Project in Setups.",
            )
        setup = next(item for item in setups if item["project_id"] == project_id)
        requested = arguments.get("workflow")
        workflow = _workflows.choose(
            setup, requested if isinstance(requested, str) and requested.strip() else None
        )
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
        card = await store.create_card(
            project_id,
            text(arguments, "title", maximum=200),
            text(arguments, "description"),
            origin=origin,
            origin_session=address,
            origin_run=context.run_id or "" if context is not None else "",
            runtime={"cwd": project["cwd"]},
            workflow_id=workflow["id"],
        )
        await self.changed(card, actor="user" if origin != "agent" else "factory")
        return {**card, "workflow_name": workflow["name"]}

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
                        {**arguments, "project_id": text(arguments, "project")},
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
                                {"step": current["name"]}
                                if card["state"] not in FINAL_STATES
                                and (current := _current_step(card["runtime"]))
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
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.store = None
        self.host = None
        self._states.clear()
