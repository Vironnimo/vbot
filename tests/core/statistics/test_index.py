"""Tests for the disposable incremental Statistics index."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from core.chat.messages import ChatMessage, ToolCall
from core.database import JOURNAL_MODE_DELETE
from core.models.pricing import TokenPricing
from core.sessions import ChatSession, ChatSessionManager, SessionAddress
from core.statistics import AgentDirectory, StatisticsService
from core.statistics.index import (
    SESSION_FACT_TABLES,
    StatisticsIndex,
    StatisticsUnavailableError,
)
from core.statistics.statistics import MAX_RUN_ACTIVITY
from core.tools import tool_success
from tests.core.sessions.history_fixtures import seed_history

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def test_index_uses_required_rollback_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.statistics import index as statistics_index

    monkeypatch.setattr(
        statistics_index, "required_journal_mode", lambda _version: JOURNAL_MODE_DELETE
    )
    index = StatisticsIndex(tmp_path)
    index.index_path.parent.mkdir(parents=True)

    connection = index._connect()
    try:
        mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        connection.close()

    assert mode == JOURNAL_MODE_DELETE


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
    session.append(
        ChatMessage.run_summary(
            run_id="run-one",
            status="completed",
            iteration_count=1,
            timing=_timing(BASE, 1000),
            timestamp=BASE + timedelta(seconds=1),
        )
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
    session.append(
        ChatMessage.run_summary(
            run_id="run-two",
            status="completed",
            iteration_count=1,
            timing=_timing(BASE + timedelta(minutes=1), 500),
            timestamp=BASE + timedelta(minutes=1, seconds=1),
        )
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
    session.append(
        ChatMessage.run_summary(
            run_id="run-two",
            status="completed",
            iteration_count=1,
            timing=_timing(BASE + timedelta(minutes=1), 500),
            timestamp=BASE + timedelta(minutes=1, seconds=1),
        )
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


def test_replaced_canonical_session_rebuilds_only_that_projection(tmp_path: Path) -> None:
    service, _manager, session = _service(tmp_path)
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
    _manager.delete(address)
    replacement = _manager.create("main", session_id=session.id)
    seed_history(replacement, replacement_messages)

    report = service.report()

    assert report.overview.total_runs == 1
    assert report.overview.run_status.failed == 1
    assert report.usage.totals.measured_input_tokens == 99


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
    with sqlite3.connect(_index_path(tmp_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6


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
    seed_history(
        fork,
        [
            ChatMessage.assistant(
                model="other/model",
                content="new work",
                timestamp=BASE + timedelta(seconds=2),
                usage={"input_tokens": 7, "output_tokens": 3, "estimated": True},
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
    assert set(vars(service._index)) == {"data_dir", "index_path", "_lock"}


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


def _make_index_file_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(self, *_args, **_kwargs):
        raise OSError("index directory is not writable")

    monkeypatch.setattr(StatisticsIndex, "_read_file", unavailable)
