"""Cron automation service for scheduled TriggerService runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter  # type: ignore[import-untyped]

from core.settings import SettingsValidationError, load_validated_cron_jobs_json
from core.utils.errors import VBotError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.automation import TriggerService

ScheduleType = Literal["cron", "once"]
CronJobStatus = Literal["active", "paused", "completed", "failed"]

_ALLOWED_SCHEDULE_TYPES = frozenset(("cron", "once"))
_ALLOWED_STATUSES = frozenset(("active", "paused", "completed", "failed"))
_RESTART_FIELDS = frozenset(("schedule_type", "cron_expression", "run_at", "timezone", "status"))
_ONCE_RETRY_DELAY_SECONDS = 60.0
# Exponential backoff for repeatedly failing once-job fires: the Nth retry waits
# base * factor**(N-1), capped, and the job is abandoned after the attempt limit
# so a permanently failing once job (e.g. its agent was deleted) stops looping.
_ONCE_RETRY_BACKOFF_FACTOR = 2.0
_ONCE_RETRY_MAX_DELAY_SECONDS = 3600.0
_ONCE_MAX_FIRE_ATTEMPTS = 5
_ONCE_FIRE_CLAIMS_DIR_NAME = "once-fire-claims"
_MUTABLE_FIELDS = frozenset(
    (
        "agent_id",
        "prompt",
        "schedule_type",
        "cron_expression",
        "run_at",
        "timezone",
        "session_id",
        "status",
        "project_id",
    )
)

_LOGGER = get_logger("automation.cron")


class CronServiceError(VBotError):
    """Base class for expected cron service errors."""


class CronJobNotFoundError(CronServiceError):
    """Raised when a cron job id is missing."""


class CronJobValidationError(CronServiceError):
    """Raised when cron job data is invalid."""


class CronStorageError(CronServiceError):
    """Raised when cron storage cannot be read or written."""


@dataclass(slots=True)
class CronJob:
    """Persisted cron job record.

    ``project_id`` is the project dimension the job fires into: ``None`` is a
    global/identity-agent target (today's behavior, byte-identical), a set value
    scopes the fired Session and Run to that project's anchor. It is the
    structured half of the outside ``agent@projekt`` address form (parsed once at
    the RPC edge), never an ``@`` string stored in ``agent_id``.
    """

    id: str
    agent_id: str
    prompt: str
    schedule_type: ScheduleType
    cron_expression: str | None
    run_at: str | None
    timezone: str | None
    session_id: str | None
    status: CronJobStatus
    last_fired_at: str | None
    created_at: str
    project_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize one CronJob to a JSON-compatible payload."""
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "prompt": self.prompt,
            "schedule_type": self.schedule_type,
            "cron_expression": self.cron_expression,
            "run_at": self.run_at,
            "timezone": self.timezone,
            "session_id": self.session_id,
            "status": self.status,
            "last_fired_at": self.last_fired_at,
            "created_at": self.created_at,
            "project_id": self.project_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CronJob:
        """Create one CronJob from persisted JSON data."""
        return cls(
            id=str(payload["id"]),
            agent_id=str(payload["agent_id"]),
            prompt=str(payload["prompt"]),
            schedule_type=payload["schedule_type"],
            cron_expression=payload.get("cron_expression"),
            run_at=payload.get("run_at"),
            timezone=payload.get("timezone"),
            session_id=payload.get("session_id"),
            status=payload["status"],
            last_fired_at=payload.get("last_fired_at"),
            created_at=str(payload["created_at"]),
            project_id=payload.get("project_id"),
        )


class CronService:
    """Manage cron jobs, persistence, and per-job scheduling tasks."""

    def __init__(self, trigger_service: TriggerService, data_root: str | Path) -> None:
        self._trigger_service = trigger_service
        self._data_root = Path(data_root).expanduser()
        self._cron_dir = self._data_root / "cron"
        self._jobs_path = self._cron_dir / "jobs.json"
        self._once_fire_claims_dir = self._cron_dir / _ONCE_FIRE_CLAIMS_DIR_NAME
        self._jobs: dict[str, CronJob] = {}
        self._jobs_loaded = False
        self._job_tasks: dict[str, asyncio.Task[None]] = {}
        self._started = False

    def create_job(
        self,
        *,
        agent_id: str,
        prompt: str,
        schedule_type: ScheduleType,
        cron_expression: str | None = None,
        run_at: str | None = None,
        timezone: str | None = None,
        session_id: str | None = None,
        status: CronJobStatus = "active",
        project_id: str | None = None,
    ) -> CronJob:
        """Create and persist a new cron job.

        ``project_id=None`` is a global/identity target (unchanged); a set value
        scopes the fired Session/Run to that project's anchor.
        """
        self._ensure_jobs_loaded()
        job = CronJob(
            id=str(uuid4()),
            agent_id=agent_id,
            prompt=prompt,
            schedule_type=schedule_type,
            cron_expression=cron_expression,
            run_at=run_at,
            timezone=timezone,
            session_id=session_id,
            status=status,
            last_fired_at=None,
            created_at=_utc_now_iso(),
            project_id=project_id,
        )
        self._validate_job(job)
        self._jobs[job.id] = job
        self._save_jobs()

        if self._started and job.status == "active":
            self._start_job_task(job)

        return self._clone_job(job)

    def list_jobs(self) -> list[CronJob]:
        """List all persisted cron jobs in stable created-order."""
        self._ensure_jobs_loaded()
        ordered = sorted(self._jobs.values(), key=lambda value: (value.created_at, value.id))
        return [self._clone_job(job) for job in ordered]

    def get_job(self, job_id: str) -> CronJob:
        """Get one cron job by id."""
        self._ensure_jobs_loaded()
        if job_id not in self._jobs:
            raise CronJobNotFoundError(f"Cron job not found: {job_id}")
        return self._clone_job(self._jobs[job_id])

    def update_job(self, job_id: str, **fields: Any) -> CronJob:
        """Update mutable cron job fields and persist changes."""
        self._ensure_jobs_loaded()
        job = self._jobs.get(job_id)
        if job is None:
            raise CronJobNotFoundError(f"Cron job not found: {job_id}")

        unknown_fields = sorted(set(fields) - _MUTABLE_FIELDS)
        if unknown_fields:
            joined = ", ".join(unknown_fields)
            raise CronJobValidationError(f"Unsupported cron job fields: {joined}")

        if not fields:
            return self._clone_job(job)

        candidate = self._clone_job(job)
        restart_task = any(field in _RESTART_FIELDS for field in fields)

        for field_name, field_value in fields.items():
            setattr(candidate, field_name, field_value)

        self._validate_job(candidate)
        self._jobs[job_id] = candidate
        self._save_jobs()

        if self._started and restart_task:
            self._restart_job_task(candidate)

        return self._clone_job(candidate)

    def delete_job(self, job_id: str) -> None:
        """Delete one cron job and cancel any active task."""
        self._ensure_jobs_loaded()
        if job_id not in self._jobs:
            raise CronJobNotFoundError(f"Cron job not found: {job_id}")

        self._jobs.pop(job_id)
        self._save_jobs()
        self._remove_once_fire_claim(job_id)
        self._cancel_job_task(job_id)

    def enable_job(self, job_id: str) -> CronJob:
        """Set a cron job status to active."""
        self._ensure_jobs_loaded()
        existing = self._jobs.get(job_id)
        if existing is None:
            raise CronJobNotFoundError(f"Cron job not found: {job_id}")
        if existing.status == "completed":
            raise CronJobValidationError("Completed jobs cannot be re-enabled")
        return self.update_job(job_id, status="active")

    def disable_job(self, job_id: str) -> CronJob:
        """Set a cron job status to paused."""
        self._ensure_jobs_loaded()
        existing = self._jobs.get(job_id)
        if existing is None:
            raise CronJobNotFoundError(f"Cron job not found: {job_id}")
        if existing.status == "completed":
            raise CronJobValidationError("Completed jobs cannot be paused")
        return self.update_job(job_id, status="paused")

    def start(self) -> None:
        """Load jobs and start per-job scheduling tasks. Idempotent."""
        if self._started:
            return

        self._jobs = self._load_jobs()
        self._jobs_loaded = True
        self._started = True
        reference_time = _utc_now()
        needs_save = False
        once_claims_to_remove: list[str] = []

        for job in self._jobs.values():
            if job.status != "active":
                continue
            if job.schedule_type == "once":
                claimed_at = self._read_once_fire_claimed_at(job.id)
                if claimed_at is not None:
                    _LOGGER.warning(
                        "Marking claimed once job as completed (id=%s claimed_at=%s)",
                        job.id,
                        claimed_at,
                    )
                    job.status = "completed"
                    job.last_fired_at = claimed_at
                    needs_save = True
                    once_claims_to_remove.append(job.id)
                    continue
            if job.schedule_type == "once" and self._is_missed_once_job(job, reference_time):
                _LOGGER.warning(
                    "Marking missed once job as completed (id=%s run_at=%s)",
                    job.id,
                    job.run_at,
                )
                job.status = "completed"
                needs_save = True
                continue
            self._start_job_task(job)

        if needs_save:
            self._save_jobs()
            for job_id in once_claims_to_remove:
                self._remove_once_fire_claim(job_id)

    def stop(self) -> None:
        """Cancel all running cron tasks. Idempotent."""
        if not self._started and not self._job_tasks:
            return

        for job_id in list(self._job_tasks):
            self._cancel_job_task(job_id)
        self._started = False

    async def aclose(self) -> None:
        """Stop cron scheduling and await canceled job tasks."""
        tasks = list(self._job_tasks.values())
        self.stop()

        pending_tasks = [task for task in tasks if not task.done()]
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

    def _load_jobs(self) -> dict[str, CronJob]:
        """Load cron jobs from <data_root>/cron/jobs.json."""
        self._ensure_storage_exists()
        try:
            raw_payload = load_validated_cron_jobs_json(self._jobs_path)
        except SettingsValidationError as error:
            raise CronStorageError(str(error)) from error

        jobs: dict[str, CronJob] = {}
        for item in raw_payload:
            job = CronJob.from_dict(item)
            self._validate_job(job)
            jobs[job.id] = job
        return jobs

    def _save_jobs(self) -> None:
        """Persist cron jobs to <data_root>/cron/jobs.json using atomic replace."""
        self._ensure_storage_exists()
        payload = [
            job.to_dict() for job in sorted(self._jobs.values(), key=lambda item: item.created_at)
        ]
        temp_path = self._jobs_path.with_name(f"{self._jobs_path.name}.{uuid4().hex}.tmp")

        try:
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
            os.replace(temp_path, self._jobs_path)
        except OSError as error:
            self._safe_remove_temporary_file(temp_path)
            raise CronStorageError(f"Cannot write {self._jobs_path}: {error}") from error

    def _start_job_task(self, job: CronJob) -> None:
        """Create and track one asyncio task for an active cron job."""
        if job.status != "active":
            return

        self._cancel_job_task(job.id)

        task: asyncio.Task[None]
        if job.schedule_type == "cron":
            task = asyncio.create_task(self._run_cron_job(job), name=f"cron-job:{job.id}:cron")
        else:
            task = asyncio.create_task(self._run_once_job(job), name=f"cron-job:{job.id}:once")

        self._job_tasks[job.id] = task

        def on_done(completed_task: asyncio.Task[None], job_id: str = job.id) -> None:
            self._on_job_task_done(job_id, completed_task)

        task.add_done_callback(on_done)

    def _cancel_job_task(self, job_id: str) -> None:
        """Cancel and forget one tracked asyncio task if present."""
        task = self._job_tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _run_cron_job(self, job: CronJob) -> None:
        """Schedule repeated fires from croniter and call TriggerService."""
        while True:
            current = self._jobs.get(job.id)
            if current is None or current.status != "active" or current.schedule_type != "cron":
                return

            if current.cron_expression is None:
                raise CronJobValidationError(
                    f"Cron job {current.id} is missing cron_expression while active"
                )

            timezone = self._resolve_timezone(current.timezone)
            now_local = datetime.now(timezone)
            next_fire_local = croniter(current.cron_expression, now_local).get_next(datetime)
            if next_fire_local.tzinfo is None:
                next_fire_local = next_fire_local.replace(tzinfo=timezone)

            delay_seconds = max((next_fire_local.astimezone(UTC) - _utc_now()).total_seconds(), 0.0)
            await asyncio.sleep(delay_seconds)

            latest = self._jobs.get(job.id)
            if latest is None or latest.status != "active" or latest.schedule_type != "cron":
                return

            if not await self._trigger_job_run(latest):
                continue

            latest = self._jobs.get(job.id)
            if latest is None:
                return

            latest.last_fired_at = _utc_now_iso()
            self._jobs[latest.id] = latest
            self._save_jobs_after_fire(latest.id)

    async def _run_once_job(self, job: CronJob) -> None:
        """Sleep until run_at, fire once, then mark completed.

        A failed fire (claim write or trigger error) is retried with bounded
        exponential backoff. Once the attempt limit is reached the job is
        abandoned (marked failed) and logged, so a permanently failing once
        job stops retrying instead of looping forever (e.g. its agent was
        deleted, leaving every trigger attempt to fail).
        """
        failed_fire_attempts = 0
        while True:
            current = self._jobs.get(job.id)
            if current is None or current.status != "active" or current.schedule_type != "once":
                return

            run_at_utc = self._parse_run_at_utc(current)
            delay_seconds = max((run_at_utc - _utc_now()).total_seconds(), 0.0)
            await asyncio.sleep(delay_seconds)

            latest = self._jobs.get(job.id)
            if latest is None or latest.status != "active" or latest.schedule_type != "once":
                return

            claimed_at = _utc_now_iso()
            try:
                self._write_once_fire_claim(latest, claimed_at)
            except CronStorageError as error:
                _LOGGER.error(
                    "Cron once job fire claim failed for job=%s: %s",
                    latest.id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
                failed_fire_attempts += 1
                if await self._back_off_or_abandon_once_job(job.id, failed_fire_attempts):
                    return
                continue

            if not await self._trigger_job_run(latest):
                self._remove_once_fire_claim(latest.id)
                failed_fire_attempts += 1
                if await self._back_off_or_abandon_once_job(job.id, failed_fire_attempts):
                    return
                continue

            latest = self._jobs.get(job.id)
            if latest is None:
                return

            latest.status = "completed"
            latest.last_fired_at = _utc_now_iso()
            self._jobs[latest.id] = latest
            while not self._save_jobs_after_fire(latest.id):
                await asyncio.sleep(_ONCE_RETRY_DELAY_SECONDS)
            self._remove_once_fire_claim(latest.id)
            return

    async def _back_off_or_abandon_once_job(self, job_id: str, attempts: int) -> bool:
        """Wait out the backoff for a failed once fire, or abandon after the cap.

        Returns True when the job has been abandoned (marked failed) and the
        caller must stop; False after sleeping the backoff delay so the caller
        can retry the fire.
        """
        if attempts >= _ONCE_MAX_FIRE_ATTEMPTS:
            self._abandon_once_job(job_id, attempts)
            return True

        await asyncio.sleep(_once_retry_delay(attempts))
        return False

    def _abandon_once_job(self, job_id: str, attempts: int) -> None:
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
        self._save_jobs_after_fire(job_id)
        self._remove_once_fire_claim(job_id)

    async def _trigger_job_run(self, job: CronJob) -> bool:
        try:
            await self._trigger_service.trigger_run(
                job.agent_id,
                job.prompt,
                job.session_id,
                project_id=job.project_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.error(
                "Cron job trigger failed for job=%s: %s",
                job.id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            return False

        return True

    def _save_jobs_after_fire(self, job_id: str) -> bool:
        try:
            self._save_jobs()
        except CronStorageError as error:
            _LOGGER.error(
                "Cron job state save failed after firing job=%s: %s",
                job_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            return False

        return True

    def _ensure_jobs_loaded(self) -> None:
        if self._jobs_loaded:
            return
        self._jobs = self._load_jobs()
        self._jobs_loaded = True

    def _ensure_storage_exists(self) -> None:
        try:
            self._cron_dir.mkdir(parents=True, exist_ok=True)
            if not self._jobs_path.exists():
                self._jobs_path.write_text("[]\n", encoding="utf-8")
        except OSError as error:
            raise CronStorageError(
                f"Cannot initialize cron storage at {self._cron_dir}: {error}"
            ) from error

    def _restart_job_task(self, job: CronJob) -> None:
        self._cancel_job_task(job.id)
        if job.status != "active":
            return
        self._start_job_task(job)

    def _on_job_task_done(self, job_id: str, task: asyncio.Task[None]) -> None:
        if self._job_tasks.get(job_id) is task:
            self._job_tasks.pop(job_id, None)

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

    def _validate_job(self, job: CronJob) -> None:
        if not isinstance(job.id, str) or not job.id:
            raise CronJobValidationError("id must be a non-empty string")

        if not isinstance(job.agent_id, str) or not job.agent_id.strip():
            raise CronJobValidationError("agent_id must be a non-empty string")
        job.agent_id = job.agent_id.strip()

        if not isinstance(job.prompt, str) or not job.prompt.strip():
            raise CronJobValidationError("prompt must be a non-empty string")
        job.prompt = job.prompt.strip()

        if job.schedule_type not in _ALLOWED_SCHEDULE_TYPES:
            raise CronJobValidationError("schedule_type must be 'cron' or 'once'")

        if job.status not in _ALLOWED_STATUSES:
            raise CronJobValidationError("status must be active, paused, completed, or failed")

        if job.session_id is not None and not isinstance(job.session_id, str):
            raise CronJobValidationError("session_id must be a string when provided")

        if job.project_id is not None:
            if not isinstance(job.project_id, str):
                raise CronJobValidationError("project_id must be a string when provided")
            normalized_project_id = job.project_id.strip()
            job.project_id = normalized_project_id or None

        if job.timezone is not None and not isinstance(job.timezone, str):
            raise CronJobValidationError("timezone must be a string when provided")

        if job.timezone is not None:
            timezone = job.timezone.strip()
            if not timezone:
                job.timezone = None
            else:
                self._resolve_timezone(timezone)
                job.timezone = timezone

        self._parse_utc_timestamp(job.created_at, field_name="created_at")
        if job.last_fired_at is not None:
            self._parse_utc_timestamp(job.last_fired_at, field_name="last_fired_at")

        if job.schedule_type == "cron":
            if not isinstance(job.cron_expression, str) or not job.cron_expression.strip():
                raise CronJobValidationError("cron_expression is required for cron jobs")
            normalized_expression = job.cron_expression.strip()
            if not croniter.is_valid(normalized_expression):
                raise CronJobValidationError("cron_expression is invalid")
            job.cron_expression = normalized_expression
            job.run_at = None
            return

        if not isinstance(job.run_at, str) or not job.run_at.strip():
            raise CronJobValidationError("run_at is required for once jobs")
        job.run_at = job.run_at.strip()
        self._parse_run_at_utc(job)
        job.cron_expression = None

    def _parse_run_at_utc(self, job: CronJob) -> datetime:
        if job.run_at is None:
            raise CronJobValidationError("run_at is required for once jobs")

        parsed = _parse_iso_datetime(job.run_at, field_name="run_at", allow_naive=True)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._resolve_timezone(job.timezone))
        return parsed.astimezone(UTC)

    def _parse_utc_timestamp(self, value: str, *, field_name: str) -> datetime:
        parsed = _parse_iso_datetime(value, field_name=field_name, allow_naive=False)
        if parsed.utcoffset() != UTC.utcoffset(parsed):
            raise CronJobValidationError(f"{field_name} must be a UTC timestamp")
        return parsed

    def _resolve_timezone(self, timezone_name: str | None) -> tzinfo:
        if timezone_name:
            normalized_timezone = timezone_name.strip()
            if normalized_timezone.upper() == "UTC":
                return UTC
            try:
                return ZoneInfo(normalized_timezone)
            except ZoneInfoNotFoundError as error:
                raise CronJobValidationError(f"Unknown timezone: {timezone_name}") from error

        local_timezone = datetime.now().astimezone().tzinfo
        if local_timezone is not None:
            return local_timezone
        return UTC

    def _is_missed_once_job(self, job: CronJob, reference_time_utc: datetime) -> bool:
        if job.schedule_type != "once":
            return False
        return self._parse_run_at_utc(job) < reference_time_utc

    def _write_once_fire_claim(self, job: CronJob, claimed_at: str) -> None:
        self._ensure_storage_exists()
        claim_path = self._once_fire_claim_path(job.id)
        temp_path = claim_path.with_name(f"{claim_path.name}.{uuid4().hex}.tmp")
        payload = {
            "job_id": job.id,
            "claimed_at": claimed_at,
            "run_at": job.run_at,
        }

        try:
            self._once_fire_claims_dir.mkdir(parents=True, exist_ok=True)
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
            os.replace(temp_path, claim_path)
        except OSError as error:
            self._safe_remove_temporary_file(temp_path)
            raise CronStorageError(f"Cannot write {claim_path}: {error}") from error

    def _read_once_fire_claimed_at(self, job_id: str) -> str | None:
        claim_path = self._once_fire_claim_path(job_id)
        if not claim_path.exists():
            return None

        try:
            payload = json.loads(claim_path.read_text(encoding="utf-8"))
        except OSError as error:
            raise CronStorageError(f"Cannot read {claim_path}: {error}") from error
        except json.JSONDecodeError as error:
            raise CronStorageError(f"Invalid once fire claim {claim_path}: {error}") from error

        if not isinstance(payload, dict) or payload.get("job_id") != job_id:
            raise CronStorageError(f"Invalid once fire claim {claim_path}: job_id mismatch")

        claimed_at = payload.get("claimed_at")
        if not isinstance(claimed_at, str):
            raise CronStorageError(f"Invalid once fire claim {claim_path}: claimed_at is required")
        self._parse_utc_timestamp(claimed_at, field_name="claimed_at")
        return claimed_at

    def _remove_once_fire_claim(self, job_id: str) -> None:
        claim_path = self._once_fire_claim_path(job_id)
        try:
            claim_path.unlink(missing_ok=True)
        except OSError as error:
            _LOGGER.warning(
                "Cannot remove once job fire claim for job=%s: %s",
                job_id,
                error,
            )

    def _once_fire_claim_path(self, job_id: str) -> Path:
        digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
        return self._once_fire_claims_dir / f"{digest}.json"

    @staticmethod
    def _clone_job(job: CronJob) -> CronJob:
        return CronJob.from_dict(job.to_dict())

    @staticmethod
    def _safe_remove_temporary_file(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return


def _once_retry_delay(attempt: int) -> float:
    """Backoff delay in seconds for the Nth (1-based) failed once-job fire."""
    exponent = max(attempt - 1, 0)
    delay = _ONCE_RETRY_DELAY_SECONDS * (_ONCE_RETRY_BACKOFF_FACTOR**exponent)
    return min(delay, _ONCE_RETRY_MAX_DELAY_SECONDS)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_iso_datetime(value: str, *, field_name: str, allow_naive: bool) -> datetime:
    if not isinstance(value, str) or not value:
        raise CronJobValidationError(f"{field_name} must be a non-empty ISO 8601 timestamp")

    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise CronJobValidationError(f"{field_name} must be a valid ISO 8601 timestamp") from error

    if parsed.tzinfo is None and not allow_naive:
        raise CronJobValidationError(f"{field_name} must include timezone information")

    return parsed
