"""Automation primitives for programmatic chat run triggering."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.chat import ChatLoop
from core.chat.content_blocks import ContentBlock
from core.runs import ActiveRunError, ChatRunManager, Run

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime


class TriggerService:
    """Start programmatic chat runs and queue triggers behind active runs."""

    def __init__(
        self,
        chat_loop: ChatLoop,
        chat_run_manager: ChatRunManager,
        runtime: Runtime,
        *,
        trigger_chat_loop: ChatLoop | None = None,
    ) -> None:
        self._chat_loop = chat_loop
        self._trigger_chat_loop = trigger_chat_loop or chat_loop
        self._chat_run_manager = chat_run_manager
        self._runtime = runtime

    async def trigger_run(
        self,
        agent_id: str,
        message: str | list[ContentBlock],
        session_id: str | None = None,
        *,
        internal: bool = False,
    ) -> Run:
        """Start a run immediately, or queue it until the target session is idle."""
        target_session_id = session_id
        if target_session_id is None:
            target_session_id = self._runtime.chat_sessions.create(agent_id).id

        try:
            if internal:
                return await self._trigger_chat_loop.start_run(
                    agent_id,
                    message,
                    session_id=target_session_id,
                    internal=True,
                )
            return await self._trigger_chat_loop.start_run(
                agent_id,
                message,
                session_id=target_session_id,
            )
        except ActiveRunError:
            if internal:
                queued_item = await self._trigger_chat_loop.queue_run(
                    agent_id,
                    message,
                    session_id=target_session_id,
                    internal=True,
                )
            else:
                queued_item = await self._trigger_chat_loop.queue_run(
                    agent_id,
                    message,
                    session_id=target_session_id,
                )
            return await queued_item.future

    async def retry_run(self, agent_id: str, session_id: str) -> Run:
        """Retry the last user turn for a channel or automation entry point."""
        return await self._trigger_chat_loop.retry_run(agent_id, session_id)

    async def compact_session(self, agent_id: str, session_id: str) -> str:
        """Compact a session and return a user-facing command reply."""
        return await self._chat_loop.compact_session(agent_id, session_id)
