"""Admitted Run execution, fallback and terminal cleanup."""

from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.chat._message_history import (
    _add_context_note,
    _append_input_origin_note,
    _append_reply_surface_note,
)
from core.chat._request_history import _assign_session_image_references
from core.chat._request_targets import _resolved_model_reference
from core.chat._run_state import (
    RequestBuildInputs,
    _SessionSnapshot,
    create_run_execution_context,
    report_subagent_user_input,
    subagent_input_reply_target,
    switches_subagent_reply_to_user,
)
from core.chat._scratch_context import (
    SCRATCH_ANNOUNCED_META_KEY,
    SCRATCH_RUN_KINDS,
    scratch_note,
    scratch_note_visible,
    uses_scratch,
)
from core.chat._skill_activation import _activate_triggered_skills
from core.chat._step_outcomes import OUTPUT_INTEGRITY_RECOVERY_NOTE
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.content_blocks import masked_content
from core.chat.errors import (
    ChatError,
    ImageBudgetExceededError,
)
from core.chat.events import (
    _close_adapter,
    _emit_display_records,
    _emit_message_event,
    _persist_run_error,
)
from core.chat.messages import (
    PARENT_AGENT_INPUT_SYSTEM_REMINDER,
    SUBAGENT_REPLY_TO_USER_SYSTEM_REMINDER,
    ChatMessage,
    ModelFallback,
)
from core.chat.model_resolution import (
    _resolve_fallback_chain,
    _split_agent_model,
    parse_bare_model,
)
from core.chat.recovery import RouteHealth, RoutePatience
from core.chat.streaming import model_fallback_scope
from core.chat.usage import (
    aggregate_session_usage,
    latest_session_context_usage,
)
from core.chat.wire_shaping import _strip_assistant_reasoning_fields
from core.extensions import HookContext, SessionRequestContext
from core.performance import measure, session_track
from core.runs import (
    MODEL_FALLBACK_ACTIVATED_EVENT,
    USER_MESSAGE_EVENT,
    Run,
    RunInterruptedError,
    RunStatus,
)
from core.sessions import (
    ChatSession,
    SeenSkillsUpdate,
    SessionAddress,
    editable_session_message_index,
)
from core.tools.scratch_areas import ScratchUnavailableError
from core.utils.errors import ConfigError, ProviderError, VBotError
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp
from core.utils.workers import settle_before_cancelling

if TYPE_CHECKING:
    from core.chat._agentic_progression import AgenticProgression
    from core.chat._request_builder import RequestBuilder
    from core.chat._run_state import (
        ChatLoopDependencies,
        ReflectionNotifier,
        SessionTitleNotifier,
        _ModelTarget,
        _RunExecutionContext,
        _RunRequest,
    )
    from core.compaction import CompactionService

_LOGGER = get_logger("chat")


@dataclass(frozen=True)
class _ChainRoute:
    """One route of a Run's Model fallback chain, the primary route first."""

    binding: str
    provider_id: str
    connection_id: str
    model_id: str
    # Exact Provider/Model/Connection/account scope (``_ModelTarget.model_reference``).
    reference: str

    @property
    def connection_scope(self) -> str:
        """The Provider, Connection and account the route's credentials belong to."""
        return f"{self.provider_id}::{self.reference.partition('::')[2]}"


def _resolve_chain_routes(
    dependencies: ChatLoopDependencies, agent: Any, primary_reference: str
) -> list[_ChainRoute]:
    """Resolve and deduplicate the Agent's exact fallback routes. Blocking."""
    routes = []
    seen = {primary_reference}
    for binding, provider_id, connection_id in _resolve_fallback_chain(dependencies, agent):
        _, model_id = _split_agent_model(binding)
        try:
            reference = _resolved_model_reference(
                dependencies, provider_id, connection_id, model_id
            )
        except (ConfigError, VBotError, KeyError) as error:
            _LOGGER.warning(
                "Skipping unresolvable fallback model %r for agent %s (%s)",
                binding,
                getattr(agent, "id", "?"),
                error,
            )
            continue
        if reference in seen:
            continue
        seen.add(reference)
        routes.append(
            _ChainRoute(
                binding=binding,
                provider_id=provider_id,
                connection_id=connection_id,
                model_id=model_id,
                reference=reference,
            )
        )
    return routes


class RunExecution:
    """Execute one admitted Run, including recovery, fallback and terminal cleanup."""

    def __init__(
        self,
        dependencies: ChatLoopDependencies,
        requests: RequestBuilder,
        progression: AgenticProgression,
        compaction_service: CompactionService | None,
        reflection_service: ReflectionNotifier | None,
        session_title_service: SessionTitleNotifier | None,
    ) -> None:
        self._dependencies = dependencies
        self._scratch_failures = LoggedConditions()
        # Routes that recently gave up, shared by every Run of this Chat Loop.
        self._route_health = RouteHealth()
        self._requests = requests
        self._progression = progression
        self._compaction_service = compaction_service
        self._reflection_service = reflection_service
        self._session_title_service = session_title_service

    async def _execute_run(
        self,
        run: Run,
        request: _RunRequest,
    ) -> ChatMessage:
        with measure(
            "chat.run",
            track=session_track(run.agent_id, run.session_id, run.project_id),
            name="run",
            args={"run_id": run.id, "run_kind": str(run.run_kind)},
        ):
            return await self._execute_admitted_run(run, request)

    async def _execute_admitted_run(
        self,
        run: Run,
        request: _RunRequest,
    ) -> ChatMessage:
        run.internal = request.internal
        extension_registry = self._dependencies.get_extension_registry()
        binding = request.temporary_binding
        if extension_registry is not None and binding is not None:

            async def notify_owner(status: RunStatus) -> None:
                await extension_registry.dispatch_session_run_finished(
                    binding,
                    self._dependencies.tools,
                    SessionRequestContext(
                        binding=binding,
                        run_id=run.id,
                        agent_id=run.agent_id,
                        session_id=run.session_id,
                        execution_owner=run.execution_owner,
                    ),
                    "success"
                    if status == RunStatus.COMPLETED
                    else "cancelled"
                    if status == RunStatus.CANCELLED
                    else "error",
                )

            run.add_completion_observer(notify_owner)
        project_id = run.project_id
        session_address = SessionAddress(
            project_id=project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        session = (await self._dependencies.sessions.get_async(session_address)).for_run(run.id)
        async with self._dependencies.sessions.write_lock(session_address):
            session_snapshot = await _SessionSnapshot.load(session)
            if request.edit_message_id is not None:
                session_snapshot.begin_edit(request.edit_message_id)
        try:
            context = await create_run_execution_context(
                self._dependencies,
                self._requests,
                run,
                request,
                session=session,
                session_snapshot=session_snapshot,
            )
            if self._compaction_service is not None:
                run.set_compaction_state("idle")
            try:
                return await self._execute_run_impl(context)
            finally:
                if run.compaction_state == "pending":
                    run.emit("compaction_aborted", {"reason": "run_finished"})
        except BaseException as exc:
            if (
                isinstance(exc, Exception)
                and not isinstance(exc, RunInterruptedError)
                and not run.cancel_requested
            ):
                try:
                    await _persist_run_error(run, session, exc)
                except Exception:
                    _LOGGER.warning("Failed to persist error for run %s", run.id, exc_info=True)
            raise

    async def _execute_run_impl(
        self,
        context: _RunExecutionContext,
    ) -> ChatMessage:
        run = context.run
        request = context.request
        session = context.session
        agent = context.agent
        target = context.primary_target
        project_id = context.project_id
        session_address = SessionAddress(
            project_id=project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        internal = request.internal
        # The Runs domain logs the one terminal line; Chat supplies the route.
        run.model = parse_bare_model(agent.model)
        start_line_extras = ""
        if project_id is not None:
            start_line_extras += f" project={project_id}"
        if internal:
            start_line_extras += " internal=true"
        _LOGGER.debug(
            "Run started (run=%s agent=%s session=%s model=%s connection=%s%s)",
            run.id,
            run.agent_id,
            run.session_id,
            agent.model,
            target.connection_id,
            start_line_extras,
        )

        try:
            session.begin_defer_notes()
            # Commits with the first write that persists the Skill note (or in its place).
            record_seen_skills: SeenSkillsUpdate | None = None
            try:
                extension_registry = self._dependencies.get_extension_registry()
                if extension_registry is not None:
                    extension_ctx = HookContext(
                        session_id=run.session_id,
                        agent_id=run.agent_id,
                        run_id=run.id,
                        add_note=session.add_note,
                    )
                    await extension_registry.dispatch_run_start(
                        extension_ctx,
                        session_id=run.session_id,
                        agent_id=run.agent_id,
                    )

                run.raise_if_cancelled()
                skill_announcement = await _CHAT_TRANSFORM_WORKERS.run(
                    self._requests._plan_skill_announcement,
                    session_address,
                    agent,
                    context.skill_registry,
                )
                if skill_announcement.note is not None:
                    session.add_note(skill_announcement.note)
                record_seen_skills = skill_announcement.record_seen
                # Persists with the input, so the first request already tells it.
                prompt_block_change = await self._requests.plan_prompt_block_change(context)
                if prompt_block_change is not None:
                    session.add_note(prompt_block_change)
                # Speech-to-text runs before the Session lock and persists with
                # the message, so history shows what a voice message said. Stop
                # abandons it: the input persists without a transcription, and
                # the Run ends after that.
                user_content = request.content
                if not internal and not request.input_already_persisted and user_content:
                    try:
                        user_content = await self._requests.media.transcribe_user_audio(
                            user_content
                        )
                    except asyncio.CancelledError:
                        if not run.cancel_requested:
                            raise
                async with self._dependencies.sessions.write_lock(session_address):
                    # Another admitted Run may have appended while this Run was
                    # queued for the Session lock. Refresh before assigning image
                    # references so each persisted image stays unique.
                    await context.session_snapshot.refresh(session)
                    if request.edit_message_id is not None:
                        editable_session_message_index(
                            context.session_snapshot.active_lineage, request.edit_message_id
                        )
                    reply_target = subagent_input_reply_target(context, request)
                    reply_to_user = await switches_subagent_reply_to_user(
                        context, reply_target, self._dependencies.sessions
                    )
                    if internal:
                        if not isinstance(request.content, str):
                            raise ChatError("internal runs require string content")
                        _add_context_note(session, request.context_note)
                        _append_reply_surface_note(
                            session,
                            request.reply_surface,
                            messages=context.session_snapshot.active_messages,
                        )
                        session.add_note(request.content)
                        persisted_messages = session.take_deferred_notes()
                    elif request.input_already_persisted:
                        persisted_messages = session.take_deferred_notes()
                    else:
                        if user_content is None:
                            raise ChatError("content is required for non-retry runs")
                        _add_context_note(session, request.context_note)
                        _append_input_origin_note(session, request.input_origin)
                        if request.parent_agent_input:
                            session.add_note(PARENT_AGENT_INPUT_SYSTEM_REMINDER)
                        if reply_to_user:
                            session.add_note(SUBAGENT_REPLY_TO_USER_SYSTEM_REMINDER)
                        _append_reply_surface_note(
                            session,
                            request.reply_surface,
                            messages=context.session_snapshot.active_messages,
                        )
                        user_message = ChatMessage.user(
                            _assign_session_image_references(
                                masked_content(user_content, self._dependencies.secret_masker),
                                context.session_snapshot.active_messages,
                            ),
                            sender=request.sender,
                            input_origin=request.input_origin,
                        )
                        persisted_messages = [*session.take_deferred_notes(), user_message]
                    # One transaction persists the input, records the announced
                    # Skills and the Sub-Agent's answer destination, so cancellation
                    # cannot split the input from its routing change. An edit also
                    # replaces the edited history in that transaction.
                    if request.edit_message_id is not None:
                        await context.session_snapshot.apply_edit(
                            session,
                            persisted_messages,
                            seen_skills=record_seen_skills,
                            subagent_reply_target=reply_target,
                        )
                    elif persisted_messages:
                        await context.session_snapshot.append(
                            session,
                            persisted_messages,
                            seen_skills=record_seen_skills,
                            subagent_reply_target=reply_target,
                        )
                    elif record_seen_skills is not None:
                        await _CHAT_TRANSFORM_WORKERS.run(
                            self._dependencies.sessions.record_seen_skills,
                            session_address,
                            record_seen_skills,
                        )
                    record_seen_skills = None
                    if not persisted_messages:
                        await context.session_snapshot.refresh(session)
                    if reply_to_user:
                        report_subagent_user_input(
                            self._dependencies, session_address, user_message.id
                        )
                    _emit_display_records(run, persisted_messages)
                    if not internal and not request.input_already_persisted:
                        # A Stop during speech-to-text still persisted the input.
                        _emit_message_event(
                            run, USER_MESSAGE_EVENT, user_message, allow_after_cancel=True
                        )
                    if request.input_persisted_hook is not None:
                        try:
                            request.input_persisted_hook()
                        except Exception:
                            _LOGGER.warning(
                                "Input-persistence callback failed for run %s",
                                run.id,
                                exc_info=True,
                            )
            finally:
                # Deferred notes left by a failed input append still persist, and
                # an unrecorded Skill note carries its seen-Skill change along.
                await session.append_many_async(
                    session.take_deferred_notes(), seen_skills=record_seen_skills
                )
            if (
                not internal
                and request.temporary_binding is None
                and request.temporary_parent_binding is None
                and run.execution_owner is None
            ):
                if self._session_title_service is not None:
                    self._session_title_service.notify_user_message(
                        agent_id=run.agent_id,
                        session_id=run.session_id,
                        project_id=project_id,
                        agent=agent,
                        run_id=run.id,
                    )
                if isinstance(request.content, str):
                    session.activated_skill_contents(context.session_snapshot.active_messages)
                    session.begin_defer_notes()
                    try:
                        await _CHAT_TRANSFORM_WORKERS.run(
                            _activate_triggered_skills,
                            agent,
                            session,
                            request.content,
                            context.skill_registry,
                        )
                        async with self._dependencies.sessions.write_lock(session_address):
                            await context.session_snapshot.flush_deferred_notes(session)
                    finally:
                        await session.flush_deferred_notes_async()
            run.raise_if_cancelled()
            request_build_started: float | None = time.perf_counter()
            try:
                context.request_state = await self._requests.build_request_state(
                    agent,
                    session,
                    inputs=RequestBuildInputs.from_context(context, target).with_session_messages(
                        context.session_snapshot.active_messages
                    ),
                )
                context.session_snapshot.forget_arrived_notes()
                try:
                    checked_scratch = await self._announce_scratch(context)
                except ScratchUnavailableError:
                    # Scratch's owner logged why the folder is unavailable.
                    # The valid request built above remains ready to run.
                    pass
                except RunInterruptedError:
                    raise
                except Exception as error:
                    # Announcement persistence and rebuilding are optional too;
                    # no Scratch failure can mask the Agent's requested work.
                    if self._scratch_failures.started(session_address, type(error).__name__):
                        _LOGGER.warning(
                            "Skipped Scratch announcement "
                            "(agent=%s session=%s project=%s error=%s)",
                            session_address.agent_id,
                            session_address.session_id,
                            session_address.project_id,
                            type(error).__name__,
                        )
                else:
                    if checked_scratch and self._scratch_failures.ended(session_address):
                        _LOGGER.info(
                            "Scratch announcement recovered (agent=%s session=%s project=%s)",
                            session_address.agent_id,
                            session_address.session_id,
                            session_address.project_id,
                        )
            except ImageBudgetExceededError as exc:
                await _persist_run_error(run, session, exc)
                raise

            context.request_build_started = request_build_started

            return await self._serve_with_fallback(context, run, session)

        except BaseException as exc:
            # Preparation can fail before Provider progression installs its own
            # handlers. Completion hooks and durable summaries need that failure too.
            if (
                isinstance(exc, Exception)
                and not isinstance(exc, RunInterruptedError)
                and not run.cancel_requested
            ):
                try:
                    await _persist_run_error(run, session, exc)
                except Exception:
                    _LOGGER.warning("Failed to persist error for run %s", run.id, exc_info=True)
            raise
        finally:
            self._register_run_end(context)
            await settle_before_cancelling(self._finish_run(context))

    async def _finish_run(self, context: _RunExecutionContext) -> None:
        run, session = context.run, context.session
        try:
            # Detach and compute final statistics once, off the Event Loop. Run
            # cancellation must not skip worker admission or lose the result:
            # reverted edits still need their explicit zero terminal totals.
            try:
                change_stats = await _CHAT_TRANSFORM_WORKERS.run(
                    self._dependencies.change_tracker.take_run_stats, (session.address, run.id)
                )
                if change_stats is not None:
                    run.terminal_payload_extras["change_stats"] = change_stats
            except Exception:
                _LOGGER.warning(
                    "Failed to compute change statistics for run %s", run.id, exc_info=True
                )
            # The Run's completion deletes the stored draft; no write may follow it.
            await context.stream_draft.settle()
            # Session usage totals ride every terminal event so accessors can
            # keep their session-level token/cache display current without
            # re-fetching history. Diagnostics only — never mask the outcome.
            try:
                await context.session_snapshot.refresh(session)
                terminal_messages = context.session_snapshot.messages
                run.terminal_payload_extras["session_usage"] = aggregate_session_usage(
                    terminal_messages
                )
                terminal_context_usage = latest_session_context_usage(
                    context.session_snapshot.active_messages
                )
                if terminal_context_usage is not None:
                    run.terminal_payload_extras["context_usage"] = terminal_context_usage
            except Exception:
                _LOGGER.warning(
                    "Failed to aggregate Session Usage for run %s", run.id, exc_info=True
                )

        finally:
            await _close_adapter(context.primary_target.adapter)

    def _register_run_end(self, context: _RunExecutionContext) -> None:
        """Register both lifecycle notifications before asynchronous cleanup can suspend."""
        run, request, session = context.run, context.request, context.session
        extension_registry = self._dependencies.get_extension_registry()
        if extension_registry is not None:

            async def notify_extensions(status: RunStatus) -> None:
                session.begin_defer_notes()
                extension_ctx = HookContext(
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                    run_id=run.id,
                    add_note=session.add_note,
                )
                try:
                    await extension_registry.dispatch_run_end(
                        extension_ctx,
                        session_id=run.session_id,
                        agent_id=run.agent_id,
                        outcome="success"
                        if status == RunStatus.COMPLETED
                        else "cancelled"
                        if status == RunStatus.CANCELLED
                        else "error",
                    )
                finally:
                    async with self._dependencies.sessions.write_lock(session.address):
                        await context.session_snapshot.flush_deferred_notes(session)

            run.add_completion_finalizer(notify_extensions)

        # A review forks only after the manager commits this Run. Forking
        # during executor cleanup would exclude the still-running turn,
        # including any Compaction checkpoints it just created.
        if (
            self._reflection_service is not None
            and request.temporary_binding is None
            and request.temporary_parent_binding is None
            and run.execution_owner is None
        ):
            reflection_service = self._reflection_service

            async def notify_reflection(status: RunStatus) -> None:
                try:
                    reflection_service.notify_run_end(
                        run,
                        context.agent,
                        internal=request.internal,
                        outcome="success"
                        if status == RunStatus.COMPLETED
                        else "cancelled"
                        if status == RunStatus.CANCELLED
                        else "error",
                    )
                except Exception:
                    _LOGGER.warning(
                        "Reflection run-end notification failed (run=%s)", run.id, exc_info=True
                    )

            run.add_completion_observer(notify_reflection)

    async def _announce_scratch(self, context: _RunExecutionContext) -> bool:
        """Tell a file-capable Run about its folder before its first Model request."""
        scratch = self._dependencies.scratch_areas
        state = context.request_state
        if scratch is None or state is None or context.run.run_kind not in SCRATCH_RUN_KINDS:
            return False
        catalog = await self._requests.tool_catalog.live_tool_catalog(
            context,
            known=state.allowed_tool_names,
            told=state.tool_epoch.known_names if state.tool_epoch is not None else (),
        )
        tool_names = set(catalog.offered_by_name)
        if context.request.tool_restriction is not None:
            tool_names.intersection_update(context.request.tool_restriction)
        denial = context.request.tool_denial_resolver
        if denial is not None:
            tool_names = {name for name in tool_names if denial(name) is None}
        if not uses_scratch(context.run.run_kind, tool_names):
            return False
        session = context.session
        folder = await scratch.folder_for(session.address)
        sessions = self._dependencies.sessions
        async with sessions.write_lock(session.address):
            await context.session_snapshot.refresh(session)
            announced = await sessions.metadata_value_async(
                session.address, SCRATCH_ANNOUNCED_META_KEY
            )
            own = str(folder.own)
            prior = announced if isinstance(announced, dict) else {}
            changed = bool(prior) and (
                folder.recreated
                or prior.get("own") != own
                or prior.get("incarnation") != folder.incarnation
            )
            visible = await _CHAT_TRANSFORM_WORKERS.run(
                scratch_note_visible,
                context.session_snapshot.active_messages,
                prior.get("note_id"),
            )
            if prior and not changed and visible:
                return True
            note = scratch_note(folder)
            await context.session_snapshot.append(session, [note])
            await sessions.run_async(
                sessions.mutate_metadata,
                session.address,
                lambda metadata: metadata.update(
                    {
                        SCRATCH_ANNOUNCED_META_KEY: {
                            "own": own,
                            "incarnation": folder.incarnation,
                            "note_id": note.id,
                        }
                    }
                ),
            )
        context.request_state = await self._requests.rebuild_live_request_state(
            context.agent,
            session,
            inputs=RequestBuildInputs.from_context(
                context, context.primary_target
            ).with_session_messages(context.session_snapshot.active_messages),
            live_messages=state.messages,
        )
        return True

    async def _serve_with_fallback(
        self,
        context: _RunExecutionContext,
        run: Run,
        session: ChatSession,
    ) -> ChatMessage:
        """Serve the Run on the primary route, then on the Model fallback chain.

        A route gives up once its recovery budget is spent; the next route then
        serves the rest of the Run. Routes that recently gave up in any Run of
        this Chat Loop (:class:`RouteHealth`) go last, so a new Run starts on
        the first route expected to answer. A failure scoped to an account
        skips later routes on the same Connection and account; a failure no
        other route can fix ends the Run. When no route completes, the last
        failure determines the outcome. Candidate construction failures only
        log a warning and skip that candidate: one broken binding must never
        take down the whole escape route.
        """
        agent = context.agent
        primary = context.primary_target
        health = self._route_health
        primary_route = _ChainRoute(
            binding=agent.model,
            provider_id=primary.provider_id,
            connection_id=primary.connection_id,
            model_id=primary.model_id,
            reference=primary.model_reference,
        )
        chain = await _CHAT_TRANSFORM_WORKERS.run(
            _resolve_chain_routes, self._dependencies, agent, primary.model_reference
        )
        served: set[str] = set()
        excluded_scopes: set[str] = set()
        acquired: dict[str, _ModelTarget] = {}

        def remaining() -> list[_ChainRoute]:
            candidates = [
                route
                for route in (primary_route, *chain)
                if route.reference not in served and route.connection_scope not in excluded_scopes
            ]
            ready = [route for route in candidates if health.retry_at(route.reference) is None]
            return ready + [route for route in candidates if route not in ready]

        async def close_candidate(reference: str) -> None:
            # Cancellation callbacks and normal cleanup share ownership, so a
            # prefetched Adapter closes once even when both paths reach it.
            candidate = acquired.pop(reference, None)
            if candidate is not None:
                await settle_before_cancelling(_close_adapter(candidate.adapter))

        async def next_target() -> tuple[_ChainRoute, _ModelTarget] | None:
            # Look ahead only as far as the next constructable route. An unusable
            # escape route must not shorten the route that can actually serve.
            for candidate in remaining():
                if candidate is primary_route:
                    return candidate, primary
                if candidate.reference in acquired:
                    return candidate, acquired[candidate.reference]
                try:
                    target = await self._requests.targets.create(
                        candidate.provider_id,
                        candidate.connection_id,
                        candidate.model_id,
                        public_model=candidate.binding,
                    )
                except (ConfigError, VBotError, KeyError) as construction_exc:
                    served.add(candidate.reference)
                    _LOGGER.warning(
                        "Skipping fallback candidate %s for agent %s (%s)",
                        candidate.binding,
                        getattr(agent, "id", "?"),
                        construction_exc,
                    )
                    continue
                acquired[candidate.reference] = target
                adapters.push_async_callback(close_candidate, candidate.reference)

                def close_on_cancel(reference: str = candidate.reference) -> Any:
                    return close_candidate(reference)

                run.add_cancel_callback(close_on_cancel)
                return candidate, target
            return None

        previous: _ChainRoute | None = None
        first_failure: ProviderError | RunInterruptedError | None = None
        last_failure: ProviderError | RunInterruptedError | None = None
        async with AsyncExitStack() as adapters:
            while selected := await next_target():
                route, target = selected
                served.add(route.reference)
                try:
                    fallback_follows = await next_target() is not None
                    context.recovery.serve(
                        route.reference,
                        RoutePatience.for_route(
                            fallback_follows=fallback_follows,
                            failed_recently=health.failed_recently(route.reference),
                        ),
                    )
                    if route is not primary_route or previous is not None:
                        await self._switch_route(
                            context,
                            run,
                            session,
                            from_route=previous or primary_route,
                            to_route=route,
                            target=target,
                            failure=last_failure,
                        )
                    try:
                        completed = await self._progression._send_until_final(context, target)
                    except (ProviderError, RunInterruptedError) as failure:
                        if context.recovery.completed(route.reference):
                            health.recovered(route.reference)
                        first_failure = first_failure or failure
                        last_failure = failure
                        scope = model_fallback_scope(failure)
                        if scope is None:
                            break
                        if scope == "connection":
                            excluded_scopes.add(route.connection_scope)
                        elif fallback_follows and (
                            isinstance(failure, RunInterruptedError) or failure.retryable
                        ):
                            health.failed(route.reference)
                        previous = route
                        continue
                    except (ChatError, ConfigError, VBotError) as failure:
                        await _persist_run_error(run, session, failure)
                        raise
                    health.recovered(route.reference)
                    return completed
                finally:
                    if target is not primary:
                        await close_candidate(route.reference)
        # A route always runs first, so a failure ended the loop.
        assert first_failure is not None and last_failure is not None
        if not isinstance(last_failure, RunInterruptedError):
            await _persist_run_error(run, session, last_failure)
        if last_failure is first_failure:
            raise last_failure
        raise last_failure from first_failure

    async def _switch_route(
        self,
        context: _RunExecutionContext,
        run: Run,
        session: ChatSession,
        *,
        from_route: _ChainRoute,
        to_route: _ChainRoute,
        target: _ModelTarget,
        failure: ProviderError | RunInterruptedError | None,
    ) -> None:
        """Record the switch to ``to_route`` and rebuild the request for ``target``."""
        retry_at = self._route_health.retry_at(from_route.reference)
        retry_at_text = (
            format_canonical_timestamp(datetime.fromtimestamp(retry_at, UTC))
            if retry_at is not None
            else None
        )
        run.model = parse_bare_model(to_route.binding)
        if failure is None:
            # The route gave up in an earlier Run and still cools down.
            _LOGGER.info(
                "Model route cooling down; Run starts on fallback (run=%s from=%s to=%s until=%s)",
                run.id,
                from_route.binding,
                to_route.binding,
                retry_at_text,
            )
        else:
            _LOGGER.warning(
                "Model fallback activated (run=%s from=%s to=%s)",
                run.id,
                from_route.binding,
                to_route.binding,
            )
        run.emit(
            MODEL_FALLBACK_ACTIVATED_EVENT,
            {
                "from_model": from_route.binding,
                "to_model": to_route.binding,
                **({"retry_at": retry_at_text} if retry_at_text is not None else {}),
            },
        )
        await session.add_note_async(
            f"Model {from_route.binding} unavailable. Switched to {to_route.binding} for this run.",
            model_fallback=ModelFallback(
                from_model=from_route.binding,
                to_model=to_route.binding,
                retry_at=retry_at_text,
            ),
        )
        if isinstance(failure, RunInterruptedError) and failure.result is not None:
            await session.add_note_async(OUTPUT_INTEGRITY_RECOVERY_NOTE)
        await context.session_snapshot.refresh(session)
        context.request_state = await self._requests.rebuild_live_request_state(
            context.agent,
            session,
            inputs=RequestBuildInputs.from_context(context, target).with_session_messages(
                context.session_snapshot.active_messages
            ),
            live_messages=context.request_state.messages if context.request_state else [],
        )
        # Rebuilding applies the new route's media and Tool capabilities. The
        # persisted previous tool cycle may still carry Provider-specific
        # reasoning, which must never cross the Provider boundary.
        _strip_assistant_reasoning_fields(context.request_state.messages)
