"""Calendar action contracts across edits, recurrence, admission, and restarts."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import override
from unittest.mock import AsyncMock, Mock

import pytest

from core.calendar import (
    CalendarActionTargetMissingError,
    CalendarEventNotFoundError,
    CalendarService,
    CalendarStorageError,
    CalendarValidationError,
)
from core.calendar import actions as actions_module
from core.calendar.actions import (
    action_message,
    parse_action_when,
    validate_calendar_actions_file,
)
from core.projects import (
    AgentResolutionError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
)
from core.runs import RunKind, RunStatus
from core.sessions import SessionNotFoundError


def setup(
    tmp_path: Path,
    *,
    start: datetime | None = None,
    recurring: bool = False,
    now: datetime | None = None,
):
    now = now or datetime.now(UTC)
    service = CalendarService(tmp_path, tz="Europe/Berlin")
    event = service.create_event(
        title="Meeting",
        start=(start or now + timedelta(minutes=30)).isoformat(),
        rrule={"freq": "daily", "count": 3} if recurring else None,
    )
    trigger = Mock(
        trigger_run=AsyncMock(
            side_effect=lambda *a, **kw: SimpleNamespace(
                id="run-1", session_id=a[2] or "new-session", wait=AsyncMock()
            )
        )
    )
    service.actions.configure(trigger, Mock(), session_manager())
    return service, event, trigger, now


def session_manager() -> Mock:
    """Session manager double whose ``run_async`` runs the work like the real pool."""
    sessions = Mock(exists=Mock(return_value=True))
    sessions.run_async = AsyncMock(
        side_effect=lambda function, *args, **kwargs: function(*args, **kwargs)
    )
    return sessions


def window(service: CalendarService, now: datetime):
    return service.occurrences_in_window(now - timedelta(days=2), now + timedelta(days=5))


async def drain(service: CalendarService):
    await asyncio.gather(*list(service.actions._workers.values()))


@pytest.mark.parametrize(
    ("value", "anchor", "minutes"),
    [
        ("start", "start", 0),
        ("end + 30m", "end", 30),
        ("start - 1h", "start", -60),
        ("end - 2d", "end", -2880),
    ],
)
def test_relative_grammar(value, anchor, minutes):
    assert parse_action_when(value)[:2] == (anchor, minutes)


@pytest.mark.parametrize(
    "value", [None, 12, "tomorrow", "in 1h", "start + 1s", "end - 32d", "start + 0m"]
)
def test_relative_grammar_rejects_unsupported_values(value):
    with pytest.raises(CalendarValidationError):
        parse_action_when(value)


@pytest.mark.asyncio
async def test_edit_preserves_event_id_and_moves_actions(tmp_path):
    service, event, _, now = setup(tmp_path)
    before = await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    after = await service.actions.add(event.id, when="end + 30m", prompt="review", target="main")
    initial = service.actions.project(window(service, now))
    updated = await service.update_event(
        event.id, start=(now + timedelta(hours=3)).isoformat(), duration_minutes=120
    )
    assert updated.id == event.id
    rows = service.actions.project(window(service, now))
    assert {row["action_id"] for row in rows} == {before["id"], after["id"]}
    assert rows[0]["scheduled_at"] != initial[0]["scheduled_at"]
    assert datetime.fromisoformat(rows[0]["expires_at"]) == now + timedelta(hours=3)
    # Due after a single event, it can start however late.
    assert rows[1]["expires_at"] is None
    reloaded = CalendarService(tmp_path, tz="Europe/Berlin")
    assert reloaded.get_event(event.id).id == event.id
    assert reloaded.actions.list_actions() == service.actions.list_actions()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recurring", "when", "closes"),
    [
        # The event starts in 30 minutes and lasts an hour.
        pytest.param(False, "start - 1h", timedelta(minutes=30), id="before-start"),
        pytest.param(False, "start", timedelta(minutes=90), id="during"),
        pytest.param(False, "end + 30m", None, id="after-a-single-event"),
        # The next daily occurrence replaces a follow-up of this one.
        pytest.param(True, "end", timedelta(days=1, minutes=30), id="after-in-a-series"),
    ],
)
async def test_windows_close_with_the_event_or_its_next_occurrence(
    tmp_path, recurring, when, closes
):
    # Whole minutes: a series keeps its start to the second.
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    service, event, _, now = setup(tmp_path, recurring=recurring, now=now)
    await service.actions.add(event.id, when=when, prompt="test", target="main")
    row = service.actions.project(window(service, now))[0]
    expires = row["expires_at"]
    assert (datetime.fromisoformat(expires) - now if expires else None) == closes


@pytest.mark.asyncio
async def test_actions_waiting_for_a_worker_slot_fire_exactly_once(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    for index in range(6):
        await service.actions.add(event.id, when="start - 1h", prompt=f"p{index}", target="main")
    for step in range(4):
        await service.actions.tick(now + timedelta(seconds=step))
        await drain(service)
    assert trigger.trigger_run.await_count == 6
    assert [row["status"] for row in service.actions._executions.values()] == ["completed"] * 6
    stored = json.loads(service.actions._path.read_text(encoding="utf-8"))["executions"]
    assert {row["status"] for row in stored.values()} == {"completed"}


@pytest.mark.asyncio
async def test_withdrawn_worker_is_redispatched_and_fires_once(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    waiting = asyncio.Event()
    calls = 0

    async def first_call_waits(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            waiting.set()
            await asyncio.Event().wait()
        return SimpleNamespace(id="run-1", session_id="new-session", wait=AsyncMock())

    trigger.trigger_run.side_effect = first_call_waits
    await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await waiting.wait()
    # An event change withdraws work that is still awaiting admission.
    await service.update_event(event.id, title="Renamed")
    await asyncio.gather(*list(service.actions._workers.values()), return_exceptions=True)
    assert service.actions.project(window(service, now))[0]["status"] == "pending"
    for step in range(1, 4):
        await service.actions.tick(now + timedelta(seconds=step))
        await drain(service)
    assert trigger.trigger_run.await_count == 2
    assert service.actions.project(window(service, now))[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_finished_history_is_pruned_after_retention_without_refiring(tmp_path, monkeypatch):
    service, event, trigger, now = setup(tmp_path)
    await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await drain(service)
    await service.actions.tick(now + timedelta(days=29))
    assert [row["status"] for row in service.actions._executions.values()] == ["completed"]
    later = now + timedelta(days=31)

    class Clock(datetime):
        @classmethod
        @override
        def now(cls, tz=None):
            return later

    monkeypatch.setattr("core.calendar.actions.datetime", Clock)
    for step in range(2):
        await service.actions.tick(later + timedelta(minutes=step))
    assert json.loads(service.actions._path.read_text(encoding="utf-8"))["executions"] == {}
    assert trigger.trigger_run.await_count == 1
    # Unknown history is omitted rather than reported as missed.
    assert service.actions.project(window(service, now)) == []


@pytest.mark.asyncio
async def test_deleted_action_history_is_pruned(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    action = await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    kept = await service.actions.add(event.id, when="start - 45m", prompt="keep", target="main")
    await service.actions.tick(now)
    await drain(service)
    await service.actions.delete(action["id"])
    await service.actions.tick(now + timedelta(seconds=1))
    stored = json.loads(service.actions._path.read_text(encoding="utf-8"))["executions"]
    assert [row["action_id"] for row in stored.values()] == [kept["id"]]
    assert trigger.trigger_run.await_count == 2


@pytest.mark.asyncio
async def test_single_action_fires_once_and_rearms_only_after_event_moves(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await drain(service)
    await service.actions.tick(now + timedelta(seconds=1))
    assert trigger.trigger_run.await_count == 1
    # Without a selected session the action requests a new one.
    assert trigger.trigger_run.call_args.args[2] is None
    old = service.actions.project(window(service, now))[0]
    assert old["status"] == "completed"
    assert old["session"] == "new-session"
    await service.update_event(event.id, title="Renamed")
    await service.actions.tick(now)
    assert trigger.trigger_run.await_count == 1
    moved_start = now + timedelta(minutes=45)
    await service.update_event(event.id, start=moved_start.isoformat())
    projected = service.actions.project(window(service, now))[0]
    assert projected["status"] == "pending"
    assert datetime.fromisoformat(projected["scheduled_at"]) == moved_start - timedelta(hours=1)
    await service.actions.tick(now)
    await drain(service)
    assert trigger.trigger_run.await_count == 2
    reloaded = CalendarService(tmp_path, tz="Europe/Berlin")
    reloaded.actions.configure(trigger, Mock(), session_manager())
    await reloaded.actions.tick(now)
    assert trigger.trigger_run.await_count == 2
    row = reloaded.actions.project(window(reloaded, now))[0]
    assert row["status"] == "completed"
    assert row["scheduled_at"] == projected["scheduled_at"]


@pytest.mark.asyncio
async def test_timezone_change_does_not_rearm_unchanged_single_instant(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    action = await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await drain(service)
    initial = service.actions.project(window(service, now))[0]
    service.set_timezone("America/New_York")
    await service.actions.tick(now)
    assert trigger.trigger_run.await_count == 1
    projected = service.actions.project(window(service, now))[0]
    assert projected["status"] == "completed"
    assert projected["scheduled_at"] == initial["scheduled_at"]
    # An explicit change of the action's due time does rearm it.
    await service.actions.update(action["id"], when="start - 45m")
    await service.actions.tick(now)
    await drain(service)
    assert trigger.trigger_run.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("recurring", [False, True])
async def test_move_during_admitted_run_keeps_claim_until_completion(
    tmp_path, monkeypatch, recurring
):
    now = datetime(2030, 10, 5, 10, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        @override
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(actions_module, "datetime", Clock)
    service, event, trigger, now = setup(tmp_path, now=now, recurring=recurring)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def wait_for_finish():
        entered.set()
        await finish.wait()

    trigger.trigger_run.side_effect = None
    trigger.trigger_run.return_value = SimpleNamespace(
        id="run-1", session_id="new-session", wait=wait_for_finish
    )
    await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main", now=now)
    try:
        await service.actions.tick(now)
        # Reconciliation before the scheduled worker starts must retain its pending row.
        await service.actions.tick(now)
        assert len(service.actions._executions) == 1
        await entered.wait()
        original = next(iter(service.actions._executions.values())).copy()
        await service.update_event(event.id, start=(now + timedelta(minutes=45)).isoformat())
        await service.actions.tick(now)
        assert next(iter(service.actions._executions.values())) == original
        assert trigger.trigger_run.await_count == 1
        assert len(service.actions._workers) == 1
        assert service.actions.project(window(service, now))[0]["status"] == "pending"
        finish.set()
        await drain(service)
        await service.actions.tick(now)
        await drain(service)
        assert trigger.trigger_run.await_count == 2
        await service.actions.tick(now)
        assert trigger.trigger_run.await_count == 2
        if recurring:
            now += timedelta(days=1)
            await service.actions.tick(now)
            await drain(service)
            assert trigger.trigger_run.await_count == 3
    finally:
        finish.set()
        await service.actions.aclose()


@pytest.mark.asyncio
async def test_expired_and_excluded_occurrences_never_fire(tmp_path):
    service, event, trigger, now = setup(
        tmp_path, start=datetime.now(UTC) - timedelta(hours=2), recurring=True
    )
    await service.actions.add(
        event.id, when="start - 1h", prompt="prepare", target="main", now=now - timedelta(days=1)
    )
    occurrences = window(service, now)
    service.add_exdate(event.id, occurrences[1].occurrence_start)
    await service.actions.tick(now)
    assert trigger.trigger_run.await_count == 0
    rows = service.actions.project(window(service, now))
    assert rows[0]["status"] == "missed"
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_selected_session_and_project_are_preserved(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    # An edit's target and Session reads are blocking; they run on the Session
    # pool, never on the Event Loop.
    reading_threads = []
    sessions, resolver = service.actions._sessions, service.actions._resolver

    async def on_the_pool(function, *args):
        return await asyncio.to_thread(function, *args)

    def read(*_args):
        reading_threads.append(threading.current_thread())
        return True

    sessions.run_async.side_effect = on_the_pool
    resolver.resolve_agent.side_effect = read
    sessions.exists.side_effect = read
    added = await service.actions.add(
        event.id, when="start - 1h", prompt="draft", target="builder@project", session="chosen"
    )
    await service.actions.update(added["id"], prompt="prepare")
    # Each edit resolved the target and checked the Session.
    assert len(reading_threads) == 4
    assert threading.current_thread() not in reading_threads
    await service.actions.tick(now)
    await drain(service)
    call = trigger.trigger_run.call_args
    assert call.args[0] == "builder"
    assert call.args[2] == "chosen"
    assert call.kwargs["project_id"] == "project"
    message = call.args[1]
    action_id = service.actions.list_actions()[0]["id"]
    assert message.startswith(
        f'Calendar action {action_id} is due (start - 1h) for "Meeting" (event {event.id}).\n'
        "Event time: "
    )
    assert message.endswith("(Europe/Berlin)\n\nInstruction:\nprepare")


def test_action_message_names_all_day_span_and_notes(tmp_path):
    service = CalendarService(tmp_path, tz="Europe/Berlin")
    event = service.create_event(
        title="Trip", start="2030-01-10", duration_days=3, notes="Pack the charger."
    )
    [occurrence] = service.event_occurrences(
        event, datetime(2030, 1, 9, tzinfo=UTC), datetime(2030, 1, 14, tzinfo=UTC)
    )
    action = {"id": "act_1", "when": "start - 1d", "prompt": "Check the trains."}

    assert action_message(action, event, occurrence, "Europe/Berlin") == (
        f'Calendar action act_1 is due (start - 1d) for "Trip" (event {event.id}).\n'
        "Event time: 2030-01-10 to 2030-01-12, all day (Europe/Berlin)\n"
        "Event notes: Pack the charger.\n"
        "\n"
        "Instruction:\n"
        "Check the trains."
    )


def test_action_message_shows_timed_span_in_minutes(tmp_path):
    service = CalendarService(tmp_path, tz="Europe/Berlin")
    event = service.create_event(title="Dentist", start="2030-01-10T15:00", duration_minutes=45)
    [occurrence] = service.event_occurrences(
        event, datetime(2030, 1, 10, tzinfo=UTC), datetime(2030, 1, 11, tzinfo=UTC)
    )

    message = action_message(
        {"id": "act_2", "when": "start", "prompt": "Go."}, event, occurrence, "Europe/Berlin"
    )

    assert "Event time: 2030-01-10T15:00 to 2030-01-10T15:45 (Europe/Berlin)\n\n" in message
    assert "Event notes" not in message


@pytest.mark.asyncio
async def test_event_delete_withdraws_queued_action_and_its_definition(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    waiting = asyncio.Event()
    cancelled = asyncio.Event()

    async def busy(*args, **kwargs):
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    trigger.trigger_run.side_effect = busy
    await service.actions.add(
        event.id, when="start - 1h", prompt="prepare", target="main", session="busy"
    )
    await service.actions.tick(now)
    await waiting.wait()
    service.delete_event(event.id)
    await cancelled.wait()
    await asyncio.sleep(0)
    await service.actions.tick(now + timedelta(seconds=1))
    assert service.actions.list_actions() == []
    assert json.loads((tmp_path / "calendar" / "actions.json").read_text())["actions"] == []
    assert not service.actions._workers


@pytest.mark.asyncio
async def test_all_day_deadlines_respect_dst(tmp_path):
    service = CalendarService(tmp_path, tz="Europe/Berlin")
    event = service.create_event(title="Day", start="2026-10-25")
    await service.actions.add(event.id, when="start", prompt="prepare", target="main")
    occurrences = service.occurrences_in_window(*service.parse_window("2026-10-25", "2026-10-25"))
    row = service.actions.project(occurrences)[0]
    assert datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(
        row["scheduled_at"]
    ) == timedelta(hours=25)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recurring", "stored_max_delay", "statuses"),
    [
        pytest.param(False, None, ["completed"], id="single-event"),
        # Each earlier follow-up closed when the next occurrence started.
        pytest.param(True, None, ["missed", "missed", "completed"], id="series"),
        # Earlier versions stored a limit that closed the window an hour after the due time.
        pytest.param(False, 3600, ["completed"], id="stored-max-delay"),
    ],
)
async def test_a_follow_up_missed_while_vbot_was_off_starts_once_late(
    tmp_path, monkeypatch, recurring, stored_max_delay, statuses
):
    now = datetime(2026, 10, 8, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(actions_module, "_utc_now", lambda: now)
    # The last occurrence ended a day ago, 09:00 Berlin; vBot was off since before the first.
    first_start = now - timedelta(days=3 if recurring else 1, hours=2)
    service, event, trigger, _ = setup(tmp_path, start=first_start, recurring=recurring, now=now)
    await service.actions.add(
        event.id,
        when="end",
        prompt="Send the minutes",
        target="main",
        now=first_start - timedelta(days=1),
    )
    if stored_max_delay is not None:
        path = tmp_path / "calendar" / "actions.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["actions"][0]["max_delay_seconds"] = stored_max_delay
        path.write_text(json.dumps(document), encoding="utf-8")
        service = CalendarService(tmp_path, tz="Europe/Berlin")
        service.actions.configure(trigger, Mock(), session_manager())

    await service.actions.tick(now)
    await drain(service)

    rows = service.actions.project(
        service.occurrences_in_window(first_start - timedelta(days=1), now)
    )
    assert [row["status"] for row in rows] == statuses
    if statuses[-1] != "completed":
        trigger.trigger_run.assert_not_awaited()
        return
    trigger.trigger_run.assert_awaited_once()
    notice = trigger.trigger_run.await_args.kwargs["context_note"]
    assert f"Calendar action {rows[-1]['action_id']} was due at 2026-10-07T09:00+02:00" in notice
    assert "starting late, at 2026-10-08T10:00+02:00" in notice


@pytest.mark.asyncio
async def test_an_action_added_after_its_event_ended_does_not_run_for_it(tmp_path):
    service, event, trigger, now = setup(tmp_path, start=datetime.now(UTC) - timedelta(hours=3))
    action = await service.actions.add(event.id, when="end", prompt="review", target="main")

    await service.actions.tick(now)

    trigger.trigger_run.assert_not_awaited()
    assert service.actions.can_fire(action["id"], now=now) is False


@pytest.mark.asyncio
async def test_timeout_after_admission_is_failed_not_missed(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    trigger.trigger_run.side_effect = None
    trigger.trigger_run.return_value = SimpleNamespace(
        id="timeout-run",
        session_id="new-session",
        status=RunStatus.FAILED,
        wait=AsyncMock(side_effect=TimeoutError),
    )
    await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await drain(service)
    row = service.actions.project(window(service, now))[0]
    assert row["status"] == "failed"
    assert row["run_id"] == "timeout-run"
    assert trigger.trigger_run.call_args.kwargs["run_kind"] == RunKind.CALENDAR
    await service.actions.tick(now)
    assert trigger.trigger_run.await_count == 1


@pytest.mark.asyncio
async def test_an_occurrence_that_expires_while_its_claim_is_saved_is_missed(tmp_path, monkeypatch):
    service, event, trigger, now = setup(tmp_path)
    clock = [now]
    monkeypatch.setattr(actions_module, "_utc_now", lambda: clock[0])
    save = service.actions._save_async

    async def slow_save():
        if any(row["status"] == "claimed" for row in service.actions._executions.values()):
            clock[0] = now + timedelta(minutes=31)  # Past the event start, its expiry.
        await save()

    monkeypatch.setattr(service.actions, "_save_async", slow_save)
    await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await drain(service)

    trigger.trigger_run.assert_not_awaited()
    assert service.actions.project(window(service, now))[0]["status"] == "missed"


def _session_gone(service, trigger):
    service.actions._sessions.exists.return_value = False


def _session_gone_at_admission(service, trigger):
    trigger.trigger_run.side_effect = SessionNotFoundError("session does not exist: chosen")


def _target_gone(service, trigger):
    service.actions._resolver.resolve_agent.side_effect = ResolutionAgentNotFoundError("gone")


def _target_cannot_run(service, trigger):
    service.actions._resolver.resolve_agent.side_effect = AgentResolutionError("no usable model")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "reason"),
    [
        pytest.param(
            _session_gone,
            "Session does not exist for calendar target main: chosen",
            id="selected-session-missing",
        ),
        # The Session can disappear between validation and admission.
        pytest.param(
            _session_gone_at_admission,
            "Session does not exist for calendar target main: chosen",
            id="selected-session-missing-at-admission",
        ),
        pytest.param(_target_gone, "Calendar target does not exist: main", id="target-missing"),
        pytest.param(
            _target_cannot_run,
            "Calendar target main cannot run: no usable model",
            id="target-cannot-run",
        ),
    ],
)
async def test_occurrence_that_cannot_start_its_run_records_why(tmp_path, caplog, arrange, reason):
    service, event, trigger, now = setup(tmp_path)
    await service.actions.add(
        event.id, when="start - 1h", prompt="prepare", target="main", session="chosen"
    )
    arrange(service, trigger)

    with caplog.at_level(logging.WARNING, logger="vbot.calendar.actions"):
        await service.actions.tick(now)
        await drain(service)

    row = service.actions.project(window(service, now))[0]
    assert (row["status"], row["error"]) == ("failed", reason)
    stored = json.loads((tmp_path / "calendar" / "actions.json").read_text())["executions"]
    assert [item["error"] for item in stored.values()] == [reason]
    # An expected state: one WARNING naming the reason, without a traceback.
    [record] = [item for item in caplog.records if item.levelno >= logging.WARNING]
    assert record.levelno == logging.WARNING
    assert reason in record.getMessage()
    assert record.exc_info is None


def _at(delay: timedelta):
    async def arrange(service, event, trigger, now):
        return now + delay

    return arrange


async def _fired(service, event, trigger, now):
    await service.actions.tick(now + timedelta(minutes=31))
    await drain(service)
    return now + timedelta(minutes=45)


async def _fired_ahead_of_its_event(service, event, trigger, now):
    await service.actions.tick(now + timedelta(minutes=11))
    await drain(service)
    # The event itself still lies ahead.
    return now + timedelta(minutes=12)


async def _starting(service, event, trigger, now):
    admitting = asyncio.Event()

    async def admission_in_progress(*args, **kwargs):
        admitting.set()
        await asyncio.Event().wait()

    trigger.trigger_run.side_effect = admission_in_progress
    await service.actions.tick(now + timedelta(minutes=31))
    await admitting.wait()
    return now + timedelta(hours=2)


async def _yearly(service, event, trigger, now):
    await service.update_event(event.id, rrule={"freq": "yearly"})
    return now + timedelta(days=40)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recurring", "when", "arrange", "can_fire"),
    [
        pytest.param(False, "start", _at(timedelta()), True, id="upcoming"),
        # Due and within its expiry, but not started yet.
        pytest.param(False, "start", _at(timedelta(minutes=45)), True, id="due"),
        pytest.param(False, "start", _starting, True, id="starting-its-run"),
        pytest.param(False, "start", _fired, False, id="fired"),
        pytest.param(
            False, "start - 20m", _fired_ahead_of_its_event, False, id="fired-ahead-of-its-event"
        ),
        pytest.param(False, "start", _at(timedelta(hours=2)), False, id="expired-unused"),
        pytest.param(False, "end", _at(timedelta(days=30)), True, id="due-after-its-event"),
        pytest.param(True, "start", _fired, True, id="series-continues"),
        pytest.param(
            True, "start - 20m", _fired_ahead_of_its_event, True, id="series-continues-ahead"
        ),
        pytest.param(True, "start", _at(timedelta(days=3, hours=2)), False, id="series-ended"),
        # The next occurrence lies far beyond any scan window.
        pytest.param(False, "start", _yearly, True, id="next-occurrence-next-year"),
    ],
)
async def test_an_action_can_fire_until_its_occurrences_are_used_up(
    tmp_path, recurring, when, arrange, can_fire
):
    service, event, trigger, now = setup(tmp_path, recurring=recurring)
    action = await service.actions.add(event.id, when=when, prompt="prepare", target="main")

    at = await arrange(service, event, trigger, now)

    assert service.actions.can_fire(action["id"], now=at) is can_fire
    await service.actions.aclose()


def _resolution_fails(error):
    def arrange(service):
        service.actions._resolver.resolve_agent.side_effect = error

    return arrange


def _session_deleted(service):
    service.actions._sessions.exists.return_value = False


@pytest.mark.parametrize(
    ("spent", "target", "arrange", "problem"),
    [
        pytest.param(
            True,
            "main",
            _session_deleted,
            "Session chosen of main no longer exists",
            id="selected-session-gone",
        ),
        pytest.param(
            True,
            "main",
            _resolution_fails(ResolutionAgentNotFoundError("main")),
            "Agent main no longer exists",
            id="agent-gone",
        ),
        pytest.param(
            True,
            "builder@vbot",
            _resolution_fails(ResolutionProjectNotFoundError("vbot")),
            "Project vbot no longer exists",
            id="project-gone",
        ),
        # A target that exists but cannot run now can recover; its occurrence records why.
        pytest.param(
            True,
            "main",
            _resolution_fails(AgentResolutionError("no usable model")),
            None,
            id="target-cannot-run",
        ),
        # An action that can still fire was checked by every removal; moving it revives nothing.
        pytest.param(False, "main", _session_deleted, None, id="action-still-live"),
    ],
)
@pytest.mark.asyncio
async def test_an_event_change_that_revives_an_action_checks_its_target(
    tmp_path, spent, target, arrange, problem
):
    now = datetime.now(UTC)
    start = now - timedelta(hours=3) if spent else now + timedelta(minutes=30)
    service, event, _, _ = setup(tmp_path, start=start)
    action = await service.actions.add(
        event.id, when="start", prompt="prepare", target=target, session="chosen"
    )
    arrange(service)
    later = (now + timedelta(days=1)).isoformat()
    # The target reads are blocking; they run on the Session pool, never on the Event Loop.
    reading_threads = []
    sessions, resolver = service.actions._sessions, service.actions._resolver
    failure = resolver.resolve_agent.side_effect

    async def on_the_pool(function, *args):
        return await asyncio.to_thread(function, *args)

    def resolve(*_args):
        reading_threads.append(threading.current_thread())
        if failure is not None:
            raise failure

    sessions.run_async.side_effect = on_the_pool
    resolver.resolve_agent.side_effect = resolve

    if problem is None:
        assert (await service.update_event(event.id, start=later)).start_utc != event.start_utc
    else:
        with pytest.raises(CalendarActionTargetMissingError) as refused:
            await service.update_event(event.id, start=later)
        assert str(refused.value) == (
            f"This event change would let an action run again whose target no longer exists: "
            f"{action['id']} ({problem}). Change each action's target or Session, or delete the "
            "action, first."
        )
        assert service.get_event(event.id) == event
    # Only a revived action is checked.
    assert bool(reading_threads) is spent
    assert threading.current_thread() not in reading_threads
    await service.actions.aclose()


@pytest.mark.asyncio
async def test_an_event_deleted_while_a_revival_is_checked_stays_deleted(tmp_path):
    now = datetime.now(UTC)
    service, event, _, _ = setup(tmp_path, start=now - timedelta(hours=3))
    await service.actions.add(event.id, when="start", prompt="prepare", target="main")
    sessions = service.actions._sessions
    on_the_pool = sessions.run_async.side_effect

    async def deleted_meanwhile(function, *args):
        service.delete_event(event.id)
        return on_the_pool(function, *args)

    sessions.run_async.side_effect = deleted_meanwhile

    with pytest.raises(CalendarEventNotFoundError):
        await service.update_event(event.id, start=(now + timedelta(days=1)).isoformat())
    assert service.list_events() == []


@pytest.mark.asyncio
async def test_cancel_before_worker_starts_releases_capacity(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    tasks = list(service.actions._workers.values())
    await service.update_event(event.id, start=(now + timedelta(days=2)).isoformat())
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    await service.actions.tick(now)
    assert not service.actions._workers
    assert trigger.trigger_run.await_count == 0
    assert service.actions.project(window(service, now))[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_uncertain_admission_is_never_replayed(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    persisted = asyncio.Event()

    async def admitting(*args, **kwargs):
        kwargs["input_persisted_hook"]()
        persisted.set()
        await asyncio.Event().wait()

    trigger.trigger_run.side_effect = admitting
    await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await persisted.wait()
    await service.actions.aclose()
    assert service.actions.project(window(service, now))[0]["status"] == "interrupted"
    reloaded = CalendarService(tmp_path, tz="Europe/Berlin")
    reloaded.actions.configure(trigger, Mock(), session_manager())
    await reloaded.actions.tick(now)
    assert trigger.trigger_run.await_count == 1


@pytest.mark.asyncio
async def test_restart_recovers_terminal_run_from_session(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await drain(service)
    row = next(iter(service.actions._executions.values()))
    row["status"] = "running"
    service.actions._save()
    sessions = session_manager()
    # The Run's outcome is read from its Session on the Session pool, never on
    # the Event Loop.
    reading_threads = []

    def read(result):
        def reading(*_args, **_kwargs):
            reading_threads.append(threading.current_thread())
            return result

        return reading

    async def on_the_pool(function, *args):
        return await asyncio.to_thread(function, *args)

    session = Mock(find_run_summary=Mock(side_effect=read(SimpleNamespace(status="completed"))))
    sessions.run_async.side_effect = on_the_pool
    sessions.exists.side_effect = read(True)
    sessions.get.side_effect = read(session)
    reloaded = CalendarService(tmp_path, tz="Europe/Berlin")
    reloaded.actions.configure(trigger, Mock(), sessions)
    await reloaded.actions.tick(now)
    assert reloaded.actions.project(window(reloaded, now))[0]["status"] == "completed"
    session.find_run_summary.assert_called_once_with(run_id="run-1")
    assert len(reading_threads) == 3
    assert threading.current_thread() not in reading_threads
    assert trigger.trigger_run.await_count == 1


@pytest.mark.parametrize("started", [False, True], ids=["before-start", "on-the-loop"])
@pytest.mark.asyncio
async def test_identity_retarget_moves_actions_and_the_rows_that_follow_them(tmp_path, started):
    service, event, _, now = setup(tmp_path)

    async def retarget(source, destination):
        # A rename completed at startup blocks; a live one awaits on the Event Loop.
        if started:
            return await service.actions.retarget_identity_async(source, destination)
        return service.actions.retarget_identity(source, destination)

    action = await service.actions.add(
        event.id, when="start - 1h", prompt="prepare", target="coder"
    )
    await service.actions.tick(now)
    await drain(service)
    ((ran_key, ran),) = service.actions._executions.items()
    # Only the Session the Run used moves with the renamed Agent.
    service.actions._sessions.existing_addresses = Mock(
        side_effect=lambda addresses: {a for a in addresses if a.session_id == ran["session"]}
    )
    rows = {
        # Without a Session a row follows its action; with one it follows the Session.
        "missed": {"action_id": action["id"], "target": "coder", "session": None},
        "stayed": {"action_id": action["id"], "target": "coder", "session": "archived"},
        "deleted-action": {"action_id": "gone", "target": "coder", "session": None},
        # Left by an earlier ``researcher``: reverting the rename must not adopt it.
        "leftover": {"action_id": action["id"], "target": "researcher", "session": "old"},
    }
    for key, fields in rows.items():
        service.actions._executions[key] = {
            **ran,
            **fields,
            "id": key,
            "run_id": None,
            "status": "missed",
        }

    def targets() -> dict[str, str]:
        stored = json.loads(service.actions._path.read_text(encoding="utf-8"))["executions"]
        return {key: row["target"] for key, row in stored.items()}

    assert await retarget("coder", "researcher") == 1
    assert service.actions.list_actions()[0]["target"] == "researcher"
    assert targets() == {
        ran_key: "researcher",
        "missed": "researcher",
        "stayed": "coder",
        "deleted-action": "coder",
        "leftover": "researcher",
    }

    assert await retarget("researcher", "coder") == 1
    assert service.actions.list_actions()[0]["target"] == "coder"
    assert targets() == {
        ran_key: "coder",
        "missed": "coder",
        "stayed": "coder",
        "deleted-action": "coder",
        "leftover": "researcher",
    }


@pytest.mark.asyncio
async def test_failed_write_rolls_back_action_mutations(tmp_path, monkeypatch):
    service, event, _, _ = setup(tmp_path)
    action = await service.actions.add(event.id, when="start", prompt="prepare", target="main")
    monkeypatch.setattr(service.actions, "_write", Mock(side_effect=CalendarStorageError("disk")))
    with pytest.raises(CalendarStorageError):
        await service.actions.update(action["id"], prompt="changed")
    with pytest.raises(CalendarStorageError):
        await service.actions.delete(action["id"])
    with pytest.raises(CalendarStorageError):
        await service.actions.add(event.id, when="end", prompt="new", target="main")
    assert service.actions.list_actions() == [action]


@pytest.mark.asyncio
async def test_recurrences_each_request_a_fresh_session(tmp_path, monkeypatch):
    now = datetime(2030, 10, 5, 10, tzinfo=UTC)
    service, event, trigger, now = setup(tmp_path, recurring=True, now=now)
    await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main", now=now)
    entered = [asyncio.Event(), asyncio.Event()]
    finish = asyncio.Event()

    async def admit(*args, **kwargs):
        index = trigger.trigger_run.await_count - 1

        async def wait():
            if index < 2:
                entered[index].set()
            if index == 0:
                await finish.wait()

        return SimpleNamespace(id=f"run-{index}", session_id=f"session-{index}", wait=wait)

    trigger.trigger_run.side_effect = admit
    try:
        for day in range(3):
            clock = now + timedelta(days=day)

            class Clock(datetime):
                @classmethod
                @override
                def now(cls, tz=None, clock=clock):
                    return clock

            monkeypatch.setattr("core.calendar.actions.datetime", Clock)
            await service.actions.tick(clock)
            if day < 2:
                await entered[day].wait()
            if day == 0:
                continue
            # An unchanged later occurrence can start while the first Run is active.
            finish.set()
            await drain(service)
        assert trigger.trigger_run.await_count == 3
        assert all(call.args[2] is None for call in trigger.trigger_run.call_args_list)
    finally:
        finish.set()
        await service.actions.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "broken",
        json.dumps({"actions": [], "executions": {}}),
        json.dumps({"format_version": 2, "actions": [], "executions": {}}),
        json.dumps({"format_version": 1, "actions": {}, "executions": {}}),
        json.dumps({"format_version": 1, "actions": []}),
        json.dumps({"format_version": 1, "actions": [], "executions": []}),
    ],
    ids=[
        "not-json",
        "no-version",
        "newer-version",
        "actions-not-a-list",
        "no-executions",
        "executions-not-an-object",
    ],
)
async def test_unreadable_store_fails_closed(tmp_path, content):
    path = tmp_path / "calendar" / "actions.json"
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")
    service = CalendarService(tmp_path, tz="UTC")
    assert service.actions.list_actions() == []
    assert service.actions.storage_error
    with pytest.raises(CalendarStorageError):
        await service.actions.delete("unused")
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.asyncio
async def test_unknown_action_fields_are_hidden_and_kept_on_save(tmp_path):
    service, event, _, _ = setup(tmp_path)
    action = await service.actions.add(event.id, when="start", prompt="prepare", target="main")
    path = tmp_path / "calendar" / "actions.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["future_setting"] = 1
    payload["actions"][0]["priority"] = "high"
    path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = CalendarService(tmp_path, tz="Europe/Berlin")
    assert "priority" not in reopened.actions.list_actions()[0]
    await reopened.actions.update(action["id"], prompt="prepare slides")

    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten["format_version"] == 1
    assert rewritten["future_setting"] == 1
    assert rewritten["actions"][0]["prompt"] == "prepare slides"
    assert rewritten["actions"][0]["priority"] == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize("whole_file", [True, False])
async def test_invalid_event_storage_never_deletes_action_definitions(tmp_path, whole_file):
    service, event, _, now = setup(tmp_path)
    action = await service.actions.add(event.id, when="start", prompt="prepare", target="main")
    path = tmp_path / "calendar" / "events.json"
    path.write_text(
        "broken" if whole_file else json.dumps({"format_version": 1, "events": [{"id": event.id}]})
    )
    reloaded = CalendarService(tmp_path, tz="UTC")
    if whole_file:
        with pytest.raises(CalendarStorageError):
            await reloaded.actions.tick(now)
    else:
        await reloaded.actions.tick(now)
    stored = json.loads(service.actions._path.read_text())
    assert stored["actions"][0]["id"] == action["id"]


@pytest.mark.asyncio
async def test_invalid_event_recurrence_does_not_block_other_actions(tmp_path):
    service, broken_event, trigger, now = setup(tmp_path, recurring=True)
    broken_action = await service.actions.add(
        broken_event.id, when="start - 1h", prompt="broken", target="main"
    )
    valid_event = service.create_event(
        title="Valid", start=(now + timedelta(minutes=30)).isoformat()
    )
    valid_action = await service.actions.add(
        valid_event.id, when="start - 1h", prompt="valid", target="main"
    )
    path = tmp_path / "calendar" / "events.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    next(event for event in payload["events"] if event["id"] == broken_event.id)["rrule"] = {}
    path.write_text(json.dumps(payload), encoding="utf-8")

    restarted = CalendarService(tmp_path, tz="Europe/Berlin")
    restarted.actions.configure(trigger, Mock(), session_manager())
    await restarted.actions.tick(now)
    await drain(restarted)

    trigger.trigger_run.assert_awaited_once()
    assert valid_action["id"] in trigger.trigger_run.call_args.args[1]
    stored = json.loads((tmp_path / "calendar" / "actions.json").read_text(encoding="utf-8"))
    assert {action["id"] for action in stored["actions"]} == {
        broken_action["id"],
        valid_action["id"],
    }
    assert {row["action_id"] for row in stored["executions"].values()} == {valid_action["id"]}


@pytest.mark.asyncio
async def test_short_action_ids_skip_collisions(tmp_path, monkeypatch):
    from core.utils import ids

    service, event, _, _ = setup(tmp_path)
    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    first = await service.actions.add(event.id, when="start", prompt="first", target="main")
    second = await service.actions.add(event.id, when="end", prompt="second", target="main")
    assert first["id"] == "act_000000000001"
    assert second["id"] == "act_000000000002"
    assert {action["id"]: action["prompt"] for action in service.actions.list_actions()} == {
        first["id"]: "first",
        second["id"]: "second",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [("prompt", ""), ("when", "nonsense")])
async def test_invalid_action_is_skipped_kept_verbatim_and_reported(tmp_path, field, value):
    service, event, trigger, now = setup(tmp_path)
    broken = await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    path = tmp_path / "calendar" / "actions.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["actions"][0][field] = value
    invalid_entry = payload["actions"][0]
    path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = CalendarService(tmp_path, tz="Europe/Berlin")
    reopened.actions.configure(trigger, Mock(), session_manager())
    assert reopened.actions.list_actions() == []
    assert reopened.actions.storage_error is None
    await reopened.actions.tick(now)
    await drain(reopened)
    added = await reopened.actions.add(event.id, when="end", prompt="wrap up", target="main")

    trigger.trigger_run.assert_not_awaited()
    stored = json.loads(path.read_text(encoding="utf-8"))["actions"]
    assert [entry["id"] for entry in stored] == [added["id"], broken["id"]]
    assert stored[1] == invalid_entry
    report = validate_calendar_actions_file(path)
    assert [diagnostic.path for diagnostic in report.diagnostics] == ["$.actions[1]"]


@pytest.mark.asyncio
async def test_invalid_execution_row_blocks_its_occurrence_and_is_kept(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    action = await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await drain(service)
    assert trigger.trigger_run.await_count == 1
    path = tmp_path / "calendar" / "actions.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    ((key, row),) = payload["executions"].items()
    row["status"] = "archived-by-a-newer-vbot"
    path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = CalendarService(tmp_path, tz="Europe/Berlin")
    reopened.actions.configure(trigger, Mock(), session_manager())
    await reopened.actions.tick(now + timedelta(seconds=1))
    await drain(reopened)
    await reopened.actions.update(action["id"], prompt="prepare slides")

    # The row may record a consumed claim, so the occurrence never fires again.
    assert trigger.trigger_run.await_count == 1
    assert reopened.actions.project(window(reopened, now)) == []
    assert json.loads(path.read_text(encoding="utf-8"))["executions"] == {key: row}
    report = validate_calendar_actions_file(path)
    assert [diagnostic.path for diagnostic in report.diagnostics] == [f"$.executions[{key!r}]"]


@pytest.mark.asyncio
async def test_history_of_unknown_actions_stays_while_invalid_actions_are_kept(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    action = await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await drain(service)
    path = tmp_path / "calendar" / "actions.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["actions"][0]["when"] = "start - 1w"
    path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = CalendarService(tmp_path, tz="Europe/Berlin")
    reopened.actions.configure(trigger, Mock(), session_manager())
    await reopened.actions.tick(now + timedelta(minutes=2))

    # Repairing the action must not refire the occurrence it already consumed.
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert [row["action_id"] for row in stored["executions"].values()] == [action["id"]]
    payload["actions"][0]["when"] = "start - 1h"
    path.write_text(json.dumps(payload), encoding="utf-8")
    repaired = CalendarService(tmp_path, tz="Europe/Berlin")
    repaired.actions.configure(trigger, Mock(), session_manager())
    await repaired.actions.tick(now + timedelta(minutes=3))
    await drain(repaired)
    assert trigger.trigger_run.await_count == 1


class _BlockedActionWrites:
    """Holds every actions.json write on the writer thread until released."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        write = actions_module.write_json_document

        def blocked_write(*args, **kwargs):
            self.entered.set()
            self.release.wait(timeout=5)
            return write(*args, **kwargs)

        monkeypatch.setattr(actions_module, "write_json_document", blocked_write)


@pytest.mark.asyncio
async def test_scheduler_saves_off_the_loop_and_recomputes_after_a_change(tmp_path, monkeypatch):
    service, event, trigger, now = setup(tmp_path)
    await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    writes = _BlockedActionWrites(monkeypatch)

    ticking = asyncio.create_task(service.actions.tick(now))
    try:
        assert await asyncio.to_thread(writes.entered.wait, 5)
        loop = asyncio.get_running_loop()
        ticked_at = loop.time()
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert loop.time() - ticked_at < 1
        assert not ticking.done()
        # An edit while the save is in flight: nothing may start from stale state.
        await service.update_event(event.id, title="Moved")
    finally:
        writes.release.set()
    await ticking

    assert service.actions._workers == {}
    assert trigger.trigger_run.await_count == 0
    await service.actions.tick(now)
    await drain(service)
    assert trigger.trigger_run.await_count == 1


@pytest.mark.asyncio
async def test_an_action_edit_lands_after_an_in_flight_scheduler_save(tmp_path, monkeypatch):
    service, event, _, now = setup(tmp_path)
    first = await service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    writes = _BlockedActionWrites(monkeypatch)

    ticking = asyncio.create_task(service.actions.tick(now))
    try:
        assert await asyncio.to_thread(writes.entered.wait, 5)
        # The edit's save queues behind the scheduler's older snapshot without
        # holding the Event Loop.
        adding = asyncio.create_task(
            service.actions.add(event.id, when="end", prompt="review", target="main")
        )
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(service.actions.list_actions()) == 2
        assert not adding.done()
    finally:
        writes.release.set()
    second = await asyncio.wait_for(adding, timeout=5)
    await ticking
    await drain(service)

    stored = json.loads((tmp_path / "calendar" / "actions.json").read_text(encoding="utf-8"))
    assert {action["id"] for action in stored["actions"]} == {first["id"], second["id"]}
