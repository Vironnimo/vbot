"""Webhook jobs: their secrets, the requests they accept and the batch each Run takes."""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from core.automation import _cron_timing as _timing
from core.automation import _cron_webhook_events as _events
from core.automation._cron_jobs import CronJob, CronJobValidationError, CronStorageError
from core.automation._cron_webhook_events import (
    WEBHOOK_PATH_PREFIX,
    WEBHOOK_STATE_DIR_NAME,
    WebhookBatch,
    WebhookState,
)
from core.secrets import WEBHOOK_SECRETS, SecretStoreError
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool, settle_before_cancelling

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


class CronWebhooks:
    """Secrets, accepted requests and Run batches of webhook jobs.

    A request is checked against the job's secret from the Secret Store and,
    once accepted, stored in the job's state file before the sender is
    answered; a request that cannot be stored is not accepted. The job's task
    (``_cron_scheduler``) starts a Run while requests wait; the Run takes them
    all once it holds a Run slot and forgets them once it is admitted, so
    requests that arrive while a Run is active start one follow-up Run. A Run
    that is not admitted leaves its requests waiting for the next attempt. A
    process that stops after admission but before the requests were forgotten
    on disk runs them again after the restart.
    """

    def __init__(self, owner: CronService) -> None:
        self._owner = owner
        self._store: SecretStore | None = None
        self._base_url: str | None = None
        self._reach: WebhookReach | None = None
        self._states: dict[str, WebhookState] = {}
        self._state_errors: dict[str, str] = {}
        self._secrets: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._wakes: dict[str, asyncio.Event] = {}
        self._taken: dict[str, WebhookBatch] = {}
        # Refused requests for unknown jobs cost the same check as a wrong secret.
        self._decoy_secret = _events.new_secret()
        self._conditions = LoggedConditions()

    @property
    def state_dir(self) -> Path:
        return self._owner._cron_dir / WEBHOOK_STATE_DIR_NAME

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
        state = self._states.get(job.id)
        taken = self._taken_seqs(job.id)
        return WebhookStatus(
            url=None if self._base_url is None else f"{self._base_url}{path}",
            path=path,
            reach=self._reach,
            last_received_at=None if state is None else state.last_received_at,
            waiting=0 if state is None else len(state.waiting(taken=taken)),
            dropped=0 if state is None else state.dropped,
            error=self._state_errors.get(job.id),
        )

    def load_states(self, job_ids: list[str]) -> None:
        """Read the stored requests of these jobs before their tasks start. Blocking."""
        for job_id in job_ids:
            with suppress(CronStorageError):
                self._states[job_id] = self._read_state(job_id)

    def wake(self, job_id: str) -> asyncio.Event:
        """The event a job's task waits on; set when a request is accepted."""
        return self._wakes.setdefault(job_id, asyncio.Event())

    async def waiting(self, job_id: str) -> int:
        state = await self._state(job_id)
        return len(state.waiting(taken=self._taken_seqs(job_id)))

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

    async def discard(self, job_id: str, *, keep_secret: bool = False) -> None:
        """Forget what the job kept as a webhook: its waiting requests and its secret.

        Failures are logged; a secret left behind belongs to no job and opens nothing.
        """
        async with self._lock(job_id):
            self._states.pop(job_id, None)
            self._state_errors.pop(job_id, None)
            self._taken.pop(job_id, None)
            await self._owner._writer.call_async(
                partial(_events.remove_state, self.state_dir, job_id)
            )
        self._wakes.pop(job_id, None)
        self._locks.pop(job_id, None)
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
        async with self._lock(job_id):
            latest = self._owner._jobs.get(job_id)
            if latest is None or latest.schedule_type != "webhook" or latest.status != "active":
                return "refused"
            try:
                state = await self._state(job_id)
            except CronStorageError:
                return "unavailable"
            updated = state.copy()
            updated.add(
                _events.WebhookRequest(
                    state.next_seq, request.received_at, request.headers, request.body, request.cut
                ),
                taken=self._taken_seqs(job_id),
            )
            try:
                await settle_before_cancelling(self._commit_received(updated))
            except CronStorageError as error:
                _LOGGER.error(
                    "Cron webhook request not stored; the sender was told to retry (job=%s): %s",
                    job_id,
                    error,
                )
                return "unavailable"
        _LOGGER.debug(
            "Cron webhook request accepted (job=%s bytes=%d waiting=%d dropped=%d)",
            job_id,
            len(body),
            len(updated.waiting(taken=self._taken_seqs(job_id))),
            updated.dropped,
        )
        return "accepted"

    async def _commit_received(self, state: WebhookState) -> None:
        """Persist and publish an admitted request before honoring caller cancellation."""
        await self._write(state)
        self._states[state.job_id] = state
        self.wake(state.job_id).set()
        self._owner._notify_changed()

    async def take(self, job_id: str) -> WebhookBatch | None:
        """Take the waiting requests for a Run about to start; None when none wait."""
        async with self._lock(job_id):
            state = await self._state(job_id)
            waiting = state.waiting(taken=self._taken_seqs(job_id))
            if not waiting:
                return None
            batch = WebhookBatch(tuple(waiting), state.dropped)
            self._taken[job_id] = batch
            return batch

    def release(self, job_id: str, batch: WebhookBatch) -> None:
        """Give back the requests of a Run that was not admitted; they wait for the next one."""
        if self._taken.get(job_id) is batch:
            self._taken.pop(job_id, None)

    async def consume(self, job_id: str, batch: WebhookBatch) -> None:
        """Forget the requests of an admitted Run. A failed write is logged.

        Memory moves on at once; the requests stay on disk only until the next
        successful write, or run again after a restart.
        """
        async with self._lock(job_id):
            if self._taken.get(job_id) is batch:
                self._taken.pop(job_id, None)
            state = self._states.get(job_id)
            if state is None:
                return
            updated = state.copy()
            updated.consume(batch)
            self._states[job_id] = updated
            try:
                await self._write(updated)
            except CronStorageError as error:
                _LOGGER.error(
                    "Cron webhook requests of a started Run not removed; they run again after "
                    "a restart (job=%s): %s",
                    job_id,
                    error,
                )
        self._owner._notify_changed()

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

    async def _state(self, job_id: str) -> WebhookState:
        state = self._states.get(job_id)
        if state is None:
            previous_error = self._state_errors.get(job_id)
            try:
                state = await self._owner._writer.call_async(partial(self._read_state, job_id))
            finally:
                if previous_error != self._state_errors.get(job_id):
                    self._owner._notify_changed()
            # Another caller may have loaded or changed it meanwhile.
            state = self._states.setdefault(job_id, state)
        return state

    def _read_state(self, job_id: str) -> WebhookState:
        try:
            state = _events.read_state(self.state_dir, job_id)
        except CronStorageError as error:
            self._state_errors[job_id] = str(error)
            if self._conditions.started(("state", job_id)):
                _LOGGER.warning(
                    "Cron webhook requests unavailable; retained for retry (job=%s)", job_id
                )
            raise
        self._state_errors.pop(job_id, None)
        if self._conditions.ended(("state", job_id)):
            _LOGGER.info("Cron webhook requests readable again (job=%s)", job_id)
        return state

    async def _write(self, state: WebhookState) -> None:
        await self._owner._writer.call_async(
            partial(_events.write_state, self.state_dir, state.job_id, state.to_dict())
        )

    def _taken_seqs(self, job_id: str) -> frozenset[int]:
        batch = self._taken.get(job_id)
        return frozenset() if batch is None else batch.seqs

    def _lock(self, job_id: str) -> asyncio.Lock:
        return self._locks.setdefault(job_id, asyncio.Lock())

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
