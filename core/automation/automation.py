"""Automation primitives for programmatic chat run triggering."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from core.chat import ChatLoop, MessageSender, ReplySurface
from core.chat.content_blocks import ContentBlock
from core.runs import (
    ActiveRunError,
    ChatRunManager,
    Run,
    RunCancelledError,
    RunExecutionOwner,
    RunKind,
    WaitingWorkAdmission,
)
from core.sessions import AGENT_DEFAULT_PROJECT, SessionAddress, WorkingProjectChoice
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime
    from core.sessions import ChatSession, ChatSessionManager

from core.automation._completion import (
    OwnedCompletionValidator,
    _CompletionDeliveryCoordinator,
)

_LOGGER = get_logger("automation")

# Opens a follow-up Run for results a request already carried that the Model never answered.
# Backoff for a failed reminder append and for blocked follow-up Run admission.

# Tests patch this seam instead of the process-wide ``asyncio.sleep``.

"""Raise ``RunAdmissionBlockedError`` when an execution owner can no longer receive work."""


def _optional_run_kwargs(
    callback: Callable[[], None] | None,
    contributes_to_agent_activity: bool,
    run_kind: RunKind,
    context_note: str | None = None,
) -> dict[str, Any]:
    """Project non-default Run options onto the ChatLoop call."""
    options: dict[str, Any] = {}
    if context_note is not None:
        options["context_note"] = context_note
    if callback is not None:
        options["input_persisted_hook"] = callback
    if not contributes_to_agent_activity:
        options["contributes_to_agent_activity"] = False
    if run_kind is not RunKind.USER:
        options["run_kind"] = run_kind
    return options


def _optional_tool_kwargs(
    restriction: Sequence[str] | None,
    denial_resolver: Callable[[str], str | None] | None,
    max_tool_iterations: int | None,
) -> dict[str, Any]:
    """Omit unrestricted defaults so unrelated trigger call shapes stay stable."""
    options: dict[str, Any] = {}
    if restriction is not None:
        options["tool_restriction"] = restriction
    if denial_resolver is not None:
        options["tool_denial_resolver"] = denial_resolver
    if max_tool_iterations is not None:
        options["max_tool_iterations"] = max_tool_iterations
    return options


class TriggerService:
    """Start programmatic chat runs and queue triggers behind active runs."""

    def __init__(
        self,
        chat_loop: ChatLoop,
        chat_run_manager: ChatRunManager,
        runtime: Runtime,
        *,
        sessions: ChatSessionManager | None = None,
    ) -> None:
        self._chat_loop = chat_loop
        self._chat_run_manager = chat_run_manager
        self._runtime = runtime
        self._completion_delivery = _CompletionDeliveryCoordinator(
            self._chat_loop,
            chat_run_manager,
            sessions,
        )

    def submit_completion(
        self,
        address: SessionAddress | str,
        session_id: str | None = None,
        *,
        project_id: str | None = None,
        notice_id: str,
        origin_run_id: str,
        body: str,
        on_persisted: Callable[[], None] | None = None,
        execution_owner: RunExecutionOwner | None = None,
        wake: bool = True,
    ) -> asyncio.Future[None]:
        """Coalesce one background result at the target Session's next Run boundary.

        An idle Session gets a Run that delivers it; with ``wake=False`` it is
        persisted without one and reaches the Agent with its next Run.
        """
        if isinstance(address, str):
            assert session_id is not None
            address = SessionAddress(project_id, address, session_id)
        return self._completion_delivery.submit(
            address,
            notice_id=notice_id,
            origin_run_id=origin_run_id,
            body=body,
            on_persisted=on_persisted,
            execution_owner=execution_owner,
            wake=wake,
        )

    def set_owned_completion_starter(self, starter: Callable[..., Awaitable[Run]]) -> None:
        self._completion_delivery._owned_run_starter = starter

    def set_owned_completion_validator(self, validator: OwnedCompletionValidator) -> None:
        """Install the check that an owned completion may still reach its Session.

        Every submission carrying an execution owner, and every Session-write
        fallback for one, is checked; a rejected owner's result is withdrawn.
        """
        self._completion_delivery._owned_completion_validator = validator

    async def close_execution_group(self, extension: str, group_id: str, epoch: str) -> None:
        await self._completion_delivery.close_execution_group(extension, group_id, epoch)

    def has_execution_work(self, owner: RunExecutionOwner) -> bool:
        return any(
            notice.execution_owner == owner and not notice.read
            for bucket in self._completion_delivery._buckets.values()
            for notice in bucket.notices.values()
        )

    def cancel_completion(
        self,
        address: SessionAddress | str,
        session_id: str | None = None,
        *,
        project_id: str | None = None,
        notice_id: str,
    ) -> bool:
        """Withdraw one pending automatic result after another durable delivery.

        Returns False when it is no longer pending, or its delivery already
        claimed it: it is then delivered once, as submitted.
        """
        if isinstance(address, str):
            assert session_id is not None
            address = SessionAddress(project_id, address, session_id)
        return self._completion_delivery.cancel(
            address,
            notice_id=notice_id,
        )

    def set_completion_run_relay(
        self,
        relay: Callable[[Run, ReplySurface], Awaitable[None]] | None,
    ) -> None:
        """Route idle-session completion Run replies through their latest accessor."""
        self._completion_delivery.set_run_relay(relay)

    def deliver_background_completions(self, run: Run, session: ChatSession) -> bool:
        """Inject ready completion results into an active Run's next Model request."""
        return self._completion_delivery.deliver_to_request(run, session)

    async def aclose(self) -> None:
        """Stop and drain automatic background-completion delivery."""
        await self._completion_delivery.aclose()

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
        working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
        actor: str | None = None,
        tool_restriction: Sequence[str] | None = None,
        tool_denial_resolver: Callable[[str], str | None] | None = None,
        max_tool_iterations: int | None = None,
        waiting_work_admission: WaitingWorkAdmission | None = None,
        input_persisted_hook: Callable[[], None] | None = None,
        run_kind: RunKind = RunKind.USER,
        contributes_to_agent_activity: bool = True,
        context_note: str | None = None,
    ) -> Run:
        """Start a run immediately, or queue it until the target session is idle.

        ``project_id=None`` keeps today's global/identity behavior. A set
        ``project_id`` creates the auto-session under that project's anchor and
        scopes the Run to the project (cwd = repo, project files in the prompt).
        ``max_tool_iterations`` narrows the Run's dispatched Tool-iteration limit
        (``ChatLoop.start_run``); ``None`` keeps the loop's limit.
        ``context_note`` is stored as a note right before the message, for
        context the Model reads with it.
        With no ``session_id``, ``working_project_id`` and ``actor`` select the
        new Session's working Project and creation attribution, as in Chat.
        """
        tool_kwargs = _optional_tool_kwargs(
            tool_restriction,
            tool_denial_resolver,
            max_tool_iterations,
        )
        if session_id is None:
            session_kwargs: dict[str, Any] = {}
            if working_project_id is not AGENT_DEFAULT_PROJECT:
                session_kwargs["working_project_id"] = working_project_id
            if actor is not None:
                session_kwargs["actor"] = actor
            try:
                if internal:
                    return await self._chat_loop.start_run_in_new_session(
                        agent_id,
                        message,
                        internal=True,
                        reply_surface=reply_surface,
                        project_id=project_id,
                        **session_kwargs,
                        **tool_kwargs,
                        **_optional_run_kwargs(
                            input_persisted_hook,
                            contributes_to_agent_activity,
                            run_kind,
                            context_note,
                        ),
                    )
                return await self._chat_loop.start_run_in_new_session(
                    agent_id,
                    message,
                    sender=sender,
                    reply_surface=reply_surface,
                    project_id=project_id,
                    **session_kwargs,
                    **tool_kwargs,
                    **_optional_run_kwargs(
                        input_persisted_hook,
                        contributes_to_agent_activity,
                        run_kind,
                        context_note,
                    ),
                )
            finally:
                self.release_waiting_work(waiting_work_admission)

        target_session_id = session_id
        try:
            if internal:
                run = await self._chat_loop.start_run(
                    agent_id,
                    message,
                    session_id=target_session_id,
                    internal=True,
                    reply_surface=reply_surface,
                    project_id=project_id,
                    **tool_kwargs,
                    **_optional_run_kwargs(
                        input_persisted_hook,
                        contributes_to_agent_activity,
                        run_kind,
                        context_note,
                    ),
                )
            else:
                run = await self._chat_loop.start_run(
                    agent_id,
                    message,
                    session_id=target_session_id,
                    sender=sender,
                    reply_surface=reply_surface,
                    project_id=project_id,
                    **tool_kwargs,
                    **_optional_run_kwargs(
                        input_persisted_hook,
                        contributes_to_agent_activity,
                        run_kind,
                        context_note,
                    ),
                )
        except ActiveRunError:
            try:
                if internal:
                    if waiting_work_admission is None:
                        queued_item = await self._chat_loop.queue_run(
                            agent_id,
                            message,
                            session_id=target_session_id,
                            internal=True,
                            reply_surface=reply_surface,
                            project_id=project_id,
                            **tool_kwargs,
                            **_optional_run_kwargs(
                                input_persisted_hook,
                                contributes_to_agent_activity,
                                run_kind,
                                context_note,
                            ),
                        )
                    else:
                        queued_item = await self._chat_loop.queue_run(
                            agent_id,
                            message,
                            session_id=target_session_id,
                            internal=True,
                            reply_surface=reply_surface,
                            project_id=project_id,
                            **tool_kwargs,
                            waiting_work_admission=waiting_work_admission,
                            **_optional_run_kwargs(
                                input_persisted_hook,
                                contributes_to_agent_activity,
                                run_kind,
                                context_note,
                            ),
                        )
                else:
                    if waiting_work_admission is None:
                        queued_item = await self._chat_loop.queue_run(
                            agent_id,
                            message,
                            session_id=target_session_id,
                            sender=sender,
                            reply_surface=reply_surface,
                            project_id=project_id,
                            **tool_kwargs,
                            **_optional_run_kwargs(
                                input_persisted_hook,
                                contributes_to_agent_activity,
                                run_kind,
                                context_note,
                            ),
                        )
                    else:
                        queued_item = await self._chat_loop.queue_run(
                            agent_id,
                            message,
                            session_id=target_session_id,
                            sender=sender,
                            reply_surface=reply_surface,
                            project_id=project_id,
                            **tool_kwargs,
                            waiting_work_admission=waiting_work_admission,
                            **_optional_run_kwargs(
                                input_persisted_hook,
                                contributes_to_agent_activity,
                                run_kind,
                                context_note,
                            ),
                        )
                try:
                    return await queued_item.future
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise
                    # Removing one Queue item is a domain cancellation, not
                    # cancellation of the producer's long-lived scheduler.
                    raise RunCancelledError("Queued Run was removed before admission") from None
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

    def running_run(self, run_id: str) -> Run | None:
        """Return the Run *run_id* while it runs; None once it ended or when it is unknown."""
        if not self._chat_run_manager.is_running(run_id):
            return None
        return self._chat_run_manager.get(run_id)

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

    async def start_compaction_run(
        self,
        agent_id: str,
        session_id: str,
        instruction: str | None = None,
        *,
        project_id: str | None = None,
    ) -> Run:
        """Start manual Compaction as the Session's observable active Run."""
        return await self._chat_loop.start_compaction_run(
            agent_id,
            session_id,
            instruction,
            project_id=project_id,
        )
