"""Temporary Agent resolution: an Extension's Session generations and their configuration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from core.permissions import PermissionCeiling, PermissionRules
from core.projects._resolution_context import _RESOLUTION_WORKERS, _require_temporary_binding
from core.projects._resolution_values import (
    _override_permission_rules,
    _project_agent_tool_access,
    _resolve_sampling,
    _resolve_thinking_effort,
    resolve_permission_rules,
)
from core.projects._runtime_agent import AgentOverrides, AgentResolutionError, RuntimeAgent
from core.projects._team_resolution import _profile_skill_allowed
from core.settings import AgentDefaults
from core.tools.availability import ToolAccess

if TYPE_CHECKING:
    from core.agents.temporary import TemporaryAgent, TemporaryAgentConfig
    from core.projects.resolver import AgentResolver
    from core.sessions import SessionAddress


class TemporaryResolution:
    """Resolve temporary Agents for an :class:`AgentResolver` (its ``temporary_agents``)."""

    def __init__(self, owner: AgentResolver) -> None:
        self._owner = owner

    def resolve(
        self,
        address: Any,
        *,
        generation_id: str,
        session: SessionAddress | None = None,
    ) -> RuntimeAgent:
        """Resolve only an exact canonical temporary Session generation.

        ``session`` names the Session whose Agent overrides apply, usually
        ``address`` itself; omit it for the binding's own configuration.
        """
        binding = self._registry().resolve(address, generation_id=generation_id)
        overrides = AgentOverrides() if session is None else self._owner.session_overrides(session)
        return self._apply_address(_require_temporary_binding(binding), address, overrides)

    async def resolve_async(
        self,
        address: Any,
        *,
        generation_id: str,
        session: SessionAddress | None = None,
    ) -> RuntimeAgent:
        """Event-Loop-safe :meth:`resolve`.

        The binding and override reads run on the Session database's pool; the
        Project check and the overrides' Model check, which read Project and Model
        configuration, run on the ``agent-resolution`` pool.
        """
        registry = self._registry()
        agent = _require_temporary_binding(
            await registry.resolve_async(address, generation_id=generation_id)
        )
        overrides = (
            AgentOverrides()
            if session is None
            else await self._owner.session_overrides_async(session)
        )
        if getattr(address, "project_id", None) is None and overrides.is_empty:
            return agent
        return await _RESOLUTION_WORKERS.run(self._apply_address, agent, address, overrides)

    def prepare_config(
        self, config: TemporaryAgentConfig, project_id: str | None
    ) -> TemporaryAgentConfig:
        """Resolve a repository or identity Profile within the owner's selection, for a snapshot."""
        if config.identity_profile is not None:
            return self._prepare_identity(config, project_id)
        if config.repository_profile is None:
            return config
        if project_id is None:
            raise AgentResolutionError("A repository profile requires a Project.")
        project = self._owner._load_project(project_id)
        profile = self._owner._team_resolution._read_agent_fresh(project, config.repository_profile)
        owner_tools = (
            config.tool_access.allowed
            if config.tool_access.mode == "selected"
            else (self._owner._tool_names() if config.tool_access.mode == "all" else ())
        )
        owner_tools = tuple(name for name in owner_tools if name not in config.tool_access.denied)
        # Team and temporary Agents use the same translation; a participant's
        # owner bounds it with its own selection.
        access = _project_agent_tool_access(project, profile, owner_tools)
        access = replace(
            access,
            allowed=tuple(name for name in access.allowed if name not in config.tool_access.denied),
            denied=tuple(sorted(set(access.denied) | set(config.tool_access.denied))),
            granted=tuple(name for name in config.tool_access.granted if name in access.allowed),
            fixed=access.fixed or config.tool_access.fixed or config.tool_access.mode == "none",
        )
        selected_skills = (
            sorted(self._owner._skill_pool_names(project_id))
            if "*" in config.allowed_skills and profile.skill_rules
            else config.allowed_skills
        )
        skills = [name for name in selected_skills if _profile_skill_allowed(profile, name)]
        profile = self._owner._team_resolution._preload_profile(project_id, profile, skills)
        overrides = project.overrides.get(profile.agent_id, {})
        defaults = AgentDefaults.from_dict(self._owner._global_agent_defaults())
        override_model = overrides.get("model")
        if override_model and self._owner.is_model_configured(override_model):
            model = override_model
        elif profile.model in {"", "inherit"} and config.model:
            model = config.model
        else:
            try:
                model = self._owner._resolve_model_or_raise(
                    profile, project, AgentDefaults.from_dict(self._owner._global_agent_defaults())
                )
            except AgentResolutionError:
                # The participant's own Model stands in for an unusable wish.
                model = config.model
        model = config.model_override if config.model_override is not None else model
        self._owner.require_model_configured(model)
        owner_rules = (
            config.permission_rules
            if isinstance(config.permission_rules, PermissionRules)
            else None
        )
        return replace(
            config,
            model=model,
            model_override=None,
            thinking_effort_override=None,
            tool_access=access,
            allowed_skills=skills,
            # The owner's parts first, then the Team member's resolution.
            permission_rules=resolve_permission_rules(
                (owner_rules, _override_permission_rules(project, profile.agent_id)),
                AgentDefaults.from_dict(self._owner._global_agent_defaults()),
                repository=profile.permission_rules,
            ),
            instructions="\n\n".join(part for part in (profile.body, config.instructions) if part),
            temperature=overrides.get(
                "temperature",
                _resolve_sampling("temperature", profile, project, defaults)
                if not config.model
                else profile.temperature
                if profile.temperature is not None
                else config.temperature,
            ),
            top_p=overrides.get(
                "top_p",
                _resolve_sampling("top_p", profile, project, defaults)
                if not config.model
                else profile.top_p
                if profile.top_p is not None
                else config.top_p,
            ),
            thinking_effort=(
                config.thinking_effort_override
                if config.thinking_effort_override is not None
                else overrides.get(
                    "thinking_effort",
                    _resolve_thinking_effort(profile, project, defaults)
                    if not config.model
                    else profile.thinking_effort
                    if profile.thinking_effort is not None
                    else config.thinking_effort,
                )
            ),
        )

    def _prepare_identity(
        self, config: TemporaryAgentConfig, project_id: str | None
    ) -> TemporaryAgentConfig:
        """Snapshot an Identity Agent's configuration, bounded by the owner's selection.

        The participant works like that Agent in the owner's directory and
        Project: its Model chain, sampling, Tool access, Skills, Permission
        Rules, Compaction Policy and SOUL.md. Its Workspace, personal Memory and
        private Skills stay with the Identity Agent.
        """
        assert config.identity_profile is not None
        agent = self._owner._resolve_identity_agent(config.identity_profile)
        if agent.builtin is not None:
            raise AgentResolutionError(
                f"Built-in Agent {agent.id} cannot be a participant; choose another Agent."
            )
        if project_id is not None:
            self._owner._load_project(project_id)
        model = config.model_override if config.model_override is not None else agent.model
        self._owner.require_model_configured(model)
        owner_rules = (
            config.permission_rules
            if isinstance(config.permission_rules, PermissionRules)
            else None
        )
        from core.agents.agents import AgentError

        try:
            soul = self._owner._agents.read_soul(agent.id).content.strip()
        except AgentError as error:
            raise AgentResolutionError(
                f"SOUL.md of Agent {agent.id} is unreadable: {error}"
            ) from error
        return replace(
            config,
            model=model,
            model_override=None,
            thinking_effort_override=None,
            fallback_models=list(agent.fallback_models),
            temperature=agent.temperature,
            top_p=agent.top_p,
            thinking_effort=(
                config.thinking_effort_override
                if config.thinking_effort_override is not None
                else agent.thinking_effort
            ),
            tool_access=_bounded_tool_access(config.tool_access, agent.tool_access),
            allowed_skills=self._identity_skills(config, agent, project_id),
            tools={**deepcopy(agent.tools), **deepcopy(config.tools)},
            # The owner's parts first, then the Agent's own (defaults already baked in).
            permission_rules=resolve_permission_rules(
                (owner_rules, agent.permission_rules),
                AgentDefaults.from_dict(self._owner._global_agent_defaults()),
            ),
            compaction_policy=(
                config.compaction_policy
                if config.compaction_policy is not None
                else deepcopy(agent.compaction_policy)
            ),
            project_memory_enabled=config.project_memory_enabled and agent.project_memory_enabled,
            instructions="\n\n".join(part for part in (soul, config.instructions) if part),
        )

    def _identity_skills(
        self, config: TemporaryAgentConfig, agent: Any, project_id: str | None
    ) -> list[str]:
        excluded = set(agent.excluded_skills)
        owner_all = "*" in config.allowed_skills
        agent_all = "*" in agent.allowed_skills
        if owner_all and agent_all and not excluded:
            return ["*"]
        if owner_all and agent_all:
            pool = self._owner._skill_pool_names(project_id) if project_id is not None else ()
            return sorted(name for name in pool if name not in excluded)
        if owner_all:
            selected = agent.allowed_skills
        elif agent_all:
            selected = config.allowed_skills
        else:
            selected = [name for name in config.allowed_skills if name in agent.allowed_skills]
        return [name for name in selected if name not in excluded]

    def preview(
        self, config: TemporaryAgentConfig, project_id: str | None = None
    ) -> TemporaryAgent:
        """Resolve editor configuration like a started one, without creating a Session."""
        from core.agents.temporary import TemporaryAgent

        config = self.prepare_config(config, project_id)
        agent = TemporaryAgent(
            id="preview",
            name=config.name,
            model=config.model,
            cwd=config.cwd,
            tool_access=config.tool_access,
            allowed_skills=config.allowed_skills,
            tools=config.tools,
            fallback_models=config.fallback_models or [],
            instructions=config.instructions,
            prompt_blocks=config.prompt_blocks,
            temperature=config.temperature,
            top_p=config.top_p,
            thinking_effort=config.thinking_effort,
            project_memory_enabled=config.project_memory_enabled and project_id is not None,
            compaction_policy=config.compaction_policy,
            permission_rules=(
                config.permission_rules
                if isinstance(config.permission_rules, PermissionRules)
                else None
            ),
            permission_ceiling=(
                PermissionCeiling.from_ceiling_dict(config.permission_ceiling)
                if config.permission_ceiling is not None
                else None
            ),
        )
        self._require_project(project_id)
        return agent

    def _registry(self) -> Any:
        if self._owner._temporary_agents is None:
            raise AgentResolutionError("temporary Session is unavailable")
        return self._owner._temporary_agents

    def _apply_address(
        self,
        agent: TemporaryAgent,
        address: Any,
        overrides: AgentOverrides,
    ) -> RuntimeAgent:
        """Check the address's Project, then apply the Session's overrides."""
        self._require_project(getattr(address, "project_id", None))
        return self._owner._session_resolution._apply_overrides(agent, overrides)

    def _require_project(self, project_id: str | None) -> None:
        # A temporary Agent keeps its owner's Tool and Skill selection: like an
        # Identity Session working in a Project it uses the Project's directory,
        # Skills and context, not the Tool and Skill whitelists that bound the
        # Project's Team.
        if project_id is not None:
            self._owner._load_project(project_id)


def _bounded_tool_access(owner: ToolAccess, agent: ToolAccess) -> ToolAccess:
    """An Identity Agent's Tool access within its owner's selection.

    Denials of both apply; a participant never gets a Tool its owner or its
    Agent withholds. Both ``all`` stay ``all`` so later catalog Tools follow.
    """
    denied = tuple(sorted(set(owner.denied) | set(agent.denied)))
    fixed = owner.fixed or agent.fixed or owner.mode == "none"
    if owner.mode == "none" or agent.mode == "none":
        return ToolAccess(mode="none", denied=denied, fixed=fixed)
    granted = tuple(sorted(set(owner.granted) | set(agent.granted)))
    if owner.mode == "all" and agent.mode == "all":
        return ToolAccess(
            mode="all",
            denied=denied,
            granted=tuple(name for name in granted if name not in denied),
            fixed=fixed,
        )
    if owner.mode == "all":
        names = set(agent.allowed)
    elif agent.mode == "all":
        names = set(owner.allowed)
    else:
        names = set(owner.allowed) & set(agent.allowed)
    allowed = tuple(sorted(names - set(denied)))
    return ToolAccess(
        mode="selected",
        allowed=allowed,
        denied=denied,
        granted=tuple(name for name in granted if name in allowed),
        fixed=fixed,
    )
