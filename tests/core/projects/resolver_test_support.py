"""Shared fixtures, fakes, and dependencies for Agent resolver tests."""

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

    def is_usable(self, provider_id: str, connection_id: str | None = None) -> bool:
        return self.has_credentials(provider_id, connection_id)


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


@dataclass(frozen=True)
class _ProjectStub:
    project_id: str


def _stub_project(project_id: str) -> Any:
    # Typed ``Any``: ``resolve_skill_scope`` takes a ``Project`` but only reads
    # ``project_id``, mirroring the ``_ws_agent`` stub pattern above.
    return _ProjectStub(project_id=project_id)


__all__ = [
    "dataclass",
    "Path",
    "Any",
    "pytest",
    "AgentStore",
    "PROJECT_DEFAULT_ALLOWED_TOOLS",
    "AgentResolutionError",
    "AgentResolver",
    "ConfigAgent",
    "ModelConfigurationChecker",
    "resolve_prompt_project",
    "resolve_skill_scope",
    "FindingType",
    "OPENCODE_AGENTS_SUBPATH",
    "ProjectStore",
    "_FakeConnection",
    "_FakeProviderConfig",
    "_FakeCatalogModel",
    "_FakeModels",
    "_FakeProviders",
    "_FakeCredentials",
    "_checker",
    "_openai_configured",
    "_write_agent",
    "template_dir",
    "data_dir",
    "repo",
    "agents",
    "projects",
    "_project",
    "_resolver",
    "_two_connection_checker",
    "_ProjectStub",
    "_stub_project",
]
