"""Durable native identity creation receipts and effective Model preflight."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.agents.temporary import TemporaryAgentConfig
from core.chat import parse_bare_model
from core.tools.availability import ToolAccess

from ._values import text
from .store import FactoryError

if TYPE_CHECKING:
    from ._native_store import NativeFactoryStore
    from .extension import FactoryExtension

type Json = dict[str, Any]


class FactoryIdentities:
    """Identity creation receipts and effective Model preflight for native memberships."""

    def __init__(self, service: FactoryExtension) -> None:
        self.service = service

    @property
    def store(self) -> NativeFactoryStore:
        return self.service.records().native

    async def _identity(self, agent_id: str, **config: Any) -> Json:
        host = self.service.host
        assert host is not None and host.identity_agents is not None
        return cast(
            Json,
            await (
                host.identity_agents.create(agent_id, **config)
                if config
                else host.identity_agents.get(agent_id)
            ),
        )

    async def _create_identity(self, args: Json, *, leader: bool = False) -> Json:
        request_id = text(args, "request_id")
        name = text(args, "leader_name" if leader else "name", maximum=200)
        payload = {key: value for key, value in args.items() if key != "expected_revision"}
        reservation = await self.store.identity_reservation(request_id) or {}
        if reservation and reservation["payload"] != payload:
            raise FactoryError(
                "receipt_conflict", "This request id already describes another identity creation."
            )
        if reservation.get("proof"):
            info = await self._identity(reservation["agent_id"])
            if info["incarnation"] != reservation["proof"]:
                raise FactoryError(
                    "identity_replaced",
                    (
                        "The reserved Identity Agent was replaced. Inspect the pending creation "
                        "before retrying."
                    ),
                )
            return info
        model = reservation.get("identity_config", {}).get("model") or args.get(
            "leader_model" if leader else "model", ""
        )
        if args.get("factory_id"):
            factory = await self.store.get_factory(args["factory_id"])
            allowed = factory.get("config", {}).get("allowed_models")
            if allowed is not None:
                candidate = model
                if not candidate:
                    host = self.service.host
                    assert host is not None and host.services is not None
                    default = await host.services.query(
                        "settings.get_path", {"path": "defaults.agent.model", "allow_missing": True}
                    )
                    candidate = default["setting"].get("value") or ""
                self._check_model_boundary(candidate, allowed)
        reservation = await self.store.reserve_identity(request_id, payload)
        identity_config = reservation.get("identity_config") or {
            "name": name,
            **({"model": model} if model else {}),
        }
        await self.store.save_identity_reservation(request_id, {"identity_config": identity_config})
        info = await self._identity(
            reservation["agent_id"], request_id=request_id, **identity_config
        )
        await self.store.save_identity_reservation(request_id, {"proof": info["incarnation"]})
        return info

    async def _validate_effective_model(self, info: Json, project_id: str, boundary: Json) -> None:
        preview = await self._preview_identity(info, project_id)
        self._check_model_boundary(preview["model"], boundary.get("allowed_models"))

    @staticmethod
    def _check_model_boundary(model: str, allowed: list[str] | None) -> None:
        if allowed is not None and model not in allowed and parse_bare_model(model) not in allowed:
            raise FactoryError(
                "configuration_boundary",
                "Choose a Model allowed by the Factory's configuration boundary.",
            )

    async def _preview_identity(self, info: Json, project_id: str) -> Json:
        host = self.service.host
        assert host is not None
        if host.inspect_prompt is not None and host.catalog is not None:
            catalog = await host.catalog()
            project = next(
                (project for project in catalog["projects"] if project["id"] == project_id), None
            )
            if project is None:
                raise FactoryError(
                    "project_missing", "The Factory's registered Project is unavailable."
                )
            try:
                return await host.inspect_prompt(
                    TemporaryAgentConfig(
                        model="",
                        name=info.get("name") or info["agent_id"],
                        cwd=Path(project["cwd"]),
                        identity_agent=info["agent_id"],
                        identity_incarnation=info["incarnation"],
                        tool_access=ToolAccess(mode="none"),
                        allowed_skills=[],
                        tools={},
                    ),
                    project_id,
                )
            except ValueError as error:
                raise FactoryError(
                    "model_unavailable", f"Choose a usable Model for this member. {error}"
                ) from error

        model = info.get("model", "")
        if not model:
            raise FactoryError(
                "model_required", "Choose a usable Model for this member or its Identity Agent."
            )
        return {"model": model}
