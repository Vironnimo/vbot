"""Durable Model accounting and upgrade import behavior."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from core.chat.messages import ChatMessage
from core.database import (
    DatabaseUnavailableError,
    create_data_snapshot,
    restore_data_snapshot,
    write_bootstrap_marker,
)
from core.model_tasks.image import ImageExecutionError
from core.models.pricing import TokenPricing, TokenRates
from core.providers.errors import NetworkError, ProviderRateLimitError, ProviderUsageLimitError
from core.sessions import ChatSessionManager
from core.usage import UsagePage, UsageRecorder
from core.utils.errors import ProviderError
from tests.core.sessions.history_fixtures import seed_history
from tests.core.usage.usage_test_support import read_ledger


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
@pytest.mark.parametrize("provider_field", ["reported_cost_usd", "cost"])
async def test_cumulative_updates_retain_one_call_and_field_provenance(recorder, provider_field):
    identifier = await recorder.start(model="p/model::p:api", kind="live_voice")
    provider_cost = {"amount_usd": 0.1, "source": "provider"}
    first = await recorder.update(
        identifier,
        {
            "input_tokens": 12,
            provider_field: 0.1 if provider_field == "reported_cost_usd" else provider_cost,
        },
    )
    revision, records = read_ledger(recorder)
    assert first["usage_call_id"] == identifier
    assert "output_tokens" not in records[0].usage
    assert records[0].usage["cost"] == {"amount_usd": 0.1, "source": "provider"}
    enriched = await recorder.update(identifier, {"input_tokens": 20, "output_tokens": 3})
    assert enriched["cost"] == provider_cost
    enriched = await recorder.update(
        identifier, {"cost": {"amount_usd": 0.0001, "source": "catalog"}}
    )
    assert enriched["cost"] == provider_cost
    updated_cost = {"amount_usd": 0.2, "source": "provider"}
    await recorder.update(
        identifier,
        {provider_field: 0.2 if provider_field == "reported_cost_usd" else updated_cost},
    )
    await recorder.finish(identifier)
    newer, changed = read_ledger(recorder, revision)
    assert newer > revision
    assert len(changed) == 1
    assert changed[0].model == "p/model"
    assert changed[0].status == "completed"
    assert changed[0].usage["input_tokens"] == 20
    assert changed[0].usage["output_tokens"] == 3
    assert changed[0].usage["cost"] == updated_cost
    assert read_ledger(recorder, newer) == (newer, ())


@pytest.mark.asyncio
async def test_changes_stream_in_pages_whose_last_watermark_covers_concurrent_changes(recorder):
    first, second, third = [await recorder.start(model="p/m", kind="decision") for _ in range(3)]
    pages = recorder.read_since(page_size=2)
    assert [record.id for record in next(pages).records] == [first, second]
    # One delivered and one not yet delivered call change between two pages.
    await recorder.finish(first, {"input_tokens": 1})
    await recorder.finish(third, {"input_tokens": 3})
    rest = list(pages)
    assert [(record.id, record.status) for page in rest for record in page.records] == [
        (first, "completed"),
        (third, "completed"),
    ]
    latest = rest[-1].revision
    assert latest == read_ledger(recorder)[0]
    # Nothing changed after the watermark: exactly one empty page arrives.
    assert list(recorder.read_since(latest)) == [UsagePage(latest, ())]


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
    assert len(read_ledger(recorder)[1]) == 2


@pytest.mark.asyncio
async def test_token_enrichment_keeps_snapshot_pricing_after_catalog_refresh(tmp_path):
    write_bootstrap_marker(tmp_path)
    pricing = TokenPricing("original", TokenRates(input=2, output=4))
    owner = UsageRecorder(tmp_path / "model-usage.db", pricing_lookup=lambda _: pricing)
    try:
        identifier = await owner.start(model="p/m", kind="chat")
        original = await owner.finish(identifier, {"input_tokens": 12, "output_tokens": 3})
        pricing = TokenPricing("refreshed", TokenRates(input=20, output=40))

        enriched = await owner.update(identifier, {"output_tokens": 4})

        assert enriched["cost"]["pricing"] == original["cost"]["pricing"]
        assert enriched["cost"]["amount_usd"] == pytest.approx(0.00004)
        later = await owner.start(model="p/m", kind="chat")
        current = await owner.finish(later, {"input_tokens": 12, "output_tokens": 4})
        assert current["cost"]["pricing"] == pricing.to_dict()
        assert current["cost"]["amount_usd"] == pytest.approx(0.0004)
    finally:
        owner.close()


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
    stored = read_ledger(recorder)[1][0].usage
    assert "context_usage" not in stored
    assert "producer_extension" not in stored


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True], ids=["first-write", "retried-write"])
async def test_settlement_records_duration_and_only_a_failure_keeps_its_class(
    tmp_path, monkeypatch, retry
):
    write_bootstrap_marker(tmp_path)
    now = [100.0]
    owner = UsageRecorder(tmp_path / "model-usage.db", clock=lambda: now[0])
    try:
        failed = await owner.start(model="p/m", kind="chat")
        cancelled = await owner.start(model="p/m", kind="chat")
        completed = await owner.start(model="p/m", kind="chat")
        now[0] += 2.5
        await owner.update(failed, {"input_tokens": 3})
        now[0] += 0.25
        error = ProviderError("Provider error: 529", retryable=True)
        error.status_code = 529
        await owner.finish(failed, status="failed", failure=error)
        await owner.finish(cancelled, status="cancelled", failure=error)
        if retry:
            write = owner.database.write

            def failed_write(operation):
                def fail(connection):
                    operation(connection)
                    raise DatabaseUnavailableError("test settlement rollback")

                return write(fail)

            with monkeypatch.context() as blocked:
                blocked.setattr(owner.database, "write", failed_write)
                with pytest.raises(DatabaseUnavailableError):
                    await owner.finish(completed, failure=error)
            now[0] += 60
        await owner.finish(completed, failure=error)
        now[0] += 60
        # A repeated settlement keeps the first duration.
        await owner.finish(completed)
        outcomes = {
            record.id: (record.duration_ms, record.failure_class, record.http_status)
            for record in read_ledger(owner)[1]
        }
        assert outcomes == {
            failed: (2750, "server", 529),
            cancelled: (2750, None, None),
            completed: (2750, None, None),
        }
    finally:
        owner.close()


def _wrapped(error: BaseException, wrapper: BaseException) -> BaseException:
    try:
        raise wrapper from error
    except BaseException as raised:
        return raised


def _status_error(status: int) -> ProviderError:
    error = ProviderError(f"Provider error: {status}", retryable=False)
    error.status_code = status
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ProviderRateLimitError("Rate limited"), ("rate_limit", None)),
        (ProviderUsageLimitError("Quota exhausted"), ("usage_limit", None)),
        (_status_error(404), ("rejected", 404)),
        (_status_error(504), ("timeout", 504)),
        # A wrapper without a class of its own defers to its cause and its status.
        (_wrapped(_status_error(503), ImageExecutionError("failed")), ("server", 503)),
        (_wrapped(NetworkError("reset"), ImageExecutionError("failed")), ("network", None)),
        # An in-band Provider error has neither a subclass nor a status.
        (ProviderError("overloaded", retryable=True), ("provider", None)),
        (ValueError("bug"), ("internal", None)),
        ("incomplete", ("incomplete", None)),
    ],
)
async def test_a_failure_is_kept_as_its_class_and_http_status(recorder, failure, expected):
    identifier = await recorder.start(model="p/m", kind="image_generation")
    await recorder.finish(identifier, status="failed", failure=failure)
    record = read_ledger(recorder)[1][0]
    assert (record.failure_class, record.http_status) == expected


@pytest.mark.asyncio
async def test_restart_marks_unfinished_call_interrupted_and_snapshots_cover_it(recorder):
    identifier = await recorder.start(model="p/m", kind="music_generation")
    path = recorder.database.path
    snapshot = create_data_snapshot(path.parent, reason="test", databases=(recorder.database,))
    assert snapshot is not None
    recorder.close()
    reopened = UsageRecorder(path)
    try:
        record = read_ledger(reopened)[1][0]
        assert record.id == identifier
        assert record.status == "interrupted"
        assert record.usage == {}
        assert (record.duration_ms, record.failure_class, record.http_status) == (None,) * 3
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["start", "finish"])
async def test_cancelled_writes_settle_before_repeated_cancellation(recorder, monkeypatch, step):
    """A started call never stays started, and reported Usage is never lost, on cancel."""
    identifier = await recorder.start(model="p/m", kind="chat") if step == "finish" else None
    entered, release = threading.Event(), threading.Event()
    owner, name = (recorder, "_save") if step == "finish" else (recorder.database, "write")
    original = getattr(owner, name)

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, delayed)
    pending = asyncio.create_task(
        recorder.finish(identifier, {"input_tokens": 8})
        if identifier is not None
        else recorder.start(model="p/m", kind="chat")
    )
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
    [record] = read_ledger(recorder)[1]
    if step == "finish":
        assert record.usage["input_tokens"] == 8
    else:
        assert record.status == "cancelled"


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
        seed_history(
            fork,
            [
                ChatMessage.assistant(model="p/m", content="secret", usage=usage),
                ChatMessage.assistant(model="p/m", content="caption"),
                replace(ChatMessage.assistant(model="p/m", content="partial"), interrupted=True),
            ],
        )
        await sessions.archive(origin.address)
        recorder.import_session_history(sessions)
        revision, records = read_ledger(recorder)
        assert len(records) == 2
        assert (
            next(record for record in records if record.id == call_id).session_generation_id
            == sessions.list_history_versions([fork.address])[fork.address][0]
        )
        assert sum(record.usage.get("input_tokens", 0) for record in records) == 17
        recorder.import_session_history(sessions)
        assert read_ledger(recorder, revision) == (revision, ())
        sessions.delete(fork.address)
        recorder.import_session_history(sessions)
        assert len(read_ledger(recorder)[1]) == 2
        assert "private" not in str(records) and "secret" not in str(records)
    finally:
        sessions.close()


def test_a_restart_keeps_the_ledger_id_and_resumes_the_import_from_its_cursor(
    recorder, monkeypatch
):
    root = recorder.database.path.parent
    sessions = ChatSessionManager(root)
    try:
        seed_history(
            sessions.create("agent"),
            [
                ChatMessage.assistant(model="p/m", content="a", usage={"input_tokens": 2})
                for _ in range(2)
            ],
        )
        recorder.import_session_history(sessions)
        cursor = sessions.usage_history()[-1]["entry_key"]
    finally:
        sessions.close()
    path, ledger_id = recorder.database.path, recorder.ledger_id
    recorder.close()
    reopened, sessions = UsageRecorder(path), ChatSessionManager(root)
    try:
        assert reopened.ledger_id == ledger_id
        reads: list[int] = []
        history = sessions.usage_history

        def recorded(after_entry_key: int = 0, *, limit: int = 1000):
            reads.append(after_entry_key)
            return history(after_entry_key, limit=limit)

        monkeypatch.setattr(sessions, "usage_history", recorded)
        reopened.import_session_history(sessions)
        assert reads == [cursor]
    finally:
        reopened.close()
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
        ledger_id = recorder.ledger_id
        recorder.close()
        restore_data_snapshot(root, snapshot, names=("model_usage",))
        recovered = UsageRecorder(path)
        # Projections of the restored ledger rebuild instead of continuing.
        assert recovered.ledger_id != ledger_id
        recovered.import_session_history(sessions)
        revision, records = read_ledger(recovered)
        assert len(records) == 1
        record = records[0]
        assert record.id == identifier
        assert record.status == ("interrupted" if interrupted else "completed")
        assert record.usage == usage
        assert record.agent_id == "agent" and record.session_id == session.id
        recovered.import_session_history(sessions)
        assert read_ledger(recovered, revision) == (revision, ())
    finally:
        if recovered is not None:
            recovered.close()
        sessions.close()


SPEED = {
    "first_token_ms": 900,
    "duration_ms": 4100,
    "output_tokens_per_second": 62.5,
    "tokens": 200,
    "window_ms": 3200,
    "basis": "output",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "provider_field"),
    [
        # Imported history reads as completed, so only a non-completed settled
        # outcome proves the recorded outcome is retained; failed and cancelled
        # share that rule.
        ("failed", "reported_cost_usd"),
        ("cancelled", "cost"),
    ],
)
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
                        "speed": SPEED,
                    },
                )
            ],
        )
        recorder.import_session_history(sessions)
        record = read_ledger(recorder)[1][0]
        assert record.status == status
        assert record.usage["input_tokens"] == 11
        assert "input_tokens_estimated" not in record.usage
        assert record.usage["output_tokens"] == 4
        assert record.usage["output_tokens_estimated"] is True
        assert record.usage["cache_read_tokens"] == 1
        assert record.usage["speed"] == SPEED
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
        revision, records = read_ledger(recorder)
        expected_new = max(1, new_message_count)
        assert len(records) == 2 + expected_new
        assert {record.model for record in records} == {
            "p/old-0",
            "p/old-1",
            *(f"p/new-{n}" for n in range(expected_new)),
        }
        assert sum(record.usage["input_tokens"] for record in records) == 4 + 3 * expected_new
        recorder.import_session_history(sessions)
        assert read_ledger(recorder, revision) == (revision, ())
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
        records = read_ledger(recorder)[1]
        assert len(records) == 3
        assert [record.kind for record in records] == ["chat", "chat", "compaction"]
        assert records[2].model == "p/summary"
        assert records[2].usage["output_tokens"] == 1
    finally:
        sessions.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_call", [False, True], ids=["record", "call_update"])
async def test_input_estimate_calibration_learns_per_model_and_survives_restart(
    recorder, with_call
):
    model = "anthropic/claude::anthropic:api"

    async def learn(measured: int, estimated: int, **flags: bool) -> None:
        if not with_call:
            recorder.record_input_estimate(model, measured=measured, estimated=estimated)
            return
        # A call's measured input teaches the calibration with its Usage update.
        call = await recorder.start(model=model, kind="chat")
        await recorder.update(
            call, {"input_tokens": measured, **flags}, input_estimate=(model, estimated)
        )

    assert recorder.input_estimate_factor(model) == 1.0
    # Tiny requests and implausible ratios are not evidence.
    await learn(900, 600)
    await learn(500_000, 100_000)
    if with_call:
        # Nor is an estimated input.
        await learn(125_000, 100_000, input_tokens_estimated=True)
    assert recorder.input_estimate_factor(model) == 1.0
    for _ in range(40):
        await learn(125_000, 100_000)
    learned = recorder.input_estimate_factor(model)
    assert 1.2 < learned < 1.25
    # The Connection scope is not part of the calibrated identity; other Models are unaffected.
    assert recorder.input_estimate_factor("anthropic/claude::anthropic:other") == learned
    assert recorder.input_estimate_factor("openai/gpt") == 1.0
    path = recorder.database.path
    recorder.close()
    reopened = UsageRecorder(path)
    try:
        assert reopened.input_estimate_factor(model) == learned
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("final", [{"input_tokens": 125_000}, {}, {"output_tokens": 2}])
async def test_retry_usage_calibrates_from_only_the_final_attempt(recorder, final):
    model = "openai/retry-calibration"
    call = await recorder.start(model=model, kind="chat")
    usage = {
        "input_tokens": 200_000,
        "output_tokens": 10,
        "last_request_usage": final,
        "context_usage": {"tokens": 1},
    }
    finished = await recorder.finish(call, usage)
    assert finished["last_request_usage"] == final
    assert finished["context_usage"] == {"tokens": 1}
    updated = await recorder.update(call, finished, input_estimate=(model, 100_000))
    assert updated["input_tokens"] == 200_000
    assert updated["last_request_usage"] == final
    assert (recorder.input_estimate_factor(model) > 1.0) is ("input_tokens" in final)
    [record] = read_ledger(recorder)[1]
    assert record.usage["input_tokens"] == 200_000
    assert "last_request_usage" not in record.usage


@pytest.mark.asyncio
async def test_a_known_session_generation_is_recorded_without_a_session_read(tmp_path):
    class UnreadSessions:
        def list_history_versions(self, addresses):
            raise AssertionError("the caller already knows the generation")

        async def run_async(self, function, *arguments, **keyword_arguments):
            return function(*arguments, **keyword_arguments)

    write_bootstrap_marker(tmp_path)
    owner = UsageRecorder(tmp_path / "model-usage.db", session_source=UnreadSessions())
    try:
        await owner.start(
            model="p/m",
            kind="chat",
            agent_id="agent",
            session_id="session",
            session_generation_id="generation-one",
        )
        [record] = read_ledger(owner)[1]
        assert record.session_generation_id == "generation-one"
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_input_estimate_calibration_of_another_estimator_version_starts_afresh(recorder):
    model = "openai/gpt::openai:subscription"
    for _ in range(40):
        recorder.record_input_estimate(model, measured=50_000, estimated=100_000)
    assert recorder.input_estimate_factor(model) < 0.6
    path = recorder.database.path
    recorder.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE input_estimate_calibration SET estimator_version=NULL")
    reopened = UsageRecorder(path)
    try:
        assert reopened.input_estimate_factor(model) == 1.0
        reopened.record_input_estimate(model, measured=100_000, estimated=100_000)
        assert reopened.input_estimate_factor(model) == 1.0
    finally:
        reopened.close()
