"""Session Agent overrides and Working Project resolution."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from core.projects._resolution_context import _RESOLUTION_WORKERS, _session_address
from core.projects._runtime_agent import (
    AGENT_OVERRIDE_FIELDS,
    AGENT_OVERRIDES_META_KEY,
    AgentOverrides,
    AgentResolutionError,
    ConfigAgent,
    ResolutionProjectNotFoundError,
    RuntimeAgent,
    WorkingProjectMissingError,
)
from core.sessions import AGENT_DEFAULT_PROJECT

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.agents.agents import Agent
    from core.permissions import PermissionCeiling
    from core.projects.resolver import AgentResolver
    from core.sessions import SessionAddress, SessionWorkingProject, WorkingProjectChoice


class SessionResolution:
    def __init__(self, owner: AgentResolver) -> None:
        self._owner = owner

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
            if stored is not None:
                return self._require_session_project(stored)
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
            if stored is not None:
                return await _RESOLUTION_WORKERS.run(self._require_session_project, stored)
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
            if default is not None and not self._owner._projects.exists(default):
                raise AgentResolutionError(
                    DEFAULT_PROJECT_MISSING_MESSAGE.format(agent_id=agent.id, project_id=default)
                )
            return cast("str | None", default)
        if requested is not None and not self._owner._projects.exists(requested):
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
        if stored is None:
            return None
        return await _RESOLUTION_WORKERS.run(self._require_session_project, stored)

    def _require_session_project(self, binding: SessionWorkingProject) -> str | None:
        """Require the bound Project, never a replacement reusing its public id."""
        project_id = binding.project_id
        if project_id is not None and (
            binding.archived or not self._owner._projects.exists(project_id)
        ):
            raise WorkingProjectMissingError(project_id)
        return project_id

    def _stored_working_project(
        self, agent_id: str, session_id: str | None
    ) -> SessionWorkingProject | None:
        """Return the Session's Working Project binding, or ``None`` without a Session."""
        if session_id is None or self._owner._sessions is None:
            return None
        from core.sessions import SessionNotFoundError

        try:
            return self._owner._sessions.working_project(
                _session_address(None, agent_id, session_id)
            )
        except SessionNotFoundError:
            return None

    async def _stored_working_project_async(
        self, agent_id: str, session_id: str | None
    ) -> SessionWorkingProject | None:
        """Event-Loop-safe :meth:`_stored_working_project`."""
        if session_id is None or self._owner._sessions is None:
            return None
        from core.sessions import SessionNotFoundError

        try:
            return await self._owner._sessions.working_project_async(
                _session_address(None, agent_id, session_id)
            )
        except SessionNotFoundError:
            return None

    def session_overrides(self, address: SessionAddress) -> AgentOverrides:
        """Return the Agent overrides *address* stores (none for a missing Session)."""
        return AgentOverrides.from_stored(self._session_value(address, AGENT_OVERRIDES_META_KEY))

    async def session_overrides_async(self, address: SessionAddress) -> AgentOverrides:
        """Event-Loop-safe :meth:`session_overrides`."""
        return AgentOverrides.from_stored(
            await self._session_value_async(address, AGENT_OVERRIDES_META_KEY)
        )

    def capture_session_settings(
        self, address: SessionAddress, agent: RuntimeAgent
    ) -> AgentOverrides:
        """Fill uncaptured Model settings once, preserving concurrent explicit choices."""
        if self._owner._sessions is None:
            raise AgentResolutionError("Session Agent overrides are unavailable")

        def capture(metadata: Any) -> None:
            stored = metadata.get(AGENT_OVERRIDES_META_KEY) or {}
            settings = AgentOverrides.from_stored(stored).capture(agent)
            metadata[AGENT_OVERRIDES_META_KEY] = {**stored, **settings.as_dict()}

        _, updated = self._owner._sessions.ensure_metadata(address, capture)
        return AgentOverrides.from_stored(updated.get(AGENT_OVERRIDES_META_KEY))

    def join_session_ceiling(
        self, address: SessionAddress, ceiling: PermissionCeiling | None
    ) -> None:
        """Narrow the Session's stored ceiling by *ceiling*, never widening it.

        Every later Run of the Session applies the joined ceiling, follow-up Runs
        included. Raises ``ValueError`` when the stored ceiling cannot be read.
        """
        if ceiling is None:
            return
        if self._owner._sessions is None:
            raise AgentResolutionError("Session Agent overrides are unavailable")

        def join(metadata: Any) -> None:
            stored = metadata.get(AGENT_OVERRIDES_META_KEY) or {}
            joined = AgentOverrides.from_stored(stored).with_ceiling(ceiling)
            metadata[AGENT_OVERRIDES_META_KEY] = {
                **stored,
                "permission_ceiling": dict(joined.permission_ceiling or {}),
            }

        self._owner._sessions.ensure_metadata(address, join)

    def preserve_agent_sessions(self, project_id: str | None, agent_id: str) -> None:
        """Keep existing Sessions' choices before changing their Agent's start values.

        Older Sessions can still inherit either value. Materialize those values
        under the caller's configuration mutation guard before the default moves.
        This does not validate availability: replacing a broken Model must work.
        """
        if self._owner._sessions is None:
            return
        agent = self._owner.resolve_agent(project_id, agent_id, require_model=False)
        for address in self._owner._sessions.list_addresses(
            project_id, agent_id=agent_id, exclude_owner_managed=True
        ):
            self.capture_session_settings(address, agent)

    async def capture_session_settings_async(
        self, address: SessionAddress, agent: RuntimeAgent
    ) -> AgentOverrides:
        if self._owner._sessions is None:
            raise AgentResolutionError("Session Agent overrides are unavailable")
        result: AgentOverrides = await self._owner._sessions.run_async(
            self.capture_session_settings, address, agent
        )
        return result

    def remember_session_settings(
        self, address: SessionAddress, changes: Mapping[str, Any]
    ) -> AgentOverrides:
        """Set this Session's choices and the Agent's start values for new Sessions."""
        if set(changes) - {"model", "thinking_effort"}:
            raise ValueError("Only model and thinking_effort can be remembered for new Sessions")
        requested = AgentOverrides(
            **{key: value for key, value in changes.items() if value is not None}
        )
        if requested.model is not None:
            self._owner.require_model_configured(requested.model)
        if "model" in changes and changes["model"] is None:
            raise ValueError("A remembered Model must be selected explicitly")
        if self._owner._sessions is None:
            raise AgentResolutionError("Session Agent overrides are unavailable")
        self._owner._sessions.get(address)
        self.preserve_agent_sessions(address.project_id, address.agent_id)
        agent_changes = dict(changes)
        if agent_changes.get("thinking_effort", False) is None:
            agent_changes["thinking_effort"] = ""
        if address.project_id is None:
            self._owner._agents.update(address.agent_id, **agent_changes)
        else:
            for field, value in agent_changes.items():
                self._owner._projects.set_override(
                    address.project_id, address.agent_id, field, value
                )
        self.update_session_overrides(address, changes)
        agent = self._owner.resolve_agent(address.project_id, address.agent_id, require_model=False)
        return self.capture_session_settings(address, agent)

    def _session_value(self, address: SessionAddress, key: str) -> Any:
        """Return one metadata value of *address*, ``None`` for a missing Session."""
        if self._owner._sessions is None:
            return None
        from core.sessions import SessionNotFoundError

        try:
            return self._owner._sessions.metadata_value(address, key)
        except SessionNotFoundError:
            return None

    async def _session_value_async(self, address: SessionAddress, key: str) -> Any:
        """Event-Loop-safe :meth:`_session_value`."""
        if self._owner._sessions is None:
            return None
        from core.sessions import SessionNotFoundError

        try:
            return await self._owner._sessions.metadata_value_async(address, key)
        except SessionNotFoundError:
            return None

    def _bind_skill_subject(
        self, agent: RuntimeAgent, subject_id: Any, subject_created_at: Any
    ) -> RuntimeAgent:
        """Return the Librarian as a bound Session runs it: on its subject Agent's Skills.

        The runtime view names the subject (``skill_agent_id``) and takes its Skill
        selection (``allowed_skills``), so the Skill catalog, triggers and the Skill
        Tools of that Session resolve the subject's Skills; nothing else changes.
        ``None`` (an unbound Session) keeps the Librarian's own Skills. A subject that
        is not the original Agent of the user any more refuses the Session.
        """
        if subject_id is None:
            return agent
        subject = self._owner._agents.find(subject_id) if isinstance(subject_id, str) else None
        if subject is None or subject.builtin is not None:
            raise AgentResolutionError(SKILL_SUBJECT_MISSING_MESSAGE.format(agent_id=subject_id))
        if not isinstance(subject_created_at, str) or not subject_created_at:
            raise AgentResolutionError(
                "This Librarian Session has no verified Agent identity. "
                "Its history remains available; start a new Librarian pass to continue."
            )
        if subject.created_at != subject_created_at:
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
        unknown = sorted(set(changes) - {*AGENT_OVERRIDE_FIELDS, "thinking_effort_default"})
        if unknown:
            raise ValueError("unknown Agent override: " + ", ".join(unknown))
        requested = AgentOverrides(
            **{name: value for name, value in changes.items() if value is not None}
        )
        if requested.model is not None:
            self._owner._model_checker.require_configured(requested.model)
        if self._owner._sessions is None:
            raise AgentResolutionError("Session Agent overrides are unavailable")
        normalized = requested.as_dict()

        def mutate(metadata: Any) -> None:
            stored = metadata.get(AGENT_OVERRIDES_META_KEY)
            merged = dict(stored) if isinstance(stored, dict) else {}
            if requested.thinking_effort_default:
                merged.pop("thinking_effort", None)
            for name in changes:
                if name == "thinking_effort":
                    merged.pop("thinking_effort_default", None)
                if name in normalized:
                    merged[name] = normalized[name]
                else:
                    merged.pop(name, None)
            if requested.thinking_effort_default:
                merged["thinking_effort_default"] = True
            if merged:
                metadata[AGENT_OVERRIDES_META_KEY] = merged
            else:
                metadata.pop(AGENT_OVERRIDES_META_KEY, None)

        updated = self._owner._sessions.mutate_metadata(address, mutate)
        return AgentOverrides.from_stored(updated.get(AGENT_OVERRIDES_META_KEY))

    async def update_session_overrides_async(
        self, address: SessionAddress, changes: Mapping[str, Any]
    ) -> AgentOverrides:
        """Event-Loop-safe :meth:`update_session_overrides` on the Session database's pool."""
        if self._owner._sessions is None:
            raise AgentResolutionError("Session Agent overrides are unavailable")
        result: AgentOverrides = await self._owner._sessions.run_async(
            self.update_session_overrides, address, changes
        )
        return result

    def _apply_overrides(
        self, agent: RuntimeAgent, overrides: AgentOverrides, *, require_model: bool = True
    ) -> RuntimeAgent:
        """Return an immutable runtime view with only the overridden fields replaced."""
        if overrides.is_empty:
            return agent

        changes = overrides.agent_changes()
        if require_model and overrides.model is not None:
            self._owner._model_checker.require_configured(overrides.model)
        if isinstance(agent, ConfigAgent):
            return replace(agent, **changes)
        from core.agents.agents import Agent
        from core.agents.temporary import TemporaryAgent

        if isinstance(agent, Agent):
            return replace(agent, **changes)
        if isinstance(agent, TemporaryAgent):
            return replace(agent, **changes)
        raise TypeError(f"unsupported RuntimeAgent implementation: {type(agent).__name__}")


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
