"""Tests for jobs that run when their webhook receives an authenticated request."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.automation import _cron_timing as cron_timing
from core.automation._cron_webhook_events import WEBHOOK_STATE_DIR_NAME, state_path
from core.automation.cron import (
    CronJobValidationError,
    CronService,
    CronStorageError,
    CronWatch,
)
from core.runs import RunStatus
from core.secrets import WEBHOOK_SECRETS
from tests.core.automation.cron_test_support import make_service, scripted_runs, secret_store

_TIMEOUT_SECONDS = 10.0


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    current = _Clock(datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
    monkeypatch.setattr(cron_timing, "_utc_now", current)
    return current


async def _webhook_job(service: CronService, **fields: Any) -> tuple[str, str]:
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Summarize what happened",
        schedule_type="webhook",
        **fields,
    )
    return job.id, await service.webhook_secret(job.id)


async def _until(service: CronService, done: Any) -> None:
    """Wait until ``done()`` holds after a saved Cron change."""
    changed = asyncio.Event()
    unsubscribe = service.add_changed_callback(changed.set)
    try:
        while not done():
            changed.clear()
            await asyncio.wait_for(changed.wait(), timeout=_TIMEOUT_SECONDS)
    finally:
        unsubscribe()


def _bearer(secret: str, **headers: str) -> list[tuple[str, str]]:
    return [("authorization", f"Bearer {secret}"), *headers.items()]


def _state(service: CronService, job_id: str) -> dict[str, Any]:
    path = state_path(service._cron_dir / WEBHOOK_STATE_DIR_NAME, job_id)
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _webhook_note(trigger_service: SimpleNamespace, call: int = -1) -> str:
    note = str(trigger_service.trigger_run.await_args_list[call].kwargs["context_note"])
    return note.split("\n\n")[0]


def _records(note: str) -> list[Any]:
    """The JSON records after the note's opening line, one per request."""
    decoder = json.JSONDecoder()
    text = note.split("\n", 1)[1]
    records, index = [], 0
    while index < len(text):
        record, index = decoder.raw_decode(text, index)
        records.append(record)
        index += 1
    return records


@pytest.mark.asyncio
async def test_a_request_with_the_secret_starts_a_run_with_its_payload_as_escaped_data(
    tmp_path: Path, clock: _Clock
) -> None:
    service, trigger_service = make_service(tmp_path, tz="Europe/Berlin")
    service.set_webhook_secrets(secret_store(tmp_path))
    service.bind_webhook_url("https://vbot.example.com", "internet")
    job_id, secret = await _webhook_job(service, remaining_runs=2)
    scripted_runs(trigger_service, (RunStatus.COMPLETED, "Noted."))
    job = service.get_job(job_id)
    assert (service.format_schedule(job), service.next_fire_at(job)) == ("webhook", None)
    status = service.webhook_status(job)
    assert status is not None
    assert status.url == f"https://vbot.example.com/api/hooks/{job_id}"
    assert service.project_occurrences(clock.now, clock.now + timedelta(days=7)) == []

    body = json.dumps({"action": "opened", "title": "<script>alert(1)</script>"}).encode()
    headers = _bearer(
        secret,
        **{
            "content-type": "application/json",
            "x-github-event": "pull_request",
            "x-github-delivery": "d-1",
            "x-hub-signature": "sha1=leaked",
            "cookie": "session=leaked",
        },
    )
    assert await service.receive_webhook(job_id, headers, body) == "accepted"
    # The request is stored before the sender hears that it was accepted.
    assert _state(service, job_id)["requests"][0]["body"]["action"] == "opened"

    service.start()
    try:
        await _until(service, lambda: service.get_job(job_id).last_outcome == "success")
    finally:
        await service.aclose()

    note = _webhook_note(trigger_service)
    assert note.startswith("This run started because the job's webhook received a request.")
    assert "<script>" not in note and "secret" not in note.split("\n", 1)[1]
    assert _records(note) == [
        {
            "received_at": "2026-10-20T14:00:00+02:00",
            "headers": {
                "content-type": "application/json",
                "x-github-delivery": "d-1",
                "x-github-event": "pull_request",
            },
            "body": {"action": "opened", "title": "<script>alert(1)</script>"},
        }
    ]
    finished = service.get_job(job_id)
    # A webhook job's repeat counts its Runs.
    assert (finished.remaining_runs, finished.status) == (1, "active")
    assert _state(service, job_id)["requests"] == []
    final = service.webhook_status(finished)
    assert final is not None
    assert (final.waiting, final.last_received_at) == (0, "2026-10-20T12:00:00+00:00")


@pytest.mark.asyncio
async def test_refused_requests_are_alike_whatever_is_wrong(tmp_path: Path) -> None:
    service, _trigger = make_service(tmp_path)
    service.set_webhook_secrets(secret_store(tmp_path))
    job_id, secret = await _webhook_job(service)
    paused_id, paused_secret = await _webhook_job(service, status="paused")
    timed = await service.create_job(
        agent_id="agent-one", prompt="Daily", schedule_type="cron", cron_expression="0 9 * * *"
    )
    body = b'{"ok": true}'
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    refused = [
        ("cron_unknown", _bearer(secret)),
        (job_id, _bearer("vbw_wrong")),
        (job_id, []),
        (job_id, [("authorization", f"Basic {secret}")]),
        (job_id, [*_bearer(secret), *_bearer(secret)]),
        (job_id, [("x-hub-signature-256", "sha256=" + "0" * 64)]),
        (paused_id, _bearer(paused_secret)),
        (timed.id, _bearer(secret)),
    ]
    for target, headers in refused:
        assert await service.receive_webhook(target, headers, body) == "refused", headers
    assert not (service._cron_dir / WEBHOOK_STATE_DIR_NAME).exists()

    # GitHub's signature over the raw body proves the secret too, in any case of its hex.
    for proof in (signature, signature.upper().replace("SHA256=", "sha256=")):
        assert (
            await service.receive_webhook(job_id, [("x-hub-signature-256", proof)], body)
            == "accepted"
        )
    assert len(_state(service, job_id)["requests"]) == 2


@pytest.mark.asyncio
async def test_requests_during_a_run_start_one_follow_up_run_dropping_the_oldest_beyond_the_cap(
    tmp_path: Path, clock: _Clock
) -> None:
    service, trigger_service = make_service(tmp_path, tz="UTC")
    service.set_webhook_secrets(secret_store(tmp_path))
    job_id, secret = await _webhook_job(service)
    admitted, finish = asyncio.Event(), asyncio.Event()

    async def trigger_run(*_args: Any, **_kwargs: Any) -> Any:
        admitted.set()

        async def wait() -> Any:
            await finish.wait()
            return SimpleNamespace(content="[SILENT]")

        return SimpleNamespace(id="run-1", session_id="ses-1", wait=wait)

    trigger_service.trigger_run.side_effect = trigger_run

    async def send(text: str) -> None:
        clock.now += timedelta(seconds=1)
        headers = _bearer(secret, **{"content-type": "text/plain"})
        assert await service.receive_webhook(job_id, headers, text.encode()) == "accepted"

    await send("first")
    run = asyncio.create_task(
        service._execution._trigger_job_run(service.get_job(job_id), webhook=True)
    )
    await asyncio.wait_for(admitted.wait(), timeout=_TIMEOUT_SECONDS)
    # While the Run is active, requests wait; beyond the cap the oldest go first.
    for number in range(12):
        await send(f"update {number}")
    waiting = service.webhook_status(service.get_job(job_id))
    assert waiting is not None
    assert (waiting.waiting, waiting.dropped) == (10, 2)
    finish.set()
    await run

    finish.clear()
    admitted.clear()
    follow_up = asyncio.create_task(
        service._execution._trigger_job_run(service.get_job(job_id), webhook=True)
    )
    await asyncio.wait_for(admitted.wait(), timeout=_TIMEOUT_SECONDS)
    note = _webhook_note(trigger_service)
    assert note.startswith(
        "This run started because the job's webhook received 10 requests. Each JSON record"
    )
    assert "2 earlier requests were dropped unseen" in note
    assert [record["body"] for record in _records(note)] == [
        f"update {number}" for number in range(2, 12)
    ]
    finish.set()
    await follow_up
    assert trigger_service.trigger_run.await_count == 2
    after = service.webhook_status(service.get_job(job_id))
    assert after is not None and (after.waiting, after.dropped) == (0, 0)


@pytest.mark.asyncio
async def test_waiting_requests_survive_a_run_that_could_not_start_and_a_restart(
    tmp_path: Path,
) -> None:
    service, trigger_service = make_service(tmp_path)
    service.set_webhook_secrets(secret_store(tmp_path))
    job_id, secret = await _webhook_job(service)
    trigger_service.trigger_run.side_effect = RuntimeError("Queue is full")
    assert await service.receive_webhook(job_id, _bearer(secret), b"one") == "accepted"

    outcome = await service._execution._trigger_job_run(service.get_job(job_id), webhook=True)
    assert outcome.admitted is False
    assert service.get_job(job_id).last_error == "Queue is full"

    restarted, restarted_trigger = make_service(tmp_path)
    restarted.set_webhook_secrets(secret_store(tmp_path))
    scripted_runs(restarted_trigger, (RunStatus.COMPLETED, "Done."))
    restarted.start()
    try:
        await _until(restarted, lambda: restarted.get_job(job_id).last_outcome == "success")
        # The secret outlives the process, so the sender keeps working.
        assert await restarted.receive_webhook(job_id, _bearer(secret), b"two") == "accepted"
    finally:
        await restarted.aclose()
    first = _records(_webhook_note(restarted_trigger, 0))
    assert [record["body"] for record in first] == ["one"]


@pytest.mark.asyncio
async def test_bodies_reach_the_run_rendered_and_cut(tmp_path: Path) -> None:
    service, trigger_service = make_service(tmp_path, tz="UTC")
    service.set_webhook_secrets(secret_store(tmp_path))
    job_id, secret = await _webhook_job(service)
    scripted_runs(trigger_service, *[(RunStatus.COMPLETED, "[SILENT]")] * 3)

    async def run_with(content_type: str, body: bytes) -> list[Any]:
        headers = _bearer(secret, **{"content-type": content_type})
        assert await service.receive_webhook(job_id, headers, body) == "accepted"
        await service._execution._trigger_job_run(service.get_job(job_id), webhook=True)
        return _records(_webhook_note(trigger_service))

    form = b"payload=%7B%22zen%22%3A%22Keep+it+logically+awesome.%22%7D"
    assert (await run_with("application/x-www-form-urlencoded", form))[0]["body"] == {
        "zen": "Keep it logically awesome."
    }
    assert (await run_with("text/plain", b"line one\nline two"))[0]["body"] == (
        "line one\nline two"
    )
    long_body = json.dumps({"items": [{"n": number} for number in range(2000)]}).encode()
    record = (await run_with("application/json", long_body))[0]
    note = _webhook_note(trigger_service)
    assert "is cut after 16000 characters" in note
    assert isinstance(record["body"], str) and len(record["body"]) <= 16000
    assert record["body"].startswith('{\n  "items": [')


@pytest.mark.asyncio
async def test_the_secret_lives_in_the_secret_store_and_follows_the_job(tmp_path: Path) -> None:
    store = secret_store(tmp_path)
    service, _trigger = make_service(tmp_path)

    # Without a Secret Store no job can get a webhook, and a webhook takes no watch.
    with pytest.raises(CronJobValidationError, match="webhook is unavailable"):
        await _webhook_job(service)
    service.set_webhook_secrets(store)
    with pytest.raises(CronJobValidationError, match="not to a one-time, event or webhook"):
        await _webhook_job(service, watch=CronWatch.parse("url", "https://example.com"))

    job_id, secret = await _webhook_job(service)
    assert secret.startswith("vbw_") and len(secret) > 40
    assert store.entry(WEBHOOK_SECRETS, job_id) == secret
    jobs_text = (service._cron_dir / "jobs.json").read_text(encoding="utf-8")
    assert secret not in jobs_text and '"schedule_type": "webhook"' in jobs_text

    rotated = await service.rotate_webhook_secret(job_id, actor="rpc")
    assert rotated != secret
    assert await service.receive_webhook(job_id, _bearer(secret), b"x") == "refused"
    assert await service.receive_webhook(job_id, _bearer(rotated), b"x") == "accepted"

    # Leaving the webhook schedule drops the secret and the waiting requests.
    await service.update_job(job_id, schedule_type="cron", cron_expression="0 9 * * *")
    assert store.entry(WEBHOOK_SECRETS, job_id) is None
    assert not state_path(service._cron_dir / WEBHOOK_STATE_DIR_NAME, job_id).exists()
    assert service.webhook_status(service.get_job(job_id)) is None
    with pytest.raises(CronJobValidationError, match="has no webhook"):
        await service.webhook_secret(job_id)

    # Coming back gets a new secret; deleting the job removes it.
    await service.update_job(job_id, schedule_type="webhook", cron_expression=None)
    renewed = await service.webhook_secret(job_id)
    assert renewed not in {secret, rotated}
    await service.delete_job(job_id)
    assert store.entry(WEBHOOK_SECRETS, job_id) is None

    # A job whose secret went missing refuses requests until it is rotated.
    other_id, other_secret = await _webhook_job(service)
    store.remove_entry(WEBHOOK_SECRETS, other_id)
    reopened, _ = make_service(tmp_path)
    reopened.set_webhook_secrets(secret_store(tmp_path))
    reopened.start()
    try:
        assert await reopened.receive_webhook(other_id, _bearer(other_secret), b"x") == "refused"
        with pytest.raises(CronStorageError, match="rotate it"):
            await reopened.webhook_secret(other_id)
        fresh = await reopened.rotate_webhook_secret(other_id)
        assert await reopened.receive_webhook(other_id, _bearer(fresh), b"x") == "accepted"
    finally:
        await reopened.aclose()
