"""Tests for the disposable incremental Statistics index."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat.messages import ChatMessage, ToolCall
from core.database import APPLICATION_IDS, DatabaseUnavailableError
from core.models.pricing import TokenPricing
from core.sessions import ChatSession, ChatSessionManager, SessionAddress
from core.statistics import AgentDirectory, StatisticsService
from core.statistics.index import (
    SESSION_FACT_TABLES,
    StatisticsIndex,
    StatisticsUnavailableError,
)
from core.statistics.report import RunActivityReport, StatisticsReport
from core.statistics.statistics import MAX_RUN_ACTIVITY
from core.tools import tool_success
from tests.core.sessions.history_fixtures import complete_run, seed_history

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def test_index_is_a_kernel_disposable_projection(tmp_path: Path) -> None:
    service, _manager, _session = _service(tmp_path)
    service.report()

    identity = _index_identity(tmp_path)
    assert identity["application_id"] == APPLICATION_IDS["statistics"]
    assert identity["database_name"] == "statistics"
    assert identity["projection_version"] == "1"


def test_discard_removes_rollback_journal(tmp_path: Path) -> None:
    index = StatisticsIndex(tmp_path)
    rollback_journal = Path(f"{index.index_path}-journal")
    rollback_journal.parent.mkdir(parents=True, exist_ok=True)
    rollback_journal.write_bytes(b"stale")

    index.discard()

    assert rollback_journal.exists() is False


@dataclass(frozen=True)
class _FakeAgent:
    id: str


class _FakeAgents:
    def __init__(self, agent_ids: list[str]) -> None:
        self._agents = [_FakeAgent(agent_id) for agent_id in agent_ids]

    def list(self) -> list[_FakeAgent]:
        return list(self._agents)


def _timing(start: datetime, duration_ms: int) -> dict:
    return {
        "started_at": start.isoformat(),
        "completed_at": (start + timedelta(milliseconds=duration_ms)).isoformat(),
        "duration_ms": duration_ms,
    }


def _service(tmp_path: Path) -> tuple[StatisticsService, ChatSessionManager, ChatSession]:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("main", session_id="session-one").start_run("run-one")
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5",
            content="hello",
            usage={"input_tokens": 10, "output_tokens": 2},
            timestamp=BASE,
        )
    )
    complete_run(
        session,
        ChatMessage.run_summary(
            run_id="run-one",
            status="completed",
            iteration_count=1,
            timing=_timing(BASE, 1000),
            timestamp=BASE + timedelta(seconds=1),
        ),
    )
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
    )
    return service, manager, session


def test_unchanged_report_uses_index_without_loading_canonical_messages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, _manager, _session = _service(tmp_path)
    first = service.report()

    def fail_load_since(self, cursor=None):
        raise AssertionError("unchanged Session should not be loaded")

    monkeypatch.setattr(ChatSession, "load_since", fail_load_since)

    second = service.report()

    assert second.overview.total_runs == first.overview.total_runs == 1
    assert second.usage.totals.measured_input_tokens == 10


def test_persisted_index_is_reused_after_service_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, manager, _session = _service(tmp_path)
    service.report()
    restarted = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
    )

    def fail_load_since(self, cursor=None):
        raise AssertionError("persisted index should survive a service restart")

    monkeypatch.setattr(ChatSession, "load_since", fail_load_since)

    report = restarted.report()

    assert report.overview.total_runs == 1
    assert report.usage.totals.measured_input_tokens == 10


def test_index_shared_by_two_services_serves_the_other_services_updates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first_service, manager, session = _service(tmp_path)
    second_service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
    )
    first_service.report()
    second_service.report()
    session = session.start_run("run-two")
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5",
            content="again",
            usage={"input_tokens": 5, "output_tokens": 1},
            timestamp=BASE + timedelta(minutes=1),
        )
    )
    complete_run(
        session,
        ChatMessage.run_summary(
            run_id="run-two",
            status="completed",
            iteration_count=1,
            timing=_timing(BASE + timedelta(minutes=1), 500),
            timestamp=BASE + timedelta(minutes=1, seconds=1),
        ),
    )
    second_service.report()

    def fail_load_since(self, cursor=None):
        raise AssertionError("fresh shared index should avoid a canonical reread")

    monkeypatch.setattr(ChatSession, "load_since", fail_load_since)

    report = first_service.report()

    assert report.overview.total_runs == 2
    assert report.usage.totals.measured_input_tokens == 15


def test_appended_messages_incrementally_extend_the_affected_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, _manager, session = _service(tmp_path)
    service.report()
    original = ChatSession.load_since
    cursors = []

    def track_load_since(self, cursor=None):
        cursors.append(cursor)
        return original(self, cursor)

    monkeypatch.setattr(ChatSession, "load_since", track_load_since)
    session = session.start_run("run-two")
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5",
            content="again",
            usage={"input_tokens": 5, "output_tokens": 1},
            timestamp=BASE + timedelta(minutes=1),
        )
    )
    complete_run(
        session,
        ChatMessage.run_summary(
            run_id="run-two",
            status="completed",
            iteration_count=1,
            timing=_timing(BASE + timedelta(minutes=1), 500),
            timestamp=BASE + timedelta(minutes=1, seconds=1),
        ),
    )

    report = service.report()

    assert len(cursors) == 1
    assert cursors[0] is not None
    assert report.overview.total_runs == 2
    assert report.usage.totals.measured_input_tokens == 15


def test_metadata_change_updates_index_without_loading_transcript(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, manager, session = _service(tmp_path)
    service.report()
    manager.set_title(
        SessionAddress(project_id=None, agent_id="main", session_id=session.id), "Renamed"
    )

    def fail_load_since(self, cursor=None):
        raise AssertionError("metadata-only update should not load the transcript")

    monkeypatch.setattr(ChatSession, "load_since", fail_load_since)

    activity = service.run_activity(
        since=BASE - timedelta(seconds=1),
        until=BASE + timedelta(minutes=1),
    )

    assert activity.runs[0].session_title == "Renamed"


def test_replaced_canonical_session_rebuilds_only_that_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, manager, session = _service(tmp_path)
    untouched = manager.create("main", session_id="session-two")
    seed_history(
        untouched,
        [
            ChatMessage.assistant(
                model="openai/gpt-5",
                content="unchanged",
                usage={"input_tokens": 7, "output_tokens": 1},
                timestamp=BASE + timedelta(minutes=5),
            ),
            ChatMessage.run_summary(
                run_id="untouched-run",
                status="completed",
                iteration_count=1,
                timing=_timing(BASE + timedelta(minutes=5), 300),
                timestamp=BASE + timedelta(minutes=5, seconds=1),
            ),
        ],
    )
    service.report()
    replacement_messages = [
        ChatMessage.assistant(
            model="openai/gpt-5",
            content="replacement",
            usage={"input_tokens": 99, "output_tokens": 4},
            timestamp=BASE + timedelta(hours=1),
        ),
        ChatMessage.run_summary(
            run_id="replacement-run",
            status="failed",
            iteration_count=1,
            timing=_timing(BASE + timedelta(hours=1), 200),
            timestamp=BASE + timedelta(hours=1, seconds=1),
        ),
    ]
    address = SessionAddress(project_id=None, agent_id="main", session_id=session.id)
    manager.delete(address)
    replacement = manager.create("main", session_id=session.id)
    seed_history(replacement, replacement_messages)
    original = ChatSession.load_since
    loads = []

    def track_load_since(self, cursor=None):
        loads.append((self.id, cursor))
        return original(self, cursor)

    monkeypatch.setattr(ChatSession, "load_since", track_load_since)

    report = service.report()

    # The replaced Session is rebuilt from its start; the other one is not read.
    assert loads == [(session.id, None)]
    assert report.overview.total_runs == 2
    assert report.overview.run_status.failed == 1
    assert report.overview.run_status.completed == 1
    assert report.usage.totals.measured_input_tokens == 99 + 7


def test_history_edit_rebuilds_the_projection_and_keeps_superseded_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("main", session_id="session-one")
    question = ChatMessage.user("first question", timestamp=BASE)
    session = session.start_run("run-one")
    session.append(question)
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5",
            content="first answer",
            usage={"input_tokens": 10, "output_tokens": 2},
            timestamp=BASE + timedelta(seconds=1),
        )
    )
    complete_run(
        session,
        ChatMessage.run_summary(
            run_id="run-one",
            status="completed",
            iteration_count=1,
            timing=_timing(BASE, 2000),
            timestamp=BASE + timedelta(seconds=2),
        ),
    )
    service = StatisticsService(manager, cast(AgentDirectory, _FakeAgents(["main"])))
    service.report()

    manager.get(session.address).apply_edit(
        question.id, [ChatMessage.user("edited question", timestamp=BASE + timedelta(minutes=1))]
    )
    session = manager.get(session.address).start_run("run-two")
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5",
            content="second answer",
            usage={"input_tokens": 7, "output_tokens": 1},
            timestamp=BASE + timedelta(minutes=1, seconds=1),
        )
    )
    complete_run(
        session,
        ChatMessage.run_summary(
            run_id="run-two",
            status="completed",
            iteration_count=1,
            timing=_timing(BASE + timedelta(minutes=1), 1000),
            timestamp=BASE + timedelta(minutes=1, seconds=2),
        ),
    )
    original = ChatSession.load_since
    cursors = []

    def track_load_since(self, cursor=None):
        cursors.append(cursor)
        return original(self, cursor)

    monkeypatch.setattr(ChatSession, "load_since", track_load_since)

    report = service.report()

    # The edit ends the stored cursor, so the Session is rebuilt from its start.
    assert cursors[-1] is None
    # The edited-away Run and its Usage were really spent and stay counted.
    assert report.overview.total_runs == 2
    assert report.usage.totals.measured_input_tokens == 17


def test_deleted_session_is_pruned_from_index(tmp_path: Path) -> None:
    service, manager, session = _service(tmp_path)
    service.report()

    manager.delete(SessionAddress(project_id=None, agent_id="main", session_id=session.id))
    report = service.report()

    assert report.overview.total_sessions == 0
    with sqlite3.connect(_index_path(tmp_path)) as connection:
        for table in ("stat_sessions", *SESSION_FACT_TABLES):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_session_deleted_between_listing_and_index_read_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, manager, session = _service(tmp_path)
    address = SessionAddress(project_id=None, agent_id="main", session_id=session.id)
    list_history_versions = manager.list_history_versions

    def list_then_delete(addresses):
        versions = list_history_versions(addresses)
        manager.delete(address)
        return versions

    monkeypatch.setattr(manager, "list_history_versions", list_then_delete)

    report = service.report()

    assert report.overview.total_sessions == 0
    assert report.overview.total_runs == 0


def test_index_projection_does_not_store_large_or_sensitive_message_content(tmp_path: Path) -> None:
    secret_text = "DO-NOT-PERSIST-RAW-CONTENT"
    manager = ChatSessionManager(tmp_path)
    session = manager.create("main", session_id="session-one")
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5",
            content=secret_text,
            tool_calls=[ToolCall(id="call-one", name="read", arguments={"path": secret_text})],
            reasoning=f"reasoning-{secret_text}",
            timestamp=BASE,
        )
    )
    session.append(
        ChatMessage.tool(
            tool_call_id="call-one",
            name="read",
            content=json.dumps(tool_success({"text": secret_text})),
            timestamp=BASE + timedelta(seconds=1),
        )
    )
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
    )

    report = service.report()

    assert report.overview.chat_messages_by_role["assistant"] == 1
    assert report.tools.tools[0].successes == 1
    stored = _index_dump(tmp_path)
    assert secret_text not in stored
    assert "reasoning" not in stored


def test_corrupt_index_is_discarded_and_rebuilt_once(tmp_path: Path) -> None:
    service, _manager, _session = _service(tmp_path)
    service.report()
    _index_path(tmp_path).write_bytes(b"not a sqlite database")

    report = service.report()

    assert report.overview.total_runs == 1
    assert _index_identity(tmp_path)["database_name"] == "statistics"
    with sqlite3.connect(_index_path(tmp_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stat_sessions").fetchone()[0] == 1


@pytest.mark.parametrize("table", ["stat_tools", "stat_skills"])
@pytest.mark.parametrize("recovery", ["rebuild", "transient"])
def test_report_recovery_discards_partial_aggregation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: str, recovery: str
) -> None:
    service, _manager, _session = _service(tmp_path)
    expected = asdict(service.report())
    expected.pop("generated_at")
    database = service._index._database.get()
    original_id = database.database_id
    # Keep the live handle open so the missing fact table fails during report
    # aggregation, after earlier sections have already accumulated totals.
    database.write(lambda connection: connection.execute(f"DROP TABLE {table}"))
    if recovery == "transient":

        def unavailable_discard() -> None:
            raise DatabaseUnavailableError("cannot discard the damaged index")

        monkeypatch.setattr(service._index._database, "discard", unavailable_discard)

    recovered = asdict(service.report())
    recovered.pop("generated_at")

    assert recovered == expected
    assert (_index_identity(tmp_path)["database_id"] != original_id) == (recovery == "rebuild")
    following = asdict(service.report())
    following.pop("generated_at")
    assert following == expected


def test_projection_version_mismatch_discards_and_rebuilds_the_index(tmp_path: Path) -> None:
    service, manager, _session = _service(tmp_path)
    service.report()
    service._index.close()
    with closing(sqlite3.connect(_index_path(tmp_path))) as connection, connection:
        connection.execute("UPDATE kernel_meta SET value = '0' WHERE key = 'projection_version'")
        connection.execute("UPDATE stat_calls SET input_tokens = 42")

    restarted = StatisticsService(manager, cast(AgentDirectory, _FakeAgents(["main"])))
    report = restarted.report()

    assert report.usage.totals.measured_input_tokens == 10
    assert _index_identity(tmp_path)["projection_version"] == "1"


def test_an_index_with_nullable_instants_is_rebuilt_at_the_same_version(tmp_path: Path) -> None:
    service, manager, _session = _service(tmp_path)
    service.report()
    service._index.close()
    # The shape before stored timestamps became strictly canonical: nullable
    # instants and an untimed-record counter.
    with closing(sqlite3.connect(_index_path(tmp_path))) as connection, connection:
        connection.execute("DROP TABLE stat_errors")
        connection.execute(
            "CREATE TABLE stat_errors (session_key INTEGER NOT NULL, seq INTEGER NOT NULL, "
            "instant INTEGER, day INTEGER, kind TEXT NOT NULL, PRIMARY KEY (session_key, seq)) "
            "WITHOUT ROWID"
        )
        connection.execute(
            "ALTER TABLE stat_sessions ADD COLUMN untimed_records INTEGER NOT NULL DEFAULT 0"
        )

    restarted = StatisticsService(manager, cast(AgentDirectory, _FakeAgents(["main"])))
    report = restarted.report()

    assert report.usage.totals.measured_input_tokens == 10
    assert _index_identity(tmp_path)["projection_version"] == "1"
    with closing(sqlite3.connect(_index_path(tmp_path))) as connection:
        columns = {row[1]: row[3] for row in connection.execute("PRAGMA table_info(stat_errors)")}
        session_columns = {row[1] for row in connection.execute("PRAGMA table_info(stat_sessions)")}
    assert columns["instant"] == 1
    assert "untimed_records" not in session_columns


def test_fork_history_counts_once_and_leaves_with_its_deleted_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, manager, source = _service(tmp_path)
    source_address = SessionAddress(None, "main", source.id)
    fork = asyncio.run(manager.fork(source_address))
    seed_history(
        fork,
        [
            ChatMessage.assistant(
                model="openai/gpt-5",
                content="fork work",
                usage={"input_tokens": 7, "output_tokens": 1},
                timestamp=BASE + timedelta(seconds=5),
            )
        ],
    )
    forked = service.report()
    assert forked.overview.total_sessions == 2
    assert forked.overview.total_runs == 1
    assert forked.usage.totals.measured_input_tokens == 17

    original = ChatSession.load_since
    cursors = []

    def track_load_since(self, cursor=None):
        cursors.append(cursor)
        return original(self, cursor)

    monkeypatch.setattr(ChatSession, "load_since", track_load_since)
    # Deleting the origin copies the history the fork shows into the fork and
    # bumps its history revision, so the index reads the fork again; its own
    # audit is unchanged, so the read continues from the stored cursor.
    manager.delete(source_address)
    after_delete = service.report()

    fork_address = SessionAddress(None, "main", fork.id)
    revision = manager.list_history_versions([fork_address])[fork_address][1]
    assert len(cursors) == 1
    assert cursors[0] is not None
    assert cursors[0].history_revision < revision
    with closing(sqlite3.connect(_index_path(tmp_path))) as connection:
        stored = connection.execute(
            "SELECT history_revision FROM stat_sessions WHERE session_id = ?", (fork.id,)
        ).fetchone()
    assert stored[0] == revision
    assert after_delete.overview.total_sessions == 1
    # The copied prefix is not the fork's own spend; the origin's usage left
    # with the deleted origin.
    assert after_delete.overview.total_runs == 0
    assert after_delete.usage.totals.measured_input_tokens == 7


@pytest.mark.parametrize("storage", ["file", "transient"])
@pytest.mark.parametrize("projection", ["report", "run_activity"])
def test_all_statistics_reads_skip_sessions_deleted_after_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: str, projection: str
) -> None:
    service, manager, session = _service(tmp_path)
    if storage == "transient":
        _make_index_file_unavailable(monkeypatch)
    list_history_versions = manager.list_history_versions
    address = SessionAddress(project_id=None, agent_id="main", session_id=session.id)

    def delete_after_listing(addresses):
        versions = list_history_versions(addresses)
        manager.delete(address)
        return versions

    monkeypatch.setattr(manager, "list_history_versions", delete_after_listing)
    if projection == "report":
        result = service.report()
        assert result.overview.total_sessions == 0
        assert result.overview.total_runs == 0
    else:
        activity = service.run_activity(since=BASE, until=BASE + timedelta(minutes=1))
        assert activity.total_runs == 0
        assert activity.runs == []


def test_index_file_and_transient_projection_agree_on_forks_windows_and_run_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, manager, source = _service(tmp_path)
    fork = asyncio.run(manager.fork(SessionAddress(None, "main", source.id)))
    # Index the fork before it writes anything: it inherits the source's Run
    # without contributing it, and its own later work extends that projection.
    just_forked = service.report()
    assert just_forked.overview.total_sessions == 2
    assert just_forked.overview.total_runs == 1
    assert just_forked.usage.totals.measured_input_tokens == 10
    seed_history(
        fork,
        [
            ChatMessage.assistant(
                model="other/model",
                content="new work",
                timestamp=BASE + timedelta(seconds=2),
                usage={
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "input_tokens_estimated": True,
                    "output_tokens_estimated": True,
                    "estimated": True,
                },
            ),
            ChatMessage.run_summary(
                run_id="fork-run",
                status="failed",
                iteration_count=1,
                timing=_timing(BASE + timedelta(seconds=2), 1000),
                timestamp=BASE + timedelta(seconds=3),
            ),
        ],
    )
    window = {"since": BASE, "until": BASE + timedelta(seconds=4)}
    indexed_report = asdict(service.report(**window))
    indexed_activity = asdict(service.run_activity(**window))
    assert indexed_report["overview"]["total_runs"] == 2
    assert indexed_activity["total_runs"] == 2
    _make_index_file_unavailable(monkeypatch)
    transient_report = asdict(service.report(**window))
    transient_activity = asdict(service.run_activity(**window))
    for result in [indexed_report, indexed_activity, transient_report, transient_activity]:
        result.pop("generated_at")
    assert transient_report == indexed_report
    assert transient_activity == indexed_activity


def test_every_report_reads_the_index_instead_of_a_retained_projection(tmp_path: Path) -> None:
    service, _manager, _session = _service(tmp_path)
    assert service.report().usage.totals.measured_input_tokens == 10
    with sqlite3.connect(_index_path(tmp_path)) as connection:
        connection.execute("UPDATE stat_calls SET input_tokens = 42")

    assert service.report().usage.totals.measured_input_tokens == 42
    assert set(vars(service._index)) == {"data_dir", "index_path", "_database", "_lock"}


def test_unchanged_index_read_performs_no_write(tmp_path: Path) -> None:
    service, _manager, _session = _service(tmp_path)
    service.report()
    observer = sqlite3.connect(_index_path(tmp_path))
    try:
        before = observer.execute("PRAGMA data_version").fetchone()[0]
        service.report()
        service.run_activity(since=BASE, until=BASE + timedelta(minutes=1))
        service.warm_index()
        after = observer.execute("PRAGMA data_version").fetchone()[0]
    finally:
        observer.close()

    assert after == before


def test_busy_index_raises_retryable_error_without_discarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _manager, session = _service(tmp_path)
    service.report()
    session.start_run("run-two").append(
        ChatMessage.assistant(
            model="openai/gpt-5",
            content="later",
            usage={"input_tokens": 5, "output_tokens": 1},
            timestamp=BASE + timedelta(minutes=1),
        )
    )
    blocker = sqlite3.connect(_index_path(tmp_path), isolation_level=None, timeout=0)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(StatisticsUnavailableError):
            service.report()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    original = ChatSession.load_since
    cursors = []

    def track_load_since(self, cursor=None):
        cursors.append(cursor)
        return original(self, cursor)

    monkeypatch.setattr(ChatSession, "load_since", track_load_since)

    report = service.report()

    assert report.usage.totals.measured_input_tokens == 15
    # The retained index is extended from its cursor, not rebuilt.
    assert len(cursors) == 1
    assert cursors[0] is not None


def test_canonical_read_failure_propagates_and_keeps_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, manager, _session = _service(tmp_path)
    service.report()

    def fail(_addresses):
        raise RuntimeError("canonical store unavailable")

    monkeypatch.setattr(manager, "list_history_versions", fail)
    with pytest.raises(RuntimeError, match="canonical store unavailable"):
        service.report()

    with sqlite3.connect(_index_path(tmp_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stat_sessions").fetchone()[0] == 1


def test_changed_catalog_pricing_reprices_retrospective_calls(tmp_path: Path) -> None:
    _unpriced, manager, _session = _service(tmp_path)
    prices = {"value": TokenPricing.from_cost({"input": 1, "output": 1}, source="test")}
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
        pricing_lookup=lambda _model: prices["value"],
    )

    first = service.report().costs.totals
    prices["value"] = TokenPricing.from_cost({"input": 100, "output": 100}, source="test")
    second = service.report().costs.totals

    assert first.retrospective_calls == second.retrospective_calls == 1
    assert first.estimated_usd == pytest.approx(12 / 1_000_000)
    assert second.estimated_usd == pytest.approx(1200 / 1_000_000)


def test_run_activity_returns_the_newest_bounded_runs_with_the_total(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("main", session_id="many-runs")
    runs = MAX_RUN_ACTIVITY + 5
    seed_history(
        session,
        [
            ChatMessage.run_summary(
                run_id=f"run-{index:03d}",
                status="completed",
                iteration_count=1,
                timing=_timing(BASE + timedelta(minutes=index), 1000),
                timestamp=BASE + timedelta(minutes=index, seconds=1),
            )
            for index in range(runs)
        ],
    )
    service = StatisticsService(manager, cast(AgentDirectory, _FakeAgents(["main"])))

    activity = service.run_activity(since=BASE, until=BASE + timedelta(days=1))

    assert activity.total_runs == runs
    assert activity.truncated is True
    assert [run.run_id for run in activity.runs] == [
        f"run-{index:03d}" for index in range(runs - 1, 4, -1)
    ]


def _index_path(tmp_path: Path) -> Path:
    return tmp_path / "statistics" / "session-statistics.sqlite"


def _index_dump(tmp_path: Path) -> str:
    """Return every stored value of every index table as text."""
    with sqlite3.connect(_index_path(tmp_path)) as connection:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        return "\n".join(
            repr(row) for table in tables for row in connection.execute(f"SELECT * FROM {table}")
        )


def _index_identity(tmp_path: Path) -> dict[str, object]:
    with closing(sqlite3.connect(_index_path(tmp_path))) as connection:
        identity: dict[str, object] = dict(
            connection.execute("SELECT key, value FROM kernel_meta").fetchall()
        )
        identity["application_id"] = connection.execute("PRAGMA application_id").fetchone()[0]
    return identity


def _make_index_file_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(self, *_args, **_kwargs):
        raise DatabaseUnavailableError("statistics: the index directory is not writable")

    monkeypatch.setattr(StatisticsIndex, "_read_file", unavailable)


def test_async_reads_run_on_the_index_worker_pool_and_are_unavailable_after_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _manager, _session = _service(tmp_path)
    threads: list[str] = []
    original = StatisticsIndex.read

    def read(self: StatisticsIndex, *args: Any, **kwargs: Any) -> Any:
        threads.append(threading.current_thread().name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(StatisticsIndex, "read", read)

    async def scenario() -> tuple[StatisticsReport, RunActivityReport]:
        report = await service.report_async()
        activity = await service.run_activity_async(since=BASE, until=BASE + timedelta(minutes=1))
        await service.warm_index_async()
        await service._index.aclose()
        await service._index.aclose()
        with pytest.raises(DatabaseUnavailableError):
            await service.report_async()
        with pytest.raises(DatabaseUnavailableError):
            await service.group_usage(owner_name="swarm", group_id="group")
        return report, activity

    report, activity = asyncio.run(scenario())

    assert report.usage.totals.measured_input_tokens == 10
    assert activity.total_runs == 1
    assert len(threads) == 3
    assert all(name.startswith("vbot-db-statistics_") for name in threads)
    # A closed index never falls back to a transient projection.
    with pytest.raises(DatabaseUnavailableError):
        service.report()
