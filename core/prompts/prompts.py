"""System Prompt assembly and prompt-epoch rendering."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from core.memory import (
    MemoryService,
    memory_block_definition,
)
from core.prompts._catalog import (
    PromptBlockCatalog,
    _normalize_prompt_scope,
    _require_scope_agent_id,
    load_bundled_default_layout,
)
from core.prompts._context import (
    project_memory_visible,
    render_memory_files,
    render_project_files,
    render_project_skills,
    render_soul,
    render_working_project,
)
from core.prompts._formatting import (
    _channel_line,
    _format_skill_catalog,
)
from core.prompts._render_inputs import PromptRenderInputs
from core.prompts._tool_policy import PromptToolPolicy, _tool_names
from core.prompts._types import (
    AGENT_SCOPE_KEY_PREFIX,
    BLOCK_OWNER_ALWAYS,
    BLOCK_OWNER_CHANNEL,
    BLOCK_OWNER_IDENTITY,
    BLOCK_OWNER_SKILL_MANAGE,
    BLOCK_OWNER_SUBAGENT_SESSION,
    CORE_AGENT_BODY_BLOCK_ID,
    CORE_CHANNELS_BLOCK_ID,
    CORE_IDENTITY_RUNTIME_BLOCK_ID,
    CORE_RUNTIME_BLOCK_ID,
    CORE_SKILL_MAINTENANCE_BLOCK_ID,
    CORE_SKILLS_BLOCK_ID,
    CORE_SOUL_BLOCK_ID,
    CORE_SUBAGENT_ROLE_BLOCK_ID,
    CORE_SYSTEM_REMINDERS_BLOCK_ID,
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
    BlockCatalog,
    BlockDefinition,
    BlockRenderContext,
    BlockStore,
    CallableOwnerActivity,
    EmptyBlockStore,
    LayoutEntry,
    OwnerActivity,
    PromptError,
    PromptFileIssue,
    RenderedBlock,
    ToolAvailability,
    assemble_system_prompt,
    render_dynamic_block,
    resolve_block_text,
    resolve_layout,
)
from core.tools.file_state import FileRead
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

__all__ = [
    "AGENT_SCOPE_KEY_PREFIX",
    "BLOCK_OWNER_ALWAYS",
    "BLOCK_OWNER_CHANNEL",
    "BLOCK_OWNER_IDENTITY",
    "BLOCK_OWNER_SUBAGENT_SESSION",
    "BLOCK_OWNER_SKILL_MANAGE",
    "CORE_AGENT_BODY_BLOCK_ID",
    "CORE_CHANNELS_BLOCK_ID",
    "CORE_IDENTITY_RUNTIME_BLOCK_ID",
    "CORE_RUNTIME_BLOCK_ID",
    "CORE_SKILLS_BLOCK_ID",
    "CORE_SKILL_MAINTENANCE_BLOCK_ID",
    "CORE_SOUL_BLOCK_ID",
    "CORE_SUBAGENT_ROLE_BLOCK_ID",
    "CORE_SYSTEM_REMINDERS_BLOCK_ID",
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
    "project_memory_visible",
    "ToolPromptRegistry",
    "USER_BLOCK_ID_PREFIX",
    "USER_BLOCK_SOURCE",
    "load_bundled_default_layout",
]

PROMPT_WORKER_LIMIT = 4
_LOGGER = get_logger("prompts")
# Names the Channels ``core:channels`` lists when a change is told entry by entry:
# "{title} changed since your System Prompt listed them."
CHANNEL_CATALOG_TITLE = "Your configured channels"

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
        self._tool_policy = PromptToolPolicy(tool_registry, agent_store)
        self._inputs = PromptRenderInputs(
            self._tool_policy,
            skill_registry,
            channel_registry,
            memory_provider or MemoryService(),
            vbot_version=vbot_version,
            vbot_root=Path(vbot_root),
            data_root=Path(data_root),
            server_hostname=server_hostname,
            operating_system=operating_system,
            current_local_date=current_local_date,
            timezone_name=timezone_name or (lambda: "UTC"),
            loaded_extensions=loaded_extensions,
        )
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
        return self._inputs._vbot_root

    def update_skill_registry(self, skill_registry: SkillPromptRegistry) -> None:
        """Replace the skill registry used for prompt and provider tool decisions."""
        self._inputs._skill_registry = skill_registry

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
        self._inputs._loaded_extensions = frozenset(loaded_extensions)

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
        subagent_session: bool = False,
        skill_registry: SkillPromptRegistry | None = None,
        skill_catalog: PinnedSkillCatalog | None = None,
        read_paths: list[FileRead] | None = None,
        effective_tool_definitions: Sequence[Mapping[str, Any]] | None = None,
        on_demand_tools: Sequence[tuple[str, str]] | None = None,
        session_tool_grants: Sequence[str] = (),
        pinned_blocks: Mapping[str, str] | None = None,
        pinned_block_order: Sequence[str] = (),
        request_block_definitions: Sequence[BlockDefinition] = (),
        block_details: list[JsonObject] | None = None,
        thinking_effort: str | None = None,
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
        prompt-epoch replacement used by Identity Sessions working in a Project and
        by Project Config Agents; when set it wins over ``project_context`` so Working Project files
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
        It stays separate from ``project_context`` because an Identity Session may
        receive files from its working Project while remaining identity-scoped.

        ``read_paths``, when a list is passed, receives the resolved path and pre-read
        metadata of every prompt file whose content reached the assembled prompt
        (a workspace ``{include:…}``, SOUL, the project auto-load files, the on-disk
        pinned-memory files). The chat loop stamps those as read-before-write so the
        agent can edit a file it was auto-shown without a separate read call. The
        assembled string is byte-for-byte identical whether or not a list is passed,
        so preview and the prompt cache are unaffected.

        ``effective_tool_definitions`` are the exact Provider Tool definitions the
        request lists (Chat passes its prompt epoch's pinned Tool list). When given,
        a ``tool:<name>`` block's owner is active exactly when ``<name>`` is among
        them, and the ``tool_list`` producer renders their names and descriptions,
        so neither changes while the definitions stay the same: readiness and
        registry changes are not consulted. ``None`` evaluates the Agent's live
        Tool policy with ``session_tool_grants`` instead (previews). The same Tool
        set decides whether a notice that stands in for an oversized file or a cut
        Memory section names the Tool that shows the rest.

        ``on_demand_tools`` are the ``(registry name, summary)`` pairs of the
        Agent's On-demand Tools (Chat passes its prompt epoch's pinned list); the
        ``on_demand_tool_list`` producer lists them for the ``tool:load_tools``
        block, and they count as available Tools for gate 2 and
        ``tool_available`` because the Agent can call them. ``None`` with
        ``effective_tool_definitions`` means none; ``None`` without them
        classifies the Agent's live Tools (``core.tools.on_demand``), and the
        live ``tool_list`` leaves them out.

        ``pinned_blocks`` maps the ids of Tool and Extension dynamic blocks and of
        ``core:channels`` and temporary Session blocks to their epoch texts.
        These blocks keep their pinned membership even if their contributor is
        removed; newly contributed dynamic blocks wait for the next epoch.
        ``pinned_block_order`` keeps their position among the other blocks.

        ``thinking_effort`` is the effort the runtime block names (Chat passes its
        prompt epoch's pinned effort, ``""`` for the provider default); ``None``
        names the Agent's own (previews).
        """
        prompt_scope = self._resolve_build_scope(agent, scope)
        scope_key = self._catalog.scope_key(prompt_scope)
        on_demand = self._tool_policy.on_demand_entries(
            agent,
            effective_tool_definitions=effective_tool_definitions,
            on_demand_tools=on_demand_tools,
            session_tool_grants=session_tool_grants,
        )
        effective_tool_names = _tool_names(effective_tool_definitions, on_demand)
        context = self._render_context(
            agent,
            scope_key,
            project_context=project_context,
            working_project_context=working_project_context,
            soul_context=soul_context,
            memory_files_context=memory_files_context,
            agent_project_id=agent_project_id,
            subagent_session=subagent_session,
            effective_tool_names=effective_tool_names,
            session_tool_grants=session_tool_grants,
            read_observer=read_paths.append if read_paths is not None else None,
        )
        producers = self._inputs._build_producers(
            agent,
            skill_registry,
            skill_catalog,
            effective_tool_definitions=effective_tool_definitions,
            on_demand=on_demand,
            session_tool_grants=session_tool_grants,
        )
        layout = self._catalog.resolve_layout(scope_key)
        definitions = self._collect_block_definitions(
            agent,
            prompt_scope,
            agent_body=agent_body,
            layout=layout,
            pinned_blocks=pinned_blocks,
            request_block_definitions=request_block_definitions,
        )
        layout = _selected_layout(agent, definitions, layout)
        if pinned_blocks is not None:
            resolved = {block.definition.id: block for block in resolve_layout(definitions, layout)}
            order = dict.fromkeys([*pinned_block_order, *resolved])
            layout = [
                LayoutEntry(
                    id=block_id, enabled=block_id in pinned_blocks or resolved[block_id].enabled
                )
                for block_id in order
                if block_id in resolved
            ]
        return assemble_system_prompt(
            definitions,
            layout,
            context,
            owner_activity=self._owner_activity(
                effective_tool_names,
                session_tool_grants,
                subagent_session=subagent_session,
                project_context=project_context,
            ),
            override_resolver=self._catalog.override_resolver(prompt_scope),
            producers=producers,
            replacements=self._inputs._runtime_replacements(agent, thinking_effort),
            block_details=block_details,
        )

    def render_dynamic_blocks(
        self,
        agent: PromptAgent,
        scope: Any = None,
        *,
        block_ids: Collection[str] | None = None,
        skip: Collection[str] = (),
        project_context: ProjectPromptContext | None = None,
        working_project_context: str | None = None,
        soul_context: str | None = None,
        memory_files_context: str | None = None,
        agent_project_id: str | None = None,
        subagent_session: bool = False,
        effective_tool_definitions: Sequence[Mapping[str, Any]] | None = None,
        on_demand_tools: Sequence[tuple[str, str]] | None = None,
        session_tool_grants: Sequence[str] = (),
        skill_registry: SkillPromptRegistry | None = None,
        skill_catalog: PinnedSkillCatalog | None = None,
        thinking_effort: str | None = None,
        request_block_definitions: Sequence[BlockDefinition] = (),
        static_contributions_only: bool = False,
        block_order: list[str] | None = None,
        failed_blocks: set[str] | None = None,
        read_paths: list[FileRead] | None = None,
    ) -> dict[str, RenderedBlock]:
        """Render live the blocks a build with these inputs shows that follow live state.

        Covers every contributed and temporary Session block and ``core:channels``
        when enabled and owner active (gates 1 and 2); only those in *block_ids*
        when given and none in *skip*, keyed by block id. Each renders exactly as
        :meth:`build_system_prompt` renders it without a pinned text; an empty text
        is kept (the block collapses), a failing render is left out and named in
        ``failed_blocks``. ``block_order`` captures the full resolved layout;
        ``read_paths`` records snapshot file reads. Chat pins this inventory and
        compares later renders with it to announce additions, changes and removals.
        ``static_contributions_only`` captures the contributions older pins did
        not cover, without adding newly visible rendered blocks or Channels.
        """
        prompt_scope = self._resolve_build_scope(agent, scope)
        scope_key = self._catalog.scope_key(prompt_scope)
        on_demand = self._tool_policy.on_demand_entries(
            agent,
            effective_tool_definitions=effective_tool_definitions,
            on_demand_tools=on_demand_tools,
            session_tool_grants=session_tool_grants,
        )
        effective_tool_names = _tool_names(effective_tool_definitions, on_demand)
        context = self._render_context(
            agent,
            scope_key,
            project_context=project_context,
            working_project_context=working_project_context,
            soul_context=soul_context,
            memory_files_context=memory_files_context,
            agent_project_id=agent_project_id,
            subagent_session=subagent_session,
            effective_tool_names=effective_tool_names,
            session_tool_grants=session_tool_grants,
            read_observer=read_paths.append if read_paths is not None else None,
        )
        owner_activity = self._owner_activity(
            effective_tool_names,
            session_tool_grants,
            subagent_session=subagent_session,
            project_context=project_context,
        )
        definitions = self._collect_block_definitions(
            agent,
            prompt_scope,
            agent_body="",
            layout=self._catalog.resolve_layout(scope_key),
            request_block_definitions=request_block_definitions,
        )
        dynamic_ids = {
            CORE_CHANNELS_BLOCK_ID,
            *(definition.id for definition in self._catalog.definitions),
            *(definition.id for definition in request_block_definitions),
        }
        layout = _selected_layout(agent, definitions, self._catalog.resolve_layout(scope_key))
        resolved = resolve_layout(definitions, layout)
        if block_order is not None:
            block_order.extend(block.definition.id for block in resolved)
        rendered: dict[str, RenderedBlock] = {}
        producers = self._inputs._build_producers(
            agent,
            skill_registry,
            skill_catalog,
            effective_tool_definitions=effective_tool_definitions,
            on_demand=on_demand,
            session_tool_grants=session_tool_grants,
        )
        for block in resolved:
            definition = block.definition
            if (
                definition.id not in dynamic_ids
                or (
                    static_contributions_only
                    and (definition.render is not None or definition.id == CORE_CHANNELS_BLOCK_ID)
                )
                or not block.enabled
                or definition.id in skip
                or (block_ids is not None and definition.id not in block_ids)
                or not owner_activity.is_owner_active(definition.owner, agent)
            ):
                continue
            if definition.render is None:
                text = resolve_block_text(
                    block,
                    context,
                    override_resolver=self._catalog.override_resolver(prompt_scope),
                    producers=producers,
                    replacements=self._inputs._runtime_replacements(agent, thinking_effort),
                )
                rendered[definition.id] = RenderedBlock(
                    text,
                    self._channel_catalog(definition, agent, prompt_scope, scope_key)
                    if definition.id == CORE_CHANNELS_BLOCK_ID
                    else None,
                )
                continue
            value = render_dynamic_block(definition, context)
            if value is not None:
                rendered[definition.id] = value
            elif failed_blocks is not None:
                failed_blocks.add(definition.id)
        return rendered

    def _channel_catalog(
        self,
        definition: BlockDefinition,
        agent: PromptAgent,
        prompt_scope: PromptScope,
        scope_key: str,
    ) -> BlockCatalog:
        """The Channels ``core:channels`` lists, framed by the block's own text."""
        override = self._catalog.override_resolver(prompt_scope)(definition, scope_key)
        return BlockCatalog(
            title=CHANNEL_CATALOG_TITLE,
            entries=tuple(
                (channel.id, _channel_line(channel))
                for channel in self._inputs._agent_enabled_channels(agent)
            ),
            frame=override if override is not None else definition.default_text or "",
        )

    def _render_context(
        self,
        agent: PromptAgent,
        scope_key: str,
        *,
        project_context: ProjectPromptContext | None,
        working_project_context: str | None,
        soul_context: str | None,
        memory_files_context: str | None,
        agent_project_id: str | None,
        subagent_session: bool,
        effective_tool_names: frozenset[str] | None,
        session_tool_grants: Sequence[str],
        read_observer: Callable[[FileRead], None] | None = None,
    ) -> BlockRenderContext:
        return BlockRenderContext(
            agent=agent,
            project_context=project_context,
            working_project_context=working_project_context,
            soul_context=soul_context,
            memory_files_context=memory_files_context,
            agent_project_id=agent_project_id,
            subagent_session=subagent_session,
            scope=scope_key,
            read_observer=read_observer,
            tool_available=self._tool_policy.tool_availability(
                agent,
                effective_tool_names=effective_tool_names,
                session_tool_grants=session_tool_grants,
            ),
        )

    def _owner_activity(
        self,
        effective_tool_names: frozenset[str] | None,
        session_tool_grants: Sequence[str],
        *,
        subagent_session: bool = False,
        project_context: ProjectPromptContext | None = None,
    ) -> OwnerActivity:
        return CallableOwnerActivity(
            lambda owner, owner_agent: self._inputs._is_owner_active(
                owner,
                owner_agent,
                effective_tool_names=effective_tool_names,
                session_tool_grants=session_tool_grants,
                subagent_session=subagent_session,
                project_context=project_context,
            )
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
        pinned_blocks: Mapping[str, str] | None = None,
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
        renders its override text. When *pinned_blocks* is given, it replaces the
        contributed/temporary inventory and ``core:channels`` with fixed data
        blocks, so current registry membership and gates cannot edit the prefix.
        """
        contributed = self._catalog.definitions
        core = self._catalog.core_text_definitions(prompt_scope, agent.id)
        definitions = [
            *core,
            memory_block_definition(),
            *self._data_block_definitions(agent_body=agent_body),
            *contributed,
            *request_block_definitions,
            *self._catalog.custom_definitions(layout),
        ]
        if pinned_blocks is None:
            return definitions
        dynamic_ids = {
            CORE_CHANNELS_BLOCK_ID,
            *(definition.id for definition in contributed),
            *(definition.id for definition in request_block_definitions),
            *pinned_blocks,
        }
        by_id = {definition.id: definition for definition in definitions}
        return [
            *(definition for definition in definitions if definition.id not in dynamic_ids),
            *(
                BlockDefinition(
                    id=block_id,
                    owner=BLOCK_OWNER_ALWAYS,
                    kind=BLOCK_KIND_DATA,
                    default_text=text,
                    default_rank=by_id[block_id].default_rank if block_id in by_id else 1000,
                )
                for block_id, text in pinned_blocks.items()
            ),
        ]

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
        → no read, oversized → notice) never drifts from a normal include. The
        context's read observer (if any) is threaded through so an inlined SOUL.md
        is stamped as read-before-write. A session-pinned ``soul_context`` wins
        verbatim so the prompt cache stays stable between Compactions.

        When SOUL is present, ``SOUL_FRAMING`` is prefixed so the model reads the
        identity/persona text as its core operating contract rather than as neutral
        file content. An absent/empty SOUL renders to ``""`` (no framing), so the
        block still gates out for a config agent with no workspace.
        """
        if context.soul_context is not None:
            return context.soul_context
        return render_soul(context.agent, context.tool_available, context.read_observer)

    def render_soul(
        self, agent: PromptAgent, *, on_read: Callable[[FileRead], None] | None = None
    ) -> str:
        """Render the SOUL block text from the agent's workspace ``SOUL.md``.

        The single live render path shared by the block and by Chat's prompt-epoch
        snapshot: fail-soft include expansion plus the ``SOUL_FRAMING`` prefix,
        empty when the file is missing or the workspace is empty. An oversized
        ``SOUL.md`` notice names ``read`` only when the Agent's current Tool policy
        offers it.
        """
        return render_soul(agent, self._tool_policy.tool_availability(agent), on_read)

    def _render_working_project_block(self, context: BlockRenderContext) -> str:
        """Render the file-backed ``core:working_project`` data block."""
        if context.working_project_context is not None:
            return context.working_project_context
        if context.project_context is None:
            return ""
        return render_working_project(
            self._storage, context.project_context, context.tool_available, context.read_observer
        )

    def render_working_project_context(
        self,
        agent: PromptAgent,
        project_context: ProjectPromptContext,
        *,
        on_read: Callable[[FileRead], None] | None = None,
    ) -> str:
        """Render the complete Working Project block from its resource template.

        *agent* is the Agent that receives the block: a notice for an oversized
        auto-load file names ``read`` only when its current Tool policy offers it.
        """
        return render_working_project(
            self._storage, project_context, self._tool_policy.tool_availability(agent), on_read
        )

    async def render_working_project_context_async(
        self,
        agent: PromptAgent,
        project_context: ProjectPromptContext,
        *,
        on_read: Callable[[FileRead], None] | None = None,
    ) -> str:
        """Render Working Project files through the prompt worker boundary."""
        return await _PROMPT_WORKERS.run(
            self.render_working_project_context,
            agent,
            project_context,
            on_read=on_read,
        )

    def render_project_files(
        self,
        project_context: ProjectPromptContext | None,
        *,
        tool_available: ToolAvailability,
        on_read: Callable[[FileRead], None] | None = None,
        on_issue: Callable[[PromptFileIssue], None] | None = None,
    ) -> str:
        """Render the project's auto-loaded files as ``<file>``-wrapped blocks.

        The ``auto_load`` files in list order. AGENTS.md is no longer special — it
        is seeded as the first entry at project creation, so the list is the single
        source of what loads. Lazy: returns ``""`` when there is no project context
        or no readable file, so the placeholder collapses. *tool_available* tells
        which Tools the receiving Agent can call; a notice for an oversized file
        names ``read`` only when it can. See :func:`_context._project_file_blocks` for paths
        and :func:`render_prompt_file` for the size limit and fail-soft rules.

        ``on_read``, when given, receives the resolved path and pre-read metadata of every
        file actually inlined, so the caller can stamp it as read-before-write —
        used both for prompt assembly and the explicit ``project`` Tool.
        ``on_issue`` reports omissions so the Tool can explain partial loads.
        """
        return render_project_files(
            project_context, tool_available=tool_available, on_read=on_read, on_issue=on_issue
        )

    def render_project_skills(
        self,
        project_name: str,
        skills: Sequence[ProjectContextSkill],
        *,
        tool_available: ToolAvailability,
    ) -> str:
        """Render a Project's Skills in its explicit Tool-result context section.

        The current prompt-epoch System Prompt catalog remains unchanged. The explicit
        Project Context names each Skill and tells the Identity Agent to activate it
        through the ordinary ``skill`` Tool, whose Run-local resolver follows the
        latest successful Project Tool Result. Returns ``""`` when the Project has
        no Skills, so no empty section is emitted.
        """
        return render_project_skills(project_name, skills, tool_available=tool_available)

    def render_skill_catalog(
        self,
        agent: PromptAgent,
        skill_registry: SkillPromptRegistry | None = None,
    ) -> PinnedSkillCatalog:
        """Render an agent's current skill catalog snapshot (the ``<available_skills>`` text).

        Chat pins this on the first build of a Session or Context epoch and reuses it
        until successful Compaction. ``None`` uses the configured global registry.
        """
        registry = self._inputs._resolve_skill_registry(skill_registry)
        skills = registry.filter_allowed(agent.allowed_skills)
        return PinnedSkillCatalog(catalog_text=_format_skill_catalog(skills))

    def provider_tool_definitions(
        self,
        agent: PromptAgent,
        *,
        session_tool_grants: Sequence[str] = (),
        ready_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Return provider tool definitions filtered by the agent allowlist.

        ``skill`` and ``skill_manage`` are ordinary allow-list tools, so they are
        offered exactly when the agent allows them — toggleable per agent like any
        tool. The only extra rule is identity-only visibility (applied inside
        :class:`PromptToolPolicy`): ``skill_manage`` writes to the
        agent's own private skill home, so it is withheld from a config/project agent
        (empty ``workspace``) even under a wildcard allow-list. ``ready_only=False``
        also returns the allowed Tools that are not ready.
        """
        return self._tool_policy.provider_definitions(
            agent, session_tool_grants, ready_only=ready_only
        )

    async def provider_tool_definitions_async(
        self,
        agent: PromptAgent,
        *,
        session_tool_grants: Sequence[str] = (),
        ready_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Build provider Tool schemas without running profile work on the Event Loop."""
        return await _PROMPT_WORKERS.run(
            self.provider_tool_definitions,
            agent,
            session_tool_grants=session_tool_grants,
            ready_only=ready_only,
        )

    def render_memory_files(
        self,
        agent: PromptAgent,
        *,
        project_context: ProjectPromptContext | None = None,
        on_read: Callable[[FileRead], None] | None = None,
    ) -> str:
        """Render enabled personal and Working Project Memory in one snapshot.

        The single live render path shared by the ``memory_files`` producer and by
        Chat's prompt-epoch snapshot. A section above its Memory budget shows the
        entries that fit plus a notice; that notice names the ``memory`` Tool only
        when the Agent's current Tool policy offers it. When *on_read* is given,
        every on-disk memory file shown whole is reported so the agent can edit a
        file it was auto-shown from that render's captured metadata; an empty
        Identity Workspace never resolves against ``Path(".")``. Shared Project
        Memory uses the explicit Anchor in *project_context*, including for
        Workspace-less Agents.
        """
        return render_memory_files(
            self._inputs._memory_provider,
            agent,
            self._tool_policy.tool_availability(agent),
            on_read,
            project_context,
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


def _selected_layout(
    agent: PromptAgent, definitions: Sequence[BlockDefinition], layout: Sequence[LayoutEntry]
) -> Sequence[LayoutEntry]:
    """Return *layout*, or the Agent's explicit block selection over *definitions*.

    An explicit per-participant selection overrides shared enablement. Every
    unselected contribution stays off, including future additions.
    """
    selected_blocks = getattr(agent, "prompt_blocks", None)
    if selected_blocks is None:
        return layout
    return [
        LayoutEntry(
            id=block.definition.id,
            enabled=block.definition.id in selected_blocks,
            source=block.definition.source,
        )
        for block in resolve_layout(definitions, layout)
    ]
