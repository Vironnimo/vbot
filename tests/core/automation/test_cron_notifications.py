"""Durable delivery contracts through CronService, without a platform or Model."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.automation import _cron_notifications as notifications
from core.automation.cron import CronDelivery, CronStorageError
from core.channels import ChannelError
from core.runs import RunStatus
from tests.core.automation.cron_test_support import FakeChats, make_service, scripted_runs

_CHAT = CronDelivery(kind="chat", channel_id="tg-main", platform_target="chat-42", thread_id="7")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["offline", "unsent", "uncertain", "interrupted"])
async def test_notifications_survive_restart_without_repeating_a_completed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    service, trigger = make_service(tmp_path)
    chats = FakeChats()
    service.set_chat_delivery(chats)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Check",
        schedule_type="interval",
        interval_seconds=60,
        remaining_runs=1,
        delivery=_CHAT,
    )
    scripted_runs(trigger, (RunStatus.COMPLETED, "The result."))
    if failure == "offline":
        service.set_chat_delivery(None)
    else:
        send_error = (
            asyncio.CancelledError()
            if failure == "interrupted"
            else ChannelError("Transport unavailable", possibly_delivered=failure == "uncertain")
        )
        monkeypatch.setattr(chats, "send", AsyncMock(side_effect=send_error))
    assert (await service._execution._trigger_job_run(job)).succeeded
    if failure == "interrupted":
        with pytest.raises(asyncio.CancelledError):
            await service._notifications.flush()
    else:
        await service._notifications.flush()
    assert service.get_job(job.id).status == "completed"
    assert service.get_job(job.id).delivery_progress()["waiting"] == 1
    assert trigger.trigger_run.await_count == 1
    await service.aclose()

    # Reconfiguration must not redirect the retained message to a different chat.
    await service.update_job(job.id, delivery=CronDelivery(kind="none"))
    recovered, recovered_trigger = make_service(tmp_path)
    recovered_chats = FakeChats()
    recovered.set_chat_delivery(recovered_chats)
    now = notifications.time.time()
    monkeypatch.setattr(notifications.time, "time", lambda: now + 60)
    if failure == "offline":
        delivered = asyncio.Event()
        original_send = recovered_chats.send

        async def send(
            channel_id: str, message: str, platform_target: str, *, thread_id: str | None = None
        ) -> None:
            await original_send(channel_id, message, platform_target, thread_id=thread_id)
            delivered.set()

        monkeypatch.setattr(recovered_chats, "send", send)
        recovered.start()
        await asyncio.wait_for(delivered.wait(), timeout=1)
    else:
        recovered.list_jobs()
    await recovered._notifications.flush()
    if failure in {"uncertain", "interrupted"}:
        assert recovered_chats.sent == []
        assert recovered.get_job(job.id).delivery_progress()["uncertain"] == 1
        await recovered.retry_delivery(job.id)
        await recovered._notifications.flush()
    assert recovered_chats.sent == [("tg-main", "The result.", "chat-42", "7")]
    assert recovered.get_job(job.id).delivery_progress()["waiting"] == 0
    assert recovered.get_job(job.id).last_delivered_at
    assert recovered_trigger.trigger_run.await_count == 0
    await recovered._notifications.flush()
    assert len(recovered_chats.sent) == 1
    persisted = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert "notifications" not in persisted["jobs"][0]
    await recovered.aclose()


@pytest.mark.asyncio
async def test_failure_alerts_retry_after_stop_and_a_failed_claim_never_sends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, trigger = make_service(tmp_path)
    chats = FakeChats()
    service.set_chat_delivery(chats)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Check",
        schedule_type="interval",
        interval_seconds=60,
        delivery=_CHAT,
    )
    service.set_chat_delivery(None)
    scripted_runs(trigger, *[(RunStatus.FAILED, "")] * 5)
    for _ in range(5):
        await service._execution._trigger_job_run(job)
    assert service.get_job(job.id).status == "failed"
    assert service.get_job(job.id).delivery_progress()["waiting"] == 2
    service.set_chat_delivery(chats)
    await service.retry_delivery(job.id)
    save = service._save_jobs_async
    with monkeypatch.context() as patch:
        patch.setattr(service, "_save_jobs_async", AsyncMock(side_effect=CronStorageError("disk")))
        await service._notifications.flush()
    assert chats.sent == []
    # Once the disk recovers, both alerts remain; no extra Model call is needed.
    await save()
    await service._notifications.flush()
    assert len(chats.sent) == 2
    assert "failed twice" in chats.sent[0][1]
    assert "stopped after 5" in chats.sent[1][1]
    assert trigger.trigger_run.await_count == 5
    await service.aclose()


@pytest.mark.asyncio
async def test_slow_delivery_never_holds_a_cron_run_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, trigger = make_service(tmp_path)
    chats = FakeChats()
    service.set_chat_delivery(chats)
    entered, release = asyncio.Event(), asyncio.Event()
    original_send = chats.send

    async def send(
        channel_id: str, message: str, platform_target: str, *, thread_id: str | None = None
    ) -> None:
        entered.set()
        await release.wait()
        await original_send(channel_id, message, platform_target, thread_id=thread_id)

    monkeypatch.setattr(chats, "send", send)
    service._run_slots = asyncio.Semaphore(1)
    service.start()
    try:
        job = await service.create_job(
            agent_id="agent-one",
            prompt="Check",
            schedule_type="interval",
            interval_seconds=60,
            delivery=_CHAT,
        )
        scripted_runs(trigger, (RunStatus.COMPLETED, "First."), (RunStatus.COMPLETED, "Second."))
        assert (await service._execution._trigger_job_run(job)).succeeded
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert (
            await asyncio.wait_for(service._execution._trigger_job_run(job), timeout=1)
        ).succeeded
        assert trigger.trigger_run.await_count == 2 and chats.sent == []
    finally:
        release.set()
        await service._notifications.flush()
        await service.aclose()
    assert [text for _, text, *_ in chats.sent] == ["First.", "Second."]
