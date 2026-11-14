"""Tests for Cron jobs that belong to an Extension: triggers whose fires call it."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.automation import _cron_timing as cron_timing
from core.automation.cron import (
    CronFire,
    CronJobNotFoundError,
    CronJobValidationError,
    CronOwner,
    CronService,
    validate_cron_jobs_file,
)
from tests.core.automation.cron_test_support import make_service, secret_store

_TIMEOUT_SECONDS = 10.0


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    current = _Clock(datetime(2026, 11, 14, 12, 0, tzinfo=UTC))
    monkeypatch.setattr(cron_timing, "_utc_now", current)
    return current


async def _until(service: CronService, done: Any) -> None:
    changed = asyncio.Event()
    unsubscribe = service.add_changed_callback(changed.set)
    try:
        while not done():
            changed.clear()
            await asyncio.wait_for(changed.wait(), timeout=_TIMEOUT_SECONDS)
    finally:
        unsubscribe()


def _github(secret: str, body: bytes) -> list[tuple[str, str]]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return [
        ("x-hub-signature-256", f"sha256={digest}"),
        ("x-github-event", "push"),
        ("content-type", "application/json"),
    ]


@pytest.mark.asyncio
async def test_an_owned_webhook_calls_its_extension_with_requests_that_waited_for_it(
    tmp_path: Path, clock: _Clock
) -> None:
    service, trigger_service = make_service(tmp_path)
    service.set_webhook_secrets(secret_store(tmp_path))
    service.bind_webhook_url("https://vbot.example.com", "internet")
    job = await service.create_owned_job(
        CronOwner("factory", "workflow:review"), name="Review pushes", schedule="webhook"
    )
    secret = await service.webhook_secret(job.id, owner="factory")
    status = service.webhook_status(job)
    assert status is not None and status.url == f"https://vbot.example.com/api/hooks/{job.id}"

    # Neither the user's surfaces nor another Extension see or change the trigger.
    assert service.list_jobs() == []
    assert [item.id for item in service.list_jobs(owner="factory")] == [job.id]
    assert service.list_jobs(owner="swarm") == []
    for refused in (
        lambda: service.get_job(job.id),
        lambda: service.get_job(job.id, owner="swarm"),
    ):
        with pytest.raises(CronJobNotFoundError):
            refused()
    with pytest.raises(CronJobNotFoundError):
        await service.delete_job(job.id)
    with pytest.raises(CronJobNotFoundError):
        await service.update_job(job.id, name="Taken over")
    with pytest.raises(CronJobValidationError):
        await service.update_job(job.id, owner="factory", prompt="Run an Agent instead")

    body = json.dumps({"ref": "refs/heads/main"}).encode()
    assert await service.receive_webhook(job.id, _github(secret, body), body) == "accepted"

    fires: list[CronFire] = []

    async def handler(fire: CronFire) -> None:
        fires.append(fire)

    service.start()
    try:
        # Without a handler the request waits; setting one delivers it at once.
        service.set_owner_handler("factory", handler)
        await _until(
            service, lambda: service.get_job(job.id, owner="factory").last_outcome == "success"
        )
    finally:
        await service.aclose()

    trigger_service.trigger_run.assert_not_awaited()
    assert len(fires) == 1
    record = fires[0].to_dict()
    assert record["trigger_id"] == job.id and record["key"] == "workflow:review"
    assert record["schedule_type"] == "webhook" and record["due_at"] is None
    assert [item["body"] for item in record["requests"]] == [{"ref": "refs/heads/main"}]
    assert record["requests"][0]["headers"]["x-github-event"] == "push"
    after = service.webhook_status(service.get_job(job.id, owner="factory"))
    assert after is not None and after.waiting == 0


@pytest.mark.asyncio
async def test_an_owned_schedule_fires_its_handler_and_counts_a_raising_handler_as_failed(
    tmp_path: Path, clock: _Clock
) -> None:
    service, trigger_service = make_service(tmp_path)
    job = await service.create_owned_job(
        CronOwner("factory", "workflow:nightly"), name="Nightly", schedule="every 1h"
    )
    calls: list[CronFire] = []

    async def failing(fire: CronFire) -> None:
        calls.append(fire)
        raise RuntimeError("card store unavailable")

    service.set_owner_handler("factory", failing)
    clock.now += timedelta(hours=1, minutes=5)
    service.start()
    try:
        await _until(
            service, lambda: service.get_job(job.id, owner="factory").last_outcome == "failed"
        )
    finally:
        await service.aclose()

    failed = service.get_job(job.id, owner="factory")
    assert failed.consecutive_failures == 1 and "card store unavailable" in (
        failed.last_error or ""
    )
    assert calls[0].due_at == (datetime(2026, 11, 14, 13, 0, tzinfo=UTC)).isoformat()
    assert calls[0].late_by_seconds == 300
    trigger_service.trigger_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_owned_trigger_survives_a_restart_and_validates_without_an_agent(
    tmp_path: Path, clock: _Clock
) -> None:
    service, _trigger = make_service(tmp_path)
    job = await service.create_owned_job(
        CronOwner("factory", "workflow:weekly"), name="Weekly", schedule="0 9 * * 1"
    )
    updated = await service.update_owned_job(
        "factory", job.id, schedule="0 10 * * 1", status="paused"
    )
    assert (service.format_schedule(updated), updated.status) == ("0 10 * * 1", "paused")
    with pytest.raises(CronJobValidationError):
        await service.create_owned_job(
            CronOwner("factory", "workflow:mail"), name="Mail", schedule="email"
        )

    reloaded, _ = make_service(tmp_path)
    again = reloaded.get_job(job.id, owner="factory")
    assert again.owner == CronOwner("factory", "workflow:weekly")
    assert reloaded.list_jobs() == []
    assert reloaded.project_occurrences(clock.now, clock.now + timedelta(days=14)).occurrences == []
    report = validate_cron_jobs_file(tmp_path / "cron" / "jobs.json")
    assert report.diagnostics == ()
