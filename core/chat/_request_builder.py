"""Pinned prompt, history, media and Model-route request construction."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents import skill_subject_id
from core.attachments.images import ImageConverter
from core.chat._message_history import finalize_checkpoint_guidance
from core.chat._prompt_block_epoch import (
    PromptBlockPin,
    ensure_prompt_block_pin,
    plan_prompt_block_change,
    without_other_epoch_prompt_block_changes,
)
from core.chat._request_history import (
    _prepare_request_messages,
    _request_content_resolution_inputs,
    _restore_in_run_tool_result_content,
)
from core.chat._run_state import RequestBuildInputs, _ModelTarget, _RequestState
from core.chat._tool_epoch import (
    LiveToolCatalog,
    ToolChange,
    ToolEpochPin,
    ToolEpochView,
    definition_source,
    without_other_epoch_tool_changes,
)
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.block_resolver import ContentBlockResolver
from core.chat.content_blocks import MediaBlock, content_block_to_dict
from core.chat.errors import ChatError
from core.chat.events import _close_adapter
from core.chat.messages import (
    ChatMessage,
    JsonObject,
)
from core.chat.model_resolution import (
    _first_usable_connection_id,
    _model_connection_allowlist,
    _model_family,
    _model_family_for_target,
    _model_input_modalities,
    _model_input_modalities_for_target,
    _resolve_agent_connection,
    _split_agent_model,
    parse_model_with_connection,
)
from core.chat.streaming import stream_stall_timeout
from core.chat.usage import latest_session_context_usage
from core.chat.wire_shaping import (
    PINNED_IMAGE_RETIREMENT_SLOT,
    RequestImageBudget,
    _restore_in_run_assistant_reasoning,
    limit_request_images,
)
from core.extensions import invoke_extension_handler
from core.projects import ProjectError
from core.prompts import BLOCK_KIND_DATA, BlockDefinition, RenderedBlock
from core.prompts.pinned_context import (
    PINNED_DYNAMIC_BLOCKS_SLOT,
    PINNED_TOOL_DEFINITIONS_SLOT,
    stamp_prompt_files_read,
)
from core.providers.accounts import DEFAULT_ACCOUNT_ID, ConnectionRef, split_connection_id
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.providers import resolve_effective_context_window
from core.runs import Run
from core.sessions import (
    SKILL_AVAILABLE_NOTE_PREFIX,
    ChatSession,
    SeenSkillsUpdate,
    SessionAddress,
)
from core.tools import (
    ANALYZE_IMAGE_TOOL_NAME,
    SUBAGENT_SESSION_TOOL_NAMES,
    EditDialect,
    Tool,
    ToolAccess,
    ToolContract,
    ToolDefinitionChangeNote,
    ToolNotFoundError,
    edit_dialect,
    known_edit_dialect,
    offer_edit_dialect,
    tool_is_ready,
)
from core.tools.on_demand import LOAD_TOOLS_TOOL_NAME, on_demand_tool_entries, on_demand_tools
from core.tools.terminal import project_terminal_tool_definitions
from core.utils.errors import ConfigError, ProviderError, VBotError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat._run_state import ChatLoopDependencies, _RunExecutionContext
    from core.chat.request_runner import WireRequestRunner
    from core.skills.skills import SkillRegistry

_LOGGER = get_logger("chat")


def _finalize_compaction_checkpoint(
    checkpoint: ChatMessage,
    session_messages: list[ChatMessage],
) -> ChatMessage:
    ordinal = sum(message.role == "compaction_checkpoint" for message in session_messages) + 1
    return finalize_checkpoint_guidance(checkpoint, ordinal=ordinal)


def _resolved_model_reference(
    dependencies: ChatLoopDependencies,
    provider_id: str,
    connection_id: str,
    model_id: str,
) -> str:
    """Return the exact Provider/Model/Connection/Account scope for one request."""

    local_connection_id, explicit_account_id = split_connection_id(provider_id, connection_id)
    resolved_account_id = dependencies.provider_credentials.resolve_account_id(
        provider_id,
        local_connection_id,
        explicit_account_id,
    )
    connection_suffix = local_connection_id
    if resolved_account_id != DEFAULT_ACCOUNT_ID:
        connection_suffix = f"{connection_suffix}:{resolved_account_id}"
    bare_reference = f"{provider_id}/{model_id}"
    return f"{bare_reference}::{connection_suffix}"


def _resolve_image_size_limit(adapter: Any, model_id: str) -> int | None:
    value = adapter.image_size_limit(model_id)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _resolve_request_image_limit(adapter: Any, model_id: str) -> int | None:
    value = adapter.request_image_limit(model_id)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _live_session_tool_grants(
    session_capability: Any | None, *, subagent_session: bool
) -> tuple[str, ...]:
    """Return the session-scoped Tools the Session grants now.

    A temporary Session grants its capability's Tools; a Sub-Agent Session grants
    the Tools a Sub-Agent uses to reach its Parent Agent.
    """
    grants = tuple(session_capability.tool_names) if session_capability is not None else ()
    if subagent_session:
        grants = (*grants, *SUBAGENT_SESSION_TOOL_NAMES)
    return grants


def _fold_prompt_epoch(
    pin: ToolEpochPin, prompt_blocks: PromptBlockPin, session_messages: list[ChatMessage]
) -> tuple[ToolEpochView, list[ChatMessage]]:
    """Return what the Model knows of its Tools and the history without other epochs' notes.

    Both walk the whole Session, so they run on a Chat worker.
    """
    return (
        ToolEpochView.fold(pin, session_messages),
        without_other_epoch_prompt_block_changes(
            without_other_epoch_tool_changes(session_messages, pin.epoch), prompt_blocks.epoch
        ),
    )


SKILL_AVAILABLE_NEW_SKILLS_HEADER = (
    "New skills are now available to you. Load one by name with the `skill` tool when relevant:"
)


@dataclass(frozen=True)
class _SkillAnnouncement:
    """A newly-available-Skill note and the seen-Skill change it commits with."""

    note: str | None = None
    record_seen: SeenSkillsUpdate | None = None


class RequestBuilder:
    """Build Chat requests from canonical history, pinned prompts and Model routes."""

    def __init__(
        self,
        dependencies: ChatLoopDependencies,
        wire_requests: WireRequestRunner,
        attachment_resolver: ContentBlockResolver | None,
    ) -> None:
        self._dependencies = dependencies
        self._wire_requests = wire_requests
        self._attachment_resolver = attachment_resolver
        self._tool_image_converter = ImageConverter()

    async def _apply_project_skill_context(
        self,
        context: _RunExecutionContext,
        project_id: str,
    ) -> None:
        """Make one explicitly loaded Project's Skills active for an Identity Run.

        The Project Tool Result remains the sole persisted context carrier. This
        updates only the Run-local Skill resolver used by activation and Tool
        dispatch; the current prompt-epoch Skill catalog and therefore the System Prompt
        remain unchanged.
        """
        try:
            registry = await _CHAT_TRANSFORM_WORKERS.run(
                self._dependencies.resolve_skills,
                project_id,
                skill_subject_id(context.agent),
            )
        except (ProjectError, OSError) as error:
            _LOGGER.warning(
                "Loaded Project Skill context is unavailable (agent=%s session=%s project=%s): %s",
                context.run.agent_id,
                context.run.session_id,
                project_id,
                error,
            )
            return
        context.skill_project_id = project_id
        context.skill_registry = registry

    def _create_model_target(
        self,
        provider_id: str,
        connection_id: str,
        model_id: str,
        *,
        public_model: str,
    ) -> _ModelTarget:
        connection = ConnectionRef(provider_id, connection_id)
        adapter = self._dependencies.get_adapter(connection)
        return _ModelTarget(
            provider_id=provider_id,
            connection_id=connection_id,
            model_id=model_id,
            model_reference=_resolved_model_reference(
                self._dependencies,
                provider_id,
                connection_id,
                model_id,
            ),
            public_model=public_model,
            adapter=adapter,
            replay_policy=adapter.reasoning_replay_policy(model_id),
            input_modalities=_model_input_modalities_for_target(
                self._dependencies,
                provider_id,
                model_id,
            ),
            wire_media_types=adapter.wire_media_support(model_id),
            chunk_timeout_seconds=stream_stall_timeout(adapter),
            max_image_bytes=self._image_size_limit(adapter, model_id),
            max_request_images=_resolve_request_image_limit(adapter, model_id),
            unlisted_tool_calls=not adapter.list_announced_tools(model_id),
            model_family=_model_family_for_target(self._dependencies, provider_id, model_id),
        )

    def _image_size_limit(self, adapter: Any, model_id: str) -> int | None:
        limit = _resolve_image_size_limit(adapter, model_id)
        if self._attachment_resolver is not None:
            configured = self._attachment_resolver.max_image_bytes
            return min(configured, limit) if limit is not None else configured
        return limit

    def resolve_project_cwd(self, project_id: str | None) -> Path | None:
        """Resolve a working Project cwd, failing closed when unavailable."""
        if project_id is None:
            return None
        cwd = Path(self._dependencies.projects.get(project_id).cwd)
        if not cwd.is_dir():
            raise ChatError(f"Project repository is unavailable: {cwd}")
        return cwd

    @staticmethod
    def available_skill_names(agent: Any, skill_registry: SkillRegistry) -> list[str]:
        """Return the currently advertised Skill names."""
        allowed_skills = getattr(agent, "allowed_skills", None)
        allowed = ["*"] if allowed_skills is None else allowed_skills
        return sorted(str(skill.name) for skill in skill_registry.filter_allowed(allowed))

    def _plan_skill_announcement(
        self,
        address: SessionAddress,
        agent: Any,
        skill_registry: SkillRegistry,
    ) -> _SkillAnnouncement:
        """Plan the note for Skills that became available during this prompt epoch.

        The Session's ``<available_skills>`` block stays pinned between Compactions,
        so a Skill that becomes available mid-epoch does not change the prompt. The
        plan is a one-time ``<system-reminder>`` note for each newly available+allowed
        Skill, leaving the cached prefix untouched. Additions only - a Skill that
        becomes unavailable is not announced. The first Run and every successful
        Compaction seed the baseline from the catalog without announcing it. The diff
        uses the registry already resolved for this Run, so it is an in-memory set
        comparison against one stored value rather than another scan.

        Nothing is written here: the caller commits ``record_seen`` in the same
        transaction as the note, so a Skill is marked seen exactly when its
        announcement persists. A Session without a seen set records the whole
        catalog as its baseline instead.
        """
        available_names = self.available_skill_names(agent, skill_registry)
        allowed_skills = getattr(agent, "allowed_skills", None)
        allowed = ["*"] if allowed_skills is None else allowed_skills
        available = {
            str(skill.name): str(skill.description)
            for skill in skill_registry.filter_allowed(allowed)
        }
        seen = self._dependencies.sessions.seen_skills(address)
        baseline = tuple(available_names)
        if seen is None:
            return _SkillAnnouncement(record_seen=SeenSkillsUpdate(baseline))
        new_names = sorted(set(available) - seen)
        if not new_names:
            return _SkillAnnouncement()
        lines = [SKILL_AVAILABLE_NEW_SKILLS_HEADER]
        lines.extend(f"- {name}: {available[name]}" for name in new_names)
        return _SkillAnnouncement(
            note=SKILL_AVAILABLE_NOTE_PREFIX + "\n".join(lines),
            record_seen=SeenSkillsUpdate(baseline, tuple(new_names)),
        )

    async def plan_prompt_block_change(self, context: _RunExecutionContext) -> str | None:
        """Return the note telling the Model which pinned prompt blocks changed, if any.

        The System Prompt keeps showing the prompt epoch's pinned Tool and Extension
        block texts. This compares a live render of those blocks with what the
        Model knows of them (the pin plus the epoch's earlier notes in the Run's
        snapshot) and returns one note for every change, which the Run persists
        with its input. ``None`` when the Session has no pins yet or nothing changed.
        """
        return await _CHAT_TRANSFORM_WORKERS.run(
            self._plan_prompt_block_change,
            context.agent,
            context.session.address,
            RequestBuildInputs.from_context(context, context.primary_target),
            context.session_snapshot.active_messages,
        )

    def _plan_prompt_block_change(
        self,
        agent: Any,
        address: SessionAddress,
        inputs: RequestBuildInputs,
        messages: list[ChatMessage],
    ) -> str | None:
        sessions = self._dependencies.sessions
        pin = PromptBlockPin.from_payload(sessions.prompt_pin(address, PINNED_DYNAMIC_BLOCKS_SLOT))
        tool_pin = self._stored_tool_epoch_pin(address)
        if pin is None or tool_pin is None or not pin.blocks:
            return None
        live = self._dependencies.get_system_prompts().render_dynamic_blocks(
            agent,
            block_ids=pin.blocks.keys(),
            **self._block_render_inputs(inputs, tool_pin),
        )
        change = plan_prompt_block_change(pin, messages, live)
        return None if change is None else change.note_content()

    def _block_render_inputs(
        self, inputs: RequestBuildInputs, tool_pin: ToolEpochPin
    ) -> dict[str, Any]:
        """The System Prompt inputs dynamic blocks render from, for builds and pins alike."""
        return {
            "project_context": inputs.project_context,
            "working_project_context": inputs.working_project_context,
            "soul_context": inputs.soul_context,
            "memory_files_context": inputs.memory_files_context,
            "agent_project_id": inputs.agent_project_id,
            "subagent_session": inputs.subagent_session,
            "effective_tool_definitions": tool_pin.definitions,
            "on_demand_tools": tool_pin.on_demand,
        }

    async def _prompt_block_pin(
        self,
        agent: Any,
        session: ChatSession,
        render_inputs: Mapping[str, Any],
        *,
        fresh: bool,
    ) -> PromptBlockPin:
        """Return the prompt epoch's dynamic block pin, pinning blocks shown for the first time.

        A *fresh* pin starts a new epoch from the blocks as they render now; the
        caller persists it (Compaction commit).
        """
        system_prompts = self._dependencies.get_system_prompts()

        def render(pinned: Collection[str]) -> Mapping[str, RenderedBlock]:
            return system_prompts.render_dynamic_blocks(agent, skip=pinned, **render_inputs)

        if fresh:
            return PromptBlockPin.start(await _CHAT_TRANSFORM_WORKERS.run(render, ()))
        return await _CHAT_TRANSFORM_WORKERS.run(
            ensure_prompt_block_pin, self._dependencies.sessions, session.address, render
        )

    async def build_request_state(
        self,
        agent: Any,
        session: ChatSession,
        *,
        inputs: RequestBuildInputs,
    ) -> _RequestState:
        # For a project-born session the Working Project context lands in the system
        # prompt; for an identity session working in its Workspace it is empty. The
        # config-agent body is inserted verbatim (never re-expanded) by the builder.
        # ``skill_registry`` scopes the skills block to the project pool (``None`` =
        # the global registry); ``inputs.skill_catalog`` is the current prompt-epoch
        # snapshot the skills block renders from, so only Compaction replaces it. The
        # ``working_project_context`` / ``soul_context`` / ``memory_files_context``
        # prompt-epoch snapshots behave the same way.
        #
        # The Tool list is the prompt epoch's pin: the Session's first request
        # pins the Tools it offers, and every later request sends the same
        # definitions (plus announced additions on routes that must list them).
        # An Agent that loads Tools on demand pins only the Tools its Tool list
        # keeps; the System Prompt lists the others and ``load_tools`` returns
        # their definitions.
        # Tool changes since the pin reach the Model as ``[tool-change]`` notes,
        # which the Run announces at its request boundaries; dispatch follows
        # what they told the Model. Tool and Extension dynamic blocks show the
        # epoch's pinned texts the same way; their changes reach the Model as
        # ``[prompt-block-change]`` notes at Run start.
        session_messages = (
            await session.load_active_async()
            if inputs.session_messages_override is None
            else list(inputs.session_messages_override)
        )
        system_prompts = self._dependencies.get_system_prompts()
        session_capability = None
        extension_registry = self._dependencies.get_extension_registry()
        if inputs.temporary_binding is not None and extension_registry is not None:
            session_capability = extension_registry.session_capability(
                inputs.temporary_binding, self._dependencies.tools
            )
        live_tool_grants = _live_session_tool_grants(
            session_capability, subagent_session=inputs.subagent_session
        )
        effective_input_modalities = (
            inputs.input_modalities
            if inputs.input_modalities is not None
            else _model_input_modalities(self._dependencies, agent)
        )
        pin = None if inputs.fresh_prompt_epoch else await self._read_tool_epoch_pin(session)
        catalog: LiveToolCatalog | None = None
        if pin is None or inputs.temporary_binding is not None:
            # Measured on the Run's primary route, like every Tool announcement,
            # even while a fallback target serves this request.
            catalog = await self._live_tool_catalog(
                agent,
                session_tool_grants=live_tool_grants,
                input_modalities=(
                    effective_input_modalities
                    if inputs.tool_route_input_modalities is None
                    else inputs.tool_route_input_modalities
                ),
                wire_media_types=(
                    inputs.wire_media_types
                    if inputs.tool_route_wire_media_types is None
                    else inputs.tool_route_wire_media_types
                ),
                model_family=(
                    _model_family(self._dependencies, agent)
                    if inputs.tool_route_model_family is None
                    else inputs.tool_route_model_family
                ),
                told=pin.names if pin is not None else (),
            )
        if pin is None:
            assert catalog is not None
            pin = await _CHAT_TRANSFORM_WORKERS.run(
                partial(ToolEpochPin.start, catalog, keep=inputs.keep_listed_tools)
            )
            if not inputs.fresh_prompt_epoch:
                pin = await self._ensure_tool_epoch_pin(session, pin)
        render_inputs = self._block_render_inputs(inputs, pin)
        prompt_blocks = await self._prompt_block_pin(
            agent, session, render_inputs, fresh=inputs.fresh_prompt_epoch
        )
        tool_epoch, session_messages = await _CHAT_TRANSFORM_WORKERS.run(
            _fold_prompt_epoch, pin, prompt_blocks, session_messages
        )
        # A temporary Session's capability Tools must be offered now; the first
        # request boundary announces any the Model does not know yet.
        if inputs.temporary_binding is not None and (
            session_capability is None
            or catalog is None
            or not set(session_capability.tool_names).issubset(catalog.offered_by_name)
        ):
            raise ChatError(
                "This Session has an invalid configuration. "
                "Ask the user to check it through its Extension."
            )
        tool_loads = tool_epoch.loads(catalog) if catalog is not None else None
        tools, allowed_tool_names, session_tool_grants, tool_contracts = await self._tool_fields(
            tool_epoch,
            live_tool_grants,
            list_announced=inputs.list_announced_tools,
            tool_loads=tool_loads,
        )
        request_block_definitions: tuple[BlockDefinition, ...] = ()
        if session_capability is not None and inputs.temporary_binding is not None:
            assert extension_registry is not None and catalog is not None
            rendered_blocks: list[BlockDefinition] = []
            for declaration in session_capability.prompt_blocks:
                try:
                    rendered = await invoke_extension_handler(
                        declaration.render, inputs.temporary_binding
                    )
                except Exception:
                    _LOGGER.warning(
                        "Session prompt block %r failed", declaration.slug, exc_info=True
                    )
                    continue
                if isinstance(rendered, str) and rendered.strip():
                    rendered_blocks.append(
                        BlockDefinition(
                            id=f"extension_session:{declaration.slug}",
                            owner="always",
                            kind=BLOCK_KIND_DATA,
                            default_text=rendered.strip(),
                            default_rank=10_000,
                        )
                    )
            if not extension_registry.is_registration_current(session_capability.identity):
                raise ChatError(
                    "This Session has an invalid configuration. "
                    "Ask the user to check it through its Extension."
                )
            current_capability = extension_registry.session_capability(
                inputs.temporary_binding, self._dependencies.tools
            )
            if (
                current_capability is None
                or current_capability.identity != session_capability.identity
                or not set(current_capability.tool_names).issubset(catalog.offered_by_name)
            ):
                raise ChatError(
                    "This Session has an invalid configuration. "
                    "Ask the user to check it through its Extension."
                )
            request_block_definitions = tuple(rendered_blocks)
        prompt_read_paths: list[Path] = []
        # The System Prompt describes the pinned Tool list (``render_inputs``)
        # and shows the pinned dynamic block texts, so it stays unchanged for
        # the whole prompt epoch too.
        system_prompt = await system_prompts.build_system_prompt_async(
            agent,
            agent_body=inputs.agent_body,
            skill_registry=inputs.skill_registry,
            skill_catalog=inputs.skill_catalog,
            read_paths=prompt_read_paths,
            pinned_blocks=prompt_blocks.texts(),
            request_block_definitions=request_block_definitions,
            **render_inputs,
        )
        # Auto-injected prompt files (SOUL, pinned memory, project auto-load files,
        # workspace includes) count as read for this session, so the agent can edit
        # one directly without a redundant read call. Rebuilt every request, so the
        # stamp always reflects what the model currently sees; a later on-disk change
        # still trips the stale guard and forces a re-read.
        await _CHAT_TRANSFORM_WORKERS.run(
            stamp_prompt_files_read,
            self._dependencies.file_read_state,
            session.id,
            prompt_read_paths,
        )
        prepared_messages = await _CHAT_TRANSFORM_WORKERS.run(
            _prepare_request_messages,
            system_prompt=system_prompt,
            agent_model=agent.model,
            session_messages=session_messages,
            replay_policy=inputs.replay_policy,
            reasoning_scope_model=inputs.reasoning_scope_model or agent.model,
        )
        effective_messages = prepared_messages.effective_messages
        request_messages = prepared_messages.messages

        session.drain_pending_notes()

        if self._attachment_resolver is None:
            return _RequestState(
                request_messages,
                tools,
                allowed_tool_names,
                session_tool_grants,
                tool_contracts,
                tool_epoch,
                prompt_blocks,
                tool_loads,
            )

        current_user_message, read_media_outputs = await _CHAT_TRANSFORM_WORKERS.run(
            _request_content_resolution_inputs,
            effective_messages,
        )
        # Use the most recently appended user turn as the current-turn marker.
        # If that turn is plain text, all user content blocks resolve as historical.
        if current_user_message is not None:
            request_messages = await self._attachment_resolver.resolve_messages(
                request_messages,
                current_user_message_id=current_user_message.id,
                input_modalities=effective_input_modalities,
                wire_media_types=inputs.wire_media_types,
                max_image_bytes=inputs.max_image_bytes,
            )

        await self._attach_tool_result_content(
            [message for message in request_messages if message.get("role") == "tool"],
            read_media_outputs,
            effective_input_modalities,
            inputs.wire_media_types,
            max_image_bytes=inputs.max_image_bytes,
        )
        image_budget = inputs.image_budget or RequestImageBudget()
        if not image_budget.restored:
            retirement_pin = await _CHAT_TRANSFORM_WORKERS.run(
                self._dependencies.sessions.prompt_pin,
                session.address,
                PINNED_IMAGE_RETIREMENT_SLOT,
            )
            await _CHAT_TRANSFORM_WORKERS.run(
                partial(
                    image_budget.restore,
                    retirement_pin,
                    request_messages,
                    current_user_message_id=(
                        current_user_message.id if current_user_message is not None else None
                    ),
                )
            )
        return _RequestState(
            await _CHAT_TRANSFORM_WORKERS.run(
                limit_request_images,
                request_messages,
                budget=image_budget,
                image_limit=inputs.max_request_images,
            ),
            tools,
            allowed_tool_names,
            session_tool_grants,
            tool_contracts,
            tool_epoch,
            prompt_blocks,
            tool_loads,
        )

    async def rebuild_live_request_state(
        self,
        agent: Any,
        session: ChatSession,
        *,
        inputs: RequestBuildInputs,
        live_messages: list[JsonObject] | None,
    ) -> _RequestState:
        """Rebuild an active Run's request without losing its live-only state.

        Canonical history shaping cannot see what exists only in the live
        request: current-Run native Reasoning (stripped from history under
        ``current_run``) and Run-local Tool media. Steering, stale-Compaction
        recovery, post-Compaction projection and Model fallback all rebuild
        through this one path; fallback strips native Reasoning afterward.
        """
        state = await self.build_request_state(agent, session, inputs=inputs)
        messages = state.messages
        if live_messages is not None:
            messages = await _restore_in_run_tool_result_content(
                _restore_in_run_assistant_reasoning(messages, live_messages),
                live_messages,
                input_modalities=inputs.input_modalities,
                wire_media_types=inputs.wire_media_types,
                image_budget=inputs.image_budget,
                image_limit=inputs.max_request_images,
                image_converter=self._tool_image_converter,
                max_image_bytes=inputs.max_image_bytes,
            )
        return replace(state, messages=messages)

    async def live_tool_catalog(
        self,
        context: _RunExecutionContext,
        *,
        known: Collection[str] = (),
        told: Collection[str] = (),
    ) -> LiveToolCatalog:
        """Measure the Tools the Run's Agent may use now, on the Run's primary route.

        *known* names the Tools the Model may call now; only for these does a
        withdrawn image-understanding backend count as a removal. *told* names
        every Tool the Model was told about in this prompt epoch; its file edit
        Tools keep their dialect after a Model change.
        """
        capability = None
        extension_registry = self._dependencies.get_extension_registry()
        if context.request.temporary_binding is not None and extension_registry is not None:
            capability = extension_registry.session_capability(
                context.request.temporary_binding, self._dependencies.tools
            )
        target = context.primary_target
        return await self._live_tool_catalog(
            context.agent,
            session_tool_grants=_live_session_tool_grants(
                capability, subagent_session=context.subagent_session
            ),
            input_modalities=target.input_modalities,
            wire_media_types=target.wire_media_types,
            model_family=target.model_family,
            known=known,
            told=told,
        )

    async def announce_tool_changes(
        self,
        state: _RequestState,
        catalog: LiveToolCatalog,
        session: ChatSession,
        *,
        unlisted_tool_calls: bool,
        list_announced: bool,
    ) -> _RequestState:
        """Add a note for each Tool change since *state*'s Tool epoch and adopt it.

        Returns *state* with the dispatch allowlist, contracts and request Tools
        of the announced knowledge, and with the definitions *catalog* gives
        the On-demand Tools the Model may load; unchanged when nothing changed.
        """
        tool_epoch = state.tool_epoch
        if tool_epoch is None:
            return state
        changes = tool_epoch.plan(catalog, unlisted_tool_calls=unlisted_tool_calls)
        for change in changes:
            session.add_note(change.note_content())
        tool_epoch = tool_epoch.with_changes(changes)
        tool_loads = tool_epoch.loads(catalog)
        if not changes and tool_loads == (state.tool_loads or {}):
            if state.tool_loads is None and tool_epoch.loadable_names:
                return replace(state, tool_loads=tool_loads)
            return state
        tools, allowed_tool_names, session_tool_grants, tool_contracts = await self._tool_fields(
            tool_epoch,
            catalog.session_tool_grants,
            list_announced=list_announced,
            tool_loads=tool_loads,
        )
        return replace(
            state,
            tools=tools,
            allowed_tool_names=allowed_tool_names,
            session_tool_grants=session_tool_grants,
            tool_contracts=tool_contracts,
            tool_epoch=tool_epoch,
            tool_loads=tool_loads,
        )

    async def adopt_tool_changes(
        self, state: _RequestState, changes: Sequence[ToolChange], *, list_announced: bool
    ) -> _RequestState:
        """Return *state* after *changes* the Run persisted itself (Tools ``load_tools`` loaded).

        The loaded Tools join the request Tools on routes that list announced
        Tools; every other route keeps sending the same Tool list.
        """
        tool_epoch = state.tool_epoch
        if tool_epoch is None or not changes:
            return state
        tool_epoch = tool_epoch.with_changes(changes)
        tools, allowed_tool_names, session_tool_grants, tool_contracts = await self._tool_fields(
            tool_epoch,
            state.session_tool_grants,
            list_announced=list_announced,
            tool_loads=state.tool_loads,
        )
        return replace(
            state,
            tools=tools,
            allowed_tool_names=allowed_tool_names,
            session_tool_grants=session_tool_grants,
            tool_contracts=tool_contracts,
            tool_epoch=tool_epoch,
        )

    async def _tool_fields(
        self,
        tool_epoch: ToolEpochView,
        live_tool_grants: Sequence[str],
        *,
        list_announced: bool,
        tool_loads: Mapping[str, ToolChange] | None = None,
    ) -> tuple[list[JsonObject], tuple[str, ...], tuple[str, ...], Mapping[str, ToolContract]]:
        """Return request Tools, allowlist, grants and contracts of *tool_epoch*.

        An On-demand Tool the Model may load gets its contract from *tool_loads*
        (the definition a load would return), so a call made without loading
        it first validates against the same definition.
        """
        allowed_tool_names = tool_epoch.allowed_names
        allowed = set(allowed_tool_names)
        loads = tool_loads or {}
        tool_contracts = await _CHAT_TRANSFORM_WORKERS.run(
            self._dependencies.tools.contracts_for_provider_definitions,
            [
                *tool_epoch.definitions(),
                *(
                    definition
                    for name in tool_epoch.loadable_names
                    if (change := loads.get(name)) is not None
                    and (definition := change.definition) is not None
                ),
            ],
        )
        return (
            tool_epoch.request_tools(list_announced=list_announced),
            allowed_tool_names,
            tuple(name for name in live_tool_grants if name in allowed),
            tool_contracts,
        )

    async def _live_tool_catalog(
        self,
        agent: Any,
        *,
        session_tool_grants: Sequence[str],
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
        model_family: str,
        known: Collection[str] = (),
        told: Collection[str] = (),
    ) -> LiveToolCatalog:
        definitions = await self._dependencies.get_system_prompts().provider_tool_definitions_async(
            agent,
            session_tool_grants=session_tool_grants,
            ready_only=False,
        )
        ready, sources, change_notes = await _CHAT_TRANSFORM_WORKERS.run(
            self._measure_tool_definitions, definitions
        )
        offered = await self._route_tool_definitions(
            ready,
            tool_access=agent.tool_access,
            input_modalities=input_modalities,
            wire_media_types=wire_media_types,
            # A prompt epoch keeps the file edit dialect its Model was told about.
            edit_dialect=known_edit_dialect(told) or edit_dialect(model_family),
        )
        usable = {str(definition["name"]) for definition in definitions}
        if (
            ANALYZE_IMAGE_TOOL_NAME in usable
            and ANALYZE_IMAGE_TOOL_NAME in known
            and all(definition.get("name") != ANALYZE_IMAGE_TOOL_NAME for definition in offered)
            and not await self._dependencies.image_understanding_available()
        ):
            usable.discard(ANALYZE_IMAGE_TOOL_NAME)
        on_demand = await _CHAT_TRANSFORM_WORKERS.run(
            partial(
                on_demand_tool_entries,
                self._dependencies.tools,
                agent,
                offered,
                session_tool_grants=session_tool_grants,
            )
        )
        # load_tools is offered while a Tool is there to load, and usable while
        # any usable Tool is on demand: readiness and route gates alone never
        # remove it from a prompt epoch that knows it.
        if not on_demand:
            offered = [
                definition
                for definition in offered
                if definition.get("name") != LOAD_TOOLS_TOOL_NAME
            ]
        if not on_demand_tools(agent, usable, session_tool_grants=session_tool_grants):
            usable.discard(LOAD_TOOLS_TOOL_NAME)
        return LiveToolCatalog(
            usable=frozenset(usable),
            offered=tuple(offered),
            sources=sources,
            session_tool_grants=tuple(session_tool_grants),
            change_notes={name: note for name, note in change_notes.items() if name in usable},
            on_demand=dict(on_demand),
        )

    def _measure_tool_definitions(
        self, definitions: Sequence[JsonObject]
    ) -> tuple[list[JsonObject], dict[str, str], dict[str, ToolDefinitionChangeNote]]:
        """Return the ready *definitions*, every fingerprint and change-note hook.

        Runs on a Chat worker: readiness checks and fingerprints cost time per Tool.
        """
        ready: list[JsonObject] = []
        sources: dict[str, str] = {}
        change_notes: dict[str, ToolDefinitionChangeNote] = {}
        for definition in definitions:
            name = str(definition["name"])
            tool = self._registered_tool(name)
            if tool is None or tool_is_ready(tool):
                ready.append(definition)
            sources[name] = definition_source(definition)
            if tool is not None and tool.definition_change_note is not None:
                change_notes[name] = tool.definition_change_note
        return ready, sources, change_notes

    def _registered_tool(self, name: str) -> Tool | None:
        try:
            return self._dependencies.tools.get(name)
        except ToolNotFoundError:
            return None

    async def _read_tool_epoch_pin(self, session: ChatSession) -> ToolEpochPin | None:
        return await _CHAT_TRANSFORM_WORKERS.run(self._stored_tool_epoch_pin, session.address)

    def _stored_tool_epoch_pin(self, address: SessionAddress) -> ToolEpochPin | None:
        return ToolEpochPin.from_payload(
            self._dependencies.sessions.prompt_pin(address, PINNED_TOOL_DEFINITIONS_SLOT)
        )

    async def _ensure_tool_epoch_pin(self, session: ChatSession, pin: ToolEpochPin) -> ToolEpochPin:
        """Pin *pin* unless a concurrent first request pinned a readable one meanwhile."""
        return await _CHAT_TRANSFORM_WORKERS.run(self._pin_tool_epoch, session.address, pin)

    def _pin_tool_epoch(self, address: SessionAddress, pin: ToolEpochPin) -> ToolEpochPin:
        pinned = self._dependencies.sessions.ensure_prompt_pin(
            address,
            PINNED_TOOL_DEFINITIONS_SLOT,
            pin.to_payload(),
            lambda current: ToolEpochPin.from_payload(current) is not None,
        )
        return ToolEpochPin.from_payload(pinned) or pin

    async def persist_image_retirement(
        self, session: ChatSession, image_budget: RequestImageBudget
    ) -> None:
        """Keep newly retired images retired in later Runs of this prompt epoch."""
        pin = image_budget.take_unsaved_pin()
        if pin is None:
            return
        await _CHAT_TRANSFORM_WORKERS.run(
            self._dependencies.sessions.ensure_prompt_pin,
            session.address,
            PINNED_IMAGE_RETIREMENT_SLOT,
            pin,
            lambda current: current == pin,
        )

    async def _route_tool_definitions(
        self,
        tools: list[JsonObject],
        *,
        tool_access: ToolAccess,
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
        edit_dialect: EditDialect,
    ) -> list[JsonObject]:
        """Apply effective Model-route gates to route-dependent Tools.

        Of the file edit Tools, only those of *edit_dialect* are offered.
        """

        tools = offer_edit_dialect(tools, edit_dialect)
        tools = project_terminal_tool_definitions(tools)
        if not any(definition.get("name") == ANALYZE_IMAGE_TOOL_NAME for definition in tools):
            return tools
        route_can_view_images = "image" in input_modalities and any(
            media_type.startswith("image/") for media_type in wire_media_types
        )
        image_task_available = (
            False
            if route_can_view_images and ANALYZE_IMAGE_TOOL_NAME not in tool_access.granted
            else await self._dependencies.image_understanding_available()
        )
        if image_task_available:
            return tools
        return [
            definition for definition in tools if definition.get("name") != ANALYZE_IMAGE_TOOL_NAME
        ]

    async def preview_tool_definitions(
        self, agent: Any, *, session_tool_grants: Sequence[str] = ()
    ) -> list[JsonObject]:
        """Apply the production Tool-route rules without starting a Run or calling a Model.

        Every Tool the Agent may use is listed with its definition, so
        ``load_tools`` is left out.
        """
        definitions = await self._dependencies.get_system_prompts().provider_tool_definitions_async(
            agent,
            session_tool_grants=session_tool_grants,
        )
        tools = [tool for tool in definitions if tool.get("name") != LOAD_TOOLS_TOOL_NAME]
        dialect = edit_dialect(_model_family(self._dependencies, agent))
        if not any(tool.get("name") == ANALYZE_IMAGE_TOOL_NAME for tool in tools):
            return await self._route_tool_definitions(
                tools,
                tool_access=agent.tool_access,
                input_modalities=frozenset(),
                wire_media_types=frozenset(),
                edit_dialect=dialect,
            )
        provider_id, connection_id = _resolve_agent_connection(self._dependencies, agent)
        _, model_id = _split_agent_model(agent.model)
        target = self._create_model_target(
            provider_id, connection_id, model_id, public_model=agent.model
        )
        try:
            return await self._route_tool_definitions(
                tools,
                tool_access=agent.tool_access,
                input_modalities=target.input_modalities,
                wire_media_types=target.wire_media_types,
                edit_dialect=dialect,
            )
        finally:
            await _close_adapter(target.adapter)

    async def store_tool_media(
        self, tool_messages: list[ChatMessage], media_outputs: list[JsonObject]
    ) -> tuple[list[ChatMessage], list[JsonObject]]:
        """Store a Tool batch's loaded images and reference them from their Results.

        Stored images reach every later request of the Session from their Tool
        message, so a new Run sends the same bytes again and keeps the prompt cache.
        """
        if self._attachment_resolver is None or not any("base64" in m for m in media_outputs):
            return tool_messages, media_outputs
        loaded = ["base64" in media for media in media_outputs]
        media_outputs = await self._attachment_resolver.store_tool_images(media_outputs)
        stored: dict[str, list[JsonObject]] = {}
        for was_loaded, media in zip(loaded, media_outputs, strict=True):
            message_id = media.get("tool_message_id")
            if was_loaded and "base64" not in media and isinstance(message_id, str):
                stored.setdefault(message_id, []).append(
                    {key: media[key] for key in ("attachment_id", "filename", "media_type")}
                )
        return [
            replace(message, tool_media=[*(message.tool_media or []), *stored[message.id]])
            if message.id in stored
            else message
            for message in tool_messages
        ], media_outputs

    async def _attach_tool_result_content(
        self,
        tool_messages: list[JsonObject],
        media_outputs: list[JsonObject],
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
        *,
        max_image_bytes: int | None = None,
    ) -> None:
        """Attach resolved media blocks to their correlated Tool Results.

        The persisted Tool message keeps only its compact result envelope. Base64
        blocks live exclusively in the in-flight request, so reading an image does
        not fabricate or persist a user turn. Correlation uses the Tool message
        identity because Providers may reuse Tool-call ids across turns.
        """

        if not media_outputs:
            return

        by_tool_message_id: dict[str, list[JsonObject]] = {}
        for media_output in media_outputs:
            tool_message_id = media_output.get("tool_message_id")
            if isinstance(tool_message_id, str):
                by_tool_message_id.setdefault(tool_message_id, []).append(media_output)

        for tool_message in tool_messages:
            tool_message_id = tool_message.get("id")
            matching = (
                by_tool_message_id.get(tool_message_id, [])
                if isinstance(tool_message_id, str)
                else []
            )
            if not matching:
                continue
            local_content = [
                block
                for media_output in matching
                if "base64" in media_output
                for block in await ContentBlockResolver.resolve_tool_image(
                    media_output,
                    input_modalities,
                    wire_media_types,
                    self._tool_image_converter,
                    max_image_bytes=max_image_bytes,
                )
            ]
            if local_content:
                tool_message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] = local_content
            matching = [media for media in matching if "attachment_id" in media]
            if not matching or self._attachment_resolver is None:
                continue
            content_blocks = [
                content_block_to_dict(
                    MediaBlock(
                        type="media",
                        attachment_id=media_output["attachment_id"],
                        filename=media_output["filename"],
                        media_type=media_output["media_type"],
                    )
                )
                for media_output in matching
            ]
            transient_message_id = f"tool-result:{tool_message_id}"
            resolved = await self._attachment_resolver.resolve_messages(
                [
                    {
                        "id": transient_message_id,
                        "role": "user",
                        "content": content_blocks,
                    }
                ],
                current_user_message_id=transient_message_id,
                input_modalities=input_modalities,
                wire_media_types=wire_media_types,
                max_image_bytes=max_image_bytes,
            )
            resolved_content = resolved[0].get("content")
            if isinstance(resolved_content, list):
                tool_message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] = local_content + resolved_content

    def resolve_context_window(self, agent: Any, target: _ModelTarget) -> int | None:
        """Resolve the usable context window for the current Model target.

        Returns ``None`` only when the Model catalog has no entry for the target.
        Otherwise the value always resolves through the shared effective chain
        (user-set/capped window for flagged-local models, else model window →
        provider-config default → global floor, see
        :func:`resolve_effective_context_window`), so a model whose window is
        ``None`` still gets a usable budget and auto-compaction keeps working
        instead of silently disabling itself.
        """
        del agent  # The target carries the resolved Model and Connection.
        provider_id = target.provider_id
        resolved_model_id = target.model_id
        connection_id = target.connection_id

        try:
            model_entry = self._dependencies.models.get(provider_id, resolved_model_id)
        except KeyError, AttributeError:
            return None

        model_context_window = model_entry.context_window
        try:
            if connection_id is None:
                raise ConfigError("Model target has no resolved Provider Connection")
            local_connection_id, _account_id = split_connection_id(provider_id, connection_id)
            context_window_for = getattr(model_entry, "context_window_for", None)
            if callable(context_window_for):
                model_context_window = context_window_for(local_connection_id)
        except AttributeError, ChatError, ConfigError, KeyError:
            # Status/build callers can be partially wired. Preserve the existing
            # Model-wide fallback when the active Connection cannot be resolved.
            pass

        local_context_windows = self._dependencies.get_local_context_windows()

        return resolve_effective_context_window(
            model_context_window,
            self._lookup_provider_config(provider_id),
            model_metadata=model_entry.metadata,
            model_key=f"{provider_id}/{resolved_model_id}",
            # Live read through the runtime's single source of truth (no reload
            # hook, StorageError-tolerant), so a settings change applies to the
            # next request without re-implementing the storage read here.
            local_context_windows=local_context_windows,
        )

    def _lookup_provider_config(self, provider_id: str) -> Any:
        """Return the ProviderConfig for the read-side window default, or None.

        Tolerant of a missing/partial runtime (the registry may be absent for a
        custom provider): the resolver treats ``None`` as "no provider default"
        and falls back to the global floor.
        """
        try:
            return self._dependencies.providers.get(provider_id)
        except KeyError, AttributeError:
            return None

    def _raise_if_measured_context_exhausted(
        self,
        session_messages: list[ChatMessage],
        request_messages: list[JsonObject],
        tools: list[JsonObject],
        agent: Any,
        run: Run,
        target: _ModelTarget,
        *,
        context_usage: JsonObject | None = None,
    ) -> None:
        """Fail fast when the projected request already fills the Model window.

        The same measured anchor plus request delta drives the indicator and
        Compaction. Output-limit safety reserves belong to the Adapter and do
        not inflate the displayed Context or override a measured anchor.
        """

        context_window = self.resolve_context_window(agent, target)
        if context_window is None:
            return
        projection = context_usage or latest_session_context_usage(session_messages)
        if projection is None or "provider_input_tokens" not in projection:
            return
        projected_tokens = int(projection["tokens"])
        if projected_tokens < context_window:
            return
        _LOGGER.error(
            "Run %s pre-send context guard tripped (agent=%s session=%s "
            "projected_input_tokens=%d context_window=%d)",
            run.id,
            run.agent_id,
            run.session_id,
            projected_tokens,
            context_window,
        )
        raise ProviderError(
            "Context usage leaves no output capacity in the Model "
            f"context window (projected_input_tokens={projected_tokens}, "
            f"context_window={context_window})",
            retryable=False,
        )

    def resolve_summary_adapter(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        settings: Any,
        *,
        active_provider_id: str,
        active_connection_id: str,
    ) -> tuple[Any, str, str]:
        """Resolve compaction summary adapter/model/provider, defaulting to active.

        A configured summary Model that names the active Model, without another
        Connection, is the active target: it reuses the active adapter so the
        summary request keeps the Run's Provider prompt cache.
        """
        del agent

        summary_model = settings.summary_model
        if not isinstance(summary_model, str) or not summary_model:
            return adapter, model_id, active_provider_id

        try:
            provider_id, summary_model_id, connection_suffix = parse_model_with_connection(
                summary_model
            )
            if (
                provider_id == active_provider_id
                and summary_model_id == model_id
                and (
                    not connection_suffix
                    or f"{provider_id}:{connection_suffix}" == active_connection_id
                )
            ):
                return adapter, model_id, active_provider_id
            if connection_suffix:
                connection_id = f"{provider_id}:{connection_suffix}"
            else:
                connection_id = _first_usable_connection_id(
                    self._dependencies,
                    provider_id,
                    _model_connection_allowlist(self._dependencies, provider_id, summary_model_id),
                )
            summary_adapter = self._dependencies.get_adapter(
                ConnectionRef(provider_id, connection_id)
            )
        except ChatError, ConfigError, VBotError, KeyError:
            _LOGGER.warning(
                "Invalid compaction summary model %r; using active run model instead.",
                summary_model,
                exc_info=True,
            )
            return adapter, model_id, active_provider_id

        return summary_adapter, summary_model_id, provider_id
