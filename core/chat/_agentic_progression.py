"""Agentic Model and Tool progression within a Run."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from core.chat._request_builder import _run_prompt_method
from core.chat._run_state import _RequestState
from core.chat._step_outcomes import (
    MAX_IDENTICAL_FAILED_TOOL_CALLS,
    MAX_STREAM_CONTINUATIONS,
    MAX_TOOL_FINALIZATION_VIOLATIONS,
    STREAM_RECOVERY_NOTE,
    TOOL_FINALIZATION_DISABLED_FAILURE_CODE,
    TOOL_FINALIZATION_NOTE,
    TOOL_ITERATION_LIMIT_FAILURE_CODE,
    _combined_interrupted_result,
    _FailedToolCallCircuitBreaker,
    _prepare_completed_assistant,
    _terminal_outcome_error,
    _terminal_tool_failure,
    _usage_token_count,
)
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.errors import ChatError
from core.chat.events import _emit_assistant_events
from core.chat.messages import (
    ChatMessage,
    JsonObject,
)
from core.chat.tool_dispatch import (
    ToolDispatchContext,
    _dispatch_tool_calls,
    _fail_tool_calls_without_dispatch,
)
from core.chat.usage import (
    add_session_turn_usage,
    aggregate_session_usage,
)
from core.chat.wire_shaping import (
    _assistant_continuation_dict,
    _message_to_request_dict,
    _notes_to_request_messages,
    limit_request_images,
)
from core.debug import DebugContext
from core.extensions import HookContext, SessionRequestContext
from core.providers.adapter import (
    TERMINAL_OUTCOME_TOOL_CALLS,
    request_input_budget,
)
from core.providers.errors import ProviderRequestTooLargeError
from core.providers.reasoning import REASONING_REPLAY_NONE
from core.runs import (
    MODEL_STEP_USAGE_EVENT,
    RUN_CHANGE_STATS_EVENT,
    Run,
    RunInterruptedError,
)
from core.sessions import (
    SessionAddress,
    project_tool_context_id,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat._request_builder import RequestBuilder
    from core.chat._run_state import ChatLoopDependencies, _ModelTarget, _RunExecutionContext
    from core.chat.request_runner import WireRequestRunner
    from core.compaction import CompactionService
    from core.compaction.run_coordination import CompactionRunCoordinator

_LOGGER = get_logger("chat")


class AgenticProgression:
    """Advance completed Model and Tool boundaries until a Run has a final answer."""

    def __init__(
        self,
        dependencies: ChatLoopDependencies,
        requests: RequestBuilder,
        wire_requests: WireRequestRunner,
        compaction_runs: CompactionRunCoordinator,
        compaction_service: CompactionService | None,
        *,
        max_tool_iterations: int,
        streaming: bool,
    ) -> None:
        self._dependencies = dependencies
        self._requests = requests
        self._wire_requests = wire_requests
        self._compaction_runs = compaction_runs
        self._compaction_service = compaction_service
        self._max_tool_iterations = max_tool_iterations
        self._streaming = streaming

    @asynccontextmanager
    async def _assistant_persistence_boundary(
        self,
        run: Run,
        *,
        project_id: str | None,
        preserve_after_cancel: bool,
    ) -> AsyncIterator[None]:
        """Acquire the Session lock without losing already streamed readable output."""
        write_lock = self._dependencies.sessions.write_lock(
            SessionAddress(project_id=project_id, agent_id=run.agent_id, session_id=run.session_id)
        )
        while True:
            try:
                await write_lock.__aenter__()
                break
            except asyncio.CancelledError:
                if not (preserve_after_cancel and run.cancel_requested):
                    raise
                # Run cancellation is forceful, but this completed readable
                # stream is already visible. Defer that cancellation only until
                # the competing writer releases the append boundary.
        try:
            yield
        finally:
            # The concrete Session lock never suppresses body exceptions and
            # needs no exception details to release its ContextVar ownership.
            await write_lock.__aexit__(None, None, None)

    async def _send_until_final(
        self,
        context: _RunExecutionContext,
        target: _ModelTarget,
    ) -> ChatMessage:
        if context.request_state is None:
            raise AssertionError("Run request state must be built before Model progression")
        run = context.run
        session = context.session
        agent = context.agent
        project_id = context.project_id
        session_address = SessionAddress(
            project_id=project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        state = context.request_state
        messages = state.messages
        tools = state.tools
        replay_policy = target.replay_policy
        session_usage = run.terminal_payload_extras.get("session_usage")
        if not isinstance(session_usage, dict):
            session_usage = aggregate_session_usage(context.session_snapshot.messages)
            run.terminal_payload_extras["session_usage"] = session_usage
        tool_iteration_count = 0
        stream_continuation_count = 0
        interruption_chain: list[ChatMessage] = []
        emitted_change_stats: dict[str, object] | None = None
        failed_tool_call_breaker = _FailedToolCallCircuitBreaker()
        tool_finalization_reason: str | None = None
        tool_finalization_violation_count = 0
        tool_catalog_revision = -1
        while True:
            run.raise_if_cancelled()
            registry = self._dependencies.tools
            if tool_finalization_reason is None and registry.revision != tool_catalog_revision:
                tool_catalog_revision = registry.revision
                refreshed = await _run_prompt_method(
                    self._dependencies.get_system_prompts(),
                    "provider_tool_definitions_async",
                    "provider_tool_definitions",
                    agent,
                    session_tool_grants=state.session_tool_grants,
                )
                tools = await self._requests._route_tool_definitions(
                    refreshed,
                    tool_access=agent.tool_access,
                    input_modalities=target.input_modalities,
                    wire_media_types=target.wire_media_types,
                )
                allowed_names = tuple(str(tool["name"]) for tool in tools)
                contracts = await _CHAT_TRANSFORM_WORKERS.run(
                    registry.contracts_for_provider_definitions,
                    tools,
                )
                state = _RequestState(
                    messages, tools, allowed_names, state.session_tool_grants, contracts
                )
                context.request_state = state
            async with self._dependencies.sessions.write_lock(session_address):
                session.begin_defer_notes()
                binding = context.request.temporary_binding
                extension_registry = self._dependencies.get_extension_registry()
                try:
                    if binding is not None and extension_registry is not None:
                        delivery = await extension_registry.dispatch_session_before_request(
                            binding,
                            self._dependencies.tools,
                            SessionRequestContext(
                                binding=binding,
                                run_id=run.id,
                                agent_id=run.agent_id,
                                session_id=run.session_id,
                                execution_owner=run.execution_owner,
                            ),
                        )
                        if delivery is not None:
                            previous_delivery = (
                                await self._dependencies.sessions.lookup_delivery_receipt(
                                    binding.address,
                                    binding.generation_id,
                                    binding.owner_name,
                                    delivery.delivery_id,
                                )
                            )
                            delivery_note = ChatMessage.note("\n\n".join(delivery.entries))
                            await self._dependencies.sessions.append_messages_with_receipts_async(
                                binding.address,
                                generation_id=binding.generation_id,
                                owner_name=binding.owner_name,
                                messages=[delivery_note],
                                deduplicate_carrier=True,
                                receipts=[
                                    (
                                        0,
                                        delivery.delivery_id,
                                        delivery.content_hash,
                                        delivery.effect_kind,
                                        "note",
                                    )
                                ],
                            )
                            if previous_delivery is None:
                                messages.extend(_notes_to_request_messages([delivery_note]))
                            await extension_registry.acknowledge_session_delivery(
                                binding,
                                self._dependencies.tools,
                                SessionRequestContext(
                                    binding=binding,
                                    run_id=run.id,
                                    agent_id=run.agent_id,
                                    session_id=run.session_id,
                                    execution_owner=run.execution_owner,
                                ),
                                delivery,
                            )
                except Exception:
                    await session.flush_deferred_notes_async()
                    raise
                try:
                    self._dependencies.deliver_background_completions(run, session)
                except Exception:
                    _LOGGER.warning(
                        "Background completion injection failed for run %s",
                        run.id,
                        exc_info=True,
                    )
                finally:
                    await session.flush_deferred_notes_async()
                    await context.session_snapshot.refresh(session)
                pending_notes = session.drain_pending_notes()
            if pending_notes:
                messages.extend(_notes_to_request_messages(pending_notes))
            extension_registry = self._dependencies.get_extension_registry()
            messages_for_request = [dict(message) for message in messages]
            if extension_registry is not None:
                session.begin_defer_notes()
                extension_ctx = HookContext(
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                    run_id=run.id,
                    add_note=session.add_note,
                )
                try:
                    messages_for_request = await extension_registry.dispatch_context(
                        extension_ctx,
                        messages=messages_for_request,
                    )
                finally:
                    async with self._dependencies.sessions.write_lock(session_address):
                        await session.flush_deferred_notes_async()
                        await context.session_snapshot.refresh(session)

            messages_for_request = await _CHAT_TRANSFORM_WORKERS.run(
                limit_request_images,
                messages_for_request,
                budget=context.image_budget,
                remember=True,
            )

            # The next ordinal is derived from the canonical completed count.
            # Failed requests therefore do not consume an Iteration number.
            request_iteration_number = run.iteration_count + 1
            if hasattr(target.adapter, "set_debug_context"):
                target.adapter.set_debug_context(
                    DebugContext(
                        run_id=run.id,
                        agent_id=run.agent_id,
                        session_id=run.session_id,
                        provider_id=target.provider_id,
                        connection_id=target.connection_id,
                        model_id=target.model_id,
                        streaming=self._streaming,
                        iteration_number=request_iteration_number,
                    )
                )
            _LOGGER.debug(
                "Iteration %d requested (run=%s model=%s messages=%d)",
                request_iteration_number,
                run.id,
                target.model_id,
                len(messages_for_request),
            )
            request_tools = [] if tool_finalization_reason is not None else tools
            step_started_perf = time.perf_counter()
            workspace = getattr(agent, "workspace", None)
            output_cwd = (
                context.project_cwd
                if context.project_cwd is not None
                else Path(workspace)
                if workspace
                else None
            )
            while True:
                run.raise_if_cancelled()
                request_context_usage = await _CHAT_TRANSFORM_WORKERS.run(
                    context.context_usage.project,
                    messages_for_request,
                    adapter=target.adapter,
                    model_id=target.model_id,
                    tools=request_tools,
                    scope=context.prompt_cache_affinity_id,
                )
                self._requests._raise_if_measured_context_exhausted(
                    context.session_snapshot.active_messages,
                    messages_for_request,
                    [] if tool_finalization_reason is not None else tools,
                    agent,
                    run,
                    target,
                    context_usage=request_context_usage,
                )
                try:
                    with request_input_budget(
                        target.model_id, int(request_context_usage["tokens"])
                    ):
                        assistant_step = await self._wire_requests.send_assistant_request(
                            agent,
                            target.adapter,
                            target.model_id,
                            target.model_reference,
                            messages_for_request,
                            [] if tool_finalization_reason is not None else tools,
                            run,
                            prompt_cache_affinity_id=context.prompt_cache_affinity_id,
                            chunk_timeout_seconds=target.chunk_timeout_seconds,
                            continuation_tracker=context.continuation_tracker,
                            output_cwd=output_cwd,
                            provider_id=target.provider_id,
                        )
                except ProviderRequestTooLargeError:
                    smaller = await _CHAT_TRANSFORM_WORKERS.run(
                        context.image_budget.project,
                        messages_for_request,
                        remember=True,
                        force=True,
                    )
                    if smaller == messages_for_request:
                        raise
                    messages_for_request = smaller
                    continue
                break
            # This is the sole mutation point for the Iteration count: one
            # completed request/response pair, independent of how many Tool
            # Calls or readable Assistant blocks the response contains.
            run.iteration_count = request_iteration_number
            assistant_message = assistant_step.message
            terminal_outcome = assistant_step.terminal_outcome
            recovery = assistant_step.recovery
            recovery_note = assistant_step.recovery_note
            if (
                not assistant_message.interrupted
                and _terminal_outcome_error(
                    terminal_outcome, has_tool_calls=bool(assistant_message.tool_calls)
                )
                is None
            ):
                await _CHAT_TRANSFORM_WORKERS.run(
                    context.image_budget.record_delivered, messages_for_request
                )
            # Both an interrupted partial and a finished readable stream may
            # already be visible when Cancel arrives. The latter can race only
            # while acquiring the append lock; neither may vanish from History.
            preserve_after_cancel = assistant_message.interrupted or (
                self._streaming
                and not assistant_message.tool_calls
                and (
                    (
                        bool(assistant_message.content)
                        if isinstance(assistant_message.content, str)
                        else False
                    )
                    or bool(assistant_message.reasoning)
                )
            )
            if not preserve_after_cancel:
                run.raise_if_cancelled()
            assistant_message = await _CHAT_TRANSFORM_WORKERS.run(
                _prepare_completed_assistant,
                assistant_message,
                messages_for_request,
                output_cwd,
                int(request_context_usage["tokens"]),
            )
            assistant_request_message = await _CHAT_TRANSFORM_WORKERS.run(
                _assistant_continuation_dict,
                assistant_message,
                replay_policy=(
                    replay_policy if assistant_step.replay_reasoning else REASONING_REPLAY_NONE
                ),
            )
            assistant_request_messages: list[JsonObject] = [assistant_request_message]
            if (
                not assistant_step.replay_reasoning
                and not assistant_request_message.get("content")
                and not assistant_request_message.get("tool_calls")
            ):
                # A Reasoning-only integrity boundary becomes empty after native
                # Reasoning is stripped. Do not send an empty Assistant entry.
                assistant_request_messages = []
            assert isinstance(assistant_message.usage, dict)
            await _CHAT_TRANSFORM_WORKERS.run(
                context.context_usage.observe,
                assistant_message.usage,
                messages_for_request,
                adapter=target.adapter,
                model_id=target.model_id,
                tools=request_tools,
                scope=context.prompt_cache_affinity_id,
            )
            assistant_context_usage = await _CHAT_TRANSFORM_WORKERS.run(
                context.context_usage.project,
                [*messages_for_request, *assistant_request_messages],
                adapter=target.adapter,
                model_id=target.model_id,
                tools=request_tools,
                scope=context.prompt_cache_affinity_id,
            )
            assistant_message = replace(
                assistant_message,
                usage={**assistant_message.usage, "context_usage": assistant_context_usage},
            )
            assert assistant_message.usage is not None
            run.input_token_total += _usage_token_count(assistant_message.usage, "input_tokens")
            run.output_token_total += _usage_token_count(assistant_message.usage, "output_tokens")
            _LOGGER.debug(
                "Iteration %d completed (run=%s duration_ms=%d input_tokens=%d "
                "output_tokens=%d tool_calls=%d)",
                request_iteration_number,
                run.id,
                round((time.perf_counter() - step_started_perf) * 1000),
                _usage_token_count(assistant_message.usage, "input_tokens"),
                _usage_token_count(assistant_message.usage, "output_tokens"),
                len(assistant_message.tool_calls or ()),
            )
            # Hold the per-session append lock from the assistant tool-call
            # message through its tool results so a writer on another accessor
            # (a channel observed note, session.link_channel) cannot land between
            # them and break the tool-cycle ordering invariant.
            repeated_failed_tool: str | None = None
            async with self._assistant_persistence_boundary(
                run,
                project_id=project_id,
                preserve_after_cancel=preserve_after_cancel,
            ):
                preserved_cancelled_output = run.cancel_requested and preserve_after_cancel
                await session.append_async(assistant_message)
                await context.session_snapshot.refresh(session)
                run.terminal_payload_extras["context_usage"] = assistant_context_usage
                session_usage = add_session_turn_usage(session_usage, assistant_message.usage)
                run.terminal_payload_extras["session_usage"] = session_usage
                run.emit(
                    MODEL_STEP_USAGE_EVENT,
                    {
                        "usage": dict(assistant_message.usage),
                        "session_usage": dict(session_usage),
                        "context_usage": dict(assistant_context_usage),
                        "iteration_count": run.iteration_count,
                    },
                    allow_after_cancel=preserved_cancelled_output,
                )
                if context.continuation_tracker is not None:
                    await context.continuation_tracker.record_assistant_boundary(
                        message_id=assistant_message.id,
                        reasoning=assistant_message.reasoning,
                        content=(
                            assistant_message.content
                            if isinstance(assistant_message.content, str)
                            else None
                        ),
                        interrupted=assistant_message.interrupted,
                        tool_calls=assistant_message.tool_calls,
                    )
                if not self._streaming:
                    _emit_assistant_events(run, assistant_message)
                messages.extend(assistant_request_messages)
                if (recovery != "none" or interruption_chain) and (
                    isinstance(assistant_message.content, str)
                    or assistant_message.reasoning is not None
                ):
                    interruption_chain.append(assistant_message)

                if not assistant_message.tool_calls:
                    if preserved_cancelled_output:
                        # The already visible response is durable; end the turn
                        # without new provider work (no auto-compaction). The Run
                        # manager sees ``cancel_requested`` and marks it cancelled.
                        return assistant_message
                    if recovery == "interrupt":
                        raise RunInterruptedError(
                            assistant_message.interruption_cause or "internal",
                            result=_combined_interrupted_result(
                                interruption_chain,
                                output_cwd=output_cwd,
                            ),
                        )
                    if recovery == "continue":
                        if stream_continuation_count >= MAX_STREAM_CONTINUATIONS:
                            raise RunInterruptedError(
                                assistant_message.interruption_cause or "internal",
                                result=_combined_interrupted_result(
                                    interruption_chain,
                                    output_cwd=output_cwd,
                                ),
                            )
                        await session.add_note_async(recovery_note or STREAM_RECOVERY_NOTE)
                        await context.session_snapshot.refresh(session)
                        stream_continuation_count += 1
                        continue
                    terminal_error = _terminal_outcome_error(
                        terminal_outcome,
                        has_tool_calls=False,
                    )
                    if terminal_error is not None:
                        raise terminal_error
                    break

                stream_continuation_count = 0
                finalization_violation = tool_finalization_reason is not None
                finalization_request_reason: str | None = None
                tool_limit_reached = (
                    not finalization_violation
                    and terminal_outcome == TERMINAL_OUTCOME_TOOL_CALLS
                    and tool_iteration_count >= self._max_tool_iterations
                )

                session.begin_defer_notes()
                try:
                    tool_dispatch_context = ToolDispatchContext(
                        registry=self._dependencies.tools,
                        extension_registry=self._dependencies.get_extension_registry(),
                        agent=agent,
                        session=session,
                        run=run,
                        nesting_depth=self._requests.nesting_depth,
                        vbot_root=Path(self._dependencies.get_system_prompts().vbot_root),
                        data_root=Path(self._dependencies.storage.data_dir),
                        project_cwd=context.project_cwd,
                        project_id=project_id,
                        skill_project_id=context.skill_project_id,
                        skill_registry=context.skill_registry,
                        tool_restriction=context.request.tool_restriction,
                        tool_denial_resolver=context.request.tool_denial_resolver,
                        base_allowed_tools=state.allowed_tool_names,
                        session_tool_grants=state.session_tool_grants,
                        tool_contracts=state.tool_contracts,
                        change_tracker=self._dependencies.change_tracker,
                        allow_owned_effects=context.request.temporary_binding is not None,
                    )
                    terminal_error = _terminal_outcome_error(
                        terminal_outcome,
                        has_tool_calls=True,
                    )
                    will_dispatch_tools = (
                        terminal_outcome == TERMINAL_OUTCOME_TOOL_CALLS
                        and not finalization_violation
                        and not tool_limit_reached
                    )
                    if not will_dispatch_tools and context.continuation_tracker is not None:
                        await context.continuation_tracker.record_tool_starts(
                            assistant_message.tool_calls
                        )
                    if terminal_outcome == TERMINAL_OUTCOME_TOOL_CALLS:
                        if finalization_violation:
                            tool_messages = _fail_tool_calls_without_dispatch(
                                tool_dispatch_context,
                                assistant_message.tool_calls,
                                code=TOOL_FINALIZATION_DISABLED_FAILURE_CODE,
                                message=(
                                    "Tool execution is disabled for the remainder of this Run. "
                                    "This Tool was not executed; provide the final answer without "
                                    "issuing another Tool Call."
                                ),
                                retryable=False,
                            )
                            media_outputs: list[JsonObject] = []
                        elif tool_limit_reached:
                            finalization_request_reason = (
                                "the Run reached its limit of "
                                f"{self._max_tool_iterations} dispatched Tool iterations"
                            )
                            tool_messages = _fail_tool_calls_without_dispatch(
                                tool_dispatch_context,
                                assistant_message.tool_calls,
                                code=TOOL_ITERATION_LIMIT_FAILURE_CODE,
                                message=(
                                    f"The Run reached its limit of {self._max_tool_iterations} "
                                    "dispatched Tool iterations. This Tool was not executed; "
                                    "provide the final answer without issuing another Tool Call."
                                ),
                                retryable=False,
                            )
                            media_outputs = []
                        else:
                            tool_iteration_count += 1
                            tool_messages, media_outputs = await _dispatch_tool_calls(
                                tool_dispatch_context,
                                assistant_message.tool_calls,
                                continuation_tracker=context.continuation_tracker,
                            )
                    else:
                        failure_code, failure_message = _terminal_tool_failure(terminal_outcome)
                        tool_messages = _fail_tool_calls_without_dispatch(
                            tool_dispatch_context,
                            assistant_message.tool_calls,
                            code=failure_code,
                            message=failure_message,
                        )
                        media_outputs = []
                    tool_request_messages: list[JsonObject] = []
                    for tool_message in tool_messages:
                        assert tool_message.tool_call_id is not None
                        request_message = _message_to_request_dict(tool_message)
                        messages.append(request_message)
                        tool_request_messages.append(request_message)
                    repeated_failed_tool = failed_tool_call_breaker.observe(
                        assistant_message.tool_calls,
                        tool_messages,
                        tool_dispatch_context.registry,
                    )
                    if repeated_failed_tool is not None and finalization_request_reason is None:
                        finalization_request_reason = (
                            f"Tool {repeated_failed_tool!r} repeated the same failed Call "
                            f"{MAX_IDENTICAL_FAILED_TOOL_CALLS} times"
                        )
                    if finalization_request_reason is not None:
                        session.add_note(
                            TOOL_FINALIZATION_NOTE.format(reason=finalization_request_reason)
                        )
                    deferred_notes = session.take_deferred_notes()
                    batch_messages = [*tool_messages, *deferred_notes]
                    binding = context.request.temporary_binding
                    extension_registry = self._dependencies.get_extension_registry()
                    if binding is not None and tool_dispatch_context.delivery_receipts:
                        await self._dependencies.sessions.append_messages_with_receipts_async(
                            binding.address,
                            generation_id=binding.generation_id,
                            owner_name=binding.owner_name,
                            messages=batch_messages,
                            receipts=[
                                (
                                    next(
                                        index
                                        for index, message in enumerate(batch_messages)
                                        if message.role == "tool"
                                        and message.tool_call_id == tool_call_id
                                    ),
                                    receipt_id,
                                    content_hash,
                                    effect_kind,
                                    "tool",
                                )
                                for (
                                    tool_call_id,
                                    receipt_id,
                                    content_hash,
                                    effect_kind,
                                ) in tool_dispatch_context.delivery_receipts
                            ],
                        )
                    else:
                        await session.append_many_async(batch_messages)
                    await context.session_snapshot.refresh(session)
                    for tool_message in tool_messages:
                        assert tool_message.tool_call_id is not None
                        tool_dispatch_context.notify_result_persisted(tool_message.tool_call_id)
                        loaded_project_id = project_tool_context_id(tool_message)
                        if project_id is None and loaded_project_id is not None:
                            await self._requests._apply_project_skill_context(
                                context, loaded_project_id
                            )
                    if context.continuation_tracker is not None:
                        await context.continuation_tracker.record_tool_results(tool_messages)
                    if terminal_error is not None:
                        raise terminal_error
                    await self._requests._attach_tool_result_content(
                        tool_request_messages,
                        media_outputs,
                        target.input_modalities,
                        target.wire_media_types,
                    )
                    if binding is not None:
                        if extension_registry is None:
                            raise ChatError(
                                "This Session is no longer available. "
                                "Check its state through its Extension."
                            )
                        decision = await extension_registry.reconcile_session_tool_batch(
                            binding,
                            self._dependencies.tools,
                            SessionRequestContext(
                                binding=binding,
                                run_id=run.id,
                                agent_id=run.agent_id,
                                session_id=run.session_id,
                                execution_owner=run.execution_owner,
                            ),
                            tool_dispatch_context.delivery_receipts,
                            tuple(
                                message.tool_call_id
                                for message in tool_messages
                                if message.tool_call_id is not None
                            ),
                            tool_dispatch_context.turn_end_requested,
                        )
                        continuation = decision.continuation
                        if continuation is not None:
                            continuation_note = ChatMessage.note("\n\n".join(continuation.entries))
                            await self._dependencies.sessions.append_messages_with_receipts_async(
                                binding.address,
                                generation_id=binding.generation_id,
                                owner_name=binding.owner_name,
                                messages=[continuation_note],
                                deduplicate_carrier=True,
                                receipts=[
                                    (
                                        0,
                                        continuation.delivery_id,
                                        continuation.content_hash,
                                        continuation.effect_kind,
                                        "note",
                                    )
                                ],
                            )
                            await extension_registry.acknowledge_session_delivery(
                                binding,
                                self._dependencies.tools,
                                SessionRequestContext(
                                    binding=binding,
                                    run_id=run.id,
                                    agent_id=run.agent_id,
                                    session_id=run.session_id,
                                    execution_owner=run.execution_owner,
                                ),
                                continuation,
                            )
                            continuation_messages = _notes_to_request_messages([continuation_note])
                            messages.extend(continuation_messages)
                            tool_request_messages.extend(continuation_messages)
                            await context.session_snapshot.refresh(session)
                        if decision.end:
                            return assistant_message
                    # Honored only after every sibling tool result is persisted, so
                    # this cooperative stop never itself dangles the assistant turn.
                    # It is not a full persistence guarantee, though: the forceful
                    # task.cancel() in Run.request_cancel (and a process kill) can
                    # still interrupt the dispatch above with tool_calls left
                    # unanswered on disk. That persisted state is not corruption —
                    # request assembly repairs it via _repair_dangling_tool_calls,
                    # synthesizing the missing results before any provider sees it.
                    run.raise_if_cancelled()
                finally:
                    await session.flush_deferred_notes_async()

            # Live git-style change statistics after each dispatched Tool round,
            # so the UI shows the same real totals during the Run that the
            # terminal payload will carry instead of summing per-call estimates.
            # Emitted only when the totals changed; best-effort like every
            # transient projection.
            if self._dependencies.change_tracker is not None:
                current_change_stats = self._dependencies.change_tracker.peek_run_stats(
                    run.session_id
                )
                if current_change_stats != emitted_change_stats:
                    emitted_change_stats = current_change_stats
                    run.emit(RUN_CHANGE_STATS_EVENT, {"change_stats": current_change_stats})

            # Bound the live request view as well, before Compaction estimates or
            # another Tool cycle. Canonical artifacts remain available to reopen.
            messages[:] = await _CHAT_TRANSFORM_WORKERS.run(
                limit_request_images, messages, budget=context.image_budget, remember=True
            )
            continuation_request_messages = await _CHAT_TRANSFORM_WORKERS.run(
                limit_request_images,
                [*messages_for_request, assistant_request_message, *tool_request_messages],
                budget=context.image_budget,
                remember=True,
            )
            tool_context_usage = await _CHAT_TRANSFORM_WORKERS.run(
                context.context_usage.project,
                continuation_request_messages,
                adapter=target.adapter,
                model_id=target.model_id,
                tools=tools,
                scope=context.prompt_cache_affinity_id,
            )
            run.terminal_payload_extras["context_usage"] = tool_context_usage

            if finalization_violation:
                tool_finalization_violation_count += 1
                if tool_finalization_violation_count >= MAX_TOOL_FINALIZATION_VIOLATIONS:
                    # The Model already received one correlated disabled-Tool
                    # failure and ignored the boundary again. Complete gracefully
                    # instead of creating an unbounded recovery loop.
                    return assistant_message
            if finalization_request_reason is not None:
                tool_finalization_reason = finalization_request_reason

            if self._compaction_service is not None:
                compacted_state = await self._compaction_runs.maybe_auto_compact_state(
                    context,
                    target,
                    usage=assistant_message.usage,
                    continuation_request_messages=continuation_request_messages,
                    context_usage=tool_context_usage,
                    allow_continuation=True,
                )
                context.request_state = compacted_state
                state = compacted_state
                messages = compacted_state.messages
                tools = compacted_state.tools

        if self._compaction_service is not None:
            await self._compaction_runs.maybe_auto_compact_state(
                context,
                target,
                usage=assistant_message.usage,
                continuation_request_messages=[
                    *messages_for_request,
                    assistant_request_message,
                ],
                context_usage=assistant_context_usage,
                continue_same_run=False,
            )
        return assistant_message
