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
from core.utils.logging import get_logger

from . import _limits, _workflows
from ._values import SETUP_DEFAULTS, require_state, text
from .store import ACTIVE_STATES, FINAL_STATES, FactoryError

if TYPE_CHECKING:
    from .extension import FactoryExtension

type Json = dict[str, Any]
ACCEPTABLE = frozenset({"checkpoint", "finished", "no_agreement"})
_LOGGER = get_logger("extensions.factory")


class FactoryManagement:
    def __init__(self, service: FactoryExtension) -> None:
        self.service = service

    async def operation(self, name: str, arguments: Json) -> Json:
        if name.startswith("library."):
            from ._library_management import operation

            return await operation(self.service, name, arguments)
        if name in {"catalog", "board.get", "card.get", "setup.list", "setup.get", "setup.preview"}:
            return await self._read(name, arguments)
        async with self.service.lock:
            if name.startswith("setup."):
                return await self._setup(name, arguments)
            if name == "card.create":
                card = await self.service.create_card(arguments, origin="operator")
                return {"card": self._projection(card)}
            card = await self.service.records().get_card(text(arguments, "id"))
            return await self._action(name, card, arguments)

    @staticmethod
    def _projection(card: Json) -> Json:
        from .extension import projection

        return projection(card)

    async def _read(self, name: str, arguments: Json) -> Json:
        service = self.service
        store = service.records()
        if name == "catalog":
            assert service.host is not None and service.host.catalog is not None
            return {
                "catalog": await service.host.catalog(),
                "templates": _workflows.templates(),
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
                    "running": sum(card["state"] in ACTIVE_STATES for card in all_cards),
                    "limit": limit,
                },
                "revision": max((card["revision"] for card in all_cards), default=0),
            }
        if name == "setup.list":
            return {"setups": await store.list_setups()}
        if name == "setup.preview":
            values = {**SETUP_DEFAULTS, **arguments}
            try:
                values["workflows"] = _workflows.normalize(values["workflows"])
            except FactoryError:
                return {"effective_models": {}}
            return {"effective_models": await self._effective_models(values)}
        if name == "setup.get":
            project_id = text(arguments, "project_id")
            try:
                setup = await store.get_setup(project_id)
            except FactoryError as error:
                if error.code != "setup_not_found":
                    raise
                starter = _workflows.templates()[0]
                setup = {
                    "project_id": project_id,
                    **SETUP_DEFAULTS,
                    "workflows": [starter],
                    "default_workflow": starter["id"],
                    "revision": 0,
                }
            return {"setup": setup, "effective_models": await self._effective_models(setup)}
        card = await store.get_card(text(arguments, "id"))
        return await self._detail(card)

    async def _detail(self, card: Json) -> Json:
        service = self.service
        store = service.records()
        host = service.host
        assert host is not None and host.temporary_agents is not None
        participants = []
        after = ""
        while True:
            bindings = await host.temporary_agents.list(card["group_id"], after=after, limit=100)
            for binding in bindings:
                participants.append(
                    {
                        "participant_id": binding.participant_id,
                        "address": asdict(binding.address),
                        "display_name": binding.config.get("name", binding.participant_id),
                        "model": binding.config.get("model", ""),
                    }
                )
            if len(bindings) < 100:
                break
            after = bindings[-1].participant_id
        run_ids = card["runtime"].get("runs", {})
        owned = (
            await host.temporary_agents.owned_runs(card["group_id"], list(run_ids.values()))
            if run_ids
            else {}
        )
        runs = []
        for participant in participants:
            run_id = run_ids.get(participant["participant_id"])
            inspected = owned.get(run_id)
            run = inspected.run if inspected is not None else None
            status = (
                run.status.value
                if run is not None
                else inspected.record.terminal_status
                if inspected is not None
                else None
            )
            participant.update(
                lifecycle_run_id=run_id,
                run_active=status in {"pending", "running", "cancelling"},
                state=status,
            )
            if run_id:
                runs.append(
                    {
                        "run_id": run_id,
                        "participant_id": participant["participant_id"],
                        "status": status,
                    }
                )
        approvals = (
            await host.pending_approvals(card["group_id"], list(owned))
            if host.pending_approvals is not None
            else {"requests": []}
        )
        return {
            "card": {**self._projection(card), "runtime": card["runtime"]},
            **await store.history(card["id"]),
            "participants": participants,
            "runs": runs,
            "approvals": approvals["requests"],
            "usage": await host.temporary_agents.usage(card["group_id"], {}),
            "budget": await service.execution.budget_status(card, report_all=True),
        }

    async def _setup(self, name: str, arguments: Json) -> Json:
        service = self.service
        store = service.records()
        project_id = text(arguments, "project_id")
        if name == "setup.delete":
            await store.delete_setup(project_id)
            self._setup_changed(project_id, 0)
            _LOGGER.info("Factory setup deleted (project=%s actor=user)", project_id)
            return {"deleted": True, "project_id": project_id}
        setups = await store.list_setups()
        previous = next((setup for setup in setups if setup["project_id"] == project_id), None)
        values = {
            **SETUP_DEFAULTS,
            **(previous or {}),
            **{key: value for key, value in arguments.items() if key in SETUP_DEFAULTS},
            "project_id": project_id,
        }
        assert service.host is not None and service.host.catalog is not None
        catalog = await service.host.catalog()
        project = next((item for item in catalog["projects"] if item["id"] == project_id), None)
        if project is None:
            raise FactoryError(
                "project_not_found",
                "The Project does not exist. Choose a Project from the catalog.",
            )
        values["workflows"] = _workflows.normalize(values["workflows"])
        if values["default_workflow"] not in {item["id"] for item in values["workflows"]}:
            values["default_workflow"] = values["workflows"][0]["id"]
        choices = {
            "agent": {item["id"] for item in catalog.get("agents", [])},
            "team": {item["id"] for item in project.get("agent_profiles", [])},
        }
        models = {item["id"] for item in catalog["models"]}
        for label, _workflow, lane in _workflows.steps(values["workflows"]):
            if lane["agent"] not in choices[lane["source"]]:
                raise FactoryError(
                    "profile_not_found",
                    f"{label}: choose an available Agent or a member of the Project's Team.",
                )
            if lane["model"] and lane["model"].partition("::")[0] not in models:
                raise FactoryError(
                    "model_not_found",
                    f"{label}: the Model is unavailable. Choose a Model from the catalog "
                    "or clear it to use the Agent's Model.",
                )
            if lane["effort"]:
                try:
                    lane["effort"] = normalize_agent_default_value(
                        "thinking_effort", lane["effort"]
                    )
                except ValueError as error:
                    raise FactoryError(
                        "invalid_effort", f"{label}: {str(error).rstrip('.')}."
                    ) from error
        effective, problems = await self._resolve_models(values)
        if problems:
            label, problem = next(iter(problems.items()))
            raise FactoryError(
                "model_unresolved",
                f"{label} cannot run with this setup: {problem}. Choose a Model for it, "
                "or give its Agent a usable Model.",
            )
        saved = await store.save_setup(values, expected_revision=arguments.get("expected_revision"))
        self._setup_changed(project_id, saved["revision"])
        _LOGGER.info(
            "Factory setup saved (project=%s actor=user revision=%d)", project_id, saved["revision"]
        )
        service.request_schedule()
        return {"setup": saved, "effective_models": effective}

    async def _effective_models(self, values: Json) -> Json:
        """Each step's Model by ``workflow/step``; empty marks a step that has none."""
        return (await self._resolve_models(values))[0]

    async def _resolve_models(self, values: Json) -> tuple[Json, dict[str, str]]:
        host = self.service.host
        assert host is not None
        lanes = _workflows.steps(values.get("workflows") or [])
        if host.inspect_prompt is None or host.catalog is None:
            return {
                _workflows.model_key(workflow, lane): lane["model"]
                for _label, workflow, lane in lanes
            }, {}
        catalog = await host.catalog()
        project = next(
            (item for item in catalog["projects"] if item["id"] == values["project_id"]), None
        )
        if project is None:
            return {}, {}
        result: Json = {}
        problems: dict[str, str] = {}
        resolved: dict[tuple[str, str, str, str], tuple[str, str]] = {}
        for label, workflow, lane in lanes:
            if not lane["agent"]:
                continue
            key = (lane["source"], lane["agent"], lane["model"], lane["effort"])
            if key not in resolved:
                config = TemporaryAgentConfig(
                    model="",
                    cwd=Path(project["cwd"]),
                    tool_access=ToolAccess(mode="none"),
                    allowed_skills=[],
                    tools={},
                    name="Factory preview",
                    **_workflows.profile(lane),
                    model_override=lane["model"] or None,
                    thinking_effort_override=lane["effort"] or None,
                )
                try:
                    preview = await host.inspect_prompt(config, values["project_id"])
                    resolved[key] = (preview["model"], "")
                except ValueError as error:
                    # Resolution refuses an Agent without a usable Model; reading
                    # or previewing a setup still answers for the other steps.
                    resolved[key] = ("", str(error).rstrip("."))
            model, problem = resolved[key]
            result[_workflows.model_key(workflow, lane)] = model
            if problem:
                problems.setdefault(label, problem)
        return result, problems

    def _setup_changed(self, project_id: str, revision: int) -> None:
        host = self.service.host
        if host is not None and host.publish_change is not None:
            host.publish_change("setups", [project_id], revision)

    async def _action(self, name: str, card: Json, arguments: Json) -> Json:
        service = self.service
        store = service.records()
        if name == "card.update":
            require_state(card, {"proposal", "queued"})
            if card["runtime"].get("pending_action"):
                raise FactoryError(
                    "phase_queued",
                    "This Card is queued to resume an existing phase. "
                    "Its reviewed objective cannot be edited while it waits for a slot.",
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
            if not runtime.get("workflow"):
                raise FactoryError(
                    "wrong_state",
                    "Configure the Workflow limits in the Project setup before starting this Card.",
                )
            for key in ("card_limits", "step_limits"):
                if key in arguments:
                    runtime[key] = _limits.normalize(arguments[key])
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
            result = await service.execution.delete(card)
            return result if isinstance(result, dict) else {"deleted": True, "id": card["id"]}
        elif name == "card.discard":
            require_state(
                card,
                {
                    "proposal",
                    "queued",
                    "working",
                    "waiting",
                },
            )
            result = await service.execution.discard(card)
            return (
                result
                if isinstance(result, dict) and "card" in result
                else {"card": self._projection(await store.get_card(card["id"]))}
            )
        elif name in {"card.accept", "card.send_back", "card.answer", "card.retry"}:
            require_state(card, {"waiting"})
            action = name.removeprefix("card.")
            if action == "accept" and arguments.get("expected_revision") != card["revision"]:
                raise FactoryError(
                    "revision_conflict",
                    "The Card changed since you viewed it. Refresh it, review its latest "
                    "results, then accept that version.",
                )
            if action == "accept" and card["waiting_reason"] not in ACCEPTABLE:
                raise FactoryError(
                    "not_acceptable",
                    "This Card has nothing to accept. "
                    "Answer its question, send it back or retry the failed step.",
                )
            if action == "answer" and card["waiting_reason"] != "blocked":
                raise FactoryError(
                    "not_blocked",
                    "The Card has no open question. Use Send back with your comment instead.",
                )
            if action == "retry" and card["waiting_reason"] not in {
                "run_failed",
                "restart",
                "budget_exhausted",
            }:
                raise FactoryError(
                    "not_retryable",
                    "This Card is not in a failed or interrupted phase. "
                    "Accept it or send it back instead.",
                )
            comment = (
                text(arguments, "text" if action == "answer" else "comment")
                if action in {"send_back", "answer"}
                else ""
            )
            step = arguments.get("step") or arguments.get("stage") or ""
            if action == "send_back" and step:
                workflow = card["runtime"].get("workflow") or {}
                steps = {item["id"]: item["name"] for item in workflow.get("steps", [])}
                if step not in steps:
                    step = next(
                        (
                            key
                            for key, value in steps.items()
                            if value.casefold() == step.casefold()
                        ),
                        step,
                    )
                if step not in steps:
                    raise FactoryError(
                        "invalid_step",
                        "Send the Card back to one of its steps: "
                        + (", ".join(steps.values()) or "(none)")
                        + ".",
                    )
            action = "send_back" if action == "answer" else action
            cards = await store.list_cards()
            limit, _ = service.limits()
            finishes = False
            if action == "accept":
                runtime = card["runtime"]
                done = [*runtime.get("done", [])]
                if card["waiting_reason"] == "no_agreement":
                    done.append(runtime["step"])
                finishes = _workflows.next_step(runtime["workflow"], done) is None
            if not finishes and sum(item["state"] in ACTIVE_STATES for item in cards) >= limit:
                runtime = {
                    **card["runtime"],
                    "pending_action": {
                        "action": action,
                        "comment": comment,
                        "step": step,
                        "comment_recorded": bool(comment),
                        "waiting_reason": card["waiting_reason"],
                    },
                }
                saved = await store.change(
                    card["id"],
                    {"state": "queued", "runtime": runtime},
                    actor="user",
                    event="queued_for_slot",
                    detail={"action": action},
                    comment=comment or None,
                )
            else:
                if action == "accept":
                    await service.execution.accept(card)
                elif action == "send_back":
                    await service.execution.send_back(card, comment, step)
                else:
                    await service.execution.retry(card)
                return {"card": self._projection(await store.get_card(card["id"]))}
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
