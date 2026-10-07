"""Prompt block identifiers and typed contributor contracts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from core.memory import (
    MemoryPromptMode,
)
from core.tools.availability import (
    SKILL_MANAGE_TOOL_NAME,
    ToolAccess,
)
from core.tools.tools import ToolDefinitionProfileContext

JsonObject = dict[str, Any]

CORE_RUNTIME_BLOCK_ID = "core:runtime"

CORE_IDENTITY_RUNTIME_BLOCK_ID = "core:identity_runtime"

CORE_TOOLS_BLOCK_ID = "core:tools"

# Tells the Model where <system-reminder> messages come from. Static text, so it
# stays byte-identical for the whole Session.
CORE_SYSTEM_REMINDERS_BLOCK_ID = "core:system_reminders"

# Ships disabled: native Provider definitions already carry Tool descriptions.
CORE_TOOLS_LIST_BLOCK_ID = "core:tools_list"

# Tells a Sub-Agent who delegated its work and where its answers go. Shown only
# in Sessions linked to a Parent Agent.
CORE_SUBAGENT_ROLE_BLOCK_ID = "core:subagent_role"

CORE_CHANNELS_BLOCK_ID = "core:channels"

CORE_SKILLS_BLOCK_ID = "core:skills"

CORE_SKILL_MAINTENANCE_BLOCK_ID = "core:skill_maintenance"

CORE_SOUL_BLOCK_ID = "core:soul"

CORE_WORKING_PROJECT_BLOCK_ID = "core:working_project"

CORE_AGENT_BODY_BLOCK_ID = "core:agent_body"

BLOCK_OWNER_ALWAYS = "always"

BLOCK_OWNER_CHANNEL = "channel"

BLOCK_OWNER_IDENTITY = "identity"

BLOCK_OWNER_SUBAGENT_SESSION = "subagent_session"

BLOCK_OWNER_SKILL_MANAGE = f"tool:{SKILL_MANAGE_TOOL_NAME}"

# Custom user blocks exist through their layout entry and override file.
USER_BLOCK_SOURCE = "user"

USER_BLOCK_ID_PREFIX = "user:"

INHERITANCE_AGENT_OVERRIDE = "agent_override"

INHERITANCE_DEFAULT_OVERRIDE = "default_override"

INHERITANCE_OWNER_DEFAULT = "owner_default"

DEFAULT_SCOPE_KEY = "default"

AGENT_SCOPE_KEY_PREFIX = "agent:"

SOUL_INCLUDE_MARKER = "{include:SOUL.md}"

# SOUL-only framing; never apply it to ordinary workspace includes or empty SOUL.
SOUL_FRAMING = (
    "The following is `SOUL.md` from your Workspace: your role, how you work, your voice "
    "and your boundaries. Follow it; when the user's current request conflicts with it, "
    "follow the request. Change `SOUL.md` only when the user asks to change one of these "
    "or agrees to a change you proposed. Propose a change only when the user's feedback "
    "shows that a lasting part of it is wrong; never ask after a task whether to change it."
)


@dataclass(frozen=True)
class PromptScope:
    """Resolved prompt-fragment scope."""

    type: str
    agent_id: str | None = None


@dataclass(frozen=True)
class ProjectPromptContext:
    """The Project inputs the prompt builder needs for the Working Project block.

    Passed explicitly into ``build_system_prompt`` (and ``render_project_files``)
    so the prompt domain never imports Project or Agent classes. The caller owns
    where the stable Project id, display name, ``cwd``, and ``auto_load`` come from.
    ``cwd`` is the Project Workspace; ``auto_load`` is its ordered file list.
    """

    project_id: str
    project_name: str
    cwd: Path
    auto_load: tuple[str, ...] = ()

    @classmethod
    def from_project(
        cls,
        project_id: str,
        project_name: str,
        cwd: str | Path,
        auto_load: Sequence[str],
    ) -> ProjectPromptContext:
        """Build a prompt context from one Project's presentation fields."""
        return cls(
            project_id=project_id,
            project_name=project_name,
            cwd=Path(cwd),
            auto_load=tuple(auto_load),
        )


class PromptAgent(Protocol):
    """Agent fields needed for prompt assembly."""

    @property
    def id(self) -> str:
        """Stable Agent identifier."""
        ...

    @property
    def name(self) -> str:
        """Display name for the Agent."""
        ...

    @property
    def model(self) -> str:
        """Resolved Agent model identifier."""
        ...

    @property
    def workspace(self) -> str:
        """Agent workspace path."""
        ...

    @property
    def thinking_effort(self) -> str | None:
        """Agent thinking effort setting."""
        ...

    @property
    def memory_prompt_mode(self) -> MemoryPromptMode:
        """Which pinned memory files are included in the system prompt."""
        ...

    @property
    def tool_access(self) -> ToolAccess:
        """Explicit Tool policy for prompt and provider schemas."""
        ...

    @property
    def allowed_skills(self) -> list[str]:
        """Skill allowlist for prompt-visible skills."""
        ...

    @property
    def tools(self) -> dict[str, Any]:
        """Optional Tool-owned settings from the root agent.json tools block."""
        ...

    @property
    def custom_system_prompt_enabled(self) -> bool:
        """Whether this Agent uses its private system prompt fragment scope."""
        ...


class PromptFragmentReader(Protocol):
    """Minimal prompt storage interface used by the system prompt manager."""

    def read_prompt_fragment(self, fragment_name: str) -> str:
        """Return a prompt fragment by resource name."""
        ...

    def read_agent_prompt_fragment(self, agent_id: str, fragment_name: str) -> str:
        """Return an Agent prompt fragment or an empty string when absent."""
        ...


class PromptAgentStore(Protocol):
    """Agent catalog methods needed for prompt scope validation."""

    def get(self, agent_id: str) -> PromptAgent:
        """Return one Agent by id."""
        ...

    def list(self) -> list[PromptAgent]:
        """Return all Agents."""
        ...


class ToolPromptRegistry(Protocol):
    """Tool registry methods needed for prompt and provider definitions."""

    def list_tools(self) -> list[Any]:
        """Return registered non-internal Tools, including Session-scoped Tools."""
        ...

    def prompt_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
        session_grants: Sequence[str] = (),
        profile_context: ToolDefinitionProfileContext | None = None,
    ) -> list[dict[str, Any]]:
        """Return prompt-ready tool name and description mappings."""
        ...

    def provider_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
        session_grants: Sequence[str] = (),
        ready_only: bool = True,
        profile_context: ToolDefinitionProfileContext | None = None,
    ) -> list[dict[str, Any]]:
        """Return provider-ready tool schemas; ``ready_only=False`` keeps not-ready Tools."""
        ...


class SkillPromptMetadata(Protocol):
    """Skill metadata fields needed for prompt assembly."""

    @property
    def name(self) -> str:
        """Stable skill identifier."""
        ...

    @property
    def description(self) -> str:
        """Prompt-visible skill description."""
        ...

    @property
    def origin(self) -> str | None:
        """Scope tag for catalog grouping (``None`` renders ungrouped)."""
        ...


class SkillPromptRegistry(Protocol):
    """Skill registry method needed for prompt-visible skill filtering."""

    def filter_allowed(self, allowed_skills: list[str]) -> Sequence[SkillPromptMetadata]:
        """Return prompt-visible skills filtered by an agent allowlist.

        Declared as a covariant ``Sequence`` (not ``list``) so the concrete
        ``SkillRegistry`` — whose ``filter_allowed`` returns ``list[SkillMetadata]``
        — structurally satisfies this protocol and a per-project registry can be
        passed straight through without a cast.
        """
        ...


@dataclass(frozen=True)
class PinnedSkillCatalog:
    """A prompt-epoch snapshot of an Agent's Skill catalog text.

    Captured on a Session's first build and reused until successful Compaction, when
    Chat rescans Skill sources and replaces it. This keeps the prompt prefix stable
    between Compactions while allowing the new Context epoch to advertise additions,
    removals, and metadata changes. ``catalog_text`` is the rendered
    ``<available_skills>`` block. Skill activation and ``/``-``$`` triggers remain
    live against the current registry; Tool presence is not pinned.
    """

    catalog_text: str


class ProjectContextSkill(Protocol):
    """Skill fields the explicit Project Context lists with paths."""

    @property
    def name(self) -> str:
        """Stable skill identifier."""
        ...

    @property
    def description(self) -> str:
        """Prompt-visible skill description."""
        ...

    @property
    def path(self) -> Path:
        """Absolute ``SKILL.md`` path an Identity Agent reads directly."""
        ...


class ChannelPromptMetadata(Protocol):
    """Channel fields needed for prompt-visible channel rendering."""

    @property
    def id(self) -> str:
        """Stable channel identifier."""
        ...

    @property
    def platform(self) -> str:
        """Channel platform identifier."""
        ...

    @property
    def agent_id(self) -> str:
        """Agent that owns this channel."""
        ...

    @property
    def allowed_chat_ids(self) -> list[str]:
        """Allowed platform chat identifiers."""
        ...

    @property
    def enabled(self) -> bool:
        """Whether this channel is enabled."""
        ...


class ChannelPromptRegistry(Protocol):
    """Channel registry methods needed for prompt-visible channel filtering."""

    def list_channels(self) -> Sequence[ChannelPromptMetadata]:
        """Return all configured channels."""
        ...


class MemoryPromptProvider(Protocol):
    """Pinned-memory renderer used by the ``memory_files`` producer.

    Only the entry half of the memory block lives here — the guidance and the
    ``<memory>`` wrapper are the ``memory:guidance`` block the memory domain
    declares (D6). This returns each selected scope's label and entries, which the
    ``{generated:memory_files}`` marker injects.
    """

    def read_prompt_files(
        self, workspace: Path, mode: MemoryPromptMode, *, memory_tool: str | None
    ) -> str:
        """Return the pinned-memory entries for a mode, each scope cut to its budget.

        *memory_tool* is the name under which the Agent can call the memory Tool,
        or ``None`` when it cannot; the notice for a cut scope names it only then.
        """
        ...
