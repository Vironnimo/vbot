"""Tests for runtime extension host."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.agents.temporary import (
    TemporaryAgentConfig,
    TemporaryAgentRegistry,
    TemporaryExecutionGroups,
)
from core.extensions import ExtensionRegistrationIdentity
from core.runs import ChatRunManager
from core.runtime import runtime as runtime_module
from core.runtime.runtime import Runtime
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.format import write_bootstrap_marker
from core.tools.availability import ToolAccess
from core.utils.config import Config
from tests.core.runtime.runtime_extensions_test_support import (
    _clean_extension_modules as _clean_extension_modules,
)


class _CatalogRegistry:
    def __init__(self, identity: ExtensionRegistrationIdentity) -> None:
        self.identity = identity
        self.current = True

    def is_registration_current(self, identity: object) -> bool:
        return self.current and identity == self.identity

    def fire_shutdown_blocking(self) -> None:
        pass


class _CatalogProjects:
    def __init__(self, registry: _CatalogRegistry, *, expire_during_list: bool = False) -> None:
        self._registry = registry
        self._expire_during_list = expire_during_list

    def list(self) -> list[SimpleNamespace]:
        if self._expire_during_list:
            self._registry.current = False
        return [
            SimpleNamespace(
                project_id="project-a",
                display_name="Project A",
                cwd="C:/work/project-a",
                allowed_tools=["read"],
                skills_bundled_enabled=[],
                skills_global_enabled=["global-skill"],
                skills_project_disabled=[],
            )
        ]


def _catalog_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, expire_during_list: bool = False
) -> tuple[Runtime, ExtensionRegistrationIdentity]:
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    identity = ExtensionRegistrationIdentity("owned", "epoch-1")
    registry = _CatalogRegistry(identity)
    monkeypatch.setattr(runtime, "_extensions", registry)
    monkeypatch.setattr(
        runtime,
        "_projects",
        _CatalogProjects(registry, expire_during_list=expire_during_list),
    )
    monkeypatch.setattr(
        runtime,
        "_skills",
        SimpleNamespace(
            list_all=lambda: [SimpleNamespace(name="global-skill", description="global sentinel")]
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_tools",
        SimpleNamespace(
            list_tools=lambda: [
                SimpleNamespace(
                    name="read",
                    family="files",
                    family_label="Files",
                    activation="configurable",
                    activation_source=None,
                    description="read sentinel",
                    parameters={"type": "object"},
                    constraints=(),
                    requires_opt_in=False,
                    catalog_visible=True,
                    session_scoped=False,
                ),
                SimpleNamespace(
                    name="hidden-session",
                    description="hidden sentinel",
                    parameters={"type": "object"},
                    constraints=(),
                    requires_opt_in=False,
                    catalog_visible=False,
                    session_scoped=True,
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_models",
        SimpleNamespace(
            query=lambda _query: [
                (
                    "provider",
                    SimpleNamespace(
                        model_id="model-a",
                        context_window=128000,
                        capabilities=SimpleNamespace(
                            tools=True,
                            reasoning=SimpleNamespace(
                                supported=True,
                                control="levels",
                                levels=("low", "high"),
                                budget_max=None,
                            ),
                        ),
                        name="Model A",
                        connections=("usable", "unavailable"),
                    ),
                )
            ]
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_provider_credentials",
        SimpleNamespace(
            is_usable=lambda provider_id, connection_id: connection_id == "provider:usable"
        ),
    )
    monkeypatch.setattr(
        runtime,
        "project_skill_names",
        lambda project_id: (
            frozenset({"project-skill"}) if project_id == "project-a" else frozenset()
        ),
    )
    return runtime, identity


def test_extension_prompt_inspection_uses_selected_blocks_and_owner_tools(tmp_path, monkeypatch):
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    try:
        identity = runtime.extensions.registration_identity("swarm")
        assert identity is not None
        config = TemporaryAgentConfig(
            model="fixture/model",
            cwd=tmp_path,
            name="Preview",
            tool_access=ToolAccess(mode="selected", allowed=()),
            allowed_skills=[],
            tools={},
            instructions="preview-body-sentinel",
            prompt_blocks=["core:agent_body"],
        )

        def no_session(*_args, **_kwargs):
            raise AssertionError("Preview must not create a Session")

        monkeypatch.setattr(runtime.chat_sessions, "create_bound_temporary_session", no_session)
        preview = asyncio.run(
            runtime._host_operations()._inspect_extension_prompt(identity, config, None)
        )
        assert preview["text"] == "preview-body-sentinel"
        assert {tool["name"] for tool in preview["tools"]} == {
            "swarm_board",
            "swarm_inbox",
            "swarm_state",
        }
        blocks = {block["id"]: block for block in preview["blocks"]}
        assert blocks["core:runtime"]["enabled"] is False
        assert blocks["core:runtime"]["text"]
        assert blocks["core:agent_body"]["included"] is True
        assert not any(key.startswith("extension_session:") for key in blocks)
    finally:
        runtime.stop()


def test_extension_catalog_projects_tools_settings_and_models_are_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, identity = _catalog_runtime(tmp_path, monkeypatch)
    try:
        catalog = asyncio.run(runtime._host_operations()._extension_catalog(identity))
    finally:
        runtime.stop()

    assert catalog["projects"] == [
        {
            "id": "project-a",
            "name": "Project A",
            "cwd": "C:/work/project-a",
            "allowed_tools": ["read"],
            "allowed_skills": ["global-skill", "project-skill"],
        }
    ]
    assert catalog["skills"] == [{"name": "global-skill", "description": "global sentinel"}]
    assert "core:runtime" in {block["id"] for block in catalog["prompt_blocks"]}
    assert [tool["name"] for tool in catalog["tools"]] == ["read"]
    assert catalog["tools"][0]["family"] == "files"
    assert catalog["tools"][0]["activation"] == "configurable"
    assert catalog["tool_settings"] == {
        "bash": {
            "type": "object",
            "properties": {
                "allowed_env": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                }
            },
            "additionalProperties": False,
        },
        "subagent": {
            "type": "object",
            "properties": {
                "allowed_agents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                }
            },
            "additionalProperties": False,
        },
    }
    assert catalog["models"] == [
        {
            "id": "provider/model-a",
            "name": "Model A",
            "connections": ["usable"],
            "context_window": 128000,
            "capabilities": {
                "tools": True,
                "reasoning": {
                    "supported": True,
                    "control": "levels",
                    "levels": ["low", "high"],
                    "budget_max": None,
                },
            },
        }
    ]


def test_extension_catalog_rejects_a_registration_retired_during_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, identity = _catalog_runtime(tmp_path, monkeypatch, expire_during_list=True)
    try:
        with pytest.raises(ValueError, match="no longer current"):
            asyncio.run(runtime._host_operations()._extension_catalog(identity))
    finally:
        runtime.stop()


@pytest.mark.parametrize(
    ("model", "cwd_name"),
    [("missing/model", "valid"), ("provider/model", "missing")],
)
def test_temporary_preflight_rejects_invalid_model_or_cwd_before_group_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    cwd_name: str,
) -> None:
    runtime = Runtime(Config(data_dir=tmp_path / "runtime"))
    runtime.start()
    host = runtime._host_operations()
    identity = ExtensionRegistrationIdentity("fixture", "epoch-1")
    monkeypatch.setattr(
        runtime,
        "_extensions",
        SimpleNamespace(
            is_registration_current=lambda candidate: candidate == identity,
            session_capability=lambda _binding, _tools: SimpleNamespace(
                tool_names=(), identity=identity
            ),
        ),
    )
    monkeypatch.setattr(host, "tools", SimpleNamespace(list_tools=lambda **_kwargs: []))

    def require_model_configured(candidate: str) -> None:
        if candidate == "missing/model":
            raise ValueError("model sentinel")

    monkeypatch.setattr(
        host,
        "agent_resolver",
        SimpleNamespace(require_model_configured=require_model_configured),
    )

    session_data_dir = tmp_path / "session-data"
    session_data_dir.mkdir()
    write_bootstrap_marker(session_data_dir)
    sessions = ChatSessionManager(session_data_dir)
    try:
        registry = TemporaryAgentRegistry(sessions)
        cwd = tmp_path / cwd_name
        if cwd_name == "valid":
            cwd.mkdir()
        registry.create(
            owner_name="fixture",
            group_id="group",
            participant_id="participant",
            config=TemporaryAgentConfig(
                model=model,
                cwd=cwd,
                tool_access=ToolAccess(mode="selected", allowed=()),
                allowed_skills=[],
                tools={},
                name="fixture",
            ),
        )
        groups = TemporaryExecutionGroups(
            registry,
            SimpleNamespace(),
            lambda candidate: candidate == identity,
            identity,
            run_manager=ChatRunManager(),
            validate_binding=host._validate_extension_session_binding,
        )

        with pytest.raises((RuntimeError, ValueError)):
            asyncio.run(groups.open_group("group"))

        assert groups._groups["group"].open is False
    finally:
        sessions.close()
        monkeypatch.undo()
        runtime.stop()


def test_temporary_preflight_rechecks_owner_after_blocking_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(Config(data_dir=tmp_path / "runtime"))
    monkeypatch.setattr(runtime, "_started", True)
    identity = ExtensionRegistrationIdentity("fixture", "epoch-1")
    registry = _CatalogRegistry(identity)
    monkeypatch.setattr(
        runtime,
        "_extensions",
        SimpleNamespace(
            is_registration_current=registry.is_registration_current,
            session_capability=lambda _binding, _tools: SimpleNamespace(
                tool_names=(), identity=identity
            ),
        ),
    )
    monkeypatch.setattr(runtime, "_tools", SimpleNamespace(list_tools=lambda **_kwargs: []))
    monkeypatch.setattr(
        runtime,
        "_agent_resolver",
        SimpleNamespace(
            require_model_configured=lambda _model: None,
            resolve_temporary_agent=lambda _address, **_kwargs: SimpleNamespace(
                tool_access=ToolAccess(mode="selected", allowed=()),
                memory_prompt_mode="off",
                workspace="",
            ),
        ),
    )
    binding = SimpleNamespace(
        address=SessionAddress(None, "temporary", "session"),
        generation_id="generation",
        owner_name="fixture",
        group_id="group",
        participant_id="participant",
        config={
            "model": "provider/model",
            "cwd": str(tmp_path),
            "tool_access": {"mode": "selected", "allowed": []},
            "allowed_skills": [],
            "tools": {},
            "name": "fixture",
        },
    )

    async def expire_after_validation(function, *arguments):
        function(*arguments)
        registry.current = False

    monkeypatch.setattr(runtime_module._RUNTIME_WORKERS, "run", expire_after_validation)

    with pytest.raises(RuntimeError, match="temporary execution is unavailable"):
        asyncio.run(runtime._host_operations()._validate_extension_session_binding(binding))
