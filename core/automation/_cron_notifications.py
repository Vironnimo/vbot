"""Durable Cron notifications: retry unsent messages without repeating their Runs."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from core.automation._cron_delivery import CronFailureAlert, outbound_note
from core.automation._cron_jobs import CronJob, CronStorageError
from core.automation._cron_notification_model import CronNotification
from core.automation._cron_timing import _utc_now_iso
from core.channels import ChannelError
from core.utils.logging import get_logger
from core.utils.workers import settle_before_cancelling

if TYPE_CHECKING:
    from core.automation.cron import CronService

_LOGGER = get_logger("automation.cron")
_RETRY_SECONDS = 30
_UNCERTAIN = "Delivery could not be confirmed. Check the chat before retrying this notification."


class CronNotifications:
    """The job file owns messages until the platform confirms their delivery.

    A durable sending marker prevents blind retries after an ambiguous send or
    process interruption. Known-unsent failures keep retrying, even for paused,
    completed or failed jobs. Editing delivery never redirects an older message.
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
        text = self._owner._secret_masker().mask(text)
        job.notifications += (
            CronNotification(
                id=uuid4().hex,
                agent_id=job.agent_id,
                channel_id=delivery.channel_id,
                platform_target=delivery.platform_target,
                thread_id=delivery.thread_id,
                text=text,
                note=outbound_note(job, text),
                session_id=session_id,
            ),
        )

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

    async def retry(self, job_id: str) -> None:
        """Explicit retry after the user checked an uncertain delivery."""
        async with self._lock:
            self._owner.get_job(job_id)
            job = self._owner._jobs[job_id]
            previous = job.notifications
            job.notifications = tuple(
                replace(item, state="pending", error=None, retry_at=0) for item in previous
            )
            await self._owner._save_edit(lambda: setattr(job, "notifications", previous))
        self.wake()

    async def flush(self, job_id: str | None = None) -> None:
        async with self._lock:
            jobs = list(self._owner._jobs) if job_id is None else [job_id]
            for key in jobs:
                job = self._owner._jobs.get(key)
                if job is None:
                    continue
                for item in job.notifications:
                    if item.state == "sending":
                        await self._update(key, replace(item, state="uncertain", error=_UNCERTAIN))
                        self._alert(key)
                    elif item.state == "pending" and item.retry_at <= time.time():
                        await self._send(key, item)
                    # Keep a conversation's notifications in order.
                    latest = self._owner._jobs.get(key)
                    if latest is None or any(
                        n.id == item.id and n.state != "uncertain" for n in latest.notifications
                    ):
                        break

    async def _update(self, job_id: str, item: CronNotification, *, sent: bool = False) -> bool:
        job = self._owner._jobs.get(job_id)
        if job is None:
            return False
        # Only an Agent rename changes a message's Agent; one it made meanwhile stays.
        job.notifications = tuple(
            replace(item, agent_id=old.agent_id) if old.id == item.id else old
            for old in job.notifications
            if not (sent and old.id == item.id)
        )
        if sent:
            job.last_delivered_at = _utc_now_iso()
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
            if channel.agent_id != item.agent_id:
                raise ChannelError("The Channel now belongs to another Agent")
        except Exception as error:
            await self._defer(job_id, item, error)
            return
        sending = replace(item, state="sending", error=None)
        if not await self._update(job_id, sending):
            # Nothing was sent; a later successful write must retain that fact.
            await self._update(job_id, replace(item, error="Notification storage is unavailable"))
            return
        try:
            await chats.send(
                item.channel_id,
                self._owner._secret_masker().mask(item.text),
                item.platform_target,
                thread_id=item.thread_id,
            )
        except ChannelError as error:
            if not error.possibly_delivered:
                await self._defer(job_id, item, error)
                return
            await self._update(job_id, replace(item, state="uncertain", error=_UNCERTAIN))
            self._alert(job_id)
            return
        except asyncio.CancelledError:
            # The saved sending marker is reconciled on the next start.
            raise
        except Exception:
            await self._update(job_id, replace(item, state="uncertain", error=_UNCERTAIN))
            self._alert(job_id)
            return
        await self._update(job_id, item, sent=True)
        try:
            await chats.record_outbound_note(
                item.channel_id,
                item.platform_target,
                self._owner._secret_masker().mask(item.note),
                thread_id=item.thread_id,
                skip_session_id=item.session_id,
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
        await self._update(
            job_id, replace(item, state="pending", error=message, retry_at=time.time() + delay)
        )

    def _alert(self, job_id: str) -> None:
        job = self._owner._jobs.get(job_id)
        if job is not None:
            self._owner._raise_failure_alert(
                CronFailureAlert(
                    job_id=job.id,
                    name=job.name,
                    agent_id=job.agent_id,
                    project_id=job.project_id,
                    message=_UNCERTAIN,
                )
            )
