"""Automation primitives for programmatic chat run triggering."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.chat import ChatLoop, MessageSender, ReplySurface
from core.chat.content_blocks import ContentBlock
from core.runs import (
    ActiveRunError,
    ChatRunManager,
    Run,
    RunKind,
    RunNotFoundError,
    RunStatus,
    WaitingWorkAdmission,
)
from core.sessions import SessionAddress
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime
    from core.sessions import ChatSession, ChatSessionManager


_LOGGER = get_logger("automation")

AUTOMATIC_COMPLETION_GUIDANCE = (
    "Automatic completion delivery — this is not a new user request. Do not restart or "
    "repeat work merely because this report arrived. Re-evaluate the original user goal "
    "and current system state before taking further action."
)
_SUPPRESSED_ORIGIN_LIMIT = 256
_COMPLETION_PERSIST_RETRY_INITIAL_SECONDS = 0.25
_COMPLETION_PERSIST_RETRY_MAX_SECONDS = 30.0


@dataclass
class _CompletionNotice:
    id: str
    origin_run_id: str
    body: str
    delivered: asyncio.Future[None]
    boundary_run: Run | None
    on_persisted: Callable[[], None] | None = None
    suppress_run: bool = False


@dataclass
class _CompletionBucket:
    notices: dict[str, _CompletionNotice] = field(default_factory=dict)
    delivery_task: asyncio.Task[None] | None = None


class _CompletionDeliveryCoordinator:
    """Coalesce background results at Session Run boundaries."""

    def __init__(
        self,
        chat_loop: ChatLoop,
        run_manager: ChatRunManager,
        sessions: ChatSessionManager | None,
    ) -> None:
        self._chat_loop = chat_loop
        self._run_manager = run_manager
        self._sessions = sessions
        self._buckets: dict[SessionAddress, _CompletionBucket] = {}
        self._suppressed_origins: dict[SessionAddress, list[str]] = {}
        self._closed = False

    def submit(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
        notice_id: str,
        origin_run_id: str,
        body: str,
        on_persisted: Callable[[], None] | None,
    ) -> asyncio.Future[None]:
        """Submit one result and return a Future resolved after durable delivery."""
        if self._closed:
            delivered: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            delivered.cancel()
            return delivered
        address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        bucket = self._buckets.setdefault(address, _CompletionBucket())
        existing = bucket.notices.get(notice_id)
        if existing is not None:
            return existing.delivered

        boundary_run = self._active_run(address)
        suppress_run = origin_run_id in self._suppressed_origins.get(
            address, ()
        ) or self._run_was_user_cancelled(origin_run_id)
        if suppress_run:
            self._suppress_origin(address, origin_run_id)
        delivered = asyncio.get_running_loop().create_future()
        notice = _CompletionNotice(
            id=notice_id,
            origin_run_id=origin_run_id,
            body=body,
            delivered=delivered,
            boundary_run=boundary_run,
            on_persisted=on_persisted,
            suppress_run=suppress_run,
        )
        bucket.notices[notice_id] = notice
        if bucket.delivery_task is None or bucket.delivery_task.done():
            bucket.delivery_task = asyncio.create_task(
                self._deliver(address, bucket),
                name=f"completion-delivery:{address.agent_id}:{address.session_id}",
            )
        return delivered

    def cancel(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
        notice_id: str,
    ) -> bool:
        """Withdraw one result before its automatic note is persisted."""
        address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        bucket = self._buckets.get(address)
        if bucket is None:
            return False
        notice = bucket.notices.pop(notice_id, None)
        if notice is None:
            return False
        if not notice.delivered.done():
            notice.delivered.cancel()
        return True

    def deliver_to_request(self, run: Run, session: ChatSession) -> bool:
        """Persist ready results for the next request of their active boundary Run."""
        if self._closed:
            return False
        address = SessionAddress(
            project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        bucket = self._buckets.get(address)
        if bucket is None or self._active_run(address) is not run:
            return False

        self._move_idle_notices_to_run(bucket, run)
        pending = [
            notice
            for notice in bucket.notices.values()
            if notice.boundary_run is run and not notice.suppress_run
        ]
        if not pending:
            return False

        try:
            session.add_note(_completion_message(pending))
        except Exception:
            # The delivery task still owns these notices. Leaving them pending
            # lets another Model-request boundary retry the append, or the
            # post-Run fallback persist them after this Run reaches terminal.
            _LOGGER.warning(
                "Completion persistence failed at a request boundary "
                "(agent=%s session=%s); keeping notices pending",
                address.agent_id,
                address.session_id,
                exc_info=True,
            )
            return False
        self._acknowledge(address, bucket, pending)
        return True

    async def _deliver(
        self,
        address: SessionAddress,
        bucket: _CompletionBucket,
    ) -> None:
        try:
            while bucket.notices:
                boundary_run = next(iter(bucket.notices.values())).boundary_run
                if boundary_run is not None:
                    await _wait_for_terminal_run(boundary_run)
                    await asyncio.sleep(0)
                    if (
                        boundary_run.status == RunStatus.CANCELLED
                        and boundary_run.cancel_reason == "user"
                    ):
                        self._suppress_origin(address, boundary_run.id)
                        for notice in bucket.notices.values():
                            if notice.boundary_run is not boundary_run:
                                continue
                            notice.suppress_run = True
                    pending = [
                        notice
                        for notice in bucket.notices.values()
                        if notice.boundary_run is boundary_run
                    ]
                else:
                    # One event-loop turn collects sibling completions that become
                    # ready together while the Session is already idle.
                    await asyncio.sleep(0)
                    active_run = self._active_run(address)
                    if active_run is not None:
                        self._move_idle_notices_to_run(bucket, active_run)
                        continue
                    pending = [
                        notice for notice in bucket.notices.values() if notice.boundary_run is None
                    ]

                if not pending:
                    continue
                suppressed = [notice for notice in pending if notice.suppress_run]
                if suppressed:
                    await self._persist_without_run(address, bucket, suppressed)
                    continue

                active_run = self._active_run(address)
                if active_run is not None:
                    for notice in pending:
                        notice.boundary_run = active_run
                    continue

                message = _completion_message(pending)
                try:
                    run = await self._chat_loop.start_run(
                        address.agent_id,
                        message,
                        session_id=address.session_id,
                        internal=True,
                        project_id=address.project_id,
                        input_persisted_hook=self._acknowledgement_callback(
                            address, bucket, pending
                        ),
                        run_kind=RunKind.SYSTEM,
                    )
                except ActiveRunError:
                    # Another ingress won the idle-session race. Keep the exact
                    # notices pending and collect everything that finishes while
                    # that Run is active.
                    active_run = self._active_run(address)
                    if active_run is not None:
                        for notice in pending:
                            notice.boundary_run = active_run
                    else:
                        await asyncio.sleep(0)
                    continue
                except Exception:
                    # Starting a follow-up Run is only the wake-up mechanism.
                    # The durable delivery boundary is the Session note, so a
                    # start failure degrades to a non-waking System Reminder.
                    _LOGGER.warning(
                        "Completion Run start failed (agent=%s session=%s); "
                        "persisting without a Run",
                        address.agent_id,
                        address.session_id,
                        exc_info=True,
                    )
                    for notice in pending:
                        if bucket.notices.get(notice.id) is notice:
                            notice.boundary_run = None
                    await self._persist_without_run(address, bucket, pending)
                    continue

                await _wait_for_terminal_run(run)
                # If execution failed before the initiating note reached disk,
                # retain the evidence without recursively starting another Run.
                undelivered = [notice for notice in pending if not notice.delivered.done()]
                if undelivered:
                    for notice in undelivered:
                        notice.suppress_run = True
                    await self._persist_without_run(address, bucket, undelivered)
        except BaseException as error:
            self._fail(bucket, list(bucket.notices.values()), error)
            if isinstance(error, asyncio.CancelledError):
                raise
        finally:
            if not bucket.notices and self._buckets.get(address) is bucket:
                self._buckets.pop(address, None)

    def _active_run(self, address: SessionAddress) -> Run | None:
        return self._run_manager.active_run(
            agent_id=address.agent_id,
            session_id=address.session_id,
            project_id=address.project_id,
        )

    @staticmethod
    def _move_idle_notices_to_run(bucket: _CompletionBucket, run: Run) -> None:
        for notice in bucket.notices.values():
            if notice.boundary_run is None:
                notice.boundary_run = run

    def _run_was_user_cancelled(self, run_id: str) -> bool:
        try:
            run = self._run_manager.get(run_id)
        except RunNotFoundError:
            return False
        return run.status == RunStatus.CANCELLED and run.cancel_reason == "user"

    async def _persist_without_run(
        self,
        address: SessionAddress,
        bucket: _CompletionBucket,
        notices: list[_CompletionNotice],
    ) -> None:
        """Persist results as a System Reminder without waking the Agent."""
        if self._sessions is None:
            self._fail(
                bucket,
                notices,
                RuntimeError("completion delivery Session service is unavailable"),
            )
            return

        retry_delay = _COMPLETION_PERSIST_RETRY_INITIAL_SECONDS
        while True:
            pending = self._still_pending(bucket, notices)
            if not pending:
                return

            async with self._sessions.write_lock(address):
                pending = self._still_pending(bucket, notices)
                if not pending:
                    return
                try:
                    session = self._sessions.get(address)
                except Exception as error:
                    # There is no durable target left. A terminal delivery
                    # failure lets producers release their process-local state.
                    self._fail(bucket, pending, error)
                    return
                try:
                    session.add_note(_completion_message(pending))
                except Exception:
                    _LOGGER.warning(
                        "Completion persistence failed (agent=%s session=%s); "
                        "retrying in %.2f seconds",
                        address.agent_id,
                        address.session_id,
                        retry_delay,
                        exc_info=True,
                    )
                else:
                    self._acknowledge(address, bucket, pending)
                    return

            await asyncio.sleep(retry_delay)
            retry_delay = min(
                retry_delay * 2,
                _COMPLETION_PERSIST_RETRY_MAX_SECONDS,
            )

    @staticmethod
    def _still_pending(
        bucket: _CompletionBucket,
        notices: list[_CompletionNotice],
    ) -> list[_CompletionNotice]:
        """Return only notices that still belong to this delivery attempt."""
        return [notice for notice in notices if bucket.notices.get(notice.id) is notice]

    def _acknowledge(
        self,
        address: SessionAddress,
        bucket: _CompletionBucket,
        notices: list[_CompletionNotice],
    ) -> None:
        """Resolve exact notices only after their combined note is persisted."""
        for notice in notices:
            if bucket.notices.get(notice.id) is not notice:
                continue
            bucket.notices.pop(notice.id, None)
            if notice.on_persisted is not None:
                try:
                    notice.on_persisted()
                except Exception:
                    _LOGGER.warning(
                        "Completion persistence callback failed (agent=%s session=%s notice=%s)",
                        address.agent_id,
                        address.session_id,
                        notice.id,
                        exc_info=True,
                    )
            if not notice.delivered.done():
                notice.delivered.set_result(None)

    def _acknowledgement_callback(
        self,
        address: SessionAddress,
        bucket: _CompletionBucket,
        notices: list[_CompletionNotice],
    ) -> Callable[[], None]:
        def acknowledge() -> None:
            self._acknowledge(address, bucket, notices)

        return acknowledge

    @staticmethod
    def _fail(
        bucket: _CompletionBucket,
        notices: list[_CompletionNotice],
        error: BaseException,
    ) -> None:
        for notice in notices:
            if bucket.notices.get(notice.id) is not notice:
                continue
            bucket.notices.pop(notice.id, None)
            if not notice.delivered.done():
                if isinstance(error, asyncio.CancelledError):
                    notice.delivered.cancel()
                else:
                    notice.delivered.set_exception(error)

    def _suppress_origin(self, address: SessionAddress, run_id: str) -> None:
        origins = self._suppressed_origins.setdefault(address, [])
        if run_id in origins:
            return
        origins.append(run_id)
        if len(origins) > _SUPPRESSED_ORIGIN_LIMIT:
            del origins[: len(origins) - _SUPPRESSED_ORIGIN_LIMIT]

    async def aclose(self) -> None:
        """Cancel delivery workers and settle every producer-facing Future."""
        if self._closed:
            return
        self._closed = True
        tasks = tuple(
            bucket.delivery_task
            for bucket in self._buckets.values()
            if bucket.delivery_task is not None and not bucket.delivery_task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for bucket in self._buckets.values():
            for notice in bucket.notices.values():
                if not notice.delivered.done():
                    notice.delivered.cancel()
        self._buckets.clear()
        self._suppressed_origins.clear()


async def _wait_for_terminal_run(run: Run) -> None:
    try:
        await run.wait()
    except Exception:
        # Status and cancel_reason on the Run are the authoritative facts here;
        # completion delivery must continue after failed or cancelled work.
        return


def _completion_message(notices: list[_CompletionNotice]) -> str:
    sections = [AUTOMATIC_COMPLETION_GUIDANCE, "", "Results:"]
    for notice in notices:
        sections.extend(("", notice.body))
    return "\n".join(sections)


def _optional_run_kwargs(
    callback: Callable[[], None] | None,
    contributes_to_agent_activity: bool,
    run_kind: RunKind,
    resume_process_restart: bool = False,
) -> dict[str, Any]:
    """Project non-default Run options onto the ChatLoop call."""
    options: dict[str, Any] = {}
    if callback is not None:
        options["input_persisted_hook"] = callback
    if not contributes_to_agent_activity:
        options["contributes_to_agent_activity"] = False
    if run_kind is not RunKind.USER:
        options["run_kind"] = run_kind
    if resume_process_restart:
        options["resume_process_restart"] = True
    return options


def _optional_tool_access_kwargs(
    restriction: Sequence[str] | None,
    denial_resolver: Callable[[str], str | None] | None,
) -> dict[str, Any]:
    """Omit unrestricted defaults so unrelated trigger call shapes stay stable."""
    options: dict[str, Any] = {}
    if restriction is not None:
        options["tool_restriction"] = restriction
    if denial_resolver is not None:
        options["tool_denial_resolver"] = denial_resolver
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
        sessions: ChatSessionManager | None = None,
    ) -> None:
        self._chat_loop = chat_loop
        self._trigger_chat_loop = trigger_chat_loop or chat_loop
        self._chat_run_manager = chat_run_manager
        self._runtime = runtime
        self._completion_delivery = _CompletionDeliveryCoordinator(
            self._trigger_chat_loop,
            chat_run_manager,
            sessions,
        )

    def submit_completion(
        self,
        agent_id: str,
        session_id: str,
        *,
        notice_id: str,
        origin_run_id: str,
        body: str,
        project_id: str | None = None,
        on_persisted: Callable[[], None] | None = None,
    ) -> asyncio.Future[None]:
        """Coalesce one background result at the target Session's next Run boundary."""
        return self._completion_delivery.submit(
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
            notice_id=notice_id,
            origin_run_id=origin_run_id,
            body=body,
            on_persisted=on_persisted,
        )

    def cancel_completion(
        self,
        agent_id: str,
        session_id: str,
        *,
        notice_id: str,
        project_id: str | None = None,
    ) -> bool:
        """Withdraw one pending automatic result after another durable delivery."""
        return self._completion_delivery.cancel(
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
            notice_id=notice_id,
        )

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
        tool_restriction: Sequence[str] | None = None,
        tool_denial_resolver: Callable[[str], str | None] | None = None,
        waiting_work_admission: WaitingWorkAdmission | None = None,
        input_persisted_hook: Callable[[], None] | None = None,
        run_kind: RunKind = RunKind.USER,
        contributes_to_agent_activity: bool = True,
        resume_process_restart: bool = False,
    ) -> Run:
        """Start a run immediately, or queue it until the target session is idle.

        ``project_id=None`` keeps today's global/identity behavior. A set
        ``project_id`` creates the auto-session under that project's anchor and
        scopes the Run to the project (cwd = repo, project files in the prompt).
        """
        tool_access_kwargs = _optional_tool_access_kwargs(
            tool_restriction,
            tool_denial_resolver,
        )
        if session_id is None:
            try:
                if internal:
                    return await self._trigger_chat_loop.start_run_in_new_session(
                        agent_id,
                        message,
                        internal=True,
                        reply_surface=reply_surface,
                        project_id=project_id,
                        **tool_access_kwargs,
                        **_optional_run_kwargs(
                            input_persisted_hook,
                            contributes_to_agent_activity,
                            run_kind,
                            resume_process_restart,
                        ),
                    )
                return await self._trigger_chat_loop.start_run_in_new_session(
                    agent_id,
                    message,
                    sender=sender,
                    reply_surface=reply_surface,
                    project_id=project_id,
                    **tool_access_kwargs,
                    **_optional_run_kwargs(
                        input_persisted_hook,
                        contributes_to_agent_activity,
                        run_kind,
                        resume_process_restart,
                    ),
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
                    **tool_access_kwargs,
                    **_optional_run_kwargs(
                        input_persisted_hook,
                        contributes_to_agent_activity,
                        run_kind,
                        resume_process_restart,
                    ),
                )
            else:
                run = await self._trigger_chat_loop.start_run(
                    agent_id,
                    message,
                    session_id=target_session_id,
                    sender=sender,
                    reply_surface=reply_surface,
                    project_id=project_id,
                    **tool_access_kwargs,
                    **_optional_run_kwargs(
                        input_persisted_hook,
                        contributes_to_agent_activity,
                        run_kind,
                        resume_process_restart,
                    ),
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
                            **tool_access_kwargs,
                            **_optional_run_kwargs(
                                input_persisted_hook,
                                contributes_to_agent_activity,
                                run_kind,
                                resume_process_restart,
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
                            **tool_access_kwargs,
                            waiting_work_admission=waiting_work_admission,
                            **_optional_run_kwargs(
                                input_persisted_hook,
                                contributes_to_agent_activity,
                                run_kind,
                                resume_process_restart,
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
                            **tool_access_kwargs,
                            **_optional_run_kwargs(
                                input_persisted_hook,
                                contributes_to_agent_activity,
                                run_kind,
                                resume_process_restart,
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
                            **tool_access_kwargs,
                            waiting_work_admission=waiting_work_admission,
                            **_optional_run_kwargs(
                                input_persisted_hook,
                                contributes_to_agent_activity,
                                run_kind,
                                resume_process_restart,
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
