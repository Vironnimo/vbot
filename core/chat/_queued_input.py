"""Captured Chat input retained by the Run Queue."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from core.chat._message_history import (
    _add_context_note,
    _append_input_origin_note,
    _append_reply_surface_note,
)
from core.chat._request_history import _assign_session_image_references
from core.chat._run_state import (
    RequestBuildInputs,
    _RunRequest,
    report_subagent_user_input,
    subagent_input_reply_target,
    switches_subagent_reply_to_user,
)
from core.chat._skill_activation import _activate_triggered_skills
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.content_blocks import ContentBlock, masked_content
from core.chat.errors import ChatError
from core.chat.events import _visible_message_payload
from core.chat.messages import (
    PARENT_AGENT_STEERING_SYSTEM_REMINDER,
    SUBAGENT_REPLY_TO_USER_SYSTEM_REMINDER,
    ChatMessage,
    InputOrigin,
    ReplySurface,
)
from core.runs import USER_MESSAGE_EVENT, QueuedRunItem, Run, RunKind
from core.sessions import SessionAddress

if TYPE_CHECKING:
    from core.chat._request_builder import RequestBuilder
    from core.chat._run_state import ChatLoopDependencies, _ModelTarget, _RunExecutionContext
    from core.chat.chat import ChatLoop
    from core.sessions import TemporarySessionBinding


STEERING_SYSTEM_REMINDER = (
    "The following user message was added to your current task. "
    "Apply its corrections and additional instructions to that task. "
    "If the message asks a question or requests a status update, answer briefly and then resume "
    "any unfinished work on the task. "
    "If the user clearly cancels or replaces the task, follow that change."
)


@dataclass(frozen=True)
class QueuedChatInput:
    """Editable queued executor retaining every immutable admission input."""

    loop: ChatLoop
    request: _RunRequest

    async def __call__(self, run: Run) -> ChatMessage:
        request = self.request
        if request.extension_app_chat:
            binding = await self.app_chat_binding(
                SessionAddress(run.project_id, run.agent_id, run.session_id),
                run.expected_session_generation_id,
            )
            request = replace(request, temporary_binding=binding)
        return await self.loop._execution._execute_run(run, request)

    async def app_chat_binding(
        self, address: SessionAddress, generation_id: str | None
    ) -> TemporarySessionBinding:
        """Resolve the registered profile admitted by this queued operator input."""
        binding = await self.loop._input_binding(address, allow_app_chat=True)
        if binding is None or binding.generation_id != generation_id:
            raise ChatError("This Extension chat was replaced. Open it again in its page.")
        return binding

    def with_edited_content(
        self,
        content: str | list[ContentBlock],
        input_origin: InputOrigin | None,
        reply_surface: ReplySurface | None = None,
    ) -> QueuedChatInput:
        return replace(
            self,
            request=replace(
                self.request,
                content=content,
                input_origin=input_origin,
                reply_surface=reply_surface or self.request.reply_surface,
            ),
        )


async def persist_steering_input(
    context: _RunExecutionContext,
    item: QueuedRunItem,
    dependencies: ChatLoopDependencies,
    content: str | list[ContentBlock],
) -> None:
    """Append one user input before the Queue relinquishes it."""
    executor = item.executor
    if not isinstance(executor, QueuedChatInput):
        raise ChatError("queued input is not a Chat request")
    request = executor.request
    if request.content is None:
        raise ChatError("steering requires user content")
    session = context.session
    await context.session_snapshot.refresh(session)
    reply_target = subagent_input_reply_target(context, request)
    reply_to_user = await switches_subagent_reply_to_user(
        context, reply_target, dependencies.sessions
    )
    session.begin_defer_notes()
    _append_reply_surface_note(
        session, request.reply_surface, messages=context.session_snapshot.active_messages
    )
    _append_input_origin_note(session, request.input_origin)
    _add_context_note(session, request.context_note)
    _add_context_note(session, request.app_view)
    if reply_to_user:
        session.add_note(SUBAGENT_REPLY_TO_USER_SYSTEM_REMINDER)
    if not request.session_message:
        session.add_note(
            PARENT_AGENT_STEERING_SYSTEM_REMINDER
            if request.parent_agent_input
            else STEERING_SYSTEM_REMINDER
        )
    message = ChatMessage.user(
        _assign_session_image_references(
            masked_content(content, dependencies.secret_masker),
            context.session_snapshot.active_messages,
        ),
        input_origin=request.input_origin,
        sender=request.sender,
    )
    # The answer destination commits with the input, even if cancellation races.
    await session.append_many_async(
        [*session.take_deferred_notes(), message],
        subagent_reply_target=reply_target,
        accessor_input_ids=(item.admission.accessor_input_id,)
        if item.admission.accessor_input_id is not None
        else (),
    )
    if request.reply_surface is not None:
        context.request = replace(context.request, reply_surface=request.reply_surface)
    if (
        not request.internal
        and not request.parent_agent_input
        and not request.session_message
        and item.admission.run_kind in (RunKind.USER, RunKind.CHANNEL)
        and (request.sender is None or request.sender.kind != "session")
    ):
        context.direct_user_input = True
    if reply_to_user:
        report_subagent_user_input(dependencies, session, message.id)
    context.run.emit(
        USER_MESSAGE_EVENT,
        {
            "message": _visible_message_payload(message),
            "queue_item_id": item.item_id,
        },
        allow_after_cancel=True,
    )


async def rebuild_after_steering(
    context: _RunExecutionContext, target: _ModelTarget, requests: RequestBuilder
) -> None:
    """Resolve newly supplied media and Skills, keeping the live Run request state."""
    session = context.session
    await context.session_snapshot.refresh(session)
    session.activated_skill_contents(context.session_snapshot.active_messages)
    session.begin_defer_notes()
    try:
        # All fresh User messages since the previous Model boundary are eligible.
        for message in reversed(context.session_snapshot.active_messages):
            if message.role == "assistant":
                break
            if (
                message.role == "user"
                and isinstance(message.content, str)
                and (message.sender is None or message.sender.kind != "session")
            ):
                await _CHAT_TRANSFORM_WORKERS.run(
                    _activate_triggered_skills,
                    context.agent,
                    session,
                    message.content,
                    context.skill_registry,
                )
    finally:
        await session.flush_deferred_notes_async()
    await context.session_snapshot.refresh(session)
    context.request_state = await requests.rebuild_live_request_state(
        context.agent,
        session,
        inputs=RequestBuildInputs.from_context(context, target).with_session_messages(
            context.session_snapshot.active_messages
        ),
        live_messages=context.request_state.messages if context.request_state else [],
    )
