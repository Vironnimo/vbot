"""Identity Agent records and domain errors."""

from __future__ import annotations

from collections.abc import Iterable
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


class AgentReferencedError(AgentError):
    """Raised when a rename targets an Agent id that references still name.

    ``references`` names each one as ``<kind>:<id>``, such as ``channel:tg-main``
    or ``cron:<job id>``.
    """

    def __init__(self, agent_id: str, references: Iterable[str]) -> None:
        self.agent_id = agent_id
        self.references = tuple(sorted(references))
        super().__init__(
            f"Cannot rename an Agent to {agent_id} while {', '.join(self.references)} "
            "still reference it; change or remove these references first"
        )


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
    # Skill names removed from what ``allowed_skills`` grants. Never hides the Agent's
    # own private Skills or the active Project's Skills.
    excluded_skills: list[str] = field(default_factory=list)
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
class AgentRename:
    """One pending Identity Agent rename, recorded in ``agents/rename-pending.json``.

    The record names the direction still to reach: ``source_id`` is replaced by
    ``target_id`` everywhere. A failed rename reverses the record in place, so
    ``rollback`` marks a record that now leads back to the original id.
    ``staging_name`` is the sibling directory a case-only rename passes through.
    """

    source_id: str
    target_id: str
    staging_name: str | None = None
    rollback: bool = False

    @property
    def old_id(self) -> str:
        """The id the Agent had before the rename started."""
        return self.target_id if self.rollback else self.source_id

    @property
    def new_id(self) -> str:
        """The id the rename set out to give the Agent."""
        return self.source_id if self.rollback else self.target_id

    def reversed(self) -> AgentRename:
        """Return the record that leads back to ``source_id``."""
        return AgentRename(
            source_id=self.target_id,
            target_id=self.source_id,
            staging_name=self.staging_name,
            rollback=not self.rollback,
        )


@dataclass(frozen=True)
class AgentRenameResult:
    """The Agent-owned half of a pending rename, applied and still recorded.

    ``session_ids`` are the live Sessions that moved to the new id,
    ``policy_agent_ids`` the Agents whose delegation allow-list changed and
    ``session_link_count`` the Sub-Agent parent links that now name it.
    """

    rename: AgentRename
    agent: Agent
    session_ids: tuple[str, ...] = ()
    policy_agent_ids: tuple[str, ...] = ()
    session_link_count: int = 0


@dataclass(frozen=True)
class ArchivedAgent:
    """An Identity Agent whose files just moved into an archive payload.

    ``roster_index`` is its position in the roster, ``workspace`` its stored
    Workspace path (data-dir relative when inside the data directory) and
    ``workspace_external`` whether that Workspace lies outside the Agent's own
    directory, where an archive leaves it in place.
    """

    agent: Agent
    roster_index: int | None
    workspace: str
    workspace_external: bool


@dataclass(frozen=True)
class ArchivedAgentPayload:
    """What an archived Agent payload holds, read without side effects.

    ``problem`` names why it cannot be restored: ``payload_missing``,
    ``payload_invalid``, ``older_format`` or ``newer_format``; ``agent`` is then
    ``None``.
    """

    agent: Agent | None
    problem: str | None = None
