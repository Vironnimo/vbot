"""Materialize Model inputs and prepare completed Assistant persistence boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from core.chat._request_history import _prepare_request_context
from core.chat._run_state import RequestState
from core.chat._step_outcomes import (
    _prepare_assistant_context,
    _prepare_completed_assistant,
    _terminal_outcome_error,
    _with_offered_tool_names,
)
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.messages import (
    ChatMessage,
    JsonObject,
    ToolCall,
)
from core.chat.output_files import OutputFileScope
from core.chat.tool_dispatch import ToolDispatchContext
from core.chat.usage import (
    RequestContextEstimate,
)
from core.chat.wire_shaping import (
    extend_request_with_notes,
)
from core.extensions import HookContext
from core.providers.adapter import (
    TerminalOutcome,
)
from core.runs import (
    Run,
)
from core.sessions import (
    SessionAddress,
)

if TYPE_CHECKING:
    from core.chat._request_builder import RequestBuilder
    from core.chat._run_state import ChatLoopDependencies, _ModelTarget, _RunExecutionContext


class ModelStepBoundary:
    """Keep request hooks and completed Assistant accounting outside loop policy."""

    def __init__(self, dependencies: ChatLoopDependencies, requests: RequestBuilder) -> None:
        self._dependencies = dependencies
        self._requests = requests

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

    async def _prepare_model_request(
        self,
        context: _RunExecutionContext,
        target: _ModelTarget,
        state: RequestState,
    ) -> tuple[list[JsonObject], RequestContextEstimate]:
        """Apply request hooks and count the selected wire's image-limited request."""
        run = context.run
        session = context.session
        messages = state.messages
        tools = state.tools
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
                async with self._dependencies.sessions.write_lock(session.address):
                    await context.session_snapshot.flush_deferred_notes(session)
            # Hook notes were persisted before this Model step. Send them
            # here too, so replay cannot move them ahead of its reply later.
            hook_notes = session.drain_pending_notes()
            for request_view in (
                [messages] if messages_for_request is messages else [messages, messages_for_request]
            ):
                extend_request_with_notes(
                    request_view,
                    hook_notes,
                    context.session_snapshot.active_messages,
                    replay_policy=target.replay_policy,
                    agent_model=target.model_reference,
                )

        messages_for_request, request_estimate = await _CHAT_TRANSFORM_WORKERS.run(
            _prepare_request_context,
            messages_for_request,
            budget=context.image_budget,
            image_limit=target.max_request_images,
            accounting=context.context_usage,
            target=target,
            tools=tools,
            scope=context.prompt_cache_affinity_id,
        )
        return messages_for_request, request_estimate

    async def prepare_assistant(
        self,
        context: _RunExecutionContext,
        target: _ModelTarget,
        assistant_message: ChatMessage,
        *,
        terminal_outcome: TerminalOutcome | None,
        messages_for_request: list[JsonObject],
        output_scope: OutputFileScope,
        request_context_usage: JsonObject,
        request_estimate: RequestContextEstimate,
        request_tools: list[JsonObject],
        offered_tool_names: list[str],
        removed_tool_names: frozenset[str],
        started_tool_calls: list[ToolCall],
    ) -> tuple[ChatMessage, JsonObject, list[JsonObject], JsonObject, RequestContextEstimate]:
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
            await self._requests.persist_image_retirement(context.session, context.image_budget)
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
            output_scope,
            int(request_context_usage["tokens"]),
            self._dependencies.models.pricing_for(target.model_reference),
        )
        recorder = self._dependencies.usage_recorder
        call_id = (assistant_message.usage or {}).get("usage_call_id")
        # The recorder learns the step's measured input together with its
        # enrichment, in one transaction, when it is this Run's calibration.
        calibrated = False
        if recorder is not None and isinstance(call_id, str):
            calibrated = context.context_usage.calibration is recorder
            assistant_message = replace(
                assistant_message,
                usage=await recorder.update(
                    call_id,
                    assistant_message.usage,
                    input_estimate=(
                        (request_estimate.model_reference, request_estimate.tokens)
                        if calibrated
                        else None
                    ),
                ),
            )
        return await _CHAT_TRANSFORM_WORKERS.run(
            _prepare_assistant_context,
            assistant_message,
            messages_for_request,
            request_estimate,
            replay_policy=target.replay_policy,
            accounting=context.context_usage,
            calibrated=calibrated,
            target=target,
            tools=request_tools,
            scope=context.prompt_cache_affinity_id,
            context_window=await self._requests.targets.resolve_context_capacity_async(
                context.agent, target
            ),
        )

    def tool_context(
        self,
        context: _RunExecutionContext,
        target: _ModelTarget,
        state: RequestState,
        removed_tool_names: frozenset[str],
    ) -> ToolDispatchContext:
        """Materialize the admitted Tool policy and runtime inputs for one Model step."""
        run, agent, session, project_id = (
            context.run,
            context.agent,
            context.session,
            context.project_id,
        )
        return ToolDispatchContext(
            registry=self._dependencies.tools,
            extension_registry=self._dependencies.get_extension_registry(),
            agent=replace(
                agent,
                model=target.model_reference,
                thinking_effort=state.effort.current
                if state.effort is not None
                else agent.thinking_effort,
            ),
            session=session,
            run=run,
            vbot_root=Path(self._dependencies.get_system_prompts().vbot_root),
            data_root=Path(self._dependencies.storage.data_dir),
            project_cwd=context.project_cwd,
            project_id=project_id,
            skill_project_id=context.skill_project_id,
            skill_registry=context.skill_registry,
            reply_surface=context.request.reply_surface,
            subagent_session=context.subagent_session,
            permissions=context.permissions,
            execution=context.execution,
            tool_restriction=context.request.tool_restriction,
            tool_denial_resolver=context.request.tool_denial_resolver,
            base_allowed_tools=state.allowed_tool_names,
            session_tool_grants=state.session_tool_grants,
            tool_contracts=state.tool_contracts,
            removed_tool_names=removed_tool_names,
            loadable_tools=state.loadable_tools,
            unloadable_tools=state.unloadable_tools,
            change_tracker=self._dependencies.change_tracker,
            allow_owned_effects=context.request.temporary_binding is not None,
        )
