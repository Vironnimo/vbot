"""System Prompt assembly and prompt-epoch rendering."""

from __future__ import annotations

import platform
import socket
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.memory import (
    DEFAULT_MEMORY_PROMPT_MODE,
    MEMORY_FILES_PRODUCER_NAME,
    MemoryService,
    memory_block_definition,
    memory_prompt_file_paths,
    read_memory_files,
)
from core.prompts._catalog import (
    PromptBlockCatalog,
    _normalize_prompt_scope,
    _require_scope_agent_id,
    load_bundled_default_layout,
)
from core.prompts._formatting import _format_channel_list, _format_skill_catalog, _format_tool_list
from core.prompts._types import (
    AGENT_SCOPE_KEY_PREFIX,
    BLOCK_OWNER_ALWAYS,
    BLOCK_OWNER_CHANNEL,
    BLOCK_OWNER_IDENTITY,
    BLOCK_OWNER_SKILL_MANAGE,
    CORE_AGENT_BODY_BLOCK_ID,
    CORE_CHANNELS_BLOCK_ID,
    CORE_IDENTITY_RUNTIME_BLOCK_ID,
    CORE_RUNTIME_BLOCK_ID,
    CORE_SKILL_MAINTENANCE_BLOCK_ID,
    CORE_SKILLS_BLOCK_ID,
    CORE_SOUL_BLOCK_ID,
    CORE_TOOLS_BLOCK_ID,
    CORE_TOOLS_LIST_BLOCK_ID,
    CORE_WORKING_PROJECT_BLOCK_ID,
    DEFAULT_SCOPE_KEY,
    INHERITANCE_AGENT_OVERRIDE,
    INHERITANCE_DEFAULT_OVERRIDE,
    INHERITANCE_OWNER_DEFAULT,
    SOUL_FRAMING,
    SOUL_INCLUDE_MARKER,
    USER_BLOCK_ID_PREFIX,
    USER_BLOCK_SOURCE,
    ChannelPromptMetadata,
    ChannelPromptRegistry,
    JsonObject,
    MemoryPromptProvider,
    PinnedSkillCatalog,
    ProjectContextSkill,
    ProjectPromptContext,
    PromptAgent,
    PromptAgentStore,
    PromptFragmentReader,
    PromptScope,
    SkillPromptMetadata,
    SkillPromptRegistry,
    ToolPromptRegistry,
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
    apply_replacements,
    assemble_system_prompt,
    expand_workspace_includes,
    resolve_layout,
    wrap_include_file,
)
from core.tools.availability import (
    agent_tool_settings,
    apply_agent_target_tool_visibility,
    memory_tool_enabled,
    resolve_tool_access,
    subagent_allowed_agents,
)
from core.tools.tools import ToolDefinitionProfileContext
from core.utils.logging import get_logger
from core.utils.paths import model_path
from core.utils.workers import BoundedWorkerPool

__all__ = [
    "AGENT_SCOPE_KEY_PREFIX",
    "BLOCK_OWNER_ALWAYS",
    "BLOCK_OWNER_CHANNEL",
    "BLOCK_OWNER_IDENTITY",
    "BLOCK_OWNER_SKILL_MANAGE",
    "CORE_AGENT_BODY_BLOCK_ID",
    "CORE_CHANNELS_BLOCK_ID",
    "CORE_IDENTITY_RUNTIME_BLOCK_ID",
    "CORE_RUNTIME_BLOCK_ID",
    "CORE_SKILLS_BLOCK_ID",
    "CORE_SKILL_MAINTENANCE_BLOCK_ID",
    "CORE_SOUL_BLOCK_ID",
    "CORE_TOOLS_BLOCK_ID",
    "CORE_TOOLS_LIST_BLOCK_ID",
    "CORE_WORKING_PROJECT_BLOCK_ID",
    "ChannelPromptMetadata",
    "ChannelPromptRegistry",
    "DEFAULT_SCOPE_KEY",
    "INHERITANCE_AGENT_OVERRIDE",
    "INHERITANCE_DEFAULT_OVERRIDE",
    "INHERITANCE_OWNER_DEFAULT",
    "JsonObject",
    "MemoryPromptProvider",
    "PROMPT_WORKER_LIMIT",
    "PinnedSkillCatalog",
    "ProjectContextSkill",
    "ProjectPromptContext",
    "PromptAgent",
    "PromptAgentStore",
    "PromptError",
    "PromptFragmentReader",
    "PromptScope",
    "SOUL_FRAMING",
    "SOUL_INCLUDE_MARKER",
    "SkillPromptMetadata",
    "SkillPromptRegistry",
    "SystemPromptManager",
    "ToolPromptRegistry",
    "USER_BLOCK_ID_PREFIX",
    "USER_BLOCK_SOURCE",
    "load_bundled_default_layout",
]

PROMPT_WORKER_LIMIT = 4
_LOGGER = get_logger("prompts")

_PROMPT_WORKERS = BoundedWorkerPool(
    name="prompt",
    max_workers=PROMPT_WORKER_LIMIT,
)


class SystemPromptManager:
    """Assemble System Prompts and expose the owned block-edit catalog."""

    def __init__(
        self,
        storage: PromptFragmentReader,
        tool_registry: ToolPromptRegistry,
        skill_registry: SkillPromptRegistry,
        channel_registry: ChannelPromptRegistry | None = None,
        *,
        vbot_version: str,
        vbot_root: str | Path,
        data_root: str | Path,
        memory_provider: MemoryPromptProvider | None = None,
        server_hostname: str | None = None,
        operating_system: str | None = None,
        current_local_date: Callable[[], str] | None = None,
        timezone_name: Callable[[], str] | None = None,
        loaded_extensions: Collection[str] = (),
        block_definitions: Sequence[BlockDefinition] = (),
        block_store: BlockStore | None = None,
        default_layout: Sequence[LayoutEntry] | None = None,
        agent_store: PromptAgentStore | None = None,
    ) -> None:
        self._storage = storage
        self._tool_registry = tool_registry
        self._skill_registry = skill_registry
        self._channel_registry = channel_registry
        self._memory_provider = memory_provider or MemoryService()
        self._vbot_version = vbot_version
        self._vbot_root = Path(vbot_root)
        self._data_root = Path(data_root)
        self._server_hostname = server_hostname
        self._operating_system = operating_system
        self._timezone_name = timezone_name or (lambda: "UTC")
        self._current_local_date = current_local_date
        # The set of loaded extension names, gate 2's input for ``extension:<name>``
        # owners (D5/D6). The runtime rebuilds and injects it on every extension
        # (re)load. Held as a frozenset for cheap membership checks.
        self._loaded_extensions = frozenset(loaded_extensions)
        self._catalog = PromptBlockCatalog(
            storage,
            block_store or EmptyBlockStore(),
            default_layout if default_layout is not None else load_bundled_default_layout(),
            agent_store,
            block_definitions,
        )

    @property
    def vbot_root(self) -> Path:
        """vBot installation or source root this prompt manager was built with."""
        return self._vbot_root

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
        self._catalog.definitions = tuple(block_definitions)
        self._loaded_extensions = frozenset(loaded_extensions)

    def build_system_prompt(
        self,
        agent: PromptAgent,
        scope: Any = None,
        *,
        agent_body: str = "",
        project_context: ProjectPromptContext | None = None,
        working_project_context: str | None = None,
        soul_context: str | None = None,
        memory_files_context: str | None = None,
        agent_project_id: str | None = None,
        nesting_depth: int = 0,
        skill_registry: SkillPromptRegistry | None = None,
        skill_catalog: PinnedSkillCatalog | None = None,
        read_paths: list[Path] | None = None,
        effective_tool_names: Sequence[str] | None = None,
        session_tool_grants: Sequence[str] = (),
        request_block_definitions: Sequence[BlockDefinition] = (),
        block_details: list[JsonObject] | None = None,
    ) -> str:
        """Build the complete system prompt for an agent (the block-model path).

        Collects the full block-definition list for this agent/run (the core text
        blocks built from the prompt resources, the SOUL / Working Project /
        agent-body data blocks, and the contributed memory / tool / extension
        blocks), reads the active scope's layout + overrides, and routes everything
        through the deterministic assembly engine.

        ``agent_body`` is a config agent's imported prompt body, inserted verbatim
        through the ``core:agent_body`` data block (empty for identity agents →
        collapses). ``project_context`` carries Project identity, Workspace, and
        auto-load files for the live ``core:working_project`` render (``None`` off a
        Project → collapses). ``working_project_context`` is the already-rendered,
        prompt-epoch replacement used by Rooted Identity Agents and Project Config
        Agents; when set it wins over ``project_context`` so Working Project files
        are not read again. ``soul_context`` and ``memory_files_context`` are the
        matching prompt-epoch replacements for the SOUL block and the pinned-memory
        producer; when set they emit verbatim instead of re-reading the workspace
        files. Chat replaces all three after Compaction. ``skill_registry`` overrides
        the registry the skills block is filtered
        against — a project run passes its project-scoped registry; ``None`` uses the
        configured global one (identity runs, unchanged). ``skill_catalog`` is a
        prompt-epoch snapshot: when present the skills block renders its frozen text
        instead of re-filtering the registry. Chat replaces it after Compaction.

        ``agent_project_id`` is the Agent's addressing scope for contributed blocks.
        It stays separate from ``project_context`` because a Rooted Identity Agent
        may receive files from its working Project while remaining identity-scoped.

        ``read_paths``, when a list is passed, is filled with the resolved absolute
        path of every prompt file whose content actually reached the assembled prompt
        (a workspace ``{include:…}``, SOUL, the project auto-load files, the on-disk
        pinned-memory files). The chat loop stamps those as read-before-write so the
        agent can edit a file it was auto-shown without a separate read call. The
        assembled string is byte-for-byte identical whether or not a list is passed,
        so preview and the prompt cache are unaffected.
        """
        prompt_scope = self._resolve_build_scope(agent, scope)
        scope_key = self._catalog.scope_key(prompt_scope)
        observer: Callable[[Path], None] | None = (
            read_paths.append if read_paths is not None else None
        )
        context = BlockRenderContext(
            agent=agent,
            project_context=project_context,
            working_project_context=working_project_context,
            soul_context=soul_context,
            memory_files_context=memory_files_context,
            agent_project_id=agent_project_id,
            nesting_depth=nesting_depth,
            scope=scope_key,
            read_observer=observer,
        )
        producers = self._build_producers(
            agent,
            skill_registry,
            skill_catalog,
            effective_tool_names=effective_tool_names,
            session_tool_grants=session_tool_grants,
        )
        layout = self._catalog.resolve_layout(scope_key)
        definitions = self._collect_block_definitions(
            agent,
            prompt_scope,
            agent_body=agent_body,
            layout=layout,
            request_block_definitions=request_block_definitions,
        )
        selected_blocks = getattr(agent, "prompt_blocks", None)
        if selected_blocks is not None:
            # An explicit per-participant selection overrides shared enablement.
            # Every unselected contribution stays off, including future additions.
            layout = [
                LayoutEntry(
                    id=block.definition.id,
                    enabled=block.definition.id in selected_blocks,
                    source=block.definition.source,
                )
                for block in resolve_layout(definitions, layout)
            ]
        return assemble_system_prompt(
            definitions,
            layout,
            context,
            owner_activity=CallableOwnerActivity(
                lambda owner, owner_agent: self._is_owner_active(
                    owner,
                    owner_agent,
                    effective_tool_names=effective_tool_names,
                    session_tool_grants=session_tool_grants,
                )
            ),
            override_resolver=self._catalog.override_resolver(prompt_scope),
            producers=producers,
            replacements=self._runtime_replacements(agent),
            block_details=block_details,
        )

    async def build_system_prompt_async(
        self,
        agent: PromptAgent,
        scope: Any = None,
        **build_options: Any,
    ) -> str:
        """Build the prompt through the bounded prompt worker boundary."""
        return await _PROMPT_WORKERS.run(
            self.build_system_prompt,
            agent,
            scope,
            **build_options,
        )

    def _collect_block_definitions(
        self,
        agent: PromptAgent,
        prompt_scope: PromptScope,
        *,
        agent_body: str,
        layout: Sequence[LayoutEntry] = (),
        request_block_definitions: Sequence[BlockDefinition] = (),
    ) -> list[BlockDefinition]:
        """Build the full ordered-agnostic block-definition list for one build.

        The core text blocks (runtime/identity-runtime/tools/channels/skills) carry their default
        text from the active scope's prompt resources; the data blocks (SOUL,
        Working Project, agent body) carry their per-run content. The contributed
        blocks (memory, tools, extensions) are merged in next, and the user's own
        custom blocks last — assembly dedupes by id (first wins), so a core block
        can never be shadowed by a contributor or a custom block. A custom
        ``user:`` block has no contributor definition; it is synthesized from the
        scope's *layout* (its existence is layout entry + override file, T1) so it
        renders its override text.
        """
        definitions = [
            *self._catalog.core_text_definitions(prompt_scope, agent.id),
            memory_block_definition(),
            *self._data_block_definitions(agent_body=agent_body),
            *self._catalog.definitions,
            *request_block_definitions,
            *self._catalog.custom_definitions(layout),
        ]
        return definitions

    def _data_block_definitions(self, *, agent_body: str) -> list[BlockDefinition]:
        """Return the SOUL / Working Project / agent-body data blocks (D2).

        All three are ``kind="data"`` (positionable, not editable) and owner
        ``always``; each collapses to nothing when its content is empty (gate 3):

        - ``core:soul`` renders the workspace ``SOUL.md`` via a ``render`` that uses
          the same ``{include:SOUL.md}`` expansion as before — empty when the file
          is missing or the workspace is ``""`` (a config agent).
        - ``core:working_project`` renders the file-backed Project identity,
          Workspace, and auto-load-file template; empty only without a Project.
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
                id=CORE_WORKING_PROJECT_BLOCK_ID,
                owner=BLOCK_OWNER_ALWAYS,
                kind=BLOCK_KIND_DATA,
                render=self._render_working_project_block,
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

        Reuses the single ``{include:…}`` expansion path so fail-soft behavior
        (missing/unreadable → dropped, unsafe path → ``PromptError``, empty workspace
        → no read) never drifts from a normal include. The context's read observer
        (if any) is threaded through so an inlined SOUL.md is stamped as
        read-before-write. A session-pinned ``soul_context`` wins verbatim so the
        prompt cache stays stable between Compactions.

        When SOUL is present, ``SOUL_FRAMING`` is prefixed so the model reads the
        identity/persona text as its core operating contract rather than as neutral
        file content. An absent/empty SOUL renders to ``""`` (no framing), so the
        block still gates out for a config agent with no workspace.
        """
        if context.soul_context is not None:
            return context.soul_context
        return self.render_soul(context.agent, on_read=context.read_observer)

    def render_soul(
        self, agent: PromptAgent, *, on_read: Callable[[Path], None] | None = None
    ) -> str:
        """Render the SOUL block text from the agent's workspace ``SOUL.md``.

        The single live render path shared by the block and by Chat's prompt-epoch
        snapshot: fail-soft include expansion plus the ``SOUL_FRAMING`` prefix,
        empty when the file is missing or the workspace is empty.
        """
        rendered = expand_workspace_includes(SOUL_INCLUDE_MARKER, agent.workspace, on_read=on_read)
        if not rendered:
            return ""
        return f"{SOUL_FRAMING}\n\n{rendered}"

    def _render_working_project_block(self, context: BlockRenderContext) -> str:
        """Render the file-backed ``core:working_project`` data block."""
        if context.working_project_context is not None:
            return context.working_project_context
        if context.project_context is None:
            return ""
        return self.render_working_project_context(
            context.project_context,
            on_read=context.read_observer,
        )

    def render_working_project_context(
        self,
        project_context: ProjectPromptContext,
        *,
        on_read: Callable[[Path], None] | None = None,
    ) -> str:
        """Render the complete Working Project block from its resource template."""
        project_files: list[str] = []
        for name in project_context.auto_load:
            block = self._read_project_file_block(
                project_context.cwd,
                name,
                on_read=on_read,
            )
            if block is not None:
                project_files.append(_indent_project_file_frame(block))

        template = self._storage.read_prompt_fragment("working_project.md")
        return apply_replacements(
            template,
            {
                "{project_id}": project_context.project_id,
                "{project_name}": " ".join(project_context.project_name.split()),
                "{project_workspace}": model_path(project_context.cwd),
                "{project_files}": "\n\n".join(project_files),
            },
        ).strip()

    async def render_working_project_context_async(
        self,
        project_context: ProjectPromptContext,
        *,
        on_read: Callable[[Path], None] | None = None,
    ) -> str:
        """Render Working Project files through the prompt worker boundary."""
        return await _PROMPT_WORKERS.run(
            self.render_working_project_context,
            project_context,
            on_read=on_read,
        )

    def _runtime_replacements(self, agent: PromptAgent) -> dict[str, str]:
        """Return the build-time runtime-variable substitutions.

        Applied to every text block by the engine. The shared Runtime and Identity
        Environment resources carry these placeholders; treating them as build-time
        globals keeps replacement behavior uniform across resource-backed text blocks.
        """
        thinking_effort = agent.thinking_effort or "provider default"
        return {
            "{server_hostname}": self._server_hostname or socket.gethostname(),
            "{vbot_version}": self._vbot_version,
            "{operating_system}": self._operating_system or platform.platform(),
            "{model}": agent.model,
            "{identity_workspace}": model_path(agent.workspace) if agent.workspace else "",
            "{vbot_root}": model_path(self._vbot_root.resolve()),
            "{data_root}": model_path(self._data_root.resolve()),
            "{thinking_effort}": thinking_effort,
            "{current_local_date}": self._resolve_current_local_date(),
            "{timezone}": self._timezone_name(),
        }

    def _resolve_current_local_date(self) -> str:
        if self._current_local_date is not None:
            return self._current_local_date()
        return _current_local_date(self._timezone_name())

    def render_project_files(
        self,
        project_context: ProjectPromptContext | None,
        *,
        on_read: Callable[[Path], None] | None = None,
    ) -> str:
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

        ``on_read``, when given, is called with the resolved absolute path of every
        file actually inlined, so the caller can stamp it as read-before-write —
        used both for prompt assembly and the explicit ``project`` Tool.

        The Working Project renderer and this explicit-Tool renderer share
        ``_read_project_file_block`` so path and fail-soft behavior cannot drift.
        """
        if project_context is None:
            return ""

        blocks: list[str] = []
        for name in project_context.auto_load:
            block = self._read_project_file_block(project_context.cwd, name, on_read=on_read)
            if block is not None:
                blocks.append(block)
        return "\n".join(blocks)

    def render_project_skills(
        self, project_name: str, skills: Sequence[ProjectContextSkill]
    ) -> str:
        """Render a Project's Skills in its explicit Tool-result context section.

        The current prompt-epoch System Prompt catalog remains unchanged. The explicit
        Project Context names each Skill and tells the Identity Agent to activate it
        through the ordinary ``skill`` Tool, whose Run-local resolver follows the
        latest successful Project Tool Result. Returns ``""`` when the Project has
        no Skills, so no empty section is emitted.
        """
        if not skills:
            return ""
        lines = [f"Skills from project '{project_name}' — load one by name with the `skill` Tool:"]
        lines.extend(
            f"- {skill.name}: {skill.description}"
            for skill in sorted(skills, key=lambda item: item.name)
        )
        return "\n".join(lines)

    def _read_project_file_block(
        self, cwd: Path, filename: str, *, on_read: Callable[[Path], None] | None = None
    ) -> str | None:
        """Read one project auto-load file and wrap it, or ``None`` when absent.

        The path is used **as the user wrote it** in the project's auto-load list:
        a relative path resolves against the project ``cwd`` at any subfolder depth,
        an absolute path is read as-is. There is deliberately **no location
        restriction** — the auto-load list is the user's own config naming the
        user's own files, so where a file lives is not vBot's business (project
        philosophy: maximum agency, minimal restrictions). A missing file is skipped
        silently (lazy rendering); an unreadable file raises, matching ``{include}``.
        The ``<file>`` wrap is shared with ``{include}`` so framing cannot drift.

        ``on_read``, when given, is called with the file's resolved absolute path
        only when its content is actually inlined (never for a missing/unreadable
        one), so the caller can stamp it as read-before-write.
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
        if on_read is not None:
            on_read(file_path.resolve())
        return wrap_include_file(model_path(filename), content)

    def render_skill_catalog(
        self,
        agent: PromptAgent,
        skill_registry: SkillPromptRegistry | None = None,
    ) -> PinnedSkillCatalog:
        """Render an agent's current skill catalog snapshot (the ``<available_skills>`` text).

        Chat pins this on the first build of a Session or Context epoch and reuses it
        until successful Compaction. ``None`` uses the configured global registry.
        """
        registry = self._resolve_skill_registry(skill_registry)
        skills = registry.filter_allowed(agent.allowed_skills)
        return PinnedSkillCatalog(catalog_text=_format_skill_catalog(skills))

    def provider_tool_definitions(
        self,
        agent: PromptAgent,
        *,
        session_tool_grants: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Return provider tool definitions filtered by the agent allowlist.

        ``skill`` and ``skill_manage`` are ordinary allow-list tools, so they are
        offered exactly when the agent allows them — toggleable per agent like any
        tool. The only extra rule is identity-only visibility (applied inside
        :meth:`_provider_definitions_for_agent`): ``skill_manage`` writes to the
        agent's own private skill home, so it is withheld from a config/project agent
        (empty ``workspace``) even under a wildcard allow-list.
        """
        return self._provider_definitions_for_agent(agent, session_tool_grants)

    async def provider_tool_definitions_async(
        self,
        agent: PromptAgent,
        *,
        session_tool_grants: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Build provider Tool schemas without running profile work on the Event Loop."""
        return await _PROMPT_WORKERS.run(
            self.provider_tool_definitions,
            agent,
            session_tool_grants=session_tool_grants,
        )

    def _resolve_skill_registry(
        self, skill_registry: SkillPromptRegistry | None
    ) -> SkillPromptRegistry:
        """Return the per-call registry, or the configured global one when absent."""
        return skill_registry if skill_registry is not None else self._skill_registry

    def _build_producers(
        self,
        agent: PromptAgent,
        skill_registry: SkillPromptRegistry | None,
        skill_catalog: PinnedSkillCatalog | None = None,
        *,
        effective_tool_names: Sequence[str] | None = None,
        session_tool_grants: Sequence[str] = (),
    ) -> dict[str, BlockProducer]:
        """Build the ``{generated:NAME}`` producer registry for this build.

        Each producer is a closure over the registries the manager already holds
        (and the per-call skill-registry override), so the list-formatting logic
        lives in one place. ``tool_list``/``channel_list``/``skill_catalog`` feed the
        core tools/channels/skills blocks; ``memory_files`` renders the
        ``USER.md``/``MEMORY.md`` ``<file>`` contents per the agent's memory mode
        (the embedded data half of the ``memory:guidance`` block — the file reading
        itself lives in the memory domain's :func:`read_memory_files`). When a
        prompt-epoch ``skill_catalog`` is given, its producer returns the frozen
        text instead of re-filtering the live registry.
        """
        active_skill_registry = self._resolve_skill_registry(skill_registry)

        def tool_list(context: BlockRenderContext) -> str:
            return _format_tool_list(
                self._prompt_definitions_for_agent(
                    context.agent,
                    session_tool_grants,
                    effective_tool_names=effective_tool_names,
                )
            )

        def channel_list(context: BlockRenderContext) -> str:
            return _format_channel_list(self._agent_enabled_channels(context.agent))

        def skill_catalog_text(context: BlockRenderContext) -> str:
            if skill_catalog is not None:
                return skill_catalog.catalog_text
            return _format_skill_catalog(
                active_skill_registry.filter_allowed(context.agent.allowed_skills)
            )

        def memory_files(context: BlockRenderContext) -> str:
            if context.memory_files_context is not None:
                return context.memory_files_context
            return self.render_memory_files(context.agent, on_read=context.read_observer)

        return {
            "tool_list": tool_list,
            "channel_list": channel_list,
            "skill_catalog": skill_catalog_text,
            MEMORY_FILES_PRODUCER_NAME: memory_files,
        }

    def render_memory_files(
        self, agent: PromptAgent, *, on_read: Callable[[Path], None] | None = None
    ) -> str:
        """Render the pinned-memory text for the agent's memory prompt mode.

        The single live render path shared by the ``memory_files`` producer and by
        Chat's prompt-epoch snapshot. When *on_read* is given, every on-disk memory
        file whose content reaches the render is reported so the agent can edit a
        file it was auto-shown (see ``memory_prompt_file_paths``); an empty
        workspace (a config agent) reports nothing and must never resolve against
        ``Path(".")``.
        """
        mode = getattr(agent, "memory_prompt_mode", DEFAULT_MEMORY_PROMPT_MODE)
        workspace = agent.workspace
        rendered = read_memory_files(Path(workspace), mode, provider=self._memory_provider)
        if on_read is not None and workspace:
            for path in memory_prompt_file_paths(Path(workspace), mode):
                on_read(path)
        return rendered

    def _is_owner_active(
        self,
        owner: str,
        agent: PromptAgent,
        *,
        effective_tool_names: Sequence[str] | None = None,
        session_tool_grants: Sequence[str] = (),
    ) -> bool:
        """Return whether a block's owner is active for *agent* (gate 2, D5).

        Reads the same seams the manager already applies — never a hardcoded or
        re-implemented gate:

        - ``always`` → always true.
        - ``identity`` → the Agent has an Identity/Memory Workspace.
        - ``memory`` → the memory tool is enabled for the agent.
        - ``tool:<name>`` → ``<name>`` is in the agent's effective allowed tools.
        - ``channel`` → the agent has at least one enabled Channel config.
        - ``extension:<name>`` → the extension is in the loaded-extension set the
          runtime rebuilds and injects on every extension (re)load.
        """
        if owner == "always":
            return True
        if owner == BLOCK_OWNER_IDENTITY:
            return bool(agent.workspace)
        if owner == "memory":
            mode = getattr(agent, "memory_prompt_mode", DEFAULT_MEMORY_PROMPT_MODE)
            return memory_tool_enabled(mode)
        if owner == "channel":
            return bool(self._agent_enabled_channels(agent))
        tool_prefix = "tool:"
        if owner.startswith(tool_prefix):
            tool_name = owner[len(tool_prefix) :]
            return self._agent_tool_allowed(
                agent,
                tool_name,
                effective_tool_names=effective_tool_names,
                session_tool_grants=session_tool_grants,
            )
        extension_prefix = "extension:"
        if owner.startswith(extension_prefix):
            return owner[len(extension_prefix) :] in self._loaded_extensions
        return False

    def _agent_tool_allowed(
        self,
        agent: PromptAgent,
        tool_name: str,
        *,
        effective_tool_names: Sequence[str] | None = None,
        session_tool_grants: Sequence[str] = (),
    ) -> bool:
        """Return whether *tool_name* is in the agent's effective prompt tool set.

        Reuses the same prompt-definition path the tools block uses (allowlist +
        derived ``memory`` visibility), so gate 2 cannot drift from what the tool
        list actually shows.
        """
        definitions = self._prompt_definitions_for_agent(
            agent,
            session_tool_grants,
            effective_tool_names=effective_tool_names,
        )
        return any(definition.get("name") == tool_name for definition in definitions)

    def _provider_definitions_for_agent(
        self,
        agent: PromptAgent,
        session_tool_grants: Sequence[str] = (),
    ) -> list[JsonObject]:
        profile_context = ToolDefinitionProfileContext(
            agent_id=agent.id, project_id=getattr(agent, "project_id", None)
        )
        resolution = resolve_tool_access(
            agent.tool_access,
            self._tool_registry.list_tools(),
            agent.memory_prompt_mode,
            workspace=agent.workspace,
            session_tool_grants=session_tool_grants,
        )
        definitions = self._tool_registry.provider_definitions(
            resolution.allowed_tools,
            session_grants=resolution.session_tool_grants,
            profile_context=profile_context,
        )
        return apply_agent_target_tool_visibility(
            definitions,
            agent_id=agent.id,
            allowed_agents=subagent_allowed_agents(
                agent_tool_settings(getattr(agent, "tools", {}))
            ),
        )

    def _prompt_definitions_for_agent(
        self,
        agent: PromptAgent,
        session_tool_grants: Sequence[str] = (),
        *,
        effective_tool_names: Sequence[str] | None = None,
    ) -> list[JsonObject]:
        profile_context = ToolDefinitionProfileContext(
            agent_id=agent.id, project_id=getattr(agent, "project_id", None)
        )
        if effective_tool_names is not None:
            return self._tool_registry.prompt_definitions(
                effective_tool_names,
                session_grants=session_tool_grants,
                profile_context=profile_context,
            )
        resolution = resolve_tool_access(
            agent.tool_access,
            self._tool_registry.list_tools(),
            agent.memory_prompt_mode,
            workspace=agent.workspace,
            session_tool_grants=session_tool_grants,
        )
        definitions = self._tool_registry.prompt_definitions(
            resolution.allowed_tools,
            session_grants=resolution.session_tool_grants,
            profile_context=profile_context,
        )
        return apply_agent_target_tool_visibility(
            definitions,
            agent_id=agent.id,
            allowed_agents=subagent_allowed_agents(
                agent_tool_settings(getattr(agent, "tools", {}))
            ),
        )

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

    def _agent_enabled_channels(self, agent: PromptAgent) -> list[ChannelPromptMetadata]:
        channel_registry = self._channel_registry
        if channel_registry is None:
            return []

        enabled_channels: list[ChannelPromptMetadata] = []
        for channel in channel_registry.list_channels():
            if channel.agent_id != agent.id:
                continue
            if not channel.enabled:
                continue
            enabled_channels.append(channel)
        return enabled_channels

    def list_scopes(self) -> list[JsonObject]:
        """Return prompt scopes available to the System Prompt editor."""
        return self._catalog.list_scopes()

    def validate_scope(self, scope: Any = None) -> PromptScope:
        """Resolve and validate a public prompt scope payload (RPC edge helper)."""
        return self._catalog.validate_scope(scope)

    def list_blocks(self, scope: Any = None) -> list[JsonObject]:
        """Return per-block static metadata for a scope, in layout order (D3)."""
        return self._catalog.list_blocks(scope)

    def update_block(self, block_id: str, content: str, scope: Any = None) -> JsonObject:
        """Write a block's text override and return its new state (T6 autosave)."""
        return self._catalog.update_block(block_id, content, scope)

    def reset_block(self, block_id: str, scope: Any = None) -> JsonObject:
        """Reset a block to its inherited/default text (T5 "reset → inherited")."""
        return self._catalog.reset_block(block_id, scope)

    def set_layout(self, layout: Sequence[Mapping[str, Any]], scope: Any = None) -> JsonObject:
        """Persist a scope's order + on/off, tolerating a contributor-gone id (T6)."""
        return self._catalog.set_layout(layout, scope)

    def create_block(
        self,
        slug: str,
        content: str | None = None,
        scope: Any = None,
        *,
        position: int | None = None,
    ) -> JsonObject:
        """Create a custom ``user:<slug>`` text block (T1): override file + layout entry."""
        return self._catalog.create_block(slug, content, scope, position=position)

    def remove_block(self, block_id: str, scope: Any = None) -> JsonObject:
        """Delete a custom ``user:`` block: drop the override file + the layout entry."""
        return self._catalog.remove_block(block_id, scope)

    def reset_layout(self, scope: Any = None) -> JsonObject:
        """Reset a scope's layout (order + on/off) to the bundled default (T6)."""
        return self._catalog.reset_layout(scope)


def _current_local_date(timezone_name: str) -> str:
    return datetime.now(UTC).astimezone(ZoneInfo(timezone_name)).date().isoformat()


def _indent_project_file_frame(block: str) -> str:
    """Indent only a Project file's wrapper tags, preserving its content exactly."""
    opening, separator, remainder = block.partition("\n")
    if not separator:
        return f" {block}"
    content, closing_separator, closing = remainder.rpartition("\n")
    if not closing_separator:
        return f" {opening}\n{remainder}"
    return f" {opening}\n{content}\n {closing}"
