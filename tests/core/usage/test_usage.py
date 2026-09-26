"""Durable Model accounting and upgrade import behavior."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from core.chat.messages import ChatMessage
from core.database import create_data_snapshot, restore_data_snapshot, write_bootstrap_marker
from core.models.pricing import TokenPricing, TokenRates
from core.sessions import ChatSessionManager
from core.usage import UsageRecorder
from tests.core.sessions.history_fixtures import seed_history


@pytest.fixture
def recorder(tmp_path: Path):
    write_bootstrap_marker(tmp_path)
    owner = UsageRecorder(
        tmp_path / "model-usage.db",
        pricing_lookup=lambda _: TokenPricing("fixture", TokenRates(input=2, output=4)),
    )
    yield owner
    owner.close()


@pytest.mark.asyncio
async def test_cumulative_updates_retain_one_call_and_field_provenance(recorder):
    identifier = await recorder.start(model="p/model::p:api", kind="live_voice")
    first = await recorder.update(identifier, {"input_tokens": 12, "reported_cost_usd": 0.1})
    revision, records = recorder.read_since()
    assert first["usage_call_id"] == identifier
    assert "output_tokens" not in records[0].usage
    assert records[0].usage["cost"] == {"amount_usd": 0.1, "source": "provider"}
    await recorder.update(identifier, {"input_tokens": 20, "output_tokens": 3})
    await recorder.finish(identifier)
    newer, changed = recorder.read_since(revision)
    assert newer > revision
    assert len(changed) == 1
    assert changed[0].model == "p/model"
    assert changed[0].status == "completed"
    assert changed[0].usage["input_tokens"] == 20
    assert changed[0].usage["output_tokens"] == 3
    assert recorder.read_since(newer) == (newer, ())


@pytest.mark.asyncio
async def test_unknown_attempts_and_estimate_enrichment_are_not_zero_usage(recorder):
    failed = await recorder.start(model="p/m", kind="image_generation")
    unknown = await recorder.finish(failed, status="failed")
    assert "input_tokens" not in unknown
    assert unknown["cost"]["amount_usd"] is None
    chat = await recorder.start(model="p/m", kind="chat")
    await recorder.finish(chat, {"input_tokens": 5})
    enriched = await recorder.update(
        chat, {"input_tokens": 5, "output_tokens": 4, "output_tokens_estimated": True}
    )
    assert enriched["estimated"] is True
    assert enriched["cost"]["estimated_tokens"] is True
    assert enriched["cost"]["amount_usd"] == pytest.approx(0.000026)
    measured = await recorder.update(chat, {"output_tokens": 2})
    assert "estimated" not in measured
    assert "output_tokens_estimated" not in measured
    assert len(recorder.read_since()[1]) == 2


@pytest.mark.asyncio
async def test_accounting_returns_but_never_stores_producer_usage_metadata(recorder):
    identifier = await recorder.start(model="p/m", kind="chat")
    usage = {
        "input_tokens": 10,
        "output_tokens": 2,
        "context_usage": {"tokens": 8, "estimated": False},
        "producer_extension": {"text": "not accounting data"},
    }
    returned = await recorder.finish(identifier, usage)
    assert returned["context_usage"] == usage["context_usage"]
    assert returned["producer_extension"] == usage["producer_extension"]
    stored = recorder.read_since()[1][0].usage
    assert "context_usage" not in stored
    assert "producer_extension" not in stored


@pytest.mark.asyncio
async def test_restart_marks_unfinished_call_interrupted_and_snapshots_cover_it(recorder):
    identifier = await recorder.start(model="p/m", kind="music_generation")
    path = recorder.database.path
    snapshot = create_data_snapshot(path.parent, reason="test", databases=(recorder.database,))
    assert snapshot is not None
    recorder.close()
    reopened = UsageRecorder(path)
    try:
        assert reopened.projection_epoch != recorder.projection_epoch
        record = reopened.read_since()[1][0]
        assert record.id == identifier
        assert record.status == "interrupted"
        assert record.usage == {}
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_finish_settles_reported_usage_before_repeated_cancellation(recorder, monkeypatch):
    identifier = await recorder.start(model="p/m", kind="chat")
    entered, release = threading.Event(), threading.Event()
    original = recorder._save

    def delayed(*args):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args)

    monkeypatch.setattr(recorder, "_save", delayed)
    pending = asyncio.create_task(recorder.finish(identifier, {"input_tokens": 8}))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert recorder.read_since()[1][0].usage["input_tokens"] == 8


@pytest.mark.asyncio
async def test_history_import_deduplicates_calls_and_retains_archive_and_fork_spend(recorder):
    sessions = ChatSessionManager(recorder.database.path.parent)
    try:
        origin = sessions.create("agent")
        old = ChatMessage.assistant(model="p/m", content="private", usage={"input_tokens": 10})
        seed_history(origin, [old])
        fork = await sessions.fork(origin.address, target_agent_id="agent")
        call_id = await recorder.start(
            model="p/m", kind="chat", agent_id="agent", session_id=fork.id
        )
        usage = await recorder.finish(call_id, {"input_tokens": 7, "output_tokens": 2})
        seed_history(fork, [ChatMessage.assistant(model="p/m", content="secret", usage=usage)])
        await sessions.archive(origin.address)
        recorder.import_session_history(sessions)
        revision, records = recorder.read_since()
        assert len(records) == 2
        assert sum(record.usage.get("input_tokens", 0) for record in records) == 17
        recorder.import_session_history(sessions)
        assert recorder.read_since(revision) == (revision, ())
        sessions.delete(fork.address)
        recorder.import_session_history(sessions)
        assert len(recorder.read_since()[1]) == 2
        assert "private" not in str(records) and "secret" not in str(records)
    finally:
        sessions.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot_status", "interrupted"),
    [("started", False), ("started", True), ("completed", False)],
)
async def test_usage_restore_recovers_existing_call_from_newer_session(
    recorder, snapshot_status, interrupted
):
    root = recorder.database.path.parent
    sessions = ChatSessionManager(root)
    recovered = None
    try:
        session = sessions.create("agent")
        identifier = await recorder.start(
            model="p/m", kind="chat", agent_id="agent", session_id=session.id
        )
        if snapshot_status == "completed":
            await recorder.finish(
                identifier,
                {"input_tokens": 17, "output_tokens": 8, "output_tokens_estimated": True},
            )
        snapshot = create_data_snapshot(
            root, reason="test", databases=(recorder.database, sessions.database)
        )
        assert snapshot is not None
        usage = await recorder.finish(
            identifier,
            {"input_tokens": 17, "output_tokens": 3},
            status="cancelled" if interrupted else "completed",
        )
        seed_history(
            session,
            [
                ChatMessage.assistant(
                    model="p/m", content="answer", usage=usage, interrupted=interrupted
                )
            ],
        )
        path = recorder.database.path
        recorder.close()
        restore_data_snapshot(root, snapshot, names=("model_usage",))
        recovered = UsageRecorder(path)
        recovered.import_session_history(sessions)
        revision, records = recovered.read_since()
        assert len(records) == 1
        record = records[0]
        assert record.id == identifier
        assert record.status == ("interrupted" if interrupted else "completed")
        assert record.usage == usage
        assert record.agent_id == "agent" and record.session_id == session.id
        recovered.import_session_history(sessions)
        assert recovered.read_since(revision) == (revision, ())
    finally:
        if recovered is not None:
            recovered.close()
        sessions.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("provider_field", ["reported_cost_usd", "cost"])
async def test_history_import_retains_stronger_measurements_cost_and_outcome(
    recorder, status, provider_field
):
    sessions = ChatSessionManager(recorder.database.path.parent)
    try:
        provider_cost = {"amount_usd": 0.7, "source": "provider"}
        identifier = await recorder.start(
            model="p/original", kind="chat", agent_id="original", session_id="original-session"
        )
        await recorder.finish(
            identifier,
            {
                "input_tokens": 11,
                provider_field: 0.7 if provider_field == "reported_cost_usd" else provider_cost,
            },
            status=status,
        )
        seed_history(
            sessions.create("agent"),
            [
                ChatMessage.assistant(
                    model="p/older",
                    content="answer",
                    usage={
                        "usage_call_id": identifier,
                        "input_tokens": 3,
                        "estimated": True,
                        "input_tokens_estimated": True,
                        "output_tokens": 4,
                        "output_tokens_estimated": True,
                        "cache_read_tokens": 1,
                        "reported_cost_usd": 0.1,
                    },
                )
            ],
        )
        recorder.import_session_history(sessions)
        record = recorder.read_since()[1][0]
        assert record.status == status
        assert record.usage["input_tokens"] == 11
        assert "input_tokens_estimated" not in record.usage
        assert record.usage["output_tokens"] == 4
        assert record.usage["output_tokens_estimated"] is True
        assert record.usage["cache_read_tokens"] == 1
        assert record.usage["cost"] == provider_cost
        if provider_field == "reported_cost_usd":
            assert record.usage["reported_cost_usd"] == 0.7
        assert record.model == "p/original"
        assert record.agent_id == "original" and record.session_id == "original-session"
    finally:
        sessions.close()


@pytest.mark.parametrize("new_message_count", [0, 1, 2], ids=["empty", "rewound", "caught-up"])
def test_session_restore_replays_reused_entry_keys_and_resets_empty_cursor(
    recorder, new_message_count
):
    root = recorder.database.path.parent
    sessions = ChatSessionManager(root)
    try:
        session = sessions.create("agent")
        address = session.address
        snapshot = create_data_snapshot(
            root, reason="test", databases=(recorder.database, sessions.database)
        )
        assert snapshot is not None
        seed_history(
            session,
            [
                ChatMessage.assistant(model=f"p/old-{n}", content="a", usage={"input_tokens": 2})
                for n in range(2)
            ],
        )
        old_keys = [record["entry_key"] for record in sessions.usage_history()]
        recorder.import_session_history(sessions)
        sessions.close()
        restore_data_snapshot(root, snapshot, names=("sessions",))
        sessions = ChatSessionManager(root)
        restored = sessions.get(address)
        assert restored is not None
        seed_history(
            restored,
            [
                ChatMessage.assistant(model=f"p/new-{n}", content="b", usage={"input_tokens": 3})
                for n in range(new_message_count)
            ],
        )
        assert [record["entry_key"] for record in sessions.usage_history()] == old_keys[
            :new_message_count
        ]
        recorder.import_session_history(sessions)
        if new_message_count == 0:
            seed_history(
                restored,
                [ChatMessage.assistant(model="p/new-0", content="b", usage={"input_tokens": 3})],
            )
            recorder.import_session_history(sessions)
        revision, records = recorder.read_since()
        expected_new = max(1, new_message_count)
        assert len(records) == 2 + expected_new
        assert {record.model for record in records} == {
            "p/old-0",
            "p/old-1",
            *(f"p/new-{n}" for n in range(expected_new)),
        }
        assert sum(record.usage["input_tokens"] for record in records) == 4 + 3 * expected_new
        recorder.import_session_history(sessions)
        assert recorder.read_since(revision) == (revision, ())
    finally:
        sessions.close()


def test_legacy_import_counts_compaction_and_distinct_entries_with_same_message_id(recorder):
    sessions = ChatSessionManager(recorder.database.path.parent)
    try:
        session = sessions.create("agent")
        answer = ChatMessage.assistant(model="p/m", content="a", usage={"input_tokens": 3})
        checkpoint = ChatMessage.compaction_checkpoint(
            summary="summary",
            projection=[],
            compacted_token_count=2,
            context_tokens_before=4,
            context_tokens_after=2,
            strategy="summary_tail",
        )
        checkpoint = replace(
            checkpoint,
            usage={
                **checkpoint.usage,
                "model_call": {
                    "model": "p/summary",
                    "usage": {"input_tokens": 4, "output_tokens": 1},
                },
            },
        )
        seed_history(session, [answer, replace(answer, content="b"), checkpoint])
        recorder.import_session_history(sessions)
        records = recorder.read_since()[1]
        assert len(records) == 3
        assert [record.kind for record in records] == ["chat", "chat", "compaction"]
        assert records[2].model == "p/summary"
        assert records[2].usage["output_tokens"] == 1
    finally:
        sessions.close()
