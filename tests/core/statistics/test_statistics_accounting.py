"""Durable Model accounting stays complete without inventing Session activity."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from core.runs import Run, RunExecutionOwner
from core.sessions import ChatSessionManager, SessionAddress
from core.statistics import AgentDirectory, StatisticsService
from core.usage import UsageRecorder
from core.utils.timestamps import format_canonical_timestamp
from tests.core.sessions.history_fixtures import complete_run
from tests.core.statistics.statistics_test_support import (
    BASE,
    _assistant,
    _compaction,
    _FakeAgents,
    _run_summary,
    _write_session,
)


@pytest.fixture
def accounting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manager = ChatSessionManager(tmp_path)
    recorder = UsageRecorder(tmp_path / "model-usage.db")
    service = StatisticsService(
        manager, cast(AgentDirectory, _FakeAgents(["main"])), usage_recorder=recorder
    )
    monkeypatch.setattr(
        "core.usage.usage.utc_now_timestamp", lambda: format_canonical_timestamp(BASE)
    )
    yield service, manager, recorder
    service._index.close()
    recorder.close()


@pytest.mark.asyncio
async def test_standalone_task_and_auxiliary_calls_count_without_fake_sessions(accounting):
    service, _manager, recorder = accounting
    kinds = [
        "speech_to_text",
        "text_to_speech",
        "image_understanding",
        "image_generation",
        "video_generation",
        "music_generation",
        "text_embedding",
        "decision",
        "live_voice",
        "live_voice_backend",
        "session_title",
        "group_title",
        "extension_sampling",
    ]
    for index, kind in enumerate(kinds):
        call = await recorder.start(model="tasks/model::private-connection", kind=kind)
        await recorder.finish(
            call,
            {"input_tokens": 10, "output_tokens": 4, "reported_cost_usd": 0.25}
            if index == 0
            else None,
        )

    report = service.report()

    assert report.overview.total_sessions == 0
    assert report.overview.total_chat_messages == 0
    assert report.overview.total_session_records == 0
    assert report.usage.totals.assistant_messages == 0
    assert report.usage.totals.chat_calls == 0
    assert report.usage.totals.auxiliary_calls == len(kinds)
    assert report.usage.totals.model_calls == len(kinds)
    assert report.usage.totals.unreported_calls == len(kinds) - 1
    assert report.usage.totals.measured_input_tokens == 10
    assert report.costs.totals.calls == len(kinds)
    assert report.costs.totals.reported_usd == 0.25
    assert report.costs.totals.unpriced_calls == len(kinds) - 1
    assert {call.status for call in report.costs.recent_calls} == {"completed"}
    assert report.costs.top_sessions == []
    assert {row.kind for row in report.costs.recent_calls} == set(kinds)
    assert {row.kind for row in report.usage.kinds} == set(kinds)
    assert report.usage.models[0].model == "tasks/model"
    assert all(row.session_id == "" and row.agent_id == "" for row in report.costs.recent_calls)


@pytest.mark.asyncio
async def test_history_deduplicates_and_usage_survives_archive_delete_and_rebuild(accounting):
    service, manager, recorder = accounting
    session = manager.create("main").start_run("run")
    call = await recorder.start(
        model="chat/m", kind="chat", agent_id="main", session_id=session.id, run_id="run"
    )
    usage = await recorder.finish(
        call, {"input_tokens": 20, "output_tokens": 5, "reported_cost_usd": 0.1}
    )
    session.append(_assistant(model="chat/m", at=BASE, usage=usage))
    compaction = await recorder.start(
        model="summary/m", kind="compaction", agent_id="main", session_id=session.id, run_id="run"
    )
    compaction_usage = await recorder.finish(compaction, {"input_tokens": 30, "output_tokens": 3})
    checkpoint = _compaction(at=BASE + timedelta(seconds=1), before=100, after=30)
    session.append(
        replace(
            checkpoint,
            usage={
                **checkpoint.usage,
                "model_call": {"model": "summary/m", "usage": compaction_usage},
            },
        )
    )
    complete_run(
        session,
        _run_summary(
            status="completed", at=BASE + timedelta(seconds=2), duration_ms=2000, run_id="run"
        ),
    )
    other = _write_session(
        manager,
        "main",
        [_assistant(model="old/m", at=BASE, usage={"input_tokens": 7, "output_tokens": 2})],
    )

    report = service.report()
    assert report.usage.totals.model_calls == 3
    assert report.usage.totals.assistant_messages == 2
    assert report.usage.totals.compaction_calls == 1
    assert report.usage.totals.measured_input_tokens == 57
    assert service.report().costs.totals == report.costs.totals

    await manager.archive(session.address)
    manager.delete(SessionAddress(None, "main", other))
    archived = service.report()
    assert archived.overview.total_sessions == 0
    assert archived.usage.totals.assistant_messages == 0
    assert archived.usage.totals.model_calls == 3
    assert archived.costs.totals == report.costs.totals
    service._index.discard()
    assert service.report().costs.totals == report.costs.totals
    manager.restore(session.address)
    assert service.report().usage.totals.model_calls == 3


@pytest.mark.asyncio
async def test_cumulative_updates_replace_counters_and_unchanged_reads_do_not_write(accounting):
    service, _manager, recorder = accounting
    call = await recorder.start(model="voice/m", kind="live_voice")
    assert service.report().usage.totals.unreported_calls == 1
    await recorder.update(call, {"input_tokens": 10, "output_tokens": 2})
    assert service.report().usage.totals.measured_input_tokens == 10
    await recorder.finish(call, {"input_tokens": 16, "output_tokens": 5, "reported_cost_usd": 0})
    report = service.report()
    assert report.usage.totals.model_calls == 1
    assert report.usage.totals.measured_input_tokens == 16
    assert report.usage.totals.measured_output_tokens == 5
    assert report.costs.totals.reported_usd == 0
    with sqlite3.connect(service._index.index_path) as observer:
        before = observer.execute("PRAGMA data_version").fetchone()[0]
        assert service.report().costs == report.costs
        service.warm_index()
        assert observer.execute("PRAGMA data_version").fetchone()[0] == before


@pytest.mark.asyncio
async def test_ledger_failure_propagates_without_discarding_index(accounting, monkeypatch):
    service, _manager, recorder = accounting
    call = await recorder.start(model="task/m", kind="decision")
    await recorder.finish(call, {"input_tokens": 1, "output_tokens": 1})
    service.report()
    original = service._index.index_path.read_bytes()

    def broken(_revision):
        raise sqlite3.DatabaseError("canonical usage unavailable")

    monkeypatch.setattr(recorder, "read_since", broken)
    with pytest.raises(sqlite3.DatabaseError, match="canonical usage unavailable"):
        service.report()
    assert service._index.index_path.read_bytes() == original


@pytest.mark.asyncio
async def test_auxiliary_requests_keep_request_windows_and_chat_cache_sequence(
    accounting, monkeypatch
):
    service, manager, recorder = accounting
    first = {"input_tokens": 4000, "output_tokens": 20, "cache_read_tokens": 0}
    second = {"input_tokens": 4200, "output_tokens": 20, "cache_read_tokens": 4000}
    identifier = _write_session(
        manager,
        "main",
        [
            _assistant(model="chat/m", at=BASE, usage=first),
            _assistant(model="chat/m", at=BASE + timedelta(seconds=2), usage=second),
        ],
    )
    monkeypatch.setattr(
        "core.usage.usage.utc_now_timestamp",
        lambda: format_canonical_timestamp(BASE + timedelta(seconds=1)),
    )
    call = await recorder.start(
        model="title/m", kind="title", agent_id="main", session_id=identifier
    )
    await recorder.finish(call, {"input_tokens": 1, "output_tokens": 1})

    report = service.report()
    assert report.usage.totals.model_calls == 3
    assert report.usage.cache.suspected_breaks.evaluated_turns == 1
    assert report.usage.cache.suspected_breaks.suspected_turns == 0
    assert report.usage.cache.lowest_hit_rate_sessions[0].cache_turns == 2
    narrow = service.report(
        since=BASE + timedelta(microseconds=1), until=BASE + timedelta(seconds=1)
    )
    assert narrow.usage.totals.model_calls == 1
    assert narrow.usage.totals.assistant_messages == 0
    assert narrow.costs.recent_calls[0].kind == "title"


@pytest.mark.asyncio
async def test_run_activity_counts_unsaved_and_auxiliary_attempts(accounting):
    service, manager, recorder = accounting
    session = manager.create("main").start_run("run")
    session.append(
        _assistant(model="chat/m", at=BASE, usage={"input_tokens": 5, "output_tokens": 1})
    )
    complete_run(
        session,
        _run_summary(
            status="completed", at=BASE + timedelta(seconds=2), duration_ms=2000, run_id="run"
        ),
    )
    for kind, model, status in [
        ("chat", "retry/m", "failed"),
        ("image_generation", "image/m", "completed"),
    ]:
        call = await recorder.start(
            model=model, kind=kind, agent_id="main", session_id=session.id, run_id="run"
        )
        await recorder.finish(call, {"input_tokens": 10, "output_tokens": 2}, status=status)

    activity = service.run_activity(since=BASE, until=BASE + timedelta(seconds=3))

    assert activity.runs[0].measured_input_tokens == 25
    assert activity.runs[0].measured_output_tokens == 5
    assert activity.runs[0].models == ["chat/m", "image/m", "retry/m"]
    report = service.report()
    assert report.usage.totals.assistant_messages == 1
    assert report.usage.totals.chat_calls == 2
    assert {row.model: row.runs for row in report.usage.models} == {
        "chat/m": 1,
        "retry/m": 1,
        "image/m": 1,
    }
    assert all(row.average_run_duration_ms == 2000 for row in report.usage.models)


@pytest.mark.asyncio
async def test_group_usage_includes_tasks_only_in_the_owned_run(accounting):
    service, manager, recorder = accounting
    binding = manager.create_bound_temporary_session(
        SessionAddress(None, "temporary", "participant"),
        owner_name="swarm",
        group_id="group",
        participant_id="peer",
        config={},
    )
    session = manager.create("main")
    owner = RunExecutionOwner("swarm", "group", "peer", binding.generation_id, "epoch")
    await manager.start_run(
        Run(run_id="owned", agent_id="main", session_id=session.id, execution_owner=owner)
    )
    session = session.for_run("owned")
    session.append(
        _assistant(model="chat/m", at=BASE, usage={"input_tokens": 5, "output_tokens": 1})
    )
    for run, tokens in [("owned", 10), ("outside", 100)]:
        call = await recorder.start(
            model="task/m", kind="decision", agent_id="main", session_id=session.id, run_id=run
        )
        await recorder.finish(call, {"input_tokens": tokens, "output_tokens": 2})

    report = await service.group_usage(owner_name="swarm", group_id="group")

    assert report["usage"]["totals"]["model_calls"] == 2
    assert report["usage"]["totals"]["measured_input_tokens"] == 15
    assert report["participants"][0]["usage"]["totals"]["model_calls"] == 2


@pytest.mark.asyncio
async def test_takeover_keeps_saved_run_usage_without_reassigning_durable_calls(accounting):
    service, manager, recorder = accounting
    binding = manager.create_bound_temporary_session(
        SessionAddress(None, "temporary", "participant"),
        owner_name="swarm",
        group_id="group",
        participant_id="peer",
        config={},
    )
    session = manager.create("before")
    owner = RunExecutionOwner("swarm", "group", "peer", binding.generation_id, "epoch")
    await manager.start_run(
        Run(run_id="owned", agent_id="before", session_id=session.id, execution_owner=owner)
    )
    session = session.for_run("owned")
    call = await recorder.start(
        model="chat/m", kind="chat", agent_id="before", session_id=session.id, run_id="owned"
    )
    usage = await recorder.finish(call, {"input_tokens": 5, "output_tokens": 1})
    session.append(_assistant(model="chat/m", at=BASE, usage=usage))
    complete_run(
        session,
        _run_summary(
            status="completed", at=BASE + timedelta(seconds=2), duration_ms=2000, run_id="owned"
        ),
    )
    for agent, identifier, tokens in [("before", session.id, 10), ("main", "unrelated", 100)]:
        call = await recorder.start(
            model="task/m", kind="decision", agent_id=agent, session_id=identifier, run_id="owned"
        )
        await recorder.finish(call, {"input_tokens": tokens, "output_tokens": 2})
    service.report()

    await manager.move(session.address, SessionAddress(None, "main", session.id))

    activity = service.run_activity(since=BASE, until=BASE + timedelta(seconds=3))
    assert activity.runs[0].agent_id == "main"
    assert activity.runs[0].measured_input_tokens == 5
    assert activity.runs[0].measured_output_tokens == 1
    assert activity.runs[0].models == ["chat/m"]
    group = await service.group_usage(owner_name="swarm", group_id="group")
    assert group["usage"]["totals"]["model_calls"] == 1
    assert group["usage"]["totals"]["measured_input_tokens"] == 5
    report = service.report()
    assert report.usage.totals.model_calls == 3
    assert report.usage.totals.measured_input_tokens == 115
    chat = next(row for row in report.costs.recent_calls if row.model == "chat/m")
    assert chat.agent_id == "before"


@pytest.mark.asyncio
async def test_partial_counters_remain_incomplete_without_invented_cache_or_output(accounting):
    service, _manager, recorder = accounting
    partial = await recorder.start(model="task/m", kind="speech_to_text")
    await recorder.finish(partial, {"input_tokens": 12, "input_tokens_estimated": True})
    details = await recorder.start(model="task/m", kind="live_voice")
    await recorder.finish(details, {"cache_read_tokens": 7, "reasoning_tokens": 4})

    totals = service.report().usage.totals

    assert totals.model_calls == 2
    assert totals.unreported_calls == 2
    assert totals.estimated_input_tokens == 12
    assert totals.measured_output_tokens == 0
    assert totals.cache_turns == 0
    assert totals.cache_read_tokens == 0
    assert totals.reasoning_turns == 0


@pytest.mark.asyncio
async def test_restored_ledger_revision_rebuilds_its_projection(accounting, monkeypatch):
    service, _manager, recorder = accounting
    first = await recorder.start(model="task/m", kind="decision")
    await recorder.finish(first, {"input_tokens": 10, "output_tokens": 1})
    saved_revision, saved_records = recorder.read_since()
    second = await recorder.start(model="task/m", kind="decision")
    await recorder.finish(second, {"input_tokens": 20, "output_tokens": 2})
    assert service.report().usage.totals.model_calls == 2

    def restored(revision=0):
        return saved_revision, tuple(
            record for record in saved_records if record.revision > revision
        )

    monkeypatch.setattr(recorder, "read_since", restored)
    report = service.report()
    assert report.usage.totals.model_calls == 1
    assert report.usage.totals.measured_input_tokens == 10


@pytest.mark.asyncio
async def test_windowed_extension_activity_includes_requests_without_saved_output(accounting):
    service, manager, recorder = accounting
    binding = manager.create_bound_temporary_session(
        SessionAddress(None, "temporary", "participant"),
        owner_name="swarm",
        group_id="group",
        participant_id="peer",
        config={},
    )
    call = await recorder.start(
        model="task/m",
        kind="decision",
        agent_id=binding.address.agent_id,
        session_id=binding.address.session_id,
    )
    await recorder.finish(call, {"input_tokens": 10, "output_tokens": 2})

    report = service.report(since=BASE, until=BASE + timedelta(seconds=1))

    assert report.overview.total_session_records == 0
    assert report.usage.totals.model_calls == 1
    activity = report.extensions.extensions[0].activity
    assert activity.model_calls == 1
    assert activity.measured_input_tokens == 10
    assert activity.last_activity == format_canonical_timestamp(BASE)


@pytest.mark.asyncio
async def test_new_recorder_lifetime_reconciles_a_restored_revision_that_caught_up(
    accounting, monkeypatch
):
    service, _manager, recorder = accounting
    for model in ("task/a", "task/b"):
        call = await recorder.start(model=model, kind="decision")
        await recorder.finish(call, {"input_tokens": 10, "output_tokens": 1})
    high, records = recorder.read_since()
    assert {model.model for model in service.report().usage.models} == {"task/a", "task/b"}
    epoch = recorder.projection_epoch
    path = recorder.database.path
    recorder.close()
    replacement = UsageRecorder(path)
    try:
        assert replacement.projection_epoch != epoch
        service._usage_recorder = replacement
        # A stopped-store restore retains database identity. New work can
        # catch up numerically before the first report of the new Runtime.
        restored = (records[0], replace(records[1], id="restored-new", model="task/c"))
        monkeypatch.setattr(
            replacement,
            "read_since",
            lambda revision=0: (
                high,
                tuple(record for record in restored if record.revision > revision),
            ),
        )
        assert {model.model for model in service.report().usage.models} == {"task/a", "task/c"}
    finally:
        replacement.close()
