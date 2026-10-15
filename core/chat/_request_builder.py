"""Pinned prompt, history, media and Model-route request construction."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents import skill_subject_id
from core.chat._effort_epoch import (
    EffortPin,
    EffortState,
    ensure_effort_pin,
    thinking_effort,
    without_other_epoch_effort_changes,
)
from core.chat._message_history import finalize_checkpoint_guidance
from core.chat._prompt_block_epoch import (
    PromptBlockPin,
    ensure_prompt_block_pin,
    plan_prompt_block_change,
    without_other_epoch_prompt_block_changes,
)
from core.chat._request_history import (
    _prepare_request_messages,
)
from core.chat._request_media import RequestMedia
from core.chat._request_targets import RequestTargets
from core.chat._run_state import RequestBuildInputs, _RequestState
from core.chat._scratch_context import latest_goal_note_id, preserve_notes
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
from core.chat.errors import ChatError
from core.chat.events import _close_adapter
from core.chat.messages import (
    ChatMessage,
    JsonObject,
)
from core.chat.model_resolution import (
    _model_family,
    _model_input_modalities,
)
from core.chat.wire_shaping import (
    PINNED_IMAGE_RETIREMENT_SLOT,
    RequestImageBudget,
    _restore_in_run_assistant_reasoning,
)
from core.extensions import invoke_extension_handler
from core.projects import ProjectError
from core.prompts import BLOCK_KIND_DATA, BlockDefinition
from core.prompts.pinned_context import (
    PINNED_DYNAMIC_BLOCKS_SLOT,
    PINNED_THINKING_EFFORT_SLOT,
    PINNED_TOOL_DEFINITIONS_SLOT,
    stamp_prompt_files_read,
)
from core.sessions import (
    SKILL_AVAILABLE_NOTE_PREFIX,
    ChatSession,
    SeenSkillsUpdate,
    SessionAddress,
)
from core.tools import (
    ANALYZE_IMAGE_TOOL_NAME,
    SUBAGENT_SESSION_TOOL_NAMES,
    UPDATE_GOAL_TOOL_NAME,
    EditDialect,
    Tool,
    ToolAccess,
    ToolContract,
    ToolNotFoundError,
    edit_dialect,
    known_edit_dialect,
    offer_edit_dialect,
    tool_is_ready,
)
from core.tools.on_demand import LOAD_TOOLS_TOOL_NAME, on_demand_tool_entries, on_demand_tools
from core.tools.terminal import project_terminal_tool_definitions
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat._run_state import ChatLoopDependencies, _RunExecutionContext
    from core.chat.request_runner import WireRequestRunner
    from core.skills.skills import SkillRegistry

_LOGGER = get_logger("chat")


def _finalize_compaction_checkpoint(
    checkpoint: ChatMessage,
    session_messages: list[ChatMessage],
    scratch_note_id: object,
    goal_open: bool,
) -> ChatMessage:
    ordinal = sum(message.role == "compaction_checkpoint" for message in session_messages) + 1
    kept_note_ids = [scratch_note_id]
    if goal_open:
        kept_note_ids.append(latest_goal_note_id(session_messages))
    return preserve_notes(
        finalize_checkpoint_guidance(checkpoint, ordinal=ordinal), session_messages, kept_note_ids
    )


def _live_session_tool_grants(
    session_capability: Any | None, *, subagent_session: bool, goal_session: bool
) -> tuple[str, ...]:
    """Return the session-scoped Tools the Session grants now.

    A temporary Session grants its capability's Tools; a Sub-Agent Session grants
    the Tools a Sub-Agent uses to reach its Parent Agent; a Session with an open
    goal grants the Tool that ends its automatic turns.
    """
    grants = tuple(session_capability.tool_names) if session_capability is not None else ()
    if subagent_session:
        grants = (*grants, *SUBAGENT_SESSION_TOOL_NAMES)
    if goal_session:
        grants = (*grants, UPDATE_GOAL_TOOL_NAME)
    return grants


def _fold_prompt_epoch(
    pin: ToolEpochPin,
    prompt_blocks: PromptBlockPin,
    effort: EffortPin,
    session_messages: list[ChatMessage],
) -> tuple[ToolEpochView, EffortState, list[ChatMessage]]:
    """Return the epoch's Tool and effort state and the history without other epochs' notes.

    They walk the whole Session, so they run on a Chat worker.
    """
    messages = without_other_epoch_effort_changes(
        without_other_epoch_prompt_block_changes(
            without_other_epoch_tool_changes(session_messages, pin.epoch), prompt_blocks.epoch
        ),
        effort.epoch,
    )
    return ToolEpochView.fold(pin, messages), EffortState.fold(effort, messages), messages


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
        self.media = RequestMedia(attachment_resolver, dependencies.sessions)
        self.targets = RequestTargets(
            dependencies,
            max_image_bytes=attachment_resolver.max_image_bytes if attachment_resolver else None,
        )

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
        inputs = RequestBuildInputs.from_context(context, context.primary_target)
        failed_blocks: set[str] = set()
        session_blocks = await self._session_prompt_blocks(inputs, failed_blocks=failed_blocks)
        read_paths: list[Path] = []
        note = await _CHAT_TRANSFORM_WORKERS.run(
            self._plan_prompt_block_change,
            context.agent,
            context.session.address,
            inputs,
            context.session_snapshot.active_messages,
            session_blocks,
            failed_blocks,
            read_paths,
        )
        await _CHAT_TRANSFORM_WORKERS.run(
            stamp_prompt_files_read,
            self._dependencies.file_read_state,
            context.session.id,
            read_paths,
        )
        return note

    def _plan_prompt_block_change(
        self,
        agent: Any,
        address: SessionAddress,
        inputs: RequestBuildInputs,
        messages: list[ChatMessage],
        session_blocks: Sequence[BlockDefinition],
        failed_blocks: set[str],
        read_paths: list[Path],
    ) -> str | None:
        sessions = self._dependencies.sessions
        pin = PromptBlockPin.from_payload(sessions.prompt_pin(address, PINNED_DYNAMIC_BLOCKS_SLOT))
        tool_pin = self._stored_tool_epoch_pin(address)
        if pin is None or tool_pin is None:
            return None
        effort_pin = EffortPin.from_payload(
            sessions.prompt_pin(address, PINNED_THINKING_EFFORT_SLOT)
        )
        render_inputs = self._block_render_inputs(
            inputs, tool_pin, prompt_effort=effort_pin.effort if effort_pin is not None else None
        )
        # Complete an older partial inventory before comparing it with live
        # state, so an already visible static block is not announced as new.
        pin = self._prompt_block_pin(
            agent,
            address,
            render_inputs,
            fresh=False,
            session_blocks=session_blocks,
            read_paths=read_paths,
        )
        live = self._dependencies.get_system_prompts().render_dynamic_blocks(
            agent,
            request_block_definitions=session_blocks,
            failed_blocks=failed_blocks,
            **render_inputs,
        )
        change = plan_prompt_block_change(pin, messages, live, unavailable=failed_blocks)
        return None if change is None else change.note_content()

    async def _session_prompt_blocks(
        self, inputs: RequestBuildInputs, *, failed_blocks: set[str] | None = None
    ) -> tuple[BlockDefinition, ...]:
        """Render temporary Session contributions for snapshots and change notes."""
        binding = inputs.temporary_binding
        if binding is None:
            return ()
        registry = self._dependencies.get_extension_registry()
        capability = (
            registry.session_capability(binding, self._dependencies.tools)
            if registry is not None
            else None
        )
        if capability is None or registry is None:
            raise ChatError(
                "This Session has an invalid configuration. "
                "Ask the user to check it through its Extension."
            )
        blocks: list[BlockDefinition] = []
        for declaration in capability.prompt_blocks:
            block_id = f"extension_session:{declaration.slug}"
            try:
                rendered = await invoke_extension_handler(declaration.render, binding)
            except Exception:
                _LOGGER.warning("Session prompt block %r failed", declaration.slug, exc_info=True)
                if failed_blocks is not None:
                    failed_blocks.add(block_id)
                continue
            if isinstance(rendered, str):
                blocks.append(
                    BlockDefinition(
                        id=block_id,
                        owner="always",
                        kind=BLOCK_KIND_DATA,
                        default_text=rendered.strip(),
                        default_rank=10_000,
                    )
                )
            elif failed_blocks is not None:
                failed_blocks.add(block_id)
        if not registry.is_registration_current(capability.identity):
            raise ChatError(
                "This Session has an invalid configuration. "
                "Ask the user to check it through its Extension."
            )
        return tuple(blocks)

    def _block_render_inputs(
        self,
        inputs: RequestBuildInputs,
        tool_pin: ToolEpochPin,
        *,
        prompt_effort: str | None = None,
    ) -> dict[str, Any]:
        """The System Prompt inputs dynamic blocks render from, for builds and pins alike."""
        return {
            "project_context": inputs.project_context,
            "working_project_context": inputs.working_project_context,
            "soul_context": inputs.soul_context,
            "memory_files_context": inputs.memory_files_context,
            "agent_project_id": inputs.agent_project_id,
            "subagent_session": inputs.subagent_session,
            "skill_registry": inputs.skill_registry,
            "skill_catalog": inputs.skill_catalog,
            "thinking_effort": prompt_effort,
            "effective_tool_definitions": tool_pin.definitions,
            "on_demand_tools": tool_pin.on_demand,
        }

    def _prompt_block_pin(
        self,
        agent: Any,
        address: SessionAddress,
        render_inputs: Mapping[str, Any],
        *,
        fresh: bool,
        session_blocks: Sequence[BlockDefinition],
        read_paths: list[Path],
    ) -> PromptBlockPin:
        """Return the prompt epoch's immutable block inventory, texts and layout order.

        A *fresh* pin starts a new epoch from the blocks as they render now; the
        caller persists it (Compaction commit).
        """
        system_prompts = self._dependencies.get_system_prompts()
        captured: PromptBlockPin | None = None
        captured_reads: list[Path] = []

        def render(previously_pinned: Collection[str] | None) -> PromptBlockPin:
            nonlocal captured
            order: list[str] = []
            blocks = system_prompts.render_dynamic_blocks(
                agent,
                skip=previously_pinned or (),
                static_contributions_only=previously_pinned is not None,
                block_order=order,
                request_block_definitions=session_blocks,
                read_paths=captured_reads,
                **render_inputs,
            )
            captured = PromptBlockPin.start(blocks, order=order)
            return captured

        if fresh:
            pin = render(None)
        else:
            pin = ensure_prompt_block_pin(self._dependencies.sessions, address, render)
        # An inventory extension keeps the old epoch id. A concurrent winning
        # pin can hold different text from what this request just captured.
        # Stamp only files whose captured content this request actually shows.
        if captured is not None and all(
            pin.blocks.get(block_id) == block for block_id, block in captured.blocks.items()
        ):
            read_paths.extend(captured_reads)
        return pin

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
            session_capability,
            subagent_session=inputs.subagent_session,
            goal_session=inputs.goal_session,
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
        effort_pin = (
            EffortPin.start(
                thinking_effort(agent)
                if inputs.fresh_thinking_effort is None
                else inputs.fresh_thinking_effort
            )
            if inputs.fresh_prompt_epoch
            else await _CHAT_TRANSFORM_WORKERS.run(
                ensure_effort_pin,
                self._dependencies.sessions,
                session.address,
                thinking_effort(agent),
            )
        )
        render_inputs = self._block_render_inputs(inputs, pin, prompt_effort=effort_pin.effort)
        prompt_read_paths: list[Path] = []
        request_block_definitions = await self._session_prompt_blocks(inputs)
        prompt_blocks = await _CHAT_TRANSFORM_WORKERS.run(
            partial(
                self._prompt_block_pin,
                agent,
                session.address,
                render_inputs,
                fresh=inputs.fresh_prompt_epoch,
                session_blocks=request_block_definitions,
                read_paths=prompt_read_paths,
            )
        )
        tool_epoch, effort, session_messages = await _CHAT_TRANSFORM_WORKERS.run(
            _fold_prompt_epoch, pin, prompt_blocks, effort_pin, session_messages
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
        if session_capability is not None and inputs.temporary_binding is not None:
            assert extension_registry is not None and catalog is not None
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
        # The System Prompt describes the pinned Tool list (``render_inputs``)
        # and shows the pinned dynamic block texts and thinking effort, so it
        # stays unchanged for the whole prompt epoch too.
        system_prompt = await system_prompts.build_system_prompt_async(
            agent,
            agent_body=inputs.agent_body,
            read_paths=prompt_read_paths,
            pinned_blocks=prompt_blocks.texts(),
            pinned_block_order=prompt_blocks.order,
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

        return _RequestState(
            await self.media.resolve_history(
                request_messages, effective_messages, session, inputs, effective_input_modalities
            ),
            tools,
            allowed_tool_names,
            session_tool_grants,
            tool_contracts,
            tool_epoch,
            prompt_blocks,
            tool_loads,
            effort,
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
            messages = await self.media.restore_tool_results(
                _restore_in_run_assistant_reasoning(messages, live_messages), live_messages, inputs
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
                capability,
                subagent_session=context.subagent_session,
                goal_session=context.goal_session,
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
        ready, sources, announce_changes = await _CHAT_TRANSFORM_WORKERS.run(
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
            announce_changes={
                name: parts for name, parts in announce_changes.items() if name in usable
            },
            on_demand=dict(on_demand),
        )

    def _measure_tool_definitions(
        self, definitions: Sequence[JsonObject]
    ) -> tuple[list[JsonObject], dict[str, str], dict[str, tuple[str, ...]]]:
        """Return the ready *definitions*, every fingerprint and announced definition parts.

        Runs on a Chat worker: readiness checks and fingerprints cost time per Tool.
        """
        ready: list[JsonObject] = []
        sources: dict[str, str] = {}
        announce_changes: dict[str, tuple[str, ...]] = {}
        for definition in definitions:
            name = str(definition["name"])
            tool = self._registered_tool(name)
            if tool is None or tool_is_ready(tool):
                ready.append(definition)
            sources[name] = definition_source(definition)
            if tool is not None and tool.announce_changes:
                announce_changes[name] = tool.announce_changes
        return ready, sources, announce_changes

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
        target = await self.targets.for_agent(agent)
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
