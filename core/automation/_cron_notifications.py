"""Durable Cron notifications: retry unsent messages without repeating their Runs."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from core.automation import _cron_timing as _timing
from core.automation._cron_delivery import (
    CHANNEL_GONE_HOURS,
    channel_gone_alert,
    chat_message_text,
    check_chat_delivery,
    dropped_messages_line,
    outbound_note,
)
from core.automation._cron_jobs import (
    CronDelivery,
    CronJob,
    CronJobValidationError,
    CronStorageError,
)
from core.automation._cron_notification_model import CronNotification
from core.channels import ChannelError, ChannelNotFoundError
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp, parse_canonical_timestamp
from core.utils.workers import settle_before_cancelling

if TYPE_CHECKING:
    from core.automation.cron import CronService

_LOGGER = get_logger("automation.cron")
_RETRY_SECONDS = 30
_UNCERTAIN = "Delivery could not be confirmed. Check the chat before retrying this notification."
# The most chat messages one job keeps waiting; a new one drops the oldest beyond it.
MAX_RETAINED_NOTIFICATIONS = 20


class CronNotifications:
    """The job file owns messages until the platform confirms their delivery.

    A durable sending marker prevents blind retries after an ambiguous send or
    process interruption. Known-unsent failures keep retrying, even for paused,
    completed or failed jobs, except that messages for a Channel that no longer
    exists are dropped after ``CHANNEL_GONE_HOURS`` with an alert. A job keeps
    at most ``MAX_RETAINED_NOTIFICATIONS``; the next sent message counts the
    ones dropped. Editing delivery never redirects an older message.
    """

    def __init__(self, owner: CronService) -> None:
        self._owner = owner
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

    def stage(self, job: CronJob, text: str, *, session_id: str | None = None) -> None:
        delivery = job.delivery
        if delivery is None or delivery.kind != "chat":
            return
        assert delivery.channel_id and delivery.platform_target
        text = chat_message_text(self._owner._secret_masker().mask(text))
        kept = (
            *job.notifications,
            CronNotification(
                id=uuid4().hex,
                agent_id=job.agent_id,
                channel_id=delivery.channel_id,
                platform_target=delivery.platform_target,
                platform=delivery.platform,
                thread_id=delivery.thread_id,
                text=text,
                note=outbound_note(job, text),
                session_id=session_id,
            ),
        )
        excess = len(kept) - MAX_RETAINED_NOTIFICATIONS
        if excess > 0:
            # A message being sent stays: its outcome is still to be recorded.
            oldest = {item.id for item in kept[:-1] if item.state != "sending"}
            dropped = [item.id for item in kept[:-1] if item.id in oldest][:excess]
            kept = tuple(item for item in kept if item.id not in dropped)
            job.dropped_notifications += len(dropped)
            _LOGGER.warning(
                "Cron notifications dropped unsent (job=%s dropped=%d waiting=%d)",
                job.id,
                len(dropped),
                len(kept),
            )
        job.notifications = kept

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="cron-notifications")

    def wake(self) -> None:
        """A persisted message can be sent without holding a Cron Run slot."""
        self._wake.set()

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def aclose(self) -> None:
        self.stop()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            await self.flush()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=_RETRY_SECONDS)

    async def retry(self, job_id: str, delivery: CronDelivery | None = None) -> int:
        """Explicit retry after the user checked an uncertain delivery."""
        async with self._owner._edits, self._lock:
            return await settle_before_cancelling(self._retry_locked(job_id, delivery))

    async def _retry_locked(self, job_id: str, delivery: CronDelivery | None) -> int:
        self._owner.get_job(job_id)
        job = self._owner._jobs[job_id]
        replacements: dict[str, CronNotification] = {}
        if delivery is not None:
            replacements = await self._reselect(job, delivery)
            retained = {item.id for item in job.notifications if item.state == "pending"}
            replacements = {key: item for key, item in replacements.items() if key in retained}
            if not replacements:
                raise CronJobValidationError(
                    "No pending messages are blocked by an unknown or changed platform. "
                    "Nothing was changed."
                )
        previous = {item.id: item for item in job.notifications}
        alert = job.unread_alert
        edited = {
            item.id: replacements.get(item.id, item)
            if delivery is not None
            else replace(item, state="pending", error=None, retry_after_at=None)
            for item in job.notifications
        }
        job.notifications = tuple(edited.values())
        if delivery is None and alert is not None and alert.kind == "delivery":
            # Retrying answers the alert about the delivery.
            job.unread_alert = None

        def undo() -> None:
            # A Run can stage more messages while this write yields. Restore only
            # this retry's changes, without losing new items or reviving evictions.
            job.notifications = tuple(
                previous[item.id] if edited.get(item.id) == item else item
                for item in job.notifications
            )
            if job.unread_alert is None:
                job.unread_alert = alert

        await self._owner._save_edit(undo)
        self.wake()
        return len(replacements)

    async def _reselect(self, job: CronJob, delivery: CronDelivery) -> dict[str, CronNotification]:
        """Rebind only provably unsent platform-blocked snapshots to an explicit target."""
        if delivery.kind != "chat":
            raise CronJobValidationError("Select a chat destination for the blocked messages")
        selected = self._owner._clone_job(job)
        selected.delivery = delivery
        await check_chat_delivery(selected, self._owner._chats, bind_platform=True)
        target = selected.delivery
        assert target is not None and target.channel_id and target.platform_target
        chats = self._owner._chats
        assert chats is not None
        replacements: dict[str, CronNotification] = {}
        for item in job.notifications:
            if item.state != "pending" or item.agent_id != job.agent_id:
                continue
            if item.platform is not None:
                try:
                    original_channel = await chats.get_channel(item.channel_id)
                except ChannelNotFoundError:
                    continue
                if original_channel.platform == item.platform:
                    continue
            replacements[item.id] = replace(
                item,
                channel_id=target.channel_id,
                platform=target.platform,
                platform_target=target.platform_target,
                thread_id=target.thread_id,
                platform_blocked=False,
                error=None,
                retry_after_at=None,
                channel_missing_since=None,
            )
        return replacements

    async def flush(self, job_id: str | None = None) -> None:
        async with self._lock:
            jobs = list(self._owner._jobs) if job_id is None else [job_id]
            for key in jobs:
                job = self._owner._jobs.get(key)
                if job is None:
                    continue
                # Keep each conversation's notifications in order: one still
                # waiting holds back only later ones of the same chat and thread.
                held: set[tuple[str, str | None, str, str | None]] = set()
                for snapshot in job.notifications:
                    conversation = (
                        snapshot.channel_id,
                        snapshot.platform,
                        snapshot.platform_target,
                        snapshot.thread_id,
                    )
                    if conversation in held:
                        continue
                    live = self._owner._jobs.get(key)
                    item = next(
                        (n for n in (live.notifications if live else ()) if n.id == snapshot.id),
                        None,
                    )
                    if item is None:
                        # Given up meanwhile with the other messages for a missing Channel.
                        continue
                    if item.state == "sending":
                        await self._update(key, replace(item, state="uncertain", error=_UNCERTAIN))
                        await self._alert(key)
                    elif item.state == "pending" and item.due(_timing._utc_now()):
                        await self._send(key, item)
                    latest = self._owner._jobs.get(key)
                    if latest is None:
                        break
                    if any(
                        n.id == item.id and n.state != "uncertain" for n in latest.notifications
                    ):
                        held.add(conversation)

    async def _update(
        self,
        job_id: str,
        item: CronNotification,
        *,
        sent: bool = False,
        announced_drops: int = 0,
        persist: bool = True,
    ) -> bool:
        """Record ``item``'s new state; ``persist=False`` keeps the change in memory only."""
        job = self._owner._jobs.get(job_id)
        if job is None or not any(old.id == item.id for old in job.notifications):
            return False
        # Only an Agent rename changes a message's Agent; one it made meanwhile stays.
        job.notifications = tuple(
            replace(item, agent_id=old.agent_id) if old.id == item.id else old
            for old in job.notifications
            if not (sent and old.id == item.id)
        )
        if sent:
            job.last_delivered_at = _timing._utc_now_iso()
            # The sent message named these drops; ones made meanwhile wait for the next.
            job.dropped_notifications = max(job.dropped_notifications - announced_drops, 0)
        if not persist:
            return True
        try:
            await settle_before_cancelling(self._owner._save_jobs_async())
        except CronStorageError:
            _LOGGER.error(
                "Cron notification state not saved (job=%s notification=%s)", job_id, item.id
            )
            return False
        finally:
            self._owner._notify_changed()
        return True

    async def _send(self, job_id: str, item: CronNotification) -> None:
        chats = self._owner._chats
        try:
            if chats is None:
                raise ChannelError("Channels are not running")
            channel = await chats.get_channel(item.channel_id)
        except ChannelNotFoundError as error:
            await self._channel_missing(job_id, item, error)
            return
        except Exception as error:
            await self._defer(job_id, item, error)
            return
        # The Channel exists (again), so no deadline for a missing one runs.
        item = replace(item, channel_missing_since=None, platform_blocked=False)
        if channel.agent_id != item.agent_id:
            moved = ChannelError("The Channel now belongs to another Agent")
            await self._defer(job_id, item, moved)
            return
        if item.platform is None or channel.platform != item.platform:
            reason = (
                "its original platform is unknown"
                if item.platform is None
                else f"the Channel platform changed from {item.platform} to {channel.platform}"
            )
            await self._defer(
                job_id,
                replace(item, platform_blocked=True),
                ChannelError(
                    f"Delivery is blocked: {reason}. "
                    "Select a destination when retrying these messages."
                ),
            )
            return
        sending = replace(item, state="sending", error=None)
        if not await self._update(job_id, sending):
            # Nothing was sent; a later successful write must retain that fact.
            await self._update(job_id, replace(item, error="Notification storage is unavailable"))
            return
        job = self._owner._jobs.get(job_id)
        if job is None:
            # Deleting the job while its claim saves discards this notification
            # before any transport send has started.
            return
        drops = job.dropped_notifications
        text = self._owner._secret_masker().mask(item.text)
        if drops:
            text = f"{dropped_messages_line(drops)}\n\n{text}"
        try:
            await chats.send(
                item.channel_id,
                text,
                item.platform_target,
                thread_id=item.thread_id,
                expected_agent_id=item.agent_id,
                expected_platform=item.platform,
            )
        except ChannelError as error:
            if not error.possibly_delivered:
                await self._defer(job_id, item, error)
                return
            await self._update(job_id, replace(item, state="uncertain", error=_UNCERTAIN))
            await self._alert(job_id)
            return
        except asyncio.CancelledError:
            # The saved sending marker is reconciled on the next start.
            raise
        except Exception:
            await self._update(job_id, replace(item, state="uncertain", error=_UNCERTAIN))
            await self._alert(job_id)
            return
        await self._update(job_id, item, sent=True, announced_drops=drops)
        try:
            await chats.record_outbound_note(
                item.channel_id,
                item.platform_target,
                self._owner._secret_masker().mask(item.note),
                thread_id=item.thread_id,
                skip_session_id=item.session_id,
                expected_agent_id=item.agent_id,
                expected_platform=item.platform,
            )
        except Exception:
            # A missing mirror note must never repeat an already sent message.
            _LOGGER.warning("Cron sent notification was not noted in its Session (job=%s)", job_id)

    async def _defer(self, job_id: str, item: CronNotification, error: Exception) -> None:
        message = self._owner._secret_masker().mask(str(error) or type(error).__name__)[:500]
        delay = max(_RETRY_SECONDS, min(float(getattr(error, "retry_after", 0) or 0), 86400))
        if item.error != message:
            _LOGGER.warning(
                "Cron notification waiting for delivery (job=%s channel=%s)",
                job_id,
                item.channel_id,
            )
        retry_after_at = format_canonical_timestamp(_timing._utc_now() + timedelta(seconds=delay))
        updated = replace(item, state="pending", error=message, retry_after_at=retry_after_at)
        job = self._owner._jobs.get(job_id)
        stored = None
        if job is not None:
            stored = next((old for old in job.notifications if old.id == item.id), None)
        # A new retry time at the usual pace alone is not worth rewriting the job
        # file: after a restart the message is simply tried at once.
        unchanged = stored is not None and replace(stored, retry_after_at=None) == replace(
            updated, retry_after_at=None
        )
        await self._update(job_id, updated, persist=not unchanged or delay > _RETRY_SECONDS)

    async def _channel_missing(
        self, job_id: str, item: CronNotification, error: ChannelNotFoundError
    ) -> None:
        """Wait for a Channel that no longer exists, then give its messages up with an alert.

        It may be created again meanwhile; the wait starts when a send first
        finds it missing.
        """
        now = _timing._utc_now()
        since = item.channel_missing_since
        if since is None:
            missing = replace(item, channel_missing_since=format_canonical_timestamp(now))
            await self._defer(job_id, missing, error)
            return
        if now - parse_canonical_timestamp(since) < timedelta(hours=CHANNEL_GONE_HOURS):
            await self._defer(job_id, item, error)
            return
        job = self._owner._jobs.get(job_id)
        if job is None:
            return
        given_up = {
            old.id
            for old in job.notifications
            if old.channel_id == item.channel_id and old.state != "sending"
        }
        job.notifications = tuple(old for old in job.notifications if old.id not in given_up)
        _LOGGER.warning(
            "Cron notifications dropped: their Channel no longer exists "
            "(job=%s channel=%s dropped=%d)",
            job_id,
            item.channel_id,
            len(given_up),
        )
        # The alert saves the job with the messages removed.
        await self._owner._record_alert(
            job_id, channel_gone_alert(job, item.channel_id, len(given_up)), kind="delivery"
        )

    async def _alert(self, job_id: str) -> None:
        await self._owner._record_alert(job_id, _UNCERTAIN, kind="delivery")
