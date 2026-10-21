"""Tests for jobs that watch a command or URL and run only when its output changed."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.automation import _cron_timing as cron_timing
from core.automation._cron_watch import WATCH_STATE_DIR_NAME, baseline_path
from core.automation.cron import (
    CronDelivery,
    CronFailureAlert,
    CronJobValidationError,
    CronService,
    CronWatch,
    WatchCheckError,
)
from core.runs import RunStatus
from core.secrets._references import SecretMasker
from tests.core.automation.cron_test_support import (
    FakeChats,
    FakeChecks,
    make_service,
    scripted_runs,
)

_COMMAND = CronWatch.parse("command", "Get-Content status.txt")
_CHAT = CronDelivery(kind="chat", channel_id="tg-main", platform_target="chat-42")


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    current = _Clock(datetime(2026, 10, 5, 4, 0, tzinfo=UTC))
    monkeypatch.setattr(cron_timing, "_utc_now", current)
    return current


async def _watching_job(
    service: CronService, checks: FakeChecks, *, watch: CronWatch = _COMMAND, **fields: Any
) -> str:
    service.set_watch_checks(checks)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Tell me what changed",
        schedule_type="interval",
        interval_seconds=3600,
        watch=watch,
        **fields,
    )
    return job.id


def _baseline(service: CronService, job_id: str) -> dict[str, Any]:
    path = baseline_path(service._cron_dir / WATCH_STATE_DIR_NAME, job_id)
    return dict(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.asyncio
async def test_a_watching_job_runs_only_when_its_check_output_changed(
    tmp_path: Path, clock: _Clock
) -> None:
    service, trigger_service = make_service(tmp_path, tz="Europe/Berlin")
    checks = FakeChecks(["v1\r\n", "v1  \n\n", "v2\n<b>", "v2\n<b>", "v2\n<b>\n"])
    job_id = await _watching_job(service, checks)
    scripted_runs(
        trigger_service, (RunStatus.FAILED, ""), (RunStatus.COMPLETED, "Status moved to v2.")
    )

    async def fire() -> None:
        clock.now += timedelta(hours=1)
        await service._watcher.fire(service.get_job(job_id))

    # The first check records the baseline; the same output in other whitespace starts nothing.
    await fire()
    await fire()
    trigger_service.trigger_run.assert_not_awaited()
    unchanged = service.get_job(job_id)
    assert unchanged.last_checked_at == "2026-10-05T06:00:00+00:00"
    assert (unchanged.last_outcome, unchanged.last_attempt_at, unchanged.last_changed_at) == (
        None,
        None,
        None,
    )
    assert _baseline(service, job_id)["output"] == "v1"

    # A change starts the Run with the diff and the output as escaped data.
    await fire()
    note = trigger_service.trigger_run.await_args.kwargs["context_note"]
    change_note = note.split("\n\n")[0]
    assert "changed since the check at 2026-10-05T07:00:00+02:00" in change_note
    assert "<b>" not in note
    assert json.loads(change_note.splitlines()[-1]) == {
        "command": "Get-Content status.txt",
        "diff": "@@ -1 +1,2 @@\n-v1\n+v2\n+<b>",
        "current": "v2\n<b>",
    }
    # Its Run failed, so the change is found again and runs again.
    assert _baseline(service, job_id)["output"] == "v1"
    await fire()
    assert trigger_service.trigger_run.await_count == 2
    changed = service.get_job(job_id)
    assert changed.last_changed_at == "2026-10-05T08:00:00+00:00"
    assert (changed.last_outcome, changed.consecutive_failures) == ("success", 0)
    baseline = _baseline(service, job_id)
    assert (baseline["output"], baseline["changed_at"]) == ("v2\n<b>", "2026-10-05T08:00:00+00:00")

    await fire()
    assert trigger_service.trigger_run.await_count == 2
    assert checks.calls[0] == ("command", "Get-Content status.txt", "agent-one", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["chat", "app"])
async def test_failed_checks_count_toward_the_streak_and_alert_without_touching_run_outcomes(
    tmp_path: Path, delivery: str
) -> None:
    service, trigger_service = make_service(tmp_path)
    chats = FakeChats()
    service.set_chat_delivery(chats)
    in_app: list[CronFailureAlert] = []
    service.add_failure_alert_callback(in_app.append)
    failure = WatchCheckError("The check command exited with code 1: boom")
    checks = FakeChecks(["ok", failure, failure, "ok", *[failure] * 5])
    job_id = await _watching_job(
        service, checks, delivery=_CHAT if delivery == "chat" else CronDelivery(kind="app")
    )

    for _ in range(4):
        await service._watcher.fire(service.get_job(job_id))
    # A successful check ends the streak its failures started.
    recovered = service.get_job(job_id)
    assert (recovered.consecutive_failures, recovered.last_check_error) == (0, None)
    for _ in range(5):
        await service._watcher.fire(service.get_job(job_id))

    stopped = service.get_job(job_id)
    assert stopped.status == "failed"
    assert stopped.last_check_error == "The check command exited with code 1: boom"
    assert (stopped.last_outcome, stopped.last_error) == (None, None)
    trigger_service.trigger_run.assert_not_awaited()
    # A chat gets the alerts as messages; vBot, without a failed Run to mark, gets
    # them as alerts for its accessors.
    await service._notifications.flush()
    alerts = [message for _, message, *_ in chats.sent] + [alert.message for alert in in_app]
    assert {(alert.job_id, alert.agent_id) for alert in in_app} <= {(job_id, "agent-one")}
    assert len(alerts) == 3
    assert alerts[0] == alerts[1]
    assert alerts[0] == (
        f'Scheduled job "{stopped.name}" failed twice in a row. Last error: The check command '
        "exited with code 1: boom. It keeps checking on schedule and stops after 5 failures in "
        "a row."
    )
    assert "stopped after 5 failures in a row" in alerts[2]


@pytest.mark.asyncio
async def test_a_new_watch_is_checked_at_once_and_its_failure_counts_toward_no_streak(
    tmp_path: Path,
) -> None:
    service, trigger_service = make_service(tmp_path)
    service.start()
    checks = FakeChecks([WatchCheckError("The watched URL answered HTTP 503"), "Status: up"])
    try:
        job_id = await _watching_job(
            service, checks, watch=CronWatch.parse("url", "https://status.example/a")
        )
        assert await service.await_first_check(job_id)
        failed = service.get_job(job_id)
        assert failed.last_check_error == "The watched URL answered HTTP 503"
        assert failed.consecutive_failures == 0

        fixed = CronWatch.parse("url", "https://status.example/b")
        await service.update_job(job_id, watch=fixed)
        assert await service.await_first_check(job_id)
        checked = service.get_job(job_id)
        assert checked.last_checked_at is not None and checked.last_check_error is None
        assert _baseline(service, job_id)["watch"] == {"url": "https://status.example/b"}
        trigger_service.trigger_run.assert_not_awaited()
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_the_baseline_is_replaced_when_unreadable_and_removed_with_its_watch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service, trigger_service = make_service(tmp_path)
    job_id = await _watching_job(service, FakeChecks(["v1", "v2"]))
    await service._watcher.fire(service.get_job(job_id))
    path = baseline_path(service._cron_dir / WATCH_STATE_DIR_NAME, job_id)
    path.write_text("{broken", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="vbot.automation.cron"):
        await service._watcher.fire(service.get_job(job_id))

    trigger_service.trigger_run.assert_not_awaited()
    assert "baseline unreadable" in caplog.text
    assert _baseline(service, job_id)["output"] == "v2"
    await service.update_job(job_id, watch=None)
    assert not path.exists()
    assert service.get_job(job_id).last_checked_at is None


@pytest.mark.asyncio
async def test_a_missed_due_time_checks_once_and_its_change_gets_no_late_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    service, trigger_service = make_service(tmp_path, tz="Europe/Berlin")
    checks = FakeChecks(["v1", "v2"])
    job_id = await _watching_job(service, checks)
    clock.now += timedelta(hours=1)
    await service._watcher.fire(service.get_job(job_id))
    # vBot was off for five due times.
    clock.now += timedelta(hours=5, minutes=30)
    waits: list[datetime] = []

    async def wait(target: datetime, **_kwargs: Any) -> bool:
        if target <= clock.now:
            return True
        waits.append(target)
        service._jobs[job_id].status = "paused"
        return False

    monkeypatch.setattr(cron_timing, "_sleep_until_utc", wait)

    await service._scheduler._run_recurring_job(service.get_job(job_id), "interval")

    assert len(checks.calls) == 2
    trigger_service.trigger_run.assert_awaited_once()
    note = trigger_service.trigger_run.await_args.kwargs["context_note"]
    assert "changed since" in note and "late" not in note
    assert waits == [datetime(2026, 10, 5, 11, 0, tzinfo=UTC)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schedule", "message"),
    [
        pytest.param(
            {"schedule_type": "once", "run_at": "2099-01-01T09:00:00+00:00"},
            "watch applies only to a repeating schedule",
            id="once",
        ),
        pytest.param(
            {"schedule_type": "cron", "cron_expression": "0 9 * * *", "watch_checks": False},
            "this vBot process runs no watch checks",
            id="no-checks",
        ),
    ],
)
async def test_a_watch_is_refused_where_no_check_can_run(
    tmp_path: Path, schedule: dict[str, Any], message: str
) -> None:
    service, _trigger_service = make_service(tmp_path)
    if schedule.pop("watch_checks", True):
        service.set_watch_checks(FakeChecks())

    with pytest.raises(CronJobValidationError, match=message):
        await service.create_job(agent_id="agent-one", prompt="Report", watch=_COMMAND, **schedule)


@pytest.mark.asyncio
async def test_watch_redacts_before_storage_and_still_detects_secret_changes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    old, new = "synthetic-old-credential", "synthetic-new-credential"
    reference = "{{secret:API}}"
    masker = SecretMasker({old: reference, new: reference})
    service, trigger = make_service(tmp_path, secret_masker=lambda: masker)
    checks = FakeChecks([old, old, new, WatchCheckError(f"Failed: {new}")])
    job_id = await _watching_job(service, checks)
    scripted_runs(trigger, (RunStatus.COMPLETED, "Updated."))
    for _ in range(4):
        await service._watcher.fire(service.get_job(job_id))
    assert trigger.trigger_run.await_count == 1
    assert _baseline(service, job_id)["output"] == reference
    assert reference in (service.get_job(job_id).last_check_error or "")
    note = trigger.trigger_run.await_args.kwargs["context_note"]
    stored = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "cron").rglob("*.json")
    )
    for value in [old, new]:
        assert value not in stored
        assert value not in note
        assert value not in caplog.text
