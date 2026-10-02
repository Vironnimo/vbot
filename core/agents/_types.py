"""Identity Agent records and domain errors."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from core.memory import (
    DEFAULT_MEMORY_PROMPT_MODE,
    MemoryPromptMode,
)
from core.tools.availability import (
    ToolAccess,
)

DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED = False
# Librarian passes curate an Agent's own Skills unless its switch is off.
DEFAULT_LIBRARIAN_ENABLED = True

# The built-in Agents vBot creates itself. ``agent.json`` marks one with
# ``builtin``; the mark is valid only on its reserved id.
BuiltinAgent = Literal["librarian"]
LIBRARIAN_BUILTIN: BuiltinAgent = "librarian"
BUILTIN_AGENTS: frozenset[str] = frozenset({LIBRARIAN_BUILTIN})
# The Librarian curates other Agents' Skills in Sessions of its own.
LIBRARIAN_AGENT_ID = "librarian"
LIBRARIAN_AGENT_NAME = "Librarian"
# The Librarian's whole Tool set; nothing widens it.
LIBRARIAN_TOOLS = ("skill", "skill_manage")
# Why the Librarian is unavailable: no Agent has its id yet, a user's Agent (or
# an unfinished rename) holds the id, or its ``agent.json`` cannot be loaded.
LibrarianProblem = Literal["missing", "agent_id_taken", "invalid_config"]
# Session metadata binding a Session of the Librarian to the Agent whose Skills
# it maintains, its Skill subject. vBot writes it when a pass starts; a Session
# without it works on the Librarian's own Skills.
SKILL_AGENT_ID_KEY = "skill_agent_id"


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


class BuiltinAgentError(AgentError):
    """Raised when an operation would rename, delete or reconfigure a built-in Agent."""


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
    # Whether Librarian passes curate this Agent's own Skills; ``librarian.enabled``
    # still switches scheduled passes off for every Agent.
    librarian_enabled: bool = DEFAULT_LIBRARIAN_ENABLED
    # Which built-in Agent this is, ``None`` for an Agent of the user. A built-in
    # Agent is left out of the roster and keeps fixed capabilities.
    builtin: BuiltinAgent | None = None
    # Never persisted: the Agent whose Skills this Agent works on, set only on the
    # Librarian as one of its bound Sessions runs it (``SKILL_AGENT_ID_KEY``).
    skill_agent_id: str | None = None


def is_librarian(agent: object) -> bool:
    """Whether ``agent`` (any resolved Agent) is the built-in Librarian."""
    return getattr(agent, "builtin", None) == LIBRARIAN_BUILTIN


def librarian_problem_message(problem: LibrarianProblem) -> str:
    """Say why the Librarian is unavailable and what the user can do about it."""
    if problem == "agent_id_taken":
        return (
            f"The Librarian is unavailable: one of your Agents uses its id {LIBRARIAN_AGENT_ID}. "
            f"Rename that Agent (vbot agent rename {LIBRARIAN_AGENT_ID} <new-id>) and restart "
            "vBot to create the Librarian."
        )
    if problem == "invalid_config":
        return (
            f"The Librarian is unavailable: agents/{LIBRARIAN_AGENT_ID}/agent.json cannot be "
            "loaded. Fix that file (vbot doctor names the problem) and restart vBot."
        )
    return "The Librarian is unavailable: it does not exist yet. Restart vBot to create it."


def skill_subject_id(agent: Any) -> str:
    """Return the id of the Agent whose Skills ``agent`` (any resolved Agent) works on.

    That is the subject of a Librarian Session, otherwise the Agent itself.
    """
    subject = getattr(agent, "skill_agent_id", None)
    return subject if isinstance(subject, str) and subject else str(agent.id)


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
