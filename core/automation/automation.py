"""Automation primitives for programmatic chat run triggering."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from core.chat import ChatLoop, MessageSender, ReplySurface
from core.chat.content_blocks import ContentBlock
from core.runs import ActiveRunError, ChatRunManager, Run, WaitingWorkAdmission

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime


def _optional_run_kwargs(
    callback: Callable[[], None] | None,
    contributes_to_agent_activity: bool,
) -> dict[str, Any]:
    """Omit defaulted Run options so existing producer call shapes stay stable."""
    options: dict[str, Any] = {}
    if callback is not None:
        options["input_persisted_hook"] = callback
    if not contributes_to_agent_activity:
        options["contributes_to_agent_activity"] = False
    return options


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
        reply_surface: ReplySurface | None = None,
        project_id: str | None = None,
        waiting_work_admission: WaitingWorkAdmission | None = None,
        input_persisted_hook: Callable[[], None] | None = None,
        contributes_to_agent_activity: bool = True,
    ) -> Run:
        """Start a run immediately, or queue it until the target session is idle.

        ``project_id=None`` keeps today's global/identity behavior. A set
        ``project_id`` creates the auto-session under that project's anchor and
        scopes the Run to the project (cwd = repo, project files in the prompt).
        """
        if session_id is None:
            try:
                if internal:
                    return await self._trigger_chat_loop.start_run_in_new_session(
                        agent_id,
                        message,
                        internal=True,
                        reply_surface=reply_surface,
                        project_id=project_id,
                        **_optional_run_kwargs(input_persisted_hook, contributes_to_agent_activity),
                    )
                return await self._trigger_chat_loop.start_run_in_new_session(
                    agent_id,
                    message,
                    sender=sender,
                    reply_surface=reply_surface,
                    project_id=project_id,
                    **_optional_run_kwargs(input_persisted_hook, contributes_to_agent_activity),
                )
            except BaseException:
                self.release_waiting_work(waiting_work_admission)
                raise

        target_session_id = session_id
        try:
            if internal:
                run = await self._trigger_chat_loop.start_run(
                    agent_id,
                    message,
                    session_id=target_session_id,
                    internal=True,
                    reply_surface=reply_surface,
                    project_id=project_id,
                    **_optional_run_kwargs(input_persisted_hook, contributes_to_agent_activity),
                )
            else:
                run = await self._trigger_chat_loop.start_run(
                    agent_id,
                    message,
                    session_id=target_session_id,
                    sender=sender,
                    reply_surface=reply_surface,
                    project_id=project_id,
                    **_optional_run_kwargs(input_persisted_hook, contributes_to_agent_activity),
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
                            reply_surface=reply_surface,
                            project_id=project_id,
                            **_optional_run_kwargs(
                                input_persisted_hook, contributes_to_agent_activity
                            ),
                        )
                    else:
                        queued_item = await self._trigger_chat_loop.queue_run(
                            agent_id,
                            message,
                            session_id=target_session_id,
                            internal=True,
                            reply_surface=reply_surface,
                            project_id=project_id,
                            waiting_work_admission=waiting_work_admission,
                            **_optional_run_kwargs(
                                input_persisted_hook, contributes_to_agent_activity
                            ),
                        )
                else:
                    if waiting_work_admission is None:
                        queued_item = await self._trigger_chat_loop.queue_run(
                            agent_id,
                            message,
                            session_id=target_session_id,
                            sender=sender,
                            reply_surface=reply_surface,
                            project_id=project_id,
                            **_optional_run_kwargs(
                                input_persisted_hook, contributes_to_agent_activity
                            ),
                        )
                    else:
                        queued_item = await self._trigger_chat_loop.queue_run(
                            agent_id,
                            message,
                            session_id=target_session_id,
                            sender=sender,
                            reply_surface=reply_surface,
                            project_id=project_id,
                            waiting_work_admission=waiting_work_admission,
                            **_optional_run_kwargs(
                                input_persisted_hook, contributes_to_agent_activity
                            ),
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
