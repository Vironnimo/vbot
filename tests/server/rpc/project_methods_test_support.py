"""Shared fixtures and fakes for project methods behavior tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from core.agents.agents import AgentStore
from core.projects.projects import PROJECT_DEFAULT_ALLOWED_TOOLS
from core.projects.resolver import (
    AgentResolver,
    ModelConfigurationChecker,
)
from core.projects.scanners.opencode import OPENCODE_AGENTS_SUBPATH
from core.projects.store import ProjectStore
from core.runs import ChatRunManager
from core.sessions import ChatSessionManager
from core.sessions.format import write_bootstrap_marker


# ---------------------------------------------------------------------------
# Fakes for the model/provider/credential surface the resolver's checker probes.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _FakeConnection:
    id: str


@dataclass(frozen=True)
class _FakeProviderConfig:
    connections: list[_FakeConnection]


class _FakeCatalogModel:
    """Catalog-model stub with no connection allowlist (every connection allowed)."""

    connections: tuple[str, ...] = ()

    def allows_connection(self, connection_id: str) -> bool:
        return True


class _FakeModels:
    def __init__(self, known: set[tuple[str, str]]) -> None:
        self._known = known

    def get(self, provider_id: str, model_id: str) -> _FakeCatalogModel:
        if (provider_id, model_id) not in self._known:
            raise KeyError(f"{provider_id}/{model_id}")
        return _FakeCatalogModel()


class _FakeProviders:
    def __init__(self, providers: dict[str, _FakeProviderConfig]) -> None:
        self._providers = providers

    def get(self, provider_id: str) -> _FakeProviderConfig:
        if provider_id not in self._providers:
            raise KeyError(provider_id)
        return self._providers[provider_id]


class _FakeCredentials:
    def __init__(self, usable: set[str]) -> None:
        self._usable = usable

    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
        target = connection_id if connection_id is not None else provider_id
        return target in self._usable

    def is_usable(self, provider_id: str, connection_id: str | None = None) -> bool:
        return self.has_credentials(provider_id, connection_id)


class _FakeTools:
    """Minimal live Tool Registry catalog for Project whitelist tests."""

    def __init__(self, names: set[str]) -> None:
        self.names = names

    def list_tools(
        self,
        *,
        include_session_scoped: bool = True,
        include_catalog_hidden: bool = True,
    ) -> list[SimpleNamespace]:
        del include_session_scoped, include_catalog_hidden
        return [
            SimpleNamespace(
                name=name,
                activation="memory_mode" if name == "memory" else "configurable",
                constraints=("identity_agent",) if name in {"project", "skill_manage"} else (),
            )
            for name in sorted(self.names)
        ]


class _FakeTerminalManager:
    def __init__(self) -> None:
        self.closed_projects: list[str] = []

    async def close_project_scope(self, project_id: str) -> None:
        self.closed_projects.append(project_id)


def _openai_configured() -> ModelConfigurationChecker:
    return ModelConfigurationChecker(
        _FakeModels({("openai", "gpt-5.2"), ("openai", "gpt-mini")}),
        _FakeProviders({"openai": _FakeProviderConfig([_FakeConnection("api-key")])}),
        _FakeCredentials({"openai:api-key"}),
    )


# ---------------------------------------------------------------------------
# Repo + state scaffolding.
# ---------------------------------------------------------------------------
def _write_agent(
    repo: Path,
    filename: str,
    *,
    model: str = "openai/gpt-5.2",
    reasoning_effort: str | None = None,
    permission: dict[str, str] | None = None,
) -> None:
    agents_dir = repo.joinpath(*OPENCODE_AGENTS_SUBPATH)
    agents_dir.mkdir(parents=True, exist_ok=True)
    lines = ["description: An agent."]
    if model:
        lines.append(f"model: {model}")
    if reasoning_effort is not None:
        lines.append(f"reasoningEffort: {reasoning_effort}")
    if permission:
        lines.append("permission:")
        lines.extend(f"  {key}: {value}" for key, value in permission.items())
    front = "\n".join(lines) + "\n"
    (agents_dir / filename).write_text(f"---\n{front}---\nBody.\n", encoding="utf-8")


def _make_repo(tmp_path: Path, name: str, *agents: str) -> Path:
    repo = tmp_path / "repos" / name
    repo.mkdir(parents=True)
    for agent in agents:
        _write_agent(repo, agent)
    return repo


def _make_state(
    tmp_path: Path,
    *,
    cron_jobs: list | None = None,
    bootstrap_jobs: list | None = None,
) -> SimpleNamespace:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_bootstrap_marker(data_dir)
    sessions = ChatSessionManager(data_dir)
    projects = ProjectStore(data_dir, sessions=sessions)
    agents = AgentStore(data_dir, sessions=sessions)
    resolver = AgentResolver(
        agents=agents,
        projects=projects,
        model_checker=_openai_configured(),
        global_agent_defaults=lambda: {},
    )
    chat_runs = ChatRunManager()
    cron_service = SimpleNamespace(list_jobs=lambda: list(cron_jobs or []))
    bootstrap_service = SimpleNamespace(list_jobs=lambda: list(bootstrap_jobs or []))
    runtime = SimpleNamespace(
        projects=projects,
        agents=agents,
        sessions=sessions,
        agent_resolver=resolver,
        terminal_manager=_FakeTerminalManager(),
        cron_service=cron_service,
        bootstrap_service=bootstrap_service,
        # ``project.set_override``'s model gate reads ``runtime.models`` only for a pinned
        # ``::connection`` suffix (never in these tests), but expose it so a plain
        # model override never trips an AttributeError.
        models=_FakeModels({("openai", "gpt-5.2"), ("openai", "gpt-mini")}),
        # Mirrors tool.list's registered normal catalog. ``memory`` and
        # ``skill_manage`` are registered but intentionally not Project-eligible.
        tools=_FakeTools(
            {
                *PROJECT_DEFAULT_ALLOWED_TOOLS,
                "memory",
                "skill_manage",
            }
        ),
    )
    return SimpleNamespace(runtime=runtime, chat_runs=chat_runs)
