"""Calendar action contracts across edits, recurrence, admission, and restarts."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.calendar import CalendarService, CalendarStorageError, CalendarValidationError
from core.calendar.actions import parse_action_when
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
    service.actions.configure(trigger, Mock(), Mock(exists=Mock(return_value=True)))
    return service, event, trigger, now


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
    reloaded.actions.configure(trigger, Mock(), Mock())
    await reloaded.actions.tick(now + timedelta(seconds=2))
    assert trigger.trigger_run.await_count == 1


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
    assert json.loads(call.args[1])["instruction"] == "prepare"


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
    reloaded.actions.configure(trigger, Mock(), Mock())
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
    sessions = Mock()
    sessions.exists.return_value = True
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
    "payload", [{"actions": {}, "executions": {}}, {"actions": [None], "executions": {}}]
)
def test_malformed_action_rows_disable_store(tmp_path, payload):
    service = CalendarService(tmp_path, tz="UTC")
    path = tmp_path / "calendar" / "actions.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload))
    assert service.actions.list_actions() == []
    assert service.actions.storage_error


@pytest.mark.asyncio
@pytest.mark.parametrize("whole_file", [True, False])
async def test_invalid_event_storage_never_deletes_action_definitions(tmp_path, whole_file):
    service, event, _, now = setup(tmp_path)
    action = service.actions.add(event.id, when="start", prompt="prepare", target="main")
    path = tmp_path / "calendar" / "events.json"
    path.write_text("broken" if whole_file else json.dumps([{"id": event.id}]))
    reloaded = CalendarService(tmp_path, tz="UTC")
    if whole_file:
        with pytest.raises(CalendarStorageError):
            await reloaded.actions.tick(now)
    else:
        await reloaded.actions.tick(now)
    stored = json.loads(service.actions._path.read_text())
    assert stored["actions"][0]["id"] == action["id"]
