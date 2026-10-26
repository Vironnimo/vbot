"""Materialize Model inputs and prepare completed Assistant persistence boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial
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
    SessionReadBatch,
    SessionReadCursor,
    ToolResultFacts,
    project_tool_context_id,
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
            activity_writer=partial(self._persist_programmatic_activity, context),
            current_skill_context=lambda: (context.skill_project_id, context.skill_registry),
        )

    async def _persist_programmatic_activity(
        self,
        context: _RunExecutionContext,
        assistant_message_id: str,
        messages: list[ChatMessage],
        facts: Mapping[str, ToolResultFacts],
        receipts: Sequence[tuple[int, str, str, str, str]],
        media: list[JsonObject],
    ) -> None:
        """Keep script activity and receipts durable without admitting a Model turn."""
        session = context.session
        messages, _ = await self._requests.media.store_tool_media(messages, media)
        project_ids: list[str] = []
        if context.project_id is None:
            notes: list[ChatMessage] = []
            for message in messages:
                project_id = project_tool_context_id(message)
                if project_id is None:
                    continue
                project_ids.append(project_id)
                data = json.loads(str(message.content))["data"]
                # Loading Project instructions is itself a context effect. Keep
                # its existing fields and body visible without replaying the
                # internal Tool declaration or creating a second instruction text.
                metadata = {key: value for key, value in data.items() if key != "content"}
                body = data.get("content")
                content = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
                if isinstance(body, str) and body:
                    content += "\n\n" + body
                notes.append(ChatMessage.note(content))
            messages = [*messages, *notes]
        binding = context.request.temporary_binding
        async with self._dependencies.sessions.write_lock(session.address):
            if binding is not None and receipts:
                write = partial(
                    self._dependencies.sessions.append_messages_with_receipts_async,
                    binding.address,
                    generation_id=binding.generation_id,
                    owner_name=binding.owner_name,
                    messages=messages,
                    run_id=context.run.id,
                    assistant_message_id=assistant_message_id,
                    tool_results=facts,
                    receipts=receipts,
                )
            else:
                write = partial(
                    session.append_many_async,
                    messages,
                    assistant_message_id=assistant_message_id,
                    tool_results=facts,
                )

            async def write_context(*, since: SessionReadCursor) -> SessionReadBatch | None:
                committed = await write(since=since)
                # The snapshot serializes commit and adoption together: a slow
                # resolver for an earlier Project cannot replace a later load.
                for project_id in project_ids:
                    await self._requests._apply_project_skill_context(context, project_id)
                return committed

            await context.session_snapshot.commit(
                session,
                write_context,
                # Context notes arrive for the next request. The child rows
                # themselves are internal activity, never delivered to the Model.
                written=[message for message in messages if message.role != "note"],
            )
