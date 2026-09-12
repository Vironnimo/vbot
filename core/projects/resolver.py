"""Identity, Project and temporary Agent resolution with fresh source reads."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

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
    _config_temperature_source,
    _config_thinking_effort_source,
    _config_tool_access_source,
    _effective_allowed_agents,
    _identity_optional_source,
    _identity_string_list_source,
    _identity_string_source,
    _overridden_model,
    _project_agent_tool_access,
    _project_agent_tools,
    _resolve_temperature,
    _resolve_thinking_effort,
    _temporary_project_allowed_skills,
    _temporary_project_tool_access,
    effective_project_allowed_skills,
)
from core.projects._runtime_agent import (
    AgentResolutionError,
    AgentRunOverrides,
    ConfigAgent,
    GlobalAgentDefaultsProvider,
    ProjectSkillNamesProvider,
    RuntimeAgent,
)
from core.projects.projects import ProjectError
from core.projects.scan_report import FindingType, ScanFinding
from core.projects.scanners.base import (
    DetectorRegistration,
    ScannedAgent,
    ScanResult,
    scan_project,
)
from core.settings import AgentDefaults

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Any

    from core.agents.agents import Agent, AgentStore
    from core.agents.temporary import TemporaryAgent, TemporaryAgentConfig
    from core.models.models import ModelRegistry
    from core.projects.projects import Project
    from core.projects.store import ProjectStore
    from core.providers.providers import ProviderRegistry
    from core.runtime.interfaces import ProviderCredentialResolverProtocol

__all__ = [
    "AgentResolutionError",
    "AgentResolver",
    "AgentRunOverrides",
    "ConfigAgent",
    "ConnectionRestrictedModel",
    "CredentialProbe",
    "GlobalAgentDefaultsProvider",
    "ModelConfigurationChecker",
    "ModelConfigurationError",
    "ModelProbe",
    "ProjectSkillNamesProvider",
    "ProviderProbe",
    "RuntimeAgent",
    "build_agent_resolver",
    "effective_project_allowed_skills",
    "resolve_prompt_project",
    "resolve_skill_scope",
    "resolve_working_project_id",
    "runtime_agent_body",
]


def _no_project_skills(_project_id: str) -> frozenset[str]:
    """Default project-skill probe: a project with no own skills (bundled-only)."""
    return frozenset()


def _bad_model_finding(member: ScannedAgent) -> ScanFinding:
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
        detector_registry: list[DetectorRegistration] | None = None,
        project_skill_names: ProjectSkillNamesProvider | None = None,
        temporary_agents: Any | None = None,
    ) -> None:
        self._agents = agents
        self._projects = projects
        self._model_checker = model_checker
        self._global_agent_defaults = global_agent_defaults
        # Captured once; ``scan_project`` falls back to its own default registry
        # when this is ``None``, so tests can inject a custom registry.
        self._detector_registry = detector_registry
        # Project-skill probe for config-agent skill resolution; defaults to "no
        # project skills" so a resolver built without it degrades to bundled-only
        # rather than failing (the runtime always wires the real probe).
        self._project_skill_names = project_skill_names or _no_project_skills
        self._temporary_agents = temporary_agents
        # Team-scan cache keyed by project id. A run reads from here; an explicit
        # re-scan / project-open repopulates it via ``rescan_project``.
        self._team_cache: dict[str, ScanResult] = {}

    def resolve_agent(
        self,
        project_id: str | None,
        agent_id: str,
        *,
        run_overrides: AgentRunOverrides | None = None,
    ) -> RuntimeAgent:
        """Resolve one agent to a runnable :class:`RuntimeAgent`.

        ``project_id is None`` returns the store identity agent unchanged. A set
        ``project_id`` returns a :class:`ConfigAgent` synthesized from the
        project's Team scan plus the resolved model. Optional Run overrides are
        applied only to the returned immutable runtime view after normal
        resolution; they never mutate either source configuration. Raises
        :class:`AgentResolutionError` for an unknown project/agent or a config
        agent whose model chain fell through, and
        :class:`ModelConfigurationError` for an unusable explicit Run Model.
        """
        if project_id is None:
            agent: RuntimeAgent = self._resolve_identity_agent(agent_id)
        else:
            agent = self._resolve_config_agent(project_id, agent_id)
        return self._apply_run_overrides(agent, run_overrides)

    def resolve_temporary_agent(
        self,
        address: Any,
        *,
        generation_id: str,
        run_overrides: AgentRunOverrides | None = None,
    ) -> RuntimeAgent:
        """Resolve only an exact canonical temporary Session generation."""
        if self._temporary_agents is None:
            raise AgentResolutionError("temporary Session is unavailable")
        agent = self._temporary_agents.resolve(address, generation_id=generation_id)
        if agent is None:
            raise AgentResolutionError("temporary Session binding is unavailable")
        project_id = getattr(address, "project_id", None)
        agent = self._apply_temporary_project(agent, project_id)
        return self._apply_run_overrides(agent, run_overrides)

    def preview_temporary_agent(
        self, config: TemporaryAgentConfig, project_id: str | None = None
    ) -> TemporaryAgent:
        """Resolve editor configuration through the same ceilings without creating a Session."""
        from core.agents.temporary import TemporaryAgent

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
            thinking_effort=config.thinking_effort,
        )
        return self._apply_temporary_project(agent, project_id)

    def _apply_temporary_project(
        self, agent: TemporaryAgent, project_id: str | None
    ) -> TemporaryAgent:
        if project_id is not None:
            project = self._load_project(project_id)
            tool_access = _temporary_project_tool_access(project, agent.tool_access)
            allowed_skills = _temporary_project_allowed_skills(
                agent.allowed_skills,
                effective_project_allowed_skills(project, self._project_skill_names(project_id)),
            )
            tools = {
                name: settings
                for name, settings in agent.tools.items()
                if name in tool_access.allowed and name not in tool_access.denied
            }
            agent = replace(
                agent,
                tool_access=tool_access,
                allowed_skills=allowed_skills,
                tools=tools,
            )
        return agent

    def _apply_run_overrides(
        self,
        agent: RuntimeAgent,
        run_overrides: AgentRunOverrides | None,
    ) -> RuntimeAgent:
        """Return an immutable runtime view with only the admitted fields replaced."""
        if run_overrides is None or run_overrides.is_empty:
            return agent

        changes: dict[str, Any] = {}
        if run_overrides.model is not None:
            self._model_checker.require_configured(run_overrides.model)
            changes["model"] = run_overrides.model
        if run_overrides.thinking_effort is not None:
            changes["thinking_effort"] = run_overrides.thinking_effort
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
            raise AgentResolutionError(str(error)) from error

    def _resolve_config_agent(self, project_id: str, agent_id: str) -> ConfigAgent:
        project = self._load_project(project_id)
        team = self._project_team(project)
        if agent_id not in {member.agent_id for member in team}:
            raise AgentResolutionError(f"agent '{agent_id}' is not on project '{project_id}' team")

        # Single-agent config freshness: re-read the agent's source file now so a
        # repo edit between the open-time scan and this run takes effect. The
        # cached Team only told us the agent still belongs; the live config comes
        # from disk.
        scanned = self._read_agent_fresh(project, agent_id)
        # Read the global tier once and feed it to all three chains, so one resolve
        # never reads the settings file three times (model + temp + thinking).
        global_defaults = AgentDefaults.from_dict(self._global_agent_defaults())
        resolved_model = self._resolve_model_or_raise(scanned, project, global_defaults)
        resolved_temperature = _resolve_temperature(scanned, project, global_defaults)
        resolved_thinking_effort = _resolve_thinking_effort(scanned, project, global_defaults)
        tool_access = _project_agent_tool_access(project, scanned)
        allowed_skills = effective_project_allowed_skills(
            project, self._project_skill_names(project_id)
        )
        allowed_agents = _effective_allowed_agents(scanned, team)
        tools = _project_agent_tools(tool_access, allowed_agents)
        return _build_config_agent(
            scanned,
            resolved_model,
            resolved_temperature,
            resolved_thinking_effort,
            tool_access,
            allowed_skills,
            tools,
            project.overrides.get(agent_id, {}).get("compaction_policy"),
            project_id=project_id,
        )

    def effective_config(self, project_id: str | None, agent_id: str) -> dict[str, dict[str, Any]]:
        """Report, per run field, the effective value and the tier that supplied it.

        The provenance-aware companion to :meth:`resolve_agent`, sharing the *same*
        chain logic so the two can never drift. Each field maps to
        ``{"value": ..., "source": ...}``:

        - **Config agents** (``project_id`` set): fields ``model``, ``temperature``,
          ``thinking_effort``. Sources: ``"override"`` (the vBot override layer),
          ``"agent"`` (the repo-declared scanned value), ``"project_default"``,
          ``"global_default"``, or ``None`` when every tier fell through. Unlike
          :meth:`resolve_agent`, a model chain that falls all the way through does
          **not** raise here — it returns ``{"value": None, "source": None}`` (an
          unknown project/agent still raises :class:`AgentResolutionError`). Each
          tier is gated by the same ``is_configured`` model check, so an unconfigured
          override/agent/default model falls through exactly as at run time.
        - **Identity agents** (``project_id is None``): fields ``model``,
          ``fallback_models``, ``temperature``, ``thinking_effort``. Sources:
          ``"agent"`` (the own persisted value) or ``"global_default"``, or ``None``
          when neither has a value. No ``is_configured`` gating — this mirrors
          ``core.agents._config.apply_defaults`` exactly: a default applies when the persisted
          ``model`` is ``""`` / ``fallback_models`` is ``[]`` or
          ``temperature``/``thinking_effort`` is ``None``.
        """
        if project_id is None:
            return self._identity_effective_config(agent_id)
        return self._config_effective_config(project_id, agent_id)

    def effective_tools_for_member(self, project: Project, member: ScannedAgent) -> dict[str, Any]:
        """Project repository-owned Tool settings for one current Team member."""
        tool_access = _project_agent_tool_access(project, member)
        allowed_agents = _effective_allowed_agents(member, self._project_team(project))
        return _project_agent_tools(tool_access, allowed_agents)

    def effective_config_for_member(
        self, project: Project, scanned: ScannedAgent
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
            raise AgentResolutionError(str(error)) from error
        defaults = AgentDefaults.from_dict(self._global_agent_defaults())
        return {
            "model": _identity_string_source(raw.model, defaults.model),
            "fallback_models": _identity_string_list_source(
                raw.fallback_models, defaults.fallback_models
            ),
            "temperature": _identity_optional_source(raw.temperature, defaults.temperature),
            "thinking_effort": _identity_optional_source(
                raw.thinking_effort, defaults.thinking_effort
            ),
        }

    def _config_effective_config(self, project_id: str, agent_id: str) -> dict[str, dict[str, Any]]:
        project = self._load_project(project_id)
        team = self._project_team(project)
        if agent_id not in {member.agent_id for member in team}:
            raise AgentResolutionError(f"agent '{agent_id}' is not on project '{project_id}' team")
        scanned = self._read_agent_fresh(project, agent_id)
        global_defaults = AgentDefaults.from_dict(self._global_agent_defaults())
        return self._config_effective_from_scanned(project, scanned, global_defaults)

    def _config_effective_from_scanned(
        self, project: Project, scanned: ScannedAgent, global_defaults: AgentDefaults
    ) -> dict[str, dict[str, Any]]:
        return {
            "model": self._config_model_source(project, scanned, global_defaults),
            "temperature": _config_temperature_source(project, scanned, global_defaults),
            "thinking_effort": _config_thinking_effort_source(project, scanned, global_defaults),
            "tool_access": _config_tool_access_source(project, scanned),
        }

    def _config_model_source(
        self, project: Project, scanned: ScannedAgent, global_defaults: AgentDefaults
    ) -> dict[str, Any]:
        """Return the effective model + source, gated by ``is_configured`` per tier.

        Same chain as :meth:`_resolve_model_or_raise` (override → agent → project
        default → global default) with each tier skipped when its model is not
        configured here, but never raising: a fully-fallen-through chain reports
        ``{"value": None, "source": None}``.
        """
        tiers = (
            ("override", _overridden_model(project, scanned.agent_id)),
            ("agent", scanned.model),
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
        whose declared model is not configured in this instance (via the
        :meth:`ScanReport.with_model_findings` seam) and one ``ORPHAN`` finding per
        anchor pointer — the project's ``default_agent`` and every session-owning
        agent under the anchor — that names an agent the scan did not produce (via
        :meth:`ScanReport.with_pointer_findings`). Both checks happen **here, at
        scan time** — not lazily at first run.
        """
        result = scan_project(
            _project_root(project),
            registry=self._detector_registry,
            source_format=project.source_format,
        )
        model_findings = self._model_findings(result.team)
        pointer_findings = self._pointer_findings(project, result.team)
        report = result.report.with_model_findings(model_findings).with_pointer_findings(
            pointer_findings
        )
        return ScanResult(team=result.team, report=report)

    def team_for_project(self, project_id: str) -> list[ScannedAgent]:
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

    def _project_team(self, project: Project) -> list[ScannedAgent]:
        cached = self._team_cache.get(project.project_id)
        if cached is not None:
            return cached.team
        # Lazy first scan: a resolve before any explicit open still works, and the
        # result is cached so the next turn does not re-walk the repo.
        return self.rescan_project(project).team

    def _load_project(self, project_id: str) -> Project:
        from core.projects.projects import ProjectError

        try:
            return self._projects.get(project_id)
        except ProjectError as error:
            raise AgentResolutionError(str(error)) from error

    def _read_agent_fresh(self, project: Project, agent_id: str) -> ScannedAgent:
        """Re-scan the repo and return this agent's current scanned profile.

        Reads the live config from disk so a repo edit is reflected on the next
        run. If the agent vanished from the repo since the cached Team was built
        (deleted file), that is an "agent no longer exists" error rather than a
        silent fall-back to the stale cached profile.
        """
        fresh = scan_project(
            _project_root(project),
            registry=self._detector_registry,
            source_format=project.source_format,
        )
        for member in fresh.team:
            if member.agent_id == agent_id:
                return member
        raise AgentResolutionError(
            f"agent '{agent_id}' is no longer present in project '{project.project_id}'"
        )

    def _resolve_model_or_raise(
        self, scanned: ScannedAgent, project: Project, global_defaults: AgentDefaults
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
        overridden = _overridden_model(project, scanned.agent_id)
        global_model = global_defaults.model or ""
        for candidate in (overridden, scanned.model, project.default_model, global_model):
            if candidate and self._model_checker.is_configured(candidate):
                return candidate
        raise AgentResolutionError(
            f"agent '{scanned.agent_id}' has no usable model: override {overridden!r}, "
            f"declared {scanned.model!r}, project default {project.default_model!r}, "
            f"and the global default are all missing or unconfigured"
        )

    def _model_findings(self, team: list[ScannedAgent]) -> list[ScanFinding]:
        """Build the scan's ``BAD_MODEL`` findings for a whole Team.

        One finding per agent whose **declared** model is non-empty yet not
        configured here. An agent with no declared model is not a finding (it
        legitimately inherits the project/global default); only a declared model
        that cannot run is unclean under what exists.
        """
        return [
            _bad_model_finding(member)
            for member in team
            if member.model and not self._model_checker.is_configured(member.model)
        ]

    def _pointer_findings(self, project: Project, team: list[ScannedAgent]) -> list[ScanFinding]:
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


def resolve_working_project_id(project_id: str | None, agent: RuntimeAgent) -> str | None:
    """Return the Project captured for work admission.

    Project Config-Agent work uses its Session/address Project. Identity work uses
    only the Agent's explicit saved selection; Workspace equality has no meaning.
    """
    if project_id is not None:
        return project_id
    return getattr(agent, "root_project_id", None)


def resolve_prompt_project(
    projects: ProjectStore, working_project_id: str | None
) -> Project | None:
    """Return the project whose auto-load files belong in this run's system prompt.

    The one rooting policy shared by the chat loop and the prompt-preview RPC, so
    the preview can never drift from what a run actually sends:

    - ``working_project_id`` set → that explicitly selected Project.
    - ``working_project_id is None`` → no Project context.

    Kept beside :func:`runtime_agent_body` for the same reason: the chat loop and
    the RPC call it with the already-resolved working scope, so prompt assembly
    never learns Rooting or Session-addressing policy itself.
    """
    if working_project_id is not None:
        project = projects.get(working_project_id)
        if not Path(project.cwd).is_dir():
            raise ProjectError(f"Project repository is unavailable: {project.cwd}")
        return project
    return None


def resolve_skill_scope(
    project_id: str | None, prompt_project: Project | None, agent_id: str
) -> tuple[str | None, str | None]:
    """Return ``(skill_project_id, identity_agent_id)`` for a run's skill pool.

    The one skill-scoping policy shared by the chat loop, the prompt-preview RPC,
    and ``$``-autocomplete, so no surface can drift from the pool a run actually
    activates against. ``prompt_project`` is the already-resolved rooting result
    from :func:`resolve_prompt_project` (pure — no second store lookup here):

    - ``skill_project_id`` — the effective skill project: the run's own project,
      or, for a **rooted identity** agent (``project_id is None`` but homed in a
      registered repo), its home project; else ``None``.
    - ``identity_agent_id`` — the agent's private-skill layer applies to identity
      runs only: a project run executes a config agent whose project-local slug
      must never resolve a same-named identity agent's private home (private
      skills bypass the project skill whitelist as always-allowed).
    """
    if project_id is not None:
        return project_id, None
    rooted_project_id = prompt_project.project_id if prompt_project is not None else None
    return rooted_project_id, agent_id


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
    detector_registry: list[DetectorRegistration] | None = None,
    project_skill_names: ProjectSkillNamesProvider | None = None,
    temporary_agents: Any | None = None,
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
        detector_registry=detector_registry,
        project_skill_names=project_skill_names,
        temporary_agents=temporary_agents,
    )
