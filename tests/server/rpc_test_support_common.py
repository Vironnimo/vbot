"""Shared lightweight doubles for server RPC tests."""

from __future__ import annotations

import asyncio
import builtins
import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override

import pytest

from core.agents import (
    AgentAlreadyExistsError,
    AgentOrderConflictError,
    AgentReferencedError,
    AgentRename,
    AgentRenameResult,
    ArchivedAgent,
    InvalidAgentOrderError,
    default_workspace_dir,
)
from core.memory import DEFAULT_MEMORY_PROMPT_MODE
from core.models import Capabilities, Model, ModelQuery, ReasoningCapabilities
from core.models.models import ModelRegistry
from core.projects import ProjectNotFoundError
from core.projects.paths import cwd_identity_key
from core.projects.resolver import (
    AgentOverrides,
    AgentResolutionError,
    AgentResolver,
    ConfigAgent,
    ModelConfigurationError,
    ResolutionAgentNotFoundError,
)
from core.sessions import SessionAddress
from core.settings import AgentDefaults, bake_agent_defaults
from core.tools.availability import ToolAccess
from server.rpc import (
    model_methods,
)
from server.rpc.methods import dispatch_rpc

JsonObject = dict[str, Any]


async def call(state: Any, method: str, **params: Any) -> JsonObject:
    """Dispatch one RPC exactly as the transport does and return its envelope."""
    return await dispatch_rpc(state, {"method": method, "params": params})


async def rpc_result(state: Any, method: str, **params: Any) -> JsonObject:
    """Dispatch one RPC that must succeed and return its result."""
    response = await call(state, method, **params)
    assert response["ok"] is True, response
    result: JsonObject = response["result"]
    return result


async def rpc_error(state: Any, method: str, **params: Any) -> JsonObject:
    """Dispatch one RPC that must fail and return its ``{code, message}`` error."""
    response = await call(state, method, **params)
    assert response["ok"] is False, response
    error: JsonObject = response["error"]
    return error


def resource_changes(state: Any, kind: str | None = None) -> list[JsonObject]:
    """Payloads of the ``resource_changed`` events on the state's bus, optionally of one kind."""
    return [
        event["payload"]
        for event in state.event_bus.events
        if event["type"] == "resource_changed"
        and (kind is None or event["payload"]["kind"] == kind)
    ]


@pytest.fixture(autouse=True)
def _no_models_dev_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop ``model.refresh_db`` from hitting the live models.dev endpoint.

    The refresh path fetches the public catalog once for canonical enrichment.
    These RPC tests exercise routing, not projection, so the fetch is stubbed to
    ``None`` (no network, no enrichment); the canonical-layer write is then
    skipped and the refresh result carries ``canonical: None``.
    """

    async def _none_catalog() -> None:
        return None

    monkeypatch.setattr(model_methods, "fetch_catalog", _none_catalog)


@dataclass(frozen=True)
class StubAgent:
    id: str
    name: str = "Coder Agent"
    model: str = "openai/gpt-5.2"
    fallback_models: list[str] = field(default_factory=list)
    workspace: str = "C:/workspace"
    root_project_id: str | None = None
    temperature: float | None = 0.1
    top_p: float | None = None
    thinking_effort: str | None = ""
    memory_prompt_mode: str = DEFAULT_MEMORY_PROMPT_MODE
    allowed_tools: list[str] | None = None
    tool_access: ToolAccess | None = None
    allowed_skills: list[str] | None = None
    excluded_skills: list[str] = field(default_factory=list)
    tools: JsonObject | None = None
    custom_system_prompt_enabled: bool = False
    compaction_policy: JsonObject | None = None
    current_session_id: str = ""
    created_at: str = "2026-05-04T00:00:00Z"
    updated_at: str = "2026-05-04T00:00:00Z"

    def __post_init__(self) -> None:
        if self.allowed_tools is None:
            object.__setattr__(self, "allowed_tools", ["*"])
        if self.tool_access is None:
            allowed_tools = self.allowed_tools or []
            object.__setattr__(
                self,
                "tool_access",
                ToolAccess(mode="all")
                if "*" in allowed_tools
                else ToolAccess(mode="selected", allowed=tuple(allowed_tools)),
            )
        if self.allowed_skills is None:
            object.__setattr__(self, "allowed_skills", ["*"])
        if self.tools is None:
            object.__setattr__(self, "tools", {})


class StubAgents:
    def __init__(
        self,
        agent: StubAgent,
        *,
        defaults_provider: Callable[[], JsonObject] | None = None,
    ) -> None:
        self._agents: dict[str, StubAgent] = {agent.id: agent}
        self._order = [agent.id]
        self._order_revision = 1
        self._defaults_provider = defaults_provider

    def _get_raw(self, agent_id: str) -> StubAgent:
        if agent_id not in self._agents:
            raise KeyError(agent_id)
        return self._agents[agent_id]

    def _apply_defaults(self, agent: StubAgent) -> StubAgent:
        defaults = AgentDefaults.from_dict(
            self._defaults_provider() if self._defaults_provider is not None else {}
        )
        changes = bake_agent_defaults(
            model=agent.model,
            fallback_models=agent.fallback_models,
            temperature=agent.temperature,
            top_p=agent.top_p,
            thinking_effort=agent.thinking_effort,
            defaults=defaults,
        )
        if not changes:
            return agent
        return StubAgent(**{**agent.__dict__, **changes})

    def get(self, agent_id: str) -> StubAgent:
        return self._apply_defaults(self._get_raw(agent_id))

    async def get_async(self, agent_id: str) -> StubAgent:
        # Like the real store: the read runs off the Event Loop.
        return await asyncio.to_thread(self.get, agent_id)

    def get_raw(self, agent_id: str) -> StubAgent:
        return self._get_raw(agent_id)

    def default_workspace(self, agent_id: str) -> str:
        return str(default_workspace_dir(Path("C:/data"), agent_id))

    def list(self) -> list[StubAgent]:
        return [self._apply_defaults(self._agents[agent_id]) for agent_id in self._order]

    def list_with_order(self) -> Any:
        return SimpleNamespace(
            agents=tuple(self.list()),
            order_revision=self._order_revision,
            order_changed=False,
        )

    def reorder(self, agent_ids: builtins.list[str], *, expected_revision: int) -> Any:
        if len(agent_ids) != len(set(agent_ids)):
            raise InvalidAgentOrderError("agent_ids must not contain duplicates")
        if set(agent_ids) != set(self._order):
            raise AgentOrderConflictError(
                "Identity Agent roster changed; reload it before reordering",
                current_revision=self._order_revision,
            )
        if agent_ids == self._order:
            return self.list_with_order()
        if expected_revision != self._order_revision:
            raise AgentOrderConflictError(
                "Identity Agent order changed; reload it before reordering",
                current_revision=self._order_revision,
            )
        self._order = list(agent_ids)
        self._order_revision += 1
        return SimpleNamespace(
            agents=tuple(self.list()),
            order_revision=self._order_revision,
            order_changed=True,
        )

    def create(self, agent_id: str, name: str | None = None, **changes: Any) -> StubAgent:
        agent = StubAgent(id=agent_id, name=name or agent_id, **changes)
        self._agents[agent_id] = agent
        self._order.append(agent_id)
        self._order_revision += 1
        return self._get_raw(agent_id)

    def update(self, agent_id: str, **changes: Any) -> StubAgent:
        agent = self._get_raw(agent_id)
        if "name" in changes and not changes["name"]:
            changes["name"] = agent_id
        updated = StubAgent(**{**agent.__dict__, **changes})
        self._agents[agent_id] = updated
        return self.get(agent_id)

    def update_with_metadata(
        self,
        agent_id: str,
        *,
        copy_workspace_identity_files: bool = False,
        **changes: Any,
    ) -> Any:
        del copy_workspace_identity_files
        return SimpleNamespace(
            agent=self.update(agent_id, **changes),
            copied_files=(),
            backed_up_files=(),
            backup_dir=None,
        )

    def rename(
        self, agent_id: str, new_agent_id: str, *, external_references: Iterable[str] = ()
    ) -> AgentRenameResult:
        if new_agent_id in self._agents:
            raise AgentAlreadyExistsError(f"Agent already exists: {new_agent_id}")
        self._get_raw(agent_id)
        if references := tuple(external_references):
            raise AgentReferencedError(new_agent_id, references)
        rename = AgentRename(source_id=agent_id, target_id=new_agent_id)
        self._move(rename)
        return AgentRenameResult(rename=rename, agent=cast(Any, self.get(new_agent_id)))

    def revert_rename(self, rename: AgentRename) -> AgentRename:
        reverse = rename.reversed()
        self._move(reverse)
        return reverse

    def finish_rename(self, _rename: AgentRename) -> None:
        return None

    def _move(self, rename: AgentRename) -> None:
        agent = self._agents.pop(rename.source_id)
        self._agents[rename.target_id] = StubAgent(**{**agent.__dict__, "id": rename.target_id})
        self._order = [
            rename.target_id if item == rename.source_id else item for item in self._order
        ]
        self._order_revision += 1

    @contextmanager
    def archive_files(self, agent_id: str, _tree: Path) -> Iterator[ArchivedAgent]:
        agent = self._get_raw(agent_id)
        index = self._order.index(agent_id)
        del self._agents[agent_id]
        self._order.remove(agent_id)
        try:
            yield ArchivedAgent(
                agent=cast(Any, agent),
                roster_index=index,
                workspace=self.default_workspace(agent_id),
                workspace_external=False,
            )
        except BaseException:
            self._agents[agent_id] = agent
            self._order.insert(index, agent_id)
            raise
        self._order_revision += 1

    def remove_delegation_grants(
        self, _agent_id: str, _record: Callable[[builtins.list[Mapping[str, Any]]], None]
    ) -> tuple[str, ...]:
        # The doubles keep no delegation lists; AgentStore tests own that behavior.
        return ()


@dataclass(frozen=True)
class StubProject:
    """Minimal project entity for the prompt-preview project path."""

    project_id: str
    display_name: str
    cwd: str
    auto_load: tuple[str, ...] = ()


class StubProjects:
    """``runtime.projects`` slice the prompt-preview path reads (cwd + auto-load)."""

    def __init__(self, *projects: StubProject) -> None:
        self._projects = {project.project_id: project for project in projects}

    def add(self, project: StubProject) -> None:
        self._projects[project.project_id] = project

    def get(self, project_id: str) -> StubProject:
        try:
            return self._projects[project_id]
        except KeyError as error:
            raise ProjectNotFoundError(f"project not found: {project_id}") from error

    def exists(self, project_id: str) -> bool:
        return project_id in self._projects

    def find_by_cwd(self, cwd: object) -> StubProject | None:
        # Mirror the real store: match on the cwd-identity key so the rooted-agent
        # detection in the preview path behaves like production.
        try:
            target = cwd_identity_key(cast(str, cwd))
        except ValueError:
            return None
        for project_id in sorted(self._projects):
            project = self._projects[project_id]
            if cwd_identity_key(project.cwd) == target:
                return project
        return None


class StubModelChecker:
    """Every Model can run except those in ``unusable``."""

    def __init__(self) -> None:
        self.unusable: set[str] = set()

    def require_configured(self, model: str) -> None:
        if model in self.unusable:
            raise ModelConfigurationError(f"model is not usable in this instance: {model}")


class StubAgentResolver:
    """Resolver seam the chat loop calls; identity path delegates to ``StubAgents``.

    ``project_id=None`` returns the same agent ``StubAgents.get`` would (byte-for-byte
    today's identity path); an unknown agent surfaces as
    :class:`ResolutionAgentNotFoundError`, matching the real resolver's failure
    surface. A set ``project_id`` returns a :class:`ConfigAgent` registered via
    :meth:`register_project_agent`, mirroring the real resolver's config path; an
    unregistered Team member raises :class:`ResolutionAgentNotFoundError`.

    Session Agent overrides are the real resolver's, stored in ``sessions``; a
    resolution or provenance read that names its Session applies them.
    """

    def __init__(self, agents: StubAgents, sessions: Any | None = None) -> None:
        self._agents = agents
        self._project_agents: dict[tuple[str, str], ConfigAgent] = {}
        self.models = StubModelChecker()
        self._overrides = AgentResolver(
            cast(Any, agents), cast(Any, None), cast(Any, self.models), dict, sessions=sessions
        )

    def register_project_agent(self, project_id: str, agent: ConfigAgent) -> None:
        self._project_agents[(project_id, agent.id)] = agent

    def resolve_agent(
        self, project_id: str | None, agent_id: str, *, session_id: str | None = None
    ) -> StubAgent | ConfigAgent:
        agent: StubAgent | ConfigAgent
        if project_id is None:
            try:
                agent = self._agents.get(agent_id)
            except KeyError as error:
                raise ResolutionAgentNotFoundError(str(error)) from error
        else:
            try:
                agent = self._project_agents[(project_id, agent_id)]
            except KeyError as error:
                raise ResolutionAgentNotFoundError(
                    f"agent '{agent_id}' is not on project '{project_id}' team"
                ) from error
        if session_id is None:
            return agent
        overrides = self.session_overrides(SessionAddress(project_id, agent_id, session_id))
        return replace(agent, **overrides.as_dict())

    async def resolve_agent_async(
        self, project_id: str | None, agent_id: str, **options: Any
    ) -> Any:
        return self.resolve_agent(project_id, agent_id, **options)

    def require_model_configured(self, model: str) -> None:
        self.models.require_configured(model)

    async def require_model_configured_async(self, model: str) -> None:
        self.models.require_configured(model)

    def session_overrides(self, address: SessionAddress) -> AgentOverrides:
        return self._overrides.session_overrides(address)

    async def session_overrides_async(self, address: SessionAddress) -> AgentOverrides:
        return await self._overrides.session_overrides_async(address)

    def update_session_overrides(
        self, address: SessionAddress, changes: Mapping[str, Any]
    ) -> AgentOverrides:
        return self._overrides.update_session_overrides(address, changes)

    async def update_session_overrides_async(
        self, address: SessionAddress, changes: Mapping[str, Any]
    ) -> AgentOverrides:
        return await self._overrides.update_session_overrides_async(address, changes)

    def effective_config(
        self, project_id: str | None, agent_id: str, *, session_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """Identity provenance seam the agent payload reads (agent CRUD is identity-only).

        Mirrors the real resolver's identity branch: the agent's raw own value wins
        (source ``agent``) unless it is ``""``/``None``, in which case a global default
        applies (source ``global_default``), else ``None``. A Session's overrides win
        with source ``session``.
        """
        if project_id is not None:
            raise AgentResolutionError("project effective_config not stubbed")
        try:
            raw = self._agents.get_raw(agent_id)
        except KeyError as error:
            raise ResolutionAgentNotFoundError(str(error)) from error
        defaults = (
            self._agents._defaults_provider() if self._agents._defaults_provider is not None else {}
        )
        effective = {
            "model": _identity_source(raw.model, defaults, "model", empty=""),
            "fallback_models": _identity_source(
                raw.fallback_models,
                defaults,
                "fallback_models",
                empty=[],
            ),
            "temperature": _identity_source(raw.temperature, defaults, "temperature", empty=None),
            "thinking_effort": _identity_source(
                raw.thinking_effort, defaults, "thinking_effort", empty=None
            ),
        }
        if session_id is not None:
            overrides = self.session_overrides(SessionAddress(project_id, agent_id, session_id))
            for name, value in overrides.as_dict().items():
                effective[name] = {"value": value, "source": "session"}
        return effective


def _identity_source(
    own_value: object, defaults: JsonObject, key: str, *, empty: object
) -> dict[str, object]:
    """Helper for :meth:`StubAgentResolver.effective_config`: own → global → none."""
    if own_value != empty:
        return {"value": own_value, "source": "agent"}
    if key in defaults and defaults.get(key) is not None:
        return {"value": defaults.get(key), "source": "global_default"}
    return {"value": None, "source": None}


class InstrumentedAgentDeleteLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.attempts = 0
        self.active = 0
        self.max_active = 0
        self._second_attempt = asyncio.Event()

    async def __aenter__(self) -> None:
        self.attempts += 1
        if self.attempts == 2:
            self._second_attempt.set()
        if self.attempts == 1:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._second_attempt.wait(), timeout=1)
        await self._lock.acquire()
        self.active += 1
        self.max_active = max(self.max_active, self.active)

    async def __aexit__(self, *_exc_info: object) -> None:
        self.active -= 1
        self._lock.release()


class StubProviders:
    def __init__(self) -> None:
        self._providers: dict[str, Any] = {
            "anthropic": SimpleNamespace(
                id="anthropic",
                name="Anthropic",
                base_url="https://api.anthropic.com/v1",
                connections=[
                    SimpleNamespace(
                        id="api-key",
                        type="api_key",
                        label="API Key",
                        auth=SimpleNamespace(credential_key="ANTHROPIC_API_KEY"),
                    )
                ],
            ),
            "openai": SimpleNamespace(
                id="openai",
                name="OpenAI",
                adapter="openai_compatible",
                base_url="https://api.openai.com/v1",
                defaults={"max_tokens": 4096},
                extra_headers={},
                models_endpoint=None,
                context_window=None,
                connections=[
                    SimpleNamespace(
                        id="oauth",
                        type="oauth",
                        label="OAuth",
                        auth=SimpleNamespace(credential_key="OPENAI_OAUTH_TOKEN"),
                    ),
                    SimpleNamespace(
                        id="api-key",
                        type="api_key",
                        label="API Key",
                        auth=SimpleNamespace(credential_key="OPENAI_API_KEY"),
                    ),
                ],
            ),
            "ollama": SimpleNamespace(
                id="ollama",
                name="Ollama",
                adapter="openai_compatible",
                base_url="",
                defaults={"max_tokens": 4096},
                extra_headers={},
                models_endpoint=None,
                connections=[
                    SimpleNamespace(
                        id="api-key",
                        type="api_key",
                        label="API Key",
                        auth=SimpleNamespace(credential_key="OLLAMA_API_KEY"),
                    )
                ],
            ),
        }

    def get(self, provider_id: str) -> object:
        if provider_id not in self._providers:
            raise KeyError(provider_id)
        return self._providers[provider_id]

    def add(self, provider: object) -> None:
        self._providers[cast(Any, provider).id] = provider

    def list_ids(self) -> list[str]:
        return sorted(self._providers)


class StubModels:
    def pricing_for(self, model_reference: str) -> None:
        return None

    def __init__(self) -> None:
        self._models = {
            "anthropic": [
                Model(
                    model_id="claude-sonnet-4-20250219",
                    name="Claude Sonnet 4",
                    capabilities=Capabilities(
                        vision=True,
                        tools=True,
                        json_mode=False,
                        reasoning=ReasoningCapabilities(supported=True),
                    ),
                    context_window=200000,
                    max_output_tokens=64000,
                )
            ],
            "openai": [
                Model(
                    model_id="gpt-4.1-mini",
                    name="GPT-4.1 mini",
                    capabilities=Capabilities(
                        vision=False,
                        tools=True,
                        json_mode=True,
                        reasoning=ReasoningCapabilities(supported=False),
                    ),
                    context_window=128000,
                    max_output_tokens=16000,
                ),
                Model(
                    model_id="gpt-5.2",
                    name="GPT-5.2",
                    capabilities=Capabilities(
                        vision=True,
                        tools=True,
                        json_mode=True,
                        reasoning=ReasoningCapabilities(supported=True),
                    ),
                    context_window=256000,
                    max_output_tokens=32000,
                ),
            ],
            "ollama": [
                Model(
                    model_id="llama3.2",
                    name="Llama 3.2",
                    capabilities=Capabilities(
                        vision=False,
                        tools=True,
                        json_mode=False,
                        reasoning=ReasoningCapabilities(supported=False),
                    ),
                    context_window=128000,
                    max_output_tokens=8192,
                )
            ],
        }

    def get(self, provider_id: str, model_id: str) -> Model:
        for model in self._models.get(provider_id, []):
            if model.model_id == model_id:
                return model
        raise KeyError(f"Model not found: {provider_id}/{model_id}")

    def list_for_provider(self, provider_id: str) -> list[object]:
        return list(self._models[provider_id])

    def query(self, model_query: ModelQuery) -> list[tuple[str, Model]]:
        provider_filter = model_query.provider_id
        matches: list[tuple[str, Model]] = []
        for provider_id, models in self._models.items():
            if provider_filter and provider_id != provider_filter:
                continue
            for model in models:
                if model_query.matches(model):
                    matches.append((provider_id, model))
        return sorted(matches, key=lambda item: (item[0], item[1].model_id))

    async def reload_async(
        self,
        resources_dir: Path,
        *,
        runtime_models_dir: Path | None = None,
        custom_providers: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """Mirror ``ModelRegistry.reload_async``: swap contents in place from disk.

        Refresh writes new ``<provider>.json`` layer files; an in-place swap keeps
        this instance's identity so holders that captured it (the command
        dispatcher, task targets, recall) see the new catalog. The freshly written
        files are assembled through the real registry and regrouped here.
        """

        ModelRegistry.invalidate(resources_dir, runtime_models_dir=runtime_models_dir)
        loaded = ModelRegistry.load(
            resources_dir,
            runtime_models_dir=runtime_models_dir,
            custom_providers=custom_providers,
        )
        regrouped: dict[str, list[Model]] = {}
        for provider_id, model in loaded.query(ModelQuery.from_filters({})):
            regrouped.setdefault(provider_id, []).append(model)
        self._models = regrouped


class EmptyStubModels(StubModels):
    @override
    def list_for_provider(self, provider_id: str) -> list[object]:
        if provider_id not in self._models:
            return []
        return super().list_for_provider(provider_id)


def openrouter_provider() -> SimpleNamespace:
    return SimpleNamespace(
        id="openrouter",
        name="OpenRouter",
        adapter="openai_compatible",
        base_url="https://openrouter.ai/api/v1",
        defaults={"max_tokens": 8192},
        extra_headers={"X-Title": "vBot"},
        models_endpoint="/models",
        connections=[
            SimpleNamespace(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=SimpleNamespace(credential_key="OPENROUTER_API_KEY"),
            )
        ],
    )


def openrouter_provider_with_secondary_connection() -> SimpleNamespace:
    provider = openrouter_provider()
    provider.connections = [
        SimpleNamespace(
            id="oauth",
            type="oauth",
            label="OAuth",
            auth=SimpleNamespace(credential_key="OPENROUTER_OAUTH_TOKEN"),
        ),
        SimpleNamespace(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=SimpleNamespace(credential_key="OPENROUTER_API_KEY"),
        ),
    ]
    return provider


async def fake_refresh_models(
    provider_config: Any,
    credential_value: str,
    resources_dir: Path,
    **kwargs: Any,
) -> JsonObject:
    FAKE_REFRESH_MODEL_PROVIDER_IDS.append(provider_config.id)
    FAKE_REFRESH_MODEL_CALLS.append(credential_value)
    FAKE_REFRESH_MODEL_KWARGS.append(kwargs)
    models_dir = resources_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    models_dir.joinpath(f"{provider_config.id}.json").write_text(
        json.dumps(
            {
                "provider_id": provider_config.id,
                "source": "discovery",
                "fetched_at": "2026-05-08T19:08:00+00:00",
                "models": {
                    "fresh-model": {
                        "name": "Fresh Model",
                        "capabilities": {
                            "vision": True,
                            "tools": True,
                            "json_mode": True,
                            "reasoning": {"supported": True},
                        },
                        "context_window": 128000,
                        "max_output_tokens": 8192,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    ModelRegistry.invalidate(resources_dir)
    return {
        "provider_id": provider_config.id,
        "model_count": 1,
        "fetched_at": "2026-05-08T19:08:00+00:00",
    }


FAKE_REFRESH_MODEL_CALLS: list[str] = []
FAKE_REFRESH_MODEL_KWARGS: list[JsonObject] = []
FAKE_REFRESH_MODEL_PROVIDER_IDS: list[str] = []
