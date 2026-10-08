"""Cron jobs bound to a calendar event: one Run per occurrence, catch-up windows and event edits."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from core.automation import _cron_timing as cron_timing
from core.automation.cron import CronJob, CronJobValidationError, CronOccurrence, CronService
from core.calendar import BoundJob, CalendarService, EventJobTargetMissingError
from core.projects import ResolutionAgentNotFoundError
from core.runs import RunKind
from tests.core.automation.cron_test_support import make_service

_ZONE = "Europe/Berlin"
_PROMPT = "Prepare the meeting."


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class _Services:
    calendar: CalendarService
    cron: CronService
    trigger: SimpleNamespace
    clock: _Clock


def _local(text: str) -> datetime:
    """A Berlin wall-clock time as a UTC instant."""
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(_ZONE)).astimezone(UTC)


@pytest.fixture
def services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Services:
    clock = _Clock(_local("2030-01-06T13:00"))
    monkeypatch.setattr(cron_timing, "_utc_now", clock)
    calendar = CalendarService(tmp_path, tz=_ZONE)
    cron, trigger = make_service(tmp_path, tz=_ZONE, calendar=calendar)
    return _Services(calendar, cron, trigger, clock)


def _schedule(cron: CronService, event_id: str, event_time: str) -> dict[str, Any]:
    """The job fields of a schedule at ``event_time`` of every occurrence of ``event_id``."""
    return dict(cron.parse_event_schedule(event_id, event_time).as_job_fields())


async def _event_job(
    services: _Services, event_id: str, event_time: str = "start", **fields: Any
) -> CronJob:
    schedule = _schedule(services.cron, event_id, event_time)
    return await services.cron.create_job(
        agent_id="agent-one", prompt=_PROMPT, **schedule, **fields
    )


def _let_time_pass(
    services: _Services, job_id: str, monkeypatch: pytest.MonkeyPatch, *, until: datetime
) -> list[datetime]:
    """Each wait of the job jumps the clock to its target; one beyond ``until`` ends the job."""
    waits: list[datetime] = []

    async def wait(target: datetime, **_kwargs: Any) -> bool:
        if target <= services.clock.now:
            return True
        waits.append(target)
        if target > until:
            services.cron._jobs[job_id].status = "paused"
            return False
        services.clock.now = target
        return True

    monkeypatch.setattr(cron_timing, "_sleep_until_utc", wait)
    return waits


def _notes(services: _Services) -> list[str]:
    return [call.kwargs["context_note"] for call in services.trigger.trigger_run.await_args_list]


def _event_times(services: _Services) -> list[str]:
    return [
        line.removeprefix("Event time: ")
        for note in _notes(services)
        for line in note.splitlines()
        if line.startswith("Event time: ")
    ]


@pytest.mark.asyncio
async def test_each_occurrence_runs_the_prompt_once_with_the_event_as_context(
    services: _Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = services.calendar.create_event(
        title="Standup",
        start="2030-01-07T09:00",
        end="2030-01-07T09:30",
        location="Hall B",
        description="Status round.",
        rrule="FREQ=WEEKLY;BYDAY=MO",
    )
    job = await _event_job(services, event.id, "start - 30m")
    _let_time_pass(services, job.id, monkeypatch, until=_local("2030-01-20T00:00"))

    await services.cron._run_event_job(job)

    calls = services.trigger.trigger_run.await_args_list
    assert [call.args for call in calls] == [("agent-one", _PROMPT, None)] * 2
    assert all(call.kwargs["run_kind"] is RunKind.CRON for call in calls)
    assert _notes(services)[0] == (
        f'Cron job {job.id} is due (start - 30m) for the calendar event "Standup" '
        f"(event {event.id}, occurrence {event.id}_20300107T0900).\n"
        "Event time: 2030-01-07T09:00 to 2030-01-07T09:30 (Europe/Berlin)\n"
        "Location: Hall B\n"
        "Description: Status round."
    )
    assert f"occurrence {event.id}_20300114T0900" in _notes(services)[1]
    assert services.cron.get_job(job.id).last_fired_at == _local("2030-01-14T08:30").isoformat()


_SINGLE = {"title": "Review", "start": "2030-01-10T15:00", "end": "2030-01-10T16:00"}
_DAILY = {
    "title": "Sync",
    "start": "2030-01-10T09:00",
    "end": "2030-01-10T10:00",
    "rrule": "FREQ=DAILY",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "event_time", "back_at", "fired"),
    [
        # Due before the event: it may start late until the event starts.
        pytest.param(_SINGLE, "start - 2h", "2030-01-10T14:30", ["2030-01-10T15:00"], id="before"),
        pytest.param(_SINGLE, "start - 2h", "2030-01-10T15:05", [], id="before-closed"),
        # Due during the event: until it ends.
        pytest.param(_SINGLE, "start + 30m", "2030-01-10T15:50", ["2030-01-10T15:00"], id="during"),
        pytest.param(_SINGLE, "start + 30m", "2030-01-10T16:10", [], id="during-closed"),
        # Due after the event: until the next occurrence starts; one starting later replaces it.
        pytest.param(_DAILY, "end + 1h", "2030-01-11T08:00", ["2030-01-10T09:00"], id="after"),
        pytest.param(_DAILY, "end + 1h", "2030-01-11T09:30", [], id="after-replaced"),
        # After the last occurrence, however late.
        pytest.param(
            _SINGLE, "end + 1h", "2031-01-10T12:00", ["2030-01-10T15:00"], id="after-last"
        ),
        # Several open windows each start their own Run, earliest first.
        pytest.param(
            _DAILY,
            "start - 2d",
            "2030-01-11T08:30",
            ["2030-01-11T09:00", "2030-01-12T09:00"],
            id="several",
        ),
    ],
)
async def test_an_occurrence_missed_while_vbot_was_off_starts_late_while_its_window_is_open(
    services: _Services,
    monkeypatch: pytest.MonkeyPatch,
    event: dict[str, str],
    event_time: str,
    back_at: str,
    fired: list[str],
) -> None:
    created = services.calendar.create_event(**event)
    job = await _event_job(services, created.id, event_time)
    services.clock.now = _local(back_at)
    _let_time_pass(services, job.id, monkeypatch, until=services.clock.now)

    await services.cron._run_event_job(job)

    assert [text.split(" to ")[0] for text in _event_times(services)] == fired
    assert all("is starting late" in note for note in _notes(services))


@pytest.mark.asyncio
async def test_an_occurrence_due_before_the_job_existed_is_not_owed(
    services: _Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = services.calendar.create_event(**_SINGLE)
    services.clock.now = _local("2030-01-10T14:30")
    job = await _event_job(services, event.id, "start - 1h")
    waits = _let_time_pass(services, job.id, monkeypatch, until=_local("2030-02-01T00:00"))

    await services.cron._run_event_job(job)

    services.trigger.trigger_run.assert_not_awaited()
    # Nothing lies ahead: the job waits for a calendar change and can no longer fire.
    assert waits == [datetime.max.replace(tzinfo=UTC)]
    assert services.cron.next_fire_at(job) is None
    assert services.cron.can_fire(job) is False


@pytest.mark.asyncio
async def test_changed_and_removed_occurrences_move_or_skip_the_job(
    services: _Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = services.calendar.create_event(
        title="Standup", start="2030-01-07T09:00", rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=4"
    )
    job = await _event_job(services, event.id)
    await services.calendar.update_occurrence(
        f"{event.id}_20300114T0900", start="2030-01-15T14:00", actor="test"
    )
    await services.calendar.delete_occurrence(f"{event.id}_20300121T0900", actor="test")
    _let_time_pass(services, job.id, monkeypatch, until=_local("2030-02-01T00:00"))

    await services.cron._run_event_job(job)

    assert [text.split(" to ")[0] for text in _event_times(services)] == [
        "2030-01-07T09:00",
        "2030-01-15T14:00",
        "2030-01-28T09:00",
    ]


@pytest.mark.asyncio
async def test_a_calendar_change_wakes_the_job_to_follow_a_moved_event(
    services: _Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = services.calendar.create_event(**_SINGLE)
    job = await _event_job(services, event.id)
    waits: list[datetime] = []
    woken: list[bool] = []

    async def wait(target: datetime, *, wake_event: asyncio.Event | None = None) -> bool:
        if target <= services.clock.now:
            return True
        waits.append(target)
        if len(waits) == 1:
            await services.calendar.update_event(event.id, start="2030-01-10T17:00", actor="test")
            woken.append(wake_event is not None and wake_event.is_set())
            return False
        if len(waits) == 2:
            services.clock.now = target
            return True
        services.cron._jobs[job.id].status = "paused"
        return False

    monkeypatch.setattr(cron_timing, "_sleep_until_utc", wait)

    await services.cron._run_event_job(job)

    assert woken == [True]
    assert waits[:2] == [_local("2030-01-10T15:00"), _local("2030-01-10T17:00")]
    assert _event_times(services) == ["2030-01-10T17:00 to 2030-01-10T18:00 (Europe/Berlin)"]


@pytest.mark.asyncio
async def test_deleting_an_event_deletes_its_jobs(services: _Services, tmp_path: Path) -> None:
    event = services.calendar.create_event(**_SINGLE)
    other = services.calendar.create_event(title="Other", start="2030-01-11T09:00")
    second = await _event_job(services, event.id, "end")
    kept = await _event_job(services, other.id)
    services.clock.now = services.clock.now.replace(minute=5)
    first = await _event_job(services, event.id, "start - 1h")

    removed = await services.calendar.delete_event(event.id, actor="test")

    # In the order the jobs were created.
    assert removed == (BoundJob(second.id, second.name), BoundJob(first.id, first.name))
    restarted, _trigger = make_service(tmp_path, tz=_ZONE)
    assert [job.id for job in restarted.list_jobs()] == [kept.id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_time", "stored", "shown"),
    [
        ("START-30m", ("start", -30), "start - 30m"),
        ("end + 90m", ("end", 90), "end + 90m"),
        ("end+120m", ("end", 120), "end + 2h"),
        ("start - 1440m", ("start", -1440), "start - 1d"),
        ("end", ("end", 0), "end"),
    ],
)
async def test_event_time_is_stored_as_edge_and_offset_and_shown_in_one_form(
    services: _Services, tmp_path: Path, event_time: str, stored: tuple[str, int], shown: str
) -> None:
    event = services.calendar.create_event(**_SINGLE)

    job = await _event_job(services, event.id, event_time)

    reloaded = make_service(tmp_path, tz=_ZONE)[0].get_job(job.id)
    assert (reloaded.event_id, reloaded.event_edge, reloaded.event_offset_minutes) == (
        event.id,
        *stored,
    )
    assert (CronService.format_schedule(reloaded), reloaded.remaining_runs) == (shown, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_id", "event_time", "fields", "message"),
    [
        (None, "start", {"remaining_runs": 2}, "repeat is not available"),
        ("evt_missing", "start", {}, "Calendar event not found: evt_missing"),
        (None, "start - 32d", {}, "must not exceed 31 days"),
        (None, "noon", {}, "event time must be start or end"),
    ],
)
async def test_an_invalid_event_job_is_refused(
    services: _Services,
    event_id: str | None,
    event_time: str,
    fields: dict[str, Any],
    message: str,
) -> None:
    event = services.calendar.create_event(**_SINGLE)

    with pytest.raises(CronJobValidationError, match=message):
        await _event_job(services, event_id or event.id, event_time, **fields)
    assert services.cron.list_jobs() == []


@pytest.mark.asyncio
async def test_a_schedule_change_replaces_the_event_binding(services: _Services) -> None:
    event = services.calendar.create_event(**_SINGLE)
    job = await services.cron.create_job(
        agent_id="agent-one",
        prompt=_PROMPT,
        schedule_type="interval",
        interval_seconds=3600,
        remaining_runs=3,
    )

    bound = await services.cron.update_job(job.id, **_schedule(services.cron, event.id, "end"))
    listed = services.cron.list_jobs(event_id=event.id)
    unbound = await services.cron.update_job(
        job.id, schedule_type="cron", cron_expression="0 9 * * *"
    )

    # The event's occurrences drive the repetition, so the remaining run count goes.
    assert (bound.schedule_type, bound.remaining_runs, bound.interval_seconds) == (
        "event",
        None,
        None,
    )
    assert [item.id for item in listed] == [job.id]
    assert services.cron.list_jobs(event_id=event.id) == []
    assert (unbound.event_id, unbound.event_edge, unbound.event_offset_minutes) == (
        None,
        None,
        None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_gone", [False, True])
async def test_moving_a_past_event_later_checks_the_target_of_the_job_it_revives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_gone: bool
) -> None:
    clock = _Clock(_local("2030-01-06T13:00"))
    monkeypatch.setattr(cron_timing, "_utc_now", clock)
    removed: set[str] = set()

    def resolve_agent(_project_id: str | None, agent_id: str) -> Any:
        if agent_id in removed:
            raise ResolutionAgentNotFoundError(f"Agent not found: {agent_id}")
        return SimpleNamespace(id=agent_id)

    calendar = CalendarService(tmp_path, tz=_ZONE)
    cron, _trigger = make_service(
        tmp_path,
        tz=_ZONE,
        calendar=calendar,
        agent_resolver=SimpleNamespace(resolve_agent=resolve_agent),
    )
    event = calendar.create_event(**_SINGLE)
    job = await cron.create_job(
        agent_id="agent-one",
        prompt=_PROMPT,
        **_schedule(cron, event.id, "start"),
    )
    # The event passed without a Run, so the job is history and the Agent could go.
    clock.now = _local("2030-01-11T09:00")
    assert cron.can_fire(job) is False
    if agent_gone:
        removed.add("agent-one")

    move = calendar.update_event(event.id, start="2030-01-20T15:00", actor="test")

    if agent_gone:
        with pytest.raises(EventJobTargetMissingError) as refused:
            await move
        assert refused.value.problems == ((job.id, "Agent agent-one no longer exists"),)
        assert calendar.get_event(event.id).start == "2030-01-10T15:00:00"
    else:
        await move
        assert cron.can_fire(job) is True


@pytest.mark.asyncio
async def test_projection_shows_each_due_time_with_its_event_and_occurrence(
    services: _Services,
) -> None:
    event = services.calendar.create_event(
        title="Standup", start="2030-01-07T09:00", rrule="FREQ=WEEKLY;BYDAY=MO"
    )
    job = await _event_job(services, event.id, "start - 15m")

    rows = services.cron.project_occurrences(_local("2030-01-07T00:00"), _local("2030-01-15T00:00"))

    assert rows == [
        CronOccurrence(
            job_id=job.id,
            name=job.name,
            fire_at_utc=_local(f"2030-01-{day}T08:45"),
            schedule_type="event",
            event_id=event.id,
            occurrence_id=f"{event.id}_203001{day}T0900",
        )
        for day in ("07", "14")
    ]
    assert services.cron.next_fire_at(job) == _local("2030-01-07T08:45").isoformat()
