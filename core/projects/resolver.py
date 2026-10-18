"""Identity, Project and temporary Agent resolution with fresh source reads."""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol

from core.projects._model_configuration import (
    ConnectionRestrictedModel,
    CredentialProbe,
    ModelConfigurationChecker,
    ModelConfigurationError,
    ModelProbe,
    ProviderProbe,
)
from core.projects._project_context import resolve_prompt_project as resolve_prompt_project
from core.projects._project_context import resolve_skill_scope as resolve_skill_scope
from core.projects._project_context import runtime_agent_body as runtime_agent_body
from core.projects._resolution_context import (
    _RESOLUTION_WORKERS,
    _require_one_override_source,
    _require_temporary_binding,
    _session_address,
    _skill_binding_key,
)
from core.projects._resolution_values import (
    _build_config_agent,
    _config_sampling_source,
    _config_thinking_effort_source,
    _config_tool_access_source,
    _effective_allowed_agents,
    _identity_optional_source,
    _identity_string_list_source,
    _identity_string_source,
    _overridden_model,
    _project_agent_tool_access,
    _project_agent_tools,
    _resolve_sampling,
    _resolve_thinking_effort,
    effective_project_allowed_skills,
)
from core.projects._runtime_agent import (
    AGENT_OVERRIDE_FIELDS,
    AgentOverrides,
    AgentResolutionError,
    ConfigAgent,
    GlobalAgentDefaultsProvider,
    ProjectSkillNamesProvider,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
    RuntimeAgent,
    WorkingProjectMissingError,
)
from core.projects._session_resolution import (
    DEFAULT_PROJECT_MISSING_MESSAGE,
    SKILL_SUBJECT_MISSING_MESSAGE,
    TEAM_WORKING_PROJECT_MESSAGE,
    SessionResolution,
)
from core.projects._team_resolution import TeamResolution, _profile_skill_allowed
from core.projects.projects import ProjectError, ProjectNotFoundError
from core.projects.sources import AgentAdapter, AgentProfile, ScanResult
from core.sessions import AGENT_DEFAULT_PROJECT
from core.settings import AgentDefaults

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from core.agents.agents import Agent, AgentStore
    from core.agents.temporary import TemporaryAgent, TemporaryAgentConfig
    from core.models.models import ModelRegistry
    from core.projects.projects import Project
    from core.projects.store import ProjectStore
    from core.providers.providers import ProviderRegistry
    from core.runtime.interfaces import ProviderCredentialResolverProtocol
    from core.sessions import SessionAddress, SessionWorkingProject, WorkingProjectChoice


__all__ = [
    "AGENT_OVERRIDE_FIELDS",
    "AgentOverrides",
    "AgentResolutionError",
    "AgentResolver",
    "ConfigAgent",
    "ConnectionRestrictedModel",
    "CredentialProbe",
    "DEFAULT_PROJECT_MISSING_MESSAGE",
    "GlobalAgentDefaultsProvider",
    "ModelConfigurationChecker",
    "ModelConfigurationError",
    "ModelProbe",
    "ProjectSkillNamesProvider",
    "ProviderProbe",
    "ResolutionAgentNotFoundError",
    "ResolutionProjectNotFoundError",
    "RuntimeAgent",
    "SKILL_SUBJECT_MISSING_MESSAGE",
    "SessionMetadataStore",
    "TEAM_WORKING_PROJECT_MESSAGE",
    "WorkingProjectMissingError",
    "build_agent_resolver",
    "effective_project_allowed_skills",
    "resolve_prompt_project",
    "resolve_skill_scope",
    "runtime_agent_body",
]


class SessionMetadataStore(Protocol):
    """The Session metadata operations the resolver needs for Agent overrides and bindings."""

    def working_project(self, address: SessionAddress) -> SessionWorkingProject: ...

    async def working_project_async(self, address: SessionAddress) -> SessionWorkingProject: ...

    def metadata_value(self, address: SessionAddress, key: str) -> Any: ...

    async def metadata_value_async(self, address: SessionAddress, key: str) -> Any: ...

    def mutate_metadata(self, address: SessionAddress, mutation: Callable[[Any], None]) -> Any: ...

    def ensure_metadata(
        self,
        address: SessionAddress,
        mutation: Callable[[Any], None],
        *,
        create_missing: bool = False,
    ) -> tuple[Any, Any]: ...

    def list_addresses(
        self,
        project_id: str | None = None,
        *,
        agent_id: str | None = None,
        exclude_owner_managed: bool = False,
    ) -> list[SessionAddress]: ...

    def get(self, address: SessionAddress) -> Any: ...

    async def run_async(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any: ...


def _no_project_skills(_project_id: str) -> frozenset[str]:
    """Default project-skill probe: a project with no own skills (bundled-only)."""
    return frozenset()


def _identity_resolution_error(error: Exception) -> AgentResolutionError:
    """Wrap an Identity Agent store failure, keeping a missing Agent precise."""
    from core.agents.agents import AgentNotFoundError

    if isinstance(error, AgentNotFoundError):
        return ResolutionAgentNotFoundError(str(error))
    return AgentResolutionError(str(error))


class AgentResolver:
    """Resolve ``(project_id | None, agent_id)`` into a uniform runtime agent.

    The single run-time entry point for agent resolution. Holds a per-project
    Team-scan cache (the slow "who is on the Team" answer) while reading each
    individual agent's config fresh from its repo file (the fast-changing "what
    is its config" answer) on every resolve.
    """

    def __init__(
        self,
        agents: AgentStore,
        projects: ProjectStore,
        model_checker: ModelConfigurationChecker,
        global_agent_defaults: GlobalAgentDefaultsProvider,
        *,
        source_adapters: dict[str, AgentAdapter] | None = None,
        project_skill_names: ProjectSkillNamesProvider | None = None,
        profile_skill_content: Callable[[str, str, list[str]], str | None] | None = None,
        skill_pool_names: ProjectSkillNamesProvider | None = None,
        tool_names: Callable[[], tuple[str, ...]] | None = None,
        temporary_agents: Any | None = None,
        sessions: SessionMetadataStore | None = None,
    ) -> None:
        self._agents = agents
        self._projects = projects
        self._model_checker = model_checker
        self._global_agent_defaults = global_agent_defaults
        # Captured once; ``scan_project`` falls back to its own default registry
        # when this is ``None``, so tests can inject a custom registry.
        self._source_adapters = source_adapters
        # Project-skill probe for config-agent skill resolution; defaults to "no
        # project skills" so a resolver built without it degrades to bundled-only
        # rather than failing (the runtime always wires the real probe).
        self._project_skill_names = project_skill_names or _no_project_skills
        self._profile_skill_content = profile_skill_content or (
            lambda project_id, name, allowed: None
        )
        self._skill_pool_names = skill_pool_names or self._project_skill_names
        self._tool_names = tool_names or (lambda: ())
        self._temporary_agents = temporary_agents
        # Without Sessions (tests of the chains alone) no Session has overrides.
        self._sessions = sessions
        # Team-scan cache keyed by project id. A run reads from here; an explicit
        # re-scan / project-open repopulates it via ``rescan_project``.
        self._team_cache: dict[str, tuple[Hashable, ScanResult]] = {}
        self._session_resolution = SessionResolution(self)
        self._team_resolution = TeamResolution(self)

    def resolve_agent(
        self,
        project_id: str | None,
        agent_id: str,
        *,
        session_id: str | None = None,
        new_session_overrides: AgentOverrides | None = None,
        require_model: bool = True,
    ) -> RuntimeAgent:
        """Resolve one agent to a runnable :class:`RuntimeAgent`.

        ``project_id is None`` returns the store identity agent unchanged. A set
        ``project_id`` returns a :class:`ConfigAgent` synthesized from the
        project's Team scan plus the resolved model. With ``session_id`` the
        result is the Agent as that Session runs it: the Session's Agent overrides
        replace the resolved values in an immutable runtime view, never in either
        source configuration; a Session that does not exist yet has none. A Session
        of the Librarian bound to an Agent (``SKILL_AGENT_ID_KEY``) also runs it on
        that Agent's Skills (:meth:`_bind_skill_subject`). Raises
        :class:`ResolutionProjectNotFoundError` / :class:`ResolutionAgentNotFoundError`
        (both :class:`AgentResolutionError`) for an unknown project/agent, a plain
        :class:`AgentResolutionError` for a config agent whose model chain fell
        through or a Librarian Session whose Agent is gone, and
        :class:`ModelConfigurationError` for a Session Model that cannot run.

        ``new_session_overrides`` resolves the Agent as a Session that does not
        exist yet runs it with these overrides (the first Run of a new Session);
        it excludes ``session_id``.

        ``require_model=False`` lets administration and catalogs inspect an
        Agent's configuration without checking Model availability, including a
        Session override. Run admission keeps the default availability checks.
        """
        _require_one_override_source(session_id, new_session_overrides)
        address = None if session_id is None else _session_address(project_id, agent_id, session_id)
        overrides = (
            self.session_overrides(address)
            if address is not None
            else new_session_overrides or AgentOverrides()
        )
        if project_id is None:
            agent: RuntimeAgent = self._resolve_identity_agent(agent_id)
        else:
            agent = self._resolve_config_agent(
                project_id, agent_id, model_override=overrides.model, require_model=require_model
            )
        agent = self._session_resolution._apply_overrides(
            agent, overrides, require_model=require_model
        )
        if address is None:
            return agent
        key = _skill_binding_key(agent) if project_id is None else None
        if key is not None:
            agent = self._session_resolution._bind_skill_subject(
                agent, self._session_resolution._session_value(address, key)
            )
        return agent

    def resolve_temporary_agent(
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
        binding = self._temporary_registry().resolve(address, generation_id=generation_id)
        overrides = AgentOverrides() if session is None else self.session_overrides(session)
        return self._apply_temporary_address(
            _require_temporary_binding(binding), address, overrides
        )

    async def resolve_agent_async(
        self,
        project_id: str | None,
        agent_id: str,
        *,
        session_id: str | None = None,
        new_session_overrides: AgentOverrides | None = None,
        require_model: bool = True,
    ) -> RuntimeAgent:
        """Event-Loop-safe :meth:`resolve_agent`.

        A Project Agent resolves on the ``agent-resolution`` pool. An Identity
        Agent read verifies, and may repair, its current-Session pointer, so it
        runs as one unit on the Session database's pool
        (:meth:`AgentStore.get_async`). The Session's overrides are read on the
        Session database's pool; their Model is checked on the
        ``agent-resolution`` pool.
        """
        _require_one_override_source(session_id, new_session_overrides)
        address = None if session_id is None else _session_address(project_id, agent_id, session_id)
        if address is not None:
            overrides = await self.session_overrides_async(address)
        else:
            overrides = new_session_overrides or AgentOverrides()
        if project_id is not None:
            agent: RuntimeAgent = await _RESOLUTION_WORKERS.run(
                self._resolve_config_agent,
                project_id,
                agent_id,
                model_override=overrides.model,
                require_model=require_model,
            )
        else:
            from core.agents.agents import AgentError

            try:
                agent = await self._agents.get_async(agent_id)
            except AgentError as error:
                raise _identity_resolution_error(error) from error
        if not overrides.is_empty:
            agent = await _RESOLUTION_WORKERS.run(
                self._session_resolution._apply_overrides,
                agent,
                overrides,
                require_model=require_model,
            )
        key = _skill_binding_key(agent) if project_id is None else None
        if address is not None and key is not None:
            subject_id = await self._session_resolution._session_value_async(address, key)
            if subject_id is not None:
                agent = await _RESOLUTION_WORKERS.run(
                    self._session_resolution._bind_skill_subject, agent, subject_id
                )
        return agent

    async def resolve_delegated_agent_async(
        self, project_id: str | None, agent_id: str, *, caller_model: str
    ) -> RuntimeAgent:
        """Resolve `inherit` before requiring a default Model, for delegation only."""
        if project_id is None:
            return await self.resolve_agent_async(None, agent_id)
        return await _RESOLUTION_WORKERS.run(
            self._resolve_config_agent, project_id, agent_id, caller_model
        )

    async def resolve_temporary_agent_async(
        self,
        address: Any,
        *,
        generation_id: str,
        session: SessionAddress | None = None,
    ) -> RuntimeAgent:
        """Event-Loop-safe :meth:`resolve_temporary_agent`.

        The binding and override reads run on the Session database's pool; the
        Project check and the overrides' Model check, which read Project and Model
        configuration, run on the ``agent-resolution`` pool.
        """
        registry = self._temporary_registry()
        agent = _require_temporary_binding(
            await registry.resolve_async(address, generation_id=generation_id)
        )
        overrides = (
            AgentOverrides() if session is None else await self.session_overrides_async(session)
        )
        if getattr(address, "project_id", None) is None and overrides.is_empty:
            return agent
        return await _RESOLUTION_WORKERS.run(
            self._apply_temporary_address, agent, address, overrides
        )

    def resolve_working_project(
        self,
        project_id: str | None,
        agent: RuntimeAgent,
        *,
        session_id: str | None = None,
        requested: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
    ) -> str | None:
        """Return the Project a Run of *agent* works in, or ``None`` for its Workspace.

        A Project Agent (``project_id`` set) works in its address Project. With
        ``session_id``, a Session of an Identity Agent works in the Project it was
        created with; once that Project no longer exists the Session is refused
        with :class:`WorkingProjectMissingError`, never moved to the Workspace.
        Without a Session, or for one that does not exist yet, the result is
        where a new Session with *requested* works
        (:meth:`new_session_working_project`).
        """
        return self._session_resolution.resolve_working_project(
            project_id, agent, session_id=session_id, requested=requested
        )

    async def resolve_working_project_async(
        self,
        project_id: str | None,
        agent: RuntimeAgent,
        *,
        session_id: str | None = None,
        requested: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
    ) -> str | None:
        """Event-Loop-safe :meth:`resolve_working_project`.

        The Session read runs on the Session database's pool; the Project check
        reads Project files on the ``agent-resolution`` pool.
        """
        return await self._session_resolution.resolve_working_project_async(
            project_id, agent, session_id=session_id, requested=requested
        )

    def new_session_working_project(
        self,
        project_id: str | None,
        agent: RuntimeAgent,
        requested: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
    ) -> str | None:
        """Return the Project a new Session of *agent* works in, ``None`` for its Workspace.

        A Session of a Project Agent works in its address Project and takes no
        other choice. For an Identity Agent, *requested* names a Project, which
        must exist (:class:`ResolutionProjectNotFoundError`), ``None`` names the
        Workspace, and :data:`~core.sessions.AGENT_DEFAULT_PROJECT` the Agent's
        default Project (its ``root_project_id``).
        """
        return self._session_resolution.new_session_working_project(project_id, agent, requested)

    async def session_working_project_async(self, address: SessionAddress) -> str | None:
        """Return the Project the Session at *address* works in, ``None`` for the Workspace.

        A Project Session works in its address Project; a Session of an Identity
        Agent in the Project it was created with (a missing Session has none).
        Raises :class:`WorkingProjectMissingError` once that Project no longer
        exists. The Session read runs on the Session database's pool, the Project
        check on the ``agent-resolution`` pool.
        """
        return await self._session_resolution.session_working_project_async(address)

    def session_overrides(self, address: SessionAddress) -> AgentOverrides:
        """Return the Agent overrides *address* stores (none for a missing Session)."""
        return self._session_resolution.session_overrides(address)

    async def session_overrides_async(self, address: SessionAddress) -> AgentOverrides:
        """Event-Loop-safe :meth:`session_overrides`."""
        return await self._session_resolution.session_overrides_async(address)

    def capture_session_settings(
        self, address: SessionAddress, agent: RuntimeAgent
    ) -> AgentOverrides:
        """Fill uncaptured Model settings once, preserving concurrent explicit choices."""
        return self._session_resolution.capture_session_settings(address, agent)

    def preserve_agent_sessions(self, project_id: str | None, agent_id: str) -> None:
        """Keep existing Sessions' choices before changing their Agent's start values.

        Older Sessions can still inherit either value. Materialize those values
        under the caller's configuration mutation guard before the default moves.
        This does not validate availability: replacing a broken Model must work.
        """
        return self._session_resolution.preserve_agent_sessions(project_id, agent_id)

    async def capture_session_settings_async(
        self, address: SessionAddress, agent: RuntimeAgent
    ) -> AgentOverrides:
        return await self._session_resolution.capture_session_settings_async(address, agent)

    def remember_session_settings(
        self, address: SessionAddress, changes: Mapping[str, Any]
    ) -> AgentOverrides:
        """Set this Session's choices and the Agent's start values for new Sessions."""
        return self._session_resolution.remember_session_settings(address, changes)

    def update_session_overrides(
        self, address: SessionAddress, changes: Mapping[str, Any]
    ) -> AgentOverrides:
        """Replace or clear some of one Session's Agent overrides; return the result.

        *changes* maps override fields to their new value; ``None`` clears one.
        Fields it leaves out keep their stored value, and stored fields a newer
        vBot added survive. Every value is validated first, a Model also for
        usability, so an invalid change writes nothing.
        """
        return self._session_resolution.update_session_overrides(address, changes)

    async def update_session_overrides_async(
        self, address: SessionAddress, changes: Mapping[str, Any]
    ) -> AgentOverrides:
        """Event-Loop-safe :meth:`update_session_overrides` on the Session database's pool."""
        return await self._session_resolution.update_session_overrides_async(address, changes)

    def prepare_temporary_config(
        self, config: TemporaryAgentConfig, project_id: str | None
    ) -> TemporaryAgentConfig:
        """Resolve a repository Profile within the owner's selection, for a snapshot."""
        if config.repository_profile is None:
            return config
        if project_id is None:
            raise AgentResolutionError("A repository profile requires a Project.")
        project = self._load_project(project_id)
        profile = self._team_resolution._read_agent_fresh(project, config.repository_profile)
        owner_tools = (
            config.tool_access.allowed
            if config.tool_access.mode == "selected"
            else (self._tool_names() if config.tool_access.mode == "all" else ())
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
            sorted(self._skill_pool_names(project_id))
            if "*" in config.allowed_skills and profile.skill_rules
            else config.allowed_skills
        )
        skills = [name for name in selected_skills if _profile_skill_allowed(profile, name)]
        profile = self._team_resolution._preload_profile(project_id, profile, skills)
        overrides = project.overrides.get(profile.agent_id, {})
        override_model = overrides.get("model")
        if override_model and self.is_model_configured(override_model):
            model = override_model
        elif profile.model in {"", "inherit"}:
            model = config.model
        else:
            try:
                model = self._resolve_model_or_raise(
                    profile, project, AgentDefaults.from_dict(self._global_agent_defaults())
                )
            except AgentResolutionError:
                # The participant's own Model stands in for an unusable wish.
                model = config.model
        self.require_model_configured(model)
        return replace(
            config,
            model=model,
            tool_access=access,
            allowed_skills=skills,
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

    def preview_temporary_agent(
        self, config: TemporaryAgentConfig, project_id: str | None = None
    ) -> TemporaryAgent:
        """Resolve editor configuration like a started one, without creating a Session."""
        from core.agents.temporary import TemporaryAgent

        config = self.prepare_temporary_config(config, project_id)
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
        )
        self._require_temporary_project(project_id)
        return agent

    def _temporary_registry(self) -> Any:
        if self._temporary_agents is None:
            raise AgentResolutionError("temporary Session is unavailable")
        return self._temporary_agents

    def _apply_temporary_address(
        self,
        agent: TemporaryAgent,
        address: Any,
        overrides: AgentOverrides,
    ) -> RuntimeAgent:
        """Check the address's Project, then apply the Session's overrides."""
        self._require_temporary_project(getattr(address, "project_id", None))
        return self._session_resolution._apply_overrides(agent, overrides)

    def _require_temporary_project(self, project_id: str | None) -> None:
        # A temporary Agent keeps its owner's Tool and Skill selection: like an
        # Identity Session working in a Project it uses the Project's directory,
        # Skills and context, not the Tool and Skill whitelists that bound the
        # Project's Team.
        if project_id is not None:
            self._load_project(project_id)

    def _resolve_identity_agent(self, agent_id: str) -> Agent:
        """Resolve one persisted Identity Agent."""
        from core.agents.agents import AgentError

        try:
            return self._agents.get(agent_id)
        except AgentError as error:
            raise _identity_resolution_error(error) from error

    def _resolve_config_agent(
        self,
        project_id: str,
        agent_id: str,
        inherit_model: str | None = None,
        *,
        model_override: str | None = None,
        require_model: bool = True,
    ) -> ConfigAgent:
        project = self._load_project(project_id)
        team = self._team_resolution._project_team(project)
        if agent_id not in {member.agent_id for member in team}:
            raise ResolutionAgentNotFoundError(
                f"agent '{agent_id}' is not on project '{project_id}' team"
            )

        # Single-agent config freshness: re-read the agent's source file now so a
        # repo edit between the open-time scan and this run takes effect. The
        # cached Team only told us the agent still belongs; the live config comes
        # from disk.
        scanned = self._team_resolution._read_agent_fresh(project, agent_id)
        # Read the global tier once and feed it to all three chains, so one resolve
        # never reads the settings file three times (model + temp + thinking).
        global_defaults = AgentDefaults.from_dict(self._global_agent_defaults())
        if model_override is not None:
            if require_model:
                self.require_model_configured(model_override)
            resolved_model = model_override
        elif not require_model:
            resolved_model = str(
                self._config_model_source(project, scanned, global_defaults)["value"] or ""
            )
        elif (
            scanned.model == "inherit"
            and inherit_model
            and not project.overrides.get(agent_id, {}).get("model")
        ):
            self.require_model_configured(inherit_model)
            resolved_model = inherit_model
        else:
            resolved_model = self._resolve_model_or_raise(scanned, project, global_defaults)
        resolved_temperature = _resolve_sampling("temperature", scanned, project, global_defaults)
        resolved_top_p = _resolve_sampling("top_p", scanned, project, global_defaults)
        resolved_thinking_effort = _resolve_thinking_effort(scanned, project, global_defaults)
        tool_access = _project_agent_tool_access(project, scanned)
        allowed_skills = effective_project_allowed_skills(
            project, self._project_skill_names(project_id)
        )
        allowed_skills = [name for name in allowed_skills if _profile_skill_allowed(scanned, name)]
        allowed_agents = _effective_allowed_agents(scanned, team)
        tools = _project_agent_tools(tool_access, allowed_agents)
        scanned = self._team_resolution._preload_profile(project_id, scanned, allowed_skills)
        result = _build_config_agent(
            scanned,
            resolved_model,
            resolved_temperature,
            resolved_thinking_effort,
            tool_access,
            allowed_skills,
            tools,
            project.overrides.get(agent_id, {}).get("compaction_policy"),
            project_id=project_id,
            resolved_top_p=resolved_top_p,
            tool_loading=project.overrides.get(agent_id, {}).get("tool_loading"),
        )
        return replace(
            result,
            model_inherit=scanned.model == "inherit"
            and not project.overrides.get(agent_id, {}).get("model"),
        )

    def effective_config(
        self, project_id: str | None, agent_id: str, *, session_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """Report, per run field, the effective value and the tier that supplied it.

        The provenance-aware companion to :meth:`resolve_agent`, sharing the *same*
        chain logic so the two can never drift. Each field maps to
        ``{"value": ..., "source": ...}``:

        - **Config agents** (``project_id`` set): fields ``model``, ``temperature``,
          ``top_p``, ``thinking_effort``, ``tool_access``. Sources: ``"override"``
          (the vBot override layer), ``"agent"`` (the repo-declared scanned value),
          ``"project_default"``,
          ``"global_default"``, or ``None`` when every tier fell through. Unlike
          :meth:`resolve_agent`, a model chain that falls all the way through does
          **not** raise here — it returns ``{"value": None, "source": None}`` (an
          unknown project/agent still raises :class:`AgentResolutionError`). Each
          tier is gated by the same ``is_configured`` model check, so an unconfigured
          override/agent/default model falls through exactly as at run time.
        - **Identity agents** (``project_id is None``): fields ``model``,
          ``fallback_models``, ``temperature``, ``top_p``, ``thinking_effort``. Sources:
          ``"agent"`` (the own persisted value) or ``"global_default"``, or ``None``
          when neither has a value. No ``is_configured`` gating — this mirrors
          ``core.agents._config.apply_defaults`` exactly: a default applies when the persisted
          ``model`` is ``""`` / ``fallback_models`` is ``[]`` or
          ``temperature``/``top_p``/``thinking_effort`` is ``None``.

        With ``session_id``, each field that Session overrides reports its value
        with source ``"session"``.
        """
        if project_id is None:
            effective = self._identity_effective_config(agent_id)
        else:
            effective = self._config_effective_config(project_id, agent_id)
        if session_id is not None:
            overrides = self.session_overrides(_session_address(project_id, agent_id, session_id))
            for name, value in overrides.agent_changes().items():
                if name in effective:
                    effective[name] = {"value": value, "source": "session"}
        return effective

    def effective_tools_for_member(self, project: Project, member: AgentProfile) -> dict[str, Any]:
        """Project repository-owned Tool settings for one current Team member."""
        tool_access = _project_agent_tool_access(project, member)
        allowed_agents = _effective_allowed_agents(
            member, self._team_resolution._project_team(project)
        )
        return _project_agent_tools(tool_access, allowed_agents)

    def effective_config_for_member(
        self, project: Project, scanned: AgentProfile
    ) -> dict[str, dict[str, Any]]:
        """Compute a config agent's effective config from an already-scanned member.

        The cheap seam behind the team listing: it runs the *same* per-tier chain
        logic as :meth:`effective_config` but takes a scanned profile the caller
        already has, so building a team response never re-scans the repo once per
        member. The chain logic itself still lives here (one place), not in the RPC
        layer.
        """
        global_defaults = AgentDefaults.from_dict(self._global_agent_defaults())
        return self._config_effective_from_scanned(project, scanned, global_defaults)

    def _identity_effective_config(self, agent_id: str) -> dict[str, dict[str, Any]]:
        from core.agents.agents import AgentError

        try:
            raw = self._agents.get_raw(agent_id)
        except AgentError as error:
            raise _identity_resolution_error(error) from error
        defaults = AgentDefaults.from_dict(self._global_agent_defaults())
        return {
            "model": _identity_string_source(raw.model, defaults.model),
            "fallback_models": _identity_string_list_source(
                raw.fallback_models, defaults.fallback_models
            ),
            "temperature": _identity_optional_source(raw.temperature, defaults.temperature),
            "top_p": _identity_optional_source(raw.top_p, defaults.top_p),
            "thinking_effort": _identity_optional_source(
                raw.thinking_effort, defaults.thinking_effort
            ),
        }

    def _config_effective_config(self, project_id: str, agent_id: str) -> dict[str, dict[str, Any]]:
        project = self._load_project(project_id)
        team = self._team_resolution._project_team(project)
        if agent_id not in {member.agent_id for member in team}:
            raise ResolutionAgentNotFoundError(
                f"agent '{agent_id}' is not on project '{project_id}' team"
            )
        scanned = self._team_resolution._read_agent_fresh(project, agent_id)
        global_defaults = AgentDefaults.from_dict(self._global_agent_defaults())
        return self._config_effective_from_scanned(project, scanned, global_defaults)

    def _config_effective_from_scanned(
        self, project: Project, scanned: AgentProfile, global_defaults: AgentDefaults
    ) -> dict[str, dict[str, Any]]:
        return {
            "model": self._config_model_source(project, scanned, global_defaults),
            "temperature": _config_sampling_source(
                "temperature", project, scanned, global_defaults
            ),
            "top_p": _config_sampling_source("top_p", project, scanned, global_defaults),
            "thinking_effort": _config_thinking_effort_source(project, scanned, global_defaults),
            "tool_access": _config_tool_access_source(project, scanned),
        }

    def _config_model_source(
        self, project: Project, scanned: AgentProfile, global_defaults: AgentDefaults
    ) -> dict[str, Any]:
        """Return the effective model + source, gated by ``is_configured`` per tier.

        Same chain as :meth:`_resolve_model_or_raise` (override → agent → project
        default → global default) with each tier skipped when its model is not
        configured here, but never raising: a fully-fallen-through chain reports
        ``{"value": None, "source": None}``.
        """
        tiers = (
            ("override", _overridden_model(project, scanned.agent_id)),
            ("agent", self._team_resolution._profile_model(project, scanned)),
            ("project_default", project.default_model),
            ("global_default", str(global_defaults.model or "")),
        )
        for source, candidate in tiers:
            if candidate and self._model_checker.is_configured(candidate):
                return {"value": candidate, "source": source}
        return {"value": None, "source": None}

    def scan_project_report(self, project: Project) -> ScanResult:
        """Scan a project into Team + a **complete** report (incl. model + pointer findings).

        This is the project-scoped scan the open/re-scan path uses: it runs the
        structural scan, then appends one ``BAD_MODEL`` finding per config agent
        whose declared model is not configured in this instance and one ``ORPHAN``
        finding per anchor pointer — the project's ``default_agent`` and every
        session-owning agent under the anchor — that names an agent the scan did
        not produce (both via :meth:`ScanReport.with_findings`). Both checks happen
        **here, at scan time** — not lazily at first run.
        """
        return self._team_resolution.scan_project_report(project)

    def team_for_project(self, project_id: str) -> list[AgentProfile]:
        """Return the current cached Team snapshot for one registered Project."""
        return self._team_resolution.team_for_project(project_id)

    def rescan_project(self, project: Project) -> ScanResult:
        """Re-run the project scan and refresh the cached Team for this project.

        Called at project-open and on an explicit re-scan. Returns the same
        Team + complete report as :meth:`scan_project_report` and updates the
        Team-membership cache so subsequent ``resolve_agent`` calls see the new
        Team without re-walking the repo.
        """
        return self._team_resolution.rescan_project(project)

    def invalidate_team_cache(self, project_id: str | None = None) -> None:
        """Drop the cached Team for one project, or for all when ``None``."""
        return self._team_resolution.invalidate_team_cache(project_id)

    def is_model_configured(self, model: str) -> bool:
        """Return whether *model* can actually run in this instance.

        The single public seam over the shared :class:`ModelConfigurationChecker`
        rule (provider registered, model in catalog, a usable connection the
        model's allowlist permits — a pinned ``::connection`` checked verbatim),
        reused by the ``/model`` command's set-time validation so the
        accepted-model rule and the scan's ``BAD_MODEL`` rule (and the resolver
        chain's per-tier gate) can never drift. An empty/malformed string is not
        configured.
        """
        return self._model_checker.is_configured(model)

    def require_model_configured(self, model: str) -> None:
        """Raise with the shared Model-usability reason when *model* cannot run."""
        self._model_checker.require_configured(model)

    async def require_model_configured_async(self, model: str) -> None:
        """Event-Loop-safe :meth:`require_model_configured` on the ``agent-resolution`` pool."""
        await _RESOLUTION_WORKERS.run(self._model_checker.require_configured, model)

    def cached_scan(self, project: Project) -> ScanResult:
        """Return the cached Team + complete report, scanning only on first use.

        Unlike :meth:`rescan_project` this never re-walks a repository that was
        already scanned, so listing every Project's Team stays cheap; an open or an
        explicit re-scan refreshes the cache.
        """
        return self._team_resolution.cached_scan(project)

    def _load_project(self, project_id: str) -> Project:
        try:
            return self._projects.get(project_id)
        except ProjectNotFoundError as error:
            raise ResolutionProjectNotFoundError(str(error)) from error
        except ProjectError as error:
            raise AgentResolutionError(str(error)) from error

    def _resolve_model_or_raise(
        self, scanned: AgentProfile, project: Project, global_defaults: AgentDefaults
    ) -> str:
        """Run the model chain and return the first usable model, or raise.

        Chain: override → agent model → project default → global default. The override
        (``project.overrides[agent_id]["model"]`` — vBot-owned, data-dir only) is the
        **top** tier, so an overridden model wins over the repo-declared one. Each
        candidate counts only when it exists/is configured in this instance, so an
        overridden model whose credential later vanished degrades to the repo value
        rather than erroring (same ``is_configured`` gate as every tier). Falling all
        the way through is a clear "cannot run" error.
        """
        effective = self._config_model_source(project, scanned, global_defaults)
        if effective["value"]:
            return str(effective["value"])
        raise AgentResolutionError(
            f"Agent '{scanned.agent_id}' has no usable Model; "
            "set a Project default or a vBot override."
        )

    def translated_profile(self, project: Project, scanned: AgentProfile) -> AgentProfile:
        return self._team_resolution.translated_profile(project, scanned)


def build_agent_resolver(
    agents: AgentStore,
    projects: ProjectStore,
    models: ModelRegistry,
    providers: ProviderRegistry,
    provider_credentials: ProviderCredentialResolverProtocol,
    global_agent_defaults: Callable[[], Mapping[str, Any]],
    *,
    source_adapters: dict[str, AgentAdapter] | None = None,
    project_skill_names: ProjectSkillNamesProvider | None = None,
    profile_skill_content: Callable[[str, str, list[str]], str | None] | None = None,
    skill_pool_names: ProjectSkillNamesProvider | None = None,
    tool_names: Callable[[], tuple[str, ...]] | None = None,
    temporary_agents: Any | None = None,
    sessions: SessionMetadataStore | None = None,
) -> AgentResolver:
    """Assemble an :class:`AgentResolver` from the runtime services.

    The runtime wiring point: it adapts the concrete registries to the resolver's
    local probe protocols and builds the shared model-configuration checker, so
    the runtime only hands over the services it already owns. ``global_agent_defaults``
    returns the live ``defaults.agent`` map (the global tier of every chain), and
    ``project_skill_names`` returns a project's own scanned skills (the project-skill
    tier of config-agent skill resolution).
    """
    checker = ModelConfigurationChecker(models, providers, provider_credentials)
    return AgentResolver(
        agents,
        projects,
        checker,
        global_agent_defaults,
        source_adapters=source_adapters,
        project_skill_names=project_skill_names,
        profile_skill_content=profile_skill_content,
        skill_pool_names=skill_pool_names,
        tool_names=tool_names,
        temporary_agents=temporary_agents,
        sessions=sessions,
    )
