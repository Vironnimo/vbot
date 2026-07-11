"""Tests for the catalog RPC handlers (project-aware ``chat.commands`` skills)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.projects.projects import PROJECT_DEFAULT_ALLOWED_TOOLS
from server.rpc.catalog_methods import _list_commands, _list_files, _list_tools
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
    agent_workspace: str = "",
    rooted_project_id: str | None = None,
) -> Any:
    global_registry = _Registry(global_names)
    project_registry = _Registry(project_names or [])
    agent = SimpleNamespace(
        allowed_skills=agent_allowed if agent_allowed is not None else ["*"],
        workspace=agent_workspace,
    )

    def resolve_agent(project_id: str | None, agent_id: str) -> object:
        if not resolvable:
            from core.projects import AgentResolutionError

            raise AgentResolutionError(f"agent '{agent_id}' not found")
        return agent

    def skills_for(project_id: str | None, agent_id: str | None = None) -> _Registry:
        return project_registry if project_id is not None else global_registry

    # ``resolve_prompt_project`` reaches through this store: ``get`` for a project
    # address, ``find_by_cwd`` for the rooted-identity lookup.
    rooted = (
        SimpleNamespace(project_id=rooted_project_id) if rooted_project_id is not None else None
    )
    projects = SimpleNamespace(
        get=lambda project_id: SimpleNamespace(project_id=project_id),
        find_by_cwd=lambda _cwd: rooted,
    )
    runtime = SimpleNamespace(
        skills=global_registry,
        skills_for=skills_for,
        agent_resolver=SimpleNamespace(resolve_agent=resolve_agent),
        projects=projects,
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


def test_rooted_identity_agent_suggests_home_project_skills() -> None:
    # A rooted identity agent (workspace == a registered repo, bare address) must
    # autocomplete against its home project's pool — the same scope a run resolves —
    # not the bare global registry.
    state = _state(
        global_names=["bundled-only"],
        project_names=["home-skill"],
        agent_allowed=["*"],
        agent_workspace="/srv/repo",
        rooted_project_id="vbot",
    )

    result = _list_commands(state, {"agent_id": "main"})

    assert _skill_names(result) == ["home-skill"]


def test_commands_are_always_present() -> None:
    state = _state(global_names=[])

    result = _list_commands(state, {})

    command_names = [item["name"] for item in result["items"] if item["type"] == "command"]
    assert command_names == [
        "agent",
        "compact",
        "continue",
        "handoff",
        "help",
        "learn",
        "model",
        "new",
        "reflect",
        "rename",
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


def _tool_stub(
    name: str,
    *,
    ready: Any = None,
    readiness_hint: str | None = None,
    extension: str | None = None,
) -> SimpleNamespace:
    """A tool stub exposing the fields ``_tool_response`` reads.

    ``ready`` is a zero-arg predicate (``lambda: bool``) or ``None`` (always ready),
    mirroring ``Tool.ready`` — the response calls ``tool_is_ready``, which invokes it.
    """
    return SimpleNamespace(
        name=name,
        description=f"{name} description",
        ready=ready,
        readiness_hint=readiness_hint,
        extension=extension,
    )


class _ToolRegistry:
    """Minimal registry: ``list_tools`` returns ALL tools (readiness not filtered).

    ``tool.list`` no longer passes ``ready_only`` — every registered tool is
    returned and a not-ready one is styled from its ``ready``/``readiness_hint``
    fields rather than hidden. The stub still accepts (and ignores) any kwargs so a
    stray ``ready_only`` would surface as an unexpected pass rather than silently
    filtering.
    """

    def __init__(self, tools: list[SimpleNamespace]) -> None:
        self._tools = tools

    def list_tools(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return list(self._tools)


def test_tool_list_exposes_default_project_tools() -> None:
    runtime = SimpleNamespace(tools=_ToolRegistry([_tool_stub("read"), _tool_stub("edit")]))
    state = SimpleNamespace(runtime=runtime)

    result = _list_tools(state, {})

    assert [tool["name"] for tool in result["tools"]] == ["read", "edit"]
    # The base project Tool Whitelist rides along as the editor's reset target.
    assert result["default_project_tools"] == list(PROJECT_DEFAULT_ALLOWED_TOOLS)


def test_tool_list_returns_not_ready_tools_with_ready_false() -> None:
    # tool.list now RETURNS the not-ready tool (no more hiding) — the picker styles
    # it from ``ready: false`` while the ready tools report ``ready: true``.
    runtime = SimpleNamespace(
        tools=_ToolRegistry(
            [
                _tool_stub("read"),
                _tool_stub("edit"),
                _tool_stub("ha_call_service", ready=lambda: False),
            ]
        )
    )
    state = SimpleNamespace(runtime=runtime)

    result = _list_tools(state, {})

    ready_by_name = {tool["name"]: tool["ready"] for tool in result["tools"]}
    # The not-ready tool IS present, flagged ready == False; the ready ones True.
    assert ready_by_name == {"read": True, "edit": True, "ha_call_service": False}
    assert result["default_project_tools"] == list(PROJECT_DEFAULT_ALLOWED_TOOLS)


def test_tool_list_surfaces_ready_hint_and_extension_fields() -> None:
    # A tool with a readiness hint + owning extension surfaces both; a default tool
    # reports null for each. A RAISING ready predicate is treated as ready == False.
    runtime = SimpleNamespace(
        tools=_ToolRegistry(
            [
                _tool_stub(
                    "ha_call_service",
                    ready=lambda: False,
                    readiness_hint="hint",
                    extension="homeassistant",
                ),
                _tool_stub("read"),
                _tool_stub("boom", ready=_raise_ready),
            ]
        )
    )
    state = SimpleNamespace(runtime=runtime)

    result = _list_tools(state, {})

    by_name = {tool["name"]: tool for tool in result["tools"]}
    assert by_name["ha_call_service"]["ready"] is False
    assert by_name["ha_call_service"]["readiness_hint"] == "hint"
    assert by_name["ha_call_service"]["extension"] == "homeassistant"
    assert by_name["read"]["ready"] is True
    assert by_name["read"]["readiness_hint"] is None
    assert by_name["read"]["extension"] is None
    # A predicate that raises counts as not-ready, never crashing the feed.
    assert by_name["boom"]["ready"] is False


def _raise_ready() -> bool:
    raise RuntimeError("readiness probe blew up")


# ---------------------------------------------------------------------------
# files.list — cwd file candidates for the composer's @-mention picker.
# ---------------------------------------------------------------------------


def _files_state(*, project_cwd: str, workspace: str, data_dir: str) -> Any:
    runtime = SimpleNamespace(
        projects=SimpleNamespace(get=lambda project_id: SimpleNamespace(cwd=project_cwd)),
        agent_resolver=SimpleNamespace(
            resolve_agent=lambda project_id, agent_id: SimpleNamespace(workspace=workspace)
        ),
        storage=SimpleNamespace(data_dir=data_dir),
    )
    return SimpleNamespace(runtime=runtime)


@pytest.mark.asyncio
async def test_files_list_returns_project_repo_files(tmp_path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("x", encoding="utf-8")
    state = _files_state(
        project_cwd=str(repo), workspace=str(tmp_path / "ws"), data_dir=str(tmp_path)
    )

    result = await _list_files(state, {"agent_id": "builder@vbot"})

    assert result["files"] == ["src/app.py"]
    assert result["truncated"] is False
    assert result["root"] == str(repo)


@pytest.mark.asyncio
async def test_files_list_identity_address_lists_workspace(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("x", encoding="utf-8")
    state = _files_state(
        project_cwd=str(tmp_path / "repo"), workspace=str(workspace), data_dir=str(tmp_path)
    )

    result = await _list_files(state, {"agent_id": "main"})

    assert result["files"] == ["MEMORY.md"]


@pytest.mark.asyncio
async def test_files_list_rejects_unknown_params(tmp_path) -> None:
    state = _files_state(project_cwd=str(tmp_path), workspace=str(tmp_path), data_dir=str(tmp_path))

    with pytest.raises(RpcError):
        await _list_files(state, {"agent_id": "main", "limit": 5})
