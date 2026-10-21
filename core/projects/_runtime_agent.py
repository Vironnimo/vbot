"""Uniform runtime Agent contracts and Session Agent overrides."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.memory import MemoryPromptMode
from core.permissions import PermissionCeiling
from core.settings import validate_temperature, validate_thinking_effort, validate_top_p

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from core.permissions import PermissionRules
    from core.tools.availability import ToolAccess

_CONFIG_AGENT_WORKSPACE = ""

_CONFIG_AGENT_MEMORY_MODE: MemoryPromptMode = "off"

_CONFIG_AGENT_FALLBACK_MODELS: tuple[str, ...] = ()

_CONFIG_AGENT_CUSTOM_PROMPT_ENABLED = False

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
    - ``temperature`` / ``top_p`` / ``thinking_effort`` — run knobs (``None`` leaves
      them to the Provider).
    - ``tool_access`` — explicit Tool Access Policy; ``allowed_skills`` remains
      an allow-list and ``tools`` carries optional Tool-owned settings (for a
      config agent, Project-derived).
    - ``memory_prompt_mode`` — pinned-memory selection (``"off"`` for config).
    - ``custom_system_prompt_enabled`` — private prompt scope (``False`` for config).
    - ``current_session_id`` — the agent's active session (empty for config; the
      anchor owns project-session selection).
    - ``created_at`` / ``updated_at`` — persisted timestamps (empty for config).
    - ``tool_loading`` — the On-demand Tools setting (``core.tools.on_demand``);
      ``None`` leaves them off (for a config agent, the Project override).
    - ``permission_rules`` — the resolved Permission Rules (``core.permissions``);
      ``None`` is full access, and so is a set that restricts nothing
      (``PermissionRules.restricts``), such as a temporary Agent's own or an
      Identity Agent's explicit full access while no shared default applies.
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
    def top_p(self) -> float | None: ...
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
    @property
    def tool_loading(self) -> dict[str, Any] | None: ...
    @property
    def permission_rules(self) -> PermissionRules | None: ...


AGENT_OVERRIDE_FIELDS = ("model", "thinking_effort", "temperature", "top_p")
# The Session metadata key holding a Session's Agent overrides.
AGENT_OVERRIDES_META_KEY = "agent_overrides"


@dataclass(frozen=True)
class AgentOverrides:
    """The Agent Run settings one Session replaces for every Run it executes.

    ``None`` keeps the Agent's resolved value. A Session stores these values as
    one object in its metadata (``agent_overrides``); :meth:`from_stored` reads
    it, ignoring fields it does not know: those a newer vBot may have added and
    ``model_defaults``, which an earlier vBot set on Librarian pass Sessions.

    ``permission_ceiling`` is no Agent setting: it is the Permission Boundary of
    the Session that started this Sub-Agent Session, in its stored ceiling form
    (``PermissionBoundary.to_ceiling_dict``). Every Run of the Session applies it
    on top of the Agent's own rules; no Session override update changes it.
    """

    model: str | None = None
    thinking_effort: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    # An additive stored flag distinguishes a captured Provider default from
    # an omitted effort, which continues to inherit the Agent's setting.
    thinking_effort_default: bool = False
    permission_ceiling: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.permission_ceiling is not None:
            # Strict: an unreadable stored ceiling stops the Session's Runs.
            PermissionCeiling.from_ceiling_dict(self.permission_ceiling)
        if not isinstance(self.thinking_effort_default, bool):
            raise ValueError("thinking_effort_default must be a boolean")
        if self.thinking_effort_default and self.thinking_effort is not None:
            raise ValueError("thinking_effort_default cannot accompany a thinking_effort")
        if self.model is not None and (not isinstance(self.model, str) or not self.model):
            raise ValueError("model must be a non-empty string")
        if self.thinking_effort is not None:
            validate_thinking_effort(
                self.thinking_effort,
                label="thinking_effort",
                allow_none=False,
            )
            if not self.thinking_effort:
                raise ValueError("thinking_effort must name an effort")
        if self.temperature is not None:
            object.__setattr__(
                self,
                "temperature",
                validate_temperature(self.temperature, label="temperature"),
            )
        if self.top_p is not None:
            object.__setattr__(self, "top_p", validate_top_p(self.top_p, label="top_p"))

    @classmethod
    def from_stored(cls, value: Any) -> AgentOverrides:
        """Read a Session's stored ``agent_overrides`` object (absent: no overrides)."""
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError("Session agent_overrides must be an object")
        return cls(
            **{name: value.get(name) for name in AGENT_OVERRIDE_FIELDS},
            # Older writers preserve the unknown flag when setting an effort.
            # An explicit stored effort takes precedence on a later upgrade.
            thinking_effort_default=(
                value.get("thinking_effort_default", False)
                if value.get("thinking_effort") is None
                else False
            ),
            permission_ceiling=value.get("permission_ceiling"),
        )

    def ceiling(self) -> PermissionCeiling | None:
        """The inherited Permission Boundary every Run of this Session applies, if any."""
        if self.permission_ceiling is None:
            return None
        return PermissionCeiling.from_ceiling_dict(self.permission_ceiling)

    def capture(self, agent: RuntimeAgent) -> AgentOverrides:
        """Keep this Session's Model and effort independently of later Agent edits."""
        from dataclasses import replace

        effort = self.thinking_effort
        if effort is None and not self.thinking_effort_default:
            effort = agent.thinking_effort or None
        return replace(
            self,
            model=self.model or agent.model or None,
            thinking_effort=effort,
            thinking_effort_default=effort is None,
        )

    @property
    def is_empty(self) -> bool:
        """Return whether this value replaces no Agent setting and carries no ceiling."""
        return (
            not self.thinking_effort_default
            and self.permission_ceiling is None
            and all(getattr(self, name) is None for name in AGENT_OVERRIDE_FIELDS)
        )

    def as_dict(self) -> dict[str, Any]:
        """Return only the replaced settings as stored, keyed by field name.

        The ``permission_ceiling`` is no setting and stays out; :meth:`session_metadata`
        stores it.
        """
        values = {
            name: getattr(self, name)
            for name in AGENT_OVERRIDE_FIELDS
            if getattr(self, name) is not None
        }
        if self.thinking_effort_default:
            values["thinking_effort_default"] = True
        return values

    def agent_changes(self) -> dict[str, Any]:
        """Return the runtime Agent fields these overrides replace, keyed by field name."""
        values = self.as_dict()
        if values.pop("thinking_effort_default", False) and self.thinking_effort is None:
            values["thinking_effort"] = ""
        return values

    def session_metadata(self) -> dict[str, Any]:
        """Return the Session metadata that stores these overrides (empty for none)."""
        if self.is_empty:
            return {}
        stored = self.as_dict()
        if self.permission_ceiling is not None:
            stored["permission_ceiling"] = dict(self.permission_ceiling)
        return {AGENT_OVERRIDES_META_KEY: stored}


@dataclass(frozen=True)
class ConfigAgent:
    """A run-time agent synthesized from a scanned project profile + a model.

    Field set mirrors the store :class:`Agent` so it satisfies
    :class:`RuntimeAgent`; the values come from the :class:`AgentProfile` profile
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
    source: str
    # Resolved through the chain (agent → project default → global default);
    # ``temperature``, ``top_p`` and ``thinking_effort`` carry the first tier that
    # delivered, or ``None`` when all tiers fell through → the provider default.
    project_id: str | None = None
    top_p: float | None = None
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
    # On-demand Tools from the Project's override; ``None`` leaves them off.
    tool_loading: dict[str, Any] | None = None
    # Permission Rules resolved per part: the vBot override, then the repository
    # translation, then ``defaults.agent.permission_rules``; ``None`` is full access.
    permission_rules: PermissionRules | None = None
    preload_skills: tuple[str, ...] = ()
    model_inherit: bool = False


class AgentResolutionError(ValueError):
    """An agent could not be resolved into a runnable runtime agent.

    Expected (handled-locally) failure: an unknown project/agent, or a config
    agent whose model chain fell all the way through (no usable model). It is a
    clear "cannot run" signal, never a silent degrade. A missing Agent or
    Project raises one of the precise subclasses below, so accessors can report
    the missing resource without parsing the message.
    """


class ResolutionAgentNotFoundError(AgentResolutionError):
    """The addressed Agent does not exist.

    No stored Identity Agent has exactly this id, or the Project Team (and its
    freshly read repository source) has no member with exactly this id.
    """


class ResolutionProjectNotFoundError(AgentResolutionError):
    """The addressed Project has no Anchor with exactly this id."""


# A Session keeps the Project it was created with; it never moves to another one.
WORKING_PROJECT_MISSING_MESSAGE = (
    "This Session works in Project {project_id}, which no longer exists. "
    "Restore the Project to continue this Session, or start a new Session."
)


class WorkingProjectMissingError(AgentResolutionError):
    """The Project a Session works in no longer exists, so the Session cannot run.

    The Session stays readable; it runs again once the Project is restored.
    """

    def __init__(self, project_id: str) -> None:
        super().__init__(WORKING_PROJECT_MISSING_MESSAGE.format(project_id=project_id))
        self.project_id = project_id


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
