"""Cron automation service for scheduled TriggerService runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from croniter import croniter  # type: ignore[import-untyped]
from tzlocal import get_localzone, get_localzone_name

from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonObject,
    JsonValidationReport,
    add_error,
    error_diagnostic,
    load_validated_json_file,
    validate_allowed_string,
    validate_json_file,
    validate_non_empty_string,
    validate_optional_allowed_string,
    validate_optional_string,
    warn_unknown_keys,
)
from core.runs import RunKind
from core.settings import is_valid_agent_id, is_valid_project_id
from core.utils.atomic import atomic_write_text
from core.utils.errors import VBotError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.projects import AgentResolver
    from core.sessions import ChatSessionManager

ScheduleType = Literal["cron", "interval", "once"]
CronJobStatus = Literal["active", "paused", "completed", "failed", "missed"]
CronRunOutcome = Literal["success", "failed", "cancelled", "missed", "unknown"]

_ALLOWED_SCHEDULE_TYPES = frozenset(("cron", "interval", "once"))
_ALLOWED_STATUSES = frozenset(("active", "paused", "completed", "failed", "missed"))
_ALLOWED_RUN_OUTCOMES = frozenset(("success", "failed", "cancelled", "missed", "unknown"))
_RESTART_FIELDS = frozenset(
    (
        "schedule_type",
        "cron_expression",
        "interval_seconds",
        "interval_anchor_at",
        "run_at",
        "remaining_runs",
        "status",
    )
)
CRON_EXPRESSION_FIELD_COUNT = 5
MIN_INTERVAL_SECONDS = 60
MAX_ACTIVE_CRON_JOBS = 64
MAX_STORED_CRON_JOBS = 512
MAX_CONCURRENT_CRON_RUNS = 4
MAX_CONSECUTIVE_CRON_FAILURES = 5
TERMINAL_CRON_JOB_STATUSES = frozenset(("completed", "missed"))
_LAST_ERROR_MAX_CHARS = 500
_ONCE_RETRY_DELAY_SECONDS = 60.0
# Exponential backoff for repeatedly failing once-job fires: the Nth retry waits
# base * factor**(N-1), capped, and the job is abandoned after the attempt limit
# so a permanently failing once job (e.g. its agent was deleted) stops looping.
_ONCE_RETRY_BACKOFF_FACTOR = 2.0
_ONCE_RETRY_MAX_DELAY_SECONDS = 3600.0
_ONCE_MAX_FIRE_ATTEMPTS = 5
_ONCE_FIRE_CLAIMS_DIR_NAME = "once-fire-claims"
# asyncio.sleep counts monotonic time, so one full-length sleep would shift a
# fire time by the size of any system-clock correction (e.g. a Raspberry Pi
# syncing NTP after boot). Bounded naps re-derive the remaining delay from the
# wall clock, re-aligning the wake-up to within one interval of the corrected
# clock.
_WALL_CLOCK_RECHECK_SECONDS = 60.0
_CRON_JOB_NAME_MAX_LENGTH = 80
_DURATION_PATTERN = re.compile(r"^(?P<amount>[1-9]\d*)(?P<unit>[mhd])$")
_DURATION_UNIT_SECONDS = {"m": 60, "h": 60 * 60, "d": 24 * 60 * 60}
_MARKDOWN_PREFIX_PATTERN = re.compile(r"^(?:(?:#{1,6}|>|[-*+])\s+|\d+[.)]\s+|\[[ xX]\]\s*)+")
_MUTABLE_FIELDS = frozenset(
    (
        "agent_id",
        "name",
        "prompt",
        "schedule_type",
        "cron_expression",
        "interval_seconds",
        "interval_anchor_at",
        "run_at",
        "remaining_runs",
        "session_id",
        "status",
        "project_id",
    )
)
_CRON_JOB_FIELDS = _MUTABLE_FIELDS | {
    "consecutive_failures",
    "created_at",
    "id",
    "last_attempt_at",
    "last_completed_at",
    "last_error",
    "last_fired_at",
    "last_outcome",
    "last_run_id",
}
_LEGACY_CRON_JOB_FIELDS = frozenset(("timezone",))

_LOGGER = get_logger("automation.cron")


class CronServiceError(VBotError):
    """Base class for expected cron service errors."""


class CronJobNotFoundError(CronServiceError):
    """Raised when a cron job id is missing."""


class CronJobValidationError(CronServiceError):
    """Raised when cron job data is invalid."""


class CronStorageError(CronServiceError):
    """Raised when cron storage cannot be read or written."""


def validate_cron_jobs_file(jobs_path: str | Path) -> JsonValidationReport:
    """Validate persisted ``cron/jobs.json`` without consuming it."""
    return validate_json_file(jobs_path, validate_cron_jobs_data, missing_ok=True)


def load_validated_cron_jobs_json(jobs_path: str | Path) -> list[JsonObject]:
    """Load schema-valid Cron jobs, defaulting a missing file to an empty list."""
    try:
        return cast(
            "list[JsonObject]",
            load_validated_json_file(
                jobs_path,
                validate_cron_jobs_data,
                missing_ok=True,
                missing_default=[],
            ),
        )
    except JsonConfigValidationError as error:
        raise CronStorageError(str(error)) from error


def _load_cron_jobs_payload(jobs_path: str | Path) -> list[Any]:
    """Load the JSON array without letting one bad job reject its siblings."""
    try:
        return cast(
            "list[Any]",
            load_validated_json_file(
                jobs_path,
                _validate_cron_jobs_container,
                missing_ok=True,
                missing_default=[],
            ),
        )
    except JsonConfigValidationError as error:
        raise CronStorageError(str(error)) from error


def _validate_cron_jobs_container(data: Any) -> list[JsonDiagnostic]:
    if isinstance(data, list):
        return []
    return [error_diagnostic("$", f"Expected a JSON array, got {type(data).__name__}")]


def validate_cron_jobs_data(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded raw ``cron/jobs.json`` array."""
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, list):
        return [error_diagnostic("$", f"Expected a JSON array, got {type(data).__name__}")]

    for index, item in enumerate(data):
        _validate_cron_job_data(diagnostics, index, item)
    return diagnostics


def _validate_cron_job_data(diagnostics: list[JsonDiagnostic], index: int, item: Any) -> None:
    item_path = f"$[{index}]"
    if not isinstance(item, dict):
        add_error(diagnostics, item_path, "Expected a JSON object")
        return
    warn_unknown_keys(
        diagnostics,
        item_path,
        item,
        _CRON_JOB_FIELDS | _LEGACY_CRON_JOB_FIELDS,
        "cron job field",
    )
    validate_non_empty_string(diagnostics, f"{item_path}.id", item.get("id"), required=True)
    _validate_cron_agent_id(diagnostics, f"{item_path}.agent_id", item.get("agent_id"))
    validate_non_empty_string(
        diagnostics,
        f"{item_path}.name",
        item.get("name"),
        required=False,
    )
    validate_non_empty_string(diagnostics, f"{item_path}.prompt", item.get("prompt"), required=True)
    validate_allowed_string(
        diagnostics,
        f"{item_path}.schedule_type",
        item.get("schedule_type"),
        _ALLOWED_SCHEDULE_TYPES,
    )
    validate_optional_allowed_string(
        diagnostics,
        f"{item_path}.status",
        item.get("status"),
        _ALLOWED_STATUSES,
    )
    for field_name in (
        "cron_expression",
        "interval_anchor_at",
        "run_at",
        # Accepted only so installations with pre-migration data can load. The
        # runtime ignores this legacy per-job override and the next save drops it.
        "timezone",
        "session_id",
        "project_id",
        "last_fired_at",
        "last_attempt_at",
        "last_completed_at",
        "last_error",
        "last_run_id",
    ):
        validate_optional_string(
            diagnostics,
            f"{item_path}.{field_name}",
            item.get(field_name),
        )
    validate_optional_allowed_string(
        diagnostics,
        f"{item_path}.last_outcome",
        item.get("last_outcome"),
        _ALLOWED_RUN_OUTCOMES,
    )
    consecutive_failures = item.get("consecutive_failures")
    if consecutive_failures is not None and (
        isinstance(consecutive_failures, bool)
        or not isinstance(consecutive_failures, int)
        or consecutive_failures < 0
    ):
        add_error(
            diagnostics,
            f"{item_path}.consecutive_failures",
            "must be a non-negative integer",
        )
    interval_seconds = item.get("interval_seconds")
    if interval_seconds is not None and (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, int)
        or interval_seconds < MIN_INTERVAL_SECONDS
        or interval_seconds % MIN_INTERVAL_SECONDS != 0
    ):
        add_error(
            diagnostics,
            f"{item_path}.interval_seconds",
            f"must be a whole number of minutes ({MIN_INTERVAL_SECONDS} seconds or more)",
        )
    remaining_runs = item.get("remaining_runs")
    if remaining_runs is not None and (
        isinstance(remaining_runs, bool)
        or not isinstance(remaining_runs, int)
        or remaining_runs < 0
    ):
        add_error(
            diagnostics,
            f"{item_path}.remaining_runs",
            "must be a non-negative integer or null",
        )
    validate_non_empty_string(
        diagnostics, f"{item_path}.created_at", item.get("created_at"), required=False
    )


def _validate_cron_agent_id(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        add_error(diagnostics, path, "must be a non-empty string")
    elif not is_valid_agent_id(value):
        add_error(
            diagnostics,
            path,
            "must be 1-64 characters using only letters, numbers, hyphen, or underscore",
        )


@dataclass(frozen=True, slots=True)
class ParsedSchedule:
    """Canonical schedule fields derived from one agent-facing schedule string."""

    schedule_type: ScheduleType
    cron_expression: str | None = None
    interval_seconds: int | None = None
    interval_anchor_at: str | None = None
    run_at: str | None = None

    def as_job_fields(self) -> dict[str, str | int | None]:
        """Return all persisted schedule fields, clearing incompatible kinds."""
        return {
            "schedule_type": self.schedule_type,
            "cron_expression": self.cron_expression,
            "interval_seconds": self.interval_seconds,
            "interval_anchor_at": self.interval_anchor_at,
            "run_at": self.run_at,
        }


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
    name: str
    prompt: str
    schedule_type: ScheduleType
    cron_expression: str | None
    run_at: str | None
    session_id: str | None
    status: CronJobStatus
    last_fired_at: str | None
    created_at: str
    project_id: str | None = None
    interval_seconds: int | None = None
    interval_anchor_at: str | None = None
    remaining_runs: int | None = None
    last_attempt_at: str | None = None
    last_completed_at: str | None = None
    last_run_id: str | None = None
    last_outcome: CronRunOutcome | None = None
    last_error: str | None = None
    consecutive_failures: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize one CronJob to a JSON-compatible payload."""
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "name": self.name,
            "prompt": self.prompt,
            "schedule_type": self.schedule_type,
            "cron_expression": self.cron_expression,
            "interval_seconds": self.interval_seconds,
            "interval_anchor_at": self.interval_anchor_at,
            "run_at": self.run_at,
            "remaining_runs": self.remaining_runs,
            "session_id": self.session_id,
            "status": self.status,
            "last_fired_at": self.last_fired_at,
            "created_at": self.created_at,
            "project_id": self.project_id,
            "last_attempt_at": self.last_attempt_at,
            "last_completed_at": self.last_completed_at,
            "last_run_id": self.last_run_id,
            "last_outcome": self.last_outcome,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CronJob:
        """Create one CronJob from persisted JSON data."""
        return cls(
            id=str(payload["id"]),
            agent_id=str(payload["agent_id"]),
            name=str(payload.get("name") or _derive_legacy_cron_job_name(payload["prompt"])),
            prompt=str(payload["prompt"]),
            schedule_type=payload["schedule_type"],
            cron_expression=payload.get("cron_expression"),
            interval_seconds=payload.get("interval_seconds"),
            interval_anchor_at=payload.get("interval_anchor_at"),
            run_at=payload.get("run_at"),
            remaining_runs=(
                payload.get("remaining_runs")
                if payload.get("remaining_runs") is not None
                else 1
                if payload["schedule_type"] == "once"
                else None
            ),
            session_id=payload.get("session_id"),
            status=payload.get("status") or "active",
            last_fired_at=payload.get("last_fired_at"),
            created_at=str(payload.get("created_at") or _utc_now_iso()),
            project_id=payload.get("project_id"),
            last_attempt_at=payload.get("last_attempt_at"),
            last_completed_at=payload.get("last_completed_at"),
            last_run_id=payload.get("last_run_id"),
            last_outcome=payload.get("last_outcome"),
            last_error=payload.get("last_error"),
            consecutive_failures=int(payload.get("consecutive_failures") or 0),
        )


class CronService:
    """Manage cron jobs, persistence, and per-job scheduling tasks."""

    def __init__(
        self,
        trigger_service: TriggerService,
        data_root: str | Path,
        *,
        agent_resolver: AgentResolver | None = None,
        sessions: ChatSessionManager | None = None,
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
        self._run_slots = asyncio.Semaphore(MAX_CONCURRENT_CRON_RUNS)
        self._changed_callbacks: set[Callable[[], None]] = set()
        self._started = False

    def add_changed_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to persisted Cron changes and return an unsubscribe function."""
        self._changed_callbacks.add(callback)

        def unsubscribe() -> None:
            self._changed_callbacks.discard(callback)

        return unsubscribe

    def create_job(
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
    ) -> CronJob:
        """Create and persist a new cron job.

        ``project_id=None`` is a global/identity target (unchanged); a set value
        scopes the fired Session/Run to that project's anchor.
        """
        self._ensure_jobs_loaded()
        if len(self._jobs) >= MAX_STORED_CRON_JOBS:
            raise CronJobValidationError(
                f"Cron stores at most {MAX_STORED_CRON_JOBS} jobs; delete history first"
            )
        if schedule_type == "interval" and interval_anchor_at is None:
            interval_anchor_at = _utc_now_iso()
        job = CronJob(
            id=str(uuid4()),
            agent_id=agent_id,
            name=name if name is not None else _derive_legacy_cron_job_name(prompt),
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
            created_at=_utc_now_iso(),
            project_id=project_id,
        )
        self._validate_job(job)
        self._validate_capacity(job)
        self._jobs[job.id] = job
        try:
            self._save_jobs()
        except Exception:
            self._jobs.pop(job.id, None)
            raise
        self._notify_changed()

        if self._started and job.status == "active":
            self._start_job_task(job)

        _LOGGER.info(
            "Cron job created (job=%s agent=%s%s schedule_type=%s status=%s)",
            job.id,
            job.agent_id,
            f" project={job.project_id}" if job.project_id else "",
            job.schedule_type,
            job.status,
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
        """Return the server's canonical IANA timezone name."""
        try:
            return get_localzone_name()
        except Exception as error:
            _LOGGER.warning("Could not resolve system timezone name: %s", error)
            return "UTC"

    def parse_schedule(
        self,
        schedule: str,
        *,
        reference_time: datetime | None = None,
    ) -> ParsedSchedule:
        """Parse the small agent-facing schedule language into persisted fields."""
        if not isinstance(schedule, str) or not schedule.strip():
            raise CronJobValidationError("schedule must be a non-empty string")

        normalized = " ".join(schedule.split())
        reference_utc = _as_utc(reference_time or _utc_now())

        relative_prefix, separator, duration_text = normalized.partition(" ")
        if separator and relative_prefix in {"in", "every"}:
            duration_seconds = _parse_duration_seconds(duration_text)
            try:
                first_fire_at = reference_utc + timedelta(seconds=duration_seconds)
            except OverflowError as error:
                raise CronJobValidationError("duration is too large") from error
            if relative_prefix == "in":
                return ParsedSchedule(
                    schedule_type="once",
                    run_at=first_fire_at.isoformat(),
                )
            return ParsedSchedule(
                schedule_type="interval",
                interval_seconds=duration_seconds,
                interval_anchor_at=reference_utc.isoformat(),
            )

        if len(normalized.split()) == CRON_EXPRESSION_FIELD_COUNT:
            if not croniter.is_valid(normalized):
                raise CronJobValidationError("schedule is not a valid five-field cron expression")
            return ParsedSchedule(schedule_type="cron", cron_expression=normalized)

        if "T" not in normalized and " " not in normalized:
            raise CronJobValidationError(
                "schedule must be an ISO 8601 timestamp, 'in <duration>', "
                "'every <duration>', or a five-field cron expression"
            )
        try:
            parsed = _parse_iso_datetime(
                normalized,
                field_name="schedule",
                allow_naive=True,
            )
        except CronJobValidationError as error:
            raise CronJobValidationError(
                "schedule must be an ISO 8601 timestamp, 'in <duration>', "
                "'every <duration>', or a five-field cron expression"
            ) from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._system_timezone())
        return ParsedSchedule(schedule_type="once", run_at=parsed.astimezone(UTC).isoformat())

    @staticmethod
    def format_schedule(job: CronJob) -> str:
        """Return the canonical agent-facing schedule string for one job."""
        if job.schedule_type == "cron":
            return job.cron_expression or ""
        if job.schedule_type == "interval":
            return (
                f"every {_format_duration(job.interval_seconds)}"
                if job.interval_seconds is not None
                else ""
            )
        return job.run_at or ""

    def next_fire_at(self, job: CronJob, *, reference_time: datetime | None = None) -> str | None:
        """Project the next UTC fire instant from the canonical schedule rules."""
        if job.status != "active" or job.remaining_runs == 0:
            return None
        if job.schedule_type == "once":
            return self._parse_run_at_utc(job).isoformat()
        if job.schedule_type == "interval":
            return self._next_interval_fire_at(job, reference_time=reference_time).isoformat()
        if job.cron_expression is None:
            return None

        timezone = self._system_timezone()
        reference_utc = reference_time or _utc_now()
        if reference_utc.tzinfo is None:
            reference_utc = reference_utc.replace(tzinfo=UTC)
        reference_local = reference_utc.astimezone(timezone)
        next_fire_local = cast(
            datetime,
            croniter(job.cron_expression, reference_local).get_next(datetime),
        )
        if next_fire_local.tzinfo is None:
            next_fire_local = next_fire_local.replace(tzinfo=timezone)
        return next_fire_local.astimezone(UTC).isoformat()

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
            candidate.interval_anchor_at = _utc_now_iso()
        if job.status in TERMINAL_CRON_JOB_STATUSES and candidate.status != job.status:
            raise CronJobValidationError(
                "Completed or missed jobs are immutable history and cannot change status"
            )
        if fields.get("status") == "active" and job.status == "failed":
            candidate.consecutive_failures = 0
            if candidate.remaining_runs == 0:
                candidate.remaining_runs = 1

        changed_fields = sorted(
            field_name
            for field_name in fields
            if getattr(job, field_name) != getattr(candidate, field_name)
        )
        if not changed_fields:
            return self._clone_job(job)

        self._validate_job(candidate)
        self._validate_capacity(candidate, replacing_id=job_id)
        self._jobs[job_id] = candidate
        try:
            self._save_jobs()
        except Exception:
            self._jobs[job_id] = job
            raise
        self._notify_changed()

        if self._started and restart_task:
            self._restart_job_task(candidate)

        if changed_fields == ["status"] and candidate.status in {"active", "paused"}:
            _LOGGER.info(
                "Cron job %s (job=%s)",
                "enabled" if candidate.status == "active" else "disabled",
                job_id,
            )
        else:
            _LOGGER.info(
                "Cron job updated (job=%s fields=%s)",
                job_id,
                ",".join(changed_fields),
            )
        return self._clone_job(candidate)

    def delete_job(self, job_id: str) -> None:
        """Delete one cron job and cancel any active task."""
        self._ensure_jobs_loaded()
        if job_id not in self._jobs:
            raise CronJobNotFoundError(f"Cron job not found: {job_id}")

        removed = self._jobs.pop(job_id)
        try:
            self._save_jobs()
        except Exception:
            self._jobs[job_id] = removed
            raise
        self._notify_changed()
        self._remove_once_fire_claim(job_id)
        self._cancel_job_task(job_id)
        _LOGGER.info("Cron job deleted (job=%s)", job_id)

    def enable_job(self, job_id: str) -> CronJob:
        """Set a cron job status to active."""
        self._ensure_jobs_loaded()
        existing = self._jobs.get(job_id)
        if existing is None:
            raise CronJobNotFoundError(f"Cron job not found: {job_id}")
        if existing.status in {"completed", "missed"}:
            raise CronJobValidationError("Completed or missed jobs cannot be re-enabled")
        return self.update_job(job_id, status="active")

    def disable_job(self, job_id: str) -> CronJob:
        """Set a cron job status to paused."""
        self._ensure_jobs_loaded()
        existing = self._jobs.get(job_id)
        if existing is None:
            raise CronJobNotFoundError(f"Cron job not found: {job_id}")
        if existing.status in {"completed", "missed"}:
            raise CronJobValidationError("Completed or missed jobs cannot be paused")
        return self.update_job(job_id, status="paused")

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
        reference_time = _utc_now()
        needs_save = False
        once_claims_to_remove: list[str] = []

        try:
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
                if job.schedule_type == "once" and self._is_missed_once_job(job, reference_time):
                    _LOGGER.warning(
                        "Marking missed once job as missed (id=%s run_at=%s)",
                        job.id,
                        job.run_at,
                    )
                    job.status = "missed"
                    job.last_outcome = "missed"
                    job.last_error = "Scheduled time passed while vBot was offline"
                    needs_save = True
                    continue

            if needs_save:
                self._save_jobs()
                self._notify_changed()
                for job_id in once_claims_to_remove:
                    self._remove_once_fire_claim(job_id)
        except CronStorageError as error:
            self._degrade_invalid_storage(error)
            return

        for job in self._jobs.values():
            if job.status == "active":
                self._start_job_task(job)

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
        """Load valid jobs while preserving invalid sibling entries verbatim."""
        self._ensure_storage_exists()
        raw_payload = _load_cron_jobs_payload(self._jobs_path)
        self._invalid_job_entries = []
        jobs: dict[str, CronJob] = {}
        for index, item in enumerate(raw_payload):
            diagnostics: list[JsonDiagnostic] = []
            _validate_cron_job_data(diagnostics, index, item)
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
                _LOGGER.warning("Skipping invalid Cron job at $[%d]: %s", index, error)
                self._invalid_job_entries.append(item)
                continue
            if job.id in jobs:
                _LOGGER.warning("Skipping duplicate Cron job id at $[%d]: %s", index, job.id)
                self._invalid_job_entries.append(item)
                continue
            jobs[job.id] = job
        return jobs

    def _save_jobs(self) -> None:
        """Persist cron jobs to <data_root>/cron/jobs.json using atomic replace."""
        self._ensure_storage_exists()
        payload = [
            job.to_dict() for job in sorted(self._jobs.values(), key=lambda item: item.created_at)
        ] + list(self._invalid_job_entries)
        serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            atomic_write_text(self._jobs_path, serialized)
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
        if job.schedule_type == "cron":
            task = asyncio.create_task(self._run_cron_job(job), name=f"cron-job:{job.id}:cron")
        elif job.schedule_type == "interval":
            task = asyncio.create_task(
                self._run_interval_job(job),
                name=f"cron-job:{job.id}:interval",
            )
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

            next_fire_at = self.next_fire_at(current)
            if next_fire_at is None:
                return
            await _sleep_until_utc(
                _parse_iso_datetime(next_fire_at, field_name="next_fire_at", allow_naive=False)
            )

            latest = self._jobs.get(job.id)
            if latest is None or latest.status != "active" or latest.schedule_type != "cron":
                return

            await self._trigger_job_run(latest)

    async def _run_interval_job(self, job: CronJob) -> None:
        """Schedule native fixed intervals from their persisted cadence anchor."""
        while True:
            current = self._jobs.get(job.id)
            if current is None or current.status != "active" or current.schedule_type != "interval":
                return

            next_fire_at = self.next_fire_at(current)
            if next_fire_at is None:
                return
            await _sleep_until_utc(
                _parse_iso_datetime(next_fire_at, field_name="next_fire_at", allow_naive=False)
            )

            latest = self._jobs.get(job.id)
            if latest is None or latest.status != "active" or latest.schedule_type != "interval":
                return

            await self._trigger_job_run(latest)

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
            await _sleep_until_utc(run_at_utc)

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
                current_after_failure = self._jobs.get(latest.id)
                if current_after_failure is None or current_after_failure.remaining_runs == 0:
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
        async with self._run_slots:
            latest = self._jobs.get(job.id)
            if latest is None or latest.status != "active":
                return False

            latest.last_attempt_at = _utc_now_iso()
            latest.last_error = None
            self._jobs[latest.id] = latest
            self._save_jobs_after_fire(latest.id)
            _LOGGER.info(
                "Cron job fired (job=%s agent=%s session=%s%s)",
                latest.id,
                latest.agent_id,
                latest.session_id,
                f" project={latest.project_id}" if latest.project_id else "",
            )
            try:
                run = await self._trigger_service.trigger_run(
                    latest.agent_id,
                    latest.prompt,
                    latest.session_id,
                    project_id=latest.project_id,
                    run_kind=RunKind.CRON,
                    contributes_to_agent_activity=False,
                )
                latest = self._jobs.get(job.id)
                if latest is None:
                    return False
                latest.last_fired_at = _utc_now_iso()
                run_id = getattr(run, "id", None)
                latest.last_run_id = run_id if isinstance(run_id, str) else None
                if latest.remaining_runs is not None:
                    latest.remaining_runs = max(latest.remaining_runs - 1, 0)
                self._jobs[latest.id] = latest
                while not self._save_jobs_after_fire(latest.id):
                    await asyncio.sleep(_ONCE_RETRY_DELAY_SECONDS)

                wait_for_run = getattr(run, "wait", None)
                if callable(wait_for_run):
                    await wait_for_run()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._record_run_failure(job.id, error)
                self._finalize_exhausted_job(job.id)
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
            latest.last_completed_at = _utc_now_iso()
            latest.last_outcome = "success"
            latest.last_error = None
            latest.consecutive_failures = 0
            self._jobs[latest.id] = latest
            self._save_jobs_after_fire(latest.id)
            self._finalize_exhausted_job(latest.id)
            return True

    def _record_run_failure(self, job_id: str, error: BaseException) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.last_completed_at = _utc_now_iso()
        job.last_outcome = "cancelled" if type(error).__name__ == "RunCancelledError" else "failed"
        job.last_error = _truncate_error(str(error) or type(error).__name__)
        job.consecutive_failures += 1
        if job.schedule_type != "once" and (
            job.consecutive_failures >= MAX_CONSECUTIVE_CRON_FAILURES
        ):
            job.status = "failed"
        self._jobs[job_id] = job
        self._save_jobs_after_fire(job_id)

    def _finalize_exhausted_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.status != "active" or job.remaining_runs != 0:
            return
        job.status = "completed" if job.last_outcome == "success" else "failed"
        self._jobs[job_id] = job
        self._save_jobs_after_fire(job_id)

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
        self._record_run_failure(job_id, error)

    def _validate_job(self, job: CronJob, *, validate_references: bool = True) -> None:
        if not isinstance(job.id, str) or not job.id:
            raise CronJobValidationError("id must be a non-empty string")

        if not isinstance(job.agent_id, str) or not is_valid_agent_id(job.agent_id.strip()):
            raise CronJobValidationError(
                "agent_id must be 1-64 characters using only letters, numbers, "
                "hyphen, or underscore"
            )
        job.agent_id = job.agent_id.strip()

        if not isinstance(job.name, str) or not job.name.strip():
            raise CronJobValidationError("name must be a non-empty string")
        job.name = job.name.strip()

        if not isinstance(job.prompt, str) or not job.prompt.strip():
            raise CronJobValidationError("prompt must be a non-empty string")
        job.prompt = job.prompt.strip()

        if job.schedule_type not in _ALLOWED_SCHEDULE_TYPES:
            raise CronJobValidationError("schedule_type must be 'cron', 'interval', or 'once'")

        if job.status not in _ALLOWED_STATUSES:
            raise CronJobValidationError(
                "status must be active, paused, completed, failed, or missed"
            )

        if job.session_id is not None and not isinstance(job.session_id, str):
            raise CronJobValidationError("session_id must be a string when provided")
        if job.session_id is not None:
            normalized_session_id = job.session_id.strip()
            job.session_id = normalized_session_id or None

        if job.project_id is not None:
            if not isinstance(job.project_id, str):
                raise CronJobValidationError("project_id must be a string when provided")
            normalized_project_id = job.project_id.strip()
            if normalized_project_id and not is_valid_project_id(normalized_project_id):
                raise CronJobValidationError(
                    "project_id must be 1-64 characters using only letters, numbers, "
                    "hyphen, or underscore"
                )
            job.project_id = normalized_project_id or None

        self._parse_utc_timestamp(job.created_at, field_name="created_at")
        if job.last_fired_at is not None:
            self._parse_utc_timestamp(job.last_fired_at, field_name="last_fired_at")
        for field_name in ("last_attempt_at", "last_completed_at"):
            value = getattr(job, field_name)
            if value is not None:
                self._parse_utc_timestamp(value, field_name=field_name)
        if job.last_outcome is not None and job.last_outcome not in _ALLOWED_RUN_OUTCOMES:
            raise CronJobValidationError(
                "last_outcome must be success, failed, cancelled, missed, unknown, or null"
            )
        if (
            isinstance(job.consecutive_failures, bool)
            or not isinstance(job.consecutive_failures, int)
            or job.consecutive_failures < 0
        ):
            raise CronJobValidationError("consecutive_failures must be a non-negative integer")
        if job.remaining_runs is not None and (
            isinstance(job.remaining_runs, bool)
            or not isinstance(job.remaining_runs, int)
            or job.remaining_runs < 0
        ):
            raise CronJobValidationError("remaining_runs must be a non-negative integer or null")
        if job.last_error is not None:
            if not isinstance(job.last_error, str):
                raise CronJobValidationError("last_error must be a string when provided")
            job.last_error = _truncate_error(job.last_error)
        if job.last_run_id is not None and not isinstance(job.last_run_id, str):
            raise CronJobValidationError("last_run_id must be a string when provided")

        if validate_references:
            self._validate_references(job)

        if job.schedule_type == "cron":
            if not isinstance(job.cron_expression, str) or not job.cron_expression.strip():
                raise CronJobValidationError("cron_expression is required for cron jobs")
            normalized_expression = job.cron_expression.strip()
            if len(normalized_expression.split()) != CRON_EXPRESSION_FIELD_COUNT:
                raise CronJobValidationError(
                    f"cron_expression must contain exactly {CRON_EXPRESSION_FIELD_COUNT} fields "
                    "(minute hour day-of-month month day-of-week)"
                )
            if not croniter.is_valid(normalized_expression):
                raise CronJobValidationError("cron_expression is invalid")
            job.cron_expression = normalized_expression
            job.interval_seconds = None
            job.interval_anchor_at = None
            job.run_at = None
            return

        if job.schedule_type == "interval":
            if (
                isinstance(job.interval_seconds, bool)
                or not isinstance(job.interval_seconds, int)
                or job.interval_seconds < MIN_INTERVAL_SECONDS
                or job.interval_seconds % MIN_INTERVAL_SECONDS != 0
            ):
                raise CronJobValidationError(
                    "interval_seconds must be a whole number of minutes "
                    f"({MIN_INTERVAL_SECONDS} seconds or more)"
                )
            if not isinstance(job.interval_anchor_at, str) or not job.interval_anchor_at.strip():
                raise CronJobValidationError("interval_anchor_at is required for interval jobs")
            anchor_utc = self._parse_utc_timestamp(
                job.interval_anchor_at.strip(),
                field_name="interval_anchor_at",
            )
            job.interval_anchor_at = anchor_utc.isoformat()
            try:
                _first_fire_at = anchor_utc + timedelta(seconds=job.interval_seconds)
            except OverflowError as error:
                raise CronJobValidationError("interval_seconds is too large") from error
            job.cron_expression = None
            job.run_at = None
            return

        if not isinstance(job.run_at, str) or not job.run_at.strip():
            raise CronJobValidationError("run_at is required for once jobs")
        if job.remaining_runs is None:
            job.remaining_runs = 1
        if job.remaining_runs not in {0, 1}:
            raise CronJobValidationError("once jobs require repeat to be 1")
        job.run_at = job.run_at.strip()
        job.run_at = self._parse_run_at_utc(job).isoformat()
        job.cron_expression = None
        job.interval_seconds = None
        job.interval_anchor_at = None

    def _validate_references(self, job: CronJob) -> None:
        if self._agent_resolver is not None:
            try:
                self._agent_resolver.resolve_agent(job.project_id, job.agent_id)
            except Exception as error:
                target = f"{job.agent_id}@{job.project_id}" if job.project_id else job.agent_id
                raise CronJobValidationError(f"Cron target does not exist: {target}") from error
        if (
            job.session_id is not None
            and self._sessions is not None
            and not self._sessions.exists(job.agent_id, job.session_id, job.project_id)
        ):
            raise CronJobValidationError(
                f"Session does not exist for cron target {job.agent_id}: {job.session_id}"
            )

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

    def _parse_run_at_utc(self, job: CronJob) -> datetime:
        if job.run_at is None:
            raise CronJobValidationError("run_at is required for once jobs")

        parsed = _parse_iso_datetime(job.run_at, field_name="run_at", allow_naive=True)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._system_timezone())
        return parsed.astimezone(UTC)

    def _next_interval_fire_at(
        self,
        job: CronJob,
        *,
        reference_time: datetime | None = None,
    ) -> datetime:
        if job.interval_seconds is None or job.interval_anchor_at is None:
            raise CronJobValidationError(
                "interval_seconds and interval_anchor_at are required for interval jobs"
            )
        reference_utc = _as_utc(reference_time or _utc_now())
        anchor_utc = self._parse_utc_timestamp(
            job.interval_anchor_at,
            field_name="interval_anchor_at",
        )
        if reference_utc < anchor_utc:
            return anchor_utc
        elapsed_seconds = (reference_utc - anchor_utc).total_seconds()
        elapsed_intervals = int(elapsed_seconds // job.interval_seconds)
        return anchor_utc + timedelta(seconds=(elapsed_intervals + 1) * job.interval_seconds)

    def _parse_utc_timestamp(self, value: str, *, field_name: str) -> datetime:
        parsed = _parse_iso_datetime(value, field_name=field_name, allow_naive=False)
        if parsed.utcoffset() != UTC.utcoffset(parsed):
            raise CronJobValidationError(f"{field_name} must be a UTC timestamp")
        return parsed

    def _system_timezone(self) -> tzinfo:
        try:
            return get_localzone()
        except Exception as error:
            _LOGGER.warning("Could not resolve system timezone: %s", error)
            return UTC

    def _is_missed_once_job(self, job: CronJob, reference_time_utc: datetime) -> bool:
        if job.schedule_type != "once":
            return False
        return self._parse_run_at_utc(job) < reference_time_utc

    def _write_once_fire_claim(self, job: CronJob, claimed_at: str) -> None:
        self._ensure_storage_exists()
        claim_path = self._once_fire_claim_path(job.id)
        payload = {
            "job_id": job.id,
            "claimed_at": claimed_at,
            "run_at": job.run_at,
        }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            atomic_write_text(claim_path, serialized)
        except OSError as error:
            raise CronStorageError(f"Cannot write {claim_path}: {error}") from error

    def _read_once_fire_claimed_at(self, job_id: str) -> str | None:
        claim_path = self._once_fire_claim_path(job_id)
        if not claim_path.exists():
            return None

        try:
            payload = json.loads(claim_path.read_text(encoding="utf-8"))
        except UnicodeError as error:
            raise CronStorageError(
                f"Invalid UTF-8 in once fire claim {claim_path}: {error}"
            ) from error
        except OSError as error:
            raise CronStorageError(f"Cannot read {claim_path}: {error}") from error
        except json.JSONDecodeError as error:
            raise CronStorageError(f"Invalid once fire claim {claim_path}: {error}") from error

        if not isinstance(payload, dict) or payload.get("job_id") != job_id:
            raise CronStorageError(f"Invalid once fire claim {claim_path}: job_id mismatch")

        claimed_at = payload.get("claimed_at")
        if not isinstance(claimed_at, str):
            raise CronStorageError(f"Invalid once fire claim {claim_path}: claimed_at is required")
        try:
            self._parse_utc_timestamp(claimed_at, field_name="claimed_at")
        except CronJobValidationError as error:
            raise CronStorageError(f"Invalid once fire claim {claim_path}: {error}") from error
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


def _once_retry_delay(attempt: int) -> float:
    """Backoff delay in seconds for the Nth (1-based) failed once-job fire."""
    exponent = max(attempt - 1, 0)
    delay = _ONCE_RETRY_DELAY_SECONDS * (_ONCE_RETRY_BACKOFF_FACTOR**exponent)
    return min(delay, _ONCE_RETRY_MAX_DELAY_SECONDS)


def _derive_legacy_cron_job_name(prompt: object) -> str:
    """Derive and persist the same stable fallback used for unnamed new jobs."""
    for line in str(prompt).splitlines():
        collapsed_line = " ".join(line.split())
        if not collapsed_line:
            continue
        without_markdown = _MARKDOWN_PREFIX_PATTERN.sub("", collapsed_line).strip()
        if without_markdown:
            return without_markdown[:_CRON_JOB_NAME_MAX_LENGTH]
    return "Scheduled Run"


def _parse_duration_seconds(value: str) -> int:
    match = _DURATION_PATTERN.fullmatch(value)
    if match is None:
        raise CronJobValidationError(
            "duration must be a positive whole number followed by m, h, or d"
        )
    amount = int(match.group("amount"))
    return amount * _DURATION_UNIT_SECONDS[match.group("unit")]


def _format_duration(seconds: int) -> str:
    for unit in ("d", "h", "m"):
        unit_seconds = _DURATION_UNIT_SECONDS[unit]
        if seconds % unit_seconds == 0:
            return f"{seconds // unit_seconds}{unit}"
    return f"{seconds // _DURATION_UNIT_SECONDS['m']}m"


def _truncate_error(message: str) -> str:
    normalized = " ".join(message.split())
    if len(normalized) <= _LAST_ERROR_MAX_CHARS:
        return normalized
    return f"{normalized[: _LAST_ERROR_MAX_CHARS - 1]}…"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _sleep_until_utc(target_utc: datetime) -> None:
    """Sleep until a UTC wall-clock instant, re-reading the clock each bounded nap."""
    while True:
        remaining_seconds = (target_utc - _utc_now()).total_seconds()
        if remaining_seconds <= 0:
            return
        await asyncio.sleep(min(remaining_seconds, _WALL_CLOCK_RECHECK_SECONDS))


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
