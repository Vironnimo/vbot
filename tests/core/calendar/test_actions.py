"""Calendar action contracts across edits, recurrence, admission, and restarts."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.calendar import CalendarService, CalendarStorageError, CalendarValidationError
from core.calendar import actions as actions_module
from core.calendar.actions import (
    action_message,
    parse_action_when,
    validate_calendar_actions_file,
)
from core.runs import RunKind, RunStatus


def setup(tmp_path: Path, *, start: datetime | None = None, recurring: bool = False):
    now = datetime.now(UTC)
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


def test_edit_preserves_event_id_and_moves_actions(tmp_path):
    service, event, _, now = setup(tmp_path)
    before = service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    after = service.actions.add(event.id, when="end + 30m", prompt="review", target="main")
    initial = service.actions.project(window(service, now))
    updated = service.update_event(
        event.id, start=(now + timedelta(hours=3)).isoformat(), duration_minutes=120
    )
    assert updated.id == event.id
    rows = service.actions.project(window(service, now))
    assert {row["action_id"] for row in rows} == {before["id"], after["id"]}
    assert rows[0]["scheduled_at"] != initial[0]["scheduled_at"]
    assert datetime.fromisoformat(rows[0]["expires_at"]) == now + timedelta(hours=3)
    assert datetime.fromisoformat(rows[1]["expires_at"]) == now + timedelta(hours=6, minutes=30)
    reloaded = CalendarService(tmp_path, tz="Europe/Berlin")
    assert reloaded.get_event(event.id).id == event.id
    assert reloaded.actions.list_actions() == service.actions.list_actions()


def test_deadlines_use_start_end_and_post_event_grace(tmp_path):
    service, event, _, now = setup(tmp_path)
    for when in ("start - 1h", "start", "end", "end + 30m"):
        service.actions.add(event.id, when=when, prompt="test", target="main")
    rows = service.actions.project(window(service, now))
    assert [datetime.fromisoformat(row["expires_at"]) - now for row in rows] == [
        timedelta(minutes=30),
        timedelta(minutes=90),
        timedelta(minutes=150),
        timedelta(minutes=180),
    ]


@pytest.mark.asyncio
async def test_fires_once_and_reloads_without_duplicate(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await drain(service)
    await service.actions.tick(now + timedelta(seconds=1))
    assert trigger.trigger_run.await_count == 1
    row = service.actions.project(window(service, now))[0]
    assert row["status"] == "completed"
    assert row["session"] == "new-session"
    assert trigger.trigger_run.call_args.args[2] is None
    reloaded = CalendarService(tmp_path, tz="Europe/Berlin")
    reloaded.actions.configure(trigger, Mock(), session_manager())
    await reloaded.actions.tick(now + timedelta(seconds=2))
    assert trigger.trigger_run.await_count == 1


@pytest.mark.asyncio
async def test_actions_waiting_for_a_worker_slot_fire_exactly_once(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    for index in range(6):
        service.actions.add(event.id, when="start - 1h", prompt=f"p{index}", target="main")
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
    service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await waiting.wait()
    # An event change withdraws work that is still awaiting admission.
    service.update_event(event.id, title="Renamed")
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
    service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await drain(service)
    await service.actions.tick(now + timedelta(days=29))
    assert [row["status"] for row in service.actions._executions.values()] == ["completed"]
    later = now + timedelta(days=31)

    class Clock(datetime):
        @classmethod
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
    action = service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    kept = service.actions.add(event.id, when="start - 45m", prompt="keep", target="main")
    await service.actions.tick(now)
    await drain(service)
    service.actions.delete(action["id"])
    await service.actions.tick(now + timedelta(seconds=1))
    stored = json.loads(service.actions._path.read_text(encoding="utf-8"))["executions"]
    assert [row["action_id"] for row in stored.values()] == [kept["id"]]
    assert trigger.trigger_run.await_count == 2


@pytest.mark.asyncio
async def test_completed_single_action_rearms_only_after_event_moves(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await drain(service)
    old = service.actions.project(window(service, now))[0]
    assert old["status"] == "completed"
    service.update_event(event.id, title="Renamed")
    await service.actions.tick(now)
    assert trigger.trigger_run.await_count == 1
    moved_start = now + timedelta(minutes=45)
    service.update_event(event.id, start=moved_start.isoformat())
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
    action = service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
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
    service.actions.update(action["id"], when="start - 45m")
    await service.actions.tick(now)
    await drain(service)
    assert trigger.trigger_run.await_count == 2


@pytest.mark.asyncio
async def test_move_during_admitted_run_keeps_claim_until_completion(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def wait_for_finish():
        entered.set()
        await finish.wait()

    trigger.trigger_run.side_effect = None
    trigger.trigger_run.return_value = SimpleNamespace(
        id="run-1", session_id="new-session", wait=wait_for_finish
    )
    service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    # Reconciliation before the scheduled worker starts must retain its pending row.
    await service.actions.tick(now)
    assert len(service.actions._executions) == 1
    await entered.wait()
    original = next(iter(service.actions._executions.values())).copy()
    service.update_event(event.id, start=(now + timedelta(minutes=45)).isoformat())
    await service.actions.tick(now)
    assert next(iter(service.actions._executions.values())) == original
    assert trigger.trigger_run.await_count == 1
    assert service.actions.project(window(service, now))[0]["status"] == "pending"
    finish.set()
    await drain(service)
    await service.actions.tick(now)
    await drain(service)
    assert trigger.trigger_run.await_count == 2
    await service.actions.tick(now)
    assert trigger.trigger_run.await_count == 2


@pytest.mark.asyncio
async def test_expired_and_excluded_occurrences_never_fire(tmp_path):
    service, event, trigger, now = setup(
        tmp_path, start=datetime.now(UTC) - timedelta(hours=2), recurring=True
    )
    service.actions.add(
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
    service.actions.add(
        event.id, when="start - 1h", prompt="prepare", target="builder@project", session="chosen"
    )
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
async def test_event_delete_withdraws_queued_action(tmp_path):
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
    service.actions.add(
        event.id, when="start - 1h", prompt="prepare", target="main", session="busy"
    )
    await service.actions.tick(now)
    await waiting.wait()
    service.delete_event(event.id)
    await cancelled.wait()
    await asyncio.sleep(0)
    await service.actions.tick(now + timedelta(seconds=1))
    assert service.actions.list_actions() == []
    assert not service.actions._workers


def test_unreadable_store_fails_closed(tmp_path):
    path = tmp_path / "calendar" / "actions.json"
    path.parent.mkdir()
    path.write_text("broken", encoding="utf-8")
    service = CalendarService(tmp_path, tz="UTC")
    assert service.actions.list_actions() == []
    assert service.actions.storage_error
    with pytest.raises(CalendarStorageError):
        service.actions.delete("unused")
    assert path.read_text() == "broken"


def test_all_day_deadlines_respect_dst(tmp_path):
    service = CalendarService(tmp_path, tz="Europe/Berlin")
    event = service.create_event(title="Day", start="2026-10-25")
    service.actions.add(event.id, when="start", prompt="prepare", target="main")
    occurrences = service.occurrences_in_window(*service.parse_window("2026-10-25", "2026-10-25"))
    row = service.actions.project(occurrences)[0]
    assert datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(
        row["scheduled_at"]
    ) == timedelta(hours=25)


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
    service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await drain(service)
    row = service.actions.project(window(service, now))[0]
    assert row["status"] == "failed"
    assert row["run_id"] == "timeout-run"
    assert trigger.trigger_run.call_args.kwargs["run_kind"] == RunKind.CALENDAR
    await service.actions.tick(now)
    assert trigger.trigger_run.await_count == 1


@pytest.mark.asyncio
async def test_cancel_before_worker_starts_releases_capacity(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    tasks = list(service.actions._workers.values())
    service.update_event(event.id, start=(now + timedelta(days=2)).isoformat())
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
    service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
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
    service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    await service.actions.tick(now)
    await drain(service)
    row = next(iter(service.actions._executions.values()))
    row["status"] = "running"
    service.actions._save()
    sessions = session_manager()
    sessions.get.return_value.find_run_summary.return_value = SimpleNamespace(status="completed")
    reloaded = CalendarService(tmp_path, tz="Europe/Berlin")
    reloaded.actions.configure(trigger, Mock(), sessions)
    await reloaded.actions.tick(now)
    assert reloaded.actions.project(window(reloaded, now))[0]["status"] == "completed"
    sessions.get.return_value.find_run_summary.assert_called_once_with(run_id="run-1")
    assert trigger.trigger_run.await_count == 1


def test_failed_write_rolls_back_action_mutations(tmp_path, monkeypatch):
    service, event, _, _ = setup(tmp_path)
    action = service.actions.add(event.id, when="start", prompt="prepare", target="main")
    monkeypatch.setattr(service.actions, "_save", Mock(side_effect=CalendarStorageError("disk")))
    with pytest.raises(CalendarStorageError):
        service.actions.update(action["id"], prompt="changed")
    with pytest.raises(CalendarStorageError):
        service.actions.delete(action["id"])
    with pytest.raises(CalendarStorageError):
        service.actions.add(event.id, when="end", prompt="new", target="main")
    assert service.actions.list_actions() == [action]


@pytest.mark.asyncio
async def test_deleted_events_release_stored_action_capacity(tmp_path):
    service, event, _, now = setup(tmp_path)
    service.actions.add(event.id, when="start", prompt="prepare", target="main")
    service.delete_event(event.id)
    await service.actions.tick(now)
    assert json.loads(service.actions._path.read_text())["actions"] == []


@pytest.mark.asyncio
async def test_recurrences_each_request_a_fresh_session(tmp_path, monkeypatch):
    service, event, trigger, now = setup(tmp_path, recurring=True)
    service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    for day in range(3):
        clock = now + timedelta(days=day)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None, clock=clock):
                return clock

        monkeypatch.setattr("core.calendar.actions.datetime", Clock)
        await service.actions.tick(clock)
        await drain(service)
    assert trigger.trigger_run.await_count == 3
    assert all(call.args[2] is None for call in trigger.trigger_run.call_args_list)


@pytest.mark.parametrize(
    "payload",
    [
        {"actions": [], "executions": {}},
        {"format_version": 2, "actions": [], "executions": {}},
        {"format_version": 1, "actions": {}, "executions": {}},
        {"format_version": 1, "actions": []},
        {"format_version": 1, "actions": [], "executions": []},
    ],
)
def test_unreadable_document_root_disables_store(tmp_path, payload):
    service = CalendarService(tmp_path, tz="UTC")
    path = tmp_path / "calendar" / "actions.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload))
    assert service.actions.list_actions() == []
    assert service.actions.storage_error
    with pytest.raises(CalendarStorageError):
        service.actions.delete("unused")
    assert json.loads(path.read_text()) == payload


def test_unknown_action_fields_are_hidden_and_kept_on_save(tmp_path):
    service, event, _, _ = setup(tmp_path)
    action = service.actions.add(event.id, when="start", prompt="prepare", target="main")
    path = tmp_path / "calendar" / "actions.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["future_setting"] = 1
    payload["actions"][0]["priority"] = "high"
    path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = CalendarService(tmp_path, tz="Europe/Berlin")
    assert "priority" not in reopened.actions.list_actions()[0]
    reopened.actions.update(action["id"], prompt="prepare slides")

    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten["format_version"] == 1
    assert rewritten["future_setting"] == 1
    assert rewritten["actions"][0]["prompt"] == "prepare slides"
    assert rewritten["actions"][0]["priority"] == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize("whole_file", [True, False])
async def test_invalid_event_storage_never_deletes_action_definitions(tmp_path, whole_file):
    service, event, _, now = setup(tmp_path)
    action = service.actions.add(event.id, when="start", prompt="prepare", target="main")
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


def test_short_action_ids_skip_collisions(tmp_path, monkeypatch):
    from core.utils import ids

    service, event, _, _ = setup(tmp_path)
    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    first = service.actions.add(event.id, when="start", prompt="first", target="main")
    second = service.actions.add(event.id, when="end", prompt="second", target="main")
    assert first["id"] == "act_000000000001"
    assert second["id"] == "act_000000000002"
    assert service.actions._actions[first["id"]]["prompt"] == "first"


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [("prompt", ""), ("when", "nonsense")])
async def test_invalid_action_is_skipped_kept_verbatim_and_reported(tmp_path, field, value):
    service, event, trigger, now = setup(tmp_path)
    broken = service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
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
    added = reopened.actions.add(event.id, when="end", prompt="wrap up", target="main")

    trigger.trigger_run.assert_not_awaited()
    stored = json.loads(path.read_text(encoding="utf-8"))["actions"]
    assert [entry["id"] for entry in stored] == [added["id"], broken["id"]]
    assert stored[1] == invalid_entry
    report = validate_calendar_actions_file(path)
    assert [diagnostic.path for diagnostic in report.diagnostics] == ["$.actions[1]"]


@pytest.mark.asyncio
async def test_invalid_execution_row_blocks_its_occurrence_and_is_kept(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    action = service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
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
    reopened.actions.update(action["id"], prompt="prepare slides")

    # The row may record a consumed claim, so the occurrence never fires again.
    assert trigger.trigger_run.await_count == 1
    assert reopened.actions.project(window(reopened, now)) == []
    assert json.loads(path.read_text(encoding="utf-8"))["executions"] == {key: row}
    report = validate_calendar_actions_file(path)
    assert [diagnostic.path for diagnostic in report.diagnostics] == [f"$.executions[{key!r}]"]


@pytest.mark.asyncio
async def test_history_of_unknown_actions_stays_while_invalid_actions_are_kept(tmp_path):
    service, event, trigger, now = setup(tmp_path)
    action = service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
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
    service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
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
        service.update_event(event.id, title="Moved")
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
    first = service.actions.add(event.id, when="start - 1h", prompt="prepare", target="main")
    writes = _BlockedActionWrites(monkeypatch)

    ticking = asyncio.create_task(service.actions.tick(now))
    try:
        assert await asyncio.to_thread(writes.entered.wait, 5)
        releaser = threading.Timer(0.1, writes.release.set)
        releaser.start()
        # The blocking edit save queues behind the scheduler's older snapshot.
        second = service.actions.add(event.id, when="end", prompt="review", target="main")
    finally:
        writes.release.set()
    await ticking
    await drain(service)

    stored = json.loads((tmp_path / "calendar" / "actions.json").read_text(encoding="utf-8"))
    assert {action["id"] for action in stored["actions"]} == {first["id"], second["id"]}
