"""Webhook jobs: their secrets and the requests they accept for their Runs."""

from __future__ import annotations

import dataclasses
import hashlib
import re
import shutil
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, override

from core.automation import _cron_timing as _timing
from core.automation import _cron_webhook_events as _events
from core.automation._cron_inbox import CronInbox, InboxWriteError, state_path
from core.automation._cron_jobs import CronJob, CronJobValidationError, CronStorageError
from core.automation._cron_webhook_events import (
    WEBHOOK_INBOX,
    WEBHOOK_PATH_PREFIX,
    WebhookRequest,
    WebhookState,
)
from core.secrets import WEBHOOK_SECRETS, SecretStoreError
from core.utils.atomic import sync_directory, write_new_bytes
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.automation.cron import CronService
    from core.secrets import SecretStore

#: Who can reach the webhook URL vBot hands out: anyone on the internet (a
#: public URL), the local network, or only this computer.
WebhookReach = Literal["internet", "network", "computer"]

#: What became of a request: acknowledged, refused (unknown job, paused
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
    answered. Setup pings and recognized signed duplicates start no Run.
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
    async def discard(self, job_id: str, *, require_removal: bool = False) -> None:
        """Retire the job's webhook activation, including its secret and receipts.

        Failures are logged; a secret left behind belongs to no job and opens nothing.
        """
        await super().discard(job_id, require_removal=require_removal)
        await self.discard_secret(job_id)
        try:
            await self._owner._writer.call_async(partial(shutil.rmtree, self._receipt_dir(job_id)))
        except FileNotFoundError:
            pass
        except OSError:
            _LOGGER.warning("Cron webhook receipts not removed (job=%s)", job_id)

    async def prepare(self, job_id: str) -> None:
        """Give a newly enabled webhook schedule a fresh secret and empty inbox."""
        await super().discard(job_id, require_removal=True)
        await self.issue_secret(job_id)

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
        verified = await _WEBHOOK_WORKERS.run(
            _events.accept_request,
            secret if secret is not None else self._decoy_secret,
            headers,
            body,
            seq=0,
            received_at=received_at,
        )
        if secret is None or verified is None:
            if secret is not None:
                _LOGGER.debug("Cron webhook request refused: wrong secret (job=%s)", job_id)
            return "refused"
        request, receipt, ping = verified

        def wanted() -> bool:
            # Rotation or a schedule change may retire the proof while its
            # worker checks the request or an earlier arrival settles.
            latest = self._owner._jobs.get(job_id)
            return (
                latest is not None
                and latest.schedule_type == "webhook"
                and latest.status == "active"
                and self._secrets.get(job_id) == secret
            )

        if ping and receipt is None:
            return "accepted" if wanted() else "refused"

        def make_request(seq: int) -> WebhookRequest:
            return dataclasses.replace(request, seq=seq)

        try:
            added, state = await self.add(
                job_id, None if ping else make_request, key=receipt, wanted=wanted
            )
        except InboxWriteError as error:
            _LOGGER.error(
                "Cron webhook request not stored; the sender was told to retry (job=%s): %s",
                job_id,
                error,
            )
            return "unavailable"
        except CronStorageError:
            return "unavailable" if wanted() else "refused"
        if added == "refused" or state is None:
            return "refused"
        if ping or added == "duplicate":
            return "accepted"
        _LOGGER.debug(
            "Cron webhook request accepted (job=%s bytes=%d waiting=%d dropped=%d)",
            job_id,
            len(body),
            len(state.waiting(taken=self._taken_seqs(job_id))),
            state.dropped,
        )
        return "accepted"

    @override
    async def _already_took(self, state: WebhookState, key: str) -> bool:
        return state.latest_receipt == key or await self._owner._writer.call_async(
            partial(_has_receipt, self._receipt_dir(state.job_id) / key)
        )

    @override
    def _remember_key(self, state: WebhookState, key: str) -> None:
        state.latest_receipt = key

    @override
    async def _write(self, state: WebhookState) -> None:
        previous = self._states.get(state.job_id)
        old_receipt = None if previous is None else previous.latest_receipt
        if old_receipt is not None and old_receipt != state.latest_receipt:
            # Keep the old receipt independently before replacing its checkpoint.
            # The new receipt commits atomically with its event in the inbox itself.
            await self._owner._writer.call_async(
                partial(_save_receipt, self._receipt_dir(state.job_id) / old_receipt)
            )
        await super()._write(state)

    def _receipt_dir(self, job_id: str) -> Path:
        return state_path(self.state_dir, job_id).with_suffix(".receipts")

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


def _has_receipt(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise CronStorageError("Webhook replay receipts could not be read.") from error
    return True


def _save_receipt(path: Path) -> None:
    try:
        # A failed inbox write leaves both the checkpoint and its ledger entry.
        with suppress(FileExistsError):
            write_new_bytes(path, b"")
        # Make the newly created receipts directory itself durable before
        # replacing the inbox's copy of this receipt.
        sync_directory(path.parent.parent)
    except OSError as error:
        raise CronStorageError("Webhook replay receipt could not be saved.") from error
