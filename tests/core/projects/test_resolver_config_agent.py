"""Config-Agent Tool and Skill resolution tests."""

from types import SimpleNamespace

from core.agents.temporary import TemporaryAgent
from core.projects.resolver import AgentResolver
from core.sessions import SessionAddress
from core.tools.availability import ToolAccess

from .resolver_test_support import (
    PROJECT_DEFAULT_ALLOWED_TOOLS,
    AgentRunOverrides,
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


def test_config_agent_run_overrides_change_only_the_runtime_view(
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

    overridden = resolver.resolve_agent(
        project.project_id,
        "builder",
        run_overrides=AgentRunOverrides(
            model="openai/gpt-mini",
            thinking_effort="",
        ),
    )
    configured = resolver.resolve_agent(project.project_id, "builder")

    assert isinstance(overridden, ConfigAgent)
    assert isinstance(configured, ConfigAgent)
    assert overridden.model == "openai/gpt-mini"
    assert overridden.thinking_effort == ""
    assert configured.model == "openai/gpt-5.2"
    assert configured.thinking_effort == "low"
    assert overridden.tool_access == configured.tool_access
    assert overridden.body == configured.body


def test_temporary_profile_is_narrowed_by_the_selected_project_ceiling(
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
    )

    resolved = resolver.resolve_temporary_agent(
        SessionAddress(project.project_id, "temporary", "session"),
        generation_id="generation",
        run_overrides=AgentRunOverrides(model="openai/gpt-mini", thinking_effort="high"),
    )

    assert resolved.model == "openai/gpt-mini"
    assert resolved.thinking_effort == "high"
    assert resolved.tool_access == ToolAccess(
        mode="selected",
        allowed=("read", "grep"),
        denied=("grep",),
        granted=("read",),
    )
    assert resolved.tools == {"read": {"safe": True}}
    assert resolved.allowed_skills == ["global-skill", "project-skill"]
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


def test_temporary_selected_empty_policy_stays_empty_inside_a_project(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    project = _project(projects, repo)
    temporary = TemporaryAgent(
        id="temporary",
        name="temporary",
        model="openai/gpt-5.2",
        cwd=repo,
        tool_access=ToolAccess(mode="selected", allowed=()),
        allowed_skills=["identity-private"],
        tools={},
        fallback_models=[],
    )
    resolver = AgentResolver(
        agents,
        projects,
        _openai_configured(),
        lambda: {},
        temporary_agents=SimpleNamespace(resolve=lambda *_args, **_kwargs: temporary),
    )

    resolved = resolver.resolve_temporary_agent(
        SessionAddress(project.project_id, "temporary", "session"), generation_id="generation"
    )

    assert resolved.tool_access == ToolAccess(mode="selected", allowed=())
    assert resolved.allowed_skills == []


def test_temporary_profile_skill_selection_cannot_widen_a_project_ceiling(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    project = _project(projects, repo)
    project = projects.update(project.project_id, skills_global_enabled=["allowed-skill"])
    temporary = TemporaryAgent(
        id="temporary",
        name="temporary",
        model="openai/gpt-5.2",
        cwd=repo,
        tool_access=ToolAccess(mode="selected", allowed=()),
        allowed_skills=["allowed-*", "outside-*"],
        tools={},
        fallback_models=[],
    )
    resolver = AgentResolver(
        agents,
        projects,
        _openai_configured(),
        lambda: {},
        temporary_agents=SimpleNamespace(resolve=lambda *_args, **_kwargs: temporary),
    )

    resolved = resolver.resolve_temporary_agent(
        SessionAddress(project.project_id, "temporary", "session"), generation_id="generation"
    )

    assert resolved.allowed_skills == ["allowed-skill"]


def test_effective_tools_drop_explorer_denials(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # An explorer-shaped agent (edit/webfetch/websearch/task all denied) resolves
    # without write+edit (permission.edit covers both), web_fetch, web_search, and
    # subagent — everything else in the project ceiling stays.
    _write_agent(
        repo,
        "explorer.md",
        model="openai/gpt-5.2",
        permission={"edit": "deny", "webfetch": "deny", "websearch": "deny", "task": "deny"},
    )
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    runtime_agent = resolver.resolve_agent(project.project_id, "explorer")

    denied = {"write", "edit", "web_fetch", "web_search", "subagent"}
    assert set(runtime_agent.tool_access.allowed).isdisjoint(denied)
    assert runtime_agent.tool_access.allowed == tuple(
        tool for tool in PROJECT_DEFAULT_ALLOWED_TOOLS if tool not in denied
    )
    assert set(runtime_agent.tool_access.denied) == denied


def test_effective_tools_drop_only_subagent_for_builder(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # A builder-shaped agent denies only task → only subagent is removed.
    _write_agent(
        repo,
        "builder.md",
        model="openai/gpt-5.2",
        permission={"task": "deny"},
    )
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert "subagent" not in runtime_agent.tool_access.allowed
    assert runtime_agent.tool_access.allowed == tuple(
        tool for tool in PROJECT_DEFAULT_ALLOWED_TOOLS if tool != "subagent"
    )
    assert runtime_agent.tool_access.denied == ("subagent",)


def test_effective_tools_no_denials_equal_project_ceiling(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "writer.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    runtime_agent = resolver.resolve_agent(project.project_id, "writer")

    assert runtime_agent.tool_access == ToolAccess(
        mode="selected", allowed=tuple(project.allowed_tools)
    )


def test_project_ceiling_omitting_a_tool_wins_over_no_denial(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # The ceiling is the hard cap: a tool the project omits is absent even when the
    # agent declares no denial for it.
    _write_agent(repo, "writer.md", model="openai/gpt-5.2")
    _project(projects, repo)
    project = projects.update("vbot", allowed_tools=["read", "grep"])
    resolver = _resolver(agents, projects, _openai_configured())

    runtime_agent = resolver.resolve_agent(project.project_id, "writer")

    assert runtime_agent.tool_access == ToolAccess(mode="selected", allowed=("read", "grep"))


def test_vbot_tool_override_replaces_repository_denials_and_can_select_one_tool(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(
        repo,
        "builder.md",
        model="openai/gpt-5.2",
        permission={"task": "deny"},
    )
    project = _project(projects, repo)
    projects.set_override(
        project.project_id,
        "builder",
        "tool_access",
        {"mode": "selected", "allowed": ["subagent"]},
    )
    resolver = _resolver(agents, projects, _openai_configured())

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")
    effective = resolver.effective_config(project.project_id, "builder")["tool_access"]

    assert runtime_agent.tool_access == ToolAccess(mode="selected", allowed=("subagent",))
    assert runtime_agent.tools == {"subagent": {"allowed_agents": []}}
    assert effective == {
        "value": {"mode": "selected", "allowed": ["subagent"]},
        "source": "override",
    }


def test_image_vision_grant_survives_project_resolution_and_reset(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = projects.create("vision", "Vision", repo)
    project = projects.update(project.project_id, allowed_tools=["analyze_image"])
    policy = {"mode": "all", "granted": ["analyze_image"]}
    projects.set_override(project.project_id, "builder", "tool_access", policy)
    resolver = _resolver(agents, projects, _openai_configured())
    assert resolver.resolve_agent(project.project_id, "builder").tool_access == ToolAccess(
        mode="selected", allowed=("analyze_image",), granted=("analyze_image",)
    )
    assert resolver.effective_config(project.project_id, "builder")["tool_access"] == {
        "value": policy,
        "source": "override",
    }
    projects.clear_override(project.project_id, "builder", "tool_access")
    assert resolver.resolve_agent(project.project_id, "builder").tool_access == ToolAccess(
        mode="selected", allowed=("analyze_image",)
    )


def test_clearing_vbot_tool_override_restores_repository_policy(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(
        repo,
        "builder.md",
        model="openai/gpt-5.2",
        permission={"task": "deny"},
    )
    project = _project(projects, repo)
    projects.set_override(
        project.project_id,
        "builder",
        "tool_access",
        {"mode": "none"},
    )
    projects.clear_override(project.project_id, "builder", "tool_access")
    resolver = _resolver(agents, projects, _openai_configured())

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")
    effective = resolver.effective_config(project.project_id, "builder")["tool_access"]

    assert "subagent" not in runtime_agent.tool_access.allowed
    assert runtime_agent.tool_access.denied == ("subagent",)
    assert effective["source"] == "agent"


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


def test_effective_skills_default_to_project_skills(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # With empty whitelist lists, a config agent's skills are exactly the project's
    # own scanned skills (bundled lie alongside as opt-in, off by default).
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(
        agents,
        projects,
        _openai_configured(),
        project_skill_names={"vbot": frozenset({"debugging", "refactoring"})},
    )

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.allowed_skills == ["debugging", "refactoring"]


def test_effective_skills_apply_disabled_and_bundled_rule(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # (project skills − disabled) ∪ enabled-bundled, sorted.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    project = projects.update(
        "vbot",
        skills_project_disabled=["refactoring"],
        skills_bundled_enabled=["pdf"],
    )
    resolver = _resolver(
        agents,
        projects,
        _openai_configured(),
        project_skill_names={"vbot": frozenset({"debugging", "refactoring"})},
    )

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.allowed_skills == ["debugging", "pdf"]


def test_effective_skills_include_enabled_global(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # (project skills − disabled) ∪ enabled-bundled ∪ enabled-global, sorted.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    project = projects.update(
        "vbot",
        skills_project_disabled=["refactoring"],
        skills_bundled_enabled=["pdf"],
        skills_global_enabled=["deploy"],
    )
    resolver = _resolver(
        agents,
        projects,
        _openai_configured(),
        project_skill_names={"vbot": frozenset({"debugging", "refactoring"})},
    )

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.allowed_skills == ["debugging", "deploy", "pdf"]


def test_effective_skills_disabled_project_skill_stays_off_despite_optin(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # A disabled project skill is off entirely: the merged registry resolves a name
    # collision to the project's own copy (project wins), so a same-named global or
    # bundled opt-in must not resurrect the disabled project skill.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    project = projects.update(
        "vbot",
        skills_project_disabled=["deploy"],
        skills_global_enabled=["deploy"],
    )
    resolver = _resolver(
        agents,
        projects,
        _openai_configured(),
        project_skill_names={"vbot": frozenset({"debugging", "deploy"})},
    )

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.allowed_skills == ["debugging"]


def test_effective_skills_disabled_nonproject_name_leaves_optin_alone(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # ``skills_project_disabled`` turns off project skills only: a disabled name
    # that is not a project skill stays inert and the opt-in keeps working.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    project = projects.update(
        "vbot",
        skills_project_disabled=["pdf"],
        skills_bundled_enabled=["pdf"],
    )
    resolver = _resolver(
        agents,
        projects,
        _openai_configured(),
        project_skill_names={"vbot": frozenset({"debugging"})},
    )

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.allowed_skills == ["debugging", "pdf"]


def test_effective_skills_drop_literal_wildcard_names(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # A repo skill *named* "*" (the lenient loader accepts that with a warning) must
    # not smuggle the allowed_skills wildcard past the project whitelist and expose
    # the whole global pool; the literal is dropped from every source list.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    project = projects.update("vbot", skills_global_enabled=["*"])
    resolver = _resolver(
        agents,
        projects,
        _openai_configured(),
        project_skill_names={"vbot": frozenset({"*", "debugging"})},
    )

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.allowed_skills == ["debugging"]
