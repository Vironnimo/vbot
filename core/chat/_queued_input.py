"""Captured Chat input retained by the Run Queue."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from core.chat._message_history import _append_input_origin_note
from core.chat._request_history import (
    _assign_session_image_references,
    _restore_in_run_tool_result_content,
)
from core.chat._run_state import RequestBuildInputs, _RunRequest
from core.chat._skill_activation import _activate_triggered_skills
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.content_blocks import ContentBlock
from core.chat.continuation import inject_continuation_reminder
from core.chat.errors import ChatError
from core.chat.events import _visible_message_payload
from core.chat.messages import ChatMessage, InputOrigin
from core.runs import USER_MESSAGE_EVENT, QueuedRunItem, Run

if TYPE_CHECKING:
    from core.chat._request_builder import RequestBuilder
    from core.chat._run_state import _ModelTarget, _RunExecutionContext
    from core.chat.chat import ChatLoop


@dataclass(frozen=True)
class QueuedChatInput:
    """Editable queued executor retaining every immutable admission input."""

    loop: ChatLoop
    request: _RunRequest

    async def __call__(self, run: Run) -> ChatMessage:
        return await self.loop._execution._execute_run(run, self.request)

    def with_edited_content(
        self,
        content: str | list[ContentBlock],
        input_origin: InputOrigin | None,
    ) -> QueuedChatInput:
        return replace(
            self,
            request=replace(
                self.request,
                content=content,
                input_origin=input_origin,
            ),
        )


async def persist_steering_input(context: _RunExecutionContext, item: QueuedRunItem) -> None:
    """Append one user input before the Queue relinquishes it."""
    executor = item.executor
    if not isinstance(executor, QueuedChatInput):
        raise ChatError("queued input is not a Chat request")
    request = executor.request
    if request.content is None:
        raise ChatError("steering requires user content")
    session = context.session
    await context.session_snapshot.refresh(session)
    session.begin_defer_notes()
    _append_input_origin_note(session, request.input_origin)
    message = ChatMessage.user(
        _assign_session_image_references(request.content, context.session_snapshot.active_messages)
    )
    await session.append_many_async([*session.take_deferred_notes(), message])
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
    """Resolve newly supplied media and Skills through the ordinary request builder."""
    session = context.session
    await context.session_snapshot.refresh(session)
    session.activated_skill_contents(context.session_snapshot.active_messages)
    session.begin_defer_notes()
    try:
        # All fresh User messages since the previous Model boundary are eligible.
        for message in reversed(context.session_snapshot.active_messages):
            if message.role == "assistant":
                break
            if message.role == "user" and isinstance(message.content, str):
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
    live_messages = context.request_state.messages if context.request_state else []
    state = await requests.build_request_state(
        context.agent,
        session,
        inputs=RequestBuildInputs.from_context(context, target).with_session_messages(
            context.session_snapshot.active_messages
        ),
    )
    state.messages[:] = await _CHAT_TRANSFORM_WORKERS.run(
        _restore_in_run_tool_result_content,
        state.messages,
        live_messages,
        input_modalities=target.input_modalities,
        wire_media_types=target.wire_media_types,
        image_budget=context.image_budget,
    )
    if context.continuation_reminder is not None:
        state = replace(
            state,
            messages=inject_continuation_reminder(state.messages, context.continuation_reminder),
        )
    context.request_state = state
