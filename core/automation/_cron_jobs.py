"""Cron job records, raw storage validation and field normalization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadDateError, croniter  # type: ignore[import-untyped]
from tzlocal import get_localzone

from core.automation._cron_timing import _utc_now_iso
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
from core.settings import is_valid_agent_id, is_valid_project_id
from core.utils.errors import VBotError
from core.utils.logging import get_logger

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

MAX_PROJECTED_OCCURRENCES_PER_JOB = 500

TERMINAL_CRON_JOB_STATUSES = frozenset(("completed", "missed"))

_LAST_ERROR_MAX_CHARS = 500

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


def _cron_is_schedulable(expression: str, reference: datetime) -> bool:
    """Require both valid syntax and a reachable fire within croniter's search horizon."""
    if not croniter.is_valid(expression):
        return False
    try:
        croniter(expression, reference).get_next(datetime)
    except CroniterBadDateError:
        return False
    return True


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


@dataclass(frozen=True, slots=True)
class CronOccurrence:
    """One projected fire instant of an active job, for read-only display."""

    job_id: str
    name: str
    fire_at_utc: datetime
    schedule_type: ScheduleType


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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _resolve_timezone(value: str | ZoneInfo | None) -> ZoneInfo:
    if isinstance(value, ZoneInfo):
        return value
    if isinstance(value, str):
        try:
            return ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise CronJobValidationError(
                f"timezone is not a known IANA timezone: {value}"
            ) from error
    try:
        local = get_localzone()
        return local if isinstance(local, ZoneInfo) else ZoneInfo(str(local))
    except Exception as error:
        get_logger("automation.cron").warning("Could not resolve system timezone: %s", error)
        return ZoneInfo("UTC")


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


def _parse_utc_timestamp(value: str, *, field_name: str) -> datetime:
    parsed = _parse_iso_datetime(value, field_name=field_name, allow_naive=False)
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise CronJobValidationError(f"{field_name} must be a UTC timestamp")
    return parsed


def normalize_job_fields(job: CronJob) -> None:
    if not isinstance(job.id, str) or not job.id:
        raise CronJobValidationError("id must be a non-empty string")

    if not isinstance(job.agent_id, str) or not is_valid_agent_id(job.agent_id.strip()):
        raise CronJobValidationError(
            "agent_id must be 1-64 characters using only letters, numbers, hyphen, or underscore"
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
        raise CronJobValidationError("status must be active, paused, completed, failed, or missed")

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

    _parse_utc_timestamp(job.created_at, field_name="created_at")
    if job.last_fired_at is not None:
        _parse_utc_timestamp(job.last_fired_at, field_name="last_fired_at")
    for field_name in ("last_attempt_at", "last_completed_at"):
        value = getattr(job, field_name)
        if value is not None:
            _parse_utc_timestamp(value, field_name=field_name)
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
