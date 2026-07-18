"""Config-Agent Tool and Skill resolution tests."""

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
    assert runtime_agent.allowed_tools == list(PROJECT_DEFAULT_ALLOWED_TOOLS)
    # No project skills and nothing opted in → the agent has zero skills.
    assert runtime_agent.allowed_skills == []
    assert runtime_agent.tools == {"subagent": {"allowed_agents": ["builder"]}}
    assert runtime_agent.fallback_model == ""
    assert runtime_agent.thinking_effort is None


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
    assert set(runtime_agent.allowed_tools).isdisjoint(denied)
    assert runtime_agent.allowed_tools == [
        tool for tool in PROJECT_DEFAULT_ALLOWED_TOOLS if tool not in denied
    ]


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

    assert "subagent" not in runtime_agent.allowed_tools
    assert runtime_agent.allowed_tools == [
        tool for tool in PROJECT_DEFAULT_ALLOWED_TOOLS if tool != "subagent"
    ]


def test_effective_tools_no_denials_equal_project_ceiling(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "writer.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    runtime_agent = resolver.resolve_agent(project.project_id, "writer")

    assert runtime_agent.allowed_tools == list(project.allowed_tools)


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

    assert runtime_agent.allowed_tools == ["read", "grep"]


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
