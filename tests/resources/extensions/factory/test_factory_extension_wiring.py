"""End-to-end wiring tests for the Simplified Factory Jobs extension."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.database import open_offline_database
from core.extensions import ExtensionAPI, SessionRequestContext
from core.extensions.databases import extension_database_spec
from core.extensions.extensions import ExtensionDeclarations
from core.extensions.operations import ExtensionHost
from core.sessions import SessionAddress
from resources.extensions.factory import register
from resources.extensions.factory.extension import FactoryExtension
from resources.extensions.factory.store import DATABASE_NAME, MIGRATIONS, SCHEMA_SQL, FactoryStore
from tests.resources.extensions.factory.factory_test_support import FakeHost, FakeTriggers

type Json = dict[str, Any]


class _FakeTemporaryAgents:
    def __init__(self, initial_groups: list[str] | None = None) -> None:
        self._groups = list(initial_groups or [])
        self.closed: list[str] = []
        self.archived: list[str] = []
        self.created: list[tuple[str, str, Any, str | None]] = []
        self.started: list[tuple[str, str, Any]] = []

    async def groups(self, *, after: str = "", limit: int = 100) -> list[str]:
        items = [g for g in sorted(self._groups) if g > after]
        return items[:limit]

    async def close_group(self, group_id: str, reason: str = "extension") -> dict[str, Any]:
        self.closed.append(group_id)
        return {"closed": True}

    async def archive_group(self, group_id: str) -> int:
        self.archived.append(group_id)
        return 1

    async def create(
        self,
        group_id: str,
        participant_id: str,
        config: Any,
        *,
        project_id: str | None = None,
    ) -> Any:
        self.created.append((group_id, participant_id, config, project_id))
        return SimpleNamespace(
            address=SessionAddress("agent-1", "session-1", project_id or ""),
            generation_id="gen-1",
        )

    async def open_group(self, group_id: str) -> Any:
        return SimpleNamespace(group_id=group_id, epoch="ep-1")

    async def start(self, handle: Any, participant_id: str, run_input: Any) -> Any:
        self.started.append((handle.group_id, participant_id, run_input))
        return SimpleNamespace(run_id="run-1", existing=False, status="running")

    async def owned_runs(self, group_id: str, run_ids: list[str]) -> dict[str, Any]:
        return {}


class _FakeIdentitiesHost(FakeHost):
    def __init__(self, directory: Path, initial_groups: list[str] | None = None) -> None:
        super().__init__(directory)
        self.temporary_agents = _FakeTemporaryAgents(initial_groups)
        self.services = SimpleNamespace(query=self._query)
        self.identity_agents = SimpleNamespace(
            get=self._get_identity,
            create=self._create_identity,
            profiles=self._profiles,
        )
        self.database: Any = None
        (directory / "project").mkdir(parents=True, exist_ok=True)
        self.catalog_data = {
            "tools": [],
            "tool_activations": [],
            "projects": [
                {
                    "id": "project",
                    "display_name": "Project",
                    "cwd": str(directory / "project"),
                    "agent_profiles": [],
                }
            ],
            "agents": [
                {
                    "id": "leader",
                    "name": "Leader",
                    "placement": "personal",
                    "model": "test/leader",
                }
            ],
            "models": [{"id": "test/leader"}],
            "permission_profiles": [],
            "default_model": "test/leader",
        }

    async def open_database(self, name: str, schema: str, migrations: tuple[Any, ...] = ()) -> Any:
        spec = extension_database_spec(
            self.data_dir, "factory", name, schema, migrations=migrations
        )
        self.database = open_offline_database(spec)
        return self.database

    async def _query(self, method: str, params: Json) -> Json:
        if method == "agent.describe_profiles":
            return {
                "profiles": [
                    {
                        "agent_id": aid,
                        "agent_name": "Leader",
                        "agent_placement": "personal",
                        "profile_available": True,
                        "created_by_factory": False,
                        "incarnation": "inc-1",
                    }
                    for aid in params.get("agent_ids", [])
                ]
            }
        if method == "agent.acknowledge_profiles":
            return {"acknowledged": len(params.get("assignments", []))}
        raise AssertionError(f"Unexpected service query: {method}")

    async def _profiles(self, agent_ids: list[str]) -> Json:
        return {
            aid: {
                "agent_id": aid,
                "agent_name": "Leader",
                "agent_placement": "personal",
                "profile_available": True,
                "created_by_factory": False,
                "incarnation": "inc-1",
            }
            for aid in agent_ids
        }

    async def _get_identity(self, agent_id: str) -> Json:
        return {
            "agent_id": agent_id,
            "name": "Leader",
            "placement": "personal",
            "model": "test/leader",
            "created_by_owner": False,
            "extension_owner": "",
            "incarnation": "inc-1",
        }

    async def _create_identity(self, agent_id: str, **config: Any) -> Json:
        return {
            "id": agent_id,
            "name": config.get("name", agent_id),
            "placement": "personal",
            "model": config.get("model", "test/leader"),
            "created_by_owner": True,
            "extension_owner": "factory",
            "incarnation": "inc-1",
        }


def _build_service() -> tuple[ExtensionAPI, ExtensionDeclarations, FactoryExtension]:
    declarations = ExtensionDeclarations()
    config: Json = {"wip_limit": 2}
    api = ExtensionAPI(
        "factory", declarations, config={}, logger=None, config_provider=lambda: dict(config)
    )
    register(api)
    handler = next(tool.handler for tool in declarations.tools if tool.name == "factory")
    service = handler.__self__  # type: ignore[attr-defined]
    assert isinstance(service, FactoryExtension)
    return api, declarations, service


def test_registration_surfaces() -> None:
    api, declarations, _service = _build_service()

    assert [tool.name for tool in declarations.tools if not tool.session_scoped] == ["factory"]
    assert {tool.name for tool in declarations.tools if tool.session_scoped} == {
        "factory_job_status",
        "factory_jobs",
        "factory_members",
        "factory_settings",
    }
    assert declarations.settings_schema is not None
    assert [setting.key for setting in declarations.settings_schema] == ["wip_limit"]

    assert declarations.operations is not None
    op_names = set(declarations.operations.operations)
    assert "catalog" in op_names
    assert {
        "factory.list",
        "factory.get",
        "factory.create",
        "factory.update",
        "factory.archive",
        "member.list",
        "member.add",
        "member.create",
        "member.update",
        "member.retire",
        "agent.profile_targets",
        "conversation.get",
        "conversation.send",
        "conversation.stop",
        "job.list",
        "job.get",
        "job.create",
        "job.update",
        "job.delete",
        "job.trigger",
        "job.secret",
        "job_run.list",
        "job_run.get",
        "job_run.reply",
        "job_run.cancel",
        "dashboard.get",
    }.issubset(op_names)
    for legacy_prefix in ("workflow.", "card.", "task.", "topic.", "work."):
        assert not any(name.startswith(legacy_prefix) for name in op_names)
    assert declarations.operations.operations["job.secret"].secret is True
    assert declarations.operations.operations["job.list"].secret is False


@pytest.mark.asyncio
async def test_startup_archives_legacy_groups_and_syncs_triggers(tmp_path: Path) -> None:
    api, _decl, service = _build_service()
    host = _FakeIdentitiesHost(
        tmp_path,
        initial_groups=["card-legacy-1", "fjob.run-1", "native.exec-active"],
    )
    # Seed an active native execution so native.exec-active is preserved.
    db = await host.open_database(DATABASE_NAME, SCHEMA_SQL, MIGRATIONS)
    store = FactoryStore(db)
    fac = await store.native.create_factory(
        "project", leader_agent_id="leader", name="Test Factory"
    )
    topic = await store.native.create_topic(
        fac["id"],
        "Chat",
        participants=[fac["leader_member_id"]],
        direct_member_id=fac["leader_member_id"],
    )
    await store.native.create_execution(
        fac["id"],
        "topic",
        topic["id"],
        fac["leader_member_id"],
        1,
        group_id="native.exec-active",
        participant_id=fac["leader_member_id"],
    )
    job = await store.jobs.create_job(
        fac["id"],
        name="Nightly",
        prompt="Run checks",
        triggers=[{"id": "trg1", "type": "schedule", "schedule": "0 9 * * *", "enabled": True}],
    )
    db.close()

    # Re-open via start()
    await service.start(cast(ExtensionHost, host))
    try:
        assert "card-legacy-1" in host.temporary_agents.archived
        assert "fjob.run-1" not in host.temporary_agents.archived
        assert "native.exec-active" not in host.temporary_agents.archived
        assert isinstance(host.triggers, FakeTriggers)
        assert host.triggers.handler == service.job_triggers.fire
        assert f"job:{fac['id']}:{job['id']}:trg1" in host.triggers.items
        assert host.badges[-1] == ("board", 0)

        catalog_res = await api.operations.invoke("catalog", {})
        assert "projects" in catalog_res["catalog"]
        assert "agents" in catalog_res["catalog"]
        assert "models" in catalog_res["catalog"]
        assert "permission_profiles" in catalog_res["catalog"]
        assert "default_model" in catalog_res["catalog"]
        assert len(catalog_res["catalog"]["factories"]) == 1
        assert catalog_res["triggers_available"] is True
    finally:
        await service.close()
        if host.database is not None:
            host.database.close()


@pytest.mark.asyncio
async def test_leader_conversation_injects_jobs_context_and_tools(tmp_path: Path) -> None:
    api, _decl, service = _build_service()
    host = _FakeIdentitiesHost(tmp_path)
    await service.start(cast(ExtensionHost, host))
    try:
        created = await api.operations.invoke(
            "factory.create",
            {"project_id": "project", "leader_agent_id": "leader", "name": "Ops Factory"},
        )
        factory_id = created["factory"]["id"]
        await api.operations.invoke(
            "job.create",
            {
                "factory_id": factory_id,
                "name": "Deploy Check",
                "prompt": "Verify staging health",
                "description": "Checks staging endpoints.",
            },
        )
        await api.operations.invoke(
            "conversation.send",
            {"project_id": "project", "text": "Hi Leader", "request_id": "req-1"},
        )
        # Drain background launch task
        while tasks := [t for t in service._tasks if not t.done()]:
            await tasks[0]
        assert len(host.temporary_agents.created) == 1
        group_id, _pid, config, _proj = host.temporary_agents.created[0]
        assert group_id.startswith("native.")
        assert "factory_jobs" not in config.tool_access.denied
        assert "factory_members" not in config.tool_access.denied
        assert "factory_settings" not in config.tool_access.denied
        assert "factory_job_status" in config.tool_access.denied
        assert "Deploy Check" in config.instructions
        assert "Checks staging endpoints." in config.instructions
    finally:
        await service.close()
        if host.database is not None:
            host.database.close()


@pytest.mark.asyncio
async def test_runtime_routing_and_quiesce(tmp_path: Path) -> None:
    _api, _decl, service = _build_service()
    host = _FakeIdentitiesHost(tmp_path)
    await service.start(cast(ExtensionHost, host))
    try:
        fjob_ctx = SimpleNamespace(binding=SimpleNamespace(group_id="fjob.run-1"))
        native_ctx = SimpleNamespace(binding=SimpleNamespace(group_id="native.exec-1"))
        assert (
            service._runtime_owner(cast(SessionRequestContext, fjob_ctx)) is service.job_execution
        )
        assert (
            service._runtime_owner(cast(SessionRequestContext, native_ctx))
            is service.native.sessions
        )

        await service.quiesce("test")
        assert service.native.quiescing is True
        assert service.job_execution._quiescing is True
    finally:
        await service.close()
        if host.database is not None:
            host.database.close()
