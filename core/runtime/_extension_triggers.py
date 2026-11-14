"""An Extension's triggers: Cron jobs it owns whose fires call the Extension.

The Extension host hands each owner-bound host an :class:`ExtensionTriggers`.
Cron owns scheduling, webhook URLs and secrets, catch-up and waiting requests
(``automation/cron.md`` -> Extension triggers); this module only binds those jobs
to one Extension's registration and projects them as plain data.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from core.automation.cron import CronFire, CronJob, CronOwner, CronService, CronServiceError
from core.utils.logging import get_logger

#: What an Extension's handler receives for each fire: ``CronFire.to_dict()``.
type TriggerHandler = Callable[[dict[str, Any]], Awaitable[None]]

_LOGGER = get_logger("extensions.triggers")


class ExtensionTriggerHandlers:
    """Which registration of each Extension currently handles its trigger fires."""

    def __init__(self, cron: CronService) -> None:
        self._cron = cron
        self._owners: dict[str, Any] = {}

    def set(self, identity: Any, handler: TriggerHandler | None) -> None:
        name = identity.name
        if handler is None:
            if self._owners.get(name) is identity:
                self._owners.pop(name)
                self._cron.set_owner_handler(name, None)
            return

        async def dispatch(fire: CronFire) -> None:
            await handler(fire.to_dict())

        self._owners[name] = identity
        self._cron.set_owner_handler(name, dispatch)

    def release(self, identity: Any) -> None:
        """A retired registration stops handling fires; a newer one keeps its handler."""
        self.set(identity, None)

    async def remove_uninstalled(self, installed: frozenset[str]) -> None:
        """Delete the triggers of every Extension that is no longer installed."""
        owners = {
            job.owner.extension
            for job in self._cron.list_all_owned_jobs()
            if job.owner is not None and job.owner.extension not in installed
        }
        for name in sorted(owners):
            jobs = self._cron.list_jobs(owner=name)
            for job in jobs:
                await self._cron.delete_job(job.id, owner=name, actor="extension_removed")
            _LOGGER.warning(
                "Deleted the triggers of a removed Extension (owner=%s triggers=%d)",
                name,
                len(jobs),
            )


class ExtensionTriggers:
    """One Extension registration's triggers.

    A trigger has a ``key`` the Extension chooses, a ``name`` and a
    ``schedule`` in Cron's schedule language: a five-field cron expression,
    ``every <duration>``, ``in <duration>``, an ISO instant, or ``webhook``.
    Every call fails once the registration is no longer current.
    """

    def __init__(
        self,
        cron: CronService,
        handlers: ExtensionTriggerHandlers,
        identity: Any,
        require_current: Callable[[], None],
    ) -> None:
        self._cron = cron
        self._handlers = handlers
        self._identity = identity
        self._require_current = require_current

    @property
    def _owner(self) -> str:
        return str(self._identity.name)

    def set_handler(self, handler: TriggerHandler | None) -> None:
        """Receive each fire as a JSON object; None stops receiving them.

        While no handler is set, scheduled fires fail without counting toward
        the failure stop and webhook requests wait. A handler that raises
        counts as a failed fire; a webhook fire's requests then wait for the
        next attempt.
        """
        self._require_current()
        self._handlers.set(self._identity, handler)

    def check_schedule(self, schedule: str) -> None:
        """Raise ValueError with a readable reason when a trigger cannot use ``schedule``."""
        self._require_current()
        try:
            self._cron.parse_owned_schedule(schedule)
        except CronServiceError as error:
            raise ValueError(str(error)) from error

    def list(self) -> list[dict[str, Any]]:
        self._require_current()
        return [self._project(job) for job in self._cron.list_jobs(owner=self._owner)]

    def get(self, trigger_id: str) -> dict[str, Any]:
        self._require_current()
        return self._project(self._cron.get_job(trigger_id, owner=self._owner))

    async def create(
        self, key: str, *, name: str, schedule: str, enabled: bool = True
    ) -> dict[str, Any]:
        self._require_current()
        job = await self._cron.create_owned_job(
            CronOwner(self._owner, key),
            name=name,
            schedule=schedule,
            status="active" if enabled else "paused",
        )
        return self._project(job)

    async def update(
        self,
        trigger_id: str,
        *,
        name: str | None = None,
        schedule: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        self._require_current()
        job = await self._cron.update_owned_job(
            self._owner,
            trigger_id,
            name=name,
            schedule=schedule,
            status=None if enabled is None else ("active" if enabled else "paused"),
        )
        return self._project(job)

    async def delete(self, trigger_id: str) -> None:
        self._require_current()
        await self._cron.delete_job(trigger_id, owner=self._owner, actor=f"extension:{self._owner}")

    async def webhook_secret(self, trigger_id: str) -> str:
        """The secret a webhook trigger's requests prove, for the sender's settings."""
        self._require_current()
        return await self._cron.webhook_secret(trigger_id, owner=self._owner)

    async def rotate_webhook_secret(self, trigger_id: str) -> str:
        self._require_current()
        return await self._cron.rotate_webhook_secret(
            trigger_id, owner=self._owner, actor=f"extension:{self._owner}"
        )

    def _project(self, job: CronJob) -> dict[str, Any]:
        assert job.owner is not None
        webhook = self._cron.webhook_status(job)
        return {
            "id": job.id,
            "key": job.owner.key,
            "name": job.name,
            "schedule": self._cron.format_schedule(job),
            "schedule_type": job.schedule_type,
            "enabled": job.status == "active",
            "status": job.status,
            "next_fire_at": self._cron.next_fire_at(job),
            "last_fired_at": job.last_fired_at,
            "last_outcome": job.last_outcome,
            "last_error": job.last_error,
            "webhook": (
                None
                if webhook is None
                else {
                    "url": webhook.url,
                    "path": webhook.path,
                    "reach": webhook.reach,
                    "last_received_at": webhook.last_received_at,
                    "waiting": webhook.waiting,
                    "dropped": webhook.dropped,
                }
            ),
        }
