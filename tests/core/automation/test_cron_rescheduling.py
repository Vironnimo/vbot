"""Cron rescheduling preserves admitted Runs and independently arms replacement fires."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.automation import _cron_claims as cron_claims
from core.automation import _cron_timing as cron_timing
from core.automation.cron import CronStorageError
from core.runs import RunCancelledError
from tests.core.automation.cron_test_support import make_service

_ASYNC_COORDINATION_TIMEOUT_SECONDS = 5.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schedule_type", "phase", "outcome"),
    [
        ("cron", "running", "success"),
        ("once", "running", "success"),
        ("once", "running", "failure"),
        ("once", "running", "cancelled"),
        ("once", "queued", "success"),
        ("once", "queued", "withdrawn"),
    ],
)
async def test_rescheduling_keeps_live_run_waiter_and_global_slot(
    tmp_path, monkeypatch, schedule_type, phase, outcome
):
    now = datetime(2030, 10, 5, 10, tzinfo=UTC)
    monkeypatch.setattr(cron_timing, "_utc_now", lambda: now)
    service, trigger = make_service(tmp_path, tz="UTC")
    schedule = (
        {"run_at": (now + timedelta(minutes=1)).isoformat()}
        if schedule_type == "once"
        else {"cron_expression": "* * * * *"}
    )
    job = await service.create_job(
        agent_id="agent", prompt="work", schedule_type=schedule_type, **schedule
    )
    running, release = asyncio.Event(), asyncio.Event()
    queued, admit = asyncio.Event(), asyncio.Event()
    sleeps = 0
    resumed, fire_replacement = asyncio.Event(), asyncio.Event()
    calls = 0

    async def start(*args, **kwargs):
        nonlocal calls
        calls += 1
        first = calls == 1
        if first and phase == "queued":
            queued.set()
            await admit.wait()
            if outcome == "withdrawn":
                raise RunCancelledError("removed from Queue")

        async def wait():
            running.set()
            await release.wait()
            if first and outcome == "failure":
                raise RuntimeError("Run failed")
            if first and outcome == "cancelled":
                raise RunCancelledError("Run cancelled")

        return SimpleNamespace(id=f"run-{calls}", wait=wait)

    async def sleep_until(target, **kwargs):
        nonlocal sleeps, now
        sleeps += 1
        if sleeps > 1:
            resumed.set()
            await fire_replacement.wait()
        now = target
        return True

    trigger.trigger_run.side_effect = start
    monkeypatch.setattr(cron_timing, "_sleep_until_utc", sleep_until)
    service.start()
    try:
        await asyncio.wait_for((queued if phase == "queued" else running).wait(), 5)
        task = service._job_tasks[job.id]
        available = service._run_slots._value
        update = (
            {"schedule_type": "once", "run_at": (now + timedelta(days=1)).isoformat()}
            if schedule_type == "once"
            else {"cron_expression": "*/2 * * * *"}
        )
        await service.update_job(job.id, **update)
        assert service._job_tasks[job.id] is task
        assert not task.cancelling()
        assert service._run_slots._value == available
        admit.set()
        release.set()
        await asyncio.wait_for(task, 5)
        updated = service.get_job(job.id)
        assert updated.status == "active"
        assert updated.remaining_runs == (1 if schedule_type == "once" else None)
        # The persisted replacement survives reopening, regardless of the old Run's outcome.
        reopened, _ = make_service(tmp_path, tz="UTC")
        assert reopened.get_job(job.id).to_dict() == updated.to_dict()
        await asyncio.wait_for(resumed.wait(), 5)
        assert service._run_slots._value == available + 1
        assert calls == 1
        if schedule_type == "once":
            replacement = service._job_tasks[job.id]
            fire_replacement.set()
            await asyncio.wait_for(replacement, 5)
            assert calls == 2
            completed = service.get_job(job.id)
            assert (completed.status, completed.remaining_runs) == ("completed", 0)
            assert completed.last_fired_at == update["run_at"]
            assert not cron_claims.path_for(service._once_fire_claims_dir, job.id).exists()
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_failed_once_reschedule_keeps_a_fire_admitted_during_its_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2030, 10, 5, 10, tzinfo=UTC)
    monkeypatch.setattr(cron_timing, "_utc_now", lambda: now)
    service, trigger = make_service(tmp_path, tz="UTC")
    job = await service.create_job(
        agent_id="agent", prompt="work", schedule_type="once", run_at=now.isoformat()
    )
    queued, admit = asyncio.Event(), asyncio.Event()
    running, finish = asyncio.Event(), asyncio.Event()
    saving, fail_save = asyncio.Event(), asyncio.Event()

    async def start(*args: Any, **kwargs: Any) -> Any:
        queued.set()
        await admit.wait()

        async def wait() -> None:
            running.set()
            await finish.wait()

        return SimpleNamespace(id="admitted-run", wait=wait)

    trigger.trigger_run.side_effect = start
    service.start()
    try:
        await asyncio.wait_for(queued.wait(), 5)
        firing = service._job_tasks[job.id]
        save = service._save_jobs_async

        async def fail_the_edit() -> None:
            if not saving.is_set():
                saving.set()
                await fail_save.wait()
                raise CronStorageError("injected edit failure")
            await save()

        monkeypatch.setattr(service, "_save_jobs_async", fail_the_edit)
        editing = asyncio.create_task(
            service.update_job(job.id, run_at=(now + timedelta(days=1)).isoformat())
        )
        try:
            await asyncio.wait_for(saving.wait(), 5)
            admit.set()
            await asyncio.wait_for(running.wait(), 5)
            fail_save.set()
            with pytest.raises(CronStorageError):
                await editing
            restored = service.get_job(job.id)
            assert (restored.run_at, restored.remaining_runs) == (job.run_at, 0)
            finish.set()
            await asyncio.wait_for(firing, 5)
            assert service.get_job(job.id).status == "completed"
            trigger.trigger_run.assert_awaited_once()
            reopened, _ = make_service(tmp_path, tz="UTC")
            assert reopened.get_job(job.id).status == "completed"
        finally:
            fail_save.set()
            await asyncio.gather(editing, return_exceptions=True)
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "change"),
    [
        ("claim", "reschedule"),
        ("claim", "pause"),
        ("claim", "shutdown"),
        ("fire", "reschedule"),
        ("fire", "pause"),
    ],
)
async def test_unadmitted_once_claim_is_withdrawn_before_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, change: str
) -> None:
    now = datetime(2030, 10, 5, 10, tzinfo=UTC)
    monkeypatch.setattr(cron_timing, "_utc_now", lambda: now)
    service, trigger = make_service(tmp_path, tz="UTC")
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Work at the selected time",
        schedule_type="once",
        run_at=(now + timedelta(minutes=1)).isoformat(),
    )
    sleeps = 0
    allow_replacement = asyncio.Event()

    async def reach_first_fire(target: datetime, **kwargs: Any) -> bool:
        nonlocal sleeps, now
        sleeps += 1
        if sleeps > 1:
            await allow_replacement.wait()
        now = target
        return True

    entered = threading.Event()
    release = threading.Event()
    write = cron_claims.write if phase == "claim" else service._write_jobs

    def hold_written_state(*args: Any, **kwargs: Any) -> None:
        write(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)

    monkeypatch.setattr(cron_timing, "_sleep_until_utc", reach_first_fire)
    if phase == "claim":
        monkeypatch.setattr(cron_claims, "write", hold_written_state)
    else:
        monkeypatch.setattr(service, "_write_jobs", hold_written_state)
    service.start()
    expected_run_at = job.run_at
    try:
        assert await asyncio.to_thread(entered.wait, _ASYNC_COORDINATION_TIMEOUT_SECONDS)
        firing = service._job_tasks[job.id]
        # An edit reaches the job task at once; its save queues behind the claim or fire save.
        release.set()
        if change == "reschedule":
            updated = await service.update_job(job.id, run_at=(now + timedelta(days=1)).isoformat())
            expected_run_at = updated.run_at
        elif change == "pause":
            await service.disable_job(job.id)
        if change != "shutdown":
            await asyncio.wait_for(firing, timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)
        await asyncio.wait_for(service.aclose(), timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)
        trigger.trigger_run.assert_not_awaited()
        assert not cron_claims.path_for(service._once_fire_claims_dir, job.id).exists()
    finally:
        release.set()
        await service.aclose()

    restarted, restarted_trigger = make_service(tmp_path, tz="UTC")
    restarted.start()
    try:
        restored = restarted.get_job(job.id)
        assert restored.status == ("paused" if change == "pause" else "active")
        assert restored.run_at == expected_run_at
        assert restored.last_outcome is None
        restarted_trigger.trigger_run.assert_not_awaited()
        if change == "reschedule":
            # The withdrawn fire must leave the replacement armed, including after restart.
            restarted_trigger.trigger_run.return_value = SimpleNamespace(
                id="replacement-run", wait=AsyncMock()
            )
            replacement = restarted._job_tasks[job.id]
            allow_replacement.set()
            await asyncio.wait_for(replacement, timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)
            restarted_trigger.trigger_run.assert_awaited_once()
            completed = restarted.get_job(job.id)
            assert completed.status == "completed"
            assert completed.last_fired_at == expected_run_at
    finally:
        await restarted.aclose()
