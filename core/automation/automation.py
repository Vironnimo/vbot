"""Automation primitives for programmatic chat run triggering."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.chat import ChatLoop, MessageSender
from core.chat.content_blocks import ContentBlock
from core.runs import ActiveRunError, ChatRunManager, Run, WaitingWorkAdmission

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
        sender: MessageSender | None = None,
        project_id: str | None = None,
        waiting_work_admission: WaitingWorkAdmission | None = None,
    ) -> Run:
        """Start a run immediately, or queue it until the target session is idle.

        ``project_id=None`` keeps today's global/identity behavior. A set
        ``project_id`` creates the auto-session under that project's anchor and
        scopes the Run to the project (cwd = repo, project files in the prompt).
        """
        target_session_id = session_id
        if target_session_id is None:
            target_session_id = self._runtime.chat_sessions.create(
                agent_id, project_id=project_id
            ).id

        try:
            if internal:
                run = await self._trigger_chat_loop.start_run(
                    agent_id,
                    message,
                    session_id=target_session_id,
                    internal=True,
                    project_id=project_id,
                )
            else:
                run = await self._trigger_chat_loop.start_run(
                    agent_id,
                    message,
                    session_id=target_session_id,
                    sender=sender,
                    project_id=project_id,
                )
        except ActiveRunError:
            try:
                if internal:
                    if waiting_work_admission is None:
                        queued_item = await self._trigger_chat_loop.queue_run(
                            agent_id,
                            message,
                            session_id=target_session_id,
                            internal=True,
                            project_id=project_id,
                        )
                    else:
                        queued_item = await self._trigger_chat_loop.queue_run(
                            agent_id,
                            message,
                            session_id=target_session_id,
                            internal=True,
                            project_id=project_id,
                            waiting_work_admission=waiting_work_admission,
                        )
                else:
                    if waiting_work_admission is None:
                        queued_item = await self._trigger_chat_loop.queue_run(
                            agent_id,
                            message,
                            session_id=target_session_id,
                            sender=sender,
                            project_id=project_id,
                        )
                    else:
                        queued_item = await self._trigger_chat_loop.queue_run(
                            agent_id,
                            message,
                            session_id=target_session_id,
                            sender=sender,
                            project_id=project_id,
                            waiting_work_admission=waiting_work_admission,
                        )
                return await queued_item.future
            except BaseException:
                self.release_waiting_work(waiting_work_admission)
                raise
        except BaseException:
            self.release_waiting_work(waiting_work_admission)
            raise
        else:
            self.release_waiting_work(waiting_work_admission)
            return run

    def reserve_waiting_work(self, *, scope: str, scope_limit: int) -> WaitingWorkAdmission:
        """Reserve shared queue capacity before an ingress path does costly work."""
        return self._chat_run_manager.reserve_waiting_work(
            scope=scope,
            scope_limit=scope_limit,
        )

    def release_waiting_work(self, admission: WaitingWorkAdmission | None) -> bool:
        """Release an ingress reservation that did not become a queued Run."""
        if admission is None:
            return False
        return self._chat_run_manager.release_waiting_work(admission)

    async def continue_run(
        self, agent_id: str, session_id: str, *, project_id: str | None = None
    ) -> Run:
        """Continue retained interrupted work for a channel or automation entry point.

        ``project_id=None`` keeps the identity scope — channels are identity-only
        callers today.
        """
        return await self._trigger_chat_loop.continue_run(
            agent_id, session_id, project_id=project_id
        )

    def has_active_run(
        self, agent_id: str, session_id: str, *, project_id: str | None = None
    ) -> bool:
        """Return whether one session currently has an active run.

        A thin delegate to the run manager's active-run guard, so command
        producers (channels) can refuse run-conflicting actions such as starting
        a new session mid-run without taking their own dependency on the run
        manager. ``project_id=None`` keeps the identity scope (channels).
        """
        return (
            self._chat_run_manager.active_run(
                agent_id=agent_id, session_id=session_id, project_id=project_id
            )
            is not None
        )

    async def compact_session(
        self,
        agent_id: str,
        session_id: str,
        instruction: str | None = None,
        *,
        project_id: str | None = None,
    ) -> str:
        """Compact a session and return a user-facing command reply.

        ``instruction`` carries the optional free-text argument from
        ``/compact <instruction>`` down into the summarization prompt.
        ``project_id`` scopes compaction to a project session (``None`` = identity).
        """
        return await self._chat_loop.compact_session(
            agent_id, session_id, instruction, project_id=project_id
        )
