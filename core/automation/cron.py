"""Cron catalog, durable fire claims and execution lifecycle."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from core.config_validation import (
    JsonDiagnostic,
)
from core.json_documents import JsonDocumentWriteError, write_json_document
from core.projects import (
    AgentResolutionError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
    format_agent_address,
)
from core.runs import RunCancelledError, RunKind
from core.sessions import SessionAddress, SessionNotFoundError
from core.utils.file_status import exists_strict
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.workers import OrderedWorker, settle_before_cancelling

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.projects import AgentResolver
    from core.sessions import ChatSessionManager
from core.automation import _cron_claims as _claims
from core.automation import _cron_schedule as _schedule
from core.automation import _cron_timing as _timing
from core.automation._cron_jobs import (
    _MUTABLE_FIELDS,
    _RESTART_FIELDS,
    CRON_EXPRESSION_FIELD_COUNT,
    CRON_JOBS_FORMAT,
    MAX_ACTIVE_CRON_JOBS,
    MAX_CONCURRENT_CRON_RUNS,
    MAX_CONSECUTIVE_CRON_FAILURES,
    MAX_PROJECTED_OCCURRENCES_PER_JOB,
    MAX_STORED_CRON_JOBS,
    MIN_INTERVAL_SECONDS,
    TERMINAL_CRON_JOB_STATUSES,
    CronJob,
    CronJobInPastError,
    CronJobNotFoundError,
    CronJobStatus,
    CronJobValidationError,
    CronOccurrence,
    CronRunOutcome,
    CronServiceError,
    CronStorageError,
    CronTargetAgentNotFoundError,
    CronTargetError,
    CronTargetProjectNotFoundError,
    CronTargetUnavailableError,
    ParsedSchedule,
    ScheduleType,
    _as_utc,
    _derive_cron_job_name,
    _format_duration,
    _load_cron_jobs_payload,
    _parse_iso_datetime,
    _resolve_timezone,
    _target_validation_error,
    _truncate_error,
    _validate_cron_job_data,
    load_validated_cron_jobs_json,
    normalize_job_fields,
    validate_cron_jobs_data,
    validate_cron_jobs_file,
)

__all__ = [
    "CRON_EXPRESSION_FIELD_COUNT",
    "CronJob",
    "CronJobInPastError",
    "CronJobNotFoundError",
    "CronJobStatus",
    "CronJobValidationError",
    "CronOccurrence",
    "CronRunOutcome",
    "CronServiceError",
    "CronStorageError",
    "CronTargetAgentNotFoundError",
    "CronTargetError",
    "CronTargetProjectNotFoundError",
    "CronTargetUnavailableError",
    "MAX_ACTIVE_CRON_JOBS",
    "MAX_CONCURRENT_CRON_RUNS",
    "MAX_CONSECUTIVE_CRON_FAILURES",
    "MAX_PROJECTED_OCCURRENCES_PER_JOB",
    "MAX_STORED_CRON_JOBS",
    "MIN_INTERVAL_SECONDS",
    "ParsedSchedule",
    "ScheduleType",
    "TERMINAL_CRON_JOB_STATUSES",
    "load_validated_cron_jobs_json",
    "validate_cron_jobs_data",
    "validate_cron_jobs_file",
    "CronService",
    "format_max_delay",
]


_ONCE_MAX_FIRE_ATTEMPTS = 5

_POST_FIRE_SAVE_MAX_ATTEMPTS = 3

_POST_FIRE_SAVE_RETRY_SECONDS = 5.0

_ONCE_FIRE_CLAIMS_DIR_NAME = "once-fire-claims"

# A fire that starts at most this late counts as on time: no late notice, and
# ``max_delay_seconds`` does not apply. Wall-clock waits recheck every minute.
_ON_TIME_SECONDS = 60

_SCHEDULE_FIELDS = (
    "schedule_type",
    "cron_expression",
    "interval_seconds",
    "interval_anchor_at",
    "run_at",
)


_LOGGER = get_logger("automation.cron")
# Who caused a mutation when the caller does not say (direct in-process callers).
_DEFAULT_ACTOR = "internal"

# Every jobs.json and fire-claim write runs here in submission order. Job fires
# and edits await their writes off the Event Loop; the blocking saves of a
# service that is not started yet still land after everything submitted earlier.
_CRON_WRITER = OrderedWorker(name="cron")


class CronService:
    """Manage cron jobs, persistence, and per-job scheduling tasks.

    Job tasks and job edits run on the Event Loop and await their writes on the
    ``cron`` ordered writer. An edit checks its target Agent and Session on the
    Session database's pool, then applies to memory and the job tasks at once and
    saves; a failed save undoes both. Edits run one at a time and finish even
    when their caller is cancelled, so that undo is exact.
    """

    def __init__(
        self,
        trigger_service: TriggerService,
        data_root: str | Path,
        *,
        agent_resolver: AgentResolver | None = None,
        sessions: ChatSessionManager | None = None,
        tz: str | ZoneInfo | None = None,
    ) -> None:
        self._trigger_service = trigger_service
        self._agent_resolver = agent_resolver
        self._sessions = sessions
        self._data_root = Path(data_root).expanduser()
        self._cron_dir = self._data_root / "cron"
        self._jobs_path = self._cron_dir / "jobs.json"
        self._once_fire_claims_dir = self._cron_dir / _ONCE_FIRE_CLAIMS_DIR_NAME
        self._jobs: dict[str, CronJob] = {}
        self._invalid_job_entries: list[Any] = []
        self._storage_load_error: CronStorageError | None = None
        self._jobs_loaded = False
        self._job_tasks: dict[str, asyncio.Task[None]] = {}
        self._executing_jobs: set[str] = set()
        self._pending_restarts: set[str] = set()
        self._run_slots = asyncio.Semaphore(MAX_CONCURRENT_CRON_RUNS)
        self._changed_callbacks: set[Callable[[], None]] = set()
        self._timezone = _resolve_timezone(tz)
        self._timezone_changed = asyncio.Event()
        self._started = False
        self._edits = asyncio.Lock()
        self._crash_saves: set[asyncio.Task[bool]] = set()

    def add_changed_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to persisted Cron changes and return an unsubscribe function."""
        self._changed_callbacks.add(callback)

        def unsubscribe() -> None:
            self._changed_callbacks.discard(callback)

        return unsubscribe

    async def create_job(
        self,
        *,
        agent_id: str,
        name: str | None = None,
        prompt: str,
        schedule_type: ScheduleType,
        cron_expression: str | None = None,
        interval_seconds: int | None = None,
        interval_anchor_at: str | None = None,
        run_at: str | None = None,
        remaining_runs: int | None = None,
        session_id: str | None = None,
        status: CronJobStatus = "active",
        project_id: str | None = None,
        max_delay_seconds: int | None = None,
        actor: str = _DEFAULT_ACTOR,
    ) -> CronJob:
        """Create and persist a new cron job.

        ``project_id=None`` is a global/identity target (unchanged); a set value
        scopes the fired Session/Run to that project's anchor.
        """
        async with self._edits:
            self._ensure_jobs_loaded()
            if len(self._jobs) >= MAX_STORED_CRON_JOBS:
                raise CronJobValidationError(
                    f"Cron stores at most {MAX_STORED_CRON_JOBS} jobs; delete history first"
                )
            if schedule_type == "interval" and interval_anchor_at is None:
                interval_anchor_at = _timing._utc_now_iso()
            job = CronJob(
                id=new_id("cron", claim=lambda candidate: candidate not in self._jobs),
                agent_id=agent_id,
                name=name if name is not None else _derive_cron_job_name(prompt),
                prompt=prompt,
                schedule_type=schedule_type,
                cron_expression=cron_expression,
                interval_seconds=interval_seconds,
                interval_anchor_at=interval_anchor_at,
                run_at=run_at,
                remaining_runs=remaining_runs,
                session_id=session_id,
                status=status,
                last_fired_at=None,
                created_at=_timing._utc_now_iso(),
                project_id=project_id,
                max_delay_seconds=max_delay_seconds,
            )
            self._validate_job(job, validate_references=False)
            await self._validate_references_async(job)
            self._reject_past_once_run(job)
            self._validate_capacity(job)
            self._jobs[job.id] = job

            async def save() -> None:
                await self._save_edit(lambda: self._jobs.pop(job.id, None))
                if self._started and job.status == "active":
                    self._start_job_task(job)

            await settle_before_cancelling(save())

        _LOGGER.info(
            "Cron job created (job=%s agent=%s%s schedule_type=%s status=%s actor=%s)",
            job.id,
            job.agent_id,
            f" project={job.project_id}" if job.project_id else "",
            job.schedule_type,
            job.status,
            actor,
        )
        return self._clone_job(job)

    def list_jobs(self) -> list[CronJob]:
        """List all persisted cron jobs in stable created-order."""
        self._ensure_jobs_loaded(allow_degraded=True)
        ordered = sorted(self._jobs.values(), key=lambda value: (value.created_at, value.id))
        return [self._clone_job(job) for job in ordered]

    def get_job(self, job_id: str) -> CronJob:
        """Get one cron job by id."""
        self._ensure_jobs_loaded()
        if job_id not in self._jobs:
            raise CronJobNotFoundError(f"Cron job not found: {job_id}")
        return self._clone_job(self._jobs[job_id])

    def system_timezone_name(self) -> str:
        """Return the configured canonical IANA timezone name."""
        return str(self._timezone)

    def set_timezone(self, timezone_name: str) -> None:
        """Apply a new application timezone and wake wall-clock schedules."""
        timezone = _resolve_timezone(timezone_name)
        if timezone == self._timezone:
            return
        previous_event = self._timezone_changed
        self._timezone = timezone
        self._timezone_changed = asyncio.Event()
        previous_event.set()
        self._notify_changed()

    def parse_schedule(
        self, schedule: str, *, reference_time: datetime | None = None
    ) -> ParsedSchedule:
        """Parse a schedule in the current application timezone."""
        return _schedule.parse_schedule(
            self._timezone, reference_time or _timing._utc_now(), schedule
        )

    @staticmethod
    def format_schedule(job: CronJob) -> str:
        """Return the canonical schedule string for one job."""
        return _schedule.format_schedule(job)

    def next_fire_at(self, job: CronJob, *, reference_time: datetime | None = None) -> str | None:
        """Project the next fire in the current application timezone."""
        return _schedule.next_fire_at(self._timezone, reference_time or _timing._utc_now(), job)

    def project_occurrences(
        self,
        window_start_utc: datetime,
        window_end_utc: datetime,
        *,
        max_per_job: int = MAX_PROJECTED_OCCURRENCES_PER_JOB,
    ) -> list[CronOccurrence]:
        """Project scheduled fire instants of active jobs into a UTC window.

        Read-only display projection for the calendar: nothing is persisted and
        no Run is started. Paused and terminal jobs never project. A job's
        future ticks respect its remaining-runs budget; ticks before the current
        time inside the window are shown as schedule history.
        """
        self._ensure_jobs_loaded(allow_degraded=True)
        window_start = _as_utc(window_start_utc)
        window_end = _as_utc(window_end_utc)
        if window_end <= window_start:
            raise CronJobValidationError("projection window end must be after its start")
        now_utc = _timing._utc_now()
        occurrences: list[CronOccurrence] = []
        for job in sorted(self._jobs.values(), key=lambda item: (item.created_at, item.id)):
            if job.status != "active" or job.remaining_runs == 0:
                continue
            occurrences.extend(
                _schedule._project_job_occurrences(
                    self._timezone, job, window_start, window_end, now_utc, max_per_job
                )
            )
        occurrences.sort(key=lambda item: (item.fire_at_utc, item.job_id))
        return occurrences

    async def update_job(
        self, job_id: str, *, actor: str = _DEFAULT_ACTOR, **fields: Any
    ) -> CronJob:
        """Update mutable cron job fields and persist changes."""
        _job, updated, changed_fields = await self._edit(job_id, fields)
        if changed_fields == ["status"] and updated.status in {"active", "paused"}:
            _LOGGER.info(
                "Cron job %s (job=%s actor=%s)",
                "enabled" if updated.status == "active" else "disabled",
                job_id,
                actor,
            )
        elif changed_fields:
            _LOGGER.info(
                "Cron job updated (job=%s fields=%s actor=%s)",
                job_id,
                ",".join(changed_fields),
                actor,
            )
        return updated

    def retarget_agent(self, job_id: str, agent_id: str) -> CronJob:
        """Point a job at another Agent id as one step of an Identity Agent rename.

        For a service that is not started, such as during a rename that completes
        at startup: blocking. The jobs of a started service belong to the Event
        Loop, so :meth:`retarget_agent_async` changes them there. The same change
        as ``update_job(job_id, agent_id=...)``; the rename logs one summary line,
        so this step logs at DEBUG only.
        """
        if self._started:
            raise RuntimeError("Retarget the jobs of a started Cron service on its Event Loop")
        job, candidate, changed_fields, _restart = self._stage_update(
            job_id, {"agent_id": agent_id}
        )
        if changed_fields:
            self._jobs[job_id] = candidate
            try:
                self._save_jobs()
            except Exception:
                self._jobs[job_id] = job
                raise
            self._notify_changed()
            _log_retarget(job_id, job.agent_id, agent_id)
        return self._clone_job(candidate)

    async def retarget_agent_async(self, job_id: str, agent_id: str) -> CronJob:
        """:meth:`retarget_agent` for a started service, on the Event Loop that owns its jobs.

        An edit like :meth:`update_job`: it applies at once and awaits its save off
        the loop.
        """
        job, updated, changed_fields = await self._edit(job_id, {"agent_id": agent_id})
        if changed_fields:
            _log_retarget(job_id, job.agent_id, agent_id)
        return updated

    async def _edit(
        self, job_id: str, fields: dict[str, Any]
    ) -> tuple[CronJob, CronJob, list[str]]:
        """Apply ``fields`` to memory and the job tasks at once, then save; undo both on failure.

        Returns the job before and after the edit and the names that changed.
        """
        async with self._edits:
            job, candidate, changed_fields, restart_task = self._stage_update(
                job_id, fields, validate_references=False
            )
            if changed_fields:
                await self._validate_references_async(candidate)
                # A fire may have changed the job while its references were checked.
                job, candidate, changed_fields, restart_task = self._stage_update(
                    job_id, fields, validate_references=False
                )
            if changed_fields:
                edited = self._clone_job(candidate)
                self._jobs[job_id] = candidate
                # Job tasks follow memory at once, so a fire in flight sees the edit.
                if self._started and restart_task:
                    self._restart_job_task(candidate)

                def undo() -> None:
                    if self._jobs.get(job_id) is candidate:
                        # A fire during the save records on the live job; keep that.
                        _take_back_edit(candidate, before=job, edited=edited)
                        if self._started and restart_task:
                            self._restart_job_task(candidate)

                await settle_before_cancelling(self._save_edit(undo))
        return self._clone_job(job), self._clone_job(candidate), changed_fields

    def _stage_update(
        self, job_id: str, fields: dict[str, Any], *, validate_references: bool = True
    ) -> tuple[CronJob, CronJob, list[str], bool]:
        """Validate ``fields`` against a job without applying them.

        Returns the current job, the updated candidate, the names that changed
        and whether the job's task must restart. Without changes the candidate
        is the current job itself. ``validate_references=False`` leaves the
        blocking target and Session reads to the caller.
        """
        self._ensure_jobs_loaded()
        job = self._jobs.get(job_id)
        if job is None:
            raise CronJobNotFoundError(f"Cron job not found: {job_id}")

        unknown_fields = sorted(set(fields) - _MUTABLE_FIELDS)
        if unknown_fields:
            joined = ", ".join(unknown_fields)
            raise CronJobValidationError(f"Unsupported cron job fields: {joined}")

        if not fields:
            return job, job, [], False

        candidate = self._clone_job(job)
        restart_task = any(field in _RESTART_FIELDS for field in fields)

        for field_name, field_value in fields.items():
            setattr(candidate, field_name, field_value)
        if candidate.schedule_type == "once":
            if "remaining_runs" in fields and candidate.remaining_runs is None:
                raise CronJobValidationError(
                    "repeat cannot be null for a one-time schedule; use repeat: 1"
                )
            if (
                "schedule_type" in fields
                and "remaining_runs" not in fields
                and job.remaining_runs != 1
            ):
                raise CronJobValidationError("Changing to a one-time schedule requires repeat: 1")
        if (
            candidate.schedule_type == "interval"
            and "interval_anchor_at" not in fields
            and ("schedule_type" in fields or "interval_seconds" in fields)
        ):
            candidate.interval_anchor_at = _timing._utc_now_iso()
        if job.status in TERMINAL_CRON_JOB_STATUSES and candidate.status != job.status:
            raise CronJobValidationError(
                "Completed or missed jobs are immutable history and cannot change status"
            )
        if fields.get("status") == "active" and job.status == "failed":
            candidate.consecutive_failures = 0
            if candidate.remaining_runs == 0:
                candidate.remaining_runs = 1
        if candidate.status == "active" and (
            job.status != "active"
            or any(getattr(job, name) != getattr(candidate, name) for name in _SCHEDULE_FIELDS)
        ):
            # Fires due while the job was inactive or on its earlier schedule are not owed.
            candidate.covered_until = _timing._utc_now_iso()

        changed_fields = sorted(
            field_name
            for field_name in fields
            if getattr(job, field_name) != getattr(candidate, field_name)
        )
        if not changed_fields:
            return job, job, [], False

        self._validate_job(candidate, validate_references=validate_references)
        if {"run_at", "schedule_type"} & set(changed_fields) or (
            candidate.status == "active" and job.status != "active"
        ):
            # Arming a one-time job for an elapsed instant would fire it at once.
            self._reject_past_once_run(candidate)
        self._validate_capacity(candidate, replacing_id=job_id)
        return job, candidate, changed_fields, restart_task

    async def delete_job(self, job_id: str, *, actor: str = _DEFAULT_ACTOR) -> None:
        """Delete one cron job and cancel any active task."""
        async with self._edits:
            self._ensure_jobs_loaded()
            if job_id not in self._jobs:
                raise CronJobNotFoundError(f"Cron job not found: {job_id}")

            removed = self._jobs.pop(job_id)
            # A fire in flight withdraws at once; the claim removal follows the save.
            self._cancel_job_task(job_id)

            def undo() -> None:
                self._jobs[job_id] = removed
                if self._started and removed.status == "active":
                    self._restart_job_task(removed)

            async def save() -> None:
                await self._save_edit(undo)
                await _CRON_WRITER.call_async(
                    partial(_claims.remove, self._once_fire_claims_dir, job_id)
                )

            await settle_before_cancelling(save())
        _LOGGER.info("Cron job deleted (job=%s actor=%s)", job_id, actor)

    async def enable_job(self, job_id: str, *, actor: str = _DEFAULT_ACTOR) -> CronJob:
        """Set a cron job status to active."""
        self._ensure_jobs_loaded()
        existing = self._jobs.get(job_id)
        if existing is None:
            raise CronJobNotFoundError(f"Cron job not found: {job_id}")
        if existing.status in {"completed", "missed"}:
            raise CronJobValidationError("Completed or missed jobs cannot be re-enabled")
        return await self.update_job(job_id, status="active", actor=actor)

    async def disable_job(self, job_id: str, *, actor: str = _DEFAULT_ACTOR) -> CronJob:
        """Set a cron job status to paused."""
        self._ensure_jobs_loaded()
        existing = self._jobs.get(job_id)
        if existing is None:
            raise CronJobNotFoundError(f"Cron job not found: {job_id}")
        if existing.status in {"completed", "missed"}:
            raise CronJobValidationError("Completed or missed jobs cannot be paused")
        return await self.update_job(job_id, status="paused", actor=actor)

    def start(self) -> None:
        """Load jobs and start per-job scheduling tasks. Idempotent."""
        if self._started:
            return

        try:
            self._jobs = self._load_jobs()
            self._storage_load_error = None
        except CronStorageError as error:
            self._degrade_invalid_storage(error)
            self._started = True
            return
        self._jobs_loaded = True
        self._started = True
        needs_save = False
        once_claims_to_remove: list[str] = []
        held_job_ids: set[str] = set()

        for job in self._jobs.values():
            if job.status != "active":
                continue
            if job.schedule_type == "once":
                try:
                    claimed_at = _claims.read(self._once_fire_claims_dir, job.id)
                except CronStorageError as error:
                    # The job may already have fired. Never fire it from this
                    # process; the next start re-evaluates a readable claim.
                    _LOGGER.warning(
                        "Holding once job with unreadable fire claim (job=%s): %s",
                        job.id,
                        error,
                    )
                    held_job_ids.add(job.id)
                    continue
                if claimed_at is not None:
                    _LOGGER.warning(
                        "Marking claimed once job as completed (id=%s claimed_at=%s)",
                        job.id,
                        claimed_at,
                    )
                    job.status = "completed"
                    job.last_fired_at = claimed_at
                    job.last_outcome = "unknown"
                    job.last_error = "vBot restarted after this once job was claimed"
                    needs_save = True
                    once_claims_to_remove.append(job.id)
                    continue
            if job.remaining_runs == 0:
                job.status = "completed"
                if job.last_outcome is None:
                    job.last_outcome = "unknown"
                    job.last_error = "vBot restarted after the final Run was admitted"
                needs_save = True
                continue

        if needs_save:
            try:
                self._save_jobs()
            except CronStorageError as error:
                # jobs.json was readable; a failed write is not invalid storage.
                # Reconciled state stays in memory (none of it is scheduled), and
                # claims remain so the next start reconciles them again.
                _LOGGER.error("Cron startup reconciliation could not be saved: %s", error)
            else:
                for job_id in once_claims_to_remove:
                    _claims.remove(self._once_fire_claims_dir, job_id)
            self._notify_changed()

        for job in self._jobs.values():
            if job.status == "active" and job.id not in held_job_ids:
                self._start_job_task(job)

    def stop(self) -> None:
        """Cancel all running cron tasks. Idempotent."""
        if not self._started and not self._job_tasks:
            return

        for job_id in list(self._job_tasks):
            self._cancel_job_task(job_id, force=True)
        self._pending_restarts.clear()
        self._started = False

    async def aclose(self) -> None:
        """Stop cron scheduling and await canceled job tasks and pending saves."""
        tasks = list(self._job_tasks.values())
        self.stop()

        pending_tasks = [task for task in tasks if not task.done()]
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        if self._crash_saves:
            await asyncio.gather(*self._crash_saves, return_exceptions=True)

    def _load_jobs(self) -> dict[str, CronJob]:
        """Load valid jobs while preserving invalid sibling entries verbatim."""
        self._ensure_storage_exists()
        raw_payload = _load_cron_jobs_payload(self._jobs_path)
        self._invalid_job_entries = []
        jobs: dict[str, CronJob] = {}
        for index, item in enumerate(raw_payload):
            diagnostics: list[JsonDiagnostic] = []
            _validate_cron_job_data(diagnostics, f"$.jobs[{index}]", item)
            errors = [diagnostic for diagnostic in diagnostics if diagnostic.severity == "error"]
            if errors:
                details = "; ".join(
                    f"{diagnostic.path}: {diagnostic.message}" for diagnostic in errors
                )
                _LOGGER.warning("Skipping invalid Cron job: %s", details)
                self._invalid_job_entries.append(item)
                continue
            try:
                job = CronJob.from_dict(cast("dict[str, Any]", item))
                self._validate_job(job, validate_references=False)
            except (CronJobValidationError, TypeError, ValueError) as error:
                _LOGGER.warning("Skipping invalid Cron job at $.jobs[%d]: %s", index, error)
                self._invalid_job_entries.append(item)
                continue
            if job.id in jobs:
                _LOGGER.warning("Skipping duplicate Cron job id at $.jobs[%d]: %s", index, job.id)
                self._invalid_job_entries.append(item)
                continue
            jobs[job.id] = job
        return jobs

    def _save_jobs(self) -> None:
        """Persist the jobs, blocking until this and every earlier write landed.

        Only for startup and worker threads: on a running Event Loop the wait
        would block the loop for as long as a data snapshot holds the file.
        """
        _CRON_WRITER.call(partial(self._write_jobs, self._jobs_snapshot()))

    async def _save_jobs_async(self) -> None:
        """Event-Loop-safe :meth:`_save_jobs`; the snapshot is taken before the first await."""
        await _CRON_WRITER.call_async(partial(self._write_jobs, self._jobs_snapshot()))

    async def _save_edit(self, undo: Callable[[], object]) -> None:
        """Persist an edit already applied in memory; call ``undo`` when the save fails."""
        try:
            await self._save_jobs_async()
        except Exception:
            undo()
            raise
        self._notify_changed()

    def _jobs_snapshot(self) -> list[Any]:
        return [
            job.to_dict() for job in sorted(self._jobs.values(), key=lambda item: item.created_at)
        ] + list(self._invalid_job_entries)

    def _write_jobs(self, jobs: list[Any]) -> None:
        """Write one snapshot to <data_root>/cron/jobs.json using atomic replace.

        Invalid entries are written back verbatim and unknown fields of the file
        on disk are kept; a file that no longer loads is never overwritten.
        """
        self._ensure_storage_exists()
        try:
            write_json_document(self._jobs_path, {"jobs": jobs}, CRON_JOBS_FORMAT)
        except JsonDocumentWriteError as error:
            raise CronStorageError(str(error)) from error
        except OSError as error:
            raise CronStorageError(f"Cannot write {self._jobs_path}: {error}") from error

    def _notify_changed(self) -> None:
        for callback in tuple(self._changed_callbacks):
            try:
                callback()
            except Exception as error:
                _LOGGER.error(
                    "Cron change callback failed: %s",
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

    def _start_job_task(self, job: CronJob) -> None:
        """Create and track one asyncio task for an active cron job."""
        if job.status != "active" or job.remaining_runs == 0:
            return

        self._cancel_job_task(job.id)

        task: asyncio.Task[None]
        if job.schedule_type == "once":
            task = asyncio.create_task(self._run_once_job(job), name=f"cron-job:{job.id}:once")
        else:
            task = asyncio.create_task(
                self._run_recurring_job(job, job.schedule_type),
                name=f"cron-job:{job.id}:{job.schedule_type}",
            )

        self._job_tasks[job.id] = task

        def on_done(completed_task: asyncio.Task[None], job_id: str = job.id) -> None:
            self._on_job_task_done(job_id, completed_task)

        task.add_done_callback(on_done)

    def _cancel_job_task(self, job_id: str, *, force: bool = False) -> None:
        """Cancel and forget one tracked asyncio task if present."""
        if not force and job_id in self._executing_jobs:
            self._pending_restarts.add(job_id)
            return
        task = self._job_tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _run_recurring_job(self, job: CronJob, schedule_type: ScheduleType) -> None:
        """Fire a cron or interval job at each due instant, catching up missed fires.

        A fire that came due while vBot did not run the job (offline, asleep)
        starts once, late, for the most recent due instant, unless the job's
        ``max_delay_seconds`` forbids it. Due instants that pass while the
        job's own Run is still running are skipped.
        """
        while True:
            current = self._jobs.get(job.id)
            if (
                current is None
                or current.status != "active"
                or current.schedule_type != schedule_type
            ):
                return
            if schedule_type == "cron" and current.cron_expression is None:
                raise CronJobValidationError(
                    f"Cron job {current.id} is missing cron_expression while active"
                )

            owed = _schedule.owed_fire(self._timezone, _timing._utc_now(), current)
            if owed is None:
                next_fire_at = self.next_fire_at(current)
                if next_fire_at is None:
                    return
                due_at = _parse_iso_datetime(
                    next_fire_at, field_name="next_fire_at", allow_naive=False
                )
                # Only wall-clock cron schedules move when the timezone changes.
                reached = await _timing._sleep_until_utc(
                    due_at,
                    wake_event=self._timezone_changed if schedule_type == "cron" else None,
                )
                if not reached:
                    continue
                current = self._jobs.get(job.id)
                if (
                    current is None
                    or current.status != "active"
                    or current.schedule_type != schedule_type
                ):
                    return
                # After a long sleep of the computer, later due instants may have passed too.
                owed = _schedule.owed_fire(
                    self._timezone, _timing._utc_now(), current
                ) or _schedule.OwedFire(due_at=due_at)

            late = self._late_fire(owed)
            if late is not None and _exceeds_max_delay(current, late):
                await self._skip_missed_fire(current.id, late)
                continue
            await self._trigger_job_run(current, late=late)
            if job.id in self._pending_restarts:
                return

    async def _run_once_job(self, job: CronJob) -> None:
        """Sleep until run_at, fire once, then mark completed.

        A failed fire (claim write or trigger error) is retried with bounded
        exponential backoff. Once the attempt limit is reached the job is
        abandoned (marked failed) and logged, so a permanently failing once
        job stops retrying instead of looping forever (e.g. its agent was
        deleted, leaving every trigger attempt to fail).
        """
        failed_fire_attempts = 0
        late: _LateFire | None = None
        while True:
            current = self._jobs.get(job.id)
            if current is None or current.status != "active" or current.schedule_type != "once":
                return

            run_at_utc = _schedule._parse_run_at_utc(self._timezone, current)
            await _timing._sleep_until_utc(run_at_utc)

            latest = self._jobs.get(job.id)
            if latest is None or latest.status != "active" or latest.schedule_type != "once":
                return
            if failed_fire_attempts == 0:
                # Retries of a failed fire keep the first attempt's decision.
                late = self._late_fire(_schedule.OwedFire(due_at=run_at_utc))
                if late is not None and _exceeds_max_delay(latest, late):
                    await self._skip_missed_fire(latest.id, late)
                    return

            claimed_at = _timing._utc_now_iso()
            try:
                await self._claim_once_fire(latest, claimed_at)
            except CronStorageError as error:
                _LOGGER.error(
                    "Cron once job fire claim failed for job=%s: %s",
                    latest.id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
                if job.id in self._pending_restarts:
                    return
                failed_fire_attempts += 1
                if await self._back_off_or_abandon_once_job(job.id, failed_fire_attempts):
                    return
                continue

            if job.id in self._pending_restarts:
                await self._remove_claim(job.id)
                return
            succeeded = await self._trigger_job_run(latest, late=late)
            if job.id in self._pending_restarts:
                await self._remove_claim(job.id)
                return
            if not succeeded:
                await self._remove_claim(latest.id)
                current_after_failure = self._jobs.get(latest.id)
                if (
                    current_after_failure is None
                    or current_after_failure.remaining_runs == 0
                    or current_after_failure.status != "active"
                ):
                    return
                failed_fire_attempts += 1
                if await self._back_off_or_abandon_once_job(job.id, failed_fire_attempts):
                    return
                continue

            latest = self._jobs.get(job.id)
            if latest is None:
                return

            if latest.status == "active":
                latest.status = "completed"
                self._jobs[latest.id] = latest
                await self._persist_after_fire(latest.id)
            await self._remove_claim(latest.id)
            return

    async def _claim_once_fire(self, job: CronJob, claimed_at: str) -> None:
        # A reschedule must wait for this claim to settle before replacing its
        # task; otherwise the old task could leave or remove the new fire's claim.
        self._executing_jobs.add(job.id)
        try:
            await _CRON_WRITER.call_async(
                partial(_claims.write, self._once_fire_claims_dir, job, claimed_at)
            )
        except asyncio.CancelledError:
            # The ordered writer has settled, and admission has not started.
            await self._remove_claim(job.id)
            raise
        finally:
            self._executing_jobs.discard(job.id)

    async def _remove_claim(self, job_id: str) -> None:
        await _CRON_WRITER.call_async(partial(_claims.remove, self._once_fire_claims_dir, job_id))

    async def _back_off_or_abandon_once_job(self, job_id: str, attempts: int) -> bool:
        """Wait out the backoff for a failed once fire, or abandon after the cap.

        Returns True when the job has been abandoned (marked failed) and the
        caller must stop; False after sleeping the backoff delay so the caller
        can retry the fire.
        """
        if attempts >= _ONCE_MAX_FIRE_ATTEMPTS:
            await self._abandon_once_job(job_id, attempts)
            return True

        await _timing._sleep(_timing._once_retry_delay(attempts))
        return False

    async def _abandon_once_job(self, job_id: str, attempts: int) -> None:
        """Mark a permanently failing once job failed so it stops retrying.

        The terminal ``failed`` status keeps the never-fired job visible and
        distinct from a successful ``completed`` fire; ``last_fired_at`` stays
        unset because the job never actually ran.
        """
        job = self._jobs.get(job_id)
        if job is None or job.schedule_type != "once":
            return

        _LOGGER.error(
            "Abandoning once job after %d failed fire attempts (id=%s)",
            attempts,
            job_id,
        )
        job.status = "failed"
        self._jobs[job_id] = job
        await self._save_jobs_after_fire(job_id)
        await self._remove_claim(job_id)

    def _late_fire(self, owed: _schedule.OwedFire) -> _LateFire | None:
        """Describe a fire that starts after it was due; ``None`` when it is on time."""
        now = _timing._utc_now()
        if (now - owed.due_at).total_seconds() <= _ON_TIME_SECONDS and not owed.earlier_missed:
            return None
        return _LateFire(owed=owed, noticed_at=now, timezone=self._timezone)

    async def _skip_missed_fire(self, job_id: str, late: _LateFire) -> None:
        """Record missed fires that the job's ``max_delay_seconds`` does not let start."""
        job = self._jobs.get(job_id)
        if job is None:
            return
        max_delay_seconds = job.max_delay_seconds or 0
        job.covered_until = late.noticed_at.isoformat()
        job.last_outcome = "missed"
        job.last_error = _truncate_error(late.skip_reason(max_delay_seconds))
        if job.schedule_type == "once":
            job.status = "missed"
        self._jobs[job_id] = job
        _LOGGER.info(
            "Cron job skipped a missed fire (job=%s due_at=%s late_by=%ds earlier_missed=%d "
            "max_delay=%ds)",
            job_id,
            late.owed.due_at.isoformat(),
            late.late_by_seconds,
            late.owed.earlier_missed,
            max_delay_seconds,
        )
        await self._persist_after_fire(job_id)

    async def _trigger_job_run(self, job: CronJob, *, late: _LateFire | None = None) -> bool:
        self._executing_jobs.add(job.id)
        try:
            async with self._run_slots:
                latest = self._jobs.get(job.id)
                if latest is None or latest.status != "active" or job.id in self._pending_restarts:
                    return False

                latest.last_attempt_at = _timing._utc_now_iso()
                latest.last_error = None
                self._jobs[latest.id] = latest
                await self._save_jobs_after_fire(latest.id)
                # Before admission begins, a scheduling edit withdraws this fire;
                # the replacement task must wait for the new schedule.
                latest = self._jobs.get(job.id)
                if latest is None or latest.status != "active" or job.id in self._pending_restarts:
                    return False
                _LOGGER.info(
                    "Cron job fired (job=%s agent=%s session=%s%s%s)",
                    latest.id,
                    latest.agent_id,
                    latest.session_id,
                    f" project={latest.project_id}" if latest.project_id else "",
                    (
                        f" late_by={late.late_by_seconds}s"
                        f" earlier_missed={late.owed.earlier_missed}"
                        if late is not None
                        else ""
                    ),
                )
                # Omitted for an on-time fire, so its call shape stays unchanged.
                note: dict[str, Any] = (
                    {"context_note": late.notice(latest.id, _timing._utc_now())}
                    if late is not None
                    else {}
                )
                run: Any | None = None
                try:
                    run = await self._trigger_service.trigger_run(
                        latest.agent_id,
                        latest.prompt,
                        latest.session_id,
                        project_id=latest.project_id,
                        run_kind=RunKind.CRON,
                        contributes_to_agent_activity=False,
                        **note,
                    )
                    latest = self._jobs.get(job.id)
                    if latest is None:
                        wait_for_run = getattr(run, "wait", None)
                        if callable(wait_for_run):
                            await wait_for_run()
                        return False
                    latest.last_fired_at = _timing._utc_now_iso()
                    run_id = getattr(run, "id", None)
                    latest.last_run_id = run_id if isinstance(run_id, str) else None
                    if latest.remaining_runs is not None:
                        latest.remaining_runs = max(latest.remaining_runs - 1, 0)
                    self._jobs[latest.id] = latest
                    await self._persist_after_fire(latest.id)

                    wait_for_run = getattr(run, "wait", None)
                    if callable(wait_for_run):
                        await wait_for_run()
                except asyncio.CancelledError:
                    raise
                except RunCancelledError as error:
                    if run is not None:
                        await self._record_run_failure(job.id, error)
                        await self._finalize_exhausted_job(job.id)
                    else:
                        await self._record_trigger_failure(job.id, error)
                        latest = self._jobs.get(job.id)
                        if latest is not None and latest.schedule_type == "once":
                            latest.status = "failed"
                            await self._save_jobs_after_fire(latest.id)
                        if latest is not None:
                            # No Run started, so no Run line reports this firing.
                            _LOGGER.log(
                                logging.WARNING if latest.status == "failed" else logging.INFO,
                                "Cron job Run cancelled before admission (job=%s status=%s)",
                                job.id,
                                latest.status,
                            )
                    return False
                except Exception as error:
                    if run is None and await self._record_unavailable_target(job.id, error):
                        return False
                    if run is None:
                        # Pre-admission failure - the Run never started. A full Queue
                        # or a shutdown window must not burn a recurring job's
                        # five-strike execution-failure budget; once jobs keep their
                        # own fire-claim retry via the False return either way.
                        await self._record_trigger_failure(job.id, error)
                        _LOGGER.error(
                            "Cron job trigger failed before admission for job=%s: %s",
                            job.id,
                            error,
                            exc_info=(type(error), error, error.__traceback__),
                        )
                        return False
                    await self._record_run_failure(job.id, error)
                    await self._finalize_exhausted_job(job.id)
                    _LOGGER.error(
                        "Cron job Run failed for job=%s: %s",
                        job.id,
                        error,
                        exc_info=(type(error), error, error.__traceback__),
                    )
                    return False

                latest = self._jobs.get(job.id)
                if latest is None:
                    return False
                latest.last_completed_at = _timing._utc_now_iso()
                latest.last_outcome = "success"
                latest.last_error = None
                latest.consecutive_failures = 0
                self._jobs[latest.id] = latest
                await self._save_jobs_after_fire(latest.id)
                await self._finalize_exhausted_job(latest.id)
                return True
        finally:
            self._executing_jobs.discard(job.id)

    async def _record_run_failure(self, job_id: str, error: BaseException) -> None:
        if self._note_run_failure(job_id, error):
            await self._save_jobs_after_fire(job_id)

    def _note_run_failure(self, job_id: str, error: BaseException) -> bool:
        """Account one failed execution in memory; ``False`` when the job is gone."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.last_completed_at = _timing._utc_now_iso()
        job.last_outcome = "cancelled" if type(error).__name__ == "RunCancelledError" else "failed"
        job.last_error = _truncate_error(str(error) or type(error).__name__)
        _count_consecutive_failure(job)
        self._jobs[job_id] = job
        return True

    async def _record_unavailable_target(self, job_id: str, error: Exception) -> bool:
        """Record a fire that could not start because its target is unavailable.

        Returns ``False`` for any other error, which the caller records as a
        trigger failure. No Run started, so no finite run is consumed. A pinned
        Session or a target Agent or Project that no longer exists lasts until
        the job is edited (for example, an Agent Takeover moved the Session, or
        the Agent was removed), so unlike a capacity or shutdown rejection it
        counts toward the stop rule like a failed Run; a once job keeps its
        bounded fire retries. A single miss, such as one during an Agent rename,
        is cleared by the next success. A target that exists but cannot run (for
        example, no usable Model) can recover without an edit, so it is recorded
        without counting.
        """
        if not isinstance(error, (SessionNotFoundError, AgentResolutionError)):
            return False
        job = self._jobs.get(job_id)
        if job is None:
            return True
        if isinstance(error, SessionNotFoundError):
            reason = _missing_session_text(job)
            counts = True
        else:
            reason = str(_target_validation_error(job, error))
            counts = isinstance(
                error, (ResolutionAgentNotFoundError, ResolutionProjectNotFoundError)
            )
        job.last_outcome = "failed"
        job.last_error = _truncate_error(reason)
        _LOGGER.warning(
            "Cron job could not start its Run (job=%s reason=%s)", job_id, job.last_error
        )
        if counts:
            _count_consecutive_failure(job)
        self._jobs[job_id] = job
        await self._save_jobs_after_fire(job_id)
        return True

    async def _record_trigger_failure(self, job_id: str, error: BaseException) -> None:
        """Record a pre-admission trigger failure without execution accounting.

        The Run never started, so ``consecutive_failures`` must not advance and a
        recurring job must not fatal-stop over capacity or shutdown windows. The
        error stays visible on the job for operators.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.last_outcome = "failed"
        job.last_error = _truncate_error(str(error) or type(error).__name__)
        self._jobs[job_id] = job
        await self._save_jobs_after_fire(job_id)

    async def _finalize_exhausted_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.status != "active" or job.remaining_runs != 0:
            return
        job.status = "completed" if job.last_outcome == "success" else "failed"
        self._jobs[job_id] = job
        await self._save_jobs_after_fire(job_id)
        _LOGGER.log(
            logging.INFO if job.status == "completed" else logging.WARNING,
            "Cron job finished its last run (job=%s outcome=%s status=%s)",
            job_id,
            job.last_outcome,
            job.status,
        )

    async def _persist_after_fire(self, job_id: str) -> None:
        """Persist job state after a fire, giving up after bounded retries.

        A persistently unwritable jobs file must not hang the firing task
        forever: the previous unbounded retry loop stalled the job task for as
        long as the storage fault lasted. After the cap this logs at error and
        continues - in-memory state stays authoritative for the process, and
        the next successful save anywhere re-syncs the file.
        """
        for _attempt in range(_POST_FIRE_SAVE_MAX_ATTEMPTS):
            if await self._save_jobs_after_fire(job_id):
                return
            await _timing._sleep(_POST_FIRE_SAVE_RETRY_SECONDS)
        _LOGGER.error(
            "Cron job state could not be persisted after %d attempts (job=%s); "
            "continuing with in-memory state",
            _POST_FIRE_SAVE_MAX_ATTEMPTS,
            job_id,
        )

    async def _save_jobs_after_fire(self, job_id: str) -> bool:
        try:
            await self._save_jobs_async()
        except CronStorageError as error:
            _log_fire_save_failure(job_id, error)
            return False

        self._notify_changed()
        return True

    def _ensure_jobs_loaded(self, *, allow_degraded: bool = False) -> None:
        if self._jobs_loaded:
            if self._storage_load_error is not None and not allow_degraded:
                raise CronStorageError(str(self._storage_load_error))
            return
        try:
            self._jobs = self._load_jobs()
            self._storage_load_error = None
            self._jobs_loaded = True
        except CronStorageError as error:
            self._degrade_invalid_storage(error)
            if not allow_degraded:
                raise CronStorageError(str(error)) from error

    def _degrade_invalid_storage(self, error: CronStorageError) -> None:
        """Keep Runtime available while preventing writes over unreadable Cron data."""
        self._jobs = {}
        self._invalid_job_entries = []
        self._storage_load_error = error
        self._jobs_loaded = True
        _LOGGER.error("Cron storage is invalid; scheduling is disabled: %s", error)

    def _ensure_storage_exists(self) -> None:
        try:
            self._cron_dir.mkdir(parents=True, exist_ok=True)
            if not exists_strict(self._jobs_path):
                write_json_document(self._jobs_path, {"jobs": []}, CRON_JOBS_FORMAT)
        except OSError as error:
            raise CronStorageError(
                f"Cannot initialize cron storage at {self._cron_dir}: {error}"
            ) from error

    def _track_crash_save(self, save: Awaitable[bool]) -> None:
        task = asyncio.ensure_future(save)
        self._crash_saves.add(task)
        task.add_done_callback(self._crash_saves.discard)

    def _restart_job_task(self, job: CronJob) -> None:
        if job.id in self._executing_jobs:
            self._pending_restarts.add(job.id)
            return
        self._cancel_job_task(job.id)
        if job.status != "active":
            return
        self._start_job_task(job)

    def _on_job_task_done(self, job_id: str, task: asyncio.Task[None]) -> None:
        owns_job_slot = self._job_tasks.get(job_id) is task
        if owns_job_slot:
            self._job_tasks.pop(job_id, None)

        if owns_job_slot and job_id in self._pending_restarts:
            self._pending_restarts.discard(job_id)
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    _LOGGER.error(
                        "Cron execution failed during reschedule (job=%s)",
                        job_id,
                        exc_info=(type(error), error, error.__traceback__),
                    )
            job = self._jobs.get(job_id)
            if self._started and job is not None and job.status == "active":
                self._start_job_task(job)
            return
        if task.cancelled():
            return

        error = task.exception()
        if error is None:
            return

        _LOGGER.error(
            "Cron job task failed for job=%s: %s",
            job_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        if not owns_job_slot:
            return

        job = self._jobs.get(job_id)
        if job is not None and job.status == "active" and job.schedule_type == "once":
            job.status = "failed"
            self._jobs[job_id] = job
        if self._note_run_failure(job_id, error):
            # A crashed job task leaves only this callback, which must not wait for the file.
            self._track_crash_save(self._save_jobs_after_fire(job_id))

        job = self._jobs.get(job_id)
        if self._started and job is not None and job.status == "active":
            self._start_job_task(job)

    def _validate_job(self, job: CronJob, *, validate_references: bool = True) -> None:
        normalize_job_fields(job)
        if validate_references:
            self._validate_references(job)
        _schedule.normalize_job_schedule(self._timezone, _timing._utc_now(), job)

    async def _validate_references_async(self, job: CronJob) -> None:
        """Event-Loop-safe :meth:`_validate_references`: its reads use the Session pool."""
        if self._sessions is None:
            self._validate_references(job)
            return
        await self._sessions.run_async(self._validate_references, job)

    def _validate_references(self, job: CronJob) -> None:
        """Refuse a job whose target Agent or selected Session is unavailable. Blocking."""
        if self._agent_resolver is not None:
            try:
                self._agent_resolver.resolve_agent(job.project_id, job.agent_id)
            except Exception as error:
                raise _target_validation_error(job, error) from error
        if (
            job.session_id is not None
            and self._sessions is not None
            and not self._sessions.exists(
                SessionAddress(
                    project_id=job.project_id, agent_id=job.agent_id, session_id=job.session_id
                )
            )
        ):
            raise CronJobValidationError(_missing_session_text(job))

    def _validate_capacity(self, candidate: CronJob, *, replacing_id: str | None = None) -> None:
        if candidate.status != "active":
            return
        active_jobs = sum(
            1
            for job_id, job in self._jobs.items()
            if job_id != replacing_id and job.status == "active"
        )
        if active_jobs >= MAX_ACTIVE_CRON_JOBS:
            raise CronJobValidationError(
                f"At most {MAX_ACTIVE_CRON_JOBS} cron jobs may be active at once"
            )

    def _reject_past_once_run(self, job: CronJob) -> None:
        if job.schedule_type != "once":
            return
        now = _timing._utc_now()
        run_at = _schedule._parse_run_at_utc(self._timezone, job)
        if run_at >= now:
            return

        def local(value: datetime) -> str:
            return value.astimezone(self._timezone).replace(tzinfo=None, microsecond=0).isoformat()

        raise CronJobInPastError(
            f"The one-time schedule {local(run_at)} is in the past: it is now {local(now)} "
            f"in the configured timezone {self._timezone}. Choose a future time"
        )

    @staticmethod
    def _clone_job(job: CronJob) -> CronJob:
        return CronJob.from_dict(job.to_dict())


def _count_consecutive_failure(job: CronJob) -> None:
    """Count one failed fire; a recurring job stops as ``failed`` at the limit."""
    job.consecutive_failures += 1
    if (
        job.schedule_type != "once"
        and job.status != "failed"
        and job.consecutive_failures >= MAX_CONSECUTIVE_CRON_FAILURES
    ):
        job.status = "failed"
        _LOGGER.warning(
            "Cron job stopped after consecutive failures (job=%s failures=%d status=%s)",
            job.id,
            job.consecutive_failures,
            job.status,
        )


def _take_back_edit(live: CronJob, *, before: CronJob, edited: CronJob) -> None:
    """Undo an edit on the live job without losing what a fire recorded since.

    Each field the edit changed returns to its earlier value unless a fire changed
    it again; runs a fire spent from the edited budget are spent from the restored one.
    """
    for field_name in (item.name for item in dataclasses.fields(CronJob)):
        old, new = getattr(before, field_name), getattr(edited, field_name)
        current = getattr(live, field_name)
        if old == new:
            continue
        if current == new:
            setattr(live, field_name, old)
        elif field_name == "remaining_runs" and isinstance(new, int) and isinstance(current, int):
            live.remaining_runs = None if old is None else max(old - (new - current), 0)


def _missing_session_text(job: CronJob) -> str:
    target = format_agent_address(job.agent_id, job.project_id)
    return f"Session does not exist for cron target {target}: {job.session_id}"


def _log_retarget(job_id: str, agent_id: str, new_agent_id: str) -> None:
    # One step of an Agent rename, which logs its own summary line.
    _LOGGER.debug(
        "Cron job retargeted (job=%s agent=%s new_agent=%s)", job_id, agent_id, new_agent_id
    )


def _log_fire_save_failure(job_id: str, error: CronStorageError) -> None:
    _LOGGER.error(
        "Cron job state save failed after firing job=%s: %s",
        job_id,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


@dataclasses.dataclass(frozen=True, slots=True)
class _LateFire:
    """A fire that starts after its due instant, or after earlier fires were missed."""

    owed: _schedule.OwedFire
    noticed_at: datetime
    timezone: ZoneInfo

    @property
    def late_by_seconds(self) -> int:
        return max(int((self.noticed_at - self.owed.due_at).total_seconds()), 0)

    def notice(self, job_id: str, starting_at: datetime) -> str:
        """The note before the Run's input that tells the Agent which fires it replaces."""
        due = self._local(self.owed.due_at)
        if self.late_by_seconds > _ON_TIME_SECONDS:
            sentences = [
                f"Cron job {job_id} was due at {due} and is starting late, at "
                f"{self._local(starting_at)}."
            ]
        else:
            sentences = [f"Cron job {job_id} is starting at its due time, {due}."]
        earlier = self.owed.earlier_missed
        first = self.owed.first_due_at
        if earlier == 1 and first is not None:
            sentences.append(
                f"The earlier due time {self._local(first)} was missed and will not run separately."
            )
        elif earlier and first is not None:
            count = (
                f"At least {earlier}" if earlier >= _schedule.MAX_COUNTED_MISSED_FIRES else earlier
            )
            sentences.append(
                f"{count} earlier due times, the first at {self._local(first)}, were missed and "
                "will not run separately."
            )
        sentences.append(
            f"vBot did not run the job at {'those times' if earlier else 'the due time'}, for "
            "example because the server was off or the computer was asleep."
        )
        sentences.append(
            "Carry out the instruction that follows now, and adapt any part of it that depends "
            "on when it runs, such as a greeting, a deadline that has passed, or the period it "
            "covers."
        )
        return " ".join(sentences)

    def skip_reason(self, max_delay_seconds: int) -> str:
        """Why the missed fire did not start, for the job's ``last_error``."""
        due = f"the fire due at {self._local(self.owed.due_at)}"
        earlier = self.owed.earlier_missed
        if earlier:
            count = (
                f"at least {earlier}"
                if earlier >= _schedule.MAX_COUNTED_MISSED_FIRES
                else str(earlier)
            )
            due += f" and {count} earlier {'one' if earlier == 1 else 'ones'}"
        if max_delay_seconds == 0:
            return f"vBot missed {due} and skipped it: the job's max_delay is 0m."
        return (
            f"vBot missed {due} and skipped it: at {self._local(self.noticed_at)} it was more "
            f"than the job's max_delay of {format_max_delay(max_delay_seconds)} late."
        )

    def _local(self, instant: datetime) -> str:
        return instant.astimezone(self.timezone).replace(microsecond=0).isoformat()


def _exceeds_max_delay(job: CronJob, late: _LateFire) -> bool:
    return job.max_delay_seconds is not None and late.late_by_seconds > max(
        job.max_delay_seconds, _ON_TIME_SECONDS
    )


def format_max_delay(seconds: int) -> str:
    """Spell a maximum delay as the duration the cron Tool accepts, such as ``2h`` or ``0m``."""
    return "0m" if seconds == 0 else _format_duration(seconds)
