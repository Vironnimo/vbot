"""completion."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.chat import ChatLoop, ChatMessage, ReplySurface, reply_surface_from_note
from core.chat.messages import REPLY_SURFACE_NOTE_PREFIX, SubAgentMessage
from core.runs import (
    ActiveRunError,
    ChatRunManager,
    Run,
    RunAdmissionBlockedError,
    RunExecutionOwner,
    RunKind,
    RunNotFoundError,
    RunStatus,
)
from core.sessions import SessionAddress, SessionNotFoundError
from core.utils.logging import get_logger
from core.utils.workers import finish_despite_cancel

if TYPE_CHECKING:
    from core.sessions import ChatSession, ChatSessionManager


_LOGGER = get_logger("automation")

AUTOMATIC_COMPLETION_GUIDANCE = (
    "Automatic completion delivery — this is not a new user request. Do not restart or "
    "repeat work merely because this report arrived. Re-evaluate the user's original "
    "request and current system state before taking further action."
)


UNREAD_COMPLETION_TEXT = "Your previous turn ended before you read the results reported above."


_SUPPRESSED_ORIGIN_LIMIT = 256


_COMPLETION_RETRY_INITIAL_SECONDS = 0.25


_COMPLETION_RETRY_MAX_SECONDS = 30.0


_sleep = asyncio.sleep


OwnedCompletionValidator = Callable[[SessionAddress, RunExecutionOwner], None]


@dataclass
class _CompletionNotice:
    id: str
    origin_run_id: str
    body: str
    delivered: asyncio.Future[None]
    boundary_run: Run | None
    suppress_run: bool = False
    execution_owner: RunExecutionOwner | None = None
    # False: reach an active Run at its next boundary, but never start one.
    wake: bool = True
    # Set when a request of ``boundary_run`` carried the notice: that Run's
    # ``iteration_count`` then. The Model has read it once the count grew;
    # until then a waking notice stays pending, so a Run that ends first
    # still wakes the Session.
    read_after: int | None = None
    # Set while a delivery builds and hands over a message carrying the
    # notice: it is being delivered and can no longer be withdrawn.
    claimed: bool = False
    # Display record of a ``message_parent`` message; the delivering note carries it.
    subagent_message: SubAgentMessage | None = None

    @property
    def read(self) -> bool:
        """Whether the Model answered a request that carried this notice."""
        run, count = self.boundary_run, self.read_after
        return run is not None and count is not None and run.iteration_count > count


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
        self._run_relay: Callable[[Run, ReplySurface], Awaitable[None]] | None = None
        self._closed = False
        self._owned_run_starter: Callable[..., Awaitable[Run]] | None = None
        self._owned_completion_validator: OwnedCompletionValidator | None = None

    def set_run_relay(
        self,
        relay: Callable[[Run, ReplySurface], Awaitable[None]] | None,
    ) -> None:
        """Set the accessor relay used by idle-session completion Runs."""
        self._run_relay = relay

    def set_owned_starter(self, starter: Callable[..., Awaitable[Run]]) -> None:
        self._owned_run_starter = starter

    def set_owned_validator(self, validator: OwnedCompletionValidator) -> None:
        self._owned_completion_validator = validator

    def has_execution_work(self, owner: RunExecutionOwner) -> bool:
        return any(
            notice.execution_owner == owner and not notice.read
            for bucket in self._buckets.values()
            for notice in bucket.notices.values()
        )

    def submit(
        self,
        address: SessionAddress,
        *,
        notice_id: str,
        origin_run_id: str,
        body: str,
        execution_owner: RunExecutionOwner | None = None,
        wake: bool = True,
        subagent_message: SubAgentMessage | None = None,
    ) -> asyncio.Future[None]:
        """Submit one result and return a Future resolved after durable delivery."""
        if self._closed or (
            execution_owner is not None and not self._owner_admits(address, execution_owner)
        ):
            # Rejected before any state is kept: a closed or stale owner can never
            # reach a Session, including the Session-write fallback.
            delivered: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            delivered.cancel()
            return delivered
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
            suppress_run=suppress_run,
            execution_owner=execution_owner,
            wake=wake,
            subagent_message=subagent_message,
        )
        bucket.notices[notice_id] = notice
        if bucket.delivery_task is None or bucket.delivery_task.done():
            bucket.delivery_task = asyncio.create_task(
                self._deliver(address, bucket),
                name=f"completion-delivery:{address.agent_id}",
            )
        return delivered

    def cancel(
        self,
        address: SessionAddress,
        *,
        notice_id: str,
    ) -> bool:
        """Withdraw one result before a delivery claimed it.

        Returns False when the result is gone, or a delivery claimed it: a
        message carrying it is being handed over or is in the Session already.
        """
        bucket = self._buckets.get(address)
        if bucket is None:
            return False
        notice = bucket.notices.get(notice_id)
        if notice is None or notice.claimed or notice.read_after is not None:
            return False
        bucket.notices.pop(notice_id, None)
        if not notice.delivered.done():
            notice.delivered.cancel()
        return True

    async def deliver_to_request(self, run: Run, session: ChatSession) -> ChatMessage | None:
        """Persist ready results for the next request of their active boundary Run."""
        if self._closed:
            return None
        address = SessionAddress(
            project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        bucket = self._buckets.get(address)
        if bucket is None or self._active_run(address) is not run:
            return None

        self._move_idle_notices_to_run(bucket, run)
        pending = [
            notice
            for notice in bucket.notices.values()
            if notice.boundary_run is run
            and not notice.suppress_run
            and notice.execution_owner == run.execution_owner
            and notice.read_after is None
        ]
        if not pending:
            return None

        message = ChatMessage.note(
            _completion_message(pending), subagent_messages=_subagent_messages(pending)
        )
        _claim(pending, True)
        try:
            # Request-boundary Notes are otherwise deferred. This append must
            # commit before a producer may retire its completion evidence.
            await finish_despite_cancel(session.append_async(message))
        except Exception:
            _claim(pending, False)
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
            return None
        except BaseException:
            _claim(pending, False)
            raise
        for notice in pending:
            notice.read_after = run.iteration_count
        self._acknowledge(address, bucket, [notice for notice in pending if not notice.wake])
        # Waking notices stay pending until the Model answered this request.
        for notice in pending:
            if notice.wake:
                self._confirm(notice)
        return message

    async def _deliver(
        self,
        address: SessionAddress,
        bucket: _CompletionBucket,
    ) -> None:
        admission_retry_delay = _COMPLETION_RETRY_INITIAL_SECONDS
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
                    pending = self._drop_read(
                        bucket,
                        [
                            notice
                            for notice in bucket.notices.values()
                            if notice.boundary_run is boundary_run
                        ],
                    )
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
                suppressed = [
                    notice for notice in pending if notice.suppress_run or not notice.wake
                ]
                if suppressed:
                    await self._persist_without_run(address, bucket, suppressed)
                    continue

                active_run = self._active_run(address)
                if active_run is not None:
                    _join_run(pending, active_run)
                    continue

                reply_surface = (
                    await self._latest_reply_surface(address)
                    if self._run_relay is not None
                    else None
                )
                # Results withdrawn meanwhile stay out of the message.
                pending = self._still_pending(bucket, pending)
                if not pending:
                    continue
                owner = pending[0].execution_owner
                pending = [notice for notice in pending if notice.execution_owner == owner]
                _claim(pending, True)
                message = _completion_message(pending)
                try:
                    if owner is not None:
                        # Each origin keeps its exact lifetime owner even if a target
                        # Session is later reused by unrelated work.
                        if self._owned_run_starter is None:
                            raise RuntimeError("owned completion starter is unavailable")
                        run = await self._owned_run_starter(
                            address,
                            owner,
                            message,
                            tuple(notice.id for notice in pending),
                            self._acknowledgement_callback(address, bucket, pending),
                        )
                    else:
                        subagent_messages = _subagent_messages(pending)
                        run = await self._chat_loop.start_run(
                            address.agent_id,
                            message,
                            session_id=address.session_id,
                            internal=True,
                            reply_surface=reply_surface,
                            project_id=address.project_id,
                            input_persisted_hook=self._acknowledgement_callback(
                                address, bucket, pending
                            ),
                            run_kind=RunKind.SYSTEM,
                            # An empty note never reaches the Model; it carries
                            # the display record ahead of the input note.
                            context_note=(
                                ChatMessage.note("", subagent_messages=subagent_messages)
                                if subagent_messages
                                else None
                            ),
                        )
                except RunAdmissionBlockedError as error:
                    _claim(pending, False)
                    if owner is not None:
                        # A stale owner (closed group, retired Extension) never
                        # becomes admissible again: fail its results terminally,
                        # as submission and the Session-write fallback do.
                        pending = self._reject_inadmissible_owned(
                            address, bucket, self._still_pending(bucket, pending)
                        )
                        if not pending:
                            continue
                    active_run = self._active_run(address)
                    if active_run is not None:
                        # A draining Run can still take the results at its next
                        # request boundary while new admission stays blocked.
                        _join_run(pending, active_run)
                        continue
                    # A lifecycle guard holds admission and does not announce its
                    # end, so retry with a bounded backoff.
                    _LOGGER.debug(
                        "Completion Run admission blocked (agent=%s session=%s reason=%s); "
                        "retrying in %.2f seconds",
                        address.agent_id,
                        address.session_id,
                        error,
                        admission_retry_delay,
                    )
                    await _sleep(admission_retry_delay)
                    admission_retry_delay = min(
                        admission_retry_delay * 2, _COMPLETION_RETRY_MAX_SECONDS
                    )
                    continue
                except ActiveRunError:
                    _claim(pending, False)
                    # Another ingress won the idle-session race. Keep the exact
                    # notices pending and collect everything that finishes while
                    # that Run is active.
                    active_run = self._active_run(address)
                    if active_run is not None:
                        _join_run(pending, active_run)
                    else:
                        await asyncio.sleep(0)
                    continue
                except Exception as error:
                    _claim(pending, False)
                    if owner is not None:
                        # An owned delivery must never bypass closed admission by
                        # falling back to an arbitrary Session write.
                        for notice in self._still_pending(bucket, pending):
                            bucket.notices.pop(notice.id, None)
                            if not notice.delivered.done():
                                notice.delivered.set_exception(error)
                        continue
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

                admission_retry_delay = _COMPLETION_RETRY_INITIAL_SECONDS
                if (
                    reply_surface is not None
                    and reply_surface.kind == "channel"
                    and self._run_relay is not None
                ):
                    try:
                        await self._run_relay(run, reply_surface)
                    except Exception:
                        _LOGGER.error(
                            "Completion Run relay failed (agent=%s session=%s surface=%s)",
                            address.agent_id,
                            address.session_id,
                            reply_surface.kind,
                            exc_info=True,
                        )
                        await _wait_for_terminal_run(run)
                else:
                    await _wait_for_terminal_run(run)
                # If execution failed before the initiating note reached disk,
                # retain the evidence without recursively starting another Run.
                # Results already in the Session need nothing more.
                self._acknowledge(
                    address, bucket, [notice for notice in pending if notice.read_after is not None]
                )
                undelivered = [notice for notice in pending if not notice.delivered.done()]
                # The Run ended before its message reached the Session.
                _claim(undelivered, False)
                if undelivered:
                    for notice in undelivered:
                        notice.suppress_run = True
                    await self._persist_without_run(address, bucket, undelivered)
        except BaseException as error:
            closing = self._closed and isinstance(error, asyncio.CancelledError)
            if not closing:
                # Closing settles the remaining notices itself (see ``aclose``).
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

    async def _latest_reply_surface(self, address: SessionAddress) -> ReplySurface | None:
        """Recover the most recent interactive surface for one persisted Session."""
        if self._sessions is None:
            return None
        try:
            session = await self._sessions.get_async(address)
            message = await session.latest_note_async(REPLY_SURFACE_NOTE_PREFIX)
        except Exception:
            _LOGGER.warning(
                "Cannot recover completion reply surface (agent=%s session=%s)",
                address.agent_id,
                address.session_id,
                exc_info=True,
            )
            return None
        return None if message is None else reply_surface_from_note(message)

    @staticmethod
    def _move_idle_notices_to_run(bucket: _CompletionBucket, run: Run) -> None:
        for notice in bucket.notices.values():
            if notice.boundary_run is None:
                notice.boundary_run = run

    def _owner_admits(self, address: SessionAddress, owner: RunExecutionOwner) -> bool:
        """Return whether the owner may still receive work in this Session.

        Without a validator no owned delivery is admitted: an owned result must
        never reach a Session unchecked.
        """
        validator = self._owned_completion_validator
        if validator is None:
            return False
        try:
            validator(address, owner)
        except RunAdmissionBlockedError:
            return False
        except Exception:
            _LOGGER.warning(
                "Owned completion validation failed (agent=%s session=%s); rejecting delivery",
                address.agent_id,
                address.session_id,
                exc_info=True,
            )
            return False
        return True

    def _reject_inadmissible_owned(
        self,
        address: SessionAddress,
        bucket: _CompletionBucket,
        notices: list[_CompletionNotice],
    ) -> list[_CompletionNotice]:
        """Fail owned notices whose owner went stale after submission; keep the rest."""
        rejected = [
            notice
            for notice in notices
            if notice.execution_owner is not None
            and not self._owner_admits(address, notice.execution_owner)
        ]
        if rejected:
            self._fail(
                bucket,
                rejected,
                RunAdmissionBlockedError("completion owner can no longer receive work"),
            )
        return [notice for notice in notices if notice not in rejected]

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
        retry_delay = _COMPLETION_RETRY_INITIAL_SECONDS
        while True:
            error = await self._persist_once(address, bucket, notices)
            if error is None:
                return
            _LOGGER.warning(
                "Completion persistence failed (agent=%s session=%s); retrying in %.2f seconds",
                address.agent_id,
                address.session_id,
                retry_delay,
                exc_info=error,
            )
            await _sleep(retry_delay)
            retry_delay = min(retry_delay * 2, _COMPLETION_RETRY_MAX_SECONDS)

    async def _persist_once(
        self,
        address: SessionAddress,
        bucket: _CompletionBucket,
        notices: list[_CompletionNotice],
    ) -> Exception | None:
        """Append one System Reminder; return the error of a retryable failure.

        ``None`` means every notice is settled: persisted, withdrawn, or failed
        terminally because no durable target is left.
        """
        sessions = self._sessions
        if sessions is None:
            self._fail(
                bucket,
                notices,
                RuntimeError("completion delivery Session service is unavailable"),
            )
            return None
        # This write bypasses Run admission, so an owned notice is checked
        # again: its owner may have gone stale since submission.
        pending = self._reject_inadmissible_owned(
            address, bucket, self._still_pending(bucket, notices)
        )
        if not pending:
            return None

        async with sessions.write_lock(address):
            pending = self._still_pending(bucket, notices)
            # A request already carried these; the Session holds them.
            self._acknowledge(
                address, bucket, [notice for notice in pending if notice.read_after is not None]
            )
            pending = self._still_pending(bucket, pending)
            if not pending:
                return None
            try:
                session = await sessions.get_async(address)
            except SessionNotFoundError as error:
                # There is no durable target left. A terminal delivery
                # failure lets producers release their process-local state.
                self._fail(bucket, pending, error)
                return None
            except Exception as error:
                return error
            # Results withdrawn meanwhile stay out of the note.
            pending = self._still_pending(bucket, pending)
            if not pending:
                return None
            _claim(pending, True)
            append = asyncio.ensure_future(
                session.add_note_async(
                    _completion_message(pending), subagent_messages=_subagent_messages(pending)
                )
            )
            try:
                await asyncio.shield(append)
            except asyncio.CancelledError:
                # Closing cancels delivery workers, but a started append still
                # lands. Settle it before the write lock is released so closing
                # never persists the same results twice.
                await asyncio.wait((append,))
                if not append.cancelled() and append.exception() is None:
                    self._acknowledge(address, bucket, pending)
                else:
                    _claim(pending, False)
                raise
            except Exception as error:
                _claim(pending, False)
                return error
            self._acknowledge(address, bucket, pending)
            return None

    async def close_execution_group(self, extension: str, group_id: str, epoch: str) -> None:
        """Withdraw this group's pending notices; its later submissions fail validation."""
        key = (extension, group_id, epoch)
        for bucket in self._buckets.values():
            for notice in list(bucket.notices.values()):
                owner = notice.execution_owner
                if owner is not None and (owner.extension, owner.group_id, owner.epoch) == key:
                    bucket.notices.pop(notice.id, None)
                    if not notice.delivered.done():
                        notice.delivered.cancel()

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
            self._confirm(notice)

    @staticmethod
    def _confirm(notice: _CompletionNotice) -> None:
        """Tell the producer, once, that the notice is persisted."""
        if not notice.delivered.done():
            notice.delivered.set_result(None)

    @staticmethod
    def _drop_read(
        bucket: _CompletionBucket, notices: list[_CompletionNotice]
    ) -> list[_CompletionNotice]:
        """Drop the notices the Model read; return the others."""
        unread: list[_CompletionNotice] = []
        for notice in notices:
            if not notice.read:
                unread.append(notice)
            elif bucket.notices.get(notice.id) is notice:
                bucket.notices.pop(notice.id, None)
        return unread

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
        """Stop delivery workers and settle every producer-facing Future.

        Results still pending are persisted as a System Reminder, the durable
        fallback used when a follow-up Run cannot start, so they survive a
        restart. Owned results are withdrawn instead: their owner closed first.
        Sessions must still be open.
        """
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
        for address, bucket in list(self._buckets.items()):
            pending = [
                notice for notice in bucket.notices.values() if notice.execution_owner is None
            ]
            if pending and self._sessions is not None:
                error = await self._persist_once(address, bucket, pending)
                if error is not None:
                    _LOGGER.error(
                        "Completion persistence failed at shutdown (agent=%s session=%s "
                        "results=%d); results dropped",
                        address.agent_id,
                        address.session_id,
                        len(self._still_pending(bucket, pending)),
                        exc_info=error,
                    )
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


def _claim(notices: list[_CompletionNotice], claimed: bool) -> None:
    """Claim *notices* for the message about to carry them, or release the
    claim when that message did not reach the Session."""
    for notice in notices:
        notice.claimed = claimed


def _join_run(notices: list[_CompletionNotice], run: Run) -> None:
    """Hand *notices* to the active *run*'s next request boundary.

    A notice already in the Session is in every request of a later Run, so
    any answer of *run* counts as reading it.
    """
    for notice in notices:
        notice.boundary_run = run
        if notice.read_after is not None:
            notice.read_after = 0


def _subagent_messages(notices: list[_CompletionNotice]) -> tuple[SubAgentMessage, ...]:
    """The display records of the messages *notices* deliver for the first time."""
    return tuple(
        notice.subagent_message
        for notice in notices
        if notice.subagent_message is not None and notice.read_after is None
    )


def _completion_message(notices: list[_CompletionNotice]) -> str:
    """The message delivering *notices*; those already in the Session are referred to."""
    sections = [AUTOMATIC_COMPLETION_GUIDANCE]
    if any(notice.read_after is not None for notice in notices):
        sections.extend(("", UNREAD_COMPLETION_TEXT))
    unsent = [notice for notice in notices if notice.read_after is None]
    if unsent:
        sections.extend(("", "Results:"))
        for notice in unsent:
            sections.extend(("", notice.body))
    return "\n".join(sections)
