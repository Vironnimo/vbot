"""Durable Model accounting stays complete without inventing Session activity."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.database import create_data_snapshot, restore_data_snapshot
from core.runs import Run, RunExecutionOwner
from core.sessions import ChatSessionManager, SessionAddress
from core.statistics import StatisticsService
from core.statistics._projection import CALL_COLUMNS
from core.statistics.index import StatisticsIndex
from core.usage import UsagePage, UsageRecorder
from core.utils.timestamps import format_canonical_timestamp
from tests.core.sessions.history_fixtures import complete_run
from tests.core.statistics.statistics_test_support import (
    BASE,
    StatisticsFactory,
    _assistant,
    _compaction,
    _run_summary,
    _write_session,
)
from tests.core.usage.usage_test_support import read_ledger


@pytest.fixture
def accounting(
    tmp_path: Path,
    manager: ChatSessionManager,
    index: StatisticsIndex,
    statistics: StatisticsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[StatisticsService, ChatSessionManager, UsageRecorder]]:
    """A service over ``index`` whose durable Usage ledger starts every call at BASE."""
    recorder = UsageRecorder(tmp_path / "model-usage.db")
    monkeypatch.setattr(
        "core.usage.usage.utc_now_timestamp", lambda: format_canonical_timestamp(BASE)
    )
    yield statistics(usage_recorder=recorder, index=index), manager, recorder
    recorder.close()


def _usage(service: StatisticsService, **request: Any) -> dict[str, Any]:
    usage: dict[str, Any] = service.report(sections=["usage"], **request)["usage"]
    return usage


def _session_records(report: dict[str, Any]) -> int:
    return sum(report["diagnostics"]["roles"]["session_records_by_role"].values())


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

    report = service.report(sections=["overview", "usage", "diagnostics"])

    assert report["overview"]["active_sessions"] == report["overview"]["active_agents"] == 0
    assert _session_records(report) == 0
    usage = report["usage"]
    totals = usage["totals"]
    assert totals["calls"] == len(kinds)
    assert totals["unreported_calls"] == totals["unpriced_calls"] == len(kinds) - 1
    assert totals["input_tokens"] == 10
    assert totals["reported_cost_usd"] == 0.25
    assert {row["key"] for row in usage["breakdowns"]["kind"]} == set(kinds)
    assert [row["key"] for row in usage["breakdowns"]["model"]] == ["tasks/model"]
    assert [row["key"] for row in usage["breakdowns"]["origin"]] == ["background"]
    assert usage["top_sessions"] == []
    assert {row["kind"] for row in usage["recent_calls"]} == set(kinds)
    assert {row["status"] for row in usage["recent_calls"]} == {"completed"}
    assert all(row["session_id"] is None and row["agent_id"] == "" for row in usage["recent_calls"])


@pytest.mark.asyncio
async def test_history_deduplicates_and_usage_survives_archive_delete_and_rebuild(
    accounting, index: StatisticsIndex
):
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
                **(checkpoint.usage or {}),
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

    report = service.report(sections=["usage", "diagnostics"])
    totals = report["usage"]["totals"]
    # The saved Chat and Compaction steps carry their ledger ids and count once.
    assert (totals["calls"], totals["input_tokens"]) == (3, 57)
    assert {row["key"]: row["calls"] for row in report["usage"]["breakdowns"]["kind"]} == {
        "chat": 2,
        "compaction": 1,
    }
    assert _session_records(report) > 0
    assert _usage(service)["totals"] == totals

    archived_entry = await manager.archive(session.address)
    manager.delete(SessionAddress(None, "main", other))
    archived = service.report(sections=["usage", "diagnostics"])
    assert _session_records(archived) == 0
    assert archived["usage"]["totals"] == totals
    index.discard()
    assert _usage(service)["totals"] == totals
    manager.archive_ledger.begin_restore(archived_entry.entry_id, {"target_id": None})
    manager.archive_ledger.commit_restore(archived_entry.entry_key)
    assert _usage(service)["totals"]["calls"] == 3


@pytest.mark.asyncio
async def test_cumulative_updates_replace_counters_and_unchanged_reads_do_not_write(
    accounting, index: StatisticsIndex
):
    service, _manager, recorder = accounting
    call = await recorder.start(model="voice/m", kind="live_voice")
    assert _usage(service)["totals"]["unreported_calls"] == 1
    await recorder.update(call, {"input_tokens": 10, "output_tokens": 2})
    assert _usage(service)["totals"]["input_tokens"] == 10
    await recorder.finish(call, {"input_tokens": 16, "output_tokens": 5, "reported_cost_usd": 0})
    totals = _usage(service)["totals"]
    assert (totals["calls"], totals["input_tokens"], totals["output_tokens"]) == (1, 16, 5)
    assert totals["reported_cost_usd"] == 0.0
    with closing(sqlite3.connect(index.index_path)) as observer:
        before = observer.execute("PRAGMA data_version").fetchone()[0]
        assert _usage(service)["totals"] == totals
        service.warm_index()
        assert observer.execute("PRAGMA data_version").fetchone()[0] == before


@pytest.mark.asyncio
async def test_ledger_failure_propagates_without_discarding_index(
    accounting, index: StatisticsIndex, monkeypatch
):
    service, _manager, recorder = accounting
    call = await recorder.start(model="task/m", kind="decision")
    await recorder.finish(call, {"input_tokens": 1, "output_tokens": 1})
    service.report()
    original = index.index_path.read_bytes()

    def broken(_revision):
        raise sqlite3.DatabaseError("canonical usage unavailable")

    monkeypatch.setattr(recorder, "read_since", broken)
    with pytest.raises(sqlite3.DatabaseError, match="canonical usage unavailable"):
        service.report()
    assert index.index_path.read_bytes() == original


@pytest.mark.asyncio
async def test_auxiliary_requests_keep_request_windows_and_chat_cache_sequence(accounting):
    service, manager, recorder = accounting
    first = {"input_tokens": 4000, "output_tokens": 20, "cache_read_tokens": 0}
    second = {"input_tokens": 4200, "output_tokens": 20, "cache_read_tokens": 4000}
    # Two Chat steps around an hour boundary with a title request between them.
    identifier = _write_session(
        manager,
        "main",
        [
            _assistant(model="chat/m", at=BASE - timedelta(seconds=1), usage=first),
            _assistant(model="chat/m", at=BASE + timedelta(seconds=1), usage=second),
        ],
    )
    call = await recorder.start(
        model="title/m", kind="title", agent_id="main", session_id=identifier
    )
    await recorder.finish(call, {"input_tokens": 1, "output_tokens": 1})

    report = service.report(sections=["usage", "diagnostics"])
    assert report["usage"]["totals"]["calls"] == 3
    cache = report["diagnostics"]["cache"]
    assert cache["suspected_breaks"]["evaluated_turns"] == 1
    assert cache["suspected_breaks"]["suspected_turns"] == 0
    assert cache["lowest_hit_rate_sessions"][0]["cache_turns"] == 2
    narrow = _usage(service, since=BASE, until=BASE + timedelta(seconds=1))
    assert narrow["totals"]["calls"] == 2
    assert [row["kind"] for row in narrow["recent_calls"]] == ["chat", "title"]


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
    usage = _usage(service)
    assert {row["key"]: row["calls"] for row in usage["breakdowns"]["kind"]} == {
        "chat": 2,
        "image_generation": 1,
    }
    assert usage["totals"]["failed_calls"] == 1
    assert {row["key"]: row["runs"] for row in usage["breakdowns"]["model"]} == {
        "chat/m": 1,
        "retry/m": 1,
        "image/m": 1,
    }
    [run] = usage["top_runs"]
    assert (run["calls"], run["input_tokens"], run["duration_ms"]) == (3, 25, 2000)
    assert sorted(run["models"]) == ["chat/m", "image/m", "retry/m"]


@pytest.mark.asyncio
async def test_run_activity_lists_running_and_extension_runs(accounting):
    service, manager, _recorder = accounting
    binding = manager.create_bound_temporary_session(
        SessionAddress(None, "temporary", "participant"),
        owner_name="swarm",
        group_id="group",
        participant_id="peer",
        config={},
    )
    owner = RunExecutionOwner("swarm", "group", "peer", binding.generation_id, "epoch")
    await manager.start_run(
        Run(
            run_id="open",
            agent_id=binding.address.agent_id,
            session_id=binding.address.session_id,
            execution_owner=owner,
        )
    )
    session = manager.get(binding.address).for_run("open")
    session.append(_assistant(model="chat/m", at=BASE, usage={"input_tokens": 5}))

    # A running Run has no end, so it overlaps every interval after its start.
    activity = service.run_activity(since=BASE, until=datetime.now(UTC) + timedelta(hours=1))

    [run] = activity.runs
    assert (run.agent_id, run.status, run.completed_at) == ("extension:swarm", "running", None)
    assert run.measured_input_tokens == 5


@pytest.mark.asyncio
async def test_group_usage_includes_tasks_only_in_the_owned_run_with_bounded_work(
    accounting, index: StatisticsIndex
):
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

    assert report["activity"]["totals"]["calls"] == 2
    assert report["activity"]["totals"]["input_tokens"] == 15
    assert report["participants"][0]["activity"]["totals"]["calls"] == 2

    # Count actual SQLite work, rather than timing or planner-specific text.
    # The index is disposable: seed unrelated projected requests directly so
    # this regression does not need thousands of canonical fsyncs.
    database = index._database.get()
    connection = database.writer

    async def measured_report():
        steps = 0

        def progress():
            nonlocal steps
            steps += 100
            return 0

        connection.set_progress_handler(progress, 100)
        try:
            result = await service.group_usage(owner_name="swarm", group_id="group")
        finally:
            connection.set_progress_handler(None, 0)
        return result, steps

    before, baseline_steps = await measured_report()

    def seed_unrelated(connection):
        source = connection.execute(
            f"SELECT {CALL_COLUMNS} FROM stat_usage_calls ORDER BY session_key, seq LIMIT 1"
        ).fetchone()
        assert source is not None
        source = tuple(source)
        last_seq = connection.execute("SELECT MAX(seq) FROM stat_usage_records").fetchone()[0]
        unrelated_key = connection.execute(
            "INSERT INTO stat_usage_units "
            "(project_id, agent_id, session_id, owner_name, group_id, session_title) "
            "VALUES ('', 'unrelated', 'other-session', 'swarm', 'other-group', NULL) "
            "RETURNING session_key"
        ).fetchone()[0]
        sequences = range(last_seq + 1, last_seq + 20_001)
        connection.executemany(
            "INSERT INTO stat_usage_records "
            "(seq, session_key, call_id, timestamp, instant, run_id, status) "
            "SELECT ?, ?, ?, timestamp, instant, 'owned', status "
            "FROM stat_usage_records WHERE seq = ?",
            [(seq, unrelated_key, f"unrelated-{seq}", source[1]) for seq in sequences],
        )
        connection.executemany(
            f"INSERT INTO stat_usage_calls ({CALL_COLUMNS}) "
            f"VALUES ({', '.join('?' for _ in source)})",
            [(unrelated_key, seq, *source[2:]) for seq in sequences],
        )

    database.write(seed_unrelated)
    after, expanded_steps = await measured_report()

    assert before == report == after
    assert baseline_steps > 0
    # Fixed selected Runs must not walk 20,000 unrelated requests, even when
    # those requests reuse the Run id under a different Session and group.
    assert expanded_steps <= baseline_steps * 2


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
    assert group["activity"]["totals"]["calls"] == 1
    assert group["activity"]["totals"]["input_tokens"] == 5
    usage = _usage(service)
    assert (usage["totals"]["calls"], usage["totals"]["input_tokens"]) == (3, 115)
    chat = next(row for row in usage["recent_calls"] if row["model"] == "chat/m")
    assert chat["agent_id"] == "before"


@pytest.mark.asyncio
async def test_partial_counters_remain_incomplete_without_invented_cache_or_output(accounting):
    service, _manager, recorder = accounting
    partial = await recorder.start(model="task/m", kind="speech_to_text")
    await recorder.finish(partial, {"input_tokens": 12, "input_tokens_estimated": True})
    details = await recorder.start(model="task/m", kind="live_voice")
    await recorder.finish(details, {"cache_read_tokens": 7, "reasoning_tokens": 4})

    totals = _usage(service)["totals"]

    assert (totals["calls"], totals["unreported_calls"]) == (2, 2)
    assert (totals["input_tokens"], totals["estimated_input_tokens"]) == (12, 12)
    assert totals["output_tokens"] == 0
    assert (totals["cache_calls"], totals["cache_read_tokens"]) == (0, 0)
    assert totals["reasoning_tokens"] == 0


@pytest.mark.asyncio
async def test_restored_ledger_revision_rebuilds_its_projection(accounting, monkeypatch):
    service, _manager, recorder = accounting
    first = await recorder.start(model="task/m", kind="decision")
    await recorder.finish(first, {"input_tokens": 10, "output_tokens": 1})
    saved_revision, saved_records = read_ledger(recorder)
    second = await recorder.start(model="task/m", kind="decision")
    await recorder.finish(second, {"input_tokens": 20, "output_tokens": 2})
    assert _usage(service)["totals"]["calls"] == 2

    def restored(revision=0):
        # The same ledger id with a shorter history, as after an outside file copy.
        return iter(
            [
                UsagePage(
                    saved_revision,
                    tuple(record for record in saved_records if record.revision > revision),
                )
            ]
        )

    monkeypatch.setattr(recorder, "read_since", restored)
    totals = _usage(service)["totals"]
    assert (totals["calls"], totals["input_tokens"]) == (1, 10)


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

    assert _session_records(report) == 0
    assert report["usage"]["totals"]["calls"] == 1
    activity = report["extensions"]["extensions"][0]["activity"]
    assert (activity["totals"]["calls"], activity["totals"]["input_tokens"]) == (1, 10)
    assert activity["last_activity"] == format_canonical_timestamp(BASE)


def _models(service: StatisticsService) -> set[str]:
    return {row["key"] for row in _usage(service)["breakdowns"]["model"]}


@pytest.mark.asyncio
async def test_a_restart_continues_the_projection_and_a_restore_rebuilds_it(
    accounting, index: StatisticsIndex, statistics: StatisticsFactory, monkeypatch
):
    service, manager, recorder = accounting
    root = recorder.database.path.parent
    for model in ("task/a", "task/b"):
        call = await recorder.start(model=model, kind="decision")
        await recorder.finish(call, {"input_tokens": 10, "output_tokens": 1})
    snapshot = create_data_snapshot(
        root, reason="test", databases=(recorder.database, manager.database)
    )
    assert snapshot is not None
    later = await recorder.start(model="task/c", kind="decision")
    await recorder.finish(later, {"input_tokens": 10, "output_tokens": 1})
    assert _models(service) == {"task/a", "task/b", "task/c"}
    projected = read_ledger(recorder)[0]
    path = recorder.database.path
    recorder.close()

    # A restarted service over the same index reads only after its revision and
    # leaves the unchanged projection untouched.
    reopened = UsageRecorder(path)
    try:
        reads: list[int] = []
        read_since = reopened.read_since

        def recorded(revision: int = 0) -> Iterator[UsagePage]:
            reads.append(revision)
            return read_since(revision)

        monkeypatch.setattr(reopened, "read_since", recorded)
        restarted = statistics(usage_recorder=reopened, index=index)
        with closing(sqlite3.connect(index.index_path)) as observer:
            before = observer.execute("PRAGMA data_version").fetchone()[0]
            assert _models(restarted) == {"task/a", "task/b", "task/c"}
            assert observer.execute("PRAGMA data_version").fetchone()[0] == before
        assert reads == [projected]
    finally:
        reopened.close()

    # The restored ledger keeps its identity, and new work catches up with the
    # projected revision before the next report: only the restore tells them apart.
    restore_data_snapshot(root, snapshot, names=("model_usage",))
    restored = UsageRecorder(path)
    try:
        call = await restored.start(model="task/d", kind="decision")
        await restored.finish(call, {"input_tokens": 10, "output_tokens": 1})
        assert read_ledger(restored)[0] == projected
        rebuilt = statistics(usage_recorder=restored, index=index)
        assert _models(rebuilt) == {"task/a", "task/b", "task/d"}
    finally:
        restored.close()
