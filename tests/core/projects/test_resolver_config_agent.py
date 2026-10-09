"""Config-Agent Tool and Skill resolution tests."""

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents.temporary import TemporaryAgent, TemporaryAgentConfig, TemporaryAgentRegistry
from core.database import DatabaseUnavailableError
from core.projects.resolver import AgentResolver
from core.sessions import ChatSessionManager, SessionAddress
from core.tools.availability import ToolAccess, resolve_tool_access

from .resolver_test_support import (
    PROJECT_DEFAULT_ALLOWED_TOOLS,
    AgentStore,
    ConfigAgent,
    Path,
    ProjectStore,
    _openai_configured,
    _project,
    _resolver,
    _write_agent,
)
from .resolver_test_support import agents as agents
from .resolver_test_support import data_dir as data_dir
from .resolver_test_support import projects as projects
from .resolver_test_support import repo as repo
from .resolver_test_support import template_dir as template_dir


def test_config_agent_resolves_to_runnable_runtime_agent(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", body="You build.")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    # Assert
    assert isinstance(runtime_agent, ConfigAgent)
    assert runtime_agent.id == "builder"
    assert runtime_agent.model == "openai/gpt-5.2"
    assert runtime_agent.body == "You build.\n"
    # v1 config-agent invariants: no workspace, no memory tool. With no agent
    # denials the effective tools are exactly the project Tool Whitelist ceiling;
    # skills stay wildcard until Phase 3 wires the project skill rule.
    assert runtime_agent.workspace == ""
    assert runtime_agent.memory_prompt_mode == "off"
    assert runtime_agent.tool_access == ToolAccess(
        mode="selected",
        allowed=tuple(PROJECT_DEFAULT_ALLOWED_TOOLS),
    )
    # No project skills and nothing opted in → the agent has zero skills.
    assert runtime_agent.allowed_skills == []
    assert runtime_agent.tools == {"subagent": {"allowed_agents": []}}
    assert runtime_agent.fallback_models == []
    assert runtime_agent.thinking_effort is None


def test_config_agent_session_overrides_change_only_the_runtime_view(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(
        repo,
        "builder.md",
        model="openai/gpt-5.2",
        reasoning_effort="low",
    )
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())
    session = agents._session_manager().create("builder", project_id=project.project_id)
    resolver.update_session_overrides(
        SessionAddress(project.project_id, "builder", session.id),
        {"model": "openai/gpt-mini", "thinking_effort": "high"},
    )

    overridden = resolver.resolve_agent(project.project_id, "builder", session_id=session.id)
    configured = resolver.resolve_agent(project.project_id, "builder")

    assert isinstance(overridden, ConfigAgent)
    assert isinstance(configured, ConfigAgent)
    assert overridden.model == "openai/gpt-mini"
    assert overridden.thinking_effort == "high"
    assert configured.model == "openai/gpt-5.2"
    assert configured.thinking_effort == "low"
    assert overridden.tool_access == configured.tool_access
    assert overridden.body == configured.body


def test_temporary_profile_keeps_its_selection_inside_a_narrower_project(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    project = _project(projects, repo)
    project = projects.update(
        project.project_id,
        allowed_tools=["read", "grep"],
        skills_global_enabled=["global-skill"],
        skills_project_disabled=["disabled-skill"],
    )
    temporary = TemporaryAgent(
        id="temporary",
        name="temporary",
        model="openai/gpt-5.2",
        cwd=repo,
        tool_access=ToolAccess(
            mode="all",
            denied=("grep",),
            granted=("write", "read"),
        ),
        allowed_skills=["*"],
        tools={"read": {"safe": True}, "write": {"unsafe": True}},
        fallback_models=[],
    )
    resolver = AgentResolver(
        agents,
        projects,
        _openai_configured(),
        lambda: {},
        project_skill_names=lambda _project_id: frozenset({"project-skill", "disabled-skill"}),
        temporary_agents=SimpleNamespace(resolve=lambda *_args, **_kwargs: temporary),
        sessions=agents._session_manager(),
    )
    address = SessionAddress(project.project_id, "temporary", "session")
    agents._session_manager().create(
        "temporary", session_id="session", project_id=project.project_id
    )
    resolver.update_session_overrides(
        address, {"model": "openai/gpt-mini", "thinking_effort": "high"}
    )

    resolved = resolver.resolve_temporary_agent(
        address, generation_id="generation", session=address
    )

    assert resolved.model == "openai/gpt-mini"
    assert resolved.thinking_effort == "high"
    # The Project's Tool and Skill whitelists bound its Team, not a temporary
    # Agent's own selection.
    assert resolved.tool_access == temporary.tool_access
    assert resolved.tools == temporary.tools
    assert resolved.allowed_skills == ["*"]
    assert resolved.workspace == ""
    assert resolved.root_project_id is None
    assert resolved.custom_system_prompt_enabled is False
    from core.agents.temporary import TemporaryAgentConfig

    preview = resolver.preview_temporary_agent(
        TemporaryAgentConfig(
            model=temporary.model,
            cwd=repo,
            tool_access=temporary.tool_access,
            allowed_skills=temporary.allowed_skills,
            tools=temporary.tools,
            name=temporary.name,
            prompt_blocks=["core:working_project"],
        ),
        project.project_id,
    )
    assert preview.tool_access == resolved.tool_access
    assert preview.allowed_skills == resolved.allowed_skills
    assert preview.tools == resolved.tools
    assert preview.prompt_blocks == ["core:working_project"]


@pytest.mark.asyncio
async def test_async_temporary_resolution_reads_the_binding_on_the_session_pool(
    agents: AgentStore,
    projects: ProjectStore,
    repo: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(projects, repo)
    sessions = ChatSessionManager(data_dir)
    registry = TemporaryAgentRegistry(sessions)
    binding = registry.create(
        owner_name="extension",
        group_id="group",
        participant_id="participant",
        config=TemporaryAgentConfig(
            model="openai/gpt-5.2",
            cwd=repo,
            tool_access=ToolAccess(mode="selected", allowed=("read",)),
            allowed_skills=[],
            tools={},
            name="Participant",
        ),
        project_id=project.project_id,
    )
    resolver = AgentResolver(
        agents, projects, _openai_configured(), lambda: {}, temporary_agents=registry
    )
    threads: dict[str, str] = {}
    resolve_binding = registry.resolve
    require_project = resolver._require_temporary_project

    def recording_resolve(*args: Any, **kwargs: Any) -> Any:
        threads["binding"] = threading.current_thread().name
        return resolve_binding(*args, **kwargs)

    def recording_project(*args: Any) -> Any:
        threads["project"] = threading.current_thread().name
        return require_project(*args)

    monkeypatch.setattr(registry, "resolve", recording_resolve)
    monkeypatch.setattr(resolver, "_require_temporary_project", recording_project)
    try:
        resolved = await resolver.resolve_temporary_agent_async(
            binding.address, generation_id=binding.generation_id
        )

        assert resolved.id == binding.address.agent_id
        assert resolved.tool_access == ToolAccess(mode="selected", allowed=("read",))
        assert threads["binding"].startswith("vbot-db-sessions")
        assert threads["project"].startswith("vbot-agent-resolution")

        sessions.close()
        with pytest.raises(DatabaseUnavailableError):
            await resolver.resolve_temporary_agent_async(
                binding.address, generation_id=binding.generation_id
            )
    finally:
        sessions.close()


_BASE_TOOLS = tuple(PROJECT_DEFAULT_ALLOWED_TOOLS)


@pytest.mark.parametrize(
    ("permission", "allowed_tools", "tool_access"),
    [
        # permission.edit covers file mutation, so it removes apply_patch.
        pytest.param(
            {"edit": "deny", "webfetch": "deny", "websearch": "deny", "task": "deny"},
            None,
            ToolAccess(
                mode="selected",
                allowed=tuple(
                    tool
                    for tool in _BASE_TOOLS
                    if tool not in {"apply_patch", "web_fetch", "web_search", "subagent"}
                ),
                denied=("apply_patch", "subagent", "web_fetch", "web_search"),
            ),
            id="explorer-denials",
        ),
        pytest.param(
            {"task": "deny"},
            None,
            ToolAccess(
                mode="selected",
                allowed=tuple(tool for tool in _BASE_TOOLS if tool != "subagent"),
                denied=("subagent",),
            ),
            id="builder-denial",
        ),
        # The ceiling is the hard cap even without an Agent denial.
        pytest.param(
            None,
            ["read", "grep"],
            ToolAccess(mode="selected", allowed=("read", "grep")),
            id="narrowed-ceiling",
        ),
    ],
)
def test_repository_denials_narrow_the_project_tool_ceiling(
    agents: AgentStore,
    projects: ProjectStore,
    repo: Path,
    permission: dict[str, str] | None,
    allowed_tools: list[str] | None,
    tool_access: ToolAccess,
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", permission=permission)
    _project(projects, repo)
    if allowed_tools is not None:
        projects.update("vbot", allowed_tools=allowed_tools)
    resolver = _resolver(agents, projects, _openai_configured())

    actual = resolver.resolve_agent("vbot", "builder").tool_access
    expected = set(tool_access.allowed) | (
        {"edit", "write"} if actual.fixed and "apply_patch" in tool_access.allowed else set()
    )
    assert set(actual.allowed) == expected
    assert not set(actual.allowed) & set(tool_access.denied)


def test_vbot_tool_override_replaces_repository_denials_until_cleared(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", permission={"task": "deny"})
    _project(projects, repo)
    policy = {"mode": "selected", "allowed": ["subagent"]}
    projects.set_override("vbot", "builder", "tool_access", policy)
    resolver = _resolver(agents, projects, _openai_configured())

    overridden = resolver.resolve_agent("vbot", "builder")
    overridden_effective = resolver.effective_config("vbot", "builder")["tool_access"]
    projects.clear_override("vbot", "builder", "tool_access")
    restored = resolver.resolve_agent("vbot", "builder")
    restored_effective = resolver.effective_config("vbot", "builder")["tool_access"]

    # The override may re-enable a repository-denied Tool and select only it.
    assert overridden.tool_access == ToolAccess(mode="selected", allowed=("subagent",))
    assert overridden.tools == {"subagent": {"allowed_agents": []}}
    assert overridden_effective == {"value": policy, "source": "override"}
    assert "subagent" not in restored.tool_access.allowed
    assert restored_effective["source"] == "agent"


def test_tool_loading_override_reaches_the_config_agent_until_cleared(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())
    value = {"on_demand": True, "always_loaded": ["read"]}

    unset = resolver.resolve_agent("vbot", "builder").tool_loading
    projects.set_override("vbot", "builder", "tool_loading", value)
    overridden = resolver.resolve_agent("vbot", "builder").tool_loading
    projects.clear_override("vbot", "builder", "tool_loading")

    assert (unset, overridden) == (None, value)
    assert resolver.resolve_agent("vbot", "builder").tool_loading is None


@pytest.mark.parametrize(
    ("mode", "granted_access", "usable"),
    [
        pytest.param(
            "all",
            ToolAccess(mode="selected", allowed=("analyze_image",), granted=("analyze_image",)),
            ("analyze_image",),
            id="all",
        ),
        pytest.param(
            "selected",
            ToolAccess(mode="selected", allowed=("analyze_image",), granted=("analyze_image",)),
            ("analyze_image",),
            id="selected",
        ),
        pytest.param("none", ToolAccess(mode="none", granted=("analyze_image",)), (), id="none"),
    ],
)
def test_opt_in_grant_survives_project_resolution_and_reset(
    agents: AgentStore,
    projects: ProjectStore,
    repo: Path,
    mode: str,
    granted_access: ToolAccess,
    usable: tuple[str, ...],
) -> None:
    # An opt-in Tool such as the image-analysis vision exception needs an explicit
    # grant on top of the Project Tool Whitelist.
    tools = [SimpleNamespace(name="analyze_image", requires_opt_in=True)]
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    projects.update("vbot", allowed_tools=["analyze_image"])
    resolver = _resolver(agents, projects, _openai_configured())
    policy: dict[str, Any] = {"mode": mode, "granted": ["analyze_image"]}
    if mode == "selected":
        policy["allowed"] = ["analyze_image"]

    ungranted = resolver.resolve_agent("vbot", "builder").tool_access
    projects.set_override("vbot", "builder", "tool_access", policy)
    granted = resolver.resolve_agent("vbot", "builder").tool_access
    effective = resolver.effective_config("vbot", "builder")["tool_access"]
    projects.clear_override("vbot", "builder", "tool_access")
    reset = resolver.resolve_agent("vbot", "builder").tool_access

    assert resolve_tool_access(ungranted, tools, "off").allowed_tools == ()
    assert granted == granted_access
    assert resolve_tool_access(granted, tools, "off").allowed_tools == usable
    assert effective == {"value": policy, "source": "override"}
    assert reset == ToolAccess(mode="selected", allowed=("analyze_image",))


def test_effective_agent_targets_are_materialized_from_current_project_team(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _write_agent(repo, "review-one.md", model="openai/gpt-5.2")
    _write_agent(repo, "review-legacy.md", model="openai/gpt-5.2")
    orchestrator = repo / ".opencode" / "agents" / "orchestrator.md"
    orchestrator.write_text(
        (
            "---\nmodel: openai/gpt-5.2\npermission:\n  task:\n"
            '    "*": deny\n    "review-*": allow\n'
            '    "review-legacy": deny\n---\nBody.\n'
        ),
        encoding="utf-8",
    )
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    runtime_agent = resolver.resolve_agent(project.project_id, "orchestrator")

    assert runtime_agent.tools == {"subagent": {"allowed_agents": ["review-one"]}}


@pytest.mark.parametrize(
    ("project_skills", "whitelist", "allowed_skills"),
    [
        # With empty rule lists, the Agent gets exactly the Project's own Skills.
        pytest.param({"debugging", "refactoring"}, {}, ["debugging", "refactoring"], id="default"),
        # (project skills - disabled) + enabled bundled + enabled global, sorted.
        pytest.param(
            {"debugging", "refactoring"},
            {
                "skills_project_disabled": ["refactoring"],
                "skills_bundled_enabled": ["pdf"],
                "skills_global_enabled": ["deploy"],
            },
            ["debugging", "deploy", "pdf"],
            id="disabled-bundled-global",
        ),
        # The Project copy wins a name collision, so an opt-in of the same name cannot
        # resurrect a disabled Project Skill.
        pytest.param(
            {"debugging", "deploy"},
            {"skills_project_disabled": ["deploy"], "skills_global_enabled": ["deploy"]},
            ["debugging"],
            id="disabled-project-skill-beats-opt-in",
        ),
        # Disabling applies to Project Skills only; the same-named opt-in stays.
        pytest.param(
            {"debugging"},
            {"skills_project_disabled": ["pdf"], "skills_bundled_enabled": ["pdf"]},
            ["debugging", "pdf"],
            id="disabled-non-project-name-is-inert",
        ),
        # A Skill literally named "*" must not smuggle the wildcard past the whitelist.
        pytest.param(
            {"*", "debugging"},
            {"skills_global_enabled": ["*"]},
            ["debugging"],
            id="literal-wildcard",
        ),
    ],
)
def test_effective_skills_follow_the_project_skill_whitelist(
    agents: AgentStore,
    projects: ProjectStore,
    repo: Path,
    project_skills: set[str],
    whitelist: dict[str, list[str]],
    allowed_skills: list[str],
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    if whitelist:
        projects.update("vbot", **whitelist)
    resolver = _resolver(
        agents,
        projects,
        _openai_configured(),
        project_skill_names={"vbot": frozenset(project_skills)},
    )

    assert resolver.resolve_agent("vbot", "builder").allowed_skills == allowed_skills
