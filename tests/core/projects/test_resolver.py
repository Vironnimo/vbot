"""Tests for the uniform agent resolver (identity store vs. project config).

Covers the resolution fork, the synthesized config runtime agent, the model
chain (agent → project default → global → error), the scan's BAD_MODEL findings,
the unchanged identity path, and the two freshness levels (single-agent config
read fresh per resolve vs. cached Team membership).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from core.agents.agents import AgentStore
from core.projects.projects import PROJECT_DEFAULT_ALLOWED_TOOLS
from core.projects.resolver import (
    AgentResolutionError,
    AgentResolver,
    ConfigAgent,
    ModelConfigurationChecker,
    resolve_prompt_project,
)
from core.projects.scan_report import FindingType
from core.projects.scanners.opencode import OPENCODE_AGENTS_SUBPATH
from core.projects.store import ProjectStore

# ---------------------------------------------------------------------------
# Fakes for the model/provider/credential surface the checker probes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeConnection:
    id: str


@dataclass(frozen=True)
class _FakeProviderConfig:
    connections: list[_FakeConnection]


class _FakeModels:
    """Catalog of configured ``(provider, model_id)`` pairs."""

    def __init__(self, known: set[tuple[str, str]]) -> None:
        self._known = known

    def get(self, provider_id: str, model_id: str) -> object:
        if (provider_id, model_id) not in self._known:
            raise KeyError(f"{provider_id}/{model_id}")
        return object()


class _FakeProviders:
    def __init__(self, providers: dict[str, _FakeProviderConfig]) -> None:
        self._providers = providers

    def get(self, provider_id: str) -> _FakeProviderConfig:
        if provider_id not in self._providers:
            raise KeyError(provider_id)
        return self._providers[provider_id]


class _FakeCredentials:
    """Set of compositional connection ids that have usable credentials."""

    def __init__(self, usable: set[str]) -> None:
        self._usable = usable

    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
        target = connection_id if connection_id is not None else provider_id
        return target in self._usable


def _checker(
    *,
    catalog: set[tuple[str, str]],
    providers: dict[str, _FakeProviderConfig],
    usable: set[str],
) -> ModelConfigurationChecker:
    return ModelConfigurationChecker(
        _FakeModels(catalog),
        _FakeProviders(providers),
        _FakeCredentials(usable),
    )


def _openai_configured() -> ModelConfigurationChecker:
    """Checker where ``openai/gpt-5.2`` exists, is in catalog, and is usable."""
    return _checker(
        catalog={("openai", "gpt-5.2"), ("openai", "gpt-mini")},
        providers={"openai": _FakeProviderConfig([_FakeConnection("api-key")])},
        usable={"openai:api-key"},
    )


# ---------------------------------------------------------------------------
# Fixture repo + stores.
# ---------------------------------------------------------------------------


def _write_agent(
    repo: Path,
    filename: str,
    *,
    model: str = "",
    body: str = "Body.",
    temperature: float | None = 0.3,
    reasoning_effort: str | None = None,
    permission: dict[str, str] | None = None,
) -> Path:
    agents_dir = repo.joinpath(*OPENCODE_AGENTS_SUBPATH)
    agents_dir.mkdir(parents=True, exist_ok=True)
    lines = ["description: An agent."]
    if model:
        lines.append(f"model: {model}")
    if temperature is not None:
        lines.append(f"temperature: {temperature}")
    if reasoning_effort is not None:
        lines.append(f"reasoningEffort: {reasoning_effort}")
    if permission:
        lines.append("permission:")
        lines.extend(f"  {key}: {value}" for key, value in permission.items())
    front = "\n".join(lines) + "\n"
    path = agents_dir / filename
    path.write_text(f"---\n{front}---\n{body}\n", encoding="utf-8")
    return path


@pytest.fixture
def template_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "templates"
    directory.mkdir()
    for filename in ("SOUL.md", "USER.md", "MEMORY.md"):
        (directory / filename).write_text(f"# {filename}\n", encoding="utf-8")
    return directory


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repos" / "vbot"
    repo_dir.mkdir(parents=True)
    return repo_dir


@pytest.fixture
def agents(data_dir: Path, template_dir: Path) -> AgentStore:
    return AgentStore(data_dir, template_dir=template_dir)


@pytest.fixture
def projects(data_dir: Path) -> ProjectStore:
    return ProjectStore(data_dir)


def _project(
    projects: ProjectStore,
    repo: Path,
    *,
    default_model: str = "",
    default_temperature: float | None = None,
    default_thinking_effort: str | None = None,
):
    return projects.create(
        "vbot",
        "vBot",
        repo,
        default_model=default_model,
        default_temperature=default_temperature,
        default_thinking_effort=default_thinking_effort,
    )


def _resolver(
    agents: AgentStore,
    projects: ProjectStore,
    checker: ModelConfigurationChecker,
    *,
    global_default: str = "",
    global_temperature: float | None = None,
    global_thinking_effort: str | None = None,
    project_skill_names: dict[str, frozenset[str]] | None = None,
) -> AgentResolver:
    """Build a resolver whose global tier is a ``defaults.agent`` dict provider.

    A key is present only when its argument is given, so each test injects exactly
    the global tier it wants — an absent key means "no global default" for that
    field (the chain falls through). ``""`` is a real value for thinking effort.
    ``project_skill_names`` injects the project-skill probe: a map of project id →
    its own scanned skill names (default: no project skills anywhere).
    """
    defaults: dict[str, Any] = {}
    if global_default:
        defaults["model"] = global_default
    if global_temperature is not None:
        defaults["temperature"] = global_temperature
    if global_thinking_effort is not None:
        defaults["thinking_effort"] = global_thinking_effort
    skill_names = project_skill_names or {}
    return AgentResolver(
        agents,
        projects,
        checker,
        lambda: defaults,
        project_skill_names=lambda project_id: skill_names.get(project_id, frozenset()),
    )


# ---------------------------------------------------------------------------
# Config-agent resolution + model chain.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Per-agent model override: the top tier of the config-agent model chain.
# ---------------------------------------------------------------------------


def test_model_override_wins_over_repo_model(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # The repo declares gpt-5.2; a vBot-owned override pins gpt-mini → override wins.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    projects.set_model_override("vbot", "builder", "openai/gpt-mini")
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
    projects.set_model_override("vbot", "builder", "openai/gpt-mini")
    resolver = _resolver(agents, projects, _openai_configured())

    assert resolver.resolve_agent("vbot", "builder").model == "openai/gpt-mini"
    assert resolver.resolve_agent("vbot", "planner").model == "openai/gpt-5.2"


def test_unconfigured_override_degrades_to_repo_model(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # An override that is not configured in this instance (e.g. credential removed)
    # falls through the same is_configured gate to the repo-declared model.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    projects.set_model_override("vbot", "builder", "openai/ghost-model")
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


# ---------------------------------------------------------------------------
# Temperature chain: agent → project default → global default → None.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Thinking-effort chain: agent → project default → global default → None.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Scan report: BAD_MODEL findings hung on at scan time.
# ---------------------------------------------------------------------------


def test_scan_reports_unconfigured_model_as_bad_model_finding(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange
    _write_agent(repo, "builder.md", model="openai/ghost-model")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    result = resolver.scan_project_report(project)

    # Assert
    bad = result.report.findings_of(FindingType.BAD_MODEL)
    assert len(bad) == 1
    assert bad[0].agent_id == "builder"
    assert bad[0].source_path is not None


def test_scan_does_not_flag_agent_without_declared_model(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: no declared model legitimately inherits a default — not a finding.
    _write_agent(repo, "writer.md", model="")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    result = resolver.scan_project_report(project)

    # Assert
    assert result.report.findings_of(FindingType.BAD_MODEL) == ()


def test_scan_reports_configured_model_clean(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    result = resolver.scan_project_report(project)

    # Assert
    assert result.report.is_clean
    assert [member.agent_id for member in result.team] == ["builder"]


# ---------------------------------------------------------------------------
# Identity path: unchanged.
# ---------------------------------------------------------------------------


def test_identity_resolution_returns_store_agent_unchanged(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange
    created = agents.create("orchestrator", "Orchestrator", model="openai/gpt-5.2")
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    resolved = resolver.resolve_agent(None, "orchestrator")

    # Assert: byte-for-byte the store agent (same object contract as today).
    assert resolved == created
    assert resolved.workspace == created.workspace
    assert resolved.model == "openai/gpt-5.2"


def test_identity_resolution_unknown_agent_raises(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    resolver = _resolver(agents, projects, _openai_configured())

    with pytest.raises(AgentResolutionError):
        resolver.resolve_agent(None, "missing-agent")


# ---------------------------------------------------------------------------
# Two freshness levels: live single-agent config vs. cached Team membership.
# ---------------------------------------------------------------------------


def test_single_agent_config_is_read_fresh_per_resolve(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: open-time scan caches the Team; then the repo file changes model.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", body="v1")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())
    resolver.rescan_project(project)  # caches the Team at open time

    # Mutate the repo file after the Team scan.
    _write_agent(repo, "builder.md", model="openai/gpt-mini", body="v2")

    # Act
    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    # Assert: config (model + body) reflects the live file, not the cached scan.
    assert isinstance(runtime_agent, ConfigAgent)
    assert runtime_agent.model == "openai/gpt-mini"
    assert runtime_agent.body == "v2\n"


def test_team_membership_uses_cache_not_live_new_file(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: Team is scanned/cached with one agent.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())
    resolver.rescan_project(project)

    # A new agent file appears in the repo *after* the open-time scan.
    _write_agent(repo, "planner.md", model="openai/gpt-5.2")

    # Act / Assert: the new agent is not on the cached Team until a re-scan.
    with pytest.raises(AgentResolutionError):
        resolver.resolve_agent(project.project_id, "planner")

    # After an explicit re-scan, the Team includes the new member.
    resolver.rescan_project(project)
    resolved = resolver.resolve_agent(project.project_id, "planner")
    assert resolved.id == "planner"


def test_resolve_unknown_project_agent_raises(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    with pytest.raises(AgentResolutionError):
        resolver.resolve_agent(project.project_id, "ghost")


# ---------------------------------------------------------------------------
# Model configuration checker rule.
# ---------------------------------------------------------------------------


def test_model_unconfigured_when_no_usable_connection(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: model is in catalog and provider exists, but no usable connection.
    checker = _checker(
        catalog={("openai", "gpt-5.2")},
        providers={"openai": _FakeProviderConfig([_FakeConnection("api-key")])},
        usable=set(),
    )
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, checker, global_default="")

    # Act / Assert: declared model is not usable → chain falls through → error.
    with pytest.raises(AgentResolutionError):
        resolver.resolve_agent(project.project_id, "builder")


# ---------------------------------------------------------------------------
# resolve_prompt_project — the rooting policy shared by chat loop and preview.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AgentWithWorkspace:
    """Minimal agent shape ``resolve_prompt_project`` reads (it only needs workspace)."""

    workspace: str


def _ws_agent(workspace: str) -> Any:
    # Typed ``Any``: ``resolve_prompt_project`` takes a ``RuntimeAgent``, and this
    # stub only needs to expose ``workspace`` for the rooting lookup.
    return _AgentWithWorkspace(workspace=workspace)


def test_resolve_prompt_project_uses_explicit_project(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "data")
    repo = tmp_path / "repo"
    repo.mkdir()
    store.create("vbot", "vBot", repo)
    # A config agent has an empty workspace; the explicit project_id is what counts.
    resolved = resolve_prompt_project(store, "vbot", _ws_agent(""))

    assert resolved is not None
    assert resolved.project_id == "vbot"


def test_resolve_prompt_project_roots_identity_agent_by_workspace(tmp_path: Path) -> None:
    # Identity session (project_id None) + workspace == a registered repo → rooted.
    store = ProjectStore(tmp_path / "data")
    repo = tmp_path / "repo"
    repo.mkdir()
    store.create("vbot", "vBot", repo)
    resolved = resolve_prompt_project(store, None, _ws_agent(str(repo)))

    assert resolved is not None
    assert resolved.project_id == "vbot"


def test_resolve_prompt_project_none_when_workspace_not_a_repo(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "data")
    repo = tmp_path / "repo"
    repo.mkdir()
    store.create("vbot", "vBot", repo)
    home = tmp_path / "workspace-coder"
    home.mkdir()

    assert resolve_prompt_project(store, None, _ws_agent(str(home))) is None


def test_resolve_prompt_project_none_for_empty_workspace(tmp_path: Path) -> None:
    # An identity agent at its data-dir home with no project match (or a config
    # agent's empty workspace) is never rooted.
    store = ProjectStore(tmp_path / "data")
    repo = tmp_path / "repo"
    repo.mkdir()
    store.create("vbot", "vBot", repo)

    assert resolve_prompt_project(store, None, _ws_agent("")) is None
