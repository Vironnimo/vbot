"""Durable delivery contracts through CronService, without a platform or Model."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.automation import _cron_notifications as cron_notifications
from core.automation import _cron_timing as cron_timing
from core.automation._cron_delivery import CHAT_MESSAGE_MAX_CHARS
from core.automation.cron import (
    CronDelivery,
    CronFailureAlert,
    CronJobValidationError,
    CronStorageError,
)
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

    # The finished job stays history; its pending delivery still completes independently.
    with pytest.raises(CronJobValidationError):
        await service.update_job(job.id, delivery=CronDelivery(kind="none"))
    # An epoch-seconds retry time of earlier builds is an unknown field: kept, never read.
    jobs_file = tmp_path / "cron" / "jobs.json"
    stored = json.loads(jobs_file.read_text(encoding="utf-8"))
    stored["jobs"][0]["notifications"][0]["retry_at"] = 4102444800.0
    jobs_file.write_text(json.dumps(stored), encoding="utf-8")
    recovered, recovered_trigger = make_service(tmp_path)
    recovered_chats = FakeChats()
    recovered.set_chat_delivery(recovered_chats)
    now = cron_timing._utc_now()
    monkeypatch.setattr(cron_timing, "_utc_now", lambda: now + timedelta(seconds=60))
    if failure == "offline":
        delivered = asyncio.Event()
        original_send = recovered_chats.send

        async def send(
            channel_id: str, message: str, platform_target: str, **kwargs: str | None
        ) -> None:
            await original_send(channel_id, message, platform_target, **kwargs)
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
        alert = recovered.get_job(job.id).unread_alert
        assert alert is not None and alert.kind == "delivery"
        await recovered.retry_delivery(job.id)
        # Retrying after checking the chat answers the alert.
        assert recovered.get_job(job.id).unread_alert is None
        kept = json.loads(jobs_file.read_text(encoding="utf-8"))["jobs"][0]["notifications"]
        assert (kept[0]["retry_at"], kept[0]["retry_after_at"]) == (4102444800.0, None)
        await recovered._notifications.flush()
    assert recovered_chats.sent == [("tg-main", "The result.", "chat-42", "7")]
    assert recovered.get_job(job.id).delivery_progress()["waiting"] == 0
    assert recovered.get_job(job.id).last_delivered_at
    assert recovered_trigger.trigger_run.await_count == 0
    await recovered._notifications.flush()
    assert len(recovered_chats.sent) == 1
    persisted = json.loads(jobs_file.read_text(encoding="utf-8"))
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
@pytest.mark.parametrize("change", ["delete", "agent"])
async def test_a_destination_changed_while_its_notification_claim_saves_prevents_the_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
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
    scripted_runs(trigger, (RunStatus.COMPLETED, "The result."))
    assert (await service._execution._trigger_job_run(job)).succeeded
    claimed, finish_claim = asyncio.Event(), asyncio.Event()
    save = service._save_jobs_async

    async def save_claim() -> None:
        await save()
        if not claimed.is_set():
            claimed.set()
            await finish_claim.wait()

    monkeypatch.setattr(service, "_save_jobs_async", save_claim)
    delivery = asyncio.create_task(service._notifications.flush())
    await claimed.wait()
    try:
        if change == "delete":
            await service.delete_job(job.id)
        else:
            chats.owners["tg-main"] = "agent-two"
    finally:
        finish_claim.set()
    await delivery
    if change == "delete":
        assert service.list_jobs() == []
    else:
        [pending] = service.get_job(job.id).notifications
        assert pending.state == "pending"
        assert pending.error is not None
    assert chats.sent == [] and chats.notes == []
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
        channel_id: str, message: str, platform_target: str, **kwargs: str | None
    ) -> None:
        entered.set()
        await release.wait()
        await original_send(channel_id, message, platform_target, **kwargs)

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


@pytest.mark.asyncio
async def test_a_waiting_notification_holds_back_only_its_own_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, trigger = make_service(tmp_path)
    chats = FakeChats(owners={"tg-main": "agent-one", "tg-other": "agent-one"})
    service.set_chat_delivery(chats)
    stopped = {"tg-main"}
    original_send = chats.send

    async def send(
        channel_id: str, message: str, platform_target: str, **kwargs: str | None
    ) -> None:
        if channel_id in stopped:
            raise ChannelError("Channel is stopped", possibly_delivered=False)
        await original_send(channel_id, message, platform_target, **kwargs)

    monkeypatch.setattr(chats, "send", send)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Check",
        schedule_type="interval",
        interval_seconds=60,
        delivery=_CHAT,
    )
    scripted_runs(
        trigger,
        (RunStatus.COMPLETED, "First."),
        (RunStatus.COMPLETED, "Second."),
        (RunStatus.COMPLETED, "Elsewhere."),
    )
    for _ in range(2):
        await service._execution._trigger_job_run(service.get_job(job.id))
    other = CronDelivery(kind="chat", channel_id="tg-other", platform_target="chat-9")
    await service.update_job(job.id, delivery=other)
    await service._execution._trigger_job_run(service.get_job(job.id))

    await service._notifications.flush()

    # The stopped chat keeps its messages in order; another chat is not held back.
    assert chats.sent == [("tg-other", "Elsewhere.", "chat-9", None)]
    assert service.get_job(job.id).delivery_progress()["waiting"] == 2
    stopped.clear()
    now = cron_timing._utc_now()
    monkeypatch.setattr(cron_timing, "_utc_now", lambda: now + timedelta(seconds=60))
    await service._notifications.flush()
    assert [text for _, text, *_ in chats.sent] == ["Elsewhere.", "First.", "Second."]
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [True, False], ids=["live", "at-startup"])
async def test_an_agent_rename_takes_the_waiting_notifications_along(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: bool
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
    # A finished job is history the rename leaves alone, but not its waiting message.
    history = await service.create_job(
        agent_id="agent-one",
        prompt="Report",
        schedule_type="interval",
        interval_seconds=60,
        remaining_runs=1,
        delivery=_CHAT,
    )
    scripted_runs(trigger, (RunStatus.COMPLETED, "The result."), (RunStatus.COMPLETED, "Done."))
    service.set_chat_delivery(None)
    assert (await service._execution._trigger_job_run(job)).succeeded
    assert (await service._execution._trigger_job_run(history)).succeeded
    assert service.get_job(history.id).status == "completed"
    # The rename moves the Channel first; until the messages follow, they wait.
    chats.owners["tg-main"] = "agent-two"
    service.set_chat_delivery(chats)
    await service._notifications.flush()
    assert chats.sent == []
    if started:
        await service.retarget_agent_async(job.id, "agent-two")
        moved = await service.retarget_notifications_async("agent-one", "agent-two")
    else:
        service.retarget_agent(job.id, "agent-two")
        moved = service.retarget_notifications("agent-one", "agent-two")
    assert moved == (history.id,)
    assert service.get_job(history.id).agent_id == "agent-one"
    now = cron_timing._utc_now()
    monkeypatch.setattr(cron_timing, "_utc_now", lambda: now + timedelta(seconds=30))
    await service._notifications.flush()
    assert sorted(text for _, text, *_ in chats.sent) == ["Done.", "The result."]
    for job_id in (job.id, history.id):
        assert service.get_job(job_id).delivery_progress()["waiting"] == 0
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow_during_lookup", [False, True])
async def test_a_job_keeps_a_bounded_backlog_and_the_next_sent_message_counts_the_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overflow_during_lookup: bool
) -> None:
    monkeypatch.setattr(cron_notifications, "MAX_RETAINED_NOTIFICATIONS", 3)
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
    long_answer = "word " * 2000
    scripted_runs(
        trigger,
        *[(RunStatus.COMPLETED, f"Answer {number}.") for number in (1, 2, 3)],
        (RunStatus.COMPLETED, long_answer),
    )
    service.set_chat_delivery(None)
    for _ in range(3):
        assert (await service._execution._trigger_job_run(job)).succeeded

    lookup_started, lookup_resume = asyncio.Event(), asyncio.Event()
    get_channel = chats.get_channel

    async def held_lookup(channel_id: str) -> SimpleNamespace:
        lookup_started.set()
        await lookup_resume.wait()
        return await get_channel(channel_id)

    flushing = None
    if overflow_during_lookup:
        monkeypatch.setattr(chats, "get_channel", held_lookup)
        service.set_chat_delivery(chats)
        flushing = asyncio.create_task(service._notifications.flush())
        await lookup_started.wait()
    assert (await service._execution._trigger_job_run(job)).succeeded

    stored = make_service(tmp_path)[0].get_job(job.id)
    assert stored.delivery_progress()["waiting"] == 3
    assert stored.dropped_notifications == 1
    # An answer longer than a chat message keeps is cut, and the cut says so.
    kept = stored.notifications[-1].text
    assert len(kept) <= CHAT_MESSAGE_MAX_CHARS < len(long_answer)
    assert kept.endswith("in vBot.]")

    if flushing is not None:
        lookup_resume.set()
        await flushing
    service.set_chat_delivery(chats)
    await service._notifications.flush()
    texts = [text for _, text, *_ in chats.sent]
    assert len(texts) == 3
    first_line, _, rest = texts[0].partition("\n\n")
    assert "1 earlier message" in first_line and rest == "Answer 2."
    assert texts[1:] == ["Answer 3.", kept]
    assert service.get_job(job.id).dropped_notifications == 0
    await service.aclose()


@pytest.mark.asyncio
async def test_messages_for_a_deleted_channel_end_after_a_day_with_an_unread_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, trigger = make_service(tmp_path)
    chats = FakeChats()
    service.set_chat_delivery(chats)
    alerts: list[CronFailureAlert] = []
    service.add_failure_alert_callback(alerts.append)
    job = await service.create_job(
        agent_id="agent-one",
        prompt="Check",
        schedule_type="interval",
        interval_seconds=60,
        delivery=_CHAT,
    )
    scripted_runs(trigger, (RunStatus.COMPLETED, "The result."))
    del chats.owners["tg-main"]
    assert (await service._execution._trigger_job_run(job)).succeeded
    saves = 0
    save = service._save_jobs_async

    async def counted() -> None:
        nonlocal saves
        saves += 1
        await save()

    monkeypatch.setattr(service, "_save_jobs_async", counted)
    start = cron_timing._utc_now()
    clock = [start]
    monkeypatch.setattr(cron_timing, "_utc_now", lambda: clock[0])

    await service._notifications.flush()
    assert saves == 1
    # Waiting on at the usual pace rewrites nothing.
    for minutes in (1, 2, 60):
        clock[0] = start + timedelta(minutes=minutes)
        await service._notifications.flush()
    assert saves == 1 and alerts == []
    assert service.get_job(job.id).delivery_progress()["waiting"] == 1

    clock[0] = start + timedelta(hours=24, minutes=1)
    await service._notifications.flush()
    gone = make_service(tmp_path)[0].get_job(job.id)
    assert gone.delivery_progress()["waiting"] == 0
    assert gone.unread_alert is not None and gone.unread_alert.kind == "delivery"
    assert "tg-main" in gone.unread_alert.message
    [alert] = alerts
    assert (alert.job_id, alert.message) == (job.id, gone.unread_alert.message)
    assert chats.sent == []
    await service.aclose()
