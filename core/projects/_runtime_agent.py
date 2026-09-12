"""Uniform runtime Agent contracts and immutable Run overrides."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.memory import MemoryPromptMode
from core.settings import validate_thinking_effort

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

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
    project_id: str | None = None
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
