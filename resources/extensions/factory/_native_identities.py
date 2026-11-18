"""Durable native identity creation receipts and effective Model preflight."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents.temporary import TemporaryAgentConfig
from core.chat import parse_bare_model
from core.tools.availability import ToolAccess

from ._values import text
from .store import FactoryError

if TYPE_CHECKING:
    from ._native_store import NativeFactoryStore
    from .extension import FactoryExtension

type Json = dict[str, Any]


def personal_agents(catalog: Json) -> list[Json]:
    return [agent for agent in catalog.get("agents", []) if not agent.get("extension_owner")]


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
        return await (
            host.identity_agents.create(agent_id, **config)
            if config
            else host.identity_agents.get(agent_id)
        )

    async def _belongs_here(self, agent_id: str, profile: Json, factory_id: str | None) -> bool:
        if not profile.get("created_by_owner"):
            return not profile.get("extension_owner")
        if not factory_id:
            return False
        origin = await self.store.identity_creation_member(agent_id, profile["incarnation"])
        return bool(origin and origin["factory_id"] == factory_id)

    @staticmethod
    def _confirms(reservation: Json, profile: Json) -> bool:
        if not profile.get("created_by_owner") or not profile.get("incarnation"):
            return False
        proof = reservation.get("proof")
        if proof:
            return bool(proof == profile["incarnation"])
        return bool(profile.get("creation_request_id") == reservation["request_id"])

    async def acknowledge_memberships(self) -> None:
        """Finish receipts interrupted after their membership was already committed."""
        for row in await self.store.pending_identities():
            if not row.get("proof"):
                continue
            origin = await self.store.identity_creation_member(row["agent_id"], row["proof"])
            if not origin:
                continue
            payload = row["payload"]
            if payload.get("factory_id") == origin["factory_id"]:
                resource = origin["id"]
            else:
                home = await self.store.get_factory(origin["factory_id"])
                if payload.get("factory_id") or payload.get("project_id") != home["project_id"]:
                    continue
                resource = home["id"]
            await self.store.complete_identity_reservation(row["request_id"], resource)

    async def pending_profiles(self) -> dict[str, list[Json]]:
        """Offer interrupted creations only inside their originally requested Project."""
        pending = await self.store.pending_identities()
        by_project: dict[str, list[Json]] = {}
        if not pending:
            return by_project
        host = self.service.host
        assert host is not None and host.identity_agents is not None
        for start in range(0, len(pending), 100):
            page = pending[start : start + 100]
            profiles = await host.identity_agents.profiles([row["agent_id"] for row in page])
            for row in page:
                agent_id = row["agent_id"]
                profile = profiles.get(agent_id, {})
                if not self._confirms(row, profile):
                    continue
                if await self.store.identity_creation_member(agent_id, profile["incarnation"]):
                    continue
                payload = row["payload"]
                home = payload.get("factory_id")
                project = (
                    (await self.store.get_factory(home))["project_id"]
                    if home
                    else payload.get("project_id")
                )
                if not project:
                    continue
                by_project.setdefault(project, []).append(
                    {"id": agent_id, "name": profile["name"], "incarnation": profile["incarnation"]}
                )
        return by_project

    async def for_link(
        self, agent_id: str, factory_id: str | None = None, *, project_id: str | None = None
    ) -> Json:
        info = await self._identity(agent_id)
        if await self._belongs_here(agent_id, info, factory_id):
            return info
        if info.get("created_by_owner"):
            pending = await self.store.pending_identities(agent_id=agent_id)
            if pending and self._confirms(pending[0], info):
                row = pending[0]
                payload = row["payload"]
                if not project_id and factory_id and not payload.get("factory_id"):
                    project_id = (await self.store.get_factory(factory_id))["project_id"]
                same_home = (
                    payload["factory_id"] == factory_id
                    if payload.get("factory_id")
                    else bool(project_id and payload.get("project_id") == project_id)
                )
                if same_home and not await self.store.identity_creation_member(
                    agent_id, info["incarnation"]
                ):
                    await self.store.save_identity_reservation(
                        row["request_id"], {"proof": info["incarnation"]}
                    )
                    return {**info, "pending_receipt": row}
        raise FactoryError(
            "identity_outside_factory",
            "Choose a personal Agent or create a specialist for this Factory.",
        )

    async def member_profiles(self, members: list[Json]) -> list[Json]:
        host = self.service.host
        assert host is not None and host.identity_agents is not None
        profiles = await host.identity_agents.profiles([member["agent_id"] for member in members])
        result = []
        for member in members:
            agent_id, proof = member["agent_id"], member.get("agent_proof")
            profile = profiles.get(agent_id, {})
            created_here = bool(member.get("created_by_factory"))
            available = bool(proof and profile.get("incarnation") == proof)
            if available:
                available = (
                    bool(profile.get("created_by_owner"))
                    if created_here
                    else await self._belongs_here(agent_id, profile, member["factory_id"])
                )
            result.append(
                {
                    **member,
                    "agent_name": profile.get("name") if available else None,
                    "agent_placement": profile.get("placement") if available else None,
                    "profile_available": available,
                    "created_by_factory": created_here
                    or bool(available and profile.get("created_by_owner")),
                }
            )
        return result

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
            if not self._confirms(reservation, info):
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
        factory = args if leader else await self.store.get_factory(args["factory_id"])
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
