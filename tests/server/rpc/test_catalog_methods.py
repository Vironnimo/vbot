"""Tests for the catalog RPC handlers (project-aware ``chat.commands`` skills)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.projects.projects import PROJECT_DEFAULT_ALLOWED_TOOLS
from server.rpc.catalog_methods import _list_commands, _list_tools
from server.rpc.errors import RpcError


class _Skill:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} description"


class _Registry:
    """Minimal skill registry: wildcard exposes all, else exact-name matches."""

    def __init__(self, names: list[str]) -> None:
        self._skills = {name: _Skill(name) for name in names}

    def filter_allowed(self, allowed_skills: list[str]) -> list[_Skill]:
        if "*" in allowed_skills:
            return list(self._skills.values())
        return [skill for name, skill in self._skills.items() if name in allowed_skills]

    def list_all(self) -> list[_Skill]:
        return list(self._skills.values())


def _state(
    *,
    global_names: list[str],
    project_names: list[str] | None = None,
    agent_allowed: list[str] | None = None,
    resolvable: bool = True,
) -> Any:
    global_registry = _Registry(global_names)
    project_registry = _Registry(project_names or [])
    agent = SimpleNamespace(allowed_skills=agent_allowed if agent_allowed is not None else ["*"])

    def resolve_agent(project_id: str | None, agent_id: str) -> object:
        if not resolvable:
            from core.projects import AgentResolutionError

            raise AgentResolutionError(f"agent '{agent_id}' not found")
        return agent

    def skills_for(project_id: str | None, agent_id: str | None = None) -> _Registry:
        return project_registry if project_id is not None else global_registry

    runtime = SimpleNamespace(
        skills=global_registry,
        skills_for=skills_for,
        agent_resolver=SimpleNamespace(resolve_agent=resolve_agent),
    )
    return SimpleNamespace(runtime=runtime)


def _skill_names(result: dict[str, Any]) -> list[str]:
    return [item["name"] for item in result["items"] if item["type"] == "skill"]


def test_no_agent_address_returns_global_skills() -> None:
    state = _state(global_names=["debugging", "frontend-design"])

    result = _list_commands(state, {})

    assert _skill_names(result) == ["debugging", "frontend-design"]


def test_identity_agent_address_filters_by_agent_allowed_skills() -> None:
    # A bare id resolves the identity agent against the global registry, narrowed by
    # the agent's own allowed_skills.
    state = _state(global_names=["debugging", "frontend-design"], agent_allowed=["debugging"])

    result = _list_commands(state, {"agent_id": "main"})

    assert _skill_names(result) == ["debugging"]


def test_project_agent_address_uses_project_registry() -> None:
    # An ``agent@projekt`` address resolves against the project's own registry, so
    # the suggestions are the project skills (not the global pool).
    state = _state(
        global_names=["bundled-only"],
        project_names=["proj-a", "proj-b"],
        agent_allowed=["*"],
    )

    result = _list_commands(state, {"agent_id": "builder@vbot"})

    assert _skill_names(result) == ["proj-a", "proj-b"]


def test_commands_are_always_present() -> None:
    state = _state(global_names=[])

    result = _list_commands(state, {})

    command_names = [item["name"] for item in result["items"] if item["type"] == "command"]
    assert command_names == [
        "agent",
        "compact",
        "handoff",
        "help",
        "model",
        "new",
        "rename",
        "retry",
        "status",
        "stop",
    ]


def test_unsupported_field_is_rejected() -> None:
    state = _state(global_names=[])

    with pytest.raises(RpcError):
        _list_commands(state, {"session_id": "s1"})


def test_empty_agent_id_is_rejected() -> None:
    state = _state(global_names=[])

    with pytest.raises(RpcError):
        _list_commands(state, {"agent_id": ""})


def test_unresolvable_agent_maps_to_rpc_error() -> None:
    state = _state(global_names=["debugging"], resolvable=False)

    with pytest.raises(RpcError):
        _list_commands(state, {"agent_id": "ghost@vbot"})


class _ToolRegistry:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_tools(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return [
            SimpleNamespace(name=name, description=f"{name} description") for name in self._names
        ]


def test_tool_list_exposes_default_project_tools() -> None:
    runtime = SimpleNamespace(tools=_ToolRegistry(["read", "edit"]))
    state = SimpleNamespace(runtime=runtime)

    result = _list_tools(state, {})

    assert [tool["name"] for tool in result["tools"]] == ["read", "edit"]
    # The base project Tool Whitelist rides along as the editor's reset target.
    assert result["default_project_tools"] == list(PROJECT_DEFAULT_ALLOWED_TOOLS)
