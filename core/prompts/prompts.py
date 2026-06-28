"""System Prompt assembly and editable prompt fragment rules."""

from __future__ import annotations

import json
import platform
import socket
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any, Protocol

from core.memory import (
    DEFAULT_MEMORY_PROMPT_MODE,
    MEMORY_FILES_PRODUCER_NAME,
    MemoryPromptMode,
    MemoryService,
    memory_block_definition,
    read_memory_files,
)
from core.prompts.blocks import (
    BLOCK_KIND_DATA,
    BlockDefinition,
    BlockProducer,
    BlockRenderContext,
    BlockStore,
    CallableOwnerActivity,
    EmptyBlockStore,
    LayoutEntry,
    PromptError,
    assemble_system_prompt,
    expand_workspace_includes,
    load_layout_entries,
    wrap_include_file,
)
from core.tools.availability import MEMORY_TOOL_NAME, memory_tool_enabled
from core.utils.logging import get_logger

JsonObject = dict[str, Any]

EDITABLE_PROMPT_FRAGMENT_NAMES = (
    "system.md",
    "runtime.md",
    "tools.md",
    "channels.md",
    "skills.md",
)
PROMPT_FRAGMENT_VARIABLES: dict[str, list[dict[str, str]]] = {
    "system.md": [
        {
            "placeholder": "{agent_body}",
            "description": "Imported config-agent prompt body (empty for identity agents)",
        },
        {"placeholder": "{memory}", "description": "Rendered memory fragment"},
        {
            "placeholder": "{project_files}",
            "description": "Auto-loaded project files (empty when no project context)",
        },
        {"placeholder": "{runtime}", "description": "Rendered runtime fragment"},
        {"placeholder": "{tools}", "description": "Rendered tools fragment"},
        {"placeholder": "{channels}", "description": "Rendered channels fragment"},
        {"placeholder": "{skills}", "description": "Rendered skills fragment"},
        {
            "placeholder": "{include:filename}",
            "description": "Include another fragment by filename",
        },
    ],
    "runtime.md": [
        {"placeholder": "{app_version}", "description": "Application version string"},
        {"placeholder": "{host}", "description": "Host machine name"},
        {"placeholder": "{os}", "description": "Operating system name"},
        {"placeholder": "{model}", "description": "Active model identifier"},
        {"placeholder": "{agent_workspace}", "description": "Agent workspace directory path"},
        {"placeholder": "{app_dir}", "description": "Application source directory path"},
        {"placeholder": "{data_root}", "description": "Data root directory path"},
        {"placeholder": "{thinking_effort}", "description": "Agent thinking effort setting"},
        {"placeholder": "{current_date}", "description": "Current date in ISO 8601 format"},
    ],
    "tools.md": [
        {"placeholder": "{tool_list}", "description": "List of available tools"},
    ],
    "channels.md": [
        {"placeholder": "{channel_list}", "description": "List of active agent-bound channels"},
    ],
    "skills.md": [
        {"placeholder": "{skill_list}", "description": "List of available skills"},
    ],
}
# --- Block model: core/data block ids, owners, and the scope key ------------
#
# Every contribution to the System Prompt is a declared block (D6). The core
# blocks ship from the prompt resources; the data blocks carry per-run content.
# Owners drive gate 2 (is the owner active for this agent/run): ``always`` always
# renders, ``channel`` drops when no channel is active. The bundled default layout
# (``resources/prompts/layout.json``) lists these ids in their shipped order so an
# identity agent at home reproduces today's content/order.
CORE_RUNTIME_BLOCK_ID = "core:runtime"
CORE_TOOLS_BLOCK_ID = "core:tools"
CORE_CHANNELS_BLOCK_ID = "core:channels"
CORE_SKILLS_BLOCK_ID = "core:skills"
CORE_SOUL_BLOCK_ID = "core:soul"
CORE_PROJECT_FILES_BLOCK_ID = "core:project_files"
CORE_AGENT_BODY_BLOCK_ID = "core:agent_body"
BLOCK_OWNER_ALWAYS = "always"
BLOCK_OWNER_CHANNEL = "channel"
# The persistence/render-context key for the default scope (an agent scope is
# ``agent:<id>``). Kept here so the manager, the BlockStore, and the render context
# share one literal. ``AGENT_SCOPE_KEY_PREFIX`` is the prefix an agent scope key
# carries; a ``BlockStore`` adapter that bridges to a different scope token
# convention (e.g. the storage layer's ``None``/bare-id) translates against these.
DEFAULT_SCOPE_KEY = "default"
AGENT_SCOPE_KEY_PREFIX = "agent:"
# The SOUL data block renders the workspace SOUL.md through the one shared
# ``{include:…}`` expansion path, so its framing and fail-soft behavior never drift
# from a normal workspace include.
SOUL_INCLUDE_MARKER = "{include:SOUL.md}"
# The bundled default layout lives in a resource file (the honest "bundled default"
# — it keeps the shipped order out of code). Resolved relative to the repo root so
# the read is cwd-independent.
_RESOURCES_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "resources" / "prompts"
_LAYOUT_FILENAME = "layout.json"
_LOGGER = get_logger("prompts")


@dataclass(frozen=True)
class PromptScope:
    """Resolved prompt-fragment scope."""

    type: str
    agent_id: str | None = None


@dataclass(frozen=True)
class ProjectPromptContext:
    """The project inputs the prompt builder needs to render ``{project_files}``.

    Passed explicitly into ``build_system_prompt`` (and ``render_project_files``)
    so the prompt domain never imports project or agent classes — the caller (the
    chat loop) owns where ``cwd`` / ``auto_load`` come from. ``cwd`` is the project
    repo root; ``auto_load`` is the project's ordered file list (AGENTS.md is seeded
    into it as the first entry at project creation, not special-cased here).
    """

    cwd: Path
    auto_load: tuple[str, ...] = ()

    @classmethod
    def from_project(cls, cwd: str | Path, auto_load: Sequence[str]) -> ProjectPromptContext:
        """Build a context from a project's raw ``cwd`` string and auto-load list."""
        return cls(cwd=Path(cwd), auto_load=tuple(auto_load))


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
    def allowed_tools(self) -> list[str]:
        """Tool allowlist for prompt and provider schemas."""
        ...

    @property
    def allowed_skills(self) -> list[str]:
        """Skill allowlist for prompt-visible skills."""
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


class PromptFragmentStorage(PromptFragmentReader, Protocol):
    """Prompt fragment storage operations used by the editable prompt surface."""

    prompts_dir: Path

    def write_prompt_fragment(self, fragment_name: str, content: str) -> Path:
        """Write a user-copy prompt fragment."""
        ...

    def reset_prompt_fragment(self, fragment_name: str) -> Path:
        """Reset a user-copy prompt fragment to the bundled default."""
        ...

    def agent_prompts_dir(self, agent_id: str) -> Path:
        """Return the prompt-fragment directory for one Agent."""
        ...

    def agent_prompt_fragment_exists(self, agent_id: str, fragment_name: str) -> bool:
        """Return whether one Agent prompt fragment exists."""
        ...

    def write_agent_prompt_fragment(self, agent_id: str, fragment_name: str, content: str) -> Path:
        """Write one Agent prompt fragment."""
        ...

    def reset_agent_prompt_fragment(self, agent_id: str, fragment_name: str) -> Path:
        """Reset one Agent prompt fragment to current default-scope content."""
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

    def prompt_definitions(
        self, allowed_tools: Sequence[str] | None = None, *, include_internal: bool = False
    ) -> list[dict[str, Any]]:
        """Return prompt-ready tool name and description mappings."""
        ...

    def provider_definitions(
        self, allowed_tools: Sequence[str] | None = None, *, include_internal: bool = False
    ) -> list[dict[str, Any]]:
        """Return provider-ready tool schemas."""
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

    def has_active_channels(self) -> bool:
        """Return whether any channel adapter is currently running."""
        ...

    def list_channels(self) -> Sequence[ChannelPromptMetadata]:
        """Return all configured channels."""
        ...

    def _is_running(self, channel_id: str) -> bool:
        """Return whether one configured channel adapter is currently running."""
        ...


class MemoryPromptProvider(Protocol):
    """Pinned-memory file renderer used by the ``memory_files`` producer.

    Only the **file** half of the memory block now lives here — the guidance and
    the ``<memory>`` wrapper moved into the ``memory:guidance`` block the memory
    domain declares (D6). This returns just the ``<file>``-wrapped contents (``""``
    when empty/absent), which the ``{generated:memory_files}`` marker injects.
    """

    def read_prompt_files(self, workspace: Path, mode: MemoryPromptMode) -> str:
        """Return the ``<file>``-wrapped pinned-memory file contents for a mode."""
        ...


@dataclass(frozen=True)
class PromptFragment:
    """Editable prompt fragment response model."""

    name: str
    content: str
    is_modified: bool
    variables: list[dict[str, str]]

    def to_dict(self) -> JsonObject:
        return {
            "name": self.name,
            "content": self.content,
            "is_modified": self.is_modified,
            "variables": list(self.variables),
        }


class PromptFragmentManager:
    """Manage editable prompt fragments through a storage backend."""

    def __init__(
        self,
        storage: PromptFragmentStorage,
        agent_store: PromptAgentStore | None = None,
    ) -> None:
        self._storage = storage
        self._agent_store = agent_store

    def list_scopes(self) -> list[JsonObject]:
        """Return prompt scopes available to the System Prompt editor."""
        scopes: list[JsonObject] = [{"type": "default", "label": "Default"}]
        if self._agent_store is None:
            return scopes

        for agent in sorted(self._agent_store.list(), key=lambda item: item.id):
            if not agent.custom_system_prompt_enabled:
                continue
            scopes.append(
                {
                    "type": "agent",
                    "agent_id": agent.id,
                    "label": agent.name or agent.id,
                }
            )
        return scopes

    def validate_scope(self, scope: Any = None) -> PromptScope:
        """Resolve and validate a public prompt scope payload."""
        return self._resolve_scope(scope)

    def list_fragments(self, scope: Any = None) -> list[JsonObject]:
        """Return editable prompt fragments in stable UI order."""
        prompt_scope = self._resolve_scope(scope)
        fragments: list[JsonObject] = []
        for name in EDITABLE_PROMPT_FRAGMENT_NAMES:
            if prompt_scope.type == "agent":
                agent_id = _require_scope_agent_id(prompt_scope)
                fragment = PromptFragment(
                    name=name,
                    content=self._storage.read_agent_prompt_fragment(agent_id, name),
                    is_modified=self._storage.agent_prompt_fragment_exists(agent_id, name),
                    variables=PROMPT_FRAGMENT_VARIABLES.get(name, []),
                )
            else:
                fragment = PromptFragment(
                    name=name,
                    content=self._storage.read_prompt_fragment(name),
                    is_modified=(self._storage.prompts_dir / name).exists(),
                    variables=PROMPT_FRAGMENT_VARIABLES.get(name, []),
                )
            fragments.append(fragment.to_dict())
        return fragments

    def update_fragment(self, name: str, content: str, scope: Any = None) -> JsonObject:
        """Write an editable prompt fragment and return its public state."""
        fragment_name = _validate_editable_prompt_fragment_name(name)
        prompt_scope = self._resolve_scope(scope)
        if prompt_scope.type == "agent":
            agent_id = _require_scope_agent_id(prompt_scope)
            self._storage.write_agent_prompt_fragment(agent_id, fragment_name, content)
            return {"name": fragment_name, "content": content, "is_modified": True}

        self._storage.write_prompt_fragment(fragment_name, content)
        return {"name": fragment_name, "content": content, "is_modified": True}

    def reset_fragment(self, name: str, scope: Any = None) -> JsonObject:
        """Reset an editable prompt fragment to its default content."""
        fragment_name = _validate_editable_prompt_fragment_name(name)
        prompt_scope = self._resolve_scope(scope)
        if prompt_scope.type == "agent":
            agent_id = _require_scope_agent_id(prompt_scope)
            self._storage.reset_agent_prompt_fragment(agent_id, fragment_name)
            content = self._storage.read_agent_prompt_fragment(agent_id, fragment_name)
            return {"name": fragment_name, "content": content, "is_modified": True}

        self._storage.reset_prompt_fragment(fragment_name)
        content = self._storage.read_prompt_fragment(fragment_name)
        return {"name": fragment_name, "content": content, "is_modified": False}

    def _resolve_scope(self, scope: Any = None) -> PromptScope:
        prompt_scope = _normalize_prompt_scope(scope)
        if prompt_scope.type == "default":
            return prompt_scope
        if self._agent_store is None:
            raise PromptError("Agent prompt scopes are not available")

        agent_id = _require_scope_agent_id(prompt_scope)
        try:
            agent = self._agent_store.get(agent_id)
        except Exception as exc:
            raise PromptError(f"unknown prompt scope agent: {agent_id}") from exc
        if not agent.custom_system_prompt_enabled:
            raise PromptError(f"Agent prompt scope is not enabled: {agent_id}")
        return prompt_scope


class SystemPromptManager:
    """Assemble system prompts from prompt fragments and workspace includes."""

    def __init__(
        self,
        storage: PromptFragmentReader,
        tool_registry: ToolPromptRegistry,
        skill_registry: SkillPromptRegistry,
        channel_registry: ChannelPromptRegistry | None = None,
        *,
        app_version: str,
        app_dir: str | Path,
        data_root: str | Path,
        memory_provider: MemoryPromptProvider | None = None,
        host: str | None = None,
        os_name: str | None = None,
        current_date: Callable[[], str] | None = None,
        loaded_extensions: Collection[str] = (),
        block_definitions: Sequence[BlockDefinition] = (),
        block_store: BlockStore | None = None,
        default_layout: Sequence[LayoutEntry] | None = None,
    ) -> None:
        self._storage = storage
        self._tool_registry = tool_registry
        self._skill_registry = skill_registry
        self._channel_registry = channel_registry
        self._memory_provider = memory_provider or MemoryService()
        self._app_version = app_version
        self._app_dir = Path(app_dir)
        self._data_root = Path(data_root)
        self._host = host
        self._os_name = os_name
        self._current_date = current_date or _current_utc_date
        # The set of loaded extension names, gate 2's input for ``extension:<name>``
        # owners (D5/D6). The runtime rebuilds and injects it on every extension
        # (re)load. Held as a frozenset for cheap membership checks.
        self._loaded_extensions = frozenset(loaded_extensions)
        # Contributed block definitions collected by the runtime: tool-owned and
        # extension-owned blocks plus the memory block (D6). The core and data
        # blocks are built per run by this manager (they need the run's
        # agent_body/project_context and the runtime variables); the contributed
        # ones are static and merged in on every build. The prompts domain consumes
        # this list of definitions and never imports concrete tool/extension classes.
        self._block_definitions = tuple(block_definitions)
        # The persisted layout + per-block override source (Phase 2). Defaults to an
        # empty store so a manager wired without persistence (unit tests, any path
        # not yet handed Phase 2's store) defaults every block in at its rank and
        # uses owner defaults.
        self._block_store = block_store or EmptyBlockStore()
        # The bundled default layout (the order + on/off the core blocks ship with),
        # used for any scope that has no saved layout. Loaded from
        # ``resources/prompts/layout.json`` by default so the shipped order lives in
        # a resource file, not in code.
        self._default_layout = (
            tuple(default_layout)
            if default_layout is not None
            else tuple(load_bundled_default_layout())
        )

    @property
    def app_dir(self) -> Path:
        """Application source directory this prompt manager was built with."""
        return self._app_dir

    def update_skill_registry(self, skill_registry: SkillPromptRegistry) -> None:
        """Replace the skill registry used for prompt and provider tool decisions."""
        self._skill_registry = skill_registry

    def update_block_definitions(
        self,
        block_definitions: Sequence[BlockDefinition],
        loaded_extensions: Collection[str],
    ) -> None:
        """Replace the contributed block definitions + loaded-extension set.

        Called by the runtime when extensions/skills reload so the block list and
        gate-2's ``extension:<name>`` membership refresh without an app restart
        (the block list is rebuilt on every (re)load and re-handed here — no live
        registry, no per-run reload).
        """
        self._block_definitions = tuple(block_definitions)
        self._loaded_extensions = frozenset(loaded_extensions)

    def build_system_prompt(
        self,
        agent: PromptAgent,
        scope: Any = None,
        *,
        agent_body: str = "",
        project_context: ProjectPromptContext | None = None,
        skill_registry: SkillPromptRegistry | None = None,
    ) -> str:
        """Build the complete system prompt for an agent (the block-model path).

        Collects the full block-definition list for this agent/run (the core text
        blocks built from the prompt resources, the SOUL / project-files /
        agent-body data blocks, and the contributed memory / tool / extension
        blocks), reads the active scope's layout + overrides, and routes everything
        through the deterministic assembly engine.

        ``agent_body`` is a config agent's imported prompt body, inserted verbatim
        through the ``core:agent_body`` data block (empty for identity agents →
        collapses). ``project_context`` carries the project's cwd and auto-load list
        for the ``core:project_files`` data block (``None`` off a project →
        collapses). ``skill_registry`` overrides the registry the skills block is
        filtered against — a project run passes its project-scoped registry; ``None``
        uses the configured global one (identity runs, unchanged).
        """
        prompt_scope = self._resolve_build_scope(agent, scope)
        scope_key = self._scope_key(prompt_scope)
        context = BlockRenderContext(agent=agent, project_context=project_context, scope=scope_key)
        producers = self._build_producers(agent, skill_registry)
        definitions = self._collect_block_definitions(
            agent,
            prompt_scope,
            agent_body=agent_body,
        )
        layout = self._resolve_scope_layout(scope_key)
        return assemble_system_prompt(
            definitions,
            layout,
            context,
            owner_activity=CallableOwnerActivity(self._is_owner_active),
            override_resolver=self._build_override_resolver(prompt_scope),
            producers=producers,
            replacements=self._runtime_replacements(agent),
        )

    def _collect_block_definitions(
        self,
        agent: PromptAgent,
        prompt_scope: PromptScope,
        *,
        agent_body: str,
    ) -> list[BlockDefinition]:
        """Build the full ordered-agnostic block-definition list for one build.

        The core text blocks (runtime/tools/channels/skills) carry their default
        text from the active scope's prompt resources; the data blocks (SOUL,
        project files, agent body) carry their per-run content. The contributed
        blocks (memory, tools, extensions) are merged in last — assembly dedupes by
        id (first wins), so a core block can never be shadowed by a contributor.
        """
        definitions = [
            *self._core_text_block_definitions(agent, prompt_scope),
            memory_block_definition(),
            *self._data_block_definitions(agent_body=agent_body),
            *self._block_definitions,
        ]
        return definitions

    def _core_text_block_definitions(
        self,
        agent: PromptAgent,
        prompt_scope: PromptScope,
    ) -> list[BlockDefinition]:
        """Return the core ``always``/``channel`` text blocks for the active scope.

        Each block's default text is the scope-aware prompt fragment (the bundled
        resource for the default scope, the agent copy for an agent scope — with no
        default fallback, exactly as today). The runtime block keeps its ``{host}``/
        ``{model}``/… placeholders (filled by the build-time replacements); the
        tools/channels/skills blocks carry the ``{generated:…}`` list markers. The
        channels block is owner ``channel`` so it drops entirely when no channel is
        active (no more ``- None``).
        """
        return [
            BlockDefinition(
                id=CORE_RUNTIME_BLOCK_ID,
                owner=BLOCK_OWNER_ALWAYS,
                default_text=self._read_prompt_fragment(prompt_scope, agent.id, "runtime.md"),
            ),
            BlockDefinition(
                id=CORE_TOOLS_BLOCK_ID,
                owner=BLOCK_OWNER_ALWAYS,
                default_text=self._read_prompt_fragment(prompt_scope, agent.id, "tools.md"),
            ),
            BlockDefinition(
                id=CORE_CHANNELS_BLOCK_ID,
                owner=BLOCK_OWNER_CHANNEL,
                default_text=self._read_prompt_fragment(prompt_scope, agent.id, "channels.md"),
            ),
            BlockDefinition(
                id=CORE_SKILLS_BLOCK_ID,
                owner=BLOCK_OWNER_ALWAYS,
                default_text=self._read_prompt_fragment(prompt_scope, agent.id, "skills.md"),
            ),
        ]

    def _data_block_definitions(self, *, agent_body: str) -> list[BlockDefinition]:
        """Return the SOUL / project-files / agent-body data blocks (D2).

        All three are ``kind="data"`` (positionable, not editable) and owner
        ``always``; each collapses to nothing when its content is empty (gate 3):

        - ``core:soul`` renders the workspace ``SOUL.md`` via a ``render`` that uses
          the same ``{include:SOUL.md}`` expansion as before — empty when the file
          is missing or the workspace is ``""`` (a config agent).
        - ``core:project_files`` renders ``render_project_files(project_context)`` —
          empty without a project context or readable files.
        - ``core:agent_body`` carries the verbatim config-agent body as ``data``
          default text, so its ``{…}`` is never re-interpreted; empty for identity
          agents.
        """
        return [
            BlockDefinition(
                id=CORE_SOUL_BLOCK_ID,
                owner=BLOCK_OWNER_ALWAYS,
                kind=BLOCK_KIND_DATA,
                render=self._render_soul_block,
            ),
            BlockDefinition(
                id=CORE_PROJECT_FILES_BLOCK_ID,
                owner=BLOCK_OWNER_ALWAYS,
                kind=BLOCK_KIND_DATA,
                render=self._render_project_files_block,
            ),
            BlockDefinition(
                id=CORE_AGENT_BODY_BLOCK_ID,
                owner=BLOCK_OWNER_ALWAYS,
                kind=BLOCK_KIND_DATA,
                default_text=agent_body,
            ),
        ]

    def _render_soul_block(self, context: BlockRenderContext) -> str:
        """Render the ``core:soul`` data block from the workspace ``SOUL.md``.

        Reuses the single ``{include:…}`` expansion path so framing and fail-soft
        behavior (missing/unreadable → dropped, unsafe path → ``PromptError``,
        empty workspace → no read) never drift from a normal include.
        """
        return expand_workspace_includes(SOUL_INCLUDE_MARKER, context.agent.workspace)

    def _render_project_files_block(self, context: BlockRenderContext) -> str:
        """Render the ``core:project_files`` data block (the auto-load files)."""
        return self.render_project_files(context.project_context)

    def _runtime_replacements(self, agent: PromptAgent) -> dict[str, str]:
        """Return the build-time runtime-variable substitutions (``{host}``, …).

        Applied to every text block by the engine; only the runtime block carries
        these placeholders today, but treating them as build-time globals matches
        how ``{app_version}`` worked at the root before the block model.
        """
        thinking_effort = "default" if agent.thinking_effort is None else agent.thinking_effort
        return {
            "{host}": self._host or socket.gethostname(),
            "{app_version}": self._app_version,
            "{os}": self._os_name or platform.platform(),
            "{model}": agent.model,
            "{agent_workspace}": agent.workspace,
            "{app_dir}": str(self._app_dir.resolve()),
            "{data_root}": str(self._data_root.resolve()),
            "{thinking_effort}": thinking_effort,
            "{current_date}": self._current_date(),
        }

    def _resolve_scope_layout(self, scope_key: str) -> Sequence[LayoutEntry]:
        """Return the active scope's saved layout, or the bundled default if none.

        A scope with a saved ``layout.json`` owns its order + on/off (D3/D4); a
        scope with none defaults to the bundled layout so the shipped order applies.
        Either way, a definition absent from the layout still defaults in at its
        rank (the assembly engine handles that), and an inert entry is skipped.
        """
        saved = self._block_store.read_layout(scope_key)
        return saved if saved else self._default_layout

    def _build_override_resolver(self, prompt_scope: PromptScope) -> Callable[..., str | None]:
        """Build the per-scope override resolver feeding the assembly cascade (D3).

        Implements the override cascade over the injected :class:`BlockStore`:
        agent-scope override ← default-scope override ← owner default (the engine's
        fallback to ``definition.default_text``). For a default build there is no
        agent layer. With the empty store no override exists, so every block uses
        its owner default text (its scope-aware fragment / data content).
        """
        agent_scope_key = self._scope_key(prompt_scope) if prompt_scope.type == "agent" else None

        def resolve(definition: BlockDefinition, _scope: str) -> str | None:
            if agent_scope_key is not None:
                agent_override = self._block_store.read_block_override(
                    agent_scope_key, definition.id
                )
                if agent_override is not None:
                    return agent_override
            return self._block_store.read_block_override(DEFAULT_SCOPE_KEY, definition.id)

        return resolve

    @staticmethod
    def _scope_key(prompt_scope: PromptScope) -> str:
        """Return the persistence key for a resolved build scope.

        ``default`` for the default scope, ``agent:<id>`` for an agent scope — the
        single string the :class:`BlockStore` and the render context use, so the
        colon-free path mapping (Phase 2) and the override cascade agree on the key.
        """
        if prompt_scope.type == "agent" and prompt_scope.agent_id:
            return f"{AGENT_SCOPE_KEY_PREFIX}{prompt_scope.agent_id}"
        return DEFAULT_SCOPE_KEY

    def render_project_files(self, project_context: ProjectPromptContext | None) -> str:
        """Render the project's auto-loaded files as ``<file>``-wrapped blocks.

        The ``auto_load`` files in list order. AGENTS.md is no longer special — it
        is seeded as the first entry at project creation, so the list is the single
        source of what loads. Each existing file wrapped exactly like ``{include}``
        (one source of wrap logic). Auto-load paths are taken verbatim — relative to
        the project cwd at any subfolder depth, or absolute, with no location
        restriction (see ``_read_project_file_block``). Lazy: returns ``""`` when
        there is no project context or no readable file, so the placeholder
        collapses. No size limit, truncation, or warning on large files — the
        technical user gets the file 1:1.

        This is the single render used both for ``{project_files}`` in the system
        prompt (project-born sessions) and for the visiting main agent's
        ``<system-reminder>`` (same content, different delivery).
        """
        if project_context is None:
            return ""

        blocks: list[str] = []
        for name in project_context.auto_load:
            block = self._read_project_file_block(project_context.cwd, name)
            if block is not None:
                blocks.append(block)
        return "\n".join(blocks)

    def _read_project_file_block(self, cwd: Path, filename: str) -> str | None:
        """Read one project auto-load file and wrap it, or ``None`` when absent.

        The path is used **as the user wrote it** in the project's auto-load list:
        a relative path resolves against the project ``cwd`` at any subfolder depth,
        an absolute path is read as-is. There is deliberately **no location
        restriction** — the auto-load list is the user's own config naming the
        user's own files, so where a file lives is not vBot's business (project
        philosophy: maximum agency, minimal restrictions). A missing file is skipped
        silently (lazy rendering); an unreadable file raises, matching ``{include}``.
        The ``<file>`` wrap is shared with ``{include}`` so framing cannot drift.
        """
        file_path = cwd / filename
        try:
            content = file_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Lazy: a configured-but-absent file is normal — including the seeded
            # AGENTS.md before the repo actually has one — so skip quietly here
            # rather than warn every turn.
            return None
        except (OSError, ValueError) as exc:
            # Present but unreadable for ANY reason — locked, no permission, a
            # directory, binary/non-UTF-8, a malformed path. A prompt-load file must
            # never abort the run (user decision): log and skip, so one bad auto-load
            # entry can never take the whole turn down. OSError covers the filesystem
            # failures, ValueError the decode/bad-path ones.
            _LOGGER.warning("Skipping unreadable project file %s: %s", file_path, exc)
            return None
        return wrap_include_file(filename, content)

    def provider_tool_definitions(
        self,
        agent: PromptAgent,
        *,
        skill_registry: SkillPromptRegistry | None = None,
    ) -> list[dict[str, Any]]:
        """Return provider tool definitions filtered by the agent allowlist.

        ``skill_registry`` scopes the "does this agent have loadable skills?" check
        that decides whether the internal ``skill`` tool is exposed — a project run
        passes its project registry so the tool appears only when the project's
        effective skills are non-empty; ``None`` uses the global registry.
        """
        active_skill_registry = self._resolve_skill_registry(skill_registry)
        definitions = self._provider_definitions_for_agent(agent)
        if not self._agent_has_loadable_skills(agent, active_skill_registry):
            return definitions

        return [
            *definitions,
            *self._tool_registry.provider_definitions(["skill"], include_internal=True),
        ]

    def _resolve_skill_registry(
        self, skill_registry: SkillPromptRegistry | None
    ) -> SkillPromptRegistry:
        """Return the per-call registry, or the configured global one when absent."""
        return skill_registry if skill_registry is not None else self._skill_registry

    def _build_producers(
        self,
        agent: PromptAgent,
        skill_registry: SkillPromptRegistry | None,
    ) -> dict[str, BlockProducer]:
        """Build the ``{generated:NAME}`` producer registry for this build.

        Each producer is a closure over the registries the manager already holds
        (and the per-call skill-registry override), so the list-formatting logic
        lives in one place. ``tool_list``/``channel_list``/``skill_list`` feed the
        core tools/channels/skills blocks; ``memory_files`` renders the
        ``USER.md``/``MEMORY.md`` ``<file>`` contents per the agent's memory mode
        (the embedded data half of the ``memory:guidance`` block — the file reading
        itself lives in the memory domain's :func:`read_memory_files`).
        """
        active_skill_registry = self._resolve_skill_registry(skill_registry)

        def tool_list(context: BlockRenderContext) -> str:
            return _format_tool_list(self._prompt_definitions_for_agent(context.agent))

        def channel_list(context: BlockRenderContext) -> str:
            return _format_channel_list(self._agent_active_channels(context.agent))

        def skill_list(context: BlockRenderContext) -> str:
            return _format_skill_list(
                active_skill_registry.filter_allowed(context.agent.allowed_skills)
            )

        def memory_files(context: BlockRenderContext) -> str:
            mode = getattr(context.agent, "memory_prompt_mode", DEFAULT_MEMORY_PROMPT_MODE)
            return read_memory_files(
                Path(context.agent.workspace), mode, provider=self._memory_provider
            )

        return {
            "tool_list": tool_list,
            "channel_list": channel_list,
            "skill_list": skill_list,
            MEMORY_FILES_PRODUCER_NAME: memory_files,
        }

    def _is_owner_active(self, owner: str, agent: PromptAgent) -> bool:
        """Return whether a block's owner is active for *agent* (gate 2, D5).

        Reads the same seams the manager already applies — never a hardcoded or
        re-implemented gate:

        - ``always`` → always true.
        - ``memory`` → the memory tool is enabled for the agent.
        - ``tool:<name>`` → ``<name>`` is in the agent's effective allowed tools.
        - ``channel`` → the agent has at least one active+enabled+running channel.
        - ``extension:<name>`` → the extension is in the loaded-extension set the
          runtime rebuilds and injects on every extension (re)load.
        """
        if owner == "always":
            return True
        if owner == "memory":
            mode = getattr(agent, "memory_prompt_mode", DEFAULT_MEMORY_PROMPT_MODE)
            return memory_tool_enabled(mode)
        if owner == "channel":
            return bool(self._agent_active_channels(agent))
        tool_prefix = "tool:"
        if owner.startswith(tool_prefix):
            tool_name = owner[len(tool_prefix) :]
            return self._agent_tool_allowed(agent, tool_name)
        extension_prefix = "extension:"
        if owner.startswith(extension_prefix):
            return owner[len(extension_prefix) :] in self._loaded_extensions
        return False

    def _agent_tool_allowed(self, agent: PromptAgent, tool_name: str) -> bool:
        """Return whether *tool_name* is in the agent's effective prompt tool set.

        Reuses the same prompt-definition path the tools block uses (allowlist +
        derived ``memory`` visibility), so gate 2 cannot drift from what the tool
        list actually shows.
        """
        definitions = self._prompt_definitions_for_agent(agent)
        return any(definition.get("name") == tool_name for definition in definitions)

    def _provider_definitions_for_agent(self, agent: PromptAgent) -> list[JsonObject]:
        definitions = self._tool_registry.provider_definitions(agent.allowed_tools)
        return self._apply_memory_tool_visibility(
            definitions,
            agent,
            self._tool_registry.provider_definitions,
        )

    def _prompt_definitions_for_agent(self, agent: PromptAgent) -> list[JsonObject]:
        definitions = self._tool_registry.prompt_definitions(agent.allowed_tools)
        return self._apply_memory_tool_visibility(
            definitions,
            agent,
            self._tool_registry.prompt_definitions,
        )

    def _apply_memory_tool_visibility(
        self,
        definitions: list[JsonObject],
        agent: PromptAgent,
        definition_loader: Callable[[Sequence[str]], list[JsonObject]],
    ) -> list[JsonObject]:
        mode = getattr(agent, "memory_prompt_mode", DEFAULT_MEMORY_PROMPT_MODE)
        if not memory_tool_enabled(mode):
            return [
                definition
                for definition in definitions
                if definition.get("name") != MEMORY_TOOL_NAME
            ]

        if any(definition.get("name") == MEMORY_TOOL_NAME for definition in definitions):
            return definitions

        return [*definitions, *definition_loader([MEMORY_TOOL_NAME])]

    def _resolve_build_scope(self, agent: PromptAgent, scope: Any = None) -> PromptScope:
        if scope is None:
            if agent.custom_system_prompt_enabled:
                return PromptScope("agent", agent.id)
            return PromptScope("default")

        prompt_scope = _normalize_prompt_scope(scope)
        if prompt_scope.type == "default":
            return prompt_scope

        agent_id = _require_scope_agent_id(prompt_scope)
        if agent_id != agent.id:
            raise PromptError("Agent prompt scope must match the preview Agent")
        if not agent.custom_system_prompt_enabled:
            raise PromptError(f"Agent prompt scope is not enabled: {agent_id}")
        return prompt_scope

    def _read_prompt_fragment(
        self,
        scope: PromptScope,
        agent_id: str,
        fragment_name: str,
    ) -> str:
        if scope.type == "agent":
            return self._storage.read_agent_prompt_fragment(agent_id, fragment_name)
        return self._storage.read_prompt_fragment(fragment_name)

    def _agent_has_loadable_skills(
        self, agent: PromptAgent, skill_registry: SkillPromptRegistry
    ) -> bool:
        return bool(skill_registry.filter_allowed(agent.allowed_skills))

    def _agent_active_channels(self, agent: PromptAgent) -> list[ChannelPromptMetadata]:
        channel_registry = self._channel_registry
        if channel_registry is None or not channel_registry.has_active_channels():
            return []

        active_channels: list[ChannelPromptMetadata] = []
        for channel in channel_registry.list_channels():
            if channel.agent_id != agent.id:
                continue
            if not channel.enabled:
                continue
            if not channel_registry._is_running(channel.id):
                continue
            active_channels.append(channel)
        return active_channels


def _normalize_prompt_scope(scope: Any = None) -> PromptScope:
    if scope is None:
        return PromptScope("default")
    if isinstance(scope, PromptScope):
        return scope
    if not isinstance(scope, Mapping):
        raise PromptError("prompt scope must be an object")

    unsupported_fields = sorted(set(scope) - {"type", "agent_id"})
    if unsupported_fields:
        raise PromptError(f"unsupported prompt scope fields: {', '.join(unsupported_fields)}")

    scope_type = scope.get("type", "default")
    if scope_type == "default":
        if scope.get("agent_id") not in (None, ""):
            raise PromptError("default prompt scope must not include agent_id")
        return PromptScope("default")
    if scope_type == "agent":
        agent_id = scope.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            raise PromptError("agent prompt scope requires agent_id")
        return PromptScope("agent", agent_id)

    raise PromptError(f"unknown prompt scope type: {scope_type}")


def _require_scope_agent_id(scope: PromptScope) -> str:
    if scope.agent_id is None:
        raise PromptError("agent prompt scope requires agent_id")
    return scope.agent_id


def _validate_editable_prompt_fragment_name(name: str) -> str:
    if name not in EDITABLE_PROMPT_FRAGMENT_NAMES:
        raise PromptError(f"unknown prompt fragment: {name}")
    return name


def _current_utc_date() -> str:
    return datetime.now(UTC).date().isoformat()


def load_bundled_default_layout() -> list[LayoutEntry]:
    """Load the bundled default block layout from ``resources/prompts/layout.json``.

    The shipped order + on/off of the core blocks, kept in a resource file so the
    default layout lives as data, not code. A missing or malformed file reads as an
    empty layout — every block then defaults in at its rank, so the prompt still
    assembles (a broken bundled layout must never take a build down).
    """
    layout_path = _RESOURCES_PROMPTS_DIR / _LAYOUT_FILENAME
    try:
        raw = json.loads(layout_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _LOGGER.warning("Bundled prompt layout not found: %s", layout_path)
        return []
    except (OSError, ValueError) as exc:
        _LOGGER.warning("Skipping unreadable bundled prompt layout %s: %s", layout_path, exc)
        return []
    return load_layout_entries(raw)


def _format_tool_list(tool_definitions: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- {definition['name']}: {definition['description']}" for definition in tool_definitions
    )


def _format_channel_list(channels: list[ChannelPromptMetadata]) -> str:
    # No ``- None`` fallback anymore: the ``core:channels`` block is owner
    # ``channel``, so with no active channels the whole block gates out (D5). This
    # producer is only invoked when at least one channel is active.
    lines: list[str] = []
    for channel in channels:
        target_hint = (
            "default target available"
            if len(channel.allowed_chat_ids) == 1
            else "explicit target required"
        )
        lines.append(f"- {channel.id}: {channel.platform} ({target_hint})")
    return "\n".join(lines)


def _format_skill_list(skills: Sequence[SkillPromptMetadata]) -> str:
    lines = ["<available_skills>"]
    for skill in skills:
        lines.extend(
            [
                "  <skill>",
                f"    <name>{escape(skill.name)}</name>",
                f"    <description>{escape(skill.description)}</description>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    return "\n".join(lines)
