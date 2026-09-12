"""Runtime integration for owner-bound Extension operations and temporary Runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from core.agents.agents import AgentStore, _normalize_agent_tools
from core.agents.temporary import (
    ExecutionResources,
    TemporaryAgentConfig,
    TemporaryAgentRegistry,
    TemporaryExecutionGroups,
)
from core.chat import ChatLoop
from core.extensions import ExtensionRegistry
from core.extensions.operations import ExtensionHost
from core.models.models import ModelRegistry
from core.models.query import ModelQuery
from core.projects import AgentResolver, ProjectStore
from core.projects.resolver import effective_project_allowed_skills
from core.prompts import SystemPromptManager
from core.prompts.prompts import ProjectPromptContext
from core.runs import ChatRunManager
from core.runtime._workers import _RUNTIME_WORKERS
from core.runtime.interfaces import (
    LoggerProtocol,
    ProviderCredentialResolverProtocol,
)
from core.sessions import ChatSessionManager
from core.skills.skills import SkillRegistry
from core.statistics import StatisticsService
from core.tools import (
    resolve_tool_access,
    tool_is_ready,
)
from core.tools.availability import (
    BASH_ALLOWED_ENV_KEY,
    BASH_TOOL_SETTINGS_KEY,
    SUBAGENT_ALLOWED_AGENTS_KEY,
    SUBAGENT_TOOL_SETTINGS_KEY,
)
from core.tools.tools import ToolRegistry


def _temporary_config_from_binding(binding: Any) -> TemporaryAgentConfig:
    """Decode one retained temporary descriptor through its canonical DTO."""

    raw = getattr(binding, "config", None)
    if not isinstance(raw, Mapping):
        raise RuntimeError("temporary execution configuration is invalid")
    try:
        return TemporaryAgentConfig(
            model=raw["model"],
            cwd=Path(raw["cwd"]),
            tool_access=raw["tool_access"],
            allowed_skills=raw["allowed_skills"],
            tools=raw["tools"],
            name=raw["name"],
            temperature=raw.get("temperature"),
            thinking_effort=raw.get("thinking_effort"),
            fallback_models=raw.get("fallback_models"),
            instructions=raw.get("instructions", ""),
            prompt_blocks=raw.get("prompt_blocks"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("temporary execution configuration is invalid") from error


def _validate_temporary_tool_configuration(
    config: TemporaryAgentConfig,
    ordinary_tools: Mapping[str, Any],
) -> None:
    """Apply the shared Identity-Agent settings grammar to temporary profiles."""

    try:
        _normalize_agent_tools(config.tools)
    except ValueError as error:
        raise RuntimeError("temporary Tool settings are invalid") from error

    requested = set(config.tool_access.denied) | set(config.tool_access.granted)
    if config.tool_access.mode == "selected":
        requested.update(config.tool_access.allowed)
    requested.update(config.tools)
    unknown = requested - set(ordinary_tools)
    if unknown:
        raise RuntimeError("temporary Tool configuration refers to an unavailable Tool")


def _validate_temporary_project_ceiling(
    config: TemporaryAgentConfig,
    allowed_tools: set[str],
) -> None:
    """Reject explicit temporary Tool choices outside their selected Project."""

    requested = set(config.tool_access.denied) | set(config.tool_access.granted)
    if config.tool_access.mode == "selected":
        requested.update(config.tool_access.allowed)
    requested.update(config.tools)
    if not requested <= allowed_tools:
        raise RuntimeError("temporary Tool configuration is outside its Project ceiling")


class ExtensionHostFactory:
    """Own temporary execution groups and Extension catalog/inspection capabilities."""

    def __init__(
        self,
        *,
        host: ExtensionHost,
        ensure_started: Callable[[], None],
        agent_resolver: AgentResolver,
        chat_loop: ChatLoop,
        chat_run_manager: ChatRunManager,
        temporary_agents: TemporaryAgentRegistry,
        projects: ProjectStore,
        agents: AgentStore,
        sessions: ChatSessionManager,
        tools: ToolRegistry,
        models: ModelRegistry,
        provider_credentials: ProviderCredentialResolverProtocol,
        system_prompts: SystemPromptManager,
        get_registry: Callable[[], ExtensionRegistry | None],
        get_skills: Callable[[], SkillRegistry],
        skills_for: Callable[[str | None, str | None], SkillRegistry],
        project_skill_names: Callable[[str | None], frozenset[str]],
        resources: Sequence[ExecutionResources],
        get_change_publisher: Callable[[], Callable[[str, str, Sequence[str], int], None] | None],
        logger: LoggerProtocol | None,
    ) -> None:
        self._host = host
        self._ensure_started = ensure_started
        self.agent_resolver = agent_resolver
        self.chat_loop = chat_loop
        self.chat_run_manager = chat_run_manager
        self._temporary_agents = temporary_agents
        self.projects = projects
        self.agents = agents
        self.chat_sessions = sessions
        self.tools = tools
        self.models = models
        self.provider_credentials = provider_credentials
        self.system_prompts = system_prompts
        self._get_registry = get_registry
        self._get_skills = get_skills
        self.skills_for = skills_for
        self.project_skill_names = project_skill_names
        self._resources = resources
        self._get_change_publisher = get_change_publisher
        self.logger = logger
        self._temporary_groups: list[TemporaryExecutionGroups] = []
        self._statistics_service: StatisticsService | None = None

    @property
    def _extensions(self) -> ExtensionRegistry | None:
        return self._get_registry()

    def make_host(self) -> ExtensionHost:
        self._ensure_started()
        return replace(self._host, for_owner=self._extension_owner_host)

    def _extension_owner_host(self, identity: Any) -> ExtensionHost:
        if self._extensions is None:
            raise RuntimeError("temporary execution is unavailable")
        self._ensure_started()
        groups = TemporaryExecutionGroups(
            self._temporary_agents,
            self.chat_loop,
            lambda candidate: (
                self._extensions is not None and self._extensions.is_registration_current(candidate)
            ),
            identity,
            run_manager=self.chat_run_manager,
            resources=self._resources,
            validate_binding=self._validate_extension_session_binding,
            usage=lambda group_id, query: self._extension_group_usage(
                identity.name, group_id, query
            ),
        )
        self._temporary_groups.append(groups)
        state_dir = self._host.data_dir / "extension-data" / identity.name
        state_dir.mkdir(parents=True, exist_ok=True)
        return replace(
            self._host,
            for_owner=lambda _identity: self._extension_owner_host(identity),
            temporary_agents=groups,
            state_dir=state_dir,
            catalog=lambda: self._extension_catalog(identity),
            inspect_prompt=lambda config, project_id: self._inspect_extension_prompt(
                identity, config, project_id
            ),
            publish_change=lambda resource, ids, revision: self._publish_extension_change(
                identity, resource, ids, revision
            ),
        )

    def _validate_temporary_admission(self, address: Any, admission: Any) -> None:
        if admission.owner is None:
            return
        matching = [groups for groups in self._temporary_groups if groups.owns(admission.owner)]
        if len(matching) != 1:
            from core.runs import RunAdmissionBlockedError

            raise RunAdmissionBlockedError(
                "This Session is no longer available. Check its state through its Extension."
            )
        matching[0].validate(address, admission)

    async def _validate_extension_session_binding(self, binding: Any) -> None:
        registry = self._extensions
        if registry is None:
            raise RuntimeError("temporary execution is unavailable")
        capability = registry.session_capability(binding, self.tools)
        if capability is None:
            raise RuntimeError("temporary execution is unavailable")
        try:
            await _RUNTIME_WORKERS.run(
                self._validate_extension_session_binding_blocking,
                binding,
                capability.tool_names,
            )
        except Exception as error:
            if self.logger is not None:
                self.logger.warning(
                    "Temporary Session preflight failed owner=%s group=%s participant=%s: %s",
                    getattr(binding, "owner_name", ""),
                    getattr(binding, "group_id", ""),
                    getattr(binding, "participant_id", ""),
                    error,
                )
            raise RuntimeError("temporary execution is unavailable") from error
        if (
            self._extensions is not registry
            or not registry.is_registration_current(capability.identity)
            or registry.session_capability(binding, self.tools) is None
        ):
            raise RuntimeError("temporary execution is unavailable")

    def _validate_extension_session_binding_blocking(
        self,
        binding: Any,
        session_tool_grants: Sequence[str],
    ) -> None:
        config = _temporary_config_from_binding(binding)
        if not config.cwd.is_dir():
            raise RuntimeError("temporary working directory is unavailable")
        self.agent_resolver.require_model_configured(config.model)
        for fallback in config.fallback_models or ():
            self.agent_resolver.require_model_configured(fallback)

        ordinary_tools = {
            tool.name: tool
            for tool in self.tools.list_tools(include_internal=False)
            if not tool.session_scoped
        }
        _validate_temporary_tool_configuration(config, ordinary_tools)
        project_id = getattr(binding.address, "project_id", None)
        if project_id is not None:
            project = self.projects.get(project_id)
            if config.cwd.resolve() != Path(project.cwd).resolve():
                raise RuntimeError("temporary working directory is outside its Project")
            _validate_temporary_project_ceiling(config, set(project.allowed_tools))

        resolved = self.agent_resolver.resolve_temporary_agent(
            binding.address,
            generation_id=binding.generation_id,
        )
        available = self.tools.list_tools(include_internal=False)
        effective = resolve_tool_access(
            resolved.tool_access,
            available,
            resolved.memory_prompt_mode,
            workspace=resolved.workspace,
            session_tool_grants=session_tool_grants,
        )
        effective_by_name = {tool.name: tool for tool in available}
        if any(
            not tool_is_ready(effective_by_name[name]) for name in effective.allowed_tools
        ) or set(effective.session_tool_grants) != set(session_tool_grants):
            raise RuntimeError("temporary execution is unavailable")

    async def _extension_group_usage(
        self,
        owner_name: str,
        group_id: str,
        query: dict[str, Any],
    ) -> dict[str, Any]:
        if self._statistics_service is None:
            self._ensure_started()
            self._statistics_service = StatisticsService(
                self.chat_sessions,
                cast(Any, self.agents),
                cast(Any, self.projects),
            )
        return await self._statistics_service.group_usage(
            owner_name=owner_name,
            group_id=group_id,
            query=query,
        )

    def _publish_extension_change(
        self,
        identity: Any,
        resource: str,
        ids: Sequence[str],
        revision: int,
    ) -> None:
        if (
            self._extensions is None
            or not self._extensions.is_registration_current(identity)
            or not isinstance(resource, str)
            or not resource
            or not isinstance(revision, int)
            or revision < 0
            or not all(isinstance(item, str) and item for item in ids)
        ):
            raise ValueError("extension change is unavailable")
        publisher = self._get_change_publisher()
        if publisher is not None:
            publisher(identity.name, resource, tuple(ids), revision)

    async def _extension_catalog(self, identity: Any) -> dict[str, Any]:
        if self._extensions is None or not self._extensions.is_registration_current(identity):
            raise ValueError("Extension registration is no longer current")
        catalog = await _RUNTIME_WORKERS.run(self._extension_catalog_projection)
        if self._extensions is None or not self._extensions.is_registration_current(identity):
            raise ValueError("Extension registration is no longer current")
        return catalog

    async def _inspect_extension_prompt(
        self, identity: Any, config: TemporaryAgentConfig, project_id: str | None
    ) -> dict[str, Any]:
        if self._extensions is None or not self._extensions.is_registration_current(identity):
            raise ValueError("Extension registration is no longer current")
        agent = await _RUNTIME_WORKERS.run(
            self.agent_resolver.preview_temporary_agent, config, project_id
        )
        record = next(item for item in self._extensions.records() if item.name == identity.name)
        grants = tuple(tool.name for tool in record.declarations.tools if tool.session_scoped)
        definitions = await self.chat_loop.preview_tool_definitions(
            agent, session_tool_grants=grants
        )
        result = await _RUNTIME_WORKERS.run(
            self._extension_prompt_projection, config, project_id, agent, definitions, grants
        )
        if self._extensions is None or not self._extensions.is_registration_current(identity):
            raise ValueError("Extension registration is no longer current")
        return result

    def _extension_prompt_projection(
        self,
        config: TemporaryAgentConfig,
        project_id: str | None,
        agent: Any,
        definitions: list[dict[str, Any]],
        grants: tuple[str, ...],
    ) -> dict[str, Any]:
        self._ensure_started()
        project = self.projects.get(project_id) if project_id else None
        context = (
            ProjectPromptContext.from_project(
                project.project_id, project.display_name, project.cwd, project.auto_load
            )
            if project is not None
            else None
        )
        blocks: list[dict[str, Any]] = []
        text = self.system_prompts.build_system_prompt(
            agent,
            agent_body=config.instructions,
            project_context=context,
            agent_project_id=project_id,
            skill_registry=self.skills_for(project_id, None),
            effective_tool_names=[item["name"] for item in definitions],
            session_tool_grants=grants,
            block_details=blocks,
        )
        return {"text": text, "blocks": blocks, "tools": definitions}

    def _extension_catalog_projection(self) -> dict[str, Any]:
        self._ensure_started()
        projects = self.projects.list()
        skill_choices = [
            {"name": skill.name, "description": skill.description}
            for skill in self._get_skills().list_all()
        ]
        return {
            "projects": [
                {
                    "id": project.project_id,
                    "name": project.display_name,
                    "cwd": project.cwd,
                    "allowed_tools": list(project.allowed_tools),
                    "allowed_skills": effective_project_allowed_skills(
                        project, self.project_skill_names(project.project_id)
                    ),
                }
                for project in projects
            ],
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "constraints": list(tool.constraints),
                    "requires_opt_in": tool.requires_opt_in,
                    "family": tool.family,
                    "family_label": tool.family_label,
                    "activation": tool.activation,
                    "activation_source": tool.activation_source,
                }
                for tool in self.tools.list_tools()
                if tool.catalog_visible and not tool.session_scoped
            ],
            "skills": skill_choices,
            "tool_settings": {
                BASH_TOOL_SETTINGS_KEY: {
                    "type": "object",
                    "properties": {
                        BASH_ALLOWED_ENV_KEY: {
                            "type": "array",
                            "items": {"type": "string"},
                            "uniqueItems": True,
                        }
                    },
                    "additionalProperties": False,
                },
                SUBAGENT_TOOL_SETTINGS_KEY: {
                    "type": "object",
                    "properties": {
                        SUBAGENT_ALLOWED_AGENTS_KEY: {
                            "type": "array",
                            "items": {"type": "string"},
                            "uniqueItems": True,
                        }
                    },
                    "additionalProperties": False,
                },
            },
            "models": self._extension_catalog_models(),
            "prompt_blocks": self.system_prompts.list_blocks(),
        }

    def _extension_catalog_models(self) -> list[dict[str, Any]]:
        models: list[dict[str, Any]] = []
        for provider_id, model in self.models.query(ModelQuery()):
            connections = [
                connection_id
                for connection_id in model.connections
                if self.provider_credentials.is_usable(
                    provider_id, f"{provider_id}:{connection_id}"
                )
            ]
            if connections:
                models.append(
                    {
                        "id": f"{provider_id}/{model.model_id}",
                        "name": model.name,
                        "connections": connections,
                        "context_window": model.context_window,
                        "capabilities": {
                            "tools": model.capabilities.tools,
                            "reasoning": {
                                "supported": model.capabilities.reasoning.supported,
                                "control": model.capabilities.reasoning.control,
                                "levels": list(model.capabilities.reasoning.levels),
                                "budget_max": model.capabilities.reasoning.budget_max,
                            },
                        },
                    }
                )
        return models

    async def _start_owned_completion(
        self,
        address: Any,
        owner: Any,
        content: str,
        notice_ids: tuple[str, ...],
        on_persisted: Any,
    ) -> Any:
        from core.runs import RunAdmission

        self._validate_temporary_admission(address, RunAdmission(owner=owner))
        groups = next(groups for groups in self._temporary_groups if groups.owns(owner))
        return await groups.continue_completion(address, owner, content, notice_ids, on_persisted)
