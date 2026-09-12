"""Admitted Run execution, fallback, Continuation and terminal cleanup."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from core.chat._message_history import (
    _append_input_origin_note,
    _append_reply_surface_note,
)
from core.chat._request_history import (
    _assign_session_image_references,
    _restore_in_run_tool_result_content,
    _serialize_continuation_request,
)
from core.chat._run_state import (
    RequestBuildInputs,
    _RequestState,
    _SessionSnapshot,
    create_run_execution_context,
)
from core.chat._skill_activation import _activate_triggered_skills
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.content_blocks import ContentBlock
from core.chat.continuation import (
    ContinuationCause,
    ContinuationState,
    ContinuationTracker,
    inject_continuation_reminder,
    normalize_interruption_cause,
    recover_continuation,
    render_continuation_reminder,
)
from core.chat.errors import (
    ChatError,
    ImageBudgetExceededError,
)
from core.chat.events import (
    _close_adapter,
    _emit_message_event,
    _persist_run_error,
    _timing_payload,
)
from core.chat.messages import ChatMessage
from core.chat.model_resolution import (
    _resolve_fallback_chain,
    _split_agent_model,
)
from core.chat.streaming import should_advance_model_fallback_chain
from core.chat.usage import (
    aggregate_session_usage,
    latest_session_context_usage,
)
from core.chat.wire_shaping import _strip_assistant_reasoning_fields
from core.extensions import HookContext, SessionRequestContext
from core.runs import (
    MODEL_FALLBACK_ACTIVATED_EVENT,
    USER_MESSAGE_EVENT,
    Run,
    RunInterruptedError,
)
from core.sessions import (
    ChatSession,
    SessionAddress,
    active_session_messages,
    editable_session_message_index,
)
from core.utils.errors import ConfigError, ProviderError, VBotError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat._agentic_progression import AgenticProgression
    from core.chat._request_builder import RequestBuilder
    from core.chat._run_state import (
        ChatLoopDependencies,
        ReflectionNotifier,
        SessionTitleNotifier,
        _RunExecutionContext,
        _RunRequest,
    )
    from core.compaction import CompactionService
    from core.compaction.run_coordination import CompactionRunCoordinator

_LOGGER = get_logger("chat")


class RunExecution:
    """Execute one admitted Run, including recovery, fallback and terminal cleanup."""

    def __init__(
        self,
        dependencies: ChatLoopDependencies,
        requests: RequestBuilder,
        progression: AgenticProgression,
        compaction_runs: CompactionRunCoordinator,
        compaction_service: CompactionService | None,
        reflection_service: ReflectionNotifier | None,
        session_title_service: SessionTitleNotifier | None,
    ) -> None:
        self._dependencies = dependencies
        self._requests = requests
        self._progression = progression
        self._compaction_runs = compaction_runs
        self._compaction_service = compaction_service
        self._reflection_service = reflection_service
        self._session_title_service = session_title_service

    async def _execute_run(
        self,
        run: Run,
        request: _RunRequest,
    ) -> ChatMessage:
        project_id = run.project_id
        session_address = SessionAddress(
            project_id=project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        await self._dependencies.sessions.record_run_start_async(session_address, run_id=run.id)
        if run.execution_owner is not None:
            await self._dependencies.sessions.record_run_owner_async(
                session_address,
                run_id=run.id,
                owner=run.execution_owner,
                input_id=run.execution_input_id,
            )
        session = await self._dependencies.sessions.get_async(session_address)
        await _CHAT_TRANSFORM_WORKERS.run(
            self._dependencies.sessions.record_run_kind,
            session_address,
            run.run_kind,
        )
        async with self._dependencies.sessions.write_lock(session_address):
            session_snapshot = await _SessionSnapshot.load(session)
            if request.edit_message_id is not None:
                session_snapshot.begin_edit(request.edit_message_id)
        prior_continuation: ContinuationState | None = None
        continuation_reminder: str | None = None
        continuation_tracker: ContinuationTracker | None = None
        if not request.internal or request.resume_process_restart:
            if request.edit_message_id is not None:
                recovered = None
            else:
                recovered = await recover_continuation(
                    session,
                    active_run_id=run.id,
                    canonical_messages=session_snapshot.active_messages,
                )
            if request.internal and recovered is not None and recovered.cause != "process_restart":
                recovered = None
            prior_continuation = recovered
            if prior_continuation is not None and not prior_continuation.active:
                continuation_reminder = render_continuation_reminder(
                    prior_continuation,
                    context_window=None,
                )
            if not request.internal or prior_continuation is not None:
                continuation_tracker = ContinuationTracker(
                    session,
                    run_id=run.id,
                    request=_serialize_continuation_request(request.content),
                    prior_state=prior_continuation,
                )
                await continuation_tracker.start()
        try:
            context = await create_run_execution_context(
                self._dependencies,
                self._requests,
                run,
                request,
                session=session,
                session_snapshot=session_snapshot,
                prior_continuation=prior_continuation,
                continuation_reminder=continuation_reminder,
                continuation_tracker=continuation_tracker,
            )
            if self._compaction_service is not None:
                run.set_compaction_state("idle")
            try:
                return await self._execute_run_impl(context)
            finally:
                if run.compaction_state == "pending":
                    run.emit("compaction_aborted", {"reason": "run_finished"})
        except BaseException as exc:
            if continuation_tracker is not None and not continuation_tracker.closed:
                cause: ContinuationCause = (
                    "user"
                    if run.cancel_requested and run.cancel_reason == "user"
                    else normalize_interruption_cause(exc)
                )
                await continuation_tracker.interrupt(cause)
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
        run_timing_started_at = datetime.now(UTC)
        run_timing_started_perf = time.perf_counter()
        _run_succeeded = True
        _run_interrupted = False
        run_error: BaseException | None = None
        completed_assistant: ChatMessage | None = None
        start_line_extras = ""
        if project_id is not None:
            start_line_extras += f" project={project_id}"
        if internal:
            start_line_extras += " internal"
        _LOGGER.info(
            "Run %s started (agent=%s session=%s model=%s connection=%s%s)",
            run.id,
            run.agent_id,
            run.session_id,
            agent.model,
            target.connection_id,
            start_line_extras,
        )

        try:
            session.begin_defer_notes()
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
                await _CHAT_TRANSFORM_WORKERS.run(
                    self._requests._announce_newly_available_skills,
                    run.agent_id,
                    run.session_id,
                    session,
                    agent,
                    context.skill_registry,
                    project_id,
                )
                async with self._dependencies.sessions.write_lock(session_address):
                    # Another admitted Run may have appended while this Run was
                    # queued for the Session lock. Refresh before assigning image
                    # references so each persisted image stays unique.
                    await context.session_snapshot.refresh(session)
                    reset_auto_title = False
                    if request.edit_message_id is not None:
                        editable_session_message_index(
                            active_session_messages(context.session_snapshot.messages),
                            request.edit_message_id,
                        )
                        reset_auto_title = not any(
                            message.role == "user"
                            for message in context.session_snapshot.active_messages
                        )
                    if internal:
                        if not isinstance(request.content, str):
                            raise ChatError("internal runs require string content")
                        _append_reply_surface_note(
                            session,
                            request.reply_surface,
                            messages=context.session_snapshot.active_messages,
                        )
                        session.add_note(request.content)
                        persisted_messages = session.take_deferred_notes()
                    elif request.input_already_persisted:
                        persisted_messages = []
                    else:
                        if request.content is None:
                            raise ChatError("content is required for non-retry runs")
                        _append_input_origin_note(session, request.input_origin)
                        _append_reply_surface_note(
                            session,
                            request.reply_surface,
                            messages=context.session_snapshot.active_messages,
                        )
                        user_message = ChatMessage.user(
                            _assign_session_image_references(
                                request.content,
                                context.session_snapshot.active_messages,
                            ),
                            sender=request.sender,
                        )
                        history_edit = (
                            ChatMessage.history_edit(request.edit_message_id)
                            if request.edit_message_id is not None
                            else None
                        )
                        persisted_messages = [
                            *([history_edit] if history_edit is not None else []),
                            *session.take_deferred_notes(),
                            user_message,
                        ]
                    await session.append_many_async(persisted_messages)
                    if request.edit_message_id is not None:
                        context.session_snapshot.commit_edit()
                        await session.clear_continuation_async()
                        context.prompt_cache_affinity_id = await _CHAT_TRANSFORM_WORKERS.run(
                            self._dependencies.sessions.rotate_prompt_cache_affinity_id,
                            session_address,
                        )
                        if reset_auto_title:
                            try:
                                await _CHAT_TRANSFORM_WORKERS.run(
                                    self._dependencies.sessions.reset_auto_title,
                                    session_address,
                                )
                            except Exception:
                                _LOGGER.warning(
                                    "Failed to reset generated Session title after history edit",
                                    exc_info=True,
                                )
                    await context.session_snapshot.refresh(session)
                    if not internal and not request.input_already_persisted:
                        _emit_message_event(run, USER_MESSAGE_EVENT, user_message)
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
                await session.flush_deferred_notes_async()
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
                        content=cast(str | list[ContentBlock], request.content),
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
                            await session.flush_deferred_notes_async()
                            await context.session_snapshot.refresh(session)
                    finally:
                        await session.flush_deferred_notes_async()
            run.raise_if_cancelled()
            try:
                context.request_state = await self._requests.build_request_state(
                    agent,
                    session,
                    inputs=RequestBuildInputs.from_context(context, target).with_session_messages(
                        context.session_snapshot.active_messages
                    ),
                )
            except ImageBudgetExceededError as exc:
                _run_succeeded = False
                run_error = exc
                await _persist_run_error(run, session, exc)
                raise
            if context.continuation_reminder is not None:
                assert context.prior_continuation is not None
                context.continuation_reminder = render_continuation_reminder(
                    context.prior_continuation,
                    context_window=self._requests.resolve_context_window(agent, target),
                )
                context.request_state = _RequestState(
                    inject_continuation_reminder(
                        context.request_state.messages,
                        context.continuation_reminder,
                    ),
                    context.request_state.tools,
                    context.request_state.allowed_tool_names,
                    context.request_state.session_tool_grants,
                    context.request_state.tool_contracts,
                )

            if self._compaction_service is not None:
                context.request_state = await self._compaction_runs.maybe_auto_compact_state(
                    context,
                    target,
                    usage=None,
                    allow_continuation=True,
                )

            try:
                completed_assistant = await self._progression._send_until_final(context, target)
                return completed_assistant
            except ProviderError as primary_exc:
                try:
                    (
                        completed_assistant,
                        chain_error,
                    ) = await self._advance_fallback_chain(context, run, session, primary_exc)
                except RunInterruptedError as exc:
                    _run_succeeded = False
                    _run_interrupted = True
                    run_error = exc
                    if isinstance(exc.result, ChatMessage):
                        completed_assistant = exc.result
                    raise
                if completed_assistant is not None:
                    return completed_assistant
                _run_succeeded = False
                run_error = chain_error
                await _persist_run_error(run, session, chain_error)
                raise chain_error from primary_exc
            except RunInterruptedError as exc:
                _run_succeeded = False
                _run_interrupted = True
                run_error = exc
                if isinstance(exc.result, ChatMessage):
                    completed_assistant = exc.result
                raise
            except (ChatError, ConfigError, VBotError) as exc:
                _run_succeeded = False
                run_error = exc
                await _persist_run_error(run, session, exc)
                raise
            except asyncio.CancelledError:
                run_error = asyncio.CancelledError()
                raise
            except BaseException as exc:
                _run_succeeded = False
                run_error = exc
                raise
        finally:
            outcome: Literal["success", "error", "cancelled"]
            if run.cancel_requested:
                outcome = "cancelled"
            elif _run_succeeded:
                outcome = "success"
            else:
                outcome = "error"
            run_status = (
                "interrupted"
                if _run_interrupted and outcome != "cancelled"
                else {"success": "completed", "error": "failed", "cancelled": "cancelled"}[outcome]
            )
            run_timing = _timing_payload(run_timing_started_at, run_timing_started_perf)
            _LOGGER.info(
                "Run %s %s (agent=%s session=%s duration_ms=%s iterations=%d "
                "tool_calls=%d input_tokens=%d output_tokens=%d)",
                run.id,
                run_status,
                run.agent_id,
                run.session_id,
                run_timing["duration_ms"],
                run.iteration_count,
                run.tool_call_count,
                run.input_token_total,
                run.output_token_total,
            )
            run_summary = ChatMessage.run_summary(
                run_id=run.id,
                work_id=run.work_id,
                status=run_status,
                timing=run_timing,
                iteration_count=run.iteration_count,
            )
            # Git-style change statistics for this run, computed from the
            # session-scoped content tracker (real before/after line diffs).
            # Peek first so an all-zero outcome persists explicitly and matches
            # the totals the live stream last showed; take consumes the deltas.
            # Best-effort: untracked files (too large, non-UTF-8) simply mean
            # the UI falls back to its per-tool-call counts.
            try:
                change_stats = self._dependencies.change_tracker.peek_run_stats(run.session_id)
                if change_stats is not None:
                    run.terminal_payload_extras["change_stats"] = change_stats
                    object.__setattr__(run_summary, "change_stats", change_stats)
                self._dependencies.change_tracker.take_run_stats(run.session_id)
            except Exception:
                _LOGGER.warning(
                    "Failed to compute change statistics for run %s", run.id, exc_info=True
                )
            run_summary_persisted = False
            owned_finalization_error: Exception | None = None
            try:
                await session.append_async(run_summary)
                run_summary_persisted = True
            except Exception as exc:
                # The model/tool outcome is already established. A failed
                # terminal annotation must not replace it or prevent the
                # Continuation journal from being finalized below.
                _LOGGER.warning("Failed to persist run summary for run %s", run.id, exc_info=True)
                if run.execution_owner is not None or request.temporary_binding is not None:
                    owned_finalization_error = exc
                    outcome = "error"
            if run_summary_persisted:
                try:
                    await context.session_snapshot.refresh(session)
                except Exception:
                    _LOGGER.warning(
                        "Failed to refresh Session snapshot after run %s summary",
                        run.id,
                        exc_info=True,
                    )
            if run.contributes_to_agent_activity and run_summary_persisted:
                try:
                    await _CHAT_TRANSFORM_WORKERS.run(
                        self._dependencies.sessions.record_terminal_run,
                        session_address,
                        run.id,
                        run_status,
                        run_summary.timestamp,
                    )
                except Exception:
                    # The canonical Run result is already durable in the transcript.
                    # A damaged activity sidecar must not turn successful agent work
                    # into a failed Run, but the missing notification is diagnosable.
                    _LOGGER.warning(
                        "Failed to record unread completion for run %s", run.id, exc_info=True
                    )
            if context.continuation_tracker is not None:
                try:
                    if (
                        outcome == "success"
                        and completed_assistant is not None
                        and not completed_assistant.interrupted
                    ):
                        await context.continuation_tracker.resolve()
                    else:
                        if outcome == "cancelled":
                            cause: ContinuationCause = (
                                "user" if run.cancel_reason == "user" else "internal"
                            )
                        else:
                            cause = (
                                context.continuation_tracker.interruption_cause
                                or normalize_interruption_cause(run_error)
                            )
                        await context.continuation_tracker.interrupt(cause)
                except Exception:
                    _LOGGER.warning(
                        "Failed to finalize Continuation state for run %s",
                        run.id,
                        exc_info=True,
                    )
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

            extension_registry = self._dependencies.get_extension_registry()
            if extension_registry is not None:
                session.begin_defer_notes()
                extension_ctx = HookContext(
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                    run_id=run.id,
                    add_note=session.add_note,
                )
                try:
                    binding = request.temporary_binding
                    if binding is not None:
                        try:
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
                                outcome,
                            )
                        except Exception as exc:
                            owned_finalization_error = owned_finalization_error or exc
                            _LOGGER.warning(
                                "Session run-finished callback failed for run %s",
                                run.id,
                                exc_info=True,
                            )
                    await extension_registry.dispatch_run_end(
                        extension_ctx,
                        session_id=run.session_id,
                        agent_id=run.agent_id,
                        outcome=outcome,
                    )
                finally:
                    async with self._dependencies.sessions.write_lock(session_address):
                        await session.flush_deferred_notes_async()
                        await context.session_snapshot.refresh(session)

            # Background reflection accounting. Fire-and-forget on the service's
            # side; a failure here must never mask the run outcome.
            if (
                self._reflection_service is not None
                and request.temporary_binding is None
                and request.temporary_parent_binding is None
                and run.execution_owner is None
            ):
                try:
                    self._reflection_service.notify_run_end(
                        run, agent, internal=internal, outcome=outcome
                    )
                except Exception:
                    _LOGGER.warning(
                        "Reflection run-end notification failed (run=%s)", run.id, exc_info=True
                    )

            await _close_adapter(target.adapter)
            if owned_finalization_error is not None:
                raise owned_finalization_error

    async def _advance_fallback_chain(
        self,
        context: _RunExecutionContext,
        run: Run,
        session: ChatSession,
        first_failure: ProviderError,
    ) -> tuple[ChatMessage | None, ProviderError]:
        """Advance through the resolved fallback chain after a provider failure.

        Returns ``(assistant, error_to_report)``. ``assistant`` is set when a
        chain candidate completed the Run. Otherwise ``error_to_report`` is the
        last actual send failure (the primary failure when no candidate ever
        ran) and the caller owns persisting it. Raises directly — already
        persisted — when a candidate fails in a way that must replace the
        reported outcome: a non-qualifying ProviderError (auth, billing,
        permission, content policy, context limits) or any Chat/Config/VBot
        error during a candidate Run. Candidate construction failures only log
        a warning and skip that candidate — one broken binding must never take
        down the whole escape route.
        """
        agent = context.agent
        chain = list(_resolve_fallback_chain(self._dependencies, agent))
        if not chain or not should_advance_model_fallback_chain(first_failure):
            return None, first_failure

        from_binding = agent.model
        last_failure: ProviderError = first_failure
        for binding, provider_id, connection_id in chain:
            _, candidate_model_id = _split_agent_model(binding)
            try:
                candidate_target = self._requests._create_model_target(
                    provider_id,
                    connection_id,
                    candidate_model_id,
                )
            except (ConfigError, VBotError) as construction_exc:
                _LOGGER.warning(
                    "Skipping fallback candidate %s for agent %s (%s)",
                    binding,
                    getattr(agent, "id", "?"),
                    construction_exc,
                )
                continue

            def _close_candidate_adapter(
                _adapter: Any = candidate_target.adapter,
            ) -> Any:
                return _close_adapter(_adapter)

            run.add_cancel_callback(_close_candidate_adapter)
            _LOGGER.info(
                "Model fallback activated (run=%s from=%s to=%s)",
                run.id,
                from_binding,
                binding,
            )
            run.emit(
                MODEL_FALLBACK_ACTIVATED_EVENT,
                {"from_model": from_binding, "to_model": binding},
            )
            await session.add_note_async(
                f"Model {from_binding} unavailable. Switched to {binding} for this run."
            )
            await context.session_snapshot.refresh(session)
            live_messages = context.request_state.messages if context.request_state else []
            context.request_state = await self._requests.build_request_state(
                agent,
                session,
                inputs=RequestBuildInputs.from_context(
                    context, candidate_target
                ).with_session_messages(context.session_snapshot.active_messages),
            )
            context.request_state.messages[:] = await _CHAT_TRANSFORM_WORKERS.run(
                _restore_in_run_tool_result_content,
                context.request_state.messages,
                live_messages,
                input_modalities=candidate_target.input_modalities,
                wire_media_types=candidate_target.wire_media_types,
                image_budget=context.image_budget,
            )
            if context.continuation_reminder is not None:
                assert context.prior_continuation is not None
                context.continuation_reminder = render_continuation_reminder(
                    context.prior_continuation,
                    context_window=self._requests.resolve_context_window(agent, candidate_target),
                )
                context.request_state = _RequestState(
                    inject_continuation_reminder(
                        context.request_state.messages,
                        context.continuation_reminder,
                    ),
                    context.request_state.tools,
                    context.request_state.allowed_tool_names,
                    context.request_state.session_tool_grants,
                    context.request_state.tool_contracts,
                )
            # Rebuilding applies the fallback route's media and Tool
            # capabilities. The persisted previous tool cycle may still carry
            # Provider-specific reasoning, which must never cross the Provider
            # boundary.
            _strip_assistant_reasoning_fields(context.request_state.messages)
            if self._compaction_service is not None:
                context.request_state = await self._compaction_runs.maybe_auto_compact_state(
                    context,
                    candidate_target,
                    usage=None,
                    allow_continuation=True,
                )
            try:
                completed = await self._progression._send_until_final(context, candidate_target)
                return completed, last_failure
            except RunInterruptedError:
                raise
            except ProviderError as candidate_failure:
                if not should_advance_model_fallback_chain(candidate_failure):
                    await _persist_run_error(run, session, candidate_failure)
                    raise
                last_failure = candidate_failure
                from_binding = binding
                continue
            except (ChatError, ConfigError, VBotError) as candidate_failure:
                await _persist_run_error(run, session, candidate_failure)
                raise
            finally:
                await _close_adapter(candidate_target.adapter)
        return None, last_failure
