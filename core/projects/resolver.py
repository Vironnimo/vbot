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
same attributes (model, tool_access, temperature, thinking_effort,
allowed_skills, fallback_models, memory_prompt_mode, workspace, id, …).

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
is in the catalog, and a connection the model's per-model allowlist permits has
usable credentials (a pinned ``::connection[:account]`` suffix is checked
verbatim). An unconfigured model is treated as **no model** and the chain falls
through; if it falls all the way through, resolution raises (the agent cannot
run). The same "exists/configured?" check produces the scan's ``BAD_MODEL``
findings, hung onto the report through :meth:`ScanReport.with_model_findings`
(the B3.1 seam).

Constructor injection only; the runtime dependencies are declared as local
structural Protocols so this module never imports ``core.runtime`` (import-cycle
risk, mirroring ``core/providers/task_client.py`` / ``usage.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.memory import MemoryPromptMode
from core.projects.projects import ProjectError
from core.projects.scan_report import FindingType, ScanFinding
from core.projects.scanners.base import (
    DetectorRegistration,
    ScannedAgent,
    ScanResult,
    scan_project,
)
from core.settings import AgentDefaults, validate_thinking_effort
from core.skills import WILDCARD_ALLOWLIST

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Any

    from core.agents.agents import Agent, AgentStore
    from core.models.models import ModelRegistry
    from core.projects.projects import Project
    from core.projects.store import ProjectStore
    from core.providers.providers import ProviderRegistry
    from core.runtime.interfaces import ProviderCredentialResolverProtocol
    from core.tools.availability import ToolAccess

# Config agents are workspace-less and memory-tool-less in v1 (plan: "Config-Agent
# = kein Workspace, kein Memory-Tool"). The empty workspace path makes that
# explicit on the runtime-agent surface; the memory mode is forced off so no
# pinned-memory block is ever assembled for a config agent.
_CONFIG_AGENT_WORKSPACE = ""
_CONFIG_AGENT_MEMORY_MODE: MemoryPromptMode = "off"
# Config agents have no fallback chain and no custom-prompt scope in v1; their
# prompt body comes verbatim from the scanned source instead.
_CONFIG_AGENT_FALLBACK_MODELS: tuple[str, ...] = ()
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
    - ``fallback_models`` — ordered fallback chain (empty for a config agent in v1).
    - ``workspace`` — identity/memory home; **empty** for a config agent.
    - ``temperature`` / ``thinking_effort`` — run knobs (may be ``None``).
    - ``tool_access`` — explicit Tool Access Policy; ``allowed_skills`` remains
      an allow-list and ``tools`` carries optional Tool-owned settings (for a
      config agent, Project-derived).
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
    def fallback_models(self) -> list[str]: ...
    @property
    def workspace(self) -> str: ...

    @property
    def root_project_id(self) -> str | None: ...
    @property
    def temperature(self) -> float | None: ...
    @property
    def thinking_effort(self) -> str | None: ...
    @property
    def tool_access(self) -> ToolAccess: ...
    @property
    def allowed_skills(self) -> list[str]: ...
    @property
    def tools(self) -> dict[str, Any]: ...
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
    @property
    def compaction_policy(self) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class AgentRunOverrides:
    """The only Agent fields one admitted Run may replace ephemerally."""

    model: str | None = None
    thinking_effort: str | None = None

    def __post_init__(self) -> None:
        if self.model is not None and (not isinstance(self.model, str) or not self.model):
            raise ValueError("model must be a non-empty string")
        if self.thinking_effort is not None:
            validate_thinking_effort(
                self.thinking_effort,
                label="thinking_effort",
                allow_none=False,
            )

    @property
    def is_empty(self) -> bool:
        """Return whether this value changes neither permitted field."""
        return self.model is None and self.thinking_effort is None


@dataclass(frozen=True)
class ConfigAgent:
    """A run-time agent synthesized from a scanned project profile + a model.

    Field set mirrors the store :class:`Agent` so it satisfies
    :class:`RuntimeAgent`; the values come from the :class:`ScannedAgent` profile
    (verbatim ``body`` becomes the system prompt later) plus the model resolved
    through the chain and the project-derived ``tool_access``/``allowed_skills``.
    It carries the scanned ``body`` and
    ``source_path`` so the prompt builder (a later task) can insert the body
    verbatim and so callers can point at the source repo file.
    """

    id: str
    name: str
    model: str
    temperature: float | None
    tool_access: ToolAccess
    allowed_skills: list[str]
    tools: dict[str, Any]
    body: str
    source_path: Path
    source_format: str
    # Resolved through the chain (agent → project default → global default); both
    # ``temperature`` and ``thinking_effort`` carry the first tier that delivered,
    # or ``None`` when all tiers fell through → the provider default.
    thinking_effort: str | None = None
    fallback_models: list[str] = field(default_factory=lambda: list(_CONFIG_AGENT_FALLBACK_MODELS))
    workspace: str = _CONFIG_AGENT_WORKSPACE
    root_project_id: str | None = None
    memory_prompt_mode: MemoryPromptMode = _CONFIG_AGENT_MEMORY_MODE
    custom_system_prompt_enabled: bool = _CONFIG_AGENT_CUSTOM_PROMPT_ENABLED
    current_session_id: str = ""
    created_at: str = _CONFIG_AGENT_TIMESTAMP
    updated_at: str = _CONFIG_AGENT_TIMESTAMP
    compaction_policy: dict[str, Any] | None = None


class AgentResolutionError(ValueError):
    """An agent could not be resolved into a runnable runtime agent.

    Expected (handled-locally) failure: an unknown project/agent, or a config
    agent whose model chain fell all the way through (no usable model). It is a
    clear "cannot run" signal, never a silent degrade.
    """


class ModelConfigurationError(ValueError):
    """A Model reference cannot run with this instance's configured routes."""


# Structural protocols for the runtime dependencies, declared locally so the
# resolver never imports core.runtime (cycle risk). Each mirrors exactly the
# slice of the real service the resolver uses.


class ConnectionRestrictedModel(Protocol):
    """The catalog-model slice the checker reads: the per-model connection rule.

    ``allows_connection`` is the single source of the connection allowlist
    (``core.models.Model.allows_connection``): an empty allowlist permits every
    connection, a non-empty one restricts the model to the listed connection ids.
    """

    @property
    def connections(self) -> tuple[str, ...]: ...

    def allows_connection(self, connection_id: str) -> bool: ...


class ModelProbe(Protocol):
    """The model-registry slice used to answer "does this model exist?"."""

    def get(self, provider_id: str, model_id: str) -> ConnectionRestrictedModel: ...


class ProviderProbe(Protocol):
    """The provider-registry slice used to find a provider's connections."""

    def get(self, provider_id: str) -> object: ...


class CredentialProbe(Protocol):
    """The credential slice used to answer "is a connection usable?".

    Usability = enabled (settings override or type default) AND credentialed —
    owned by ``ProviderCredentialResolver.is_usable``.
    """

    def is_usable(self, provider_id: str, connection_id: str | None = None) -> bool: ...


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


def _orphan_finding(agent_id: str, detail: str) -> ScanFinding:
    """Build an ``ORPHAN`` finding for an anchor pointer at a non-team agent id.

    Pointer-origin findings carry no ``source_path`` — the pointer lives in the
    anchor (``project.json`` / the sessions subtree), not in a repo source file.
    """
    return ScanFinding(type=FindingType.ORPHAN, detail=detail, agent_id=agent_id)


class ModelConfigurationChecker:
    """Decides whether a ``<provider>/<model-id>[::connection[:account]]`` can run here.

    "Configured in this instance" = the provider is registered, the model is in
    that provider's catalog, and a connection is usable **for this model**: it
    has usable credentials and the model's per-model connection allowlist
    (``Model.allows_connection`` — empty means unrestricted) permits it. A pinned
    ``::connection[:account]`` suffix narrows the question to exactly that
    connection (and account). This mirrors what the chat runtime enforces at
    request time (``core/chat/model_resolution.py`` — allowlist-filtered
    connection pick, verbatim pinned suffix), so a model this gate accepts never
    fails connection resolution at run time. It is the single rule the model
    chain, the scan's ``BAD_MODEL`` check, and the ``/model`` set-time gate all
    consult, so they cannot drift.
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
        provider_id, model_id, connection_suffix = parsed

        try:
            self._providers.get(provider_id)
        except KeyError:
            return False

        try:
            catalog_model = self._models.get(provider_id, model_id)
        except KeyError:
            return False

        if connection_suffix:
            return self._pinned_connection_usable(provider_id, catalog_model, connection_suffix)
        return self._has_usable_allowed_connection(provider_id, catalog_model)

    def require_configured(self, model: str) -> None:
        """Require *model* to be runnable and retain precise Connection failures."""
        parsed = _parse_provider_model(model)
        if parsed is None:
            raise self._unusable_error(model)
        provider_id, model_id, connection_suffix = parsed

        try:
            provider_config = self._providers.get(provider_id)
            catalog_model = self._models.get(provider_id, model_id)
        except KeyError as error:
            raise self._unusable_error(model) from error

        if connection_suffix:
            connection_local_id = connection_suffix.partition(":")[0]
            if not catalog_model.allows_connection(connection_local_id):
                allowed = ", ".join(catalog_model.connections)
                raise ModelConfigurationError(
                    f"model {provider_id}/{model_id} is not available on connection "
                    f"'{connection_local_id}' (allowed connections: {allowed})"
                )
            connections = getattr(provider_config, "connections", [])
            if all(connection.id != connection_local_id for connection in connections):
                raise self._unusable_error(model)
            if not self._provider_credentials.is_usable(
                provider_id, f"{provider_id}:{connection_suffix}"
            ):
                raise self._unusable_error(model)
            return

        if not self._has_usable_allowed_connection(provider_id, catalog_model):
            raise self._unusable_error(model)

    @staticmethod
    def _unusable_error(model: str) -> ModelConfigurationError:
        return ModelConfigurationError(
            f"model {model!r} is not usable in this instance "
            "(unknown provider/model or no usable credential on an allowed connection)"
        )

    def _has_usable_allowed_connection(
        self, provider_id: str, catalog_model: ConnectionRestrictedModel
    ) -> bool:
        """Whether any connection is both allowed by the model and credentialed.

        Mirrors the runtime's unpinned pick (``_first_usable_connection_id``): a
        connection outside the model's allowlist never counts, so a
        connection-bound model (e.g. subscription-only) with credentials only on
        a forbidden connection is *not* configured — the chain falls through
        instead of the run failing later.
        """
        provider_config = self._providers.get(provider_id)
        # ProviderConfig.connections is a list of ConnectionConfig with an ``id``
        # local part; the usable check uses the compositional ``provider:conn`` id.
        connections = getattr(provider_config, "connections", [])
        for connection in connections:
            if not catalog_model.allows_connection(connection.id):
                continue
            connection_id = f"{provider_id}:{connection.id}"
            if self._provider_credentials.is_usable(provider_id, connection_id):
                return True
        return False

    def _pinned_connection_usable(
        self, provider_id: str, catalog_model: ConnectionRestrictedModel, connection_suffix: str
    ) -> bool:
        """Whether the pinned ``connection[:account]`` exists, is allowed, and is usable.

        The runtime reconstructs the pinned connection verbatim and resolves its
        credential downstream, so the gate checks exactly that path: the local
        connection id must exist on the provider, pass the model's allowlist, and
        ``is_usable`` (enabled + credentialed) must hold for the full (possibly
        account-pinned) id.
        """
        connection_local_id = connection_suffix.partition(":")[0]
        if not catalog_model.allows_connection(connection_local_id):
            return False
        provider_config = self._providers.get(provider_id)
        connections = getattr(provider_config, "connections", [])
        if all(connection.id != connection_local_id for connection in connections):
            return False
        return self._provider_credentials.is_usable(
            provider_id, f"{provider_id}:{connection_suffix}"
        )


def _parse_provider_model(model: str) -> tuple[str, str, str] | None:
    """Split ``<provider>/<model-id>[::connection[:account]]`` into its parts.

    Returns ``(provider, model_id, suffix)`` with ``suffix == ""`` when unpinned,
    or ``None`` for an empty or malformed string (no provider/model split, or an
    empty suffix after ``::``), which the chain treats as "no model" so it falls
    through cleanly. Uses ``rpartition`` like the canonical chat-side parse
    (``parse_model_with_connection``) so the two can never split differently.
    """
    if not model:
        return None
    before, suffix_separator, connection_suffix = model.rpartition("::")
    if suffix_separator and not connection_suffix:
        return None
    bare = before if suffix_separator else model
    provider_id, separator, model_id = bare.partition("/")
    if not separator or not provider_id or not model_id:
        return None
    return provider_id, model_id, connection_suffix if suffix_separator else ""


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

        if isinstance(agent, Agent):
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
          ``AgentStore._apply_defaults`` exactly: a default applies when the persisted
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
    return agent.body if isinstance(agent, ConfigAgent) else ""


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


def _project_agent_tool_access(project: Project, scanned: ScannedAgent) -> ToolAccess:
    """Return the Project-scoped Tool policy for one Project Agent.

    A vBot override replaces the repository-scanned Tool policy. ``all`` is
    materialized against the Project Tool Whitelist so the shared runtime resolver
    never interprets it as every Tool registered in the whole vBot instance.
    """

    from core.tools.availability import ToolAccess, normalize_tool_access

    raw_override = project.overrides.get(scanned.agent_id, {}).get("tool_access")
    if raw_override is not None:
        override = normalize_tool_access(raw_override)
        if override.mode == "none":
            return override
        if override.mode == "all":
            return ToolAccess(
                mode="selected",
                allowed=tuple(project.allowed_tools),
                denied=override.denied,
            )
        return override

    denied = tuple(sorted(scanned.denied_tools))
    allowed = tuple(tool for tool in project.allowed_tools if tool not in scanned.denied_tools)
    return ToolAccess(
        mode="selected",
        allowed=allowed,
        denied=denied,
    )


def effective_project_allowed_skills(
    project: Project, project_skill_names: frozenset[str]
) -> list[str]:
    """Return the effective names from the Project Skill Whitelist rule.

    ``(project skills ∪ skills_bundled_enabled ∪ skills_global_enabled) −
    (skills_project_disabled ∩ project skills)`` — the project's own scanned skills
    are active by default, plus any bundled or global skills explicitly opted in
    (decision 3). Two hardenings on top of the plain union:

    - **A disabled project skill name is off entirely.** The merged registry
      resolves a name collision to the project's own copy, so leaving the name
      allowed through a same-named bundled/global opt-in would silently serve the
      disabled project skill. A disabled name that is *not* a project skill stays
      inert (the opt-ins keep working).
    - **The literal wildcard is dropped.** This list is a resolved set of exact
      names; a repo-scanned skill *named* ``*`` (the lenient loader accepts that
      with a warning) must not smuggle the ``allowed_skills`` wildcard past the
      whitelist and expose the whole global pool to a project agent.

    OpenCode does not narrow skills per agent in v1, so this is purely
    project-derived. Config-Agent resolution and Identity Project Context both use
    this function so their interpretation cannot drift. The result is sorted for
    determinism; ``filter_allowed`` harmlessly ignores any name that no longer
    resolves to a loadable skill.
    """
    disabled = set(project.skills_project_disabled)
    enabled_bundled = set(project.skills_bundled_enabled)
    enabled_global = set(project.skills_global_enabled)
    allowed = set(project_skill_names) | enabled_bundled | enabled_global
    allowed -= disabled & project_skill_names
    allowed.discard(WILDCARD_ALLOWLIST)
    return sorted(allowed)


def _effective_allowed_agents(scanned: ScannedAgent, team: list[ScannedAgent]) -> list[str]:
    """Materialize additional targets against the Team; self is always implicit."""
    allowed: list[str] = []
    for member in team:
        if member.agent_id == scanned.agent_id:
            continue
        member_allowed = True
        for rule in scanned.agent_target_rules:
            if fnmatchcase(member.agent_id, rule.pattern):
                member_allowed = rule.allowed
        if member_allowed:
            allowed.append(member.agent_id)
    return sorted(allowed)


def _project_agent_tools(tool_access: ToolAccess, allowed_agents: list[str]) -> dict[str, Any]:
    """Project effective targets into the optional root Tool-settings block."""
    if (
        tool_access.mode == "none"
        or "subagent" not in tool_access.allowed
        or "subagent" in tool_access.denied
    ):
        return {}
    return {"subagent": {"allowed_agents": allowed_agents}}


def _build_config_agent(
    scanned: ScannedAgent,
    resolved_model: str,
    resolved_temperature: float | None,
    resolved_thinking_effort: str | None,
    tool_access: ToolAccess,
    allowed_skills: list[str],
    tools: dict[str, Any],
    compaction_policy: Any,
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
        tool_access=tool_access,
        allowed_skills=allowed_skills,
        tools=tools,
        compaction_policy=(
            dict(compaction_policy) if isinstance(compaction_policy, dict) else None
        ),
    )


def _resolve_temperature(
    scanned: ScannedAgent, project: Project, global_defaults: AgentDefaults
) -> float | None:
    """Resolve temperature: override → agent value → project default → global default → None.

    The first tier that carries a number wins; ``0.0`` is a real value (the
    sampling floor) and stops the chain. An override present (not ``None``, including
    ``0.0``) is the top tier and wins. Falling through every tier yields ``None`` →
    the field is dropped at the wire and the provider default applies.
    """
    candidates = (
        _overridden_temperature(project, scanned.agent_id),
        scanned.temperature,
        project.default_temperature,
        global_defaults.temperature,
    )
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


def _resolve_thinking_effort(
    scanned: ScannedAgent, project: Project, global_defaults: AgentDefaults
) -> str | None:
    """Resolve thinking effort: override → agent → project default → global default → None.

    The first tier that is not ``None`` wins. ``""`` is a real value meaning
    "provider default" and stops the chain, so an override (or project
    ``default_thinking_effort``) of ``""`` blocks the lower tiers (forces the
    provider default) while ``None`` lets them through. An override present (not
    ``None``, including ``""``) is the top tier. Falling through every tier yields ``None``.
    """
    candidates = (
        _overridden_thinking_effort(project, scanned.agent_id),
        scanned.thinking_effort,
        project.default_thinking_effort,
        global_defaults.thinking_effort,
    )
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


def _config_temperature_source(
    project: Project, scanned: ScannedAgent, global_defaults: AgentDefaults
) -> dict[str, Any]:
    """Return the effective temperature + source for a config agent.

    Same chain as :func:`_resolve_temperature` (override → agent → project default →
    global default) but reporting which tier won; ``0.0`` is a real stopping value.
    """
    tiers = (
        ("override", _overridden_temperature(project, scanned.agent_id)),
        ("agent", scanned.temperature),
        ("project_default", project.default_temperature),
        ("global_default", _global_default_temperature(global_defaults)),
    )
    for source, candidate in tiers:
        if candidate is not None:
            return {"value": candidate, "source": source}
    return {"value": None, "source": None}


def _config_thinking_effort_source(
    project: Project, scanned: ScannedAgent, global_defaults: AgentDefaults
) -> dict[str, Any]:
    """Return the effective thinking effort + source for a config agent.

    Same chain as :func:`_resolve_thinking_effort` (override → agent → project default →
    global default) but reporting which tier won; ``""`` is a real stopping value.
    """
    tiers = (
        ("override", _overridden_thinking_effort(project, scanned.agent_id)),
        ("agent", scanned.thinking_effort),
        ("project_default", project.default_thinking_effort),
        ("global_default", _global_default_thinking_effort(global_defaults)),
    )
    for source, candidate in tiers:
        if candidate is not None:
            return {"value": candidate, "source": source}
    return {"value": None, "source": None}


def _identity_string_source(own_value: str, default_value: Any) -> dict[str, Any]:
    """Return the identity effective value + source for a string field.

    Mirrors ``AgentStore._apply_defaults``: the persisted own value wins unless it is
    ``""``, in which case the global default applies when present. Source is
    ``"agent"`` / ``"global_default"`` / ``None``.
    """
    if own_value != "":
        return {"value": own_value, "source": "agent"}
    if default_value is not None and isinstance(default_value, str):
        return {"value": default_value, "source": "global_default"}
    return {"value": None, "source": None}


def _identity_string_list_source(own_value: list[str], default_value: Any) -> dict[str, Any]:
    """Return the identity effective value + source for a string-list field.

    Mirrors ``AgentStore._apply_defaults`` for ``fallback_models``: the persisted
    own value wins unless it is empty, in which case the global default applies
    when present. Source is ``"agent"`` / ``"global_default"`` / ``None``.
    """
    if own_value:
        return {"value": list(own_value), "source": "agent"}
    if (
        default_value is not None
        and isinstance(default_value, list)
        and all(isinstance(item, str) for item in default_value)
    ):
        return {"value": list(default_value), "source": "global_default"}
    return {"value": None, "source": None}


def _identity_optional_source(own_value: Any, default_value: Any) -> dict[str, Any]:
    """Return the identity effective value + source for a nullable field.

    Mirrors ``AgentStore._apply_defaults``: the persisted own value wins unless it is
    ``None``, in which case the global default applies when present. Source is
    ``"agent"`` / ``"global_default"`` / ``None``. A present own value (including
    ``0.0`` for temperature or ``""`` for thinking effort) stops the chain.
    """
    if own_value is not None:
        return {"value": own_value, "source": "agent"}
    if default_value is not None:
        return {"value": default_value, "source": "global_default"}
    return {"value": None, "source": None}


def _global_default_temperature(global_defaults: AgentDefaults) -> float | None:
    value = global_defaults.temperature
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _global_default_thinking_effort(global_defaults: AgentDefaults) -> str | None:
    value = global_defaults.thinking_effort
    return value if isinstance(value, str) else None


def _config_tool_access_source(project: Project, scanned: ScannedAgent) -> dict[str, Any]:
    """Return the editable Project Agent Tool policy and its winning source."""

    from core.tools.availability import normalize_tool_access

    raw_override = project.overrides.get(scanned.agent_id, {}).get("tool_access")
    if raw_override is not None:
        return {
            "value": normalize_tool_access(raw_override).to_dict(),
            "source": "override",
        }
    policy = _project_agent_tool_access(project, scanned)
    return {"value": policy.to_dict(), "source": "agent"}


def _overridden_model(project: Project, agent_id: str) -> str:
    """Return the agent's overridden model, or ``""`` when not overridden."""
    return str(project.overrides.get(agent_id, {}).get("model", "") or "")


def _overridden_temperature(project: Project, agent_id: str) -> float | None:
    """Return the agent's overridden temperature, or ``None`` when not overridden."""
    value = project.overrides.get(agent_id, {}).get("temperature")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _overridden_thinking_effort(project: Project, agent_id: str) -> str | None:
    """Return the agent's overridden thinking effort (``""`` allowed), or ``None`` when not set."""
    value = project.overrides.get(agent_id, {}).get("thinking_effort")
    return value if isinstance(value, str) else None


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
