"""The disposable incremental Statistics index: reuse, reconciliation, recovery and lifetime."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from core.chat.messages import ChatMessage, ToolCall
from core.database import APPLICATION_IDS, DatabaseUnavailableError
from core.sessions import ChatSession, ChatSessionManager
from core.statistics.index import (
    SESSION_FACT_TABLES,
    StatisticsIndex,
    StatisticsUnavailableError,
)
from core.statistics.report import RunActivityReport, StatisticsReport
from core.tools import tool_success
from tests.core.sessions.history_fixtures import complete_run, seed_history
from tests.core.statistics.statistics_test_support import (
    BASE,
    StatisticsFactory,
    _assistant,
    _index_path,
    _record_canonical_reads,
    _run_summary,
)


def _append_run(
    session: ChatSession,
    run_id: str,
    *,
    minutes: int,
    input_tokens: int,
    status: str = "completed",
    prompt: ChatMessage | None = None,
) -> ChatSession:
    """Append one Run with a single Model call of *input_tokens* and return its writer."""
    at = BASE + timedelta(minutes=minutes)
    session = session.start_run(run_id)
    if prompt is not None:
        session.append(prompt)
    session.append(
        _assistant(
            model="openai/gpt-5", at=at, usage={"input_tokens": input_tokens, "output_tokens": 1}
        )
    )
    complete_run(
        session,
        _run_summary(status=status, at=at + timedelta(seconds=1), duration_ms=500, run_id=run_id),
    )
    return session


@pytest.fixture
def session(manager: ChatSessionManager) -> ChatSession:
    """One Session holding one completed Run with 10 measured input tokens."""
    return _append_run(
        manager.create("main", session_id="session-one"), "run-one", minutes=0, input_tokens=10
    )


def _index_identity(data_dir: Path) -> dict[str, object]:
    with closing(sqlite3.connect(_index_path(data_dir))) as connection:
        identity: dict[str, object] = dict(
            connection.execute("SELECT key, value FROM kernel_meta").fetchall()
        )
        identity["application_id"] = connection.execute("PRAGMA application_id").fetchone()[0]
    return identity


def _index_dump(data_dir: Path) -> str:
    """Return every stored value of every index table as text."""
    with closing(sqlite3.connect(_index_path(data_dir))) as connection:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        return "\n".join(
            repr(row) for table in tables for row in connection.execute(f"SELECT * FROM {table}")
        )


def _count(data_dir: Path, table: str) -> int:
    with closing(sqlite3.connect(_index_path(data_dir))) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _make_index_file_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(self, *_args, **_kwargs):
        raise DatabaseUnavailableError("statistics: the index directory is not writable")

    monkeypatch.setattr(StatisticsIndex, "_read_file", unavailable)


def _without_generated_at(result: Any) -> dict[str, Any]:
    values = asdict(result)
    values.pop("generated_at")
    return values


# -- Identity and reuse -------------------------------------------------------


def test_a_corrupt_kernel_projection_is_discarded_and_rebuilt_once(
    tmp_path: Path, session: ChatSession, statistics: StatisticsFactory, index: StatisticsIndex
) -> None:
    statistics(index=index).report()
    expected_identity = {
        "application_id": APPLICATION_IDS["statistics"],
        "database_name": "statistics",
        "projection_version": "2",
    }
    assert _index_identity(tmp_path).items() >= expected_identity.items()
    # Corrupt at rest: under WAL an open connection keeps reading its own pages.
    index.close()
    _index_path(tmp_path).write_bytes(b"not a sqlite database")

    report = statistics().report()

    assert report.overview.total_runs == 1
    assert _index_identity(tmp_path).items() >= expected_identity.items()
    assert _count(tmp_path, "stat_sessions") == 1


def test_discard_removes_rollback_journal(index: StatisticsIndex) -> None:
    rollback_journal = Path(f"{index.index_path}-journal")
    rollback_journal.parent.mkdir(parents=True, exist_ok=True)
    rollback_journal.write_bytes(b"stale")

    index.discard()

    assert rollback_journal.exists() is False


def test_persisted_index_serves_every_service_and_reads_only_appended_messages(
    session: ChatSession, statistics: StatisticsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = statistics()
    first.report()
    reads = _record_canonical_reads(monkeypatch)

    # A restarted service and a repeated report reuse the persisted projection.
    second = statistics()
    assert second.report().overview.total_runs == 1
    assert first.report().usage.totals.measured_input_tokens == 10
    assert reads == []

    _append_run(session, "run-two", minutes=1, input_tokens=5)
    assert second.report().overview.total_runs == 2
    # Only the appended part is read, continuing from the stored cursor.
    assert [(session_id, cursor is not None) for session_id, cursor in reads] == [
        ("session-one", True)
    ]

    reads.clear()
    report = first.report()
    # The other service's update is already in the shared index.
    assert reads == []
    assert report.overview.total_runs == 2
    assert report.usage.totals.measured_input_tokens == 15


def test_metadata_change_updates_index_without_loading_transcript(
    manager: ChatSessionManager,
    session: ChatSession,
    statistics: StatisticsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = statistics()
    service.report()
    manager.set_title(session.address, "Renamed")
    reads = _record_canonical_reads(monkeypatch)

    activity = service.run_activity(
        since=BASE - timedelta(seconds=1), until=BASE + timedelta(minutes=1)
    )

    assert activity.runs[0].session_title == "Renamed"
    assert reads == []


def test_every_report_reads_the_index_instead_of_a_retained_projection(
    tmp_path: Path, session: ChatSession, statistics: StatisticsFactory
) -> None:
    service = statistics()
    assert service.report().usage.totals.measured_input_tokens == 10
    with closing(sqlite3.connect(_index_path(tmp_path))) as connection, connection:
        connection.execute("UPDATE stat_calls SET input_tokens = 42")

    assert service.report().usage.totals.measured_input_tokens == 42


def test_unchanged_index_read_performs_no_write(
    tmp_path: Path, session: ChatSession, statistics: StatisticsFactory
) -> None:
    service = statistics()
    service.report()
    with closing(sqlite3.connect(_index_path(tmp_path))) as observer:
        before = observer.execute("PRAGMA data_version").fetchone()[0]
        service.report()
        service.run_activity(since=BASE, until=BASE + timedelta(minutes=1))
        service.warm_index()
        after = observer.execute("PRAGMA data_version").fetchone()[0]

    assert after == before


def test_index_projection_does_not_store_large_or_sensitive_message_content(
    tmp_path: Path, manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    secret_text = "DO-NOT-PERSIST-RAW-CONTENT"
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

    report = statistics().report()

    assert report.overview.chat_messages_by_role["assistant"] == 1
    assert report.tools.tools[0].successes == 1
    stored = _index_dump(tmp_path)
    assert secret_text not in stored
    assert "reasoning" not in stored


# -- Reconciliation -----------------------------------------------------------


def test_replaced_canonical_session_rebuilds_only_that_projection(
    manager: ChatSessionManager,
    session: ChatSession,
    statistics: StatisticsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _append_run(
        manager.create("main", session_id="session-two"), "untouched", minutes=5, input_tokens=7
    )
    service = statistics()
    service.report()
    manager.delete(session.address)
    _append_run(
        manager.create("main", session_id=session.id),
        "replacement",
        minutes=60,
        input_tokens=99,
        status="failed",
    )
    reads = _record_canonical_reads(monkeypatch)

    report = service.report()

    # The replaced Session is rebuilt from its start; the other one is not read.
    assert reads == [(session.id, None)]
    assert report.overview.total_runs == 2
    assert report.overview.run_status.failed == 1
    assert report.overview.run_status.completed == 1
    assert report.usage.totals.measured_input_tokens == 99 + 7


def test_history_edit_rebuilds_the_projection_and_keeps_superseded_spend(
    manager: ChatSessionManager, statistics: StatisticsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    question = ChatMessage.user("first question", timestamp=BASE)
    session = _append_run(
        manager.create("main", session_id="session-one"),
        "run-one",
        minutes=0,
        input_tokens=10,
        prompt=question,
    )
    service = statistics()
    service.report()

    manager.get(session.address).apply_edit(
        question.id, [ChatMessage.user("edited question", timestamp=BASE + timedelta(minutes=1))]
    )
    _append_run(manager.get(session.address), "run-two", minutes=1, input_tokens=7)
    reads = _record_canonical_reads(monkeypatch)

    report = service.report()

    # The edit ends the stored cursor, so the Session is rebuilt from its start.
    assert reads[-1] == (session.id, None)
    # The edited-away Run and its Usage were really spent and stay counted.
    assert report.overview.total_runs == 2
    assert report.usage.totals.measured_input_tokens == 17


def test_deleted_session_is_pruned_from_index(
    tmp_path: Path,
    manager: ChatSessionManager,
    session: ChatSession,
    statistics: StatisticsFactory,
) -> None:
    service = statistics()
    service.report()

    manager.delete(session.address)

    assert service.report().overview.total_sessions == 0
    for table in ("stat_sessions", *SESSION_FACT_TABLES):
        assert _count(tmp_path, table) == 0


@pytest.mark.parametrize(
    ("storage", "projection"), [("file", "report"), ("transient", "run_activity")]
)
def test_all_statistics_reads_skip_sessions_deleted_after_listing(
    manager: ChatSessionManager,
    session: ChatSession,
    statistics: StatisticsFactory,
    monkeypatch: pytest.MonkeyPatch,
    storage: str,
    projection: str,
) -> None:
    service = statistics()
    if storage == "transient":
        _make_index_file_unavailable(monkeypatch)
    list_history_versions = manager.list_history_versions

    def delete_after_listing(addresses):
        versions = list_history_versions(addresses)
        manager.delete(session.address)
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


def test_fork_history_counts_once_and_leaves_with_its_deleted_origin(
    tmp_path: Path,
    manager: ChatSessionManager,
    session: ChatSession,
    statistics: StatisticsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = statistics()
    fork = asyncio.run(manager.fork(session.address))
    seed_history(
        fork,
        [
            _assistant(
                model="openai/gpt-5",
                at=BASE + timedelta(seconds=5),
                usage={"input_tokens": 7, "output_tokens": 1},
            )
        ],
    )
    forked = service.report()
    assert forked.overview.total_sessions == 2
    assert forked.overview.total_runs == 1
    assert forked.usage.totals.measured_input_tokens == 17
    reads = _record_canonical_reads(monkeypatch)

    # Deleting the origin copies the history the fork shows into the fork and
    # bumps its history revision, so the index reads the fork again; its own
    # audit is unchanged, so the read continues from the stored cursor.
    manager.delete(session.address)
    after_delete = service.report()

    revision = manager.list_history_versions([fork.address])[fork.address][1]
    [(read_id, cursor)] = reads
    assert read_id == fork.id
    assert cursor is not None
    assert cursor.history_revision < revision
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


def test_index_file_and_transient_projection_agree_on_forks_windows_and_run_activity(
    manager: ChatSessionManager,
    session: ChatSession,
    statistics: StatisticsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = statistics()
    fork = asyncio.run(manager.fork(session.address))
    # Index the fork before it writes anything: it inherits the source's Run
    # without contributing it, and its own later work extends that projection.
    just_forked = service.report()
    assert just_forked.overview.total_sessions == 2
    assert just_forked.overview.total_runs == 1
    assert just_forked.usage.totals.measured_input_tokens == 10
    seed_history(
        fork,
        [
            _assistant(
                model="other/model",
                at=BASE + timedelta(seconds=2),
                usage={
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "input_tokens_estimated": True,
                    "output_tokens_estimated": True,
                    "estimated": True,
                },
            ),
            _run_summary(
                status="failed",
                at=BASE + timedelta(seconds=3),
                duration_ms=1000,
                run_id="fork-run",
            ),
        ],
    )
    window = {"since": BASE, "until": BASE + timedelta(seconds=4)}
    indexed_report = _without_generated_at(service.report(**window))
    indexed_activity = _without_generated_at(service.run_activity(**window))
    assert indexed_report["overview"]["total_runs"] == 2
    assert indexed_activity["total_runs"] == 2

    _make_index_file_unavailable(monkeypatch)

    assert _without_generated_at(service.report(**window)) == indexed_report
    assert _without_generated_at(service.run_activity(**window)) == indexed_activity


# -- Failure policy -----------------------------------------------------------


@pytest.mark.parametrize("recovery", ["rebuild", "transient"])
def test_report_recovery_discards_partial_aggregation(
    tmp_path: Path,
    index: StatisticsIndex,
    session: ChatSession,
    statistics: StatisticsFactory,
    monkeypatch: pytest.MonkeyPatch,
    recovery: str,
) -> None:
    service = statistics(index=index)
    expected = _without_generated_at(service.report())
    database = index._database.get()
    original_id = database.database_id
    # Keep the live handle open so the missing fact table fails during report
    # aggregation, after earlier sections have already accumulated totals.
    database.write(lambda connection: connection.execute("DROP TABLE stat_skills"))
    if recovery == "transient":

        def unavailable_discard() -> None:
            raise DatabaseUnavailableError("cannot discard the damaged index")

        monkeypatch.setattr(index._database, "discard", unavailable_discard)

    assert _without_generated_at(service.report()) == expected
    assert (_index_identity(tmp_path)["database_id"] != original_id) == (recovery == "rebuild")
    assert _without_generated_at(service.report()) == expected


@pytest.mark.parametrize(
    "stale_shape",
    [
        ["UPDATE kernel_meta SET value = '0' WHERE key = 'projection_version'"],
        # The shape before stored timestamps became strictly canonical, at the
        # same version: nullable instants and an untimed-record counter.
        [
            "DROP TABLE stat_errors",
            "CREATE TABLE stat_errors (session_key INTEGER NOT NULL, seq INTEGER NOT NULL, "
            "instant INTEGER, day INTEGER, kind TEXT NOT NULL, PRIMARY KEY (session_key, seq)) "
            "WITHOUT ROWID",
            "ALTER TABLE stat_sessions ADD COLUMN untimed_records INTEGER NOT NULL DEFAULT 0",
        ],
    ],
    ids=["older-projection-version", "nullable-instants"],
)
def test_an_index_of_another_shape_is_discarded_and_rebuilt(
    tmp_path: Path,
    index: StatisticsIndex,
    session: ChatSession,
    statistics: StatisticsFactory,
    stale_shape: list[str],
) -> None:
    statistics(index=index).report()
    index.close()
    with closing(sqlite3.connect(_index_path(tmp_path))) as connection, connection:
        for statement in stale_shape:
            connection.execute(statement)
        connection.execute("UPDATE stat_calls SET input_tokens = 42")

    report = statistics().report()

    assert report.usage.totals.measured_input_tokens == 10
    assert _index_identity(tmp_path)["projection_version"] == "2"
    with closing(sqlite3.connect(_index_path(tmp_path))) as connection:
        error_columns = {
            row[1]: row[3] for row in connection.execute("PRAGMA table_info(stat_errors)")
        }
        session_columns = {row[1] for row in connection.execute("PRAGMA table_info(stat_sessions)")}
    assert error_columns["instant"] == 1
    assert "untimed_records" not in session_columns


def test_busy_index_raises_retryable_error_without_discarding(
    tmp_path: Path,
    session: ChatSession,
    statistics: StatisticsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The busy index is detected after the SQLite busy timeout and the write
    # patience; both shrink so the test does not wait out the real ones.
    monkeypatch.setattr("core.database._runtime.BUSY_TIMEOUT_MS", 0)
    monkeypatch.setattr("core.statistics.index._WRITE_PATIENCE_S", 0.05)
    service = statistics()
    service.report()
    _append_run(session, "run-two", minutes=1, input_tokens=5)
    blocker = sqlite3.connect(_index_path(tmp_path), isolation_level=None, timeout=0)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(StatisticsUnavailableError):
            service.report()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    reads = _record_canonical_reads(monkeypatch)

    report = service.report()

    assert report.usage.totals.measured_input_tokens == 15
    # The retained index is extended from its cursor, not rebuilt.
    assert [cursor is not None for _session_id, cursor in reads] == [True]


def test_canonical_read_failure_propagates_and_keeps_the_index(
    tmp_path: Path,
    manager: ChatSessionManager,
    session: ChatSession,
    statistics: StatisticsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = statistics()
    service.report()

    def fail(_addresses):
        raise RuntimeError("canonical store unavailable")

    monkeypatch.setattr(manager, "list_history_versions", fail)
    with pytest.raises(RuntimeError, match="canonical store unavailable"):
        service.report()

    assert _count(tmp_path, "stat_sessions") == 1


# -- Lifetime -----------------------------------------------------------------


def test_async_reads_run_on_the_index_worker_pool_and_are_unavailable_after_close(
    index: StatisticsIndex,
    session: ChatSession,
    statistics: StatisticsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = statistics(index=index)
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
        await index.aclose()
        await index.aclose()
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
