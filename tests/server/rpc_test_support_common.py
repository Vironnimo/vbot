"""Shared lightweight doubles for server RPC tests."""

from __future__ import annotations

import asyncio
import builtins
import json
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar, cast

import pytest

from core.agents import (
    AgentAlreadyExistsError,
    AgentOrderConflictError,
    InvalidAgentOrderError,
    default_workspace_dir,
)
from core.memory import DEFAULT_MEMORY_PROMPT_MODE
from core.models import Capabilities, Model, ModelQuery, ReasoningCapabilities
from core.models.models import ModelRegistry
from core.projects import ProjectNotFoundError
from core.projects.paths import cwd_identity_key
from core.projects.resolver import AgentResolutionError, ConfigAgent
from core.tools.availability import ToolAccess
from server.rpc import (
    connection_methods,
)

JsonObject = dict[str, Any]
SettingsUpdateResult = TypeVar("SettingsUpdateResult")
STUB_SUBAGENT_SETTING_FIELDS = (
    "max_subagent_depth",
    "max_subagents_per_turn",
    "subagent_timeout_minutes",
)


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

    monkeypatch.setattr(connection_methods, "fetch_catalog", _none_catalog)


@dataclass(frozen=True)
class StubAgent:
    id: str
    name: str = "Coder Agent"
    model: str = "openai/gpt-5.2"
    fallback_model: str = ""
    workspace: str = "C:/workspace"
    root_project_id: str | None = None
    temperature: float | None = 0.1
    thinking_effort: str | None = ""
    memory_prompt_mode: str = DEFAULT_MEMORY_PROMPT_MODE
    allowed_tools: list[str] | None = None
    tool_access: ToolAccess | None = None
    allowed_skills: list[str] | None = None
    tools: JsonObject | None = None
    custom_system_prompt_enabled: bool = False
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
        defaults = self._defaults_provider() if self._defaults_provider is not None else {}

        model = agent.model
        fallback_model = agent.fallback_model
        temperature = agent.temperature
        thinking_effort = agent.thinking_effort

        default_model = defaults.get("model")
        if model == "" and isinstance(default_model, str):
            model = default_model

        default_fallback_model = defaults.get("fallback_model")
        if fallback_model == "" and isinstance(default_fallback_model, str):
            fallback_model = default_fallback_model

        default_temperature = defaults.get("temperature")
        if (
            temperature is None
            and isinstance(default_temperature, int | float)
            and not isinstance(default_temperature, bool)
        ):
            temperature = float(default_temperature)

        default_thinking_effort = defaults.get("thinking_effort")
        if thinking_effort is None and isinstance(default_thinking_effort, str):
            thinking_effort = default_thinking_effort

        return StubAgent(
            **{
                **agent.__dict__,
                "model": model,
                "fallback_model": fallback_model,
                "temperature": temperature,
                "thinking_effort": thinking_effort,
            }
        )

    def get(self, agent_id: str) -> StubAgent:
        return self._apply_defaults(self._get_raw(agent_id))

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

    def rename(self, agent_id: str, new_agent_id: str) -> Any:
        if new_agent_id in self._agents:
            raise AgentAlreadyExistsError(f"Agent already exists: {new_agent_id}")
        previous = self._get_raw(agent_id)
        renamed = StubAgent(**{**previous.__dict__, "id": new_agent_id})
        del self._agents[agent_id]
        self._agents[new_agent_id] = renamed
        self._order = [new_agent_id if item == agent_id else item for item in self._order]
        self._order_revision += 1
        return SimpleNamespace(agent=self.get(new_agent_id), previous_agent=previous)

    def restore_rename(self, result: Any) -> None:
        del self._agents[result.agent.id]
        self._agents[result.previous_agent.id] = result.previous_agent
        self._order = [
            result.previous_agent.id if item == result.agent.id else item for item in self._order
        ]
        self._order_revision += 1

    def retarget_allowed_agent_references(
        self,
        old_agent_id: str,
        new_agent_id: str,
    ) -> Any:
        previous_agents: list[StubAgent] = []
        for agent_id, agent in list(self._agents.items()):
            tools = deepcopy(agent.tools or {})
            allowed = tools.get("subagent", {}).get("allowed_agents")
            if not isinstance(allowed, list) or old_agent_id not in allowed:
                continue
            previous_agents.append(agent)
            tools["subagent"]["allowed_agents"] = list(
                dict.fromkeys(new_agent_id if item == old_agent_id else item for item in allowed)
            )
            self._agents[agent_id] = StubAgent(**{**agent.__dict__, "tools": tools})
        return SimpleNamespace(
            previous_agents=tuple(previous_agents),
            agent_ids=tuple(agent.id for agent in previous_agents),
        )

    def restore_allowed_agent_references(self, result: Any) -> None:
        for agent in result.previous_agents:
            self._agents[agent.id] = agent

    def delete(self, agent_id: str) -> Path:
        self._get_raw(agent_id)
        del self._agents[agent_id]
        self._order.remove(agent_id)
        self._order_revision += 1
        return Path("archive") / agent_id


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


class StubAgentResolver:
    """Resolver seam the chat loop calls; identity path delegates to ``StubAgents``.

    ``project_id=None`` returns the same agent ``StubAgents.get`` would (byte-for-byte
    today's identity path); an unknown agent surfaces as :class:`AgentResolutionError`,
    matching the real resolver's failure surface. A set ``project_id`` returns a
    :class:`ConfigAgent` registered via :meth:`register_project_agent`, mirroring the
    real resolver's config path; an unknown project/agent raises
    :class:`AgentResolutionError`.
    """

    def __init__(self, agents: StubAgents) -> None:
        self._agents = agents
        self._project_agents: dict[tuple[str, str], ConfigAgent] = {}

    def register_project_agent(self, project_id: str, agent: ConfigAgent) -> None:
        self._project_agents[(project_id, agent.id)] = agent

    def resolve_agent(self, project_id: str | None, agent_id: str) -> StubAgent | ConfigAgent:
        if project_id is None:
            try:
                return self._agents.get(agent_id)
            except KeyError as error:
                raise AgentResolutionError(str(error)) from error
        try:
            return self._project_agents[(project_id, agent_id)]
        except KeyError as error:
            raise AgentResolutionError(
                f"agent '{agent_id}' is not on project '{project_id}' team"
            ) from error

    def effective_config(self, project_id: str | None, agent_id: str) -> dict[str, dict[str, Any]]:
        """Identity provenance seam the agent payload reads (agent CRUD is identity-only).

        Mirrors the real resolver's identity branch: the agent's raw own value wins
        (source ``agent``) unless it is ``""``/``None``, in which case a global default
        applies (source ``global_default``), else ``None``.
        """
        if project_id is not None:
            raise AgentResolutionError("project effective_config not stubbed")
        try:
            raw = self._agents.get_raw(agent_id)
        except KeyError as error:
            raise AgentResolutionError(str(error)) from error
        defaults = (
            self._agents._defaults_provider() if self._agents._defaults_provider is not None else {}
        )
        return {
            "model": _identity_source(raw.model, defaults, "model", empty=""),
            "fallback_model": _identity_source(
                raw.fallback_model, defaults, "fallback_model", empty=""
            ),
            "temperature": _identity_source(raw.temperature, defaults, "temperature", empty=None),
            "thinking_effort": _identity_source(
                raw.thinking_effort, defaults, "thinking_effort", empty=None
            ),
        }


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

    def reload(
        self,
        resources_dir: Path,
        *,
        runtime_models_dir: Path | None = None,
    ) -> None:
        """Mirror ``ModelRegistry.reload``: swap contents in place from disk.

        Refresh writes new ``<provider>.json`` layer files; an in-place swap keeps
        this instance's identity so holders that captured it (the command
        dispatcher, task targets, recall) see the new catalog. The freshly written
        files are assembled through the real registry and regrouped here.
        """

        ModelRegistry.invalidate(resources_dir, runtime_models_dir=runtime_models_dir)
        loaded = ModelRegistry.load(resources_dir, runtime_models_dir=runtime_models_dir)
        regrouped: dict[str, list[Model]] = {}
        for provider_id, model in loaded.query(ModelQuery.from_filters({})):
            regrouped.setdefault(provider_id, []).append(model)
        self._models = regrouped


class EmptyStubModels(StubModels):
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
