"""Uniform agent resolution: one fork, two sources, one runtime-agent form.

Every run path resolves an agent through one entry point —
:meth:`AgentResolver.resolve_agent` — instead of reaching for
``runtime.agents.get`` directly. The fork lives at exactly **one** place
(decision #3 in the plan):

- ``project_id is None`` → the **identity** path: return the store ``Agent``
  unchanged (same model chain Model → global → empty that ``AgentStore`` already
  applies, same workspace, same fields). Nothing about the identity path changes
  here; the resolver only wraps it.
- ``project_id`` set → the **config** path: the agent comes from the project's
  Team scan, and a :class:`ConfigAgent` is *synthesized* from the scanned profile
  plus a resolved model.

Both branches return a :class:`RuntimeAgent` — a structural protocol the store
``Agent`` already satisfies field-for-field, so a later run-path migration just
re-types its parameter from ``Agent`` to ``RuntimeAgent`` and keeps reading the
same attributes (model, allowed_tools, temperature, thinking_effort,
allowed_skills, fallback_model, memory_prompt_mode, workspace, id, …).

**Two freshness levels** (decision in the plan, "zwei Frische-Ebenen"):

- **Team membership** — which agents exist — comes from the *scan*, run at
  project-open and explicit re-scan, and cached per ``project_id`` here. A run
  does not re-walk the whole repo every turn.
- **Single-agent config** — model/tools/prompt for the run — is read **fresh from
  the repo file** on every ``resolve_agent`` (mirroring how identity agents
  re-read their ``agent.json`` each turn). The cached Team answers "is this agent
  on the Team?"; the fresh per-file read answers "what is its current config?".

**Model chain for config agents** (decision in the plan): agent model → project
default → global default → **error**. A model counts only when it
*exists/is configured in this instance* — its provider is registered, the model
is in the catalog, and the provider has a usable connection (credentials). An
unconfigured model is treated as **no model** and the chain falls through; if it
falls all the way through, resolution raises (the agent cannot run). The same
"exists/configured?" check produces the scan's ``BAD_MODEL`` findings, hung onto
the report through :meth:`ScanReport.with_model_findings` (the B3.1 seam).

Constructor injection only; the runtime dependencies are declared as local
structural Protocols so this module never imports ``core.runtime`` (import-cycle
risk, mirroring ``core/providers/task_client.py`` / ``usage.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.memory import MemoryPromptMode
from core.projects.scan_report import FindingType, ScanFinding
from core.projects.scanners.base import (
    DetectorRegistration,
    ScannedAgent,
    ScanResult,
    scan_project,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Any

    from core.agents.agents import Agent, AgentStore
    from core.models.models import ModelRegistry
    from core.projects.projects import Project
    from core.projects.store import ProjectStore
    from core.providers.providers import ProviderRegistry
    from core.runtime.interfaces import ProviderCredentialResolverProtocol

# Config agents are workspace-less and memory-tool-less in v1 (plan: "Config-Agent
# = kein Workspace, kein Memory-Tool"). The empty workspace path makes that
# explicit on the runtime-agent surface; the memory mode is forced off so no
# pinned-memory block is ever assembled for a config agent.
_CONFIG_AGENT_WORKSPACE = ""
_CONFIG_AGENT_MEMORY_MODE: MemoryPromptMode = "off"
# Config agents have no fallback model and no custom-prompt scope in v1; their
# prompt body comes verbatim from the scanned source instead.
_CONFIG_AGENT_FALLBACK_MODEL = ""
_CONFIG_AGENT_CUSTOM_PROMPT_ENABLED = False
# A config agent has no persisted timestamps (it is synthesized per run from the
# repo file); the runtime-agent surface still needs the fields for compatibility.
_CONFIG_AGENT_TIMESTAMP = ""


@runtime_checkable
class RuntimeAgent(Protocol):
    """The uniform run-time agent surface both resolution branches return.

    This is the contract the run consumers (chat loop, sub-agents, ``/status``,
    prompt assembly) read. The store :class:`core.agents.agents.Agent` already
    satisfies it field-for-field, so the identity branch returns the store agent
    as-is and the config branch returns a :class:`ConfigAgent` exposing the same
    surface. Keeping it a Protocol (not a new base class) is what makes the
    later run-path migration a re-type, not a rewrite.

    Every attribute here is one a run path reads today off the identity
    ``Agent``:

    - ``id`` — the project-local agent id (for a config agent, the slug).
    - ``name`` — display name.
    - ``model`` — the **resolved** ``<provider>/<model-id>`` the run uses (for a
      config agent, the model chain has already run; never empty).
    - ``fallback_model`` — secondary model (empty for a config agent in v1).
    - ``workspace`` — identity/memory home; **empty** for a config agent.
    - ``temperature`` / ``thinking_effort`` — run knobs (may be ``None``).
    - ``allowed_tools`` / ``allowed_skills`` — allow-lists (for a config agent the
      project Tool Whitelist minus the agent's denials, and the project-derived
      skills, respectively).
    - ``memory_prompt_mode`` — pinned-memory selection (``"off"`` for config).
    - ``custom_system_prompt_enabled`` — private prompt scope (``False`` for config).
    - ``current_session_id`` — the agent's active session (empty for config; the
      anchor owns project-session selection).
    - ``created_at`` / ``updated_at`` — persisted timestamps (empty for config).
    """

    @property
    def id(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def model(self) -> str: ...
    @property
    def fallback_model(self) -> str: ...
    @property
    def workspace(self) -> str: ...
    @property
    def temperature(self) -> float | None: ...
    @property
    def thinking_effort(self) -> str | None: ...
    @property
    def allowed_tools(self) -> list[str]: ...
    @property
    def allowed_skills(self) -> list[str]: ...
    @property
    def memory_prompt_mode(self) -> MemoryPromptMode: ...
    @property
    def custom_system_prompt_enabled(self) -> bool: ...
    @property
    def current_session_id(self) -> str: ...
    @property
    def created_at(self) -> str: ...
    @property
    def updated_at(self) -> str: ...


@dataclass(frozen=True)
class ConfigAgent:
    """A run-time agent synthesized from a scanned project profile + a model.

    Field set mirrors the store :class:`Agent` so it satisfies
    :class:`RuntimeAgent`; the values come from the :class:`ScannedAgent` profile
    (verbatim ``body`` becomes the system prompt later) plus the model resolved
    through the chain and the project-derived ``allowed_tools``/``allowed_skills``.
    It carries the scanned ``body`` and
    ``source_path`` so the prompt builder (a later task) can insert the body
    verbatim and so callers can point at the source repo file.
    """

    id: str
    name: str
    model: str
    temperature: float | None
    allowed_tools: list[str]
    allowed_skills: list[str]
    body: str
    source_path: Path
    source_format: str
    # Resolved through the chain (agent → project default → global default); both
    # ``temperature`` and ``thinking_effort`` carry the first tier that delivered,
    # or ``None`` when all tiers fell through → the provider default.
    thinking_effort: str | None = None
    fallback_model: str = _CONFIG_AGENT_FALLBACK_MODEL
    workspace: str = _CONFIG_AGENT_WORKSPACE
    memory_prompt_mode: MemoryPromptMode = _CONFIG_AGENT_MEMORY_MODE
    custom_system_prompt_enabled: bool = _CONFIG_AGENT_CUSTOM_PROMPT_ENABLED
    current_session_id: str = ""
    created_at: str = _CONFIG_AGENT_TIMESTAMP
    updated_at: str = _CONFIG_AGENT_TIMESTAMP


class AgentResolutionError(ValueError):
    """An agent could not be resolved into a runnable runtime agent.

    Expected (handled-locally) failure: an unknown project/agent, or a config
    agent whose model chain fell all the way through (no usable model). It is a
    clear "cannot run" signal, never a silent degrade.
    """


# Structural protocols for the runtime dependencies, declared locally so the
# resolver never imports core.runtime (cycle risk). Each mirrors exactly the
# slice of the real service the resolver uses.


class ModelProbe(Protocol):
    """The model-registry slice used to answer "does this model exist?"."""

    def get(self, provider_id: str, model_id: str) -> object: ...


class ProviderProbe(Protocol):
    """The provider-registry slice used to find a provider's connections."""

    def get(self, provider_id: str) -> object: ...


class CredentialProbe(Protocol):
    """The credential slice used to answer "is a connection usable?"."""

    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool: ...


class GlobalAgentDefaultsProvider(Protocol):
    """Returns the instance-wide ``defaults.agent`` map (model, temperature, …).

    One seam for the whole global tier of the resolution chains: the resolver
    reads ``model`` / ``temperature`` / ``thinking_effort`` out of the returned
    mapping. Missing keys mean "no global default" for that field. An empty map is
    a valid answer (nothing configured globally)."""

    def __call__(self) -> Mapping[str, Any]: ...


class ProjectSkillNamesProvider(Protocol):
    """Returns the names of a project's own scanned skills, by project id.

    The skill-side counterpart to the model/credential probes: it lets the resolver
    compute a config agent's effective skills without importing ``core.runtime`` or
    the skills module. The runtime wires it to its cached project-skill scan; an
    unknown project yields an empty set (the agent then has only its opted-in
    bundled skills)."""

    def __call__(self, project_id: str) -> frozenset[str]: ...


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


class ModelConfigurationChecker:
    """Decides whether a ``<provider>/<model-id>`` exists and is configured here.

    "Configured in this instance" = the provider is registered, the model is in
    that provider's catalog, and the provider has at least one connection with
    usable credentials. This is the single rule the model chain and the scan's
    ``BAD_MODEL`` check both consult, so they cannot drift.
    """

    def __init__(
        self,
        models: ModelProbe,
        providers: ProviderProbe,
        provider_credentials: CredentialProbe,
    ) -> None:
        self._models = models
        self._providers = providers
        self._provider_credentials = provider_credentials

    def is_configured(self, model: str) -> bool:
        """Return whether *model* names a model that can actually run here."""
        parsed = _parse_provider_model(model)
        if parsed is None:
            return False
        provider_id, model_id = parsed

        try:
            self._providers.get(provider_id)
        except KeyError:
            return False

        try:
            self._models.get(provider_id, model_id)
        except KeyError:
            return False

        return self._has_usable_connection(provider_id)

    def _has_usable_connection(self, provider_id: str) -> bool:
        provider_config = self._providers.get(provider_id)
        # ProviderConfig.connections is a list of ConnectionConfig with an ``id``
        # local part; the usable check uses the compositional ``provider:conn`` id.
        connections = getattr(provider_config, "connections", [])
        for connection in connections:
            connection_id = f"{provider_id}:{connection.id}"
            if self._provider_credentials.has_credentials(provider_id, connection_id):
                return True
        return False


def _parse_provider_model(model: str) -> tuple[str, str] | None:
    """Split ``<provider>/<model-id>[::suffix]`` into ``(provider, model_id)``.

    Returns ``None`` for an empty or malformed string (no provider/model split),
    which the chain treats as "no model" so it falls through cleanly. The optional
    ``::connection[:account]`` suffix is dropped — existence is per provider+model,
    the connection only affects credential selection downstream.
    """
    if not model:
        return None
    bare, _, _suffix = model.partition("::")
    provider_id, separator, model_id = bare.partition("/")
    if not separator or not provider_id or not model_id:
        return None
    return provider_id, model_id


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
        # Team-scan cache keyed by project id. A run reads from here; an explicit
        # re-scan / project-open repopulates it via ``rescan_project``.
        self._team_cache: dict[str, ScanResult] = {}

    def resolve_agent(self, project_id: str | None, agent_id: str) -> RuntimeAgent:
        """Resolve one agent to a runnable :class:`RuntimeAgent`.

        ``project_id is None`` returns the store identity agent unchanged. A set
        ``project_id`` returns a :class:`ConfigAgent` synthesized from the
        project's Team scan plus the resolved model. Raises
        :class:`AgentResolutionError` for an unknown project/agent or a config
        agent whose model chain fell through.
        """
        if project_id is None:
            return self._resolve_identity_agent(agent_id)
        return self._resolve_config_agent(project_id, agent_id)

    def _resolve_identity_agent(self, agent_id: str) -> Agent:
        """Identity path: the store agent, unchanged (decision: byte-for-byte today)."""
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
        global_defaults = self._global_agent_defaults()
        resolved_model = self._resolve_model_or_raise(scanned, project, global_defaults)
        resolved_temperature = _resolve_temperature(scanned, project, global_defaults)
        resolved_thinking_effort = _resolve_thinking_effort(scanned, project, global_defaults)
        allowed_tools = _effective_allowed_tools(project, scanned)
        allowed_skills = _effective_allowed_skills(project, self._project_skill_names(project_id))
        return _build_config_agent(
            scanned,
            resolved_model,
            resolved_temperature,
            resolved_thinking_effort,
            allowed_tools,
            allowed_skills,
        )

    def scan_project_report(self, project: Project) -> ScanResult:
        """Scan a project into Team + a **complete** report (incl. model findings).

        This is the project-scoped scan the open/re-scan path uses: it runs the
        structural scan, then appends one ``BAD_MODEL`` finding per config agent
        whose declared model is not configured in this instance (via the
        :meth:`ScanReport.with_model_findings` seam). The model check happens
        **here, at scan time** — not lazily at first run.
        """
        result = scan_project(_project_root(project), registry=self._detector_registry)
        model_findings = self._model_findings(result.team)
        report = result.report.with_model_findings(model_findings)
        return ScanResult(team=result.team, report=report)

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
        fresh = scan_project(_project_root(project), registry=self._detector_registry)
        for member in fresh.team:
            if member.agent_id == agent_id:
                return member
        raise AgentResolutionError(
            f"agent '{agent_id}' is no longer present in project '{project.project_id}'"
        )

    def _resolve_model_or_raise(
        self, scanned: ScannedAgent, project: Project, global_defaults: Mapping[str, Any]
    ) -> str:
        """Run the model chain and return the first usable model, or raise.

        Chain: agent model → project default → global default. Each candidate
        counts only when it exists/is configured in this instance; an unconfigured
        candidate is skipped as if absent. Falling all the way through is a clear
        "cannot run" error.
        """
        global_model = global_defaults.get("model", "")
        for candidate in (scanned.model, project.default_model, global_model):
            if candidate and self._model_checker.is_configured(candidate):
                return candidate
        raise AgentResolutionError(
            f"agent '{scanned.agent_id}' has no usable model: declared "
            f"{scanned.model!r}, project default {project.default_model!r}, "
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


def runtime_agent_body(agent: RuntimeAgent) -> str:
    """Return the verbatim prompt body of a runtime agent, or ``""``.

    The single seam that maps the resolver's two agent forms onto the prompt
    builder's ``agent_body`` parameter: a :class:`ConfigAgent` carries an imported
    body, an identity ``Agent`` carries none. Keeping this here (not in the prompt
    domain) lets prompt assembly stay on its Protocols without importing
    ``ConfigAgent`` or probing types — the chat loop calls this on the agent it
    already resolved and hands the result over as an explicit argument.
    """
    return agent.body if isinstance(agent, ConfigAgent) else ""


def _effective_allowed_tools(project: Project, scanned: ScannedAgent) -> list[str]:
    """Return the config agent's tools: the project ceiling minus the agent's denials.

    The Project Tool Whitelist (``project.allowed_tools``) is the hard ceiling; the
    agent's scanned ``denied_tools`` can only remove from it, never add. Order
    follows the project list so the result is deterministic, and an empty ceiling
    yields no tools regardless of what the agent denies.
    """
    return [tool for tool in project.allowed_tools if tool not in scanned.denied_tools]


def _effective_allowed_skills(project: Project, project_skill_names: frozenset[str]) -> list[str]:
    """Return the config agent's skills from the project Skill Whitelist rule.

    ``(project skills − skills_project_disabled) ∪ skills_bundled_enabled`` — the
    project's own scanned skills are active by default, the named ones turned off,
    plus any bundled skills explicitly opted in (decision 3). OpenCode does not
    narrow skills per agent in v1, so this is purely project-derived. The result is
    sorted for determinism; ``filter_allowed`` harmlessly ignores any name that no
    longer resolves to a loadable skill.
    """
    disabled = set(project.skills_project_disabled)
    enabled_bundled = set(project.skills_bundled_enabled)
    return sorted((project_skill_names - disabled) | enabled_bundled)


def _build_config_agent(
    scanned: ScannedAgent,
    resolved_model: str,
    resolved_temperature: float | None,
    resolved_thinking_effort: str | None,
    allowed_tools: list[str],
    allowed_skills: list[str],
) -> ConfigAgent:
    return ConfigAgent(
        id=scanned.agent_id,
        name=scanned.display_name,
        model=resolved_model,
        temperature=resolved_temperature,
        thinking_effort=resolved_thinking_effort,
        body=scanned.body,
        source_path=scanned.source_path,
        source_format=scanned.source_format,
        allowed_tools=allowed_tools,
        allowed_skills=allowed_skills,
    )


def _resolve_temperature(
    scanned: ScannedAgent, project: Project, global_defaults: Mapping[str, Any]
) -> float | None:
    """Resolve temperature: agent value → project default → global default → None.

    The first tier that carries a number wins; ``0.0`` is a real value (the
    sampling floor) and stops the chain. Falling through every tier yields
    ``None`` → the field is dropped at the wire and the provider default applies.
    """
    candidates = (
        scanned.temperature,
        project.default_temperature,
        global_defaults.get("temperature"),
    )
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


def _resolve_thinking_effort(
    scanned: ScannedAgent, project: Project, global_defaults: Mapping[str, Any]
) -> str | None:
    """Resolve thinking effort: agent → project default → global default → None.

    The first tier that is not ``None`` wins. ``""`` is a real value meaning
    "provider default" and stops the chain, so a project ``default_thinking_effort
    = ""`` blocks the global default (forces the provider default) while ``None``
    lets the global default through. Falling through every tier yields ``None``.
    """
    candidates = (
        scanned.thinking_effort,
        project.default_thinking_effort,
        global_defaults.get("thinking_effort"),
    )
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


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
    )
