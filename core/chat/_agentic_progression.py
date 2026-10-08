"""Agentic Model and Tool progression within a Run."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from core.chat._boundaries import _finish_visible_boundary
from core.chat._queued_input import persist_steering_input, rebuild_after_steering
from core.chat._run_state import _AssistantStep
from core.chat._step_outcomes import (
    MAX_IDENTICAL_FAILED_TOOL_CALLS,
    MAX_TOOL_FINALIZATION_VIOLATIONS,
    STREAM_RECOVERY_NOTE,
    TOOL_FINALIZATION_DISABLED_FAILURE_CODE,
    TOOL_FINALIZATION_DISABLED_FAILURE_MESSAGE,
    TOOL_FINALIZATION_NOTE,
    TOOL_ITERATION_LIMIT_FAILURE_CODE,
    TOOL_ITERATION_LIMIT_FAILURE_MESSAGE,
    _combined_interrupted_result,
    _offered_tool_calls,
    _prepare_completed_assistant,
    _terminal_outcome_error,
    _terminal_tool_failure,
    _usage_token_count,
    _with_offered_tool_names,
    tool_result_facts,
)
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.errors import ChatError
from core.chat.messages import (
    ChatMessage,
    JsonObject,
    ToolCall,
    _new_message_id,
)
from core.chat.tool_dispatch import (
    ToolDispatchContext,
    ToolRound,
    _fail_tool_calls_without_dispatch,
)
from core.chat.usage import (
    CONTEXT_ESTIMATION_FIELD,
    add_session_turn_usage,
    aggregate_session_usage,
)
from core.chat.wire_shaping import (
    _assistant_continuation_dict,
    _message_to_request_dict,
    _notes_to_request_messages,
    _parse_response_tool_calls,
    extend_request_with_notes,
    limit_request_images,
)
from core.debug import DebugContext
from core.extensions import HookContext, SessionRequestContext
from core.performance import measure, record_span, session_track
from core.providers.adapter import (
    TERMINAL_OUTCOME_OUTPUT_TRUNCATED,
    TERMINAL_OUTCOME_TOOL_CALLS,
    TerminalOutcome,
    request_input_budget,
)
from core.providers.errors import ProviderRequestTooLargeError
from core.runs import (
    MODEL_STEP_USAGE_EVENT,
    RUN_CHANGE_STATS_EVENT,
    QueuedRunItem,
    Run,
    RunInterruptedError,
)
from core.sessions import (
    SessionAddress,
    SessionWriteLeaseScope,
    project_tool_context_id,
)
from core.tools import model_tool_name
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
    ) -> None:
        self._dependencies = dependencies
        self._requests = requests
        self._wire_requests = wire_requests
        self._compaction_runs = compaction_runs
        self._compaction_service = compaction_service
        self._max_tool_iterations = max_tool_iterations

    def _tool_iteration_limit(self, context: _RunExecutionContext) -> int:
        """Return the loop's iteration limit, narrowed by the Run's own limit."""
        run_limit = context.request.max_tool_iterations
        if run_limit is None:
            return self._max_tool_iterations
        return min(self._max_tool_iterations, run_limit)

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
        # Tool Calls start while the Model still streams. However the
        # progression ends, none of them outlives it. They start before the
        # progression takes the Session write lock to persist their turn and
        # waits for them there; the lease scope lets them write to the Session
        # under that lease instead of waiting for it.
        with SessionWriteLeaseScope():
            async with AsyncExitStack() as tool_rounds:
                return await self._advance_until_final(context, target, tool_rounds)

    async def _advance_until_final(
        self,
        context: _RunExecutionContext,
        target: _ModelTarget,
        tool_rounds: AsyncExitStack,
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
        interruption_chain = context.interruption_chain
        emitted_change_stats: dict[str, object] | None = None
        # The Tool registry revision and prompt epoch whose Tool changes were
        # last announced; ``None`` evaluates at the next request boundary.
        evaluated_tool_key: tuple[int, str] | None = None
        # Set when a final answer is followed by selected steering input.
        awaiting_steering = False
        track = session_track(run.agent_id, run.session_id, project_id)
        run_args = {"run_id": run.id}
        while True:
            # Step 1 began before the Run assembled its initial request state.
            build_started = context.request_build_started or time.perf_counter()
            context.request_build_started = None
            run.raise_if_cancelled()
            async with self._dependencies.sessions.write_lock(session_address):

                async def persist_input(item: QueuedRunItem) -> None:
                    await persist_steering_input(context, item, self._dependencies)

                delivered = await _finish_visible_boundary(
                    self._dependencies.run_manager.deliver_steering(run, persist_input),
                    run,
                    True,
                )
            if awaiting_steering and not delivered:
                # The selected input was withdrawn before delivery. Without new
                # User input, the persisted final answer remains the Run result.
                if self._dependencies.run_manager.pending_steering(run):
                    continue
                break
            awaiting_steering = False
            if delivered:
                await rebuild_after_steering(context, target, self._requests)
                assert context.request_state is not None
                state = context.request_state
                messages = state.messages
                tools = state.tools
                evaluated_tool_key = None
            run.raise_if_cancelled()
            # Tool changes are measured once per Run, after steering, and when
            # the Tool registry changed; the notes announcing them join this
            # request's boundary below, so the Tool list itself stays pinned.
            tool_catalog = None
            tool_epoch = state.tool_epoch
            if context.tool_progress.finalization_reason is None and tool_epoch is not None:
                tool_key = (self._dependencies.tools.revision, tool_epoch.pin.epoch)
                if tool_key != evaluated_tool_key:
                    evaluated_tool_key = tool_key
                    tool_catalog = await self._requests.live_tool_catalog(
                        context, known=tool_epoch.allowed_names, told=tool_epoch.known_names
                    )
            boundary_notes: list[ChatMessage] = []
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
                                boundary_notes.append(delivery_note)
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
                    try:
                        self._dependencies.deliver_background_completions(run, session)
                    except Exception:
                        _LOGGER.warning(
                            "Background completion injection failed for run %s",
                            run.id,
                            exc_info=True,
                        )
                    if tool_catalog is not None:
                        state = await self._requests.announce_tool_changes(
                            state,
                            tool_catalog,
                            session,
                            unlisted_tool_calls=context.primary_target.unlisted_tool_calls,
                            list_announced=(
                                target is not context.primary_target
                                or not target.unlisted_tool_calls
                            ),
                        )
                        context.request_state = state
                        tools = state.tools
                finally:
                    await context.session_snapshot.flush_deferred_notes(session)
                boundary_notes.extend(session.drain_pending_notes())
            # Rendered as the next request replays them, grouped with the notes
            # this request already ends with, so the history bytes stay stable.
            extend_request_with_notes(
                messages,
                boundary_notes,
                context.session_snapshot.active_messages,
                replay_policy=target.replay_policy,
                agent_model=target.model_reference,
            )
            extension_registry = self._dependencies.get_extension_registry()
            # The request shares the live message dicts read-only: context hooks
            # receive their own copies, image limiting copies on write, and
            # Provider adapters never mutate the messages they are sent. Image
            # limiting always returns a new list, so later in-place updates of
            # the live list leave this request view unchanged.
            messages_for_request = messages
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
                        messages=messages,
                    )
                finally:
                    async with self._dependencies.sessions.write_lock(session_address):
                        await context.session_snapshot.flush_deferred_notes(session)

            messages_for_request = await _CHAT_TRANSFORM_WORKERS.run(
                limit_request_images,
                messages_for_request,
                budget=context.image_budget,
                image_limit=target.max_request_images,
                remember=True,
            )

            # The next ordinal is derived from the canonical completed count.
            # Failed requests therefore do not consume an Iteration number.
            request_iteration_number = run.iteration_count + 1
            target.adapter.set_debug_context(
                DebugContext(
                    run_id=run.id,
                    agent_id=run.agent_id,
                    session_id=run.session_id,
                    provider_id=target.provider_id,
                    connection_id=target.connection_id,
                    model_id=target.model_id,
                    streaming=True,
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
            # Finalization requests keep the pinned Tool list: dropping it would
            # change the Provider prompt-cache prefix. The finalization note and
            # the bounded refusal below stop further Tool use instead.
            # Calls resolve against the listed Tools plus those announced as
            # enabled; a Tool announced as removed keeps its own name.
            offered_tool_names = [str(tool.get("name")) for tool in tools]
            removed_tool_names: frozenset[str] = frozenset()
            if state.tool_epoch is not None:
                offered_tool_names.extend(state.tool_epoch.announced_names)
                removed_tool_names = state.tool_epoch.removed_names
            step_started_perf = time.perf_counter()
            record_span(
                "chat.request_build",
                build_started,
                ended=step_started_perf,
                track=track,
                name="request build",
                args=run_args,
            )
            workspace = getattr(agent, "workspace", None)
            output_cwd = (
                context.project_cwd
                if context.project_cwd is not None
                else Path(workspace)
                if workspace
                else None
            )
            tool_dispatch_context = ToolDispatchContext(
                registry=self._dependencies.tools,
                extension_registry=self._dependencies.get_extension_registry(),
                agent=agent,
                session=session,
                run=run,
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
                removed_tool_names=removed_tool_names,
                change_tracker=self._dependencies.change_tracker,
                allow_owned_effects=context.request.temporary_binding is not None,
            )
            # The Assistant turn is named before the request, so the Tool Calls
            # that start while it streams report the turn they belong to.
            assistant_message_id = _new_message_id()
            tool_round: ToolRound | None = None
            if (
                context.tool_progress.finalization_reason is None
                and context.tool_progress.iteration_count < self._tool_iteration_limit(context)
            ):
                # Only a turn whose Tool Calls may run starts them early; the
                # others are refused once the response is complete.
                tool_round = ToolRound(
                    tool_dispatch_context,
                    assistant_message_id=assistant_message_id,
                    iteration_number=request_iteration_number,
                )
                tool_rounds.push_async_callback(tool_round.aclose)

            def start_tool_calls(
                raw_calls: list[JsonObject],
                tool_round: ToolRound | None = tool_round,
                offered_tool_names: list[str] = offered_tool_names,
                removed_tool_names: frozenset[str] = removed_tool_names,
            ) -> None:
                assert tool_round is not None
                tool_round.start(
                    _offered_tool_calls(
                        _parse_response_tool_calls(raw_calls) or [],
                        offered_tool_names,
                        self._dependencies.tools,
                        removed=removed_tool_names,
                    )
                )

            while True:
                run.raise_if_cancelled()
                request_context_usage = await _CHAT_TRANSFORM_WORKERS.run(
                    context.context_usage.project,
                    messages_for_request,
                    target=target,
                    tools=tools,
                    scope=context.prompt_cache_affinity_id,
                    context_window=self._requests.resolve_context_window(agent, target),
                )
                self._requests._raise_if_measured_context_exhausted(
                    context.session_snapshot.active_messages,
                    messages_for_request,
                    tools,
                    agent,
                    run,
                    target,
                    context_usage=request_context_usage,
                )
                try:
                    with (
                        request_input_budget(target.model_id, int(request_context_usage["tokens"])),
                        measure(
                            "provider.response",
                            track=track,
                            name="provider response",
                            args={**run_args, "iteration": request_iteration_number},
                        ),
                    ):
                        assistant_step = await self._wire_requests.send_assistant_request(
                            agent,
                            target.adapter,
                            target.model_id,
                            target.model_reference,
                            messages_for_request,
                            tools,
                            run,
                            prompt_cache_affinity_id=context.prompt_cache_affinity_id,
                            chunk_timeout_seconds=target.chunk_timeout_seconds,
                            stream_draft=context.stream_draft,
                            output_cwd=output_cwd,
                            public_model=target.public_model,
                            provider_id=target.provider_id,
                            recovery=context.recovery,
                            message_id=assistant_message_id,
                            on_tool_calls=start_tool_calls if tool_round is not None else None,
                        )
                except ProviderRequestTooLargeError as exc:
                    smaller = await _CHAT_TRANSFORM_WORKERS.run(
                        partial(
                            context.image_budget.shrink,
                            messages_for_request,
                            max_bytes=exc.max_bytes,
                            image_limit=target.max_request_images,
                        )
                    )
                    if smaller == messages_for_request:
                        raise
                    messages_for_request = smaller
                    continue
                except RunInterruptedError as exc:
                    if exc.result is None and interruption_chain:
                        exc.result = _combined_interrupted_result(
                            interruption_chain, output_cwd=output_cwd
                        )
                    raise
                break
            # This is the sole mutation point for the Iteration count: one
            # completed request/response pair, independent of how many Tool
            # Calls or readable Assistant blocks the response contains.
            run.iteration_count = request_iteration_number
            assistant_message = assistant_step.message
            terminal_outcome = assistant_step.terminal_outcome
            recovery = assistant_step.recovery
            recovery_note = assistant_step.recovery_note
            started_tool_calls = tool_round.calls if tool_round is not None else []
            # Both an interrupted partial and a finished readable stream may
            # already be visible when Cancel arrives, and started Tool Calls may
            # already have effects. Preparation and lock admission are both
            # cancellable; neither may lose this boundary.
            preserve_after_cancel = (
                assistant_message.interrupted
                or bool(started_tool_calls)
                or (
                    not assistant_message.tool_calls
                    and (
                        (
                            bool(assistant_message.content)
                            if isinstance(assistant_message.content, str)
                            else False
                        )
                        or bool(assistant_message.reasoning)
                    )
                )
            )
            if not preserve_after_cancel:
                run.raise_if_cancelled()

            async def prepare_boundary(
                assistant_message: ChatMessage,
                terminal_outcome: TerminalOutcome | None = terminal_outcome,
                messages_for_request: list[JsonObject] = messages_for_request,
                output_cwd: Path | None = output_cwd,
                request_context_usage: JsonObject = request_context_usage,
                assistant_step: _AssistantStep = assistant_step,
                request_tools: list[JsonObject] = tools,
                offered_tool_names: list[str] = offered_tool_names,
                removed_tool_names: frozenset[str] = removed_tool_names,
                started_tool_calls: list[ToolCall] = started_tool_calls,
            ) -> tuple[ChatMessage, JsonObject, list[JsonObject], JsonObject]:
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
                    await self._requests.persist_image_retirement(
                        context.session, context.image_budget
                    )
                assistant_message = _with_offered_tool_names(
                    assistant_message,
                    offered_tool_names,
                    self._dependencies.tools,
                    removed=removed_tool_names,
                )
                if started_tool_calls:
                    # The turn records its started calls exactly as they run.
                    later_calls = (assistant_message.tool_calls or [])[len(started_tool_calls) :]
                    assistant_message = replace(
                        assistant_message, tool_calls=[*started_tool_calls, *later_calls]
                    )
                assistant_message = await _CHAT_TRANSFORM_WORKERS.run(
                    _prepare_completed_assistant,
                    assistant_message,
                    messages_for_request,
                    output_cwd,
                    int(request_context_usage["tokens"]),
                    self._dependencies.models.pricing_for(target.model_reference),
                )
                recorder = self._dependencies.usage_recorder
                call_id = (assistant_message.usage or {}).get("usage_call_id")
                if recorder is not None and isinstance(call_id, str):
                    assistant_message = replace(
                        assistant_message,
                        usage=await recorder.update(call_id, assistant_message.usage),
                    )
                assistant_request_message = await _CHAT_TRANSFORM_WORKERS.run(
                    _assistant_continuation_dict,
                    assistant_message,
                    replay_policy=replay_policy,
                )
                # An interrupted Reasoning-only boundary becomes empty once its
                # native Reasoning is stripped. Never send an empty Assistant entry.
                assistant_request_messages: list[JsonObject] = (
                    [assistant_request_message]
                    if any(
                        assistant_request_message.get(field)
                        for field in ("content", "tool_calls", "reasoning", "reasoning_meta")
                    )
                    else []
                )
                assert isinstance(assistant_message.usage, dict)
                await _CHAT_TRANSFORM_WORKERS.run(
                    context.context_usage.observe,
                    assistant_message.usage,
                    messages_for_request,
                    target=target,
                    tools=request_tools,
                    scope=context.prompt_cache_affinity_id,
                )
                assistant_context_usage = await _CHAT_TRANSFORM_WORKERS.run(
                    context.context_usage.project,
                    [*messages_for_request, *assistant_request_messages],
                    target=target,
                    tools=request_tools,
                    scope=context.prompt_cache_affinity_id,
                    context_window=self._requests.resolve_context_window(agent, target),
                )
                assistant_message = replace(
                    assistant_message,
                    usage={
                        **assistant_message.usage,
                        "context_usage": assistant_context_usage,
                        CONTEXT_ESTIMATION_FIELD: context.context_usage.estimation_record(target),
                    },
                )
                return (
                    assistant_message,
                    assistant_request_message,
                    assistant_request_messages,
                    assistant_context_usage,
                )

            (
                assistant_message,
                assistant_request_message,
                assistant_request_messages,
                assistant_context_usage,
            ) = await _finish_visible_boundary(
                prepare_boundary(assistant_message), run, preserve_after_cancel
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
                persist_started = time.perf_counter()

                async def persist_assistant(message: ChatMessage) -> None:
                    # The Assistant entry holds the streamed output and deletes its
                    # draft; no earlier draft write may commit after it.
                    await context.stream_draft.settle()
                    await context.session_snapshot.append(session, [message])

                await _finish_visible_boundary(
                    persist_assistant(assistant_message), run, preserve_after_cancel
                )
                session.assistant_message_id = assistant_message.id
                record_span(
                    "chat.persist",
                    persist_started,
                    track=track,
                    name="persist assistant",
                    args=run_args,
                )
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
                messages.extend(assistant_request_messages)
                if assistant_message.interrupted:
                    if (
                        isinstance(assistant_message.content, str)
                        or assistant_message.reasoning is not None
                    ):
                        interruption_chain.append(assistant_message)
                elif terminal_outcome != TERMINAL_OUTCOME_OUTPUT_TRUNCATED:
                    # A complete response finishes the unfinished step, so its
                    # earlier fragments are no longer a partial Run result.
                    interruption_chain.clear()

                if not assistant_message.tool_calls:
                    if assistant_step.failure is not None:
                        raise assistant_step.failure
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
                        if not context.recovery.available(target.model_reference):
                            # The last failure rides along for the terminal log line.
                            raise context.recovery.exhausted(
                                result=_combined_interrupted_result(
                                    interruption_chain,
                                    output_cwd=output_cwd,
                                ),
                            ) from context.recovery.last_error
                        await session.add_note_async(recovery_note or STREAM_RECOVERY_NOTE)
                        await context.session_snapshot.refresh(session)
                        continue
                    terminal_error = _terminal_outcome_error(
                        terminal_outcome,
                        has_tool_calls=False,
                    )
                    if terminal_error is not None:
                        raise terminal_error
                    if self._dependencies.run_manager.pending_steering(run):
                        awaiting_steering = True
                        continue
                    # Input selected after this check starts the next Run.
                    break

                if preserved_cancelled_output:
                    # Cancel arrived while started Tool Calls ran. The turn is
                    # durable; the next request repairs their missing results.
                    return assistant_message
                finalization_violation = context.tool_progress.finalization_reason is not None
                finalization_request_reason: str | None = None
                tool_iteration_limit = self._tool_iteration_limit(context)
                tool_limit_reached = (
                    not finalization_violation
                    and terminal_outcome == TERMINAL_OUTCOME_TOOL_CALLS
                    and context.tool_progress.iteration_count >= tool_iteration_limit
                )

                session.begin_defer_notes()
                try:
                    terminal_error = _terminal_outcome_error(
                        terminal_outcome,
                        has_tool_calls=True,
                    )
                    if terminal_outcome == TERMINAL_OUTCOME_TOOL_CALLS:
                        if finalization_violation:
                            tool_messages = _fail_tool_calls_without_dispatch(
                                tool_dispatch_context,
                                assistant_message.tool_calls,
                                code=TOOL_FINALIZATION_DISABLED_FAILURE_CODE,
                                message=TOOL_FINALIZATION_DISABLED_FAILURE_MESSAGE,
                                retryable=False,
                            )
                            media_outputs: list[JsonObject] = []
                        elif tool_limit_reached:
                            finalization_request_reason = (
                                "the Run reached its limit of "
                                f"{tool_iteration_limit} dispatched Tool iterations"
                            )
                            tool_messages = _fail_tool_calls_without_dispatch(
                                tool_dispatch_context,
                                assistant_message.tool_calls,
                                code=TOOL_ITERATION_LIMIT_FAILURE_CODE,
                                message=TOOL_ITERATION_LIMIT_FAILURE_MESSAGE.format(
                                    limit=tool_iteration_limit
                                ),
                                retryable=False,
                            )
                            media_outputs = []
                        else:
                            assert tool_round is not None
                            context.tool_progress.iteration_count += 1
                            with measure(
                                "chat.tool_round",
                                track=track,
                                name="tool round",
                                args={**run_args, "tool_calls": len(assistant_message.tool_calls)},
                            ):
                                # Calls that arrived only with the end of the
                                # response start now, after the started ones.
                                tool_messages, media_outputs = await tool_round.finish(
                                    assistant_message.tool_calls[len(started_tool_calls) :]
                                )
                    else:
                        # The terminal outcome forbids starting further calls.
                        # Calls that already started keep their real results.
                        tool_messages, media_outputs = [], []
                        if tool_round is not None and started_tool_calls:
                            context.tool_progress.iteration_count += 1
                            tool_messages, media_outputs = await tool_round.finish()
                        failure_code, failure_message = _terminal_tool_failure(terminal_outcome)
                        tool_messages.extend(
                            _fail_tool_calls_without_dispatch(
                                tool_dispatch_context,
                                assistant_message.tool_calls[len(started_tool_calls) :],
                                code=failure_code,
                                message=failure_message,
                                start_index=len(started_tool_calls),
                            )
                        )
                    tool_messages, media_outputs = await self._requests.store_tool_media(
                        tool_messages, media_outputs
                    )
                    tool_request_messages: list[JsonObject] = []
                    for tool_message in tool_messages:
                        assert tool_message.tool_call_id is not None
                        request_message = _message_to_request_dict(tool_message)
                        messages.append(request_message)
                        tool_request_messages.append(request_message)
                    repeated_failed_tool = context.tool_progress.failed_calls.observe(
                        assistant_message.tool_calls,
                        tool_messages,
                        tool_dispatch_context.registry,
                    )
                    if repeated_failed_tool is not None and finalization_request_reason is None:
                        finalization_request_reason = (
                            f"Tool {model_tool_name(repeated_failed_tool)!r} repeated the same "
                            f"failed Call {MAX_IDENTICAL_FAILED_TOOL_CALLS} times"
                        )
                    if finalization_request_reason is not None:
                        session.add_note(
                            TOOL_FINALIZATION_NOTE.format(reason=finalization_request_reason)
                        )
                    if recovery == "continue" and recovery_note is not None:
                        # The stream broke after these calls started; the note
                        # follows their results.
                        session.add_note(recovery_note)
                    deferred_notes = session.take_deferred_notes()
                    session.assistant_message_id = assistant_message.id
                    batch_messages = [*tool_messages, *deferred_notes]
                    persist_started = time.perf_counter()
                    binding = context.request.temporary_binding
                    extension_registry = self._dependencies.get_extension_registry()
                    result_facts = tool_dispatch_context.with_result_payloads(
                        tool_result_facts(tool_messages)
                    )
                    if binding is not None and tool_dispatch_context.delivery_receipts:
                        # The binding addresses this Run's own Session.
                        owned_receipts = [
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
                        ]
                        await context.session_snapshot.commit(
                            session,
                            partial(
                                self._dependencies.sessions.append_messages_with_receipts_async,
                                binding.address,
                                generation_id=binding.generation_id,
                                owner_name=binding.owner_name,
                                messages=batch_messages,
                                run_id=run.id,
                                assistant_message_id=assistant_message.id,
                                tool_results=result_facts,
                                receipts=owned_receipts,
                            ),
                        )
                    else:
                        await context.session_snapshot.append(
                            session,
                            batch_messages,
                            tool_results=result_facts,
                        )
                    record_span(
                        "chat.persist",
                        persist_started,
                        track=track,
                        name="persist tool results",
                        args=run_args,
                    )
                    for tool_message in tool_messages:
                        assert tool_message.tool_call_id is not None
                        tool_dispatch_context.notify_result_persisted(tool_message.tool_call_id)
                        loaded_project_id = project_tool_context_id(tool_message)
                        if project_id is None and loaded_project_id is not None:
                            await self._requests._apply_project_skill_context(
                                context, loaded_project_id
                            )
                    if assistant_step.failure is not None:
                        raise assistant_step.failure
                    if terminal_error is not None:
                        raise terminal_error
                    await self._requests._attach_tool_result_content(
                        tool_request_messages,
                        media_outputs,
                        target.input_modalities,
                        target.wire_media_types,
                        max_image_bytes=target.max_image_bytes,
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

            # Change statistics after each dispatched Tool round, when they
            # changed: stored on the running Run first (a restart keeps them and
            # Session totals include them), then streamed live. The line diffs
            # run off the Event Loop.
            if self._dependencies.change_tracker is not None:
                current_change_stats = await _CHAT_TRANSFORM_WORKERS.run(
                    self._dependencies.change_tracker.peek_run_stats, (session_address, run.id)
                )
                if (
                    current_change_stats is not None
                    and current_change_stats != emitted_change_stats
                ):
                    emitted_change_stats = current_change_stats
                    try:
                        await session.record_change_stats_async(current_change_stats)
                    except Exception:
                        _LOGGER.warning(
                            "Failed to store change statistics for run %s", run.id, exc_info=True
                        )
                    run.emit(RUN_CHANGE_STATS_EVENT, {"change_stats": current_change_stats})

            # Bound the live request view as well, before Compaction estimates or
            # another Tool cycle. Canonical artifacts remain available to reopen.
            messages[:] = await _CHAT_TRANSFORM_WORKERS.run(
                limit_request_images,
                messages,
                budget=context.image_budget,
                image_limit=target.max_request_images,
                remember=True,
            )
            continuation_request_messages = await _CHAT_TRANSFORM_WORKERS.run(
                limit_request_images,
                [*messages_for_request, assistant_request_message, *tool_request_messages],
                budget=context.image_budget,
                image_limit=target.max_request_images,
                remember=True,
            )
            tool_context_usage = await _CHAT_TRANSFORM_WORKERS.run(
                context.context_usage.project,
                continuation_request_messages,
                target=target,
                tools=tools,
                scope=context.prompt_cache_affinity_id,
                context_window=self._requests.resolve_context_window(context.agent, target),
            )
            run.terminal_payload_extras["context_usage"] = tool_context_usage

            if finalization_violation:
                context.tool_progress.finalization_violations += 1
                if (
                    context.tool_progress.finalization_violations
                    >= MAX_TOOL_FINALIZATION_VIOLATIONS
                ):
                    # The Model already received one correlated disabled-Tool
                    # failure and ignored the boundary again. Complete gracefully
                    # instead of creating an unbounded recovery loop.
                    return assistant_message
            if finalization_request_reason is not None:
                context.tool_progress.finalization_reason = finalization_request_reason

            if self._compaction_service is not None:
                compacted_state = await self._compaction_runs.maybe_auto_compact_state(
                    context,
                    target,
                    usage=assistant_message.usage,
                    continuation_request_messages=continuation_request_messages,
                    allow_continuation=True,
                )
                context.request_state = compacted_state
                state = compacted_state
                messages = compacted_state.messages
                tools = compacted_state.tools

        if self._compaction_service is not None:
            try:
                await self._compaction_runs.maybe_auto_compact_state(
                    context,
                    target,
                    usage=assistant_message.usage,
                    continuation_request_messages=[
                        *messages_for_request,
                        assistant_request_message,
                    ],
                    continue_same_run=False,
                )
            except asyncio.CancelledError:
                if not run.cancel_requested:
                    raise
                # Stop ends only this optional post-answer Compaction. The final
                # answer is already durable and remains the Run result; the Run
                # manager sees ``cancel_requested`` and marks the Run cancelled.
        return assistant_message
