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
    resolve_skill_scope,
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


@dataclass(frozen=True)
class _FakeCatalogModel:
    """Catalog-model stub carrying the per-model connection allowlist rule."""

    connections: tuple[str, ...] = ()

    def allows_connection(self, connection_id: str) -> bool:
        return not self.connections or connection_id in self.connections


class _FakeModels:
    """Catalog of configured ``(provider, model_id)`` pairs with optional allowlists."""

    def __init__(
        self,
        known: set[tuple[str, str]],
        connections: dict[tuple[str, str], tuple[str, ...]] | None = None,
    ) -> None:
        self._known = known
        self._connections = connections or {}

    def get(self, provider_id: str, model_id: str) -> _FakeCatalogModel:
        if (provider_id, model_id) not in self._known:
            raise KeyError(f"{provider_id}/{model_id}")
        return _FakeCatalogModel(self._connections.get((provider_id, model_id), ()))


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
    model_connections: dict[tuple[str, str], tuple[str, ...]] | None = None,
) -> ModelConfigurationChecker:
    return ModelConfigurationChecker(
        _FakeModels(catalog, model_connections),
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


def test_scan_honors_project_source_format(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: agents in both formats; the project declares claude as its format.
    _write_agent(repo, "builder.md")
    claude_dir = repo / ".claude" / "agents"
    claude_dir.mkdir(parents=True)
    (claude_dir / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Reviews.\n---\nBody.\n", encoding="utf-8"
    )
    project = projects.create("vbot", "vBot", repo, source_format="claude")
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    result = resolver.scan_project_report(project)

    # Assert: only the claude member is on the team — no mixing.
    assert [member.agent_id for member in result.team] == ["reviewer"]
    assert result.team[0].source_format == "claude"


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


def _two_connection_checker(
    *,
    usable: set[str],
    allowlist: tuple[str, ...],
) -> ModelConfigurationChecker:
    """Checker with connections ``api-key``/``subscription`` and one allowlisted model."""
    return _checker(
        catalog={("openai", "gpt-5.2")},
        providers={
            "openai": _FakeProviderConfig(
                [_FakeConnection("api-key"), _FakeConnection("subscription")]
            )
        },
        usable=usable,
        model_connections={("openai", "gpt-5.2"): allowlist},
    )


def test_connection_bound_model_unconfigured_without_allowed_credential() -> None:
    # A subscription-only model whose only credential is on the forbidden api-key
    # connection cannot run — the runtime would refuse the connection pick, so the
    # gate must refuse too (chain falls through instead of a hard run failure).
    checker = _two_connection_checker(usable={"openai:api-key"}, allowlist=("subscription",))
    assert checker.is_configured("openai/gpt-5.2") is False


def test_connection_bound_model_configured_on_allowed_credential() -> None:
    checker = _two_connection_checker(usable={"openai:subscription"}, allowlist=("subscription",))
    assert checker.is_configured("openai/gpt-5.2") is True


def test_pinned_connection_without_credential_is_unconfigured() -> None:
    # The pin is verbatim: a credential on another connection does not help.
    checker = _two_connection_checker(usable={"openai:api-key"}, allowlist=())
    assert checker.is_configured("openai/gpt-5.2::subscription") is False


def test_pinned_connection_with_credential_is_configured() -> None:
    checker = _two_connection_checker(usable={"openai:subscription"}, allowlist=())
    assert checker.is_configured("openai/gpt-5.2::subscription") is True


def test_pinned_account_suffix_checks_full_compositional_id() -> None:
    checker = _two_connection_checker(usable={"openai:subscription:work"}, allowlist=())
    assert checker.is_configured("openai/gpt-5.2::subscription:work") is True
    assert checker.is_configured("openai/gpt-5.2::subscription:home") is False


def test_pinned_unknown_connection_is_unconfigured() -> None:
    checker = _two_connection_checker(usable={"openai:ghost"}, allowlist=())
    assert checker.is_configured("openai/gpt-5.2::ghost") is False


def test_pinned_connection_forbidden_by_model_allowlist_is_unconfigured() -> None:
    checker = _two_connection_checker(
        usable={"openai:api-key", "openai:subscription"}, allowlist=("subscription",)
    )
    assert checker.is_configured("openai/gpt-5.2::api-key") is False


def test_empty_suffix_after_separator_is_unconfigured() -> None:
    checker = _two_connection_checker(usable={"openai:api-key"}, allowlist=())
    assert checker.is_configured("openai/gpt-5.2::") is False


def test_connection_bound_declared_model_falls_through_chain(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # The declared model is allowlist-bound to a connection without credentials;
    # the chain must degrade to the configured project default instead of
    # resolving a model that would fail connection resolution at run time.
    checker = _checker(
        catalog={("openai", "gpt-5.2"), ("openai", "gpt-mini")},
        providers={
            "openai": _FakeProviderConfig(
                [_FakeConnection("api-key"), _FakeConnection("subscription")]
            )
        },
        usable={"openai:api-key"},
        model_connections={("openai", "gpt-5.2"): ("subscription",)},
    )
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo, default_model="openai/gpt-mini")
    resolver = _resolver(agents, projects, checker)

    resolved = resolver.resolve_agent(project.project_id, "builder")

    assert resolved.model == "openai/gpt-mini"


def test_scan_reports_connection_bound_model_as_bad_model_finding(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    checker = _two_connection_checker(usable={"openai:api-key"}, allowlist=("subscription",))
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, checker)

    result = resolver.scan_project_report(project)

    findings = result.report.findings_of(FindingType.BAD_MODEL)
    assert len(findings) == 1
    assert findings[0].agent_id == "builder"


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


# ---------------------------------------------------------------------------
# resolve_skill_scope — the skill-scoping policy shared by chat loop, prompt
# preview, and $-autocomplete.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ProjectStub:
    project_id: str


def _stub_project(project_id: str) -> Any:
    # Typed ``Any``: ``resolve_skill_scope`` takes a ``Project`` but only reads
    # ``project_id``, mirroring the ``_ws_agent`` stub pattern above.
    return _ProjectStub(project_id=project_id)


def test_resolve_skill_scope_project_run_drops_private_layer() -> None:
    # A project run scopes to its own project and never carries an identity layer:
    # a team slug colliding with an identity agent's id must not pull that agent's
    # private skills past the project skill whitelist.
    scope = resolve_skill_scope("vbot", _stub_project("vbot"), "builder")

    assert scope == ("vbot", None)


def test_resolve_skill_scope_rooted_identity_uses_home_project() -> None:
    # A rooted identity run (project_id None, prompt project resolved by rooting)
    # sees its home project's skills plus its own private layer.
    scope = resolve_skill_scope(None, _stub_project("vbot"), "main")

    assert scope == ("vbot", "main")


def test_resolve_skill_scope_plain_identity_stays_global() -> None:
    scope = resolve_skill_scope(None, None, "main")

    assert scope == (None, "main")


# ---------------------------------------------------------------------------
# effective_config — config agent: per-tier value + source (never raises for a
# fallen-through model). Same chain as resolve_agent, so the two cannot drift.
# ---------------------------------------------------------------------------


def test_effective_config_model_override_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    projects.set_override("vbot", "builder", "model", "openai/gpt-mini")
    resolver = _resolver(agents, projects, _openai_configured())

    result = resolver.effective_config("vbot", "builder")

    assert result["model"] == {"value": "openai/gpt-mini", "source": "override"}


def test_effective_config_model_agent_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    result = resolver.effective_config("vbot", "builder")

    assert result["model"] == {"value": "openai/gpt-5.2", "source": "agent"}


def test_effective_config_model_project_default_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "writer.md", model="")
    _project(projects, repo, default_model="openai/gpt-mini")
    resolver = _resolver(agents, projects, _openai_configured())

    result = resolver.effective_config("vbot", "writer")

    assert result["model"] == {"value": "openai/gpt-mini", "source": "project_default"}


def test_effective_config_model_global_default_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "writer.md", model="")
    _project(projects, repo, default_model="")
    resolver = _resolver(agents, projects, _openai_configured(), global_default="openai/gpt-5.2")

    result = resolver.effective_config("vbot", "writer")

    assert result["model"] == {"value": "openai/gpt-5.2", "source": "global_default"}


def test_effective_config_model_fell_through_returns_none_without_raising(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # A fully-fallen-through model chain does NOT raise here (unlike resolve_agent);
    # it reports {"value": None, "source": None}.
    _write_agent(repo, "writer.md", model="")
    _project(projects, repo, default_model="")
    resolver = _resolver(agents, projects, _openai_configured(), global_default="")

    result = resolver.effective_config("vbot", "writer")

    assert result["model"] == {"value": None, "source": None}


def test_effective_config_unconfigured_model_override_skipped_falls_to_agent(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # An unconfigured overridden model is skipped by the is_configured gate for BOTH
    # resolve_agent and effective_config — the chain falls to the repo-declared model.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    projects.set_override("vbot", "builder", "model", "openai/ghost-model")
    resolver = _resolver(agents, projects, _openai_configured())

    assert resolver.resolve_agent("vbot", "builder").model == "openai/gpt-5.2"
    assert resolver.effective_config("vbot", "builder")["model"] == {
        "value": "openai/gpt-5.2",
        "source": "agent",
    }


def test_effective_config_temperature_override_zero_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # A temperature override of 0.0 (a real value) is the top tier and wins.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=0.7)
    _project(projects, repo, default_temperature=0.2)
    projects.set_override("vbot", "builder", "temperature", 0.0)
    resolver = _resolver(agents, projects, _openai_configured(), global_temperature=0.9)

    result = resolver.effective_config("vbot", "builder")

    assert result["temperature"] == {"value": 0.0, "source": "override"}


def test_effective_config_temperature_agent_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=0.7)
    _project(projects, repo, default_temperature=0.2)
    resolver = _resolver(agents, projects, _openai_configured(), global_temperature=0.9)

    result = resolver.effective_config("vbot", "builder")

    assert result["temperature"] == {"value": 0.7, "source": "agent"}


def test_effective_config_temperature_project_default_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=None)
    _project(projects, repo, default_temperature=0.2)
    resolver = _resolver(agents, projects, _openai_configured(), global_temperature=0.9)

    result = resolver.effective_config("vbot", "builder")

    assert result["temperature"] == {"value": 0.2, "source": "project_default"}


def test_effective_config_temperature_global_default_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=None)
    _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured(), global_temperature=0.9)

    result = resolver.effective_config("vbot", "builder")

    assert result["temperature"] == {"value": 0.9, "source": "global_default"}


def test_effective_config_thinking_override_empty_string_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # A thinking_effort override of "" (force provider default, a real value) wins.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", reasoning_effort="high")
    _project(projects, repo, default_thinking_effort="low")
    projects.set_override("vbot", "builder", "thinking_effort", "")
    resolver = _resolver(agents, projects, _openai_configured(), global_thinking_effort="medium")

    result = resolver.effective_config("vbot", "builder")

    assert result["thinking_effort"] == {"value": "", "source": "override"}


def test_effective_config_thinking_agent_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", reasoning_effort="high")
    _project(projects, repo, default_thinking_effort="low")
    resolver = _resolver(agents, projects, _openai_configured(), global_thinking_effort="medium")

    result = resolver.effective_config("vbot", "builder")

    assert result["thinking_effort"] == {"value": "high", "source": "agent"}


def test_effective_config_thinking_project_default_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo, default_thinking_effort="low")
    resolver = _resolver(agents, projects, _openai_configured(), global_thinking_effort="medium")

    result = resolver.effective_config("vbot", "builder")

    assert result["thinking_effort"] == {"value": "low", "source": "project_default"}


def test_effective_config_thinking_global_default_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured(), global_thinking_effort="medium")

    result = resolver.effective_config("vbot", "builder")

    assert result["thinking_effort"] == {"value": "medium", "source": "global_default"}


def test_effective_config_unknown_project_raises(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    resolver = _resolver(agents, projects, _openai_configured())

    with pytest.raises(AgentResolutionError):
        resolver.effective_config("missing", "builder")


def test_effective_config_unknown_agent_raises(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    with pytest.raises(AgentResolutionError):
        resolver.effective_config("vbot", "ghost")


def test_effective_config_for_member_matches_effective_config(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # The scanned-member seam runs the same per-tier chain as effective_config,
    # so a team listing never re-scans yet reports the identical result.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=0.7)
    project = _project(projects, repo)
    projects.set_override("vbot", "builder", "model", "openai/gpt-mini")
    resolver = _resolver(agents, projects, _openai_configured())
    result = resolver.scan_project_report(project)
    member = next(m for m in result.team if m.agent_id == "builder")

    from_member = resolver.effective_config_for_member(projects.get("vbot"), member)

    assert from_member == resolver.effective_config("vbot", "builder")
    assert from_member["model"] == {"value": "openai/gpt-mini", "source": "override"}


# ---------------------------------------------------------------------------
# effective_config — identity agent: own value vs. baked global default. No
# is_configured gating; mirrors AgentStore._apply_defaults exactly.
# ---------------------------------------------------------------------------


def test_identity_effective_config_reports_own_value_as_agent(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    agents.create(
        "orchestrator",
        "Orchestrator",
        model="openai/gpt-5.2",
        fallback_model="openai/gpt-mini",
        temperature=0.3,
        thinking_effort="high",
    )
    resolver = _resolver(agents, projects, _openai_configured(), global_default="openai/ghost")

    result = resolver.effective_config(None, "orchestrator")

    assert result["model"] == {"value": "openai/gpt-5.2", "source": "agent"}
    assert result["fallback_model"] == {"value": "openai/gpt-mini", "source": "agent"}
    assert result["temperature"] == {"value": 0.3, "source": "agent"}
    assert result["thinking_effort"] == {"value": "high", "source": "agent"}


def test_identity_effective_config_reports_global_default_when_own_empty(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # No own model/temperature/thinking → the global default is the source, exactly
    # as AgentStore.get would bake it. get_raw is what lets the resolver see the "".
    agents.create("orchestrator", "Orchestrator")
    resolver = _resolver(
        agents,
        projects,
        _openai_configured(),
        global_default="openai/gpt-5.2",
        global_temperature=0.9,
        global_thinking_effort="medium",
    )

    result = resolver.effective_config(None, "orchestrator")

    assert result["model"] == {"value": "openai/gpt-5.2", "source": "global_default"}
    assert result["temperature"] == {"value": 0.9, "source": "global_default"}
    assert result["thinking_effort"] == {"value": "medium", "source": "global_default"}


def test_identity_effective_config_reports_none_when_neither(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    agents.create("orchestrator", "Orchestrator")
    resolver = _resolver(agents, projects, _openai_configured())

    result = resolver.effective_config(None, "orchestrator")

    assert result["model"] == {"value": None, "source": None}
    assert result["fallback_model"] == {"value": None, "source": None}
    assert result["temperature"] == {"value": None, "source": None}
    assert result["thinking_effort"] == {"value": None, "source": None}


def test_identity_effective_config_own_zero_temperature_is_agent(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # A present own value including 0.0 temperature and "" thinking effort stops the
    # chain at "agent", never falling to the global default.
    agents.create("orchestrator", "Orchestrator", temperature=0.0, thinking_effort="")
    resolver = _resolver(
        agents,
        projects,
        _openai_configured(),
        global_temperature=0.9,
        global_thinking_effort="medium",
    )

    result = resolver.effective_config(None, "orchestrator")

    assert result["temperature"] == {"value": 0.0, "source": "agent"}
    assert result["thinking_effort"] == {"value": "", "source": "agent"}


def test_identity_effective_config_unknown_agent_raises(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    resolver = _resolver(agents, projects, _openai_configured())

    with pytest.raises(AgentResolutionError):
        resolver.effective_config(None, "missing-agent")


def test_get_bakes_global_default_but_get_raw_does_not(
    agents: AgentStore, projects: ProjectStore, repo: Path, data_dir: Path, template_dir: Path
) -> None:
    # The get/get_raw seam the identity effective_config relies on: with a global
    # default set, get() bakes it in while get_raw() returns the unbaked "" / None.
    baking_store = AgentStore(
        data_dir,
        template_dir=template_dir,
        defaults_provider=lambda: {"model": "openai/gpt-5.2", "temperature": 0.9},
    )
    baking_store.create("orchestrator", "Orchestrator")

    baked = baking_store.get("orchestrator")
    raw = baking_store.get_raw("orchestrator")

    assert baked.model == "openai/gpt-5.2"
    assert baked.temperature == 0.9
    assert raw.model == ""
    assert raw.temperature is None
