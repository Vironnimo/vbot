"""Tests for fires that came due while vBot did not run a job."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.automation import _cron_timing as cron_timing
from core.automation.cron import CronJob, CronService
from tests.core.automation.cron_test_support import make_service

_ZONE = "Europe/Berlin"


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    # 06:00 in Berlin, two hours before the first 08:00 fire.
    current = _Clock(datetime(2026, 10, 5, 4, 0, tzinfo=UTC))
    monkeypatch.setattr(cron_timing, "_utc_now", current)
    return current


def _stop_at_next_wait(
    service: CronService, job_id: str, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> list[datetime]:
    """Pause the job when the scheduler waits for a future fire; return those waits."""
    waits: list[datetime] = []

    async def wait(target: datetime, **_kwargs: Any) -> bool:
        if target <= clock.now:
            return True
        waits.append(target)
        service._jobs[job_id].status = "paused"
        return False

    monkeypatch.setattr(cron_timing, "_sleep_until_utc", wait)
    return waits


async def _run(service: CronService, job: CronJob) -> None:
    if job.schedule_type == "once":
        await service._run_once_job(job)
    else:
        await service._run_recurring_job(job, job.schedule_type)


_SCHEDULES: dict[str, tuple[dict[str, Any], int]] = {
    # Daily at 08:00 Berlin: missed on 10-05, 10-06, 10-07 and 10-08.
    "cron": ({"schedule_type": "cron", "cron_expression": "0 8 * * *"}, 3),
    # Every day from creation at 06:00 Berlin: missed on 10-06, 10-07 and 10-08.
    "interval": ({"schedule_type": "interval", "interval_seconds": 86400}, 2),
    "once": ({"schedule_type": "once", "run_at": "2026-10-08T08:00:00+02:00"}, 0),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_SCHEDULES))
async def test_fires_missed_while_offline_start_once_late_with_a_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock, kind: str
) -> None:
    schedule, earlier = _SCHEDULES[kind]
    service, trigger_service = make_service(tmp_path, tz=_ZONE)
    job = await service.create_job(agent_id="agent-one", prompt="Morning brief", **schedule)
    # vBot was off until 10:12 Berlin on 10-08.
    clock.now = datetime(2026, 10, 8, 8, 12, tzinfo=UTC)
    _stop_at_next_wait(service, job.id, clock, monkeypatch)

    await _run(service, job)

    trigger_service.trigger_run.assert_awaited_once()
    notice = trigger_service.trigger_run.await_args.kwargs["context_note"]
    due = "2026-10-08T06:00:00+02:00" if kind == "interval" else "2026-10-08T08:00:00+02:00"
    assert due in notice
    assert "2026-10-08T10:12:00+02:00" in notice
    assert job.id in notice
    if earlier:
        first = "2026-10-06T06:00:00+02:00" if kind == "interval" else "2026-10-05T08:00:00+02:00"
        assert f"{earlier} earlier" in notice
        assert first in notice
    else:
        assert "earlier" not in notice
    fired = service.get_job(job.id)
    assert fired.last_fired_at == "2026-10-08T08:12:00+00:00"
    assert fired.last_error is None


@pytest.mark.asyncio
async def test_a_stored_max_delay_no_longer_skips_a_missed_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    schedule, _earlier = _SCHEDULES["once"]
    created, _trigger_service = make_service(tmp_path, tz=_ZONE)
    job = await created.create_job(agent_id="agent-one", prompt="Wake me", **schedule)
    # Earlier versions stored a limit that skipped every missed fire.
    jobs_path = tmp_path / "cron" / "jobs.json"
    document = json.loads(jobs_path.read_text(encoding="utf-8"))
    document["jobs"][0]["max_delay_seconds"] = 0
    jobs_path.write_text(json.dumps(document), encoding="utf-8")
    service, trigger_service = make_service(tmp_path, tz=_ZONE)
    clock.now = datetime(2026, 10, 8, 8, 12, tzinfo=UTC)
    _stop_at_next_wait(service, job.id, clock, monkeypatch)

    await _run(service, service.get_job(job.id))

    trigger_service.trigger_run.assert_awaited_once()
    assert "starting late" in trigger_service.trigger_run.await_args.kwargs["context_note"]
    assert service.get_job(job.id).status == "completed"


@pytest.mark.asyncio
async def test_a_fire_up_to_a_minute_late_starts_without_a_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    service, trigger_service = make_service(tmp_path, tz=_ZONE)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Wake me",
        schedule_type="cron",
        cron_expression="0 8 * * *",
    )
    # Thirty seconds after 08:00 Berlin counts as on time.
    clock.now = datetime(2026, 10, 5, 6, 0, 30, tzinfo=UTC)
    _stop_at_next_wait(service, job.id, clock, monkeypatch)

    await _run(service, job)

    trigger_service.trigger_run.assert_awaited_once()
    assert "context_note" not in trigger_service.trigger_run.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["enable", "reschedule"])
async def test_fires_due_before_activation_or_a_new_schedule_are_not_owed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock, change: str
) -> None:
    service, trigger_service = make_service(tmp_path, tz=_ZONE)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Morning brief",
        schedule_type="cron",
        cron_expression="0 8 * * *",
        status="paused" if change == "enable" else "active",
    )
    # 11:12 Berlin on 10-08: 08:00 and 09:00 of that day have passed.
    clock.now = datetime(2026, 10, 8, 9, 12, tzinfo=UTC)
    if change == "enable":
        job = await service.enable_job(job.id)
    else:
        job = await service.update_job(job.id, cron_expression="0 9 * * *")
    waits = _stop_at_next_wait(service, job.id, clock, monkeypatch)

    await _run(service, job)

    trigger_service.trigger_run.assert_not_awaited()
    next_hour = 6 if change == "enable" else 7
    assert waits == [datetime(2026, 10, 9, next_hour, 0, tzinfo=UTC)]
