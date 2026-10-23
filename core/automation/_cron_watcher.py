"""Watching jobs: the check before each Run, its accounting and the baseline commit."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from core.automation import _cron_timing as _timing
from core.automation._cron_execution import _count_consecutive_failure, end_failure_alert
from core.automation._cron_jobs import (
    MAX_CONCURRENT_CRON_RUNS,
    CronJob,
    CronStorageError,
    CronWatch,
    _truncate_error,
)
from core.automation._cron_watch import (
    WATCH_STATE_DIR_NAME,
    WatchBaseline,
    WatchChange,
    WatchCheckError,
    compare,
    read_baseline,
    remove_baseline,
    write_baseline,
)
from core.permissions import PermissionRulesError
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool, cancel_and_wait

if TYPE_CHECKING:
    from core.automation import _cron_schedule as _schedule
    from core.automation.cron import CronService

# Normalizing, hashing and diffing up to 200,000 characters of output per check.
_COMPARE_WORKERS = BoundedWorkerPool(name="cron-watch", max_workers=2)

# How long a caller waits for a first check before it goes on in the background.
FIRST_CHECK_WAIT_SECONDS = 90.0

_LOGGER = get_logger("automation.cron")


class CronWatcher:
    """Check watching jobs and start their Run when the output changed.

    At a due time the check runs first, in one of ``MAX_CONCURRENT_CRON_RUNS``
    check slots and without a Model. The first check records the baseline and
    an unchanged output starts nothing; both record only ``last_checked_at``.
    A changed output starts the Run with the change as data. A watch with
    ``always`` starts the Run after every successful due-time check, also with
    an unchanged output or the one that just became the baseline; only a change
    moves the baseline. The baseline moves
    to the new output once that Run succeeded, so a change whose Run failed or
    could not start is found again at the next check.

    A failed check counts toward the job's failure streak like a failed Run,
    with the same alerts and stop, but leaves the Run's outcome fields alone and
    records ``last_check_error``. The next successful check clears the error
    and ends the streak.

    Setting a watch on an active job checks it at once (its first check), which
    records the baseline and shows whether the check works; a failure there
    counts toward no streak. Checks of one job never overlap.
    """

    def __init__(self, owner: CronService) -> None:
        self._owner = owner
        self._check_slots = asyncio.Semaphore(MAX_CONCURRENT_CRON_RUNS)
        self._job_locks: dict[str, asyncio.Lock] = {}
        self._first_checks: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def state_dir(self) -> Path:
        return self._owner._cron_dir / WATCH_STATE_DIR_NAME

    async def fire(self, job: CronJob, owed: _schedule.OwedFire | None = None) -> None:
        """Check a watching job at a due time; start its Run when the output changed.

        A watch with ``always`` starts the Run after every successful check, and
        such a Run is told when it starts late for ``owed``: the due time, not
        the change, is why it runs.
        """
        async with self._lock(job.id):
            change = await self._check(job.id, first=False)
            latest = self._owner._jobs.get(job.id)
            watch = latest.watch if latest is not None else None
            if latest is None or watch is None or change is None:
                return
            if not change.watch.same_check(watch):
                return
            # The mode at the start of the Run decides, also when an edit switched it meanwhile.
            change = replace(change, watch=watch)
            if not change.changed and not watch.always:
                return
            outcome = await self._owner._execution._trigger_job_run(
                latest, owed=owed if watch.always else None, change=change
            )
            # Edits wait for the baseline commit as they waited for the Run.
            self._owner._executing_jobs.add(job.id)
            try:
                if outcome.succeeded and change.changed:
                    await self._commit(job.id, change)
            finally:
                self._owner._executing_jobs.discard(job.id)

    def start_first_check(self, job_id: str) -> None:
        """Check a watch just set on an active job at once, in the background."""
        task = asyncio.create_task(self._first_check(job_id), name=f"cron-watch:{job_id}:first")
        self._first_checks[job_id] = task
        self._tasks.add(task)

        def done(finished: asyncio.Task[None]) -> None:
            self._tasks.discard(finished)
            if self._first_checks.get(job_id) is finished:
                self._first_checks.pop(job_id, None)
            if not finished.cancelled() and (error := finished.exception()) is not None:
                _LOGGER.error(
                    "Cron watch first check failed (job=%s)",
                    job_id,
                    exc_info=(
                        RuntimeError,
                        RuntimeError("Watch check failed"),
                        error.__traceback__,
                    ),
                )

        task.add_done_callback(done)

    async def wait_for_first_check(self, job_id: str, timeout: float) -> bool:
        """Wait until no first check of the job is pending; False when one still runs.

        A caller that stops waiting leaves the check running.
        """
        task = self._first_checks.get(job_id)
        if task is None:
            return True
        await asyncio.wait({task}, timeout=timeout)
        return task.done()

    def forget(self, job_id: str) -> None:
        """Drop what the watcher keeps for a deleted job."""
        self._job_locks.pop(job_id, None)

    async def remove_baseline(self, job_id: str) -> None:
        await self._owner._writer.call_async(partial(remove_baseline, self.state_dir, job_id))

    async def aclose(self) -> None:
        """Stop first checks still running."""
        await cancel_and_wait(*self._tasks)

    async def _first_check(self, job_id: str) -> None:
        async with self._lock(job_id):
            await self._check(job_id, first=True)

    def _lock(self, job_id: str) -> asyncio.Lock:
        return self._job_locks.setdefault(job_id, asyncio.Lock())

    async def _check(self, job_id: str, *, first: bool) -> WatchChange | None:
        """Run one check and record it; the change when the output differs from the baseline.

        A first check runs only while the job has no baseline for its watch.
        """
        async with self._check_slots:
            job = self._owner._jobs.get(job_id)
            if (
                job is None
                or job.watch is None
                or job.status != "active"
                or job_id in self._owner._pending_restarts
            ):
                return None
            watch = job.watch
            checked_at = _timing._utc_now_iso()
            baseline = await self._owner._writer.call_async(
                partial(read_baseline, self.state_dir, job_id, watch)
            )
            if baseline is not None:
                protected = replace(
                    baseline, output=self._owner._secret_masker().mask(baseline.output)
                )
                if protected.output != baseline.output:
                    try:
                        await self._owner._writer.call_async(
                            partial(write_baseline, self.state_dir, job_id, protected)
                        )
                    except CronStorageError:
                        await self._record_failure(
                            job_id,
                            watch,
                            "The protected watch baseline could not be saved",
                            checked_at=checked_at,
                            counts=not first,
                        )
                        return None
                baseline = protected
            if first and baseline is not None:
                return None
            try:
                output = await self._run(job, watch)
            except WatchCheckError as error:
                await self._record_failure(
                    job_id, watch, str(error), checked_at=checked_at, counts=not first
                )
                return None
            comparison = await _COMPARE_WORKERS.run(
                compare, output, baseline, self._owner._secret_masker()
            )
            if not self._still_watching(job_id, watch):
                # An edit replaced or stopped what was checked.
                return None
            if baseline is None:
                recorded = WatchBaseline(
                    watch, comparison.digest, comparison.output, checked_at=checked_at
                )
                try:
                    await self._owner._writer.call_async(
                        partial(write_baseline, self.state_dir, job_id, recorded)
                    )
                except CronStorageError as error:
                    _LOGGER.error("Cron watch baseline not saved (job=%s): %s", job_id, error)
                else:
                    _LOGGER.info(
                        "Cron watch baseline recorded (job=%s chars=%d)",
                        job_id,
                        len(comparison.output),
                    )
                await self._record_success(job_id, watch, checked_at)
                if first or not watch.always:
                    return None
                return WatchChange(
                    watch,
                    since=None,
                    output=comparison.output,
                    digest=comparison.digest,
                    diff="",
                    diff_cut=False,
                    checked_at=checked_at,
                    changed=False,
                )
            await self._record_success(job_id, watch, checked_at)
            if comparison.digest == baseline.digest:
                _LOGGER.debug("Cron watch output unchanged (job=%s)", job_id)
                if not watch.always:
                    return None
                return WatchChange(
                    watch,
                    since=baseline.checked_at,
                    output=comparison.output,
                    digest=comparison.digest,
                    diff="",
                    diff_cut=False,
                    checked_at=checked_at,
                    changed=False,
                )
            _LOGGER.info("Cron watch output changed (job=%s)", job_id)
            return WatchChange(
                watch,
                since=baseline.checked_at,
                output=comparison.output,
                digest=comparison.digest,
                diff=comparison.diff,
                diff_cut=comparison.diff_cut,
                checked_at=checked_at,
            )

    async def _run(self, job: CronJob, watch: CronWatch) -> str:
        checks = self._owner._watch_checks
        if checks is None:
            raise WatchCheckError("vBot cannot run watch checks in this process")
        try:
            ceiling = job.ceiling()
        except PermissionRulesError as error:
            raise WatchCheckError(
                f"The check did not run: the job's permission rules cannot be read ({error})"
            ) from error
        try:
            if watch.kind == "command":
                return await checks.run_command(
                    watch.target,
                    agent_id=job.agent_id,
                    project_id=job.project_id,
                    ceiling=ceiling,
                )
            return await checks.fetch_url(watch.target, ceiling=ceiling)
        except WatchCheckError:
            raise
        except Exception as error:
            _LOGGER.error(
                "Cron watch check could not run (job=%s)",
                job.id,
                exc_info=(RuntimeError, RuntimeError("Watch check failed"), error.__traceback__),
            )
            raise WatchCheckError(f"The check could not run: {error}") from error

    def _still_watching(self, job_id: str, watch: CronWatch) -> bool:
        job = self._owner._jobs.get(job_id)
        return (
            job is not None
            and watch.same_check(job.watch)
            and job.status == "active"
            and job_id not in self._owner._pending_restarts
        )

    async def _record_success(self, job_id: str, watch: CronWatch, checked_at: str) -> None:
        job = self._owner._jobs.get(job_id)
        if job is None or not watch.same_check(job.watch):
            return
        job.last_checked_at = checked_at
        if job.last_check_error is not None:
            # The check works again: its failure streak ends.
            job.last_check_error = None
            job.consecutive_failures = 0
            end_failure_alert(job)
        await self._owner._execution._save_jobs_after_fire(job_id)

    async def _record_failure(
        self, job_id: str, watch: CronWatch, message: str, *, checked_at: str, counts: bool
    ) -> None:
        job = self._owner._jobs.get(job_id)
        if job is None or not watch.same_check(job.watch):
            return
        baseline = self._owner._execution._failure_baseline(job_id)
        job.last_checked_at = checked_at
        job.last_check_error = _truncate_error(self._owner._secret_masker().mask(message))
        if counts:
            _count_consecutive_failure(job)
        _LOGGER.warning(
            "Cron watch check failed (job=%s failures=%d first=%s)",
            job_id,
            job.consecutive_failures,
            not counts,
        )
        await self._owner._execution._save_jobs_after_fire(job_id)
        if counts:
            await self._owner._execution._alert_failure(job_id, baseline, check=True)

    async def _commit(self, job_id: str, change: WatchChange) -> None:
        """Make the changed output the baseline once its Run succeeded."""
        job = self._owner._jobs.get(job_id)
        if job is None or not change.watch.same_check(job.watch):
            return
        try:
            await self._owner._writer.call_async(
                partial(write_baseline, self.state_dir, job_id, change.baseline())
            )
        except CronStorageError as error:
            _LOGGER.error(
                "Cron watch baseline not saved after its Run; the next check finds the change "
                "again (job=%s): %s",
                job_id,
                error,
            )
            return
        latest = self._owner._jobs.get(job_id)
        if latest is None:
            return
        latest.last_changed_at = change.checked_at
        await self._owner._execution._save_jobs_after_fire(job_id)
