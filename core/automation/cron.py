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
from core.automation._cron_delivery import (
    ChatDelivery,
    CronFailureAlert,
    check_chat_delivery,
    check_email_channel,
)
from core.automation._cron_email import CronEmails
from core.automation._cron_execution import CronExecution, _missing_session_text
from core.automation._cron_inbox import CronInbox, InboxCounts
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
    CronEmailTrigger,
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
    cron_run_overrides,
    normalize_job_fields,
    patch_run_overrides,
    validate_cron_jobs_data,
    validate_cron_jobs_file,
)
from core.automation._cron_notification_model import (
    ALERT_MAX_CHARS,
    AlertKind,
    CronAlert,
    CronNotification,
)
from core.automation._cron_notifications import CronNotifications
from core.automation._cron_projection import CronProjection
from core.automation._cron_scheduler import CronScheduler
from core.automation._cron_watch import WatchCheckError, WatchChecks
from core.automation._cron_watcher import FIRST_CHECK_WAIT_SECONDS, CronWatcher
from core.automation._cron_webhook_events import WEBHOOK_BODY_MAX_BYTES, WEBHOOK_PATH_PREFIX
from core.automation._cron_webhooks import (
    CronWebhooks,
    WebhookReach,
    WebhookReceipt,
    WebhookStatus,
)
from core.calendar import BoundJob, CalendarProjection, CalendarStorageError
from core.config_validation import JsonDiagnostic
from core.json_documents import JsonDocumentWriteError, write_json_document
from core.permissions import PermissionCeiling, PermissionRefusal
from core.projects import (
    AgentOverrides,
    AgentResolutionError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
    WorkingProjectMissingError,
    format_agent_address,
)
from core.secrets._references import EMPTY_MASKER, SecretMasker
from core.sessions import (
    AGENT_DEFAULT_PROJECT,
    SessionAddress,
    SessionNotFoundError,
    WorkingProjectChoice,
)
from core.utils.file_status import exists_strict
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp
from core.utils.workers import OrderedWorker, settle_before_cancelling

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.calendar import CalendarEvent
    from core.channels import InboundMail
    from core.projects import AgentResolver
    from core.secrets import SecretStore
    from core.sessions import ChatSessionManager


__all__ = [
    "CRON_EXPRESSION_FIELD_COUNT",
    "ChatDelivery",
    "CronAlert",
    "CronDelivery",
    "CronEmailTrigger",
    "CronFailureAlert",
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
    "InboxCounts",
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
    "WEBHOOK_BODY_MAX_BYTES",
    "WEBHOOK_PATH_PREFIX",
    "WatchCheckError",
    "WatchChecks",
    "WatchKind",
    "WebhookReach",
    "WebhookReceipt",
    "WebhookStatus",
    "validate_cron_jobs_data",
    "validate_cron_jobs_file",
    "CronService",
    "cron_run_overrides",
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

    A ``webhook`` job runs when its URL receives a request that proves the
    job's secret (``_cron_webhooks``). The secrets live in the Secret Store the
    Runtime wires in (``set_webhook_secrets``); the server tells the service
    its address (``bind_webhook_url``) and hands it each request
    (``receive_webhook``).

    An ``email`` job runs when an email Channel of its Identity Agent receives
    a mail that matches its filters (``_cron_email``). The service is the
    Channels' mail sink: they hand it each mail no conversation takes
    (``accept_mail``).
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
        secret_masker: Callable[[], SecretMasker] = lambda: EMPTY_MASKER,
    ) -> None:
        self._secret_masker = secret_masker
        self._writer = _CRON_WRITER
        self._trigger_service = trigger_service
        self._agent_resolver = agent_resolver
        self._sessions = sessions
        self._calendar = calendar
        # Delivers Run answers and failure alerts to Channel chats once wired.
        self._chats: ChatDelivery | None = None
        self._mail_reference_lock: asyncio.Lock | None = None
        self._notifications = CronNotifications(self)
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
        self._failure_alert_callbacks: set[Callable[[CronFailureAlert], None]] = set()
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
        self._webhooks: CronWebhooks = CronWebhooks(self)
        self._emails: CronEmails = CronEmails(self)
        if calendar is not None:
            calendar.add_changed_callback(self._event_binding._on_calendar_changed)

    def set_chat_delivery(
        self, chats: ChatDelivery | None, *, reference_lock: asyncio.Lock | None = None
    ) -> None:
        """Wire the Channels that deliver jobs' answers and alerts to chats.

        Without it, a job cannot be set to deliver to a chat, and a stored chat
        delivery logs a warning instead of sending.
        """
        self._chats = chats
        if reference_lock is not None:
            self._mail_reference_lock = reference_lock

    def set_watch_checks(self, checks: WatchChecks | None) -> None:
        """Wire what runs the checks of watching jobs.

        Without it, a job cannot be set to watch, and a stored watch fails its
        checks.
        """
        self._watch_checks = checks

    def set_webhook_secrets(self, store: SecretStore | None) -> None:
        """Wire the Secret Store that keeps the secrets of webhook jobs.

        Without it, a job cannot get a webhook schedule, and requests to a
        stored webhook job are refused.
        """
        self._webhooks.set_secret_store(store)

    def bind_webhook_url(self, base_url: str | None, reach: WebhookReach | None) -> None:
        """Tell the service the address its webhook URLs start with, and who reaches it.

        ``base_url`` is a scheme and authority such as ``https://vbot.example.com``;
        None while no server serves the webhooks.
        """
        self._webhooks.bind_url(base_url, reach)

    def webhook_status(self, job: CronJob) -> WebhookStatus | None:
        """The URL and the received requests of a webhook job; None for any other job."""
        if job.schedule_type != "webhook":
            return None
        return self._webhooks.status(job)

    async def webhook_secret(self, job_id: str) -> str:
        """The secret a webhook job's requests prove.

        Raises ``CronJobNotFoundError``, ``CronJobValidationError`` for a job
        without a webhook schedule, and ``CronStorageError`` when the secret
        cannot be read or none is stored (rotating creates a new one).
        """
        self._webhook_job(job_id)
        secret = await self._webhooks.secret(job_id)
        if secret is None:
            raise CronStorageError(
                f"Cron job {job_id} has no webhook secret stored; rotate it to create a new one"
            )
        return secret

    async def rotate_webhook_secret(self, job_id: str, *, actor: str = _DEFAULT_ACTOR) -> str:
        """Replace a webhook job's secret; requests proving the old one are refused from now on."""
        async with self._edits:
            self._webhook_job(job_id)
            secret = await settle_before_cancelling(self._webhooks.issue_secret(job_id))
        _LOGGER.info("Cron webhook secret rotated (job=%s actor=%s)", job_id, actor)
        return secret

    async def receive_webhook(
        self, job_id: str, headers: list[tuple[str, str]], body: bytes
    ) -> WebhookReceipt:
        """Accept one request to a webhook job's URL for the job's next Run.

        ``headers`` are the request's header pairs with lowercase names and
        ``body`` its raw body of at most ``WEBHOOK_BODY_MAX_BYTES``. The request
        is stored before this returns ``accepted``. An unknown, inactive or
        non-webhook job and a wrong secret are all ``refused``; ``unavailable``
        means the request proved the secret but could not be stored.
        """
        return await self._webhooks.receive(job_id, headers, body)

    async def accept_mail(self, mail: InboundMail) -> None:
        """Store a mail for every active email job of its Channel whose filters match.

        Returns once each such job holds it durably; raises ``CronStorageError``
        when it could not be stored, so the Channel hands it over again. A job
        takes each ``message_id`` at most once.
        """
        if self._mail_reference_lock is None:
            await self._emails.accept_mail(mail)
        else:
            # A rename moves Channels before Cron jobs. New mail waits for the
            # complete owner transition; an old adapter can cancel while waiting.
            async with self._mail_reference_lock:
                await self._emails.accept_mail(mail)

    def email_status(self, job: CronJob) -> InboxCounts | None:
        """The mails an email job received and keeps for its next Run; None for any other job."""
        if job.schedule_type != "email":
            return None
        return self._emails.counts(job.id)

    def _inbox_of(self, job: CronJob) -> CronInbox[Any]:
        """Where the webhook requests or mails of a job that runs on arrivals wait."""
        return self._emails if job.schedule_type == "email" else self._webhooks

    def _webhook_job(self, job_id: str) -> CronJob:
        self._ensure_jobs_loaded()
        job = self._jobs.get(job_id)
        if job is None:
            raise CronJobNotFoundError(f"Cron job not found: {job_id}")
        if job.schedule_type != "webhook":
            raise CronJobValidationError(
                f"Cron job {job_id} has no webhook: its schedule is not 'webhook'"
            )
        return job

    def add_changed_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to persisted Cron changes and return an unsubscribe function."""
        self._changed_callbacks.add(callback)

        def unsubscribe() -> None:
            self._changed_callbacks.discard(callback)

        return unsubscribe

    def add_failure_alert_callback(
        self, callback: Callable[[CronFailureAlert], None]
    ) -> Callable[[], None]:
        """Subscribe to the alerts no Run reports and return an unsubscribe function.

        A job that delivers to vBot raises one at the second failure of a streak
        and when it stops, for failures no failed Run reports: failed checks and
        Runs that could not start. A chat delivery raises one when a message's
        delivery is uncertain or its Channel is gone. Each alert is stored on its
        job as ``unread_alert`` before it is raised. The callback runs on the
        Event Loop.
        """
        self._failure_alert_callbacks.add(callback)

        def unsubscribe() -> None:
            self._failure_alert_callbacks.discard(callback)

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
        working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
        run_overrides: AgentOverrides | None = None,
        delivery: CronDelivery | None = None,
        notification_policy: str = "all",
        watch: CronWatch | None = None,
        email: CronEmailTrigger | None = None,
        permission_ceiling: dict[str, Any] | None = None,
        actor: str = _DEFAULT_ACTOR,
    ) -> CronJob:
        """Create and persist a new cron job.

        ``project_id=None`` is a global/identity target (unchanged); a set value
        scopes the fired Session/Run to that project's anchor. An ``event`` job
        names an existing calendar event; occurrences due before it was created
        are not owed. ``delivery`` says where each Run's final answer goes; a
        chat must belong to a Channel of the job's Identity Agent. ``watch``
        makes a recurring job run only when the output of its check changed;
        an active job checks it at once (:meth:`await_first_check`). An
        ``email`` job names in ``email`` an email Channel of its Agent and the
        filters its mails must match.
        ``permission_ceiling`` is the creating Session's boundary
        (``PermissionBoundary.to_ceiling_dict(source="cron")``); every fire
        runs under it.
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
                working_project_id=working_project_id,
                run_overrides=run_overrides,
                delivery=delivery,
                notification_policy=notification_policy,
                watch=watch,
                email=email,
                permission_ceiling=permission_ceiling,
            )
            self._validate_job(job, validate_references=False)
            self._check_watch_available(job)
            if job.schedule_type == "webhook":
                self._webhooks.check_available()
            self._event_binding._check_event_binding(job)
            await self._validate_references_async(
                job, check_delivery=True, bind_delivery=True, check_email=True, snapshot_new=True
            )
            self._reject_past_once_run(job)
            self._validate_capacity(job)
            self._jobs[job.id] = job

            async def save() -> None:
                if job.schedule_type == "webhook":
                    try:
                        await self._webhooks.issue_secret(job.id)
                    except BaseException:
                        self._jobs.pop(job.id, None)
                        raise
                try:
                    await self._save_edit(lambda: self._jobs.pop(job.id, None))
                except BaseException:
                    if job.schedule_type == "webhook":
                        await self._webhooks.discard_secret(job.id)
                    raise
                if self._started and job.status == "active":
                    self._scheduler._start_job_task(job)
                    if job.watch is not None:
                        self._watcher.start_first_check(job.id)

            await settle_before_cancelling(save())

        _LOGGER.info(
            "Cron job created (job=%s agent=%s%s schedule_type=%s status=%s actor=%s%s)",
            job.id,
            job.agent_id,
            f" project={job.project_id}" if job.project_id else "",
            job.schedule_type,
            job.status,
            actor,
            " restricted=true" if job.permission_ceiling is not None else "",
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

    def references_project(self, job: CronJob, project_id: str) -> bool:
        """Whether the job's explicit target or pinned Session works in a Project.

        A legacy fresh-Session job has no stable Project binding. A pinned
        Identity Session does, independently of its Agent's current default.
        The caller runs these Session reads on the Session database's pool.
        """
        if job.project_id is not None:
            return job.project_id == project_id
        if job.session_id is not None and self._sessions is not None:
            try:
                binding = self._sessions.working_project(
                    SessionAddress(None, job.agent_id, job.session_id)
                )
            except SessionNotFoundError:
                return False
            return binding.project_id == project_id
        return job.working_project_id == project_id

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
        as ``update_job(job_id, agent_id=...)``, except that the job's retained
        chat notifications of the earlier id follow it, as their Channel does;
        the rename logs one summary line, so this step logs at DEBUG only.
        """
        if self._started:
            raise RuntimeError("Retarget the jobs of a started Cron service on its Event Loop")
        job, candidate, changed_fields, _restart = self._stage_update(
            job_id, {"agent_id": agent_id}, rename=True
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
        job, updated, changed_fields = await self._edit(job_id, {"agent_id": agent_id}, rename=True)
        if changed_fields:
            _log_retarget(job_id, job.agent_id, agent_id)
        return updated

    def retarget_notifications(self, agent_id: str, new_agent_id: str) -> tuple[str, ...]:
        """Move every retained chat notification of ``agent_id`` to ``new_agent_id``.

        One step of an Identity Agent rename, after its jobs moved: an Agent
        rename moves the Agent's Channels, so messages waiting for them follow,
        also those of jobs the rename does not retarget (completed history, or a
        job edited to another Agent since). Returns the ids of the changed jobs.
        For a service that is not started: blocking, like :meth:`retarget_agent`.
        """
        if self._started:
            raise RuntimeError("Retarget the notifications of a started Cron service on its loop")
        self._ensure_jobs_loaded()
        before = self._move_notifications(agent_id, new_agent_id)
        if before:
            try:
                self._save_jobs()
            except Exception:
                self._restore_notifications(before)
                raise
            self._notify_changed()
        return tuple(before)

    async def retarget_notifications_async(
        self, agent_id: str, new_agent_id: str
    ) -> tuple[str, ...]:
        """:meth:`retarget_notifications` for a started service, on its Event Loop."""
        async with self._edits:
            self._ensure_jobs_loaded()
            before = self._move_notifications(agent_id, new_agent_id)
            if before:
                await settle_before_cancelling(
                    self._save_edit(lambda: self._restore_notifications(before))
                )
        return tuple(before)

    def _move_notifications(
        self, agent_id: str, new_agent_id: str
    ) -> dict[str, tuple[CronNotification, ...]]:
        """Point notifications of ``agent_id`` at ``new_agent_id``.

        Returns the earlier notifications of each job it changed.
        """
        before: dict[str, tuple[CronNotification, ...]] = {}
        for job in self._jobs.values():
            if not any(item.agent_id == agent_id for item in job.notifications):
                continue
            before[job.id] = job.notifications
            job.notifications = tuple(
                dataclasses.replace(item, agent_id=new_agent_id)
                if item.agent_id == agent_id
                else item
                for item in job.notifications
            )
        return before

    def _restore_notifications(self, before: dict[str, tuple[CronNotification, ...]]) -> None:
        for job_id, notifications in before.items():
            job = self._jobs.get(job_id)
            if job is not None:
                job.notifications = notifications

    async def acknowledge_alert(
        self, job_id: str, *, actor: str = _DEFAULT_ACTOR, expected_at: str | None = None
    ) -> CronJob:
        """Mark a job's unread alert as read; a job without one stays as it is."""
        async with self._edits:
            self._ensure_jobs_loaded()
            job = self._jobs.get(job_id)
            if job is None:
                raise CronJobNotFoundError(f"Cron job not found: {job_id}")
            alert = job.unread_alert
            if alert is not None and (expected_at is None or alert.at == expected_at):
                job.unread_alert = None

                def undo() -> None:
                    if job.unread_alert is None:
                        job.unread_alert = alert

                await settle_before_cancelling(self._save_edit(undo))
                _LOGGER.info("Cron job alert acknowledged (job=%s actor=%s)", job_id, actor)
            return self._clone_job(job)

    async def _record_alert(
        self, job_id: str, message: str, *, kind: AlertKind = "failure"
    ) -> None:
        """Store an alert no Run reports on its job as unread, then raise it to subscribers.

        A later alert replaces an unread earlier one. The alert is raised even
        when the job file cannot be written, so the tray still shows it.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.unread_alert = CronAlert(
            message=message[:ALERT_MAX_CHARS],
            at=format_canonical_timestamp(_timing._utc_now()),
            kind=kind,
        )
        await self._execution._save_jobs_after_fire(job_id)
        if job.notification_policy == "muted":
            return
        self._raise_failure_alert(
            CronFailureAlert(
                job_id=job.id,
                name=job.name,
                agent_id=job.agent_id,
                project_id=job.project_id,
                message=message,
            )
        )

    async def _edit(
        self, job_id: str, fields: dict[str, Any], *, rename: bool = False
    ) -> tuple[CronJob, CronJob, list[str]]:
        """Apply ``fields`` to memory and the job tasks at once, then save; undo both on failure.

        Returns the job before and after the edit and the names that changed.
        A job that gets a webhook schedule gets a new secret before the save,
        and one that leaves it loses its secret and waiting requests after it.
        """
        async with self._edits:
            job, candidate, changed_fields, restart_task = self._stage_update(
                job_id, fields, validate_references=False, rename=rename
            )
            issued = False
            try:
                if (
                    changed_fields and changed_fields != ["notification_policy"]
                ) or "delivery" in fields:
                    candidate = self._clone_job(candidate)
                    snapshot_target = (
                        not rename
                        and "working_project_id" not in fields
                        and candidate.project_id is None
                        and candidate.session_id is None
                        and any(
                            getattr(job, name) != getattr(candidate, name)
                            for name in ("agent_id", "project_id", "session_id")
                        )
                    )
                    # A chat is checked when it or the job's Agent changes, so an
                    # unrelated edit (pausing, say) works while its Channel is gone.
                    await self._validate_references_async(
                        candidate,
                        check_delivery="delivery" in fields or "agent_id" in changed_fields,
                        bind_delivery="delivery" in fields and not rename,
                        check_email=bool(
                            {"email", "agent_id", "schedule_type"} & set(changed_fields)
                        ),
                        snapshot_new=snapshot_target,
                    )
                    if snapshot_target:
                        fields = {**fields, "working_project_id": candidate.working_project_id}
                    if "delivery" in fields:
                        fields = {**fields, "delivery": candidate.delivery}
                    if candidate.schedule_type == "webhook" and job.schedule_type != "webhook":
                        await settle_before_cancelling(self._prepare_webhook(job_id))
                        issued = True
                    # A fire may have changed the job while its references were checked.
                    job, candidate, changed_fields, restart_task = self._stage_update(
                        job_id, fields, validate_references=False, rename=rename
                    )
                if changed_fields:
                    await self._apply_edit(
                        job_id, job, candidate, changed_fields, restart_task, rename=rename
                    )
            except BaseException:
                if issued:
                    await settle_before_cancelling(self._webhooks.discard_secret(job_id))
                raise
        return self._clone_job(job), self._clone_job(candidate), changed_fields

    async def _apply_edit(
        self,
        job_id: str,
        job: CronJob,
        candidate: CronJob,
        changed_fields: list[str],
        restart_task: bool,
        *,
        rename: bool = False,
    ) -> None:
        """Apply a staged edit to memory and the job tasks at once, then save; undo on failure."""
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
        if job.schedule_type == "webhook" and candidate.schedule_type != "webhook":
            await settle_before_cancelling(self._webhooks.discard(job_id))
        if job.email is not None and (
            candidate.email is None or candidate.email.channel_id != job.email.channel_id
        ):
            # Mails of another Channel are not this job's any more.
            await settle_before_cancelling(self._emails.discard(job_id))
        if _new_check(job, candidate, rename=rename):
            await self._renew_watch(candidate)

    async def _prepare_webhook(self, job_id: str) -> None:
        """Give a job that gets a webhook schedule a new secret and no stale requests."""
        await self._webhooks.discard(job_id, keep_secret=True)
        await self._webhooks.issue_secret(job_id)

    async def _renew_watch(self, job: CronJob) -> None:
        """After a saved watch change: forget the baseline, and check a new watch at once.

        The ordered writer removes the baseline before the first check can write one.
        """
        await self._watcher.remove_baseline(job.id)
        if self._started and job.status == "active" and job.watch is not None:
            self._watcher.start_first_check(job.id)

    async def watch_refusal(
        self,
        watch: CronWatch,
        *,
        agent_id: str,
        project_id: str | None,
        permission_ceiling: dict[str, Any] | None,
        working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
        session_id: str | None = None,
    ) -> PermissionRefusal | None:
        """Whether a job's stored-form *permission_ceiling* refuses *watch*.

        None when it allows it, when there is no ceiling, or when this process
        runs no checks (``set_watch_checks``), so a check would not run anyway.
        """
        if permission_ceiling is None or self._watch_checks is None:
            return None
        return await self._watch_checks.permission_refusal(
            watch,
            agent_id=agent_id,
            project_id=project_id,
            working_project_id=working_project_id,
            session_id=session_id,
            ceiling=PermissionCeiling.from_ceiling_dict(permission_ceiling),
        )

    async def await_first_check(self, job_id: str) -> bool:
        """Wait for the first check of a watch just set on the job to record its outcome.

        True once no first check of the job is pending: it recorded
        ``last_checked_at`` (and ``last_check_error`` when it failed), or none
        ran because the job is paused. False when it still runs after
        ``FIRST_CHECK_WAIT_SECONDS``; the wait never stops it.
        """
        return await self._watcher.wait_for_first_check(job_id, FIRST_CHECK_WAIT_SECONDS)

    def _stage_update(
        self,
        job_id: str,
        fields: dict[str, Any],
        *,
        validate_references: bool = True,
        rename: bool = False,
    ) -> tuple[CronJob, CronJob, list[str], bool]:
        """Validate ``fields`` against a job without applying them.

        Returns the current job, the updated candidate, the names that changed
        and whether the job's task must restart. Without changes the candidate
        is the current job itself. ``validate_references=False`` leaves the
        blocking target and Session reads to the caller. ``rename`` moves the
        retained notifications of the job's earlier Agent id to the new one: an
        Agent rename moves their Channel too, while an ordinary edit never
        redirects an older message.
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

        fields = dict(fields)
        if isinstance(fields.get("run_overrides"), dict):
            fields["run_overrides"] = patch_run_overrides(
                job.run_overrides, fields["run_overrides"]
            )
        if (
            not rename
            and "working_project_id" not in fields
            and any(
                name in fields and fields[name] != getattr(job, name)
                for name in ("agent_id", "project_id", "session_id")
            )
        ):
            # A changed target derives its context anew. Fresh Identity targets
            # capture the current default during reference validation.
            fields["working_project_id"] = AGENT_DEFAULT_PROJECT

        if (
            job.status in TERMINAL_CRON_JOB_STATUSES
            and not rename
            and any(
                getattr(job, name) != value
                for name, value in fields.items()
                if name != "notification_policy"
            )
        ):
            raise CronJobValidationError(
                "Completed or missed jobs are immutable history and cannot be changed"
            )

        candidate = self._clone_job(job)
        restart_task = any(field in _RESTART_FIELDS for field in fields)

        for field_name, field_value in fields.items():
            setattr(candidate, field_name, field_value)
        if rename and candidate.agent_id != job.agent_id:
            candidate.notifications = tuple(
                dataclasses.replace(item, agent_id=candidate.agent_id)
                if item.agent_id == job.agent_id
                else item
                for item in job.notifications
            )
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
        if fields.get("status") == "active" and job.status == "failed":
            candidate.consecutive_failures = 0
            # Enabling the stopped job again answers the alert about its failures.
            if candidate.unread_alert is not None and candidate.unread_alert.kind == "failure":
                candidate.unread_alert = None
            if candidate.remaining_runs == 0:
                candidate.remaining_runs = 1
        if candidate.schedule_type == "webhook" and job.schedule_type != "webhook":
            self._webhooks.check_available()
        watch_changed = _new_check(job, candidate, rename=rename)
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

        self._validate_job(
            candidate,
            validate_references=validate_references and changed_fields != ["notification_policy"],
        )
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

    async def retry_delivery(self, job_id: str, *, delivery: CronDelivery | None = None) -> int:
        """Retry retained notifications, including an explicitly checked uncertain send."""
        return await self._notifications.retry(job_id, delivery)

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
                if removed.schedule_type == "webhook":
                    await self._webhooks.discard(job_id)
                if removed.schedule_type == "email":
                    await self._emails.discard(job_id)

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

    def _raise_failure_alert(self, alert: CronFailureAlert) -> None:
        for callback in tuple(self._failure_alert_callbacks):
            try:
                callback(alert)
            except Exception as error:
                _LOGGER.error(
                    "Cron failure alert callback failed: %s",
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
            if self._sessions is not None and any(job.last_run_id for job in self._jobs.values()):
                self._sessions.notifications.adopt_schedule_runs(
                    [
                        (
                            job.id,
                            job.agent_id,
                            job.project_id,
                            job.last_run_id,
                            job.notification_policy,
                        )
                        for job in self._jobs.values()
                        if job.last_run_id
                    ]
                )
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
            agent = None
            try:
                agent = self._agent_resolver.resolve_agent(
                    job.project_id, job.agent_id, require_model=False
                )
            except ResolutionProjectNotFoundError:
                return f"Project {job.project_id} no longer exists"
            except ResolutionAgentNotFoundError:
                return f"Agent {target} no longer exists"
            except AgentResolutionError:
                pass  # It exists but cannot run now; the fire records why.
            if (
                agent is not None
                and job.project_id is None
                and (
                    job.session_id is not None
                    or job.working_project_id is not AGENT_DEFAULT_PROJECT
                )
            ):
                try:
                    self._agent_resolver.resolve_working_project(
                        None, agent, session_id=job.session_id, requested=job.working_project_id
                    )
                except WorkingProjectMissingError as error:
                    return f"Project {error.project_id} no longer exists"
                except ResolutionProjectNotFoundError:
                    return f"Project {job.working_project_id} no longer exists"
                except AgentResolutionError:
                    pass
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
        self,
        job: CronJob,
        *,
        check_delivery: bool = False,
        bind_delivery: bool = False,
        check_email: bool = False,
        snapshot_new: bool = False,
    ) -> None:
        """Event-Loop-safe :meth:`_validate_references`: its reads use the Session pool.

        With ``check_delivery``, a chat delivery's Channel must exist and belong
        to the job's Agent; with ``check_email``, so must an email job's email
        Channel.
        """
        validate = (
            partial(self._validate_references, snapshot_new=True)
            if snapshot_new
            else self._validate_references
        )
        if self._sessions is None:
            validate(job)
        else:
            await self._sessions.run_async(validate, job)
        if check_delivery:
            await check_chat_delivery(job, self._chats, bind_platform=bind_delivery)
        if check_email:
            await check_email_channel(job, self._chats)

    def _validate_references(self, job: CronJob, *, snapshot_new: bool = False) -> None:
        """Refuse a job whose target Agent or selected Session is unavailable. Blocking."""
        if self._agent_resolver is not None:
            try:
                kwargs: dict[str, Any] = {}
                if job.session_id is not None:
                    kwargs["session_id"] = job.session_id
                if job.run_overrides is not None:
                    kwargs["run_overrides"] = job.run_overrides
                agent = self._agent_resolver.resolve_agent(job.project_id, job.agent_id, **kwargs)
                choose_new = snapshot_new and job.session_id is None and job.project_id is None
                if job.project_id is None and (
                    choose_new
                    or job.session_id is not None
                    or job.working_project_id is not AGENT_DEFAULT_PROJECT
                ):
                    actual = self._agent_resolver.resolve_working_project(
                        job.project_id,
                        agent,
                        session_id=job.session_id,
                        requested=job.working_project_id,
                    )
                    if (
                        job.session_id is not None
                        and job.working_project_id is not AGENT_DEFAULT_PROJECT
                        and actual != job.working_project_id
                    ):
                        raise CronJobValidationError(
                            "A pinned Session keeps its own Working Project"
                        )
                    if choose_new:
                        job.working_project_id = actual
            except Exception as error:
                if isinstance(error, CronJobValidationError):
                    raise
                raise _target_validation_error(job, error) from error
        elif (
            snapshot_new
            and job.session_id is None
            and job.project_id is None
            and job.working_project_id is AGENT_DEFAULT_PROJECT
        ):
            job.working_project_id = None
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


def _new_check(before: CronJob, after: CronJob, *, rename: bool = False) -> bool:
    """A new check or command context needs a fresh baseline; renames keep identity."""
    if before.watch is None:
        return after.watch is not None
    if not before.watch.same_check(after.watch):
        return True
    return (
        not rename
        and after.watch is not None
        and after.watch.kind == "command"
        and any(
            getattr(before, name) != getattr(after, name)
            for name in ("agent_id", "project_id", "working_project_id", "session_id")
        )
    )
