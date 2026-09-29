"""Swarm fixtures: a bare Store, a registered Extension with bound participants, and live Runs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio

from core.agents.temporary import (
    TemporaryAgentConfig,
    TemporaryAgentRegistry,
    TemporaryExecutionGroups,
)
from core.database import write_bootstrap_marker
from core.extensions import (
    ExtensionAPI,
    ExtensionRecord,
    ExtensionRegistrationIdentity,
    ExtensionRegistry,
)
from core.extensions.databases import Database, ExtensionDatabases
from core.extensions.extensions import ExtensionDeclarations
from core.extensions.operations import ExtensionHost
from core.runs import ChatRunManager, RunExecutionOwner
from core.sessions import ChatSessionManager
from core.tools import ToolContext, ToolRegistry
from core.tools.availability import ToolAccess
from resources.extensions.swarm.extension import register
from resources.extensions.swarm.store import SwarmStore
from tests.core.chat.chat_loop_support import StubAdapter, StubAgent, StubRuntime, build_chat_loop
from tests.resources.extensions.swarm.swarm_test_support import open_swarm_database


@pytest.fixture
def swarm_database(tmp_path: Path) -> Iterator[Database]:
    database = open_swarm_database(tmp_path)
    yield database
    database.close()


@pytest_asyncio.fixture
async def store(swarm_database: Database) -> AsyncIterator[SwarmStore]:
    value = SwarmStore(swarm_database)
    await value.open()
    yield value
    await value.close()


@pytest_asyncio.fixture
async def board(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[SimpleNamespace]:
    """Register the Extension and bind three participants of one open Swarm.

    Indirect parametrization with ``False`` creates participants whose Sessions do not
    offer swarm_inbox.
    """

    inbox = getattr(request, "param", True)
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    manager = ChatRunManager()
    identity = SimpleNamespace(name="swarm", epoch="registration")
    groups = TemporaryExecutionGroups(
        TemporaryAgentRegistry(sessions),
        None,
        lambda value: value is identity,
        identity,
        run_manager=manager,
    )
    declarations = ExtensionDeclarations()
    api = ExtensionAPI("swarm", declarations, config={}, logger=None)
    register(api)
    registry = ExtensionRegistry()
    registry._records.append(
        ExtensionRecord(
            "swarm", tmp_path, tmp_path / "extension.py", "loaded", declarations=declarations
        )
    )
    tools = ToolRegistry()
    registry.apply_tools(tools)
    databases = ExtensionDatabases(tmp_path)
    host = ExtensionHost(
        data_dir=tmp_path,
        sample=None,  # type: ignore[arg-type]
        resolve_agent=None,  # type: ignore[arg-type]
        resolve_tool_agent=lambda context: None,
        store_attachment=None,  # type: ignore[arg-type]
        resolve_credential=None,  # type: ignore[arg-type]
        set_credential=None,  # type: ignore[arg-type]
        state_dir=tmp_path,
        temporary_agents=groups,
        open_database=databases.opener(ExtensionRegistrationIdentity("swarm", "registration")),
    )
    await api.operations.startup[0](host)
    service = cast(Any, declarations.tools[0].handler).__self__
    store = service.store
    profile = await store.save_profile(
        {
            "schema_version": 1,
            "slug": "fixture",
            "name": "Fixture",
            "participants": [{"model": "fixture/model", "count": 3}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
        },
        expected_revision=None,
    )
    swarm = await store.create_swarm(
        profile["id"],
        "fixture-goal",
        {"cwd": str(tmp_path)},
        request_id="start",
        expected_profile_revision=1,
    )
    swarm = await store.get_swarm(swarm["swarm_id"])
    denied = () if inbox else ("swarm_inbox",)
    bindings = []
    for peer in swarm["participants"]:
        binding = await groups.create(
            swarm["id"],
            peer["id"],
            TemporaryAgentConfig(
                model="fixture/model",
                cwd=tmp_path,
                tool_access=ToolAccess(mode="selected", allowed=(), denied=denied),
                allowed_skills=[],
                tools={},
                name=peer["display_name"],
            ),
        )
        await store.bind_participant_session(binding)
        bindings.append(binding)
    handle = await groups.open_group(swarm["id"])
    await store.bind_execution_epoch(swarm["id"], expected_epoch=0, execution_epoch=handle.epoch)
    contexts = [
        ToolContext(
            agent_id=binding.address.agent_id,
            session_id=binding.address.session_id,
            project_id=binding.address.project_id,
            run_id=f"run-{index}",
            tool_call_id=f"call-{index}",
            tool_name="swarm_board",
            tool_call_index=0,
            workspace=tmp_path,
            vbot_root=tmp_path,
            data_root=tmp_path,
            execution_owner=RunExecutionOwner(
                extension="swarm",
                group_id=swarm["id"],
                participant_id=binding.participant_id,
                epoch=handle.epoch,
                generation_id=binding.generation_id,
            ),
            delivery_receipt_hook=lambda *_: None,
        )
        for index, binding in enumerate(bindings)
    ]
    fixture = SimpleNamespace(
        service=service,
        runtime=declarations.session_runtime,
        operations=api.operations,
        store=store,
        swarm=swarm,
        contexts=contexts,
        bindings=bindings,
        sessions=sessions,
        tools=tools,
        registry=registry,
        groups=groups,
        databases=databases,
    )
    try:
        yield fixture
    finally:
        await service.close()
        databases.close()
        await manager.aclose()
        sessions.close()


@pytest_asyncio.fixture
async def lifecycle(tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    """Load the production Extension into a real ChatLoop whose Provider answers forty Runs."""

    # Tasks of earlier tests on this worker's shared Event Loop, which ``settled`` ignores.
    earlier_tasks = asyncio.all_tasks()
    responses = [{"content": "Run finished"} for index in range(40)] + [
        {"content": "completion recorded"} for _ in range(40)
    ]
    tools = ToolRegistry()
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="ordinary", model="fixture/model", allowed_tools=["*"]),
        adapter=StubAdapter(responses),
        tools=tools,
    )
    declarations = ExtensionDeclarations()
    api = ExtensionAPI(
        "swarm", declarations, config={}, logger=logging.getLogger("vbot.test.swarm")
    )
    register(api)
    extensions = ExtensionRegistry()
    extensions._records.append(  # noqa: SLF001 - production declaration fixture
        ExtensionRecord(
            "swarm", tmp_path, tmp_path / "extension.py", "loaded", declarations=declarations
        )
    )
    extensions.apply_tools(tools)
    runtime.extensions = extensions
    temporary = TemporaryAgentRegistry(runtime.chat_sessions)
    runtime.agent_resolver.temporary_agents = temporary
    identity = extensions.registration_identity("swarm")
    titled: list[tuple[str, str]] = []

    async def title(group_id: str, source_text: str) -> str:
        # Stands in for the shared title generator without a Model request.
        titled.append((group_id, source_text))
        await runtime.chat_sessions.set_temporary_group_title_async(
            owner_name="swarm", group_id=group_id, title="Generated Run title"
        )
        return "Generated Run title"

    groups = TemporaryExecutionGroups(
        temporary,
        build_chat_loop(runtime),
        extensions.is_registration_current,
        identity,
        run_manager=runtime.chat_run_manager,
        title=title,
    )

    async def catalog() -> dict[str, Any]:
        return {"models": [{"id": "fixture/model"}], "tools": [], "skills": []}

    databases = ExtensionDatabases(tmp_path)
    host = ExtensionHost(
        data_dir=tmp_path,
        sample=None,  # type: ignore[arg-type]
        resolve_agent=None,  # type: ignore[arg-type]
        resolve_tool_agent=lambda context: None,
        store_attachment=None,  # type: ignore[arg-type]
        resolve_credential=None,  # type: ignore[arg-type]
        set_credential=None,  # type: ignore[arg-type]
        state_dir=tmp_path,
        temporary_agents=groups,
        catalog=catalog,
        open_database=databases.opener(identity),
    )
    await api.operations.startup[0](host)
    service = cast(Any, declarations.tools[0].handler).__self__
    try:
        yield SimpleNamespace(
            service=service,
            runtime=runtime,
            groups=groups,
            tools=tools,
            titled=titled,
            workspace=tmp_path,
            earlier_tasks=earlier_tasks,
        )
    finally:
        await service.close()
        databases.close()
        await runtime.chat_run_manager.aclose()
        runtime.chat_sessions.close()
