"""Cron catalog, durable fire claims and execution lifecycle."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from core.automation import _cron_claims as _claims
from core.automation import _cron_events as _events
from core.automation import _cron_schedule as _schedule
from core.automation import _cron_timing as _timing
from core.automation._cron_binding import CronEventBinding
from core.automation._cron_delivery import ChatDelivery, check_chat_delivery
from core.automation._cron_execution import CronExecution, _missing_session_text
from core.automation._cron_jobs import (
    _MUTABLE_FIELDS,
    _RESTART_FIELDS,
    _SCHEDULE_FIELDS,
    CRON_EXPRESSION_FIELD_COUNT,
    CRON_JOBS_FORMAT,
    LAST_RESULT_MAX_CHARS,
    MAX_ACTIVE_CRON_JOBS,
    MAX_CONCURRENT_CRON_RUNS,
    MAX_CONSECUTIVE_CRON_FAILURES,
    MAX_PROJECTED_OCCURRENCES_PER_JOB,
    MAX_STORED_CRON_JOBS,
    MIN_INTERVAL_SECONDS,
    TERMINAL_CRON_JOB_STATUSES,
    CronDelivery,
    CronJob,
    CronJobInPastError,
    CronJobNotFoundError,
    CronJobStatus,
    CronJobValidationError,
    CronOccurrence,
    CronResult,
    CronRunOutcome,
    CronServiceError,
    CronStorageError,
    CronTargetAgentNotFoundError,
    CronTargetError,
    CronTargetProjectNotFoundError,
    CronTargetUnavailableError,
    CronWatch,
    DeliveryKind,
    EventEdge,
    ParsedSchedule,
    ScheduleType,
    WatchKind,
    _as_utc,
    _derive_cron_job_name,
    _load_cron_jobs_payload,
    _resolve_timezone,
    _target_validation_error,
    _validate_cron_job_data,
    load_validated_cron_jobs_json,
    normalize_job_fields,
    validate_cron_jobs_data,
    validate_cron_jobs_file,
)
from core.automation._cron_projection import CronProjection
from core.automation._cron_scheduler import CronScheduler
from core.automation._cron_watch import WatchCheckError, WatchChecks
from core.automation._cron_watcher import FIRST_CHECK_WAIT_SECONDS, CronWatcher
from core.calendar import BoundJob, CalendarProjection, CalendarStorageError
from core.config_validation import JsonDiagnostic
from core.json_documents import JsonDocumentWriteError, write_json_document
from core.projects import (
    AgentResolutionError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
    format_agent_address,
)
from core.sessions import SessionAddress
from core.utils.file_status import exists_strict
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.workers import OrderedWorker, settle_before_cancelling

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.calendar import CalendarEvent
    from core.projects import AgentResolver
    from core.sessions import ChatSessionManager


__all__ = [
    "CRON_EXPRESSION_FIELD_COUNT",
    "ChatDelivery",
    "CronDelivery",
    "CronJob",
    "CronJobInPastError",
    "CronJobNotFoundError",
    "CronJobStatus",
    "CronJobValidationError",
    "CronOccurrence",
    "CronResult",
    "CronRunOutcome",
    "CronServiceError",
    "CronStorageError",
    "CronTargetAgentNotFoundError",
    "CronTargetError",
    "CronTargetProjectNotFoundError",
    "CronTargetUnavailableError",
    "CronWatch",
    "DeliveryKind",
    "EventCalendar",
    "LAST_RESULT_MAX_CHARS",
    "MAX_ACTIVE_CRON_JOBS",
    "MAX_CONCURRENT_CRON_RUNS",
    "MAX_CONSECUTIVE_CRON_FAILURES",
    "MAX_PROJECTED_OCCURRENCES_PER_JOB",
    "MAX_STORED_CRON_JOBS",
    "MIN_INTERVAL_SECONDS",
    "ParsedSchedule",
    "ScheduleType",
    "TERMINAL_CRON_JOB_STATUSES",
    "WatchCheckError",
    "WatchChecks",
    "WatchKind",
    "load_validated_cron_jobs_json",
    "validate_cron_jobs_data",
    "validate_cron_jobs_file",
    "CronService",
]


_ONCE_FIRE_CLAIMS_DIR_NAME = "once-fire-claims"


_EVENT_FIELDS = frozenset(("event_id", "event_edge", "event_offset_minutes"))
_EVENT_FIELDS_ELSEWHERE = (
    "event_id and the event time apply only to a job bound to a calendar event "
    "(schedule type event)"
)

EventCalendar = _events.EventCalendar


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

    With a ``calendar``, jobs can run at the occurrences of a calendar event
    (schedule type ``event``). The calendar asks this service before an event
    change and after an event is deleted (``check_event_change`` and
    ``event_deleted``), and its change notifications wake the event jobs.

    A job with a ``watch`` checks a command or URL at each due time and runs
    only when the output changed (``_cron_watcher``); the Runtime wires the
    checks in (``set_watch_checks``).
    """

    def __init__(
        self,
        trigger_service: TriggerService,
        data_root: str | Path,
        *,
        agent_resolver: AgentResolver | None = None,
        sessions: ChatSessionManager | None = None,
        tz: str | ZoneInfo | None = None,
        calendar: EventCalendar | None = None,
    ) -> None:
        self._writer = _CRON_WRITER
        self._trigger_service = trigger_service
        self._agent_resolver = agent_resolver
        self._sessions = sessions
        self._calendar = calendar
        # Delivers Run answers and failure alerts to Channel chats once wired.
        self._chats: ChatDelivery | None = None
        # Runs the checks of watching jobs once wired.
        self._watch_checks: WatchChecks | None = None
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
        # Replaced and set on every calendar change: event jobs recompute their occurrences.
        self._calendar_changed = asyncio.Event()
        self._started = False
        self._edits = asyncio.Lock()
        self._crash_saves: set[asyncio.Task[bool]] = set()
        self._scheduler: CronScheduler = CronScheduler(self)
        self._execution: CronExecution = CronExecution(self)
        self._event_binding: CronEventBinding = CronEventBinding(self)
        self._watcher: CronWatcher = CronWatcher(self)
        if calendar is not None:
            calendar.add_changed_callback(self._event_binding._on_calendar_changed)

    def set_chat_delivery(self, chats: ChatDelivery | None) -> None:
        """Wire the Channels that deliver jobs' answers and alerts to chats.

        Without it, a job cannot be set to deliver to a chat, and a stored chat
        delivery logs a warning instead of sending.
        """
        self._chats = chats

    def set_watch_checks(self, checks: WatchChecks | None) -> None:
        """Wire what runs the checks of watching jobs.

        Without it, a job cannot be set to watch, and a stored watch fails its
        checks.
        """
        self._watch_checks = checks

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
        event_id: str | None = None,
        event_edge: EventEdge | None = None,
        event_offset_minutes: int | None = None,
        remaining_runs: int | None = None,
        session_id: str | None = None,
        status: CronJobStatus = "active",
        project_id: str | None = None,
        delivery: CronDelivery | None = None,
        watch: CronWatch | None = None,
        actor: str = _DEFAULT_ACTOR,
    ) -> CronJob:
        """Create and persist a new cron job.

        ``project_id=None`` is a global/identity target (unchanged); a set value
        scopes the fired Session/Run to that project's anchor. An ``event`` job
        names an existing calendar event; occurrences due before it was created
        are not owed. ``delivery`` says where each Run's final answer goes; a
        chat must belong to a Channel of the job's Identity Agent. ``watch``
        makes a recurring job run only when the output of its check changed;
        an active job checks it at once (:meth:`await_first_check`).
        """
        if schedule_type != "event" and (
            event_id is not None or event_edge is not None or event_offset_minutes is not None
        ):
            raise CronJobValidationError(_EVENT_FIELDS_ELSEWHERE)
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
                event_id=event_id,
                event_edge=event_edge,
                event_offset_minutes=event_offset_minutes,
                remaining_runs=remaining_runs,
                session_id=session_id,
                status=status,
                last_fired_at=None,
                created_at=_timing._utc_now_iso(),
                project_id=project_id,
                delivery=delivery,
                watch=watch,
            )
            self._validate_job(job, validate_references=False)
            self._check_watch_available(job)
            self._event_binding._check_event_binding(job)
            await self._validate_references_async(job, check_delivery=True)
            self._reject_past_once_run(job)
            self._validate_capacity(job)
            self._jobs[job.id] = job

            async def save() -> None:
                await self._save_edit(lambda: self._jobs.pop(job.id, None))
                if self._started and job.status == "active":
                    self._scheduler._start_job_task(job)
                    if job.watch is not None:
                        self._watcher.start_first_check(job.id)

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

    def list_jobs(self, *, event_id: str | None = None) -> list[CronJob]:
        """List persisted cron jobs in created order; with ``event_id``, that event's jobs."""
        self._ensure_jobs_loaded(allow_degraded=True)
        ordered = sorted(self._jobs.values(), key=lambda value: (value.created_at, value.id))
        return [
            self._clone_job(job)
            for job in ordered
            if event_id is None or (job.schedule_type == "event" and job.event_id == event_id)
        ]

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
    def parse_event_time(event_time: str) -> tuple[EventEdge, int]:
        """The edge and signed offset in minutes of an event time such as ``start - 30m``.

        An event time is ``start`` or ``end``, optionally + or - a duration of
        at most 31 days.
        """
        return _events.parse_event_time(event_time)

    @staticmethod
    def parse_event_schedule(event_id: str, event_time: str) -> ParsedSchedule:
        """The schedule of a job that runs at every occurrence of ``event_id`` at ``event_time``."""
        edge, offset = _events.parse_event_time(event_time)
        return ParsedSchedule(
            schedule_type="event",
            event_id=event_id,
            event_edge=edge,
            event_offset_minutes=offset,
        )

    @staticmethod
    def format_schedule(job: CronJob) -> str:
        """Return the canonical schedule string for one job."""
        return _schedule.format_schedule(job)

    def next_fire_at(self, job: CronJob, *, reference_time: datetime | None = None) -> str | None:
        """Project the next fire in the current application timezone.

        An event job's next fire is its next occurrence's due time after the
        reference time; None when no occurrence lies ahead or its event is gone.
        """
        reference = reference_time or _timing._utc_now()
        if job.schedule_type != "event":
            return _schedule.next_fire_at(self._timezone, reference, job)
        if job.status != "active":
            return None
        event = self._event_binding._bound_event(job)
        if event is None or self._calendar is None:
            return None
        due = _events.next_due(self._calendar, event, job, _as_utc(reference))
        return None if due is None else due.due_at.isoformat()

    def bound_event(self, job: CronJob) -> CalendarEvent | None:
        """The calendar event an event job runs at; None when it is gone or cannot be read."""
        return self._event_binding._bound_event(job)

    def can_fire(self, job: CronJob, *, now: datetime | None = None) -> bool:
        """Whether a job that is not terminal history can still start a Run.

        An event job can no longer fire once no occurrence of its event is owed
        or ahead, for example after a one-time event has passed, or when its
        event is gone; such a job is history until the event moves later. While
        the calendar cannot be read, an event job counts as able to fire.
        """
        if job.status in TERMINAL_CRON_JOB_STATUSES:
            return False
        if job.schedule_type != "event":
            return True
        try:
            event = self._event_binding._event_of(job)
        except CalendarStorageError:
            return True
        if event is None:
            return False
        return self._event_binding._event_job_can_fire(job, event, now or _timing._utc_now())

    async def load_projection(self) -> None:
        """Preload the persisted job catalog before capturing a display projection."""
        if not self._jobs_loaded:
            await _CRON_WRITER.call_async(partial(self._ensure_jobs_loaded, allow_degraded=True))

    def projection(self) -> CronProjection:
        """Detach scheduled jobs and their bound event records from the live owner."""
        self._ensure_jobs_loaded(allow_degraded=True)
        jobs = tuple(self._clone_job(job) for job in self._jobs.values())
        events = [
            event for job in jobs if (event := self._event_binding._bound_event(job)) is not None
        ]
        calendar_zone = (
            self._calendar.system_timezone_name()
            if self._calendar is not None
            else str(self._timezone)
        )
        return CronProjection(
            jobs,
            self._timezone,
            _timing._utc_now(),
            CalendarProjection(events, ZoneInfo(calendar_zone)),
        )

    def project_occurrences(
        self,
        window_start_utc: datetime,
        window_end_utc: datetime,
        *,
        max_per_job: int = MAX_PROJECTED_OCCURRENCES_PER_JOB,
    ) -> list[CronOccurrence]:
        """Project committed schedules without mutating scheduler state."""
        return self.projection().project_occurrences(
            window_start_utc, window_end_utc, max_per_job=max_per_job
        )

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
                # A chat is checked when it or the job's Agent changes, so an
                # unrelated edit (pausing, say) works while its Channel is gone.
                await self._validate_references_async(
                    candidate,
                    check_delivery=bool({"delivery", "agent_id"} & set(changed_fields)),
                )
                # A fire may have changed the job while its references were checked.
                job, candidate, changed_fields, restart_task = self._stage_update(
                    job_id, fields, validate_references=False
                )
            if changed_fields:
                edited = self._clone_job(candidate)
                self._jobs[job_id] = candidate
                # Job tasks follow memory at once, so a fire in flight sees the edit.
                if self._started and restart_task:
                    self._scheduler._restart_job_task(candidate)

                def undo() -> None:
                    if self._jobs.get(job_id) is candidate:
                        # A fire during the save records on the live job; keep that.
                        _take_back_edit(candidate, before=job, edited=edited)
                        self._execution._restore_once_budget(candidate)
                        if self._started and restart_task:
                            self._scheduler._restart_job_task(candidate)

                await settle_before_cancelling(self._save_edit(undo))
                if "watch" in changed_fields:
                    await self._renew_watch(candidate)
        return self._clone_job(job), self._clone_job(candidate), changed_fields

    async def _renew_watch(self, job: CronJob) -> None:
        """After a saved watch change: forget the baseline, and check a new watch at once.

        The ordered writer removes the baseline before the first check can write one.
        """
        await self._watcher.remove_baseline(job.id)
        if self._started and job.status == "active" and job.watch is not None:
            self._watcher.start_first_check(job.id)

    async def await_first_check(self, job_id: str) -> CronJob | None:
        """Wait for the first check of a watch just set on the job; the job after it.

        None when no first check is pending for the job, or it still runs after
        a while; it then goes on and records its result on the job. The wait
        never stops the check.
        """
        if not await self._watcher.wait_for_first_check(job_id, FIRST_CHECK_WAIT_SECONDS):
            return None
        job = self._jobs.get(job_id)
        return None if job is None else self._clone_job(job)

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
                and job.schedule_type != "once"
                and "remaining_runs" not in fields
                and job.remaining_runs != 1
            ):
                raise CronJobValidationError("Changing to a one-time schedule requires repeat: 1")
        if candidate.schedule_type != "event" and any(
            fields.get(name) is not None for name in _EVENT_FIELDS
        ):
            raise CronJobValidationError(_EVENT_FIELDS_ELSEWHERE)
        if candidate.schedule_type == "event" and job.schedule_type != "event":
            # The event's occurrences drive the repetition; a run budget does not carry over.
            if "remaining_runs" not in fields:
                candidate.remaining_runs = None
            # Without an event time the job runs at each occurrence's start.
            if fields.get("event_edge") is None:
                candidate.event_edge = "start"
                candidate.event_offset_minutes = fields.get("event_offset_minutes") or 0
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
        watch_changed = "watch" in fields and candidate.watch != job.watch
        if watch_changed:
            # Checks of an earlier watch say nothing about this one.
            candidate.last_checked_at = None
            candidate.last_changed_at = None
            candidate.last_check_error = None
        if candidate.status == "active" and (
            job.status != "active"
            or watch_changed
            or any(getattr(job, name) != getattr(candidate, name) for name in _SCHEDULE_FIELDS)
        ):
            # Fires due while the job was inactive, on its earlier schedule or
            # under its earlier watch are not owed.
            candidate.covered_until = _timing._utc_now_iso()
            candidate.event_progress = None

        changed_fields = sorted(
            field_name
            for field_name in fields
            if getattr(job, field_name) != getattr(candidate, field_name)
        )
        if not changed_fields:
            return job, job, [], False

        self._validate_job(candidate, validate_references=validate_references)
        if watch_changed:
            self._check_watch_available(candidate)
        if (
            candidate.schedule_type == "once"
            and job.status not in TERMINAL_CRON_JOB_STATUSES
            and candidate.run_at != job.run_at
            and "remaining_runs" not in fields
        ):
            # A new once time arms a new fire even while the previous Run is
            # still settling with its own fire already consumed.
            candidate.remaining_runs = 1
        if set(_SCHEDULE_FIELDS) & set(changed_fields) or (
            candidate.status == "active" and job.status != "active"
        ):
            self._event_binding._check_event_binding(candidate)
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
            self._scheduler._cancel_job_task(job_id)

            def undo() -> None:
                self._jobs[job_id] = removed
                if self._started and removed.status == "active":
                    self._scheduler._restart_job_task(removed)

            async def save() -> None:
                await self._save_edit(undo)
                await _CRON_WRITER.call_async(
                    partial(_claims.remove, self._once_fire_claims_dir, job_id)
                )
                if removed.watch is not None:
                    await self._watcher.remove_baseline(job_id)
                self._watcher.forget(job_id)

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
        return self._scheduler.start()

    def stop(self) -> None:
        """Cancel all running cron tasks. Idempotent."""
        return self._scheduler.stop()

    async def aclose(self) -> None:
        """Stop cron scheduling and await canceled job tasks and pending saves."""
        return await self._scheduler.aclose()

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

    # -- jobs bound to calendar events ------------------------------------------------------

    async def check_event_change(self, before: CalendarEvent, after: CalendarEvent) -> None:
        """Refuse an event change that would revive jobs into a target that is gone.

        A job that can no longer fire is history, so removing its Agent,
        Project or selected Session did not check it. When the change (for
        example, moving a past one-time event later) lets such a job fire
        again, its target is checked like a new job's; a target that exists but
        cannot run now is allowed, because it can recover. The Agent and
        Session reads run on the Session pool, and only for revived jobs.

        Raises:
            EventJobTargetMissingError: naming each revived job and what it misses.
        """
        return await self._event_binding.check_event_change(before, after)

    async def event_deleted(self, event_id: str, *, actor: str) -> tuple[BoundJob, ...]:
        """Delete the jobs bound to a deleted calendar event; return what was deleted."""
        return await self._event_binding.event_deleted(event_id, actor=actor)

    def _missing_targets(self, jobs: list[CronJob]) -> list[tuple[str, str]]:
        """Pair each job whose target is partly gone with what is missing; blocking."""
        return [(job.id, problem) for job in jobs if (problem := self._missing_target(job))]

    def _missing_target(self, job: CronJob) -> str | None:
        """Say which part of the job's target no longer exists, if one does; blocking."""
        target = format_agent_address(job.agent_id, job.project_id)
        if self._agent_resolver is not None:
            try:
                self._agent_resolver.resolve_agent(job.project_id, job.agent_id)
            except ResolutionProjectNotFoundError:
                return f"Project {job.project_id} no longer exists"
            except ResolutionAgentNotFoundError:
                return f"Agent {target} no longer exists"
            except AgentResolutionError:
                pass  # It exists but cannot run now; the fire records why.
        if (
            job.session_id is not None
            and self._sessions is not None
            and not self._sessions.exists(
                SessionAddress(
                    project_id=job.project_id, agent_id=job.agent_id, session_id=job.session_id
                )
            )
        ):
            return f"Session {job.session_id} of {target} no longer exists"
        return None

    def _validate_job(self, job: CronJob, *, validate_references: bool = True) -> None:
        normalize_job_fields(job)
        if validate_references:
            self._validate_references(job)
        _schedule.normalize_job_schedule(self._timezone, _timing._utc_now(), job)

    async def _validate_references_async(
        self, job: CronJob, *, check_delivery: bool = False
    ) -> None:
        """Event-Loop-safe :meth:`_validate_references`: its reads use the Session pool.

        With ``check_delivery``, a chat delivery's Channel must exist and belong
        to the job's Agent.
        """
        if self._sessions is None:
            self._validate_references(job)
        else:
            await self._sessions.run_async(self._validate_references, job)
        if check_delivery:
            await check_chat_delivery(job, self._chats)

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

    def _check_watch_available(self, job: CronJob) -> None:
        if job.watch is not None and self._watch_checks is None:
            raise CronJobValidationError(
                "watch is unavailable: this vBot process runs no watch checks"
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


def _log_retarget(job_id: str, agent_id: str, new_agent_id: str) -> None:
    # One step of an Agent rename, which logs its own summary line.
    _LOGGER.debug(
        "Cron job retargeted (job=%s agent=%s new_agent=%s)", job_id, agent_id, new_agent_id
    )
