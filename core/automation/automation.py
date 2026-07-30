"""Automation primitives for programmatic chat run triggering."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.chat import ChatLoop, MessageSender, ReplySurface
from core.chat.content_blocks import ContentBlock
from core.runs import (
    ActiveRunError,
    ChatRunManager,
    Run,
    RunNotFoundError,
    RunStatus,
    WaitingWorkAdmission,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime
    from core.sessions import ChatSessionManager


_LOGGER = get_logger("automation")

AUTOMATIC_COMPLETION_GUIDANCE = (
    "Automatic completion delivery — this is not a new user request. Do not restart or "
    "repeat work merely because this report arrived. Re-evaluate the original user goal "
    "and current system state before taking further action."
)
_SUPPRESSED_ORIGIN_LIMIT = 256

CompletionSessionKey = tuple[str | None, str, str]


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
        self._buckets: dict[CompletionSessionKey, _CompletionBucket] = {}
        self._suppressed_origins: dict[CompletionSessionKey, list[str]] = {}

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
        key = (project_id, agent_id, session_id)
        bucket = self._buckets.setdefault(key, _CompletionBucket())
        existing = bucket.notices.get(notice_id)
        if existing is not None:
            return existing.delivered

        boundary_run = self._active_run(key)
        suppress_run = origin_run_id in self._suppressed_origins.get(
            key, ()
        ) or self._run_was_user_cancelled(origin_run_id)
        if suppress_run:
            self._suppress_origin(key, origin_run_id)
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
                self._deliver(key, bucket),
                name=f"completion-delivery:{agent_id}:{session_id}",
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
        key = (project_id, agent_id, session_id)
        bucket = self._buckets.get(key)
        if bucket is None:
            return False
        notice = bucket.notices.pop(notice_id, None)
        if notice is None:
            return False
        if not notice.delivered.done():
            notice.delivered.cancel()
        return True

    async def _deliver(
        self,
        key: CompletionSessionKey,
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
                        self._suppress_origin(key, boundary_run.id)
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
                    active_run = self._active_run(key)
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
                    await self._persist_without_run(key, bucket, suppressed)
                    continue

                active_run = self._active_run(key)
                if active_run is not None:
                    for notice in pending:
                        notice.boundary_run = active_run
                    continue

                message = _completion_message(pending)
                try:
                    run = await self._chat_loop.start_run(
                        key[1],
                        message,
                        session_id=key[2],
                        internal=True,
                        project_id=key[0],
                        input_persisted_hook=self._acknowledgement_callback(key, bucket, pending),
                    )
                except ActiveRunError:
                    # Another ingress won the idle-session race. Keep the exact
                    # notices pending and collect everything that finishes while
                    # that Run is active.
                    active_run = self._active_run(key)
                    if active_run is not None:
                        for notice in pending:
                            notice.boundary_run = active_run
                    else:
                        await asyncio.sleep(0)
                    continue
                except Exception as error:
                    self._fail(bucket, pending, error)
                    continue

                await _wait_for_terminal_run(run)
                # If execution failed before the initiating note reached disk,
                # retain the evidence without recursively starting another Run.
                undelivered = [notice for notice in pending if not notice.delivered.done()]
                if undelivered:
                    for notice in undelivered:
                        notice.suppress_run = True
                    await self._persist_without_run(key, bucket, undelivered)
        except BaseException as error:
            self._fail(bucket, list(bucket.notices.values()), error)
            if isinstance(error, asyncio.CancelledError):
                raise
        finally:
            if not bucket.notices and self._buckets.get(key) is bucket:
                self._buckets.pop(key, None)

    def _active_run(self, key: CompletionSessionKey) -> Run | None:
        return self._run_manager.active_run(
            agent_id=key[1],
            session_id=key[2],
            project_id=key[0],
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
        key: CompletionSessionKey,
        bucket: _CompletionBucket,
        notices: list[_CompletionNotice],
    ) -> None:
        """Persist cancelled-chain results as notes without waking the Agent."""
        if self._sessions is None:
            self._fail(
                bucket,
                notices,
                RuntimeError("completion delivery Session service is unavailable"),
            )
            return
        try:
            async with self._sessions.write_lock(key[1], key[2], key[0]):
                session = self._sessions.get(key[1], key[2], key[0])
                session.add_note(_completion_message(notices))
        except Exception as error:
            self._fail(bucket, notices, error)
            return
        self._acknowledge(key, bucket, notices)

    def _acknowledge(
        self,
        key: CompletionSessionKey,
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
                        key[1],
                        key[2],
                        notice.id,
                        exc_info=True,
                    )
            if not notice.delivered.done():
                notice.delivered.set_result(None)

    def _acknowledgement_callback(
        self,
        key: CompletionSessionKey,
        bucket: _CompletionBucket,
        notices: list[_CompletionNotice],
    ) -> Callable[[], None]:
        def acknowledge() -> None:
            self._acknowledge(key, bucket, notices)

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
                notice.delivered.set_exception(error)

    def _suppress_origin(self, key: CompletionSessionKey, run_id: str) -> None:
        origins = self._suppressed_origins.setdefault(key, [])
        if run_id in origins:
            return
        origins.append(run_id)
        if len(origins) > _SUPPRESSED_ORIGIN_LIMIT:
            del origins[: len(origins) - _SUPPRESSED_ORIGIN_LIMIT]


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
