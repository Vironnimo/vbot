"""Pure Cron schedule parsing, normalization and occurrence projection."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from croniter import croniter  # type: ignore[import-untyped]

from core.automation._cron_jobs import (
    CRON_EXPRESSION_FIELD_COUNT,
    MIN_INTERVAL_SECONDS,
    CronJob,
    CronJobValidationError,
    CronOccurrence,
    ParsedSchedule,
    _as_utc,
    _cron_is_schedulable,
    _format_duration,
    _parse_duration_seconds,
    _parse_iso_datetime,
    _parse_utc_timestamp,
)


def parse_schedule(
    timezone: ZoneInfo,
    now: datetime,
    schedule: str,
    *,
    reference_time: datetime | None = None,
) -> ParsedSchedule:
    """Parse the small agent-facing schedule language into persisted fields."""
    if not isinstance(schedule, str) or not schedule.strip():
        raise CronJobValidationError("schedule must be a non-empty string")

    normalized = " ".join(schedule.split())
    reference_utc = _as_utc(reference_time or now)

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
        if not _cron_is_schedulable(normalized, reference_utc.astimezone(timezone)):
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
        parsed = parsed.replace(tzinfo=timezone)
    return ParsedSchedule(schedule_type="once", run_at=parsed.astimezone(UTC).isoformat())


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


def next_fire_at(
    timezone: ZoneInfo, now: datetime, job: CronJob, *, reference_time: datetime | None = None
) -> str | None:
    """Project the next UTC fire instant from the canonical schedule rules."""
    if job.status != "active" or job.remaining_runs == 0:
        return None
    if job.schedule_type == "once":
        return _parse_run_at_utc(timezone, job).isoformat()
    if job.schedule_type == "interval":
        return _next_interval_fire_at(timezone, now, job, reference_time=reference_time).isoformat()
    if job.cron_expression is None:
        return None

    reference_utc = reference_time or now
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


def _project_job_occurrences(
    timezone: ZoneInfo,
    job: CronJob,
    window_start: datetime,
    window_end: datetime,
    now_utc: datetime,
    cap: int,
) -> list[CronOccurrence]:
    ticks = _job_schedule_ticks(timezone, job, window_start, window_end, cap)
    if job.remaining_runs is not None:
        future_budget = job.remaining_runs
        limited: list[datetime] = []
        for tick in ticks:
            if tick >= now_utc:
                if future_budget <= 0:
                    break
                future_budget -= 1
            limited.append(tick)
        ticks = limited
    return [
        CronOccurrence(
            job_id=job.id, name=job.name, fire_at_utc=tick, schedule_type=job.schedule_type
        )
        for tick in ticks
    ]


def _job_schedule_ticks(
    timezone: ZoneInfo,
    job: CronJob,
    window_start: datetime,
    window_end: datetime,
    cap: int,
) -> list[datetime]:
    if job.schedule_type == "once":
        fire_at = _parse_run_at_utc(timezone, job)
        if window_start <= fire_at < window_end:
            return [fire_at]
        return []
    if job.schedule_type == "interval":
        return _project_interval_ticks(timezone, job, window_start, window_end, cap)
    return _project_cron_ticks(timezone, job, window_start, window_end, cap)


def _project_interval_ticks(
    timezone: ZoneInfo,
    job: CronJob,
    window_start: datetime,
    window_end: datetime,
    cap: int,
) -> list[datetime]:
    if job.interval_seconds is None or job.interval_anchor_at is None:
        return []
    anchor = _parse_utc_timestamp(
        job.interval_anchor_at,
        field_name="interval_anchor_at",
    )
    elapsed = (window_start - anchor).total_seconds()
    first_index = max(1, math.ceil(elapsed / job.interval_seconds)) if elapsed > 0 else 1
    ticks: list[datetime] = []
    index = first_index
    tick = anchor + timedelta(seconds=index * job.interval_seconds)
    while tick < window_end and len(ticks) < cap:
        ticks.append(tick)
        index += 1
        tick = anchor + timedelta(seconds=index * job.interval_seconds)
    return ticks


def _project_cron_ticks(
    timezone: ZoneInfo,
    job: CronJob,
    window_start: datetime,
    window_end: datetime,
    cap: int,
) -> list[datetime]:
    if job.cron_expression is None:
        return []
    # get_next is exclusive; step back in UTC to include an exact window-start tick.
    cursor_local = (window_start - timedelta(microseconds=1)).astimezone(timezone)
    iterator = croniter(job.cron_expression, cursor_local)
    ticks: list[datetime] = []
    while len(ticks) < cap:
        next_local = cast(datetime, iterator.get_next(datetime))
        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=timezone)
        next_utc = next_local.astimezone(UTC)
        if next_utc >= window_end:
            break
        if next_utc >= window_start:
            ticks.append(next_utc)
    return ticks


def _parse_run_at_utc(timezone: ZoneInfo, job: CronJob) -> datetime:
    if job.run_at is None:
        raise CronJobValidationError("run_at is required for once jobs")

    parsed = _parse_iso_datetime(job.run_at, field_name="run_at", allow_naive=True)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(UTC)


def _next_interval_fire_at(
    timezone: ZoneInfo,
    now: datetime,
    job: CronJob,
    *,
    reference_time: datetime | None = None,
) -> datetime:
    if job.interval_seconds is None or job.interval_anchor_at is None:
        raise CronJobValidationError(
            "interval_seconds and interval_anchor_at are required for interval jobs"
        )
    reference_utc = _as_utc(reference_time or now)
    anchor_utc = _parse_utc_timestamp(
        job.interval_anchor_at,
        field_name="interval_anchor_at",
    )
    if reference_utc < anchor_utc:
        return anchor_utc
    elapsed_seconds = (reference_utc - anchor_utc).total_seconds()
    elapsed_intervals = int(elapsed_seconds // job.interval_seconds)
    return anchor_utc + timedelta(seconds=(elapsed_intervals + 1) * job.interval_seconds)


def normalize_job_schedule(timezone: ZoneInfo, now: datetime, job: CronJob) -> None:
    if job.schedule_type == "cron":
        if not isinstance(job.cron_expression, str) or not job.cron_expression.strip():
            raise CronJobValidationError("cron_expression is required for cron jobs")
        normalized_expression = job.cron_expression.strip()
        if len(normalized_expression.split()) != CRON_EXPRESSION_FIELD_COUNT:
            raise CronJobValidationError(
                f"cron_expression must contain exactly {CRON_EXPRESSION_FIELD_COUNT} fields "
                "(minute hour day-of-month month day-of-week)"
            )
        if not _cron_is_schedulable(normalized_expression, now.astimezone(timezone)):
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
        anchor_utc = _parse_utc_timestamp(
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
    job.run_at = _parse_run_at_utc(timezone, job).isoformat()
    job.cron_expression = None
    job.interval_seconds = None
    job.interval_anchor_at = None
