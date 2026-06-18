"""Tests for the uniform agent resolver (identity store vs. project config).

Covers the resolution fork, the synthesized config runtime agent, the model
chain (agent → project default → global → error), the scan's BAD_MODEL findings,
the unchanged identity path, and the two freshness levels (single-agent config
read fresh per resolve vs. cached Team membership).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from core.agents.agents import AgentStore
from core.projects.projects import build_project
from core.projects.resolver import (
    AgentResolutionError,
    AgentResolver,
    ConfigAgent,
    ModelConfigurationChecker,
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


def _write_agent(repo: Path, filename: str, *, model: str = "", body: str = "Body.") -> Path:
    agents_dir = repo.joinpath(*OPENCODE_AGENTS_SUBPATH)
    agents_dir.mkdir(parents=True, exist_ok=True)
    front = f"description: An agent.\nmodel: {model}\ntemperature: 0.3\n" if model else (
        "description: An agent.\ntemperature: 0.3\n"
    )
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


def _project(projects: ProjectStore, repo: Path, *, default_model: str = ""):
    return projects.create("vbot", "vBot", repo, default_model=default_model)


def _resolver(
    agents: AgentStore,
    projects: ProjectStore,
    checker: ModelConfigurationChecker,
    *,
    global_default: str = "",
) -> AgentResolver:
    return AgentResolver(agents, projects, checker, lambda: global_default)


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
    # v1 config-agent invariants: no workspace, no memory tool, tools/skills = *.
    assert runtime_agent.workspace == ""
    assert runtime_agent.memory_prompt_mode == "off"
    assert runtime_agent.allowed_tools == ["*"]
    assert runtime_agent.allowed_skills == ["*"]
    assert runtime_agent.fallback_model == ""
    assert runtime_agent.thinking_effort is None


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
    resolver = _resolver(
        agents, projects, _openai_configured(), global_default="openai/gpt-5.2"
    )

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
