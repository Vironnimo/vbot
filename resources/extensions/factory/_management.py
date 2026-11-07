"""Operator actions and read models shared by the Factory page and CLI."""

from __future__ import annotations

import math
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents.temporary import TemporaryAgentConfig
from core.settings.agent_defaults import normalize_agent_default_value
from core.tools.availability import ToolAccess
from core.utils.logging import get_logger

from ._values import SETUP_DEFAULTS, require_state, text
from .store import ACTIVE_STATES, FINAL_STATES, FactoryError

if TYPE_CHECKING:
    from .extension import FactoryExtension

type Json = dict[str, Any]
_LOGGER = get_logger("extensions.factory")


class FactoryManagement:
    def __init__(self, service: FactoryExtension) -> None:
        self.service = service

    async def operation(self, name: str, arguments: Json) -> Json:
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
            return {"catalog": await service.host.catalog()}
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
            return {"effective_models": await self._effective_models(values)}
        if name == "setup.get":
            project_id = text(arguments, "project_id")
            try:
                setup = await store.get_setup(project_id)
            except FactoryError as error:
                if error.code != "setup_not_found":
                    raise
                setup = {"project_id": project_id, **SETUP_DEFAULTS, "revision": 0}
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
        profiles = {item["id"] for item in project.get("agent_profiles", [])}
        models = {item["id"] for item in catalog["models"]}
        for role in ("worker", "reviewer"):
            if values[f"{role}_profile"] not in profiles:
                available = ", ".join(sorted(profiles)) or "(none; configure a Team profile first)"
                raise FactoryError(
                    "profile_not_found",
                    f"Choose an available {role} profile from the Project's Team: {available}.",
                )
            model = values[f"{role}_model"]
            if model and model.partition("::")[0] not in models:
                raise FactoryError(
                    "model_not_found",
                    f"The {role} Model is unavailable. Choose a Model from the catalog "
                    "or clear it to inherit the profile's Model.",
                )
            if values[f"{role}_effort"]:
                values[f"{role}_effort"] = normalize_agent_default_value(
                    "thinking_effort", values[f"{role}_effort"]
                )
        if not values["base_branch"].strip() or values["base_branch"].startswith("-"):
            raise FactoryError(
                "invalid_base", "Use a non-empty base branch name that does not begin with '-'."
            )
        effective = await self._effective_models(values)
        saved = await store.save_setup(values, expected_revision=arguments.get("expected_revision"))
        self._setup_changed(project_id, saved["revision"])
        _LOGGER.info(
            "Factory setup saved (project=%s actor=user revision=%d)", project_id, saved["revision"]
        )
        service.request_schedule()
        return {"setup": saved, "effective_models": effective}

    async def _effective_models(self, values: Json) -> Json:
        host = self.service.host
        assert host is not None
        if host.inspect_prompt is None or host.catalog is None:
            return {role: values.get(f"{role}_model", "") for role in ("worker", "reviewer")}
        catalog = await host.catalog()
        project = next(
            (item for item in catalog["projects"] if item["id"] == values["project_id"]), None
        )
        if project is None:
            return {}
        result = {}
        for role in ("worker", "reviewer"):
            profile = values.get(f"{role}_profile")
            if not profile:
                continue
            config = TemporaryAgentConfig(
                model="",
                cwd=Path(project["cwd"]),
                tool_access=ToolAccess(mode="none"),
                allowed_skills=[],
                tools={},
                name=f"Factory {role} preview",
                repository_profile=profile,
                model_override=values.get(f"{role}_model") or None,
                thinking_effort_override=values.get(f"{role}_effort") or None,
            )
            preview = await host.inspect_prompt(config, values["project_id"])
            result[role] = preview["model"]
        return result

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
                    "reviewing",
                    "waiting",
                    "integrating",
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
            if action == "accept" and card["waiting_reason"] not in {"approved", "no_agreement"}:
                raise FactoryError(
                    "not_reviewed",
                    "This Card needs a submitted review before acceptance. "
                    "Answer its question, send it back or retry the failed phase.",
                )
            if action == "answer" and card["waiting_reason"] != "blocked":
                raise FactoryError(
                    "not_blocked",
                    "The Card has no open question. Use Send back with your comment instead.",
                )
            if action == "retry" and card["waiting_reason"] not in {
                "run_failed",
                "review_failed",
                "restart",
                "integration_failed",
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
            action = "send_back" if action == "answer" else action
            cards = await store.list_cards()
            limit, _ = service.limits()
            if sum(item["state"] in ACTIVE_STATES for item in cards) >= limit:
                runtime = {
                    **card["runtime"],
                    "pending_action": {
                        "action": action,
                        "comment": comment,
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
                    await service.execution.send_back(card, comment)
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
        return {"card": self._projection(saved)}
