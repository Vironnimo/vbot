"""Cron job records, raw storage validation and field normalization."""

from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadDateError, croniter  # type: ignore[import-untyped]
from tzlocal import get_localzone

from core.automation._cron_notification_model import NOTIFICATION_FIELDS, CronNotification
from core.automation._cron_timing import _utc_now_iso
from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonObject,
    JsonValidationReport,
    add_error,
    load_validated_json_file,
    validate_allowed_string,
    validate_json_file,
    validate_non_empty_string,
    validate_optional_allowed_string,
    validate_optional_string,
    warn_unknown_keys,
)
from core.json_documents import (
    JsonDocumentFormat,
    json_document,
    json_list,
    json_object,
    strip_unknown_fields,
    validate_collection_root,
)
from core.permissions import PermissionCeiling, PermissionRulesError
from core.projects import ResolutionAgentNotFoundError, ResolutionProjectNotFoundError
from core.settings import is_valid_agent_id, is_valid_project_id
from core.utils.errors import VBotError
from core.utils.logging import get_logger

ScheduleType = Literal["cron", "interval", "once", "event", "webhook"]

EventEdge = Literal["start", "end"]

CronJobStatus = Literal["active", "paused", "completed", "failed", "missed"]

CronRunOutcome = Literal["success", "failed", "cancelled", "missed", "unknown"]

DeliveryKind = Literal["none", "chat", "app"]

WatchKind = Literal["command", "url"]

_ALLOWED_SCHEDULE_TYPES = frozenset(("cron", "interval", "once", "event", "webhook"))

# The schedule types a watch applies to: a check needs a time to repeat at.
WATCH_SCHEDULE_TYPES = frozenset(("cron", "interval"))

WATCH_COMMAND_MAX_CHARS = 4000

WATCH_URL_MAX_CHARS = 2048

_ALLOWED_EVENT_EDGES = frozenset(("start", "end"))

# How far an event job may run before or after its event edge.
MAX_EVENT_OFFSET_MINUTES = 31 * 24 * 60

_ALLOWED_STATUSES = frozenset(("active", "paused", "completed", "failed", "missed"))

_ALLOWED_RUN_OUTCOMES = frozenset(("success", "failed", "cancelled", "missed", "unknown"))

_ALLOWED_DELIVERY_KINDS = frozenset(("none", "chat", "app"))

# How much of a Run's final answer a job keeps for its next Run.
LAST_RESULT_MAX_CHARS = 2000

_RESTART_FIELDS = frozenset(
    (
        "schedule_type",
        "cron_expression",
        "interval_seconds",
        "interval_anchor_at",
        "run_at",
        "event_id",
        "event_edge",
        "event_offset_minutes",
        "remaining_runs",
        "status",
        "watch",
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
        "event_id",
        "event_edge",
        "event_offset_minutes",
        "remaining_runs",
        "session_id",
        "status",
        "project_id",
        "delivery",
        "watch",
        "permission_ceiling",
    )
)

_CRON_JOB_FIELDS = _MUTABLE_FIELDS | {
    "consecutive_failures",
    "covered_until",
    "event_progress",
    "created_at",
    "id",
    "last_attempt_at",
    "last_changed_at",
    "last_check_error",
    "last_checked_at",
    "last_completed_at",
    "last_error",
    "last_fired_at",
    "last_outcome",
    "last_result",
    "last_run_id",
    "last_run_silent",
    "notifications",
    "last_delivered_at",
}

CRON_JOBS_FORMAT_VERSION = 1

_EVENT_PROGRESS_FIELDS = frozenset(("due_at", "occurrence_ids"))
_DELIVERY_FIELDS = frozenset(("kind", "channel_id", "platform_target", "thread_id"))
_LAST_RESULT_FIELDS = frozenset(("text", "at", "run_id"))
_WATCH_FIELDS = frozenset(("command", "url"))
CRON_JOB_SHAPE = json_object(
    _CRON_JOB_FIELDS,
    {
        "event_progress": json_object(_EVENT_PROGRESS_FIELDS),
        "delivery": json_object(_DELIVERY_FIELDS),
        "last_result": json_object(_LAST_RESULT_FIELDS),
        "watch": json_object(_WATCH_FIELDS),
        "notifications": json_list(json_object(NOTIFICATION_FIELDS), key="id"),
    },
)

CRON_JOBS_SHAPE = json_document({"jobs"}, {"jobs": json_list(CRON_JOB_SHAPE, key="id")})


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


class CronJobInPastError(CronJobValidationError):
    """Raised when a one-time job would be created or armed for a past instant."""


class CronTargetError(CronJobValidationError):
    """The Cron job's target Agent cannot be resolved into a runnable Agent.

    Accessors branch on this type to give target guidance instead of schedule
    guidance.
    """


class CronTargetAgentNotFoundError(CronTargetError, ResolutionAgentNotFoundError):
    """The Cron target's Agent does not exist.

    Both a Cron validation error, for Cron's own catchers, and a resolver
    not-found error, so accessors report the missing Agent precisely.
    """


class CronTargetProjectNotFoundError(CronTargetError, ResolutionProjectNotFoundError):
    """The Cron target's Project does not exist.

    Both a Cron validation error, for Cron's own catchers, and a resolver
    not-found error, so accessors report the missing Project precisely.
    """


class CronTargetUnavailableError(CronTargetError):
    """The Cron target exists but cannot run (for example, no usable Model)."""


class CronStorageError(CronServiceError):
    """Raised when cron storage cannot be read or written."""


def _target_validation_error(job: CronJob, error: Exception) -> CronTargetError:
    """Describe why a Cron job's target Agent cannot be resolved.

    Only a missing Agent or Project says the target does not exist; a target
    that exists but cannot run (for example, no usable Model) keeps the
    resolver's reason.
    """
    target = f"{job.agent_id}@{job.project_id}" if job.project_id else job.agent_id
    if isinstance(error, ResolutionAgentNotFoundError):
        return CronTargetAgentNotFoundError(f"Cron target does not exist: {target}")
    if isinstance(error, ResolutionProjectNotFoundError):
        return CronTargetProjectNotFoundError(f"Cron target does not exist: {target}")
    return CronTargetUnavailableError(f"Cron target {target} cannot run: {error}")


def validate_cron_jobs_file(jobs_path: str | Path) -> JsonValidationReport:
    """Validate persisted ``cron/jobs.json`` without consuming it."""
    return validate_json_file(jobs_path, validate_cron_jobs_data, missing_ok=True)


def load_validated_cron_jobs_json(jobs_path: str | Path) -> list[JsonObject]:
    """Load the modeled fields of schema-valid Cron jobs; a missing file has none."""
    try:
        data = load_validated_json_file(jobs_path, validate_cron_jobs_data, missing_ok=True)
    except JsonConfigValidationError as error:
        raise CronStorageError(str(error)) from error
    if data is None:
        return []
    return cast("list[JsonObject]", strip_unknown_fields(data, CRON_JOBS_SHAPE)["jobs"])


def _load_cron_jobs_payload(jobs_path: str | Path) -> list[Any]:
    """Load the job entries without letting one bad job reject its siblings."""
    try:
        data = load_validated_json_file(jobs_path, _validate_cron_jobs_root, missing_ok=True)
    except JsonConfigValidationError as error:
        raise CronStorageError(str(error)) from error
    return [] if data is None else list(data["jobs"])


def _validate_cron_jobs_root(data: Any) -> list[JsonDiagnostic]:
    diagnostics: list[JsonDiagnostic] = []
    validate_collection_root(
        diagnostics,
        data,
        version=CRON_JOBS_FORMAT_VERSION,
        shape=CRON_JOBS_SHAPE,
        collection="jobs",
        label="cron jobs field",
    )
    return diagnostics


def validate_cron_jobs_data(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded raw ``cron/jobs.json`` document."""
    diagnostics: list[JsonDiagnostic] = []
    entries = validate_collection_root(
        diagnostics,
        data,
        version=CRON_JOBS_FORMAT_VERSION,
        shape=CRON_JOBS_SHAPE,
        collection="jobs",
        label="cron jobs field",
    )
    for index, item in enumerate(entries or []):
        _validate_cron_job_data(diagnostics, f"$.jobs[{index}]", item)
    return diagnostics


# A Cron job file keeps invalid entries verbatim, so only an unreadable document
# root refuses a write.
CRON_JOBS_FORMAT = JsonDocumentFormat(
    name="Cron jobs",
    version=CRON_JOBS_FORMAT_VERSION,
    shape=CRON_JOBS_SHAPE,
    validate=_validate_cron_jobs_root,
    sort_keys=True,
)


def _validate_cron_job_data(diagnostics: list[JsonDiagnostic], item_path: str, item: Any) -> None:
    if not isinstance(item, dict):
        add_error(diagnostics, item_path, "Expected a JSON object")
        return
    warn_unknown_keys(diagnostics, item_path, item, CRON_JOB_SHAPE.fields, "cron job field")
    validate_non_empty_string(diagnostics, f"{item_path}.id", item.get("id"), required=True)
    _validate_cron_agent_id(diagnostics, f"{item_path}.agent_id", item.get("agent_id"))
    validate_non_empty_string(diagnostics, f"{item_path}.name", item.get("name"), required=True)
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
        "event_id",
        "session_id",
        "project_id",
        "last_fired_at",
        "last_attempt_at",
        "last_completed_at",
        "last_error",
        "last_run_id",
        "covered_until",
        "last_checked_at",
        "last_changed_at",
        "last_check_error",
    ):
        validate_optional_string(
            diagnostics,
            f"{item_path}.{field_name}",
            item.get(field_name),
        )
    validate_optional_allowed_string(
        diagnostics, f"{item_path}.event_edge", item.get("event_edge"), _ALLOWED_EVENT_EDGES
    )
    event_offset = item.get("event_offset_minutes")
    if event_offset is not None and (
        isinstance(event_offset, bool)
        or not isinstance(event_offset, int)
        or abs(event_offset) > MAX_EVENT_OFFSET_MINUTES
    ):
        add_error(
            diagnostics,
            f"{item_path}.event_offset_minutes",
            f"must be a whole number of minutes from -{MAX_EVENT_OFFSET_MINUTES} "
            f"to {MAX_EVENT_OFFSET_MINUTES}",
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
    nested: tuple[tuple[str, Callable[[Any], object], frozenset[str], str], ...] = (
        ("event_progress", EventProgress.from_dict, _EVENT_PROGRESS_FIELDS, "event progress"),
        ("delivery", CronDelivery.from_dict, _DELIVERY_FIELDS, "delivery"),
        ("last_result", CronResult.from_dict, _LAST_RESULT_FIELDS, "last result"),
        ("watch", CronWatch.from_dict, _WATCH_FIELDS, "watch"),
    )
    for field_name, parse, known, label in nested:
        value = item.get(field_name)
        if value is None:
            continue
        try:
            parse(value)
        except CronJobValidationError as error:
            add_error(diagnostics, f"{item_path}.{field_name}", str(error))
        if isinstance(value, dict):
            warn_unknown_keys(
                diagnostics, f"{item_path}.{field_name}", value, known, f"{label} field"
            )
    notifications = item.get("notifications", [])
    if not isinstance(notifications, list):
        add_error(diagnostics, f"{item_path}.notifications", "must be a list")
    else:
        seen: set[str] = set()
        for index, raw in enumerate(notifications):
            try:
                notification = CronNotification.from_dict(raw)
                if notification.id in seen:
                    raise ValueError("duplicate notification id")
                seen.add(notification.id)
            except ValueError as error:
                add_error(diagnostics, f"{item_path}.notifications[{index}]", str(error))
    validate_optional_string(
        diagnostics, f"{item_path}.last_delivered_at", item.get("last_delivered_at")
    )
    silent = item.get("last_run_silent")
    if silent is not None and not isinstance(silent, bool):
        add_error(diagnostics, f"{item_path}.last_run_silent", "must be a boolean")
    ceiling = item.get("permission_ceiling")
    if ceiling is not None:
        try:
            PermissionCeiling.from_ceiling_dict(ceiling)
        except PermissionRulesError as error:
            add_error(diagnostics, f"{item_path}.permission_ceiling", str(error))


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
    event_id: str | None = None
    event_edge: EventEdge | None = None
    event_offset_minutes: int | None = None

    def as_job_fields(self) -> dict[str, str | int | None]:
        """Return all persisted schedule fields, clearing incompatible kinds."""
        return {
            "schedule_type": self.schedule_type,
            "cron_expression": self.cron_expression,
            "interval_seconds": self.interval_seconds,
            "interval_anchor_at": self.interval_anchor_at,
            "run_at": self.run_at,
            "event_id": self.event_id,
            "event_edge": self.event_edge,
            "event_offset_minutes": self.event_offset_minutes,
        }


@dataclass(frozen=True, slots=True)
class CronOccurrence:
    """One projected fire instant of an active job, for read-only display.

    An event job's fire names the event and the occurrence it belongs to.
    """

    job_id: str
    name: str
    fire_at_utc: datetime
    schedule_type: ScheduleType
    event_id: str | None = None
    occurrence_id: str | None = None


@dataclass(slots=True)
class CronJob:
    """Persisted cron job record.

    ``project_id`` is the project dimension the job fires into: ``None`` is a
    global/identity-agent target (today's behavior, byte-identical), a set value
    scopes the fired Session and Run to that project's anchor. It is the
    structured half of the outside ``agent@projekt`` address form (parsed once at
    the RPC edge), never an ``@`` string stored in ``agent_id``.

    ``covered_until`` marks the instant through which no fire is owed even
    though no Run started: activation or a schedule change. Fires due after it,
    after ``created_at`` and after the last attempt and completion are owed.

    An ``event`` job runs at every occurrence of the calendar event
    ``event_id``, ``event_offset_minutes`` before (negative) or after its
    ``event_edge``. Its occurrences are owed one by one, so its
    ``covered_until`` is the due time of the last occurrence it fired for (or
    its activation), not its last attempt. Optional ``event_progress`` names
    consumed occurrences at that exact due time; without matching progress,
    coverage includes every occurrence at its boundary as in earlier files.
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
    event_id: str | None = None
    event_edge: EventEdge | None = None
    event_offset_minutes: int | None = None
    last_attempt_at: str | None = None
    last_completed_at: str | None = None
    last_run_id: str | None = None
    last_outcome: CronRunOutcome | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    covered_until: str | None = None
    event_progress: EventProgress | None = None
    # Where each Run's final answer goes; None delivers it nowhere.
    delivery: CronDelivery | None = None
    # The latest final answer that was not [SILENT], for the next Run's context.
    last_result: CronResult | None = None
    # Whether the latest Run succeeded with a [SILENT] (or empty) answer.
    last_run_silent: bool = False
    # What the job checks at each due time; its Run starts only when the output changed.
    watch: CronWatch | None = None
    # When the watch was last checked, whatever the result.
    last_checked_at: str | None = None
    # When the watched output last changed and a Run handled the change.
    last_changed_at: str | None = None
    # Why the latest check failed; the next successful check clears it.
    last_check_error: str | None = None
    notifications: tuple[CronNotification, ...] = ()
    last_delivered_at: str | None = None
    # The boundary of the restricted Session that created or last changed the job
    # through the ``cron`` Tool, in its stored ceiling form; every fire applies it.
    # ``None``: the job was set up by the user or a full-access Session.
    permission_ceiling: dict[str, Any] | None = None

    def ceiling(self) -> PermissionCeiling | None:
        """The stored creator boundary every fire of this job runs under."""
        if self.permission_ceiling is None:
            return None
        return PermissionCeiling.from_ceiling_dict(self.permission_ceiling)

    def to_dict(self) -> dict[str, Any]:
        """Serialize one CronJob to a JSON-compatible payload.

        Delivery, result and watch fields appear only when set, so jobs without
        them keep the format earlier versions wrote.
        """
        data: dict[str, Any] = {
            "id": self.id,
            "agent_id": self.agent_id,
            "name": self.name,
            "prompt": self.prompt,
            "schedule_type": self.schedule_type,
            "cron_expression": self.cron_expression,
            "interval_seconds": self.interval_seconds,
            "interval_anchor_at": self.interval_anchor_at,
            "run_at": self.run_at,
            "event_id": self.event_id,
            "event_edge": self.event_edge,
            "event_offset_minutes": self.event_offset_minutes,
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
            "covered_until": self.covered_until,
            "event_progress": (
                None if self.event_progress is None else self.event_progress.to_dict()
            ),
        }
        if self.delivery is not None:
            data["delivery"] = self.delivery.to_dict()
        if self.last_result is not None:
            data["last_result"] = self.last_result.to_dict()
        if self.last_run_silent:
            data["last_run_silent"] = True
        if self.watch is not None:
            data["watch"] = self.watch.to_dict()
        for field_name in ("last_checked_at", "last_changed_at", "last_check_error"):
            value = getattr(self, field_name)
            if value is not None:
                data[field_name] = value
        if self.notifications:
            data["notifications"] = [item.to_dict() for item in self.notifications]
        if self.last_delivered_at is not None:
            data["last_delivered_at"] = self.last_delivered_at
        if self.permission_ceiling is not None:
            data["permission_ceiling"] = deepcopy(self.permission_ceiling)
        return data

    def delivery_progress(self) -> dict[str, Any]:
        return {
            "waiting": len(self.notifications),
            "uncertain": sum(item.state in {"sending", "uncertain"} for item in self.notifications),
            "error": next((item.error for item in self.notifications if item.error), None),
            "last_delivered_at": self.last_delivered_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CronJob:
        """Create one CronJob from persisted JSON data."""
        return cls(
            id=str(payload["id"]),
            agent_id=str(payload["agent_id"]),
            name=str(payload["name"]),
            prompt=str(payload["prompt"]),
            schedule_type=payload["schedule_type"],
            cron_expression=payload.get("cron_expression"),
            interval_seconds=payload.get("interval_seconds"),
            interval_anchor_at=payload.get("interval_anchor_at"),
            run_at=payload.get("run_at"),
            event_id=payload.get("event_id"),
            event_edge=payload.get("event_edge"),
            event_offset_minutes=payload.get("event_offset_minutes"),
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
            covered_until=payload.get("covered_until"),
            event_progress=(
                None
                if payload.get("event_progress") is None
                else EventProgress.from_dict(payload["event_progress"])
            ),
            delivery=(
                None
                if payload.get("delivery") is None
                else CronDelivery.from_dict(payload["delivery"]).stored()
            ),
            last_result=(
                None
                if payload.get("last_result") is None
                else CronResult.from_dict(payload["last_result"])
            ),
            last_run_silent=payload.get("last_run_silent") is True,
            watch=None if payload.get("watch") is None else CronWatch.from_dict(payload["watch"]),
            last_checked_at=payload.get("last_checked_at"),
            last_changed_at=payload.get("last_changed_at"),
            last_check_error=payload.get("last_check_error"),
            notifications=tuple(
                CronNotification.from_dict(item) for item in payload.get("notifications", [])
            ),
            last_delivered_at=payload.get("last_delivered_at"),
            permission_ceiling=(
                None
                if payload.get("permission_ceiling") is None
                else deepcopy(dict(payload["permission_ceiling"]))
            ),
        )


@dataclass(frozen=True, slots=True)
class EventProgress:
    """Consumed occurrence ids at one event job's coverage boundary.

    The timestamp binds the ids to their due time even if an older writer
    preserves this unknown field while advancing ``covered_until``.
    """

    due_at: str
    occurrence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"due_at": self.due_at, "occurrence_ids": list(self.occurrence_ids)}

    @classmethod
    def from_dict(cls, value: Any) -> EventProgress:
        if not isinstance(value, dict):
            raise CronJobValidationError("event_progress must be an object or null")
        due_at = value.get("due_at")
        if not isinstance(due_at, str):
            raise CronJobValidationError("event_progress.due_at must be a UTC timestamp")
        _parse_utc_timestamp(due_at, field_name="event_progress.due_at")
        ids = value.get("occurrence_ids")
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(item, str) or not item for item in ids)
        ):
            raise CronJobValidationError(
                "event_progress.occurrence_ids must be a non-empty list of occurrence ids"
            )
        return cls(due_at=due_at, occurrence_ids=tuple(dict.fromkeys(ids)))


@dataclass(frozen=True, slots=True)
class CronDelivery:
    """Where a job's Run answers and failure alerts go.

    ``chat`` sends them to one chat of a Channel of the job's Identity Agent;
    ``app`` shows the Run's Session to the user in vBot with a notification;
    ``none`` delivers nothing (a job stores it as no delivery at all).
    """

    kind: DeliveryKind
    channel_id: str | None = None
    platform_target: str | None = None
    thread_id: str | None = None

    def stored(self) -> CronDelivery | None:
        """The value a job keeps: ``none`` is stored as no delivery."""
        return None if self.kind == "none" else self

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind}
        for field_name in ("channel_id", "platform_target", "thread_id"):
            value = getattr(self, field_name)
            if value is not None:
                data[field_name] = value
        return data

    @classmethod
    def from_dict(cls, value: Any) -> CronDelivery:
        if not isinstance(value, dict):
            raise CronJobValidationError("delivery must be an object or null")
        kind = value.get("kind")
        if kind not in _ALLOWED_DELIVERY_KINDS:
            raise CronJobValidationError("delivery.kind must be none, chat or app")
        if kind != "chat":
            if any(value.get(name) is not None for name in _DELIVERY_FIELDS - {"kind"}):
                raise CronJobValidationError(
                    "delivery.channel_id, platform_target and thread_id apply only to kind chat"
                )
            return cls(kind=cast(DeliveryKind, kind))
        texts: dict[str, str | None] = {}
        for name in ("channel_id", "platform_target", "thread_id"):
            raw = value.get(name)
            text = raw.strip() if isinstance(raw, str) else None
            if (raw is not None or name != "thread_id") and not text:
                raise CronJobValidationError(f"delivery.{name} must be a non-empty string")
            texts[name] = text
        return cls(
            kind="chat",
            channel_id=texts["channel_id"],
            platform_target=texts["platform_target"],
            thread_id=texts["thread_id"],
        )


@dataclass(frozen=True, slots=True)
class CronResult:
    """The start of a Run's final answer, kept for the job's next Run."""

    text: str
    at: str
    run_id: str | None = None

    @classmethod
    def excerpt(cls, answer: str, *, at: str, run_id: str | None) -> CronResult:
        """Keep at most ``LAST_RESULT_MAX_CHARS`` of ``answer``; a cut ends with an ellipsis."""
        text = answer.strip()
        if len(text) > LAST_RESULT_MAX_CHARS:
            text = f"{text[: LAST_RESULT_MAX_CHARS - 1]}…"
        return cls(text=text, at=at, run_id=run_id)

    @property
    def cut(self) -> bool:
        """Whether the answer was longer than the excerpt."""
        return len(self.text) == LAST_RESULT_MAX_CHARS and self.text.endswith("…")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"text": self.text, "at": self.at}
        if self.run_id is not None:
            data["run_id"] = self.run_id
        return data

    @classmethod
    def from_dict(cls, value: Any) -> CronResult:
        if not isinstance(value, dict):
            raise CronJobValidationError("last_result must be an object or null")
        text, at, run_id = value.get("text"), value.get("at"), value.get("run_id")
        if not isinstance(text, str) or not text or len(text) > LAST_RESULT_MAX_CHARS:
            raise CronJobValidationError(
                f"last_result.text must be a non-empty string of at most "
                f"{LAST_RESULT_MAX_CHARS} characters"
            )
        if not isinstance(at, str):
            raise CronJobValidationError("last_result.at must be a UTC timestamp")
        _parse_utc_timestamp(at, field_name="last_result.at")
        if run_id is not None and (not isinstance(run_id, str) or not run_id):
            raise CronJobValidationError("last_result.run_id must be a non-empty string")
        return cls(text=text, at=at, run_id=run_id)


@dataclass(frozen=True, slots=True)
class CronWatch:
    """What a job checks at each due time before its Run starts.

    ``command`` runs in the host shell (PowerShell on Windows, bash elsewhere)
    in the job's working directory; ``url`` is an http(s) address read with GET.
    The Run starts only when the check's output changed (``_cron_watch``).
    """

    kind: WatchKind
    target: str

    @classmethod
    def parse(cls, kind: str, target: object) -> CronWatch:
        """Validate one watch; surrounding whitespace is dropped."""
        if kind not in _WATCH_FIELDS:
            raise CronJobValidationError("watch must name a command or a url")
        if not isinstance(target, str) or not target.strip():
            raise CronJobValidationError(f"watch.{kind} must be a non-empty string")
        text = target.strip()
        if kind == "command":
            if len(text) > WATCH_COMMAND_MAX_CHARS:
                raise CronJobValidationError(
                    f"watch.command must be at most {WATCH_COMMAND_MAX_CHARS} characters"
                )
            return cls(kind="command", target=text)
        try:
            parts = urlsplit(text)
        except ValueError:
            parts = None
        if (
            parts is None
            or parts.scheme.lower() not in {"http", "https"}
            or not parts.hostname
            or any(character.isspace() for character in text)
            or len(text) > WATCH_URL_MAX_CHARS
        ):
            raise CronJobValidationError(
                f"watch.url must be an absolute http or https URL of at most "
                f"{WATCH_URL_MAX_CHARS} characters"
            )
        return cls(kind="url", target=text)

    def to_dict(self) -> dict[str, str]:
        return {self.kind: self.target}

    @classmethod
    def from_dict(cls, value: Any) -> CronWatch:
        if not isinstance(value, dict):
            raise CronJobValidationError("watch must be an object or null")
        kinds = [name for name in sorted(_WATCH_FIELDS) if value.get(name) is not None]
        if len(kinds) != 1:
            raise CronJobValidationError("watch must name exactly one of command or url")
        return cls.parse(kinds[0], value[kinds[0]])


def _derive_cron_job_name(prompt: object) -> str:
    """Derive the stable name of a new job created without one."""
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
        return get_localzone()
    except Exception as error:
        get_logger("automation.cron").warning("Could not resolve system timezone: %s", error)
        return ZoneInfo("UTC")


def _parse_iso_datetime(value: str, *, field_name: str, allow_naive: bool) -> datetime:
    if not isinstance(value, str) or not value:
        raise CronJobValidationError(f"{field_name} must be a non-empty ISO 8601 timestamp")

    try:
        parsed = datetime.fromisoformat(value)
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
        raise CronJobValidationError(
            "schedule_type must be 'cron', 'interval', 'once', 'event' or 'webhook'"
        )

    if job.status not in _ALLOWED_STATUSES:
        raise CronJobValidationError("status must be active, paused, completed, failed, or missed")

    if job.permission_ceiling is not None:
        try:
            job.ceiling()
        except PermissionRulesError as error:
            raise CronJobValidationError(f"permission_ceiling: {error}") from error

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
    for field_name in (
        "last_attempt_at",
        "last_completed_at",
        "last_checked_at",
        "last_changed_at",
    ):
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
    if job.covered_until is not None:
        _parse_utc_timestamp(job.covered_until, field_name="covered_until")
    if job.last_run_id is not None and not isinstance(job.last_run_id, str):
        raise CronJobValidationError("last_run_id must be a string when provided")
    if job.delivery is not None:
        if not isinstance(job.delivery, CronDelivery):
            raise CronJobValidationError("delivery must be a delivery target or null")
        job.delivery = job.delivery.stored()
    if job.delivery is not None and job.delivery.kind == "chat" and job.project_id is not None:
        raise CronJobValidationError(
            "a job of a Project Agent cannot deliver to a chat: Channels belong to Identity Agents"
        )
    if job.last_check_error is not None:
        if not isinstance(job.last_check_error, str):
            raise CronJobValidationError("last_check_error must be a string when provided")
        job.last_check_error = _truncate_error(job.last_check_error)
    if job.watch is not None:
        if not isinstance(job.watch, CronWatch):
            raise CronJobValidationError("watch must be a command or URL to check, or null")
        if job.schedule_type not in WATCH_SCHEDULE_TYPES:
            raise CronJobValidationError(
                "watch applies only to a repeating schedule (a cron expression or an "
                "interval), not to a one-time, event or webhook schedule"
            )


_SCHEDULE_FIELDS = (
    "schedule_type",
    "cron_expression",
    "interval_seconds",
    "interval_anchor_at",
    "run_at",
    "event_id",
    "event_edge",
    "event_offset_minutes",
)
