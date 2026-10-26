"""Measurement periods retain history and count from the exact server timestamp."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from core.chat.messages import ChatMessage
from core.json_documents import snapshot_document_paths
from core.sessions import ChatSessionManager
from core.settings.validation import validate_data_dir_config
from core.statistics._period import StatisticsPeriodError
from core.statistics.index import StatisticsIndex
from core.tools import tool_success
from core.usage import UsageRecorder
from core.utils.timestamps import format_canonical_timestamp
from tests.core.sessions.history_fixtures import seed_history
from tests.core.statistics.statistics_test_support import (
    BASE,
    StatisticsFactory,
    _admit,
    _assistant,
    _call,
    _run_summary,
    _tool,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [timedelta(minutes=37, microseconds=123456), timedelta(hours=1)])
async def test_period_counts_exactly_from_start_without_changing_history(
    manager: ChatSessionManager,
    statistics: StatisticsFactory,
    ledger: UsageRecorder,
    index: StatisticsIndex,
    offset: timedelta,
) -> None:
    start = BASE + offset
    now = start
    service = statistics(usage_recorder=ledger, index=index, clock=lambda: now)
    session = manager.create("main")
    assert service.measurement_period() == {"started_at": None}
    marker = service.start_measurement_period()
    assert marker == {"started_at": format_canonical_timestamp(start)}
    # A pre-start call and a call at the exact boundary, in the same Session;
    # another call in the next full hour exercises the retained aggregate tier.
    for i, at in enumerate((start - timedelta(microseconds=1), start, start + timedelta(hours=1))):
        run_id = f"r{i}"
        _admit(manager, session.address, run_id, at=at)
        usage = {
            "input_tokens": 3000,
            "output_tokens": 10,
            "cache_read_tokens": 2500,
            "reported_cost_usd": i + 1,
            "speed": {"first_token_ms": (i + 1) * 100},
        }
        call_id = await _call(ledger, usage, address=session.address, run_id=run_id, at=at)
        seed_history(
            manager.get(session.address),
            [
                ChatMessage.user("work", timestamp=at),
                _assistant(model="chat/m", at=at, usage={**usage, "usage_call_id": call_id}),
                _tool(name="shell", at=at, envelope=tool_success({}), duration_ms=10),
                _run_summary(status="completed", at=at, duration_ms=10, run_id=run_id),
            ],
        )
    now = start + timedelta(hours=2)
    full = service.report()
    history = manager.get(session.address).load()

    report = service.report(period=True)

    assert report["window"]["since"] == marker["started_at"]
    assert report["measurement_period"] == marker
    assert report["overview"]["previous"] is None
    assert report["runs"]["previous"] is None
    assert report["overview"]["totals"]["calls"] == 2
    assert report["usage"]["totals"]["cost_usd"] == 5
    assert report["usage"]["totals"]["first_token_p50_ms"] == 200
    assert sum(point["calls"] for point in report["usage"]["series"]) == 2
    assert report["runs"]["totals"]["total"] == 2
    assert report["tools"]["totals"]["calls"] == 2
    assert report["activity"]["totals"]["your_runs"] == 2
    assert report["diagnostics"]["roles"]["chat_messages_by_role"] == {"user": 2, "assistant": 2}
    cache = report["diagnostics"]["cache"]["lowest_hit_rate_sessions"]
    assert cache[0]["turns"] == 2
    # Recreating the service and its projection must retain the operator state.
    index.discard()
    restarted = statistics(usage_recorder=ledger, index=index, clock=lambda: now)
    assert restarted.measurement_period() == marker
    assert restarted.report(period=True)["usage"]["totals"] == report["usage"]["totals"]
    assert restarted.report() == full
    assert manager.get(session.address).load() == history
    assert restarted.clear_measurement_period() == {"started_at": None}
    assert service.measurement_period() == {"started_at": None}
    assert service.report() == full
    assert service.report(period=True)["usage"]["totals"] == full["usage"]["totals"]


def test_period_document_is_guarded_preserved_and_included_in_snapshots(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    service = statistics(clock=lambda: BASE)
    service.start_measurement_period()
    path = Path(manager.data_dir) / "statistics" / "period.json"
    data = json.loads(path.read_text())
    data["future"] = {"preserve": True}
    path.write_text(json.dumps(data))
    service.clear_measurement_period()
    assert json.loads(path.read_text())["future"] == data["future"]
    assert "statistics/period.json" in snapshot_document_paths(Path(manager.data_dir))
    assert any(report.file_path == path for report in validate_data_dir_config(manager.data_dir))
    for value in ("invalid", 123, "2026-06-01T12:00:00", "2026-06-01T12:00:00+00:00"):
        invalid = json.dumps({"format_version": 1, "started_at": value})
        path.write_text(invalid)
        with pytest.raises(StatisticsPeriodError):
            service.measurement_period()
        with pytest.raises(StatisticsPeriodError):
            service.start_measurement_period()
        assert path.read_text() == invalid
