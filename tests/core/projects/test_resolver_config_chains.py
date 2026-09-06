"""Config-Agent Model, temperature, and thinking resolution-chain tests."""

from .resolver_test_support import (
    AgentResolutionError,
    AgentStore,
    Path,
    ProjectStore,
    _openai_configured,
    _project,
    _resolver,
    _write_agent,
    pytest,
)
from .resolver_test_support import agents as agents
from .resolver_test_support import data_dir as data_dir
from .resolver_test_support import projects as projects
from .resolver_test_support import repo as repo
from .resolver_test_support import template_dir as template_dir


def test_model_chain_falls_back_to_project_default(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: agent declares no model; project default is configured.
    _write_agent(repo, "writer.md", model="")
    project = _project(projects, repo, default_model="openai/gpt-mini")
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    runtime_agent = resolver.resolve_agent(project.project_id, "writer")

    # Assert
    assert runtime_agent.model == "openai/gpt-mini"


def test_model_chain_falls_back_to_global_default(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: no agent model, no project default; global default configured.
    _write_agent(repo, "writer.md", model="")
    project = _project(projects, repo, default_model="")
    resolver = _resolver(agents, projects, _openai_configured(), global_default="openai/gpt-5.2")

    # Act
    runtime_agent = resolver.resolve_agent(project.project_id, "writer")

    # Assert
    assert runtime_agent.model == "openai/gpt-5.2"


def test_model_chain_falls_all_the_way_through_raises(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: no usable model anywhere.
    _write_agent(repo, "writer.md", model="")
    project = _project(projects, repo, default_model="")
    resolver = _resolver(agents, projects, _openai_configured(), global_default="")

    # Act / Assert
    with pytest.raises(AgentResolutionError):
        resolver.resolve_agent(project.project_id, "writer")


def test_unconfigured_agent_model_falls_through_to_default(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: agent declares an unconfigured model; project default is usable.
    _write_agent(repo, "builder.md", model="openai/ghost-model")
    project = _project(projects, repo, default_model="openai/gpt-5.2")
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    # Assert: chain fell through the unconfigured declared model.
    assert runtime_agent.model == "openai/gpt-5.2"


def test_model_override_wins_over_repo_model(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # The repo declares gpt-5.2; a vBot-owned override sets gpt-mini → the override wins.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    projects.set_override("vbot", "builder", "model", "openai/gpt-mini")
    resolver = _resolver(agents, projects, _openai_configured())

    runtime_agent = resolver.resolve_agent("vbot", "builder")

    assert runtime_agent.model == "openai/gpt-mini"


def test_model_override_applies_only_to_its_agent(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # An override keyed on builder must not bleed onto another agent.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _write_agent(repo, "planner.md", model="openai/gpt-5.2")
    _project(projects, repo)
    projects.set_override("vbot", "builder", "model", "openai/gpt-mini")
    resolver = _resolver(agents, projects, _openai_configured())

    assert resolver.resolve_agent("vbot", "builder").model == "openai/gpt-mini"
    assert resolver.resolve_agent("vbot", "planner").model == "openai/gpt-5.2"


def test_unconfigured_model_override_degrades_to_repo_model(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # An override that is not configured in this instance (e.g. credential removed)
    # falls through the same is_configured gate to the repo-declared model.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    projects.set_override("vbot", "builder", "model", "openai/ghost-model")
    resolver = _resolver(agents, projects, _openai_configured())

    runtime_agent = resolver.resolve_agent("vbot", "builder")

    assert runtime_agent.model == "openai/gpt-5.2"


def test_is_model_configured_matches_checker(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # The public seam the /model command reuses delegates to the same rule as the
    # scan's BAD_MODEL check, so accepted models and clean-scan models cannot drift.
    resolver = _resolver(agents, projects, _openai_configured())

    assert resolver.is_model_configured("openai/gpt-5.2") is True
    assert resolver.is_model_configured("openai/ghost-model") is False
    assert resolver.is_model_configured("") is False


def test_temperature_chain_agent_value_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=0.7)
    project = _project(projects, repo, default_temperature=0.2)
    resolver = _resolver(agents, projects, _openai_configured(), global_temperature=0.9)

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.temperature == 0.7


def test_temperature_chain_falls_back_to_project_default(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Agent declares no temperature; the project default delivers.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=None)
    project = _project(projects, repo, default_temperature=0.2)
    resolver = _resolver(agents, projects, _openai_configured(), global_temperature=0.9)

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.temperature == 0.2


def test_temperature_chain_falls_back_to_global_default(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=None)
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured(), global_temperature=0.9)

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.temperature == 0.9


def test_temperature_chain_all_empty_yields_none(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=None)
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.temperature is None


def test_temperature_project_zero_stops_chain(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # 0.0 is a real value (the sampling floor), not "unset" — it must stop the
    # chain before the global default, not fall through.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=None)
    project = _project(projects, repo, default_temperature=0.0)
    resolver = _resolver(agents, projects, _openai_configured(), global_temperature=0.9)

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.temperature == 0.0


def test_thinking_chain_agent_value_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # The agent tier is the scanned reasoningEffort (Phase 1b).
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", reasoning_effort="high")
    project = _project(projects, repo, default_thinking_effort="low")
    resolver = _resolver(agents, projects, _openai_configured(), global_thinking_effort="medium")

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.thinking_effort == "high"


def test_thinking_chain_falls_back_to_project_default(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo, default_thinking_effort="low")
    resolver = _resolver(agents, projects, _openai_configured(), global_thinking_effort="medium")

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.thinking_effort == "low"


def test_thinking_chain_falls_back_to_global_default(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured(), global_thinking_effort="medium")

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.thinking_effort == "medium"


def test_thinking_project_empty_string_blocks_global(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # "" is a real value meaning "provider default" — it stops the chain, so the
    # global default never applies. The resolved value is "" (not the global one).
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo, default_thinking_effort="")
    resolver = _resolver(agents, projects, _openai_configured(), global_thinking_effort="medium")

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.thinking_effort == ""


def test_thinking_chain_all_empty_yields_none(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    assert runtime_agent.thinking_effort is None


@pytest.mark.parametrize("mode", ["all", "selected", "none"])
def test_project_opt_in_requires_grant_beyond_whitelist(agents, projects, repo, mode):
    from types import SimpleNamespace

    from core.tools.availability import resolve_tool_access

    _write_agent(repo, "writer.md", model="openai/gpt-mini")
    project = _project(projects, repo)
    project = projects.update(project.project_id, allowed_tools=["computer"])
    resolver = _resolver(agents, projects, _openai_configured())
    tools = [SimpleNamespace(name="computer", requires_opt_in=True)]
    runtime_agent = resolver.resolve_agent(project.project_id, "writer")
    assert resolve_tool_access(runtime_agent.tool_access, tools, "off").allowed_tools == ()
    policy = {"mode": mode, "granted": ["computer"]}
    if mode == "selected":
        policy["allowed"] = ["computer"]
    projects.set_override(project.project_id, "writer", "tool_access", policy)
    runtime_agent = resolver.resolve_agent(project.project_id, "writer")
    assert runtime_agent.tool_access.granted == ("computer",)
    assert resolve_tool_access(runtime_agent.tool_access, tools, "off").allowed_tools == (
        () if mode == "none" else ("computer",)
    )


def test_project_cannot_grant_opt_in_outside_whitelist(agents, projects, repo):
    from core.projects.projects import ProjectError

    project = _project(projects, repo)
    project = projects.update(project.project_id, allowed_tools=["read"])
    with pytest.raises(ProjectError):
        projects.set_override(
            project.project_id, "writer", "tool_access", {"mode": "all", "granted": ["computer"]}
        )
