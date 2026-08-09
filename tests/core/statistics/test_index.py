"""Tests for the disposable incremental Statistics index."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from core.chat.messages import ChatMessage
from core.sessions import ChatSession, ChatSessionManager
from core.statistics import AgentDirectory, StatisticsService
from core.tools import tool_success

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


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
    session = manager.create("main", session_id="session-one")
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


def test_cached_snapshot_reloads_when_another_service_updates_index(
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


def test_appended_messages_are_loaded_from_the_persisted_tail_cursor(
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
    assert cursors[0].message_count == 2
    assert report.overview.total_runs == 2
    assert report.usage.totals.measured_input_tokens == 15


def test_metadata_change_updates_index_without_loading_transcript(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, manager, session = _service(tmp_path)
    service.report()
    manager.set_title("main", session.id, "Renamed")

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
    replacement_path = session.path.with_suffix(".replacement")
    replacement_path.write_text(
        "".join(
            json.dumps(message.to_dict(), separators=(",", ":")) + "\n"
            for message in replacement_messages
        ),
        encoding="utf-8",
    )
    replacement_path.replace(session.path)

    report = service.report()

    assert report.overview.total_runs == 1
    assert report.overview.run_status.failed == 1
    assert report.usage.totals.measured_input_tokens == 99


def test_deleted_session_is_pruned_from_index(tmp_path: Path) -> None:
    service, manager, session = _service(tmp_path)
    service.report()

    manager.delete("main", session.id)
    report = service.report()

    assert report.overview.total_sessions == 0
    index_path = tmp_path / "statistics" / "session-statistics.sqlite"
    with sqlite3.connect(index_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM statistics_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM statistics_records").fetchone()[0] == 0


def test_index_projection_does_not_store_large_or_sensitive_message_content(tmp_path: Path) -> None:
    secret_text = "DO-NOT-PERSIST-RAW-CONTENT"
    manager = ChatSessionManager(tmp_path)
    session = manager.create("main", session_id="session-one")
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5",
            content=secret_text,
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

    service.report()

    index_path = tmp_path / "statistics" / "session-statistics.sqlite"
    with sqlite3.connect(index_path) as connection:
        payload = "\n".join(
            row[0] for row in connection.execute("SELECT payload_json FROM statistics_records")
        )
    assert secret_text not in payload
    assert "reasoning" not in payload
    assert '"content":"visible"' in payload


def test_corrupt_index_is_discarded_and_rebuilt_once(tmp_path: Path) -> None:
    service, _manager, _session = _service(tmp_path)
    service.report()
    index_path = tmp_path / "statistics" / "session-statistics.sqlite"
    index_path.write_bytes(b"not a sqlite database")

    report = service.report()

    assert report.overview.total_runs == 1
    with sqlite3.connect(index_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
