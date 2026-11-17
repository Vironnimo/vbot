"""Operator actions and read models shared by the Factory page and CLI."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents.temporary import TemporaryAgentConfig
from core.settings.agent_defaults import normalize_agent_default_value
from core.tools.availability import ToolAccess
from core.utils.errors import StorageError
from core.utils.logging import get_logger
from core.utils.workers import settle_before_cancelling

from . import _limits, _workflows
from ._values import require_state, text
from .store import FINAL_STATES, FactoryError

if TYPE_CHECKING:
    from .extension import FactoryExtension

type Json = dict[str, Any]
_LOGGER = get_logger("extensions.factory")
_READS = frozenset({"catalog", "board.get", "card.get", "workflow.list", "workflow.get"})


class FactoryManagement:
    def __init__(self, service: FactoryExtension) -> None:
        self.service = service

    async def operation(self, name: str, arguments: Json) -> Json:
        if name.partition(".")[0] in {"factory", "member", "task", "topic"}:
            return await self.service.native_management.operation(name, arguments)
        if name in _READS:
            return await self._read(name, arguments)
        if name == "workflow.preview":
            return await self._preview(arguments)
        if name == "workflow.secret":
            secret = await self.service.triggers.secret(
                text(arguments, "id"),
                text(arguments, "trigger"),
                rotate=arguments.get("rotate") is True,
            )
            return {"secret": secret}
        async with self.service.lock:
            if name.startswith("workflow."):
                return await settle_before_cancelling(self._workflow(name, arguments))
            if name == "card.create":
                card = await settle_before_cancelling(
                    self.service.create_card(arguments, origin="operator")
                )
                return {"card": self._projection(card)}
            card = await self.service.records().get_card(text(arguments, "id"))
            # A saved transition must also schedule its launch/drain and publish
            # before cancellation releases the scheduler's lock.
            return await settle_before_cancelling(self._action(name, card, arguments))

    @staticmethod
    def _projection(card: Json) -> Json:
        from .extension import projection

        return projection(card)

    # Reads

    async def _read(self, name: str, arguments: Json) -> Json:
        service = self.service
        store = service.records()
        if name == "catalog":
            assert service.host is not None and service.host.catalog is not None
            return {
                "catalog": await service.host.catalog(),
                "templates": _workflows.templates(),
                "triggers_available": service.host.triggers is not None,
            }
        if name == "board.get":
            all_cards = await store.list_cards()
            project_id = arguments.get("project_id")
            limit, _max_waiting = service.limits()
            return {
                "cards": [
                    self._projection(card)
                    for card in all_cards
                    if project_id is None or card["project_id"] == project_id
                ],
                "wip": {
                    "running": sum(service.execution.running(card) for card in all_cards),
                    "limit": limit,
                },
                "revision": max((card["revision"] for card in all_cards), default=0),
            }
        if name == "workflow.list":
            return {
                "workflows": [
                    await self._workflow_view(workflow) for workflow in await store.list_workflows()
                ]
            }
        if name == "workflow.get":
            workflow = await store.get_workflow(text(arguments, "id"))
            return {
                "workflow": await self._workflow_view(workflow),
                "effective_models": (await self._resolve_models(workflow))[0],
            }
        card = await store.get_card(text(arguments, "id"))
        return await self._detail(card)

    async def _workflow_view(self, workflow: Json) -> Json:
        status = self.service.triggers.status(workflow["id"])
        return {
            **workflow,
            "triggers": [
                {**trigger, "status": self._trigger_status(status.get(trigger["id"]))}
                for trigger in workflow["triggers"]
            ],
        }

    @staticmethod
    def _trigger_status(item: Json | None) -> Json | None:
        if item is None:
            return None
        return {
            key: item[key]
            for key in ("status", "next_fire_at", "last_fired_at", "last_outcome", "last_error")
        } | {"webhook": item["webhook"]}

    async def _detail(self, card: Json) -> Json:
        service = self.service
        store = service.records()
        host = service.host
        assert host is not None and host.temporary_agents is not None
        groups = host.temporary_agents
        participants: list[Json] = []
        runs: list[Json] = []
        approvals: list[Any] = []
        usage: list[Json] = []
        steps = card["runtime"].get("steps", {})
        for group_id in service.execution.group_ids(card):
            _card_id, step_id = _workflows.split_group(group_id)
            bindings = []
            after = ""
            while True:
                page = await groups.list(group_id, after=after, limit=100)
                bindings.extend(page)
                if len(page) < 100:
                    break
                after = page[-1].participant_id
            if not bindings:
                continue
            run_ids = (
                steps.get(step_id, {}).get("runs", {})
                if step_id
                else card["runtime"].get("runs", {})
            )
            owned = await groups.owned_runs(group_id, list(run_ids.values())) if run_ids else {}
            for binding in bindings:
                run_id = run_ids.get(binding.participant_id)
                inspected = owned.get(run_id)
                run = inspected.run if inspected is not None else None
                status = (
                    run.status.value
                    if run is not None
                    else inspected.record.terminal_status
                    if inspected is not None
                    else None
                )
                participants.append(
                    {
                        "participant_id": binding.participant_id,
                        "step": step_id,
                        "address": asdict(binding.address),
                        "display_name": binding.config.get("name", binding.participant_id),
                        "model": binding.config.get("model", ""),
                        "lifecycle_run_id": run_id,
                        "run_active": status in {"pending", "running", "cancelling"},
                        "state": status,
                    }
                )
                if run_id:
                    runs.append(
                        {
                            "run_id": run_id,
                            "participant_id": binding.participant_id,
                            "step": step_id,
                            "status": status,
                        }
                    )
            if host.pending_approvals is not None and owned:
                approvals.extend((await host.pending_approvals(group_id, list(owned)))["requests"])
            usage.append(await groups.usage(group_id, {}))
        budgets = {
            step_id: await service.execution.budgets.budget_status(card, step_id, report_all=True)
            for step_id in steps
        }
        return {
            "card": {**self._projection(card), "runtime": card["runtime"]},
            **await store.history(card["id"]),
            "trigger": await store.trigger_event(card["id"]),
            "children": [
                self._projection(item)
                for item in await store.list_cards()
                if item.get("parent_id") == card["id"]
            ],
            "participants": participants,
            "runs": runs,
            "approvals": approvals,
            "usage": _merge_usage(usage),
            "budget": budgets,
        }

    # Workflows

    async def _validated(self, raw: Any, *, workflow_id: str = "") -> tuple[Json, Json]:
        """A normalized Workflow checked against the catalog, and its steps' Models."""
        service = self.service
        workflow = _workflows.normalize(raw, workflow_id=workflow_id)
        assert service.host is not None and service.host.catalog is not None
        catalog = await service.host.catalog()
        projects = {item["id"]: item for item in catalog["projects"]}
        where = f"Workflow {workflow['name']!r}"
        if workflow["project_id"] and workflow["project_id"] not in projects:
            raise FactoryError(
                "project_not_found",
                f"{where}: Project {workflow['project_id']!r} does not exist. Choose a Project "
                "or leave it empty.",
            )
        for trigger in workflow["triggers"]:
            choice = trigger["project"]
            if choice and choice != _workflows.REPOSITORY_PROJECT and choice not in projects:
                raise FactoryError(
                    "project_not_found",
                    f"{where}, trigger {trigger['title'] or trigger['id']!r}: Project {choice!r} "
                    "does not exist.",
                )
        agents = {item["id"] for item in catalog.get("agents", [])}
        team = {
            item["id"]
            for item in projects.get(workflow["project_id"], {}).get("agent_profiles", [])
        }
        models = {item["id"] for item in catalog["models"]}
        saved = {item["id"] for item in await service.records().list_workflows()}
        for step in workflow["steps"]:
            label = f"{where}, step {step['name']!r}"
            if step["kind"] == "workflow":
                if step["workflow"] not in saved:
                    raise FactoryError(
                        "workflow_not_found",
                        f"{label}: choose a saved Workflow for it to start.",
                    )
                continue
            if step["source"] == "team" and not workflow["project_id"]:
                raise FactoryError(
                    "profile_not_found",
                    f"{label}: a Team member needs the Workflow's Project. Set the Project or "
                    "choose an Agent.",
                )
            if step["agent"] not in (agents if step["source"] == "agent" else team):
                raise FactoryError(
                    "profile_not_found",
                    f"{label}: choose an available Agent or a member of the Project's Team.",
                )
            if step["model"] and step["model"].partition("::")[0] not in models:
                raise FactoryError(
                    "model_not_found",
                    f"{label}: the Model is unavailable. Choose a Model from the catalog "
                    "or clear it to use the Agent's Model.",
                )
            if step["effort"]:
                try:
                    step["effort"] = normalize_agent_default_value(
                        "thinking_effort", step["effort"]
                    )
                except (StorageError, ValueError) as error:
                    raise FactoryError(
                        "invalid_effort", f"{label}: {str(error).rstrip('.')}."
                    ) from error
        service.triggers.check(workflow)
        effective, problems = await self._resolve_models(workflow)
        if problems:
            label, problem = next(iter(problems.items()))
            raise FactoryError(
                "model_unresolved",
                f"{label} cannot run: {problem}. Choose a Model for it, "
                "or give its Agent a usable Model.",
            )
        return workflow, effective

    async def _preview(self, arguments: Json) -> Json:
        try:
            workflow = _workflows.normalize(arguments.get("workflow"))
        except FactoryError:
            return {"effective_models": {}}
        return {"effective_models": (await self._resolve_models(workflow))[0]}

    async def _workflow(self, name: str, arguments: Json) -> Json:
        service = self.service
        store = service.records()
        if name == "workflow.delete":
            workflow_id = text(arguments, "id")
            await store.delete_workflow(workflow_id)
            await service.triggers.sync()
            self._workflows_changed(workflow_id, 0)
            _LOGGER.info("Factory Workflow deleted (workflow=%s actor=user)", workflow_id)
            return {"deleted": True, "id": workflow_id}
        raw = arguments.get("workflow")
        expected = arguments.get("expected_revision")
        workflow_id = ""
        if expected:
            workflow_id = raw.get("id", "") if isinstance(raw, dict) else ""
            await store.get_workflow(str(workflow_id))
        elif isinstance(raw, dict):
            # A new Workflow, such as a copied template, never takes over a saved id.
            taken = {item["id"] for item in await store.list_workflows()}
            if raw.get("id") in taken:
                raw = {key: value for key, value in raw.items() if key != "id"}
        workflow, effective = await self._validated(raw, workflow_id=workflow_id)
        saved = await store.save_workflow(workflow, expected_revision=expected or 0)
        await service.triggers.sync()
        self._workflows_changed(saved["id"], saved["revision"])
        _LOGGER.info(
            "Factory Workflow saved (workflow=%s actor=user revision=%d)",
            saved["id"],
            saved["revision"],
        )
        service.request_schedule()
        return {"workflow": await self._workflow_view(saved), "effective_models": effective}

    async def _resolve_models(self, workflow: Json) -> tuple[Json, dict[str, str]]:
        """Each Agent step's Model by ``workflow/step``; empty marks a step that has none."""
        host = self.service.host
        assert host is not None
        lanes = [step for step in workflow["steps"] if step["kind"] == "agent"]
        if host.inspect_prompt is None or host.catalog is None:
            return {_workflows.model_key(workflow, step): step["model"] for step in lanes}, {}
        project_id = workflow.get("project_id") or None
        cwd = host.state_dir or host.data_dir
        if project_id:
            catalog = await host.catalog()
            project = next((item for item in catalog["projects"] if item["id"] == project_id), None)
            if project is None:
                return {}, {}
            cwd = Path(project["cwd"])
        result: Json = {}
        problems: dict[str, str] = {}
        resolved: dict[tuple[str, str, str, str], tuple[str, str]] = {}
        for step in lanes:
            if not step["agent"]:
                continue
            key = (step["source"], step["agent"], step["model"], step["effort"])
            if key not in resolved:
                config = TemporaryAgentConfig(
                    model="",
                    cwd=cwd,
                    tool_access=ToolAccess(mode="none"),
                    allowed_skills=[],
                    tools={},
                    name="Factory preview",
                    **_workflows.profile(step),
                    model_override=step["model"] or None,
                    thinking_effort_override=step["effort"] or None,
                )
                try:
                    preview = await host.inspect_prompt(config, project_id)
                    resolved[key] = (preview["model"], "")
                except ValueError as error:
                    # Resolution refuses an Agent without a usable Model; reading
                    # or previewing a Workflow still answers for the other steps.
                    resolved[key] = ("", str(error).rstrip("."))
            model, problem = resolved[key]
            result[_workflows.model_key(workflow, step)] = model
            if problem:
                problems.setdefault(f"Step {step['name']!r}", problem)
        return result, problems

    def _workflows_changed(self, workflow_id: str, revision: int) -> None:
        host = self.service.host
        if host is not None and host.publish_change is not None:
            host.publish_change("workflows", [workflow_id], revision)

    # Card actions

    @staticmethod
    def _step(card: Json, arguments: Json) -> str:
        """The step an action names, by id or name; empty when it names none."""
        value = arguments.get("step") or ""
        if not isinstance(value, str) or not value.strip():
            return ""
        steps = {
            item["id"]: item["name"]
            for item in (card["runtime"].get("workflow") or {}).get("steps", [])
        }
        if value in steps:
            return value
        found = next(
            (key for key, name in steps.items() if name.casefold() == value.strip().casefold()),
            None,
        )
        if found is None:
            raise FactoryError(
                "invalid_step",
                "Choose one of the Card's steps: " + (", ".join(steps.values()) or "(none)") + ".",
            )
        return str(found)

    async def _action(self, name: str, card: Json, arguments: Json) -> Json:
        service = self.service
        store = service.records()
        execution = service.execution
        if name == "card.update":
            require_state(card, {"proposal", "queued"})
            if card["runtime"].get("queued_action"):
                raise FactoryError(
                    "wrong_state",
                    "This Card has already started. Wait for its queued continuation.",
                )
            patch: Json = {
                field: text(arguments, field, maximum=200 if field == "title" else 20000)
                for field in ("title", "description")
                if field in arguments
            }
            if not patch:
                return {"card": self._projection(card)}
            saved = await store.change(
                card["id"],
                patch,
                actor="user",
                event="updated",
                expected_revision=arguments.get("expected_revision"),
            )
        elif name == "card.limits":
            require_state(card, {"waiting"})
            runtime = deepcopy(card["runtime"])
            if not runtime.get("steps"):
                raise FactoryError(
                    "wrong_state",
                    "Configure the Workflow's limits on the Workflows page before this Card "
                    "starts.",
                )
            if "card_limits" in arguments:
                runtime["card_limits"] = _limits.normalize(arguments["card_limits"])
            if "step_limits" in arguments:
                step_id = self._step(card, arguments)
                if not step_id:
                    raise FactoryError(
                        "missing_field", "step_limits needs the step it applies to in step."
                    )
                runtime["steps"][step_id]["limits"] = _limits.normalize(arguments["step_limits"])
            for notice in runtime.get("budget_notices", {}).values():
                notice.pop("pending", None)
            saved = await store.change(
                card["id"],
                {"runtime": runtime},
                actor="user",
                event="limits_updated",
                expected_revision=arguments["expected_revision"],
            )
        elif name == "card.move":
            position = arguments["position"]
            if not isinstance(position, (float, int)) or not math.isfinite(position):
                raise FactoryError(
                    "invalid_position", "Use a finite backlog position from the Board."
                )
            saved = await store.move_card(card["id"], position)
        elif name == "card.approve_proposal":
            require_state(card, {"proposal"})
            saved = await store.change(
                card["id"], {"state": "queued"}, actor="user", event="proposal_approved"
            )
        elif name == "card.delete":
            require_state(card, FINAL_STATES)
            return await execution.delete(card)
        elif name == "card.discard":
            require_state(card, {"proposal", "queued", "working", "waiting"})
            saved = await execution.discard(card)
            return {"card": self._projection(saved)}
        elif name in {"card.accept", "card.send_back", "card.answer", "card.retry"}:
            require_state(card, {"waiting"})
            step = self._step(card, arguments)
            if name == "card.accept":
                if arguments.get("expected_revision") != card["revision"]:
                    raise FactoryError(
                        "revision_conflict",
                        "The Card changed since you viewed it. Refresh it, review its latest "
                        "results, then accept that version.",
                    )
                saved = await execution.accept(card, step)
            elif name == "card.send_back":
                saved = await execution.send_back(card, text(arguments, "comment"), step)
            elif name == "card.answer":
                saved = await execution.answer(card, text(arguments, "text"), step)
            else:
                saved = await execution.retry(card, step)
            return {"card": {**self._projection(saved), "runtime": saved["runtime"]}}
        else:
            raise FactoryError(
                "unknown_operation", "Unknown Factory action. Refresh the operation catalog."
            )
        await service.changed(saved, card, actor="user")
        if saved["state"] == card["state"]:
            _LOGGER.info(
                "Factory Card updated (card=%s actor=user action=%s revision=%d)",
                card["id"],
                name,
                saved["revision"],
            )
        return {"card": {**self._projection(saved), "runtime": saved["runtime"]}}


def _merge_usage(reports: list[Json]) -> Json:
    """Add the usage reports of a Card's execution groups; nested totals are added too."""
    total: Json = {}
    for report in reports:
        for key, value in report.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                total[key] = total.get(key, 0) + value
            elif isinstance(value, dict):
                total[key] = _merge_usage([total.get(key, {}), value])
            elif key not in total:
                total[key] = value
    return total
