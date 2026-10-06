"""Identity, Project and temporary Agent resolution with fresh source reads."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from core.projects._model_configuration import (
    ConnectionRestrictedModel,
    CredentialProbe,
    ModelConfigurationChecker,
    ModelConfigurationError,
    ModelProbe,
    ProviderProbe,
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
    AGENT_OVERRIDES_META_KEY,
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
from core.projects.projects import ProjectError, ProjectNotFoundError
from core.projects.scan_report import FindingType, ScanFinding
from core.projects.sources import (
    AgentAdapter,
    AgentProfile,
    ScanResult,
    Translation,
    read_profile,
    scan_project,
)
from core.projects.sources._translation import rules_allow
from core.sessions import AGENT_DEFAULT_PROJECT
from core.settings import AgentDefaults
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from core.agents.agents import Agent, AgentStore
    from core.agents.temporary import TemporaryAgent, TemporaryAgentConfig
    from core.models.models import ModelRegistry
    from core.projects.projects import Project
    from core.projects.store import ProjectStore
    from core.providers.providers import ProviderRegistry
    from core.runtime.interfaces import ProviderCredentialResolverProtocol
    from core.sessions import SessionAddress, WorkingProjectChoice

__all__ = [
    "AGENT_OVERRIDE_FIELDS",
    "AgentOverrides",
    "AgentResolutionError",
    "AgentResolver",
    "SessionMetadataStore",
    "ConfigAgent",
    "ConnectionRestrictedModel",
    "CredentialProbe",
    "GlobalAgentDefaultsProvider",
    "ModelConfigurationChecker",
    "ModelConfigurationError",
    "ModelProbe",
    "ProjectSkillNamesProvider",
    "ProviderProbe",
    "ResolutionAgentNotFoundError",
    "ResolutionProjectNotFoundError",
    "RuntimeAgent",
    "WorkingProjectMissingError",
    "build_agent_resolver",
    "effective_project_allowed_skills",
    "resolve_prompt_project",
    "resolve_skill_scope",
    "runtime_agent_body",
]


# Project Agent resolution and the Project and Model checks applied to other
# Agents read Project, repository and configuration files; the async variants
# run them here. Identity Agent reads and temporary bindings run on the Session
# database's pool instead. The first resolution in a Project whose Team is not
# cached yet also lists its Session-owning Agents here, for the scan report.
_RESOLUTION_WORKERS = BoundedWorkerPool(name="agent-resolution", max_workers=4)

# A Librarian Session cannot run once the Agent whose Skills it maintains is gone.
SKILL_SUBJECT_MISSING_MESSAGE = (
    "This Session works on the Skills of Agent {agent_id}, which no longer exists, "
    "so it cannot continue."
)
# An Agent's default Project names where its new Sessions work.
DEFAULT_PROJECT_MISSING_MESSAGE = (
    "Agent {agent_id} starts new Sessions in Project {project_id}, which no longer exists. "
    "Choose another default Project for the Agent."
)
# A Team Agent's Session works in the Team's Project, never in another one.
TEAM_WORKING_PROJECT_MESSAGE = (
    "A Session of Team Agent {agent_id}@{project_id} works in Project {project_id}; "
    "it cannot work in another Project."
)
# What a Session read finds when the Session does not exist (yet).
_NO_SESSION = object()


class SessionMetadataStore(Protocol):
    """The Session metadata operations the resolver needs for Agent overrides and bindings."""

    def metadata_value(self, address: SessionAddress, key: str) -> Any: ...

    async def metadata_value_async(self, address: SessionAddress, key: str) -> Any: ...

    def mutate_metadata(self, address: SessionAddress, mutation: Callable[[Any], None]) -> Any: ...

    async def run_async(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any: ...


def _profile_skill_allowed(profile: AgentProfile, name: str) -> bool:
    return rules_allow(profile.skill_rules, name)


def _no_project_skills(_project_id: str) -> frozenset[str]:
    """Default project-skill probe: a project with no own skills (bundled-only)."""
    return frozenset()


def _bad_model_finding(member: AgentProfile) -> ScanFinding:
    """Build a ``BAD_MODEL`` finding for a scanned agent's unconfigured model."""
    return ScanFinding(
        type=FindingType.BAD_MODEL,
        detail=(
            f"model '{member.model}' is not configured in this instance "
            f"(unknown provider/model or no usable connection)"
        ),
        agent_id=member.agent_id,
        source_path=member.source_path,
    )


def _identity_resolution_error(error: Exception) -> AgentResolutionError:
    """Wrap an Identity Agent store failure, keeping a missing Agent precise."""
    from core.agents.agents import AgentNotFoundError

    if isinstance(error, AgentNotFoundError):
        return ResolutionAgentNotFoundError(str(error))
    return AgentResolutionError(str(error))


def _session_address(project_id: str | None, agent_id: str, session_id: str) -> SessionAddress:
    from core.sessions import SessionAddress

    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)


def _require_one_override_source(
    session_id: str | None, new_session_overrides: AgentOverrides | None
) -> None:
    if session_id is not None and new_session_overrides is not None:
        raise ValueError("an existing Session already has its own Agent overrides")


def _require_temporary_binding(agent: TemporaryAgent | None) -> TemporaryAgent:
    if agent is None:
        raise AgentResolutionError("temporary Session binding is unavailable")
    return agent


def _orphan_finding(agent_id: str, detail: str) -> ScanFinding:
    """Build an ``ORPHAN`` finding for an anchor pointer at a non-team agent id.

    Pointer-origin findings carry no ``source_path`` — the pointer lives in the
    anchor (``project.json`` / the sessions subtree), not in a repo source file.
    """
    return ScanFinding(type=FindingType.ORPHAN, detail=detail, agent_id=agent_id)


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
        self._team_cache: dict[str, ScanResult] = {}

    def resolve_agent(
        self,
        project_id: str | None,
        agent_id: str,
        *,
        session_id: str | None = None,
        new_session_overrides: AgentOverrides | None = None,
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
        """
        _require_one_override_source(session_id, new_session_overrides)
        if project_id is None:
            agent: RuntimeAgent = self._resolve_identity_agent(agent_id)
        else:
            agent = self._resolve_config_agent(project_id, agent_id)
        if session_id is None:
            return self._apply_overrides(agent, new_session_overrides or AgentOverrides())
        address = _session_address(project_id, agent_id, session_id)
        agent = self._apply_overrides(agent, self.session_overrides(address))
        key = _skill_binding_key(agent) if project_id is None else None
        if key is not None:
            agent = self._bind_skill_subject(agent, self._session_value(address, key))
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
                self._resolve_config_agent, project_id, agent_id
            )
        else:
            from core.agents.agents import AgentError

            try:
                agent = await self._agents.get_async(agent_id)
            except AgentError as error:
                raise _identity_resolution_error(error) from error
        if not overrides.is_empty:
            agent = await _RESOLUTION_WORKERS.run(self._apply_overrides, agent, overrides)
        key = _skill_binding_key(agent) if project_id is None else None
        if address is not None and key is not None:
            subject_id = await self._session_value_async(address, key)
            if subject_id is not None:
                agent = await _RESOLUTION_WORKERS.run(self._bind_skill_subject, agent, subject_id)
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
        if project_id is None:
            stored = self._stored_working_project(agent.id, session_id)
            if stored is not _NO_SESSION:
                return self._require_session_project(cast("str | None", stored))
        return self.new_session_working_project(project_id, agent, requested)

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
        if project_id is None:
            stored = await self._stored_working_project_async(agent.id, session_id)
            if stored is not _NO_SESSION:
                return await _RESOLUTION_WORKERS.run(
                    self._require_session_project, cast("str | None", stored)
                )
        return await _RESOLUTION_WORKERS.run(
            self.new_session_working_project, project_id, agent, requested
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
        if project_id is not None:
            if requested is not AGENT_DEFAULT_PROJECT:
                raise AgentResolutionError(
                    TEAM_WORKING_PROJECT_MESSAGE.format(agent_id=agent.id, project_id=project_id)
                )
            return project_id
        if requested is AGENT_DEFAULT_PROJECT:
            default = getattr(agent, "root_project_id", None)
            if default is not None and not self._projects.exists(default):
                raise AgentResolutionError(
                    DEFAULT_PROJECT_MISSING_MESSAGE.format(agent_id=agent.id, project_id=default)
                )
            return cast("str | None", default)
        if requested is not None and not self._projects.exists(requested):
            raise ResolutionProjectNotFoundError(f"Project not found: {requested}")
        return requested

    async def session_working_project_async(self, address: SessionAddress) -> str | None:
        """Return the Project the Session at *address* works in, ``None`` for the Workspace.

        A Project Session works in its address Project; a Session of an Identity
        Agent in the Project it was created with (a missing Session has none).
        Raises :class:`WorkingProjectMissingError` once that Project no longer
        exists. The Session read runs on the Session database's pool, the Project
        check on the ``agent-resolution`` pool.
        """
        if address.project_id is not None:
            return address.project_id
        stored = await self._stored_working_project_async(address.agent_id, address.session_id)
        if stored is _NO_SESSION:
            return None
        return await _RESOLUTION_WORKERS.run(
            self._require_session_project, cast("str | None", stored)
        )

    def _require_session_project(self, project_id: str | None) -> str | None:
        """Return a Session's stored working Project once it is known to exist."""
        if project_id is not None and not self._projects.exists(project_id):
            raise WorkingProjectMissingError(project_id)
        return project_id

    def _stored_working_project(self, agent_id: str, session_id: str | None) -> object:
        """Return the working Project a Session stores, or ``_NO_SESSION`` without one."""
        if session_id is None or self._sessions is None:
            return _NO_SESSION
        from core.sessions import SESSION_WORKING_PROJECT_META_KEY, SessionNotFoundError

        try:
            return self._sessions.metadata_value(
                _session_address(None, agent_id, session_id), SESSION_WORKING_PROJECT_META_KEY
            )
        except SessionNotFoundError:
            return _NO_SESSION

    async def _stored_working_project_async(self, agent_id: str, session_id: str | None) -> object:
        """Event-Loop-safe :meth:`_stored_working_project`."""
        if session_id is None or self._sessions is None:
            return _NO_SESSION
        from core.sessions import SESSION_WORKING_PROJECT_META_KEY, SessionNotFoundError

        try:
            return await self._sessions.metadata_value_async(
                _session_address(None, agent_id, session_id), SESSION_WORKING_PROJECT_META_KEY
            )
        except SessionNotFoundError:
            return _NO_SESSION

    def session_overrides(self, address: SessionAddress) -> AgentOverrides:
        """Return the Agent overrides *address* stores (none for a missing Session)."""
        return AgentOverrides.from_stored(self._session_value(address, AGENT_OVERRIDES_META_KEY))

    async def session_overrides_async(self, address: SessionAddress) -> AgentOverrides:
        """Event-Loop-safe :meth:`session_overrides`."""
        return AgentOverrides.from_stored(
            await self._session_value_async(address, AGENT_OVERRIDES_META_KEY)
        )

    def _session_value(self, address: SessionAddress, key: str) -> Any:
        """Return one metadata value of *address*, ``None`` for a missing Session."""
        if self._sessions is None:
            return None
        from core.sessions import SessionNotFoundError

        try:
            return self._sessions.metadata_value(address, key)
        except SessionNotFoundError:
            return None

    async def _session_value_async(self, address: SessionAddress, key: str) -> Any:
        """Event-Loop-safe :meth:`_session_value`."""
        if self._sessions is None:
            return None
        from core.sessions import SessionNotFoundError

        try:
            return await self._sessions.metadata_value_async(address, key)
        except SessionNotFoundError:
            return None

    def _bind_skill_subject(self, agent: RuntimeAgent, subject_id: Any) -> RuntimeAgent:
        """Return the Librarian as a bound Session runs it: on its subject Agent's Skills.

        The runtime view names the subject (``skill_agent_id``) and takes its Skill
        selection (``allowed_skills``), so the Skill catalog, triggers and the Skill
        Tools of that Session resolve the subject's Skills; nothing else changes.
        ``None`` (an unbound Session) keeps the Librarian's own Skills. A subject that
        is not an Agent of the user any more refuses the Session.
        """
        if subject_id is None:
            return agent
        subject = self._agents.find(subject_id) if isinstance(subject_id, str) else None
        if subject is None or subject.builtin is not None:
            raise AgentResolutionError(SKILL_SUBJECT_MISSING_MESSAGE.format(agent_id=subject_id))
        return replace(
            cast("Agent", agent),
            skill_agent_id=subject.id,
            allowed_skills=list(subject.allowed_skills),
        )

    def update_session_overrides(
        self, address: SessionAddress, changes: Mapping[str, Any]
    ) -> AgentOverrides:
        """Replace or clear some of one Session's Agent overrides; return the result.

        *changes* maps override fields to their new value; ``None`` clears one.
        Fields it leaves out keep their stored value, and stored fields a newer
        vBot added survive. Every value is validated first, a Model also for
        usability, so an invalid change writes nothing.
        """
        unknown = sorted(set(changes) - set(AGENT_OVERRIDE_FIELDS))
        if unknown:
            raise ValueError("unknown Agent override: " + ", ".join(unknown))
        requested = AgentOverrides(
            **{name: value for name, value in changes.items() if value is not None}
        )
        if requested.model is not None:
            self._model_checker.require_configured(requested.model)
        if self._sessions is None:
            raise AgentResolutionError("Session Agent overrides are unavailable")
        normalized = requested.as_dict()

        def mutate(metadata: Any) -> None:
            stored = metadata.get(AGENT_OVERRIDES_META_KEY)
            merged = dict(stored) if isinstance(stored, dict) else {}
            for name in changes:
                if name in normalized:
                    merged[name] = normalized[name]
                else:
                    merged.pop(name, None)
            if merged:
                metadata[AGENT_OVERRIDES_META_KEY] = merged
            else:
                metadata.pop(AGENT_OVERRIDES_META_KEY, None)

        updated = self._sessions.mutate_metadata(address, mutate)
        return AgentOverrides.from_stored(updated.get(AGENT_OVERRIDES_META_KEY))

    async def update_session_overrides_async(
        self, address: SessionAddress, changes: Mapping[str, Any]
    ) -> AgentOverrides:
        """Event-Loop-safe :meth:`update_session_overrides` on the Session database's pool."""
        if self._sessions is None:
            raise AgentResolutionError("Session Agent overrides are unavailable")
        result: AgentOverrides = await self._sessions.run_async(
            self.update_session_overrides, address, changes
        )
        return result

    def _preload_profile(
        self, project_id: str, profile: AgentProfile, allowed: list[str]
    ) -> AgentProfile:
        from core.skills.skills import format_skill_activation_context

        body = [profile.body]
        for name in profile.preload_skills:
            if name not in allowed and "*" not in allowed:
                continue
            content = self._profile_skill_content(project_id, name, allowed)
            if content is not None:
                body.append(format_skill_activation_context(name, content))
        return replace(profile, body="\n\n".join(part for part in body if part))

    def prepare_temporary_config(
        self, config: TemporaryAgentConfig, project_id: str | None
    ) -> TemporaryAgentConfig:
        """Resolve a repository Profile within the owner's selection, for a snapshot."""
        if config.repository_profile is None:
            return config
        if project_id is None:
            raise AgentResolutionError("A repository profile requires a Project.")
        project = self._load_project(project_id)
        profile = self._read_agent_fresh(project, config.repository_profile)
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
        profile = self._preload_profile(project_id, profile, skills)
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
        return self._apply_overrides(agent, overrides)

    def _require_temporary_project(self, project_id: str | None) -> None:
        # A temporary Agent keeps its owner's Tool and Skill selection: like an
        # Identity Session working in a Project it uses the Project's directory,
        # Skills and context, not the Tool and Skill whitelists that bound the
        # Project's Team.
        if project_id is not None:
            self._load_project(project_id)

    def _apply_overrides(self, agent: RuntimeAgent, overrides: AgentOverrides) -> RuntimeAgent:
        """Return an immutable runtime view with only the overridden fields replaced."""
        if overrides.is_empty:
            return agent

        changes = overrides.agent_changes()
        if overrides.model is not None:
            self._model_checker.require_configured(overrides.model)
        if isinstance(agent, ConfigAgent):
            return replace(agent, **changes)
        from core.agents.agents import Agent
        from core.agents.temporary import TemporaryAgent

        if isinstance(agent, Agent):
            return replace(agent, **changes)
        if isinstance(agent, TemporaryAgent):
            return replace(agent, **changes)
        raise TypeError(f"unsupported RuntimeAgent implementation: {type(agent).__name__}")

    def _resolve_identity_agent(self, agent_id: str) -> Agent:
        """Resolve one persisted Identity Agent."""
        from core.agents.agents import AgentError

        try:
            return self._agents.get(agent_id)
        except AgentError as error:
            raise _identity_resolution_error(error) from error

    def _resolve_config_agent(
        self, project_id: str, agent_id: str, inherit_model: str | None = None
    ) -> ConfigAgent:
        project = self._load_project(project_id)
        team = self._project_team(project)
        if agent_id not in {member.agent_id for member in team}:
            raise ResolutionAgentNotFoundError(
                f"agent '{agent_id}' is not on project '{project_id}' team"
            )

        # Single-agent config freshness: re-read the agent's source file now so a
        # repo edit between the open-time scan and this run takes effect. The
        # cached Team only told us the agent still belongs; the live config comes
        # from disk.
        scanned = self._read_agent_fresh(project, agent_id)
        # Read the global tier once and feed it to all three chains, so one resolve
        # never reads the settings file three times (model + temp + thinking).
        global_defaults = AgentDefaults.from_dict(self._global_agent_defaults())
        if (
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
        scanned = self._preload_profile(project_id, scanned, allowed_skills)
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
        allowed_agents = _effective_allowed_agents(member, self._project_team(project))
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
        team = self._project_team(project)
        if agent_id not in {member.agent_id for member in team}:
            raise ResolutionAgentNotFoundError(
                f"agent '{agent_id}' is not on project '{project_id}' team"
            )
        scanned = self._read_agent_fresh(project, agent_id)
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
            ("agent", self._profile_model(project, scanned)),
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
        result = scan_project(
            _project_root(project),
            adapters=self._source_adapters,
            sources=project.sources,
        )
        translated = [self.translated_profile(project, member) for member in result.team]
        model_findings = self._model_findings(project, translated)
        pointer_findings = self._pointer_findings(project, result.team)
        report = result.report.with_findings(model_findings + pointer_findings)
        return replace(result, team=translated, report=report)

    def team_for_project(self, project_id: str) -> list[AgentProfile]:
        """Return the current cached Team snapshot for one registered Project."""
        return list(self._project_team(self._load_project(project_id)))

    def rescan_project(self, project: Project) -> ScanResult:
        """Re-run the project scan and refresh the cached Team for this project.

        Called at project-open and on an explicit re-scan. Returns the same
        Team + complete report as :meth:`scan_project_report` and updates the
        Team-membership cache so subsequent ``resolve_agent`` calls see the new
        Team without re-walking the repo.
        """
        result = self.scan_project_report(project)
        self._team_cache[project.project_id] = result
        return result

    def invalidate_team_cache(self, project_id: str | None = None) -> None:
        """Drop the cached Team for one project, or for all when ``None``."""
        if project_id is None:
            self._team_cache.clear()
            return
        self._team_cache.pop(project_id, None)

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
        cached = self._team_cache.get(project.project_id)
        if cached is not None:
            return cached
        # Lazy first scan: a resolve before any explicit open still works, and the
        # result is cached so the next turn does not re-walk the repo.
        return self.rescan_project(project)

    def _project_team(self, project: Project) -> list[AgentProfile]:
        return self.cached_scan(project).team

    def _load_project(self, project_id: str) -> Project:
        try:
            return self._projects.get(project_id)
        except ProjectNotFoundError as error:
            raise ResolutionProjectNotFoundError(str(error)) from error
        except ProjectError as error:
            raise AgentResolutionError(str(error)) from error

    def _read_agent_fresh(self, project: Project, agent_id: str) -> AgentProfile:
        """Re-scan the repo and return this agent's current scanned profile.

        Reads the live config from disk so a repo edit is reflected on the next
        run. If the agent vanished from the repo since the cached Team was built
        (deleted file), that is an "agent no longer exists" error rather than a
        silent fall-back to the stale cached profile.
        """
        try:
            member = read_profile(
                _project_root(project), project.sources, agent_id, self._source_adapters
            )
        except (OSError, ValueError) as error:
            raise AgentResolutionError(
                f"Cannot read repository Agent '{agent_id}': {error}"
            ) from error
        if member is not None:
            if member.unavailable_reason:
                raise AgentResolutionError(
                    f"Repository Agent '{agent_id}' is unavailable: {member.unavailable_reason}"
                )
            return member
        raise ResolutionAgentNotFoundError(
            f"agent '{agent_id}' is no longer present in project '{project.project_id}'"
        )

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

    def _profile_model(self, project: Project, scanned: AgentProfile) -> str:
        if scanned.model == "inherit":
            return ""
        return project.model_mappings.get(scanned.model, scanned.model)

    def translated_profile(self, project: Project, scanned: AgentProfile) -> AgentProfile:
        reports = list(scanned.translations)
        mapped = self._profile_model(project, scanned)
        if scanned.model == "inherit":
            reports = [entry for entry in reports if entry.setting != "model"]
            reports.append(
                Translation(
                    "model",
                    "translated",
                    "Uses the delegating caller's Model, otherwise the Project or global default.",
                )
            )
        elif mapped != scanned.model and self._model_checker.is_configured(mapped):
            reports = [entry for entry in reports if entry.setting != "model"]
            reports.append(
                Translation(
                    "model",
                    "translated",
                    f"Model wish '{scanned.model}' maps to '{mapped}'.",
                )
            )
        if (
            scanned.model
            and scanned.model != "inherit"
            and not self._model_checker.is_configured(mapped)
        ):
            reports = [entry for entry in reports if entry.setting != "model"]
            reports.append(
                Translation(
                    "model",
                    "not_supported",
                    f"Model wish '{scanned.model}' is unmapped or unusable; "
                    "the Project or global default is used.",
                )
            )
        for setting in project.overrides.get(scanned.agent_id, {}):
            reports = [
                replace(
                    entry,
                    status="overridden",
                    detail="The explicit vBot Tool policy replaces this imported restriction.",
                )
                if setting == "tool_access"
                and entry.setting not in {"permission.skill", "permission.task"}
                and entry.setting.startswith(
                    (
                        "tools",
                        "permission",
                        "disallowedTools",
                        "hooks",
                        "sandbox",
                        "approval",
                        "isolation",
                        "readonly",
                    )
                )
                else entry
                for entry in reports
                if entry.setting != setting
                and not (
                    setting == "thinking_effort"
                    and entry.setting in {"effort", "model_reasoning_effort"}
                )
            ]
            reports.append(
                Translation(
                    setting,
                    "overridden",
                    "The explicit vBot override replaces this repository setting.",
                )
            )
        allowed = effective_project_allowed_skills(
            project, self._project_skill_names(project.project_id)
        )
        for name in scanned.preload_skills:
            if name not in allowed or not _profile_skill_allowed(scanned, name):
                reports.append(
                    Translation(
                        f"skills.{name}",
                        "not_supported",
                        "Skill is absent or outside the Project Skill selection; it cannot"
                        " be preloaded.",
                    )
                )
            elif self._profile_skill_content(project.project_id, name, allowed) is None:
                reports.append(
                    Translation(
                        f"skills.{name}",
                        "not_supported",
                        "Skill is unavailable or unreadable; it cannot be preloaded.",
                    )
                )
        return replace(scanned, translations=tuple(reports))

    def _model_findings(self, project: Project, team: list[AgentProfile]) -> list[ScanFinding]:
        return [
            _bad_model_finding(member)
            for member in team
            if member.model
            and member.model != "inherit"
            and not self._model_checker.is_configured(_overridden_model(project, member.agent_id))
            and not self._model_checker.is_configured(self._profile_model(project, member))
        ]

    def _pointer_findings(self, project: Project, team: list[AgentProfile]) -> list[ScanFinding]:
        """Build the scan's ``ORPHAN`` findings for the project's anchor pointers.

        The pointers live in the anchor, not the repo, so the structural scan
        cannot see them: the project's ``default_agent`` and every session-owning
        agent under the anchor must name an agent the current scan produced. A
        pointer at an id the scan did not yield is unclean under what exists — the
        default agent cannot resolve, and orphaned sessions belong to an agent that
        is no longer in the repo (renamed or deleted). A project without a default
        agent and without sessions yields no findings (clean empty is normal).
        """
        team_ids = {member.agent_id for member in team}
        findings: list[ScanFinding] = []
        if project.default_agent and project.default_agent not in team_ids:
            findings.append(
                _orphan_finding(
                    project.default_agent,
                    f"default agent '{project.default_agent}' is not in the scanned team",
                )
            )
        for owner in self._projects.session_owning_agents(project.project_id):
            if owner not in team_ids:
                findings.append(
                    _orphan_finding(
                        owner,
                        f"agent '{owner}' owns sessions in this project "
                        f"but is not in the scanned team",
                    )
                )
        return findings


def runtime_agent_body(agent: RuntimeAgent) -> str:
    """Return the verbatim prompt body of a runtime agent, or ``""``.

    The single seam that maps the resolver's two agent forms onto the prompt
    builder's ``agent_body`` parameter: a :class:`ConfigAgent` carries an imported
    body, an identity ``Agent`` carries none. Keeping this here (not in the prompt
    domain) lets prompt assembly stay on its Protocols without importing
    ``ConfigAgent`` or probing types — the chat loop calls this on the agent it
    already resolved and hands the result over as an explicit argument.
    """
    if isinstance(agent, ConfigAgent):
        return agent.body
    # Owner-managed temporary Agents carry their reviewed profile instructions
    # in the protected Session binding rather than an Identity workspace.
    instructions = getattr(agent, "instructions", "")
    return instructions if isinstance(instructions, str) else ""


def resolve_prompt_project(
    projects: ProjectStore, working_project_id: str | None
) -> Project | None:
    """Return the project whose auto-load files belong in this run's system prompt.

    The one policy shared by the chat loop and the prompt-preview RPC, so the
    preview can never drift from what a run actually sends:

    - ``working_project_id`` set → the Project the Session works in.
    - ``working_project_id is None`` → no Project context.

    Kept beside :func:`runtime_agent_body` for the same reason: the chat loop and
    the RPC call it with the already-resolved working Project, so prompt assembly
    never learns working-Project or Session-addressing policy itself.
    """
    if working_project_id is not None:
        project = projects.get(working_project_id)
        if not Path(project.cwd).is_dir():
            raise ProjectError(f"Project repository is unavailable: {project.cwd}")
        return replace(project, auto_load=project.instruction_files)
    return None


def resolve_skill_scope(
    project_id: str | None, prompt_project: Project | None, agent: RuntimeAgent
) -> tuple[str | None, str | None]:
    """Return ``(skill_project_id, identity_agent_id)`` for a run's skill pool.

    The one skill-scoping policy shared by the chat loop, the prompt-preview RPC,
    and ``$``-autocomplete, so no surface can drift from the pool a run actually
    activates against. ``prompt_project`` is the already-resolved working Project
    from :func:`resolve_prompt_project` (pure — no second store lookup here), and
    ``agent`` the resolved runtime Agent:

    - ``skill_project_id`` — the effective skill project: the run's own project,
      or, for an identity Session working in a Project (``project_id is None``),
      that Project; else ``None``.
    - ``identity_agent_id`` — the agent's private-skill layer applies to identity
      runs only: a project run executes a config agent whose project-local slug
      must never resolve a same-named identity agent's private home (private
      skills bypass the project skill whitelist as always-allowed). It names the
      Agent whose Skills the run works on (``skill_subject_id``): the Agent
      itself, or the subject of a Librarian Session.
    """
    if project_id is not None:
        return project_id, None
    from core.agents import skill_subject_id

    working_project_id = prompt_project.project_id if prompt_project is not None else None
    return working_project_id, skill_subject_id(agent)


def _skill_binding_key(agent: RuntimeAgent) -> str | None:
    """Return the Session metadata key binding *agent* to a Skill subject, if any.

    Only Sessions of the Librarian have one.
    """
    from core.agents import SKILL_AGENT_ID_KEY, is_librarian

    return SKILL_AGENT_ID_KEY if is_librarian(agent) else None


def _project_root(project: Project) -> Path:
    """Return the repo root a project's scan runs against (its cwd)."""
    return Path(project.cwd)


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
