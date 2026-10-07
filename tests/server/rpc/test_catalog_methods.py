"""Tests for the catalog RPC handlers (project-aware ``chat.commands`` skills)."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.chat import CommandDispatcher, CommandOutcome
from core.projects import AgentResolver, build_project
from core.projects.projects import PROJECT_DEFAULT_ALLOWED_TOOLS
from core.runs import ChatRunManager
from core.sessions import SessionAddress, SessionNotFoundError
from core.tools import ToolRegistry, tool_success
from server.rpc.catalog_methods import _list_commands, _list_files, _list_tools
from server.rpc.errors import RpcError
from tests.server.rpc.chat_methods_test_support import _InlineSessionPool
from tests.server.rpc_test_support import StubAdapter, make_state, rpc_result


class _Skill:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} description"


class _Registry:
    """Minimal skill registry: wildcard exposes all, else exact-name matches."""

    def __init__(self, names: list[str]) -> None:
        self._skills = {name: _Skill(name) for name in names}
        self.filter_threads: list[threading.Thread] = []

    def filter_allowed(self, allowed_skills: list[str]) -> list[_Skill]:
        # Real availability checks probe PATH for binary requirements.
        self.filter_threads.append(threading.current_thread())
        if "*" in allowed_skills:
            return list(self._skills.values())
        return [skill for name, skill in self._skills.items() if name in allowed_skills]

    def list_all(self) -> list[_Skill]:
        return list(self._skills.values())


class _SessionProjects:
    """Session store double: the working Project each existing Session stores."""

    def __init__(self, projects: dict[str, str | None]) -> None:
        self._projects = projects

    def metadata_value(self, address: SessionAddress, key: str) -> str | None:
        assert key == "working_project_id"
        if address.session_id not in self._projects:
            raise SessionNotFoundError(f"session does not exist: {address.session_id}")
        return address.project_id or self._projects[address.session_id]

    async def metadata_value_async(self, address: SessionAddress, key: str) -> str | None:
        return self.metadata_value(address, key)


def _working_projects(
    projects: Any, session_projects: dict[str, str | None] | None = None
) -> AgentResolver:
    """The real resolver's working-Project policy over *projects* and these Sessions."""
    return AgentResolver(
        cast(Any, None),
        projects,
        cast(Any, None),
        dict,
        sessions=cast(Any, _SessionProjects(session_projects or {})),
    )


def _state(
    *,
    global_names: list[str],
    project_names: list[str] | None = None,
    agent_allowed: list[str] | None = None,
    resolvable: bool = True,
    agent_workspace: str = "",
    rooted_project_id: str | None = None,
    project_cwd: str | None = None,
    command_dispatcher: CommandDispatcher | None = None,
    agent_skills: dict[str, list[str]] | None = None,
    session_subjects: dict[str, str] | None = None,
    session_projects: dict[str, str | None] | None = None,
    existing_projects: frozenset[str] = frozenset({"vbot"}),
) -> Any:
    """``agent_skills`` gives Agents their own registries; ``session_subjects`` binds
    a Session to the Agent whose Skills it works on (a Librarian Session);
    ``session_projects`` names the Project each existing Session works in."""
    global_registry = _Registry(global_names)
    project_registry = _Registry(project_names or [])
    agent_registries = {
        agent_id: _Registry(names) for agent_id, names in (agent_skills or {}).items()
    }

    async def resolve_agent_async(
        project_id: str | None, agent_id: str, *, session_id: str | None = None
    ) -> object:
        if not resolvable:
            from core.projects import AgentResolutionError

            raise AgentResolutionError(f"agent '{agent_id}' not found")
        return SimpleNamespace(
            id=agent_id,
            allowed_skills=agent_allowed if agent_allowed is not None else ["*"],
            workspace=agent_workspace,
            root_project_id=rooted_project_id,
            skill_agent_id=(session_subjects or {}).get(session_id or ""),
        )

    def skills_for(project_id: str | None, agent_id: str | None = None) -> _Registry:
        if project_id is not None:
            return project_registry
        return agent_registries.get(agent_id or "", global_registry)

    projects = SimpleNamespace(
        get=lambda project_id: build_project(
            project_id, project_id, project_cwd or str(Path.cwd()), sources=[]
        ),
        exists=existing_projects.__contains__,
    )
    working_projects = _working_projects(projects, session_projects)
    runtime = SimpleNamespace(
        skills=global_registry,
        skills_for=skills_for,
        agent_resolver=SimpleNamespace(
            resolve_agent_async=resolve_agent_async,
            resolve_working_project_async=working_projects.resolve_working_project_async,
        ),
        projects=projects,
    )
    return SimpleNamespace(
        runtime=runtime,
        command_dispatcher=command_dispatcher or CommandDispatcher(ChatRunManager()),
    )


def _skill_names(result: dict[str, Any]) -> list[str]:
    return [item["name"] for item in result["items"] if item["type"] == "skill"]


@pytest.mark.asyncio
async def test_no_agent_address_returns_global_skills_sorted_by_name() -> None:
    state = _state(global_names=["frontend-design", "debugging"])

    result = await _list_commands(state, {})

    assert _skill_names(result) == ["debugging", "frontend-design"]


@pytest.mark.asyncio
async def test_identity_agent_address_filters_by_agent_allowed_skills() -> None:
    # A bare id resolves the identity agent against the global registry, narrowed by
    # the agent's own allowed_skills.
    state = _state(global_names=["debugging", "frontend-design"], agent_allowed=["debugging"])

    result = await _list_commands(state, {"agent_id": "main"})

    assert _skill_names(result) == ["debugging"]


@pytest.mark.asyncio
async def test_a_librarian_session_suggests_the_skills_it_maintains() -> None:
    state = _state(
        global_names=["debugging"],
        agent_skills={"coder": ["release"]},
        session_subjects={"curating": "coder"},
    )

    in_session = await _list_commands(state, {"agent_id": "librarian", "session_id": "curating"})
    elsewhere = await _list_commands(state, {"agent_id": "librarian"})

    assert (_skill_names(in_session), _skill_names(elsewhere)) == (["release"], ["debugging"])
    # A Session belongs to an Agent.
    with pytest.raises(RpcError, match="need params.agent_id"):
        await _list_commands(state, {"session_id": "curating"})


@pytest.mark.asyncio
async def test_project_agent_address_uses_project_registry() -> None:
    # An ``agent@projekt`` address resolves against the project's own registry, so
    # the suggestions are the project skills (not the global pool).
    state = _state(
        global_names=["bundled-only"],
        project_names=["proj-a", "proj-b"],
        agent_allowed=["*"],
    )

    result = await _list_commands(state, {"agent_id": "builder@vbot"})

    assert _skill_names(result) == ["proj-a", "proj-b"]


@pytest.mark.parametrize(
    ("default_project", "params", "expected"),
    [
        pytest.param("vbot", {}, ["project-skill"], id="draft-in-the-default-project"),
        pytest.param(
            "vbot", {"working_project_id": None}, ["global-skill"], id="draft-in-the-workspace"
        ),
        pytest.param(
            None, {"working_project_id": "vbot"}, ["project-skill"], id="draft-in-a-project"
        ),
        pytest.param(None, {"session_id": "in-vbot"}, ["project-skill"], id="session-in-a-project"),
        pytest.param(
            "vbot", {"session_id": "in-workspace"}, ["global-skill"], id="session-in-workspace"
        ),
        pytest.param("vbot", {"session_id": "in-removed"}, [], id="session-project-removed"),
    ],
)
@pytest.mark.asyncio
async def test_identity_agent_suggests_the_skills_of_its_working_project(
    default_project: str | None, params: dict[str, Any], expected: list[str]
) -> None:
    # An Identity Agent autocompletes against the pool of the Project its Session
    # works in; a draft against the Project its new Session would work in. A Session
    # whose Project no longer exists cannot run, so it is offered no Skills.
    state = _state(
        global_names=["global-skill"],
        project_names=["project-skill"],
        rooted_project_id=default_project,
        session_projects={"in-vbot": "vbot", "in-workspace": None, "in-removed": "gone"},
    )

    result = await _list_commands(state, {"agent_id": "main", **params})

    assert _skill_names(result) == expected


@pytest.mark.parametrize(
    ("params", "message"),
    [
        pytest.param(
            {"agent_id": "main", "session_id": "s1", "working_project_id": "vbot"},
            "exclude each other",
            id="session-and-draft-project",
        ),
        pytest.param(
            {"agent_id": "builder@vbot", "working_project_id": "other"},
            "not accepted for a Team Agent",
            id="team-agent",
        ),
        pytest.param(
            {"agent_id": "main", "working_project_id": "Not A Project!"},
            "must be null or a valid Project id",
            id="invalid-project-id",
        ),
        pytest.param({"working_project_id": "vbot"}, "need params.agent_id", id="no-agent"),
    ],
)
@pytest.mark.asyncio
async def test_a_draft_working_project_is_validated(params: dict[str, Any], message: str) -> None:
    state = _state(global_names=[])

    with pytest.raises(RpcError, match=message) as exc_info:
        await _list_commands(state, params)
    assert exc_info.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_rooted_identity_missing_project_cwd_maps_error_without_global_fallback(
    tmp_path: Path,
) -> None:
    state = _state(
        global_names=["must-not-fallback"],
        project_names=["project-skill"],
        rooted_project_id="vbot",
        project_cwd=str(tmp_path / "missing-repo"),
    )

    with pytest.raises(RpcError) as exc_info:
        await _list_commands(state, {"agent_id": "main"})
    assert exc_info.value.code == "domain_error"


@pytest.mark.asyncio
async def test_skill_suggestions_are_computed_off_the_event_loop() -> None:
    state = _state(global_names=["debugging"], project_names=["proj-a"])

    await _list_commands(state, {})
    await _list_commands(state, {"agent_id": "builder@vbot"})

    loop_thread = threading.current_thread()
    registries = (state.runtime.skills, state.runtime.skills_for("vbot"))
    filter_threads = [thread for registry in registries for thread in registry.filter_threads]
    assert len(filter_threads) == 2
    assert loop_thread not in filter_threads


@pytest.mark.asyncio
async def test_built_in_commands_are_always_present_with_their_input_and_output() -> None:
    state = _state(global_names=[])

    result = await _list_commands(state, {})

    commands = [item for item in result["items"] if item["type"] == "command"]
    assert all(item["description"] for item in commands)
    assert [(item["name"], item["argument"], item["output"]) for item in commands] == [
        ("agent", "optional", "action"),
        ("compact", "optional", "toast"),
        ("handoff", "optional", "action"),
        ("help", "none", "transient"),
        ("learn", "optional", "action"),
        ("model", "optional", "action"),
        ("new", "none", "action"),
        ("reflect", "optional", "action"),
        ("rename", "optional", "toast"),
        ("status", "none", "transient"),
        ("stop", "optional", "toast"),
    ]


@pytest.mark.asyncio
async def test_extension_commands_come_from_live_dispatcher_catalog() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())
    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Run the workflow.",
        handler=lambda _context, _argument: CommandOutcome(command="workflow"),
    )
    state = _state(global_names=[], command_dispatcher=dispatcher)

    result = await _list_commands(state, {})

    command_items = [item for item in result["items"] if item["type"] == "command"]
    assert command_items[-1] == {
        "name": "workflow",
        "description": "Run the workflow.",
        "type": "command",
        "argument": "optional",
        "output": "toast",
    }


@pytest.mark.asyncio
async def test_unsupported_field_is_rejected() -> None:
    state = _state(global_names=[])

    with pytest.raises(RpcError):
        await _list_commands(state, {"project_id": "p1"})


@pytest.mark.asyncio
async def test_empty_agent_id_is_rejected() -> None:
    state = _state(global_names=[])

    with pytest.raises(RpcError):
        await _list_commands(state, {"agent_id": ""})


@pytest.mark.asyncio
async def test_unresolvable_agent_maps_to_rpc_error() -> None:
    state = _state(global_names=["debugging"], resolvable=False)

    with pytest.raises(RpcError):
        await _list_commands(state, {"agent_id": "ghost@vbot"})


def _tool_stub(
    name: str,
    *,
    ready: Any = None,
    readiness_hint: str | None = None,
    extension: str | None = None,
    family: str | None = None,
    family_label: str | None = None,
    activation: str = "configurable",
    activation_source: str | None = None,
    constraints: tuple[str, ...] = (),
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
        family=family,
        family_label=family_label,
        activation=activation,
        activation_source=activation_source,
        constraints=constraints,
        session_scoped=activation == "session_grant",
        contract=SimpleNamespace(schema_fingerprint=f"fingerprint:{name}"),
        parallel_safe=False,
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


def test_tool_list_projects_server_owned_configurability_policy() -> None:
    runtime = SimpleNamespace(
        tools=_ToolRegistry(
            [
                _tool_stub("read"),
                _tool_stub("memory", activation="memory_mode"),
                _tool_stub("project", constraints=("identity_agent",)),
                _tool_stub("skill_manage", constraints=("identity_agent",)),
            ]
        )
    )
    state = SimpleNamespace(runtime=runtime)

    result = _list_tools(state, {})

    policy_by_name = {
        tool["name"]: (
            tool["project_configurable"],
            tool["project_configurability_reason"],
        )
        for tool in result["tools"]
    }
    assert policy_by_name == {
        "read": (True, None),
        "memory": (False, "activated_by_memory_mode"),
        "project": (False, "requires_identity_agent"),
        "skill_manage": (False, "requires_identity_agent"),
    }


def test_tool_list_includes_session_scoped_tools_with_activation_metadata() -> None:
    registry = ToolRegistry()
    registry.register(
        name="read",
        description="Read a file",
        parameters={"type": "object"},
        handler=lambda _context, _arguments: tool_success({}),
    )
    registry.register(
        name="inbox",
        description="Read this Session's inbox",
        parameters={"type": "object"},
        handler=lambda _context, _arguments: tool_success({}),
        session_scoped=True,
        activation="session_grant",
    )
    state = SimpleNamespace(runtime=SimpleNamespace(tools=registry))

    result = _list_tools(state, {})

    assert [tool["name"] for tool in result["tools"]] == ["inbox", "read"]
    inbox = result["tools"][0]
    assert inbox["session_scoped"] is True
    assert inbox["activation"] == "session_grant"
    assert inbox["project_configurable"] is False
    assert result["default_project_tools"] == list(PROJECT_DEFAULT_ALLOWED_TOOLS)


def test_tool_list_hides_tools_excluded_from_public_catalog() -> None:
    registry = ToolRegistry()
    registry.register(
        name="private_session_tool",
        description="Private Session capability",
        parameters={"type": "object"},
        handler=lambda _context, _arguments: tool_success({}),
        session_scoped=True,
        activation="session_grant",
        catalog_visible=False,
    )
    registry.register(
        name="read",
        description="Read a file",
        parameters={"type": "object"},
        handler=lambda _context, _arguments: tool_success({}),
    )

    result = _list_tools(SimpleNamespace(runtime=SimpleNamespace(tools=registry)), {})

    assert [tool["name"] for tool in result["tools"]] == ["read"]
    assert [tool.name for tool in registry.list_tools(include_internal=True)] == [
        "private_session_tool",
        "read",
    ]


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
                    family="extension:homeassistant:home_assistant",
                    family_label="Home Assistant",
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
    assert by_name["ha_call_service"]["family"] == "extension:homeassistant:home_assistant"
    assert by_name["ha_call_service"]["family_label"] == "Home Assistant"
    assert by_name["read"]["ready"] is True
    assert by_name["read"]["readiness_hint"] is None
    assert by_name["read"]["extension"] is None
    # A predicate that raises counts as not-ready, never crashing the feed.
    assert by_name["boom"]["ready"] is False


def _raise_ready() -> bool:
    raise RuntimeError("readiness probe blew up")


@pytest.mark.asyncio
async def test_tool_list_returns_public_tools_sorted_with_their_full_projection(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    registry = state.runtime.tools
    for name, description in (("z_tool", "Last tool"), ("a_tool", "First tool")):
        registry.register(
            name,
            description,
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda _context, _arguments: tool_success({}),
        )
    # The internal Skill loader is not a user-selectable Tool.
    registry.register(
        "skill",
        "Load skills",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda _context, _arguments: tool_success({}),
        internal=True,
    )

    result = await rpc_result(state, "tool.list")

    assert result == {
        "tools": [
            {
                "name": name,
                "description": description,
                "ready": True,
                "readiness_hint": None,
                "family": None,
                "family_label": None,
                "activation": "configurable",
                "requires_opt_in": False,
                "activation_source": None,
                "constraints": [],
                "session_scoped": False,
                "extension": None,
                "schema_fingerprint": registry.schema_fingerprint(name),
                "parallel_safe": True,
                "project_configurable": True,
                "project_configurability_reason": None,
            }
            for name, description in (("a_tool", "First tool"), ("z_tool", "Last tool"))
        ],
        "default_project_tools": list(PROJECT_DEFAULT_ALLOWED_TOOLS),
    }


@pytest.mark.asyncio
async def test_skill_list_returns_loadable_and_invalid_diagnostics(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    result = await rpc_result(state, "skill.list")

    assert result == {
        "skills": [
            {
                "name": "debugging",
                "description": "Debug failures.",
                "origin": None,
                "valid": True,
                "warnings": [],
                "state": "available",
                "requirements": {"missing": [], "optional_missing": []},
            },
            {
                "name": "warned",
                "description": "Loads with warnings.",
                "origin": None,
                "valid": False,
                "warnings": ["Name does not match directory."],
                "state": "available",
                "requirements": {"missing": [], "optional_missing": []},
            },
        ],
        "invalid_skills": [
            {
                "name": "broken",
                "path": str(Path("/skills/broken/SKILL.md")),
                "valid": False,
                "warnings": ["missing description"],
            }
        ],
    }


# ---------------------------------------------------------------------------
# files.list — cwd file candidates for the composer's @-mention picker.
# ---------------------------------------------------------------------------


def _files_state(
    *,
    project_cwd: str,
    workspace: str,
    data_dir: str,
    root_project_id: str | None = None,
    session_projects: dict[str, str | None] | None = None,
) -> Any:
    projects = SimpleNamespace(
        get=lambda project_id: SimpleNamespace(cwd=project_cwd), exists=lambda _id: True
    )
    working_projects = _working_projects(projects, session_projects)
    runtime = SimpleNamespace(
        projects=projects,
        agent_resolver=SimpleNamespace(
            resolve_agent=lambda project_id, agent_id: SimpleNamespace(
                id=agent_id,
                workspace=workspace,
                root_project_id=root_project_id,
            ),
            resolve_working_project=working_projects.resolve_working_project,
        ),
        storage=SimpleNamespace(data_dir=data_dir),
        chat_sessions=_InlineSessionPool(),
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

    assert result == {
        "root": str(repo),
        "files": ["src/app.py"],
        "directories": ["src"],
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_files_list_lists_one_directory_with_ignored_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "build").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    state = _files_state(
        project_cwd=str(repo), workspace=str(tmp_path / "ws"), data_dir=str(tmp_path)
    )

    result = await _list_files(state, {"agent_id": "builder@vbot", "directory": ""})

    assert result == {
        "root": str(repo),
        "files": [],
        "directories": [],
        "truncated": False,
        "entries": [
            {"name": "build", "kind": "directory", "ignored": True},
            {"name": "src", "kind": "directory", "ignored": False},
            {"name": ".gitignore", "kind": "file", "ignored": False},
        ],
    }


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


@pytest.mark.parametrize(
    ("default_project", "params", "expected"),
    [
        pytest.param("vbot", {}, ["project.txt"], id="draft-in-the-default-project"),
        pytest.param(
            "vbot", {"working_project_id": None}, ["private.txt"], id="draft-in-the-workspace"
        ),
        pytest.param(
            None, {"working_project_id": "vbot"}, ["project.txt"], id="draft-in-a-project"
        ),
        pytest.param(None, {"session_id": "in-vbot"}, ["project.txt"], id="session-in-a-project"),
        pytest.param(
            "vbot", {"session_id": "in-workspace"}, ["private.txt"], id="session-in-workspace"
        ),
    ],
)
@pytest.mark.asyncio
async def test_files_list_lists_the_working_project_of_an_identity_agent(
    tmp_path: Path, default_project: str | None, params: dict[str, Any], expected: list[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "project.txt").write_text("project", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "private.txt").write_text("private", encoding="utf-8")
    state = _files_state(
        project_cwd=str(repo),
        workspace=str(workspace),
        data_dir=str(tmp_path),
        root_project_id=default_project,
        session_projects={"in-vbot": "vbot", "in-workspace": None},
    )

    result = await _list_files(state, {"agent_id": "main", **params})

    assert result["files"] == expected


@pytest.mark.asyncio
async def test_files_list_missing_rooted_cwd_maps_error_without_workspace_fallback(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "must-not-list.txt").write_text("private", encoding="utf-8")
    state = _files_state(
        project_cwd=str(tmp_path / "missing-repo"),
        workspace=str(workspace),
        data_dir=str(tmp_path),
        root_project_id="vbot",
    )

    with pytest.raises(RpcError) as exc_info:
        await _list_files(state, {"agent_id": "main"})
    assert exc_info.value.code == "domain_error"


@pytest.mark.parametrize(
    ("params", "code", "data"),
    [
        pytest.param({"limit": 5}, "invalid_request", None, id="unknown-param"),
        pytest.param({"directory": 3}, "invalid_request", None, id="directory-not-a-string"),
        pytest.param({"directory": "../x"}, "invalid_request", None, id="outside-the-root"),
        pytest.param(
            {"directory": "missing"},
            "domain_error",
            {"reason": "not_found"},
            id="no-such-directory",
        ),
    ],
)
@pytest.mark.asyncio
async def test_files_list_refusals(
    tmp_path: Path, params: dict[str, Any], code: str, data: dict[str, str] | None
) -> None:
    state = _files_state(project_cwd=str(tmp_path), workspace=str(tmp_path), data_dir=str(tmp_path))

    with pytest.raises(RpcError) as exc_info:
        await _list_files(state, {"agent_id": "main", **params})
    assert (exc_info.value.code, exc_info.value.data) == (code, data)
