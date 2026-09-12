"""Identity Agent records and domain errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.memory import (
    DEFAULT_MEMORY_PROMPT_MODE,
    MemoryPromptMode,
)
from core.tools.availability import (
    ToolAccess,
)

DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED = False


class AgentError(ValueError):
    """Base error for expected agent lifecycle failures."""


class AgentAlreadyExistsError(AgentError):
    """Raised when creating an agent whose ID already exists."""


class AgentNotFoundError(AgentError):
    """Raised when an agent cannot be found."""


class InvalidAgentIdError(AgentError):
    """Raised when an agent ID is unsafe for filesystem use."""


class InvalidAgentOrderError(AgentError):
    """Raised when a requested Identity Agent order is malformed."""


class AgentOrderConflictError(AgentError):
    """Raised when a reorder was based on stale roster or revision state."""

    def __init__(self, message: str, *, current_revision: int) -> None:
        super().__init__(message)
        self.current_revision = current_revision


@dataclass(frozen=True)
class Agent:
    """Persisted agent configuration stored in ``agent.json``."""

    id: str
    name: str
    model: str
    fallback_models: list[str]
    workspace: str
    temperature: float | None
    thinking_effort: str | None
    tool_access: ToolAccess
    allowed_skills: list[str]
    created_at: str
    updated_at: str
    tools: dict[str, Any] = field(default_factory=dict)
    root_project_id: str | None = None
    current_session_id: str = ""
    custom_system_prompt_enabled: bool = DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED
    memory_prompt_mode: MemoryPromptMode = DEFAULT_MEMORY_PROMPT_MODE
    compaction_policy: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentListResult:
    """The canonical Identity Agent roster and its persisted order revision."""

    agents: tuple[Agent, ...]
    order_revision: int
    order_changed: bool = False


@dataclass(frozen=True)
class _AgentOrderDocument:
    """Validated collection metadata stored once for the whole Agent roster."""

    agent_ids: tuple[str, ...]
    revision: int


@dataclass(frozen=True)
class AgentUpdateResult:
    """An Agent update plus non-persisted Workspace relocation metadata."""

    agent: Agent
    copied_files: tuple[str, ...] = ()
    backed_up_files: tuple[str, ...] = ()
    backup_dir: str | None = None
    created_files: tuple[str, ...] = field(default=(), repr=False)
    destination: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class AgentRenameResult:
    """A completed Identity Agent tree rename and its rollback snapshot."""

    agent: Agent
    previous_agent: Agent = field(repr=False)
    previous_order: _AgentOrderDocument | None = field(default=None, repr=False)
    order_updated: bool = field(default=False, repr=False)


@dataclass(frozen=True)
class AgentReferenceUpdateResult:
    """Exact Agent-config snapshots changed by an Identity Agent rename."""

    previous_agents: tuple[Agent, ...] = field(repr=False)

    @property
    def agent_ids(self) -> tuple[str, ...]:
        """Return the Identity Agent configs whose policies changed."""
        return tuple(agent.id for agent in self.previous_agents)
