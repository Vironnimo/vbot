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
    ensure_effort_pin,
    thinking_effort,
)
from core.chat._prompt_block_epoch import (
    PromptBlockPin,
    ensure_prompt_block_pin,
    plan_prompt_block_change,
)
from core.chat._request_history import (
    _prepare_request_messages,
)
from core.chat._request_media import RequestMedia
from core.chat._request_targets import RequestTargets
from core.chat._request_tool_catalog import RequestToolCatalog
from core.chat._run_state import RequestBuildInputs, _RequestState
from core.chat._tool_epoch import (
    LiveToolCatalog,
    ToolEpochPin,
)
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.block_resolver import ContentBlockResolver
from core.chat.errors import ChatError
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
    stamp_prompt_files_read,
)
from core.sessions import (
    SKILL_AVAILABLE_NOTE_PREFIX,
    ChatSession,
    SeenSkillsUpdate,
    SessionAddress,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat._run_state import ChatLoopDependencies, _RunExecutionContext
    from core.chat.request_runner import WireRequestRunner
    from core.skills.skills import SkillRegistry

from core.chat._request_epoch import _fold_prompt_epoch, _live_session_tool_grants

_LOGGER = get_logger("chat")


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

        self.tool_catalog = RequestToolCatalog(dependencies, self.targets)

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
        tool_pin = self.tool_catalog._stored_tool_epoch_pin(address)
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
        pin = (
            None
            if inputs.fresh_prompt_epoch
            else await self.tool_catalog._read_tool_epoch_pin(session)
        )
        catalog: LiveToolCatalog | None = None
        if pin is None or inputs.temporary_binding is not None:
            # Measured on the Run's primary route, like every Tool announcement,
            # even while a fallback target serves this request.
            catalog = await self.tool_catalog._live_tool_catalog(
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
                route_on_demand=inputs.tool_route_on_demand_tools,
                told=pin.names if pin is not None else (),
            )
        if pin is None:
            assert catalog is not None
            pin = await _CHAT_TRANSFORM_WORKERS.run(
                partial(ToolEpochPin.start, catalog, keep=inputs.keep_listed_tools)
            )
            if not inputs.fresh_prompt_epoch:
                pin = await self.tool_catalog._ensure_tool_epoch_pin(session, pin)
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
        (
            tools,
            allowed_tool_names,
            session_tool_grants,
            tool_contracts,
        ) = await self.tool_catalog._tool_fields(
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
