"""Temporary Agent resolution: an Extension's Session generations and their configuration."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from core.permissions import PermissionCeiling, PermissionRules
from core.projects._resolution_context import _RESOLUTION_WORKERS, _require_temporary_binding
from core.projects._resolution_values import (
    _override_permission_rules,
    _project_agent_tool_access,
    resolve_permission_rules,
)
from core.projects._runtime_agent import AgentOverrides, AgentResolutionError, RuntimeAgent
from core.projects._team_resolution import _profile_skill_allowed
from core.settings import AgentDefaults

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
        """Resolve a repository Profile within the owner's selection, for a snapshot."""
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
        # Team and temporary Agents use the same translation; the ceiling belongs
        # to the Project for a Team member and to its owner for a participant.
        access = _project_agent_tool_access(
            replace(project, allowed_tools=list(owner_tools)), profile
        )
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
        override_model = overrides.get("model")
        if override_model and self._owner.is_model_configured(override_model):
            model = override_model
        elif profile.model in {"", "inherit"}:
            model = config.model
        else:
            try:
                model = self._owner._resolve_model_or_raise(
                    profile, project, AgentDefaults.from_dict(self._owner._global_agent_defaults())
                )
            except AgentResolutionError:
                # The participant's own Model stands in for an unusable wish.
                model = config.model
        self._owner.require_model_configured(model)
        owner_rules = (
            config.permission_rules
            if isinstance(config.permission_rules, PermissionRules)
            else None
        )
        return replace(
            config,
            model=model,
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
                profile.temperature if profile.temperature is not None else config.temperature,
            ),
            top_p=overrides.get(
                "top_p", profile.top_p if profile.top_p is not None else config.top_p
            ),
            thinking_effort=overrides.get(
                "thinking_effort",
                profile.thinking_effort
                if profile.thinking_effort is not None
                else config.thinking_effort,
            ),
        )

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
