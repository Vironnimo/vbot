"""Commit ordered Tool Results and settle cancellation at their durable boundary."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING

from core.chat._boundaries import _finish_visible_boundary
from core.chat._folder_instructions import folder_instruction_notes
from core.chat._run_state import RequestState
from core.chat._step_outcomes import (
    tool_result_facts,
)
from core.chat._tool_epoch import ToolChange
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.messages import (
    ChatMessage,
    JsonObject,
    ToolCall,
)
from core.chat.tool_dispatch import (
    ToolDispatchContext,
    ToolRound,
)
from core.performance import record_span, session_track
from core.runs import RUN_CHANGE_STATS_EVENT
from core.sessions import (
    project_tool_context_id,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat._request_builder import RequestBuilder
    from core.chat._run_state import ChatLoopDependencies, _RunExecutionContext


_LOGGER = get_logger("chat")


class ToolResultPersistence:
    """Own the Tool batch persistence boundary of an admitted Run."""

    def __init__(self, dependencies: ChatLoopDependencies, requests: RequestBuilder) -> None:
        self._dependencies = dependencies
        self._requests = requests

    async def _persist_tool_results(
        self,
        context: _RunExecutionContext,
        tool_dispatch_context: ToolDispatchContext,
        assistant_message_id: str,
        tool_messages: list[ChatMessage],
    ) -> list[ToolChange]:
        """Persist a turn's Tool Results with the deferred Notes in one transaction.

        A ``load_tools`` call whose Result is persisted adds a silent ``loaded``
        note per Tool it loaded to the same transaction; returns those changes.
        Runs while the Session defers its notes, so the folder instruction notes
        of the batch join the deferred ones: stored with the Results and shown
        with the next request.
        """
        run = context.run
        session = context.session
        loads = _tool_load_changes(context.request_state, tool_dispatch_context, tool_messages)
        for note in await self._folder_instruction_notes(
            context, tool_dispatch_context, tool_messages
        ):
            session.add_note_message(note)
        deferred_notes = session.take_deferred_notes()
        session.assistant_message_id = assistant_message_id
        batch_messages = [
            *tool_messages,
            *(ChatMessage.note(change.note_content()) for change in loads),
            *deferred_notes,
        ]
        persist_started = time.perf_counter()
        binding = context.request.temporary_binding
        result_facts = tool_dispatch_context.with_result_payloads(tool_result_facts(tool_messages))
        result_indexes = {
            message.tool_call_id: index
            for index, message in enumerate(batch_messages)
            if message.role == "tool"
        }
        # A receipt counts only with its call's persisted Result; a call a Run
        # cancel stopped has none.
        owned_receipts = [
            (result_indexes[tool_call_id], receipt_id, content_hash, effect_kind, "tool")
            for (
                tool_call_id,
                receipt_id,
                content_hash,
                effect_kind,
            ) in tool_dispatch_context.delivery_receipts
            if tool_call_id in result_indexes
        ]
        if binding is not None and owned_receipts:
            # The binding addresses this Run's own Session.
            await context.session_snapshot.commit(
                session,
                partial(
                    self._dependencies.sessions.append_messages_with_receipts_async,
                    binding.address,
                    generation_id=binding.generation_id,
                    owner_name=binding.owner_name,
                    messages=batch_messages,
                    run_id=run.id,
                    assistant_message_id=assistant_message_id,
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
            track=session_track(run.agent_id, run.session_id, context.project_id),
            name="persist tool results",
            args={"run_id": run.id},
        )
        for tool_message in tool_messages:
            assert tool_message.tool_call_id is not None
            tool_dispatch_context.notify_result_persisted(tool_message.tool_call_id)
            loaded_project_id = project_tool_context_id(tool_message)
            if context.project_id is None and loaded_project_id is not None:
                await self._requests._apply_project_skill_context(context, loaded_project_id)
        return loads

    async def _folder_instruction_notes(
        self,
        context: _RunExecutionContext,
        tool_dispatch_context: ToolDispatchContext,
        tool_messages: Sequence[ChatMessage],
    ) -> list[ChatMessage]:
        """Return the folder instruction notes for the paths the persisted calls worked in."""
        finder = context.folder_instructions
        if finder is None:
            return []
        persisted = {message.tool_call_id for message in tool_messages}
        paths = [
            path
            for tool_call_id, call_paths in tool_dispatch_context.worked_paths
            if tool_call_id in persisted
            for path in call_paths
        ]
        if not paths:
            return []
        state = context.request_state
        allowed = frozenset(state.allowed_tool_names if state is not None else ())
        # A shown file counts as read, so the Agent can edit it without a ``read``.
        return await _CHAT_TRANSFORM_WORKERS.run(
            folder_instruction_notes,
            finder,
            paths,
            context.session_snapshot.active_messages,
            permissions=context.permissions,
            tool_available=allowed.__contains__,
            on_read=lambda read: self._dependencies.file_read_state.record_stamp(
                context.run.session_id, *read
            ),
        )

    async def _finish_tool_round(
        self,
        context: _RunExecutionContext,
        tool_dispatch_context: ToolDispatchContext,
        tool_round: ToolRound,
        assistant_message_id: str,
        remaining: Sequence[ToolCall] = (),
    ) -> tuple[list[ChatMessage], list[JsonObject]] | None:
        """Return the round's ordered Tool Results, or ``None`` after a Run cancel.

        A Run cancel persists the Results of the calls that already finished.
        """
        try:
            return await tool_round.finish(remaining)
        except asyncio.CancelledError:
            if not context.run.cancel_requested:
                raise
        await self._persist_cancelled_tool_round(
            context, tool_dispatch_context, tool_round, assistant_message_id
        )
        return None

    async def _persist_cancelled_tool_round(
        self,
        context: _RunExecutionContext,
        tool_dispatch_context: ToolDispatchContext,
        tool_round: ToolRound,
        assistant_message_id: str,
    ) -> None:
        """After a Run cancel, persist the Results of the calls that finished.

        The calls still running are stopped and keep no Result; the next request
        repairs them as missing Results.
        """

        async def persist() -> None:
            tool_messages, media_outputs = await tool_round.settle_cancelled()
            if not tool_messages:
                return
            tool_messages, _ = await self._requests.media.store_tool_media(
                tool_messages, media_outputs
            )
            await self._persist_tool_results(
                context, tool_dispatch_context, assistant_message_id, tool_messages
            )

        await _finish_visible_boundary(persist(), context.run, True)

    async def record_change_stats(
        self, context: _RunExecutionContext, emitted_change_stats: dict[str, object] | None
    ) -> dict[str, object] | None:
        """Persist changed Run statistics before publishing their live event."""
        if self._dependencies.change_tracker is not None:
            current_change_stats = await _CHAT_TRANSFORM_WORKERS.run(
                self._dependencies.change_tracker.peek_run_stats,
                (context.session.address, context.run.id),
            )
            if current_change_stats is not None and current_change_stats != emitted_change_stats:
                emitted_change_stats = current_change_stats
                try:
                    await context.session.record_change_stats_async(current_change_stats)
                except Exception:
                    _LOGGER.warning(
                        "Failed to store change statistics for run %s",
                        context.run.id,
                        exc_info=True,
                    )
                context.run.emit(RUN_CHANGE_STATS_EVENT, {"change_stats": current_change_stats})

        return emitted_change_stats


def _tool_load_changes(
    state: RequestState | None,
    tool_dispatch_context: ToolDispatchContext,
    tool_messages: Sequence[ChatMessage],
) -> list[ToolChange]:
    """Return the ``loaded`` changes of the ``load_tools`` calls among *tool_messages*.

    Each Tool counts once, with the definition the call returned.
    """
    loads = state.tool_loads if state is not None else None
    if not loads:
        return []
    persisted = {message.tool_call_id for message in tool_messages}
    changes: dict[str, ToolChange] = {}
    for tool_call_id, names in tool_dispatch_context.tool_loads:
        if tool_call_id not in persisted:
            continue
        for name in names:
            change = loads.get(name)
            if change is not None:
                changes.setdefault(name, change)
    return list(changes.values())
