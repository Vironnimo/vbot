"""Operator actions and read models shared by the Factory page and CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._native_identities import personal_agents
from .store import FactoryError

if TYPE_CHECKING:
    from .extension import FactoryExtension

type Json = dict[str, Any]


class FactoryManagement:
    def __init__(self, service: FactoryExtension) -> None:
        self.service = service

    async def operation(self, name: str, arguments: Json) -> Json:
        if name == "catalog":
            return await self._catalog()
        if name.startswith("job.") or name.startswith("job_run.") or name == "dashboard.get":
            result = await self.service.job_management.operation(name, arguments)
            if result is not None:
                return result
        if name.partition(".")[0] in {"agent", "factory", "member", "conversation"}:
            return await self.service.native_management.operation(name, arguments)
        raise FactoryError(
            "unknown_operation", "Unknown Factory action. Refresh the operation catalog."
        )

    async def _catalog(self) -> Json:
        service = self.service
        store = service.records()
        assert service.host is not None and service.host.catalog is not None
        catalog = await service.host.catalog()
        pending = await service.native_management.identities.pending_profiles()
        by_project = await self._project_profiles(catalog)
        factories: list[Json] = []
        after = ""
        while True:
            page = await store.native.list_factories(after=after, limit=100)
            factories.extend(page)
            if len(page) < 100:
                break
            after = page[-1]["id"]
        return {
            "catalog": {
                **catalog,
                "agents": personal_agents(catalog),
                "projects": [
                    {
                        **item,
                        "factory_agents": by_project.get(item["id"], []),
                        "pending_factory_agents": pending.get(item["id"], []),
                    }
                    for item in catalog["projects"]
                ],
                "factories": factories,
            },
            "triggers_available": service.host.triggers is not None,
        }

    async def _project_profiles(self, catalog: Json) -> dict[str, list[Json]]:
        by_project: dict[str, list[Json]] = {}
        owned = [agent for agent in catalog["agents"] if agent.get("extension_owner") == "factory"]
        if owned:
            projects = await self.service.records().native.workflow_profile_projects()
            for agent in owned:
                project = projects.get((agent["id"], agent["incarnation"]))
                if project:
                    by_project.setdefault(project, []).append(agent)
        return by_project
