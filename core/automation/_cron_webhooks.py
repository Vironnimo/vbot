"""Webhook jobs: their secrets and the requests they accept for their Runs."""

from __future__ import annotations

import dataclasses
import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, override

from core.automation import _cron_timing as _timing
from core.automation import _cron_webhook_events as _events
from core.automation._cron_inbox import CronInbox, InboxWriteError
from core.automation._cron_jobs import CronJob, CronJobValidationError, CronStorageError
from core.automation._cron_webhook_events import (
    WEBHOOK_INBOX,
    WEBHOOK_PATH_PREFIX,
    WebhookRequest,
)
from core.secrets import WEBHOOK_SECRETS, SecretStoreError
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.automation.cron import CronService
    from core.secrets import SecretStore

#: Who can reach the webhook URL vBot hands out: anyone on the internet (a
#: public URL), the local network, or only this computer.
WebhookReach = Literal["internet", "network", "computer"]

#: What became of a request: accepted and stored, refused (unknown job, paused
#: job, no webhook, wrong secret: all alike), or authenticated but not stored.
WebhookReceipt = Literal["accepted", "refused", "unavailable"]

# Secret Store reads and writes, and checking and rendering request bodies.
_WEBHOOK_WORKERS = BoundedWorkerPool(name="cron-webhooks", max_workers=2)

# Secret Store entry names; a job id outside them is stored under its digest.
_ENTRY_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}$")

_LOGGER = get_logger("automation.cron")


@dataclass(frozen=True, slots=True)
class WebhookStatus:
    """What a webhook job shows about its URL and the requests it received."""

    url: str | None
    """The full URL; None while vBot's server address is unknown."""
    path: str
    reach: WebhookReach | None
    last_received_at: str | None
    waiting: int
    """Requests waiting for the next Run."""
    dropped: int
    """Requests dropped unseen since the last Run started."""
    error: str | None = None


class CronWebhooks(CronInbox[WebhookRequest]):
    """Secrets, accepted requests and Run batches of webhook jobs.

    A request is checked against the job's secret from the Secret Store and,
    once accepted, stored for the job's next Run before the sender is
    answered (``_cron_inbox``); a request that cannot be stored is not accepted.
    """

    def __init__(self, owner: CronService) -> None:
        super().__init__(owner, WEBHOOK_INBOX)
        self._store: SecretStore | None = None
        self._base_url: str | None = None
        self._reach: WebhookReach | None = None
        self._secrets: dict[str, str] = {}
        # Refused requests for unknown jobs cost the same check as a wrong secret.
        self._decoy_secret = _events.new_secret()

    @property
    def available(self) -> bool:
        return self._store is not None

    def set_secret_store(self, store: SecretStore | None) -> None:
        self._store = store
        self._secrets.clear()

    def bind_url(self, base_url: str | None, reach: WebhookReach | None) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._reach = reach if base_url else None

    def status(self, job: CronJob) -> WebhookStatus:
        path = f"{WEBHOOK_PATH_PREFIX}{job.id}"
        counts = self.counts(job.id)
        return WebhookStatus(
            url=None if self._base_url is None else f"{self._base_url}{path}",
            path=path,
            reach=self._reach,
            last_received_at=counts.last_received_at,
            waiting=counts.waiting,
            dropped=counts.dropped,
            error=counts.error,
        )

    # -- secrets --------------------------------------------------------------------------

    def check_available(self) -> None:
        if self._store is None:
            raise CronJobValidationError(
                "webhook is unavailable: this vBot process has no Secret Store"
            )

    async def issue_secret(self, job_id: str) -> str:
        """Store a new secret for the job, replacing any earlier one.

        Raises ``CronStorageError`` when the Secret Store refuses it.
        """
        store = self._require_store()
        secret = _events.new_secret()
        name = _entry_name(job_id)
        try:
            await _WEBHOOK_WORKERS.run(store.set_entry, WEBHOOK_SECRETS, name, secret)
        except SecretStoreError as error:
            raise CronStorageError(f"The webhook secret could not be stored: {error}") from error
        self._secrets[job_id] = secret
        return secret

    async def secret(self, job_id: str) -> str | None:
        """The job's secret; None when none is stored. Raises ``CronStorageError``."""
        cached = self._secrets.get(job_id)
        if cached is not None:
            return cached
        store = self._require_store()
        try:
            secret = await _WEBHOOK_WORKERS.run(store.entry, WEBHOOK_SECRETS, _entry_name(job_id))
        except SecretStoreError as error:
            raise CronStorageError(f"The webhook secret could not be read: {error}") from error
        if secret is not None:
            # A rotation may publish its new secret while this worker read is
            # returning. A stale read must not reactivate the replaced secret.
            secret = self._secrets.setdefault(job_id, secret)
        return secret

    @override
    async def discard(self, job_id: str, *, keep_secret: bool = False) -> None:
        """Forget what the job kept as a webhook: its waiting requests and its secret.

        Failures are logged; a secret left behind belongs to no job and opens nothing.
        """
        await super().discard(job_id)
        if not keep_secret:
            await self.discard_secret(job_id)

    async def discard_secret(self, job_id: str) -> None:
        self._secrets.pop(job_id, None)
        store = self._store
        if store is None:
            return
        try:
            await _WEBHOOK_WORKERS.run(store.remove_entry, WEBHOOK_SECRETS, _entry_name(job_id))
        except SecretStoreError as error:
            _LOGGER.warning("Cron webhook secret not removed (job=%s): %s", job_id, error)

    # -- requests -------------------------------------------------------------------------

    async def receive(
        self, job_id: str, headers: list[tuple[str, str]], body: bytes
    ) -> WebhookReceipt:
        """Check one request against the job's secret and store it for the job's next Run.

        ``headers`` are the request's header pairs with lowercase names. An
        unknown, inactive or non-webhook job and a wrong or missing secret are
        refused alike; a request that proved the secret but could not be stored
        is ``unavailable``.
        """
        job = self._owner._jobs.get(job_id)
        secret: str | None = None
        if job is not None and job.schedule_type == "webhook" and job.status == "active":
            secret = await self._secret_for_request(job_id)
        received_at = _timing._utc_now_iso()
        request = await _WEBHOOK_WORKERS.run(
            _events.accept_request,
            secret if secret is not None else self._decoy_secret,
            headers,
            body,
            seq=0,
            received_at=received_at,
        )
        if secret is None or request is None:
            if secret is not None:
                _LOGGER.debug("Cron webhook request refused: wrong secret (job=%s)", job_id)
            return "refused"
        if _is_github_ping(headers):
            # GitHub's check that a new webhook reaches its URL starts no Run.
            _LOGGER.debug("Cron webhook ping acknowledged (job=%s)", job_id)
            return "accepted"

        def wanted() -> bool:
            latest = self._owner._jobs.get(job_id)
            return (
                latest is not None
                and latest.schedule_type == "webhook"
                and latest.status == "active"
            )

        try:
            added, state = await self.add(
                job_id, lambda seq: dataclasses.replace(request, seq=seq), wanted=wanted
            )
        except InboxWriteError as error:
            _LOGGER.error(
                "Cron webhook request not stored; the sender was told to retry (job=%s): %s",
                job_id,
                error,
            )
            return "unavailable"
        except CronStorageError:
            return "unavailable"
        if added != "added" or state is None:
            return "refused"
        _LOGGER.debug(
            "Cron webhook request accepted (job=%s bytes=%d waiting=%d dropped=%d)",
            job_id,
            len(body),
            len(state.waiting(taken=self._taken_seqs(job_id))),
            state.dropped,
        )
        return "accepted"

    async def _secret_for_request(self, job_id: str) -> str | None:
        try:
            secret = await self.secret(job_id)
        except (CronStorageError, CronJobValidationError) as error:
            if self._conditions.started(("secret", job_id)):
                _LOGGER.warning(
                    "Cron webhook requests refused: the secret cannot be read (job=%s): %s",
                    job_id,
                    error,
                )
            return None
        if self._conditions.ended(("secret", job_id)):
            _LOGGER.info("Cron webhook secret readable again (job=%s)", job_id)
        if secret is None and self._conditions.started(("no-secret", job_id)):
            _LOGGER.warning(
                "Cron webhook requests refused: the job has no secret; rotate it (job=%s)", job_id
            )
        return secret

    def _require_store(self) -> SecretStore:
        self.check_available()
        assert self._store is not None
        return self._store


def _entry_name(job_id: str) -> str:
    """The job's Secret Store entry name: its id, or a digest of an unusual one."""
    if _ENTRY_NAME.fullmatch(job_id):
        return job_id
    return "sha256-" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()


def _is_github_ping(headers: list[tuple[str, str]]) -> bool:
    """Whether the request is the ping GitHub sends when a webhook is created or tested."""
    return any(name == "x-github-event" and value.strip() == "ping" for name, value in headers)
