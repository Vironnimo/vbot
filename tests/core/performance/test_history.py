"""Window summaries on disk: per-window changes, the shutdown window, reading and retention."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.performance import PerformanceService, count, record_duration, set_gauge
from core.performance._history import append_window
from core.performance._monitor import StallRecord, StallThread

DAY = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


def _service(tmp_path: Path, **kwargs: Any) -> PerformanceService:
    return PerformanceService(tmp_path / "performance", **{"history_interval_s": 3600, **kwargs})


def _window(started_at: datetime, *, minutes: int = 10, **fields: Any) -> dict[str, Any]:
    return {
        "started_at": started_at.isoformat(),
        "ended_at": (started_at + timedelta(minutes=minutes)).isoformat(),
        "reason": "interval",
        "process_started_at": DAY.isoformat(),
        "metrics": {},
        "counters": {},
        "gauges": {},
        "gauges_max": {},
        "stalls": [],
        "stalls_dropped": 0,
        **fields,
    }


def _append(directory: Path, window: dict[str, Any], **limits: int) -> None:
    append_window(
        directory,
        window,
        retention_days=limits.get("retention_days", 14),
        max_bytes=limits.get("max_bytes", 1 << 20),
    )


@pytest.mark.asyncio
async def test_windows_hold_only_their_own_changes_and_outlive_the_process(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.start()
    record_duration("test.op", 100.0)
    record_duration("test.op", 1.0)
    count("test.events", 3)
    set_gauge("test.level", 7)
    set_gauge("test.level", 2)
    stack = tuple(f"core/x.py:{line} f" for line in range(3))
    samples = tuple((5 - index, (f"core/y.py:{index} g", *stack)) for index in range(5))
    service._record_stall(  # noqa: SLF001 - watchdog callback.
        StallRecord(
            started_perf=time.perf_counter(),
            started_at=datetime.now(UTC),
            duration_ms=400.0,
            samples=samples,
            threads=(StallThread("performance_0", 150.0, samples),),
            awaited_by=stack,
        )
    )
    await service._write_window("interval")  # noqa: SLF001 - end the window now.
    record_duration("test.op", 5.0)
    count("test.events")
    await service.aclose()

    # A later server process reads the same files.
    later = _service(tmp_path)
    first, second = (await later.history())["windows"]
    await later.aclose()

    assert (first["reason"], second["reason"]) == ("interval", "shutdown")
    assert second["started_at"] == first["ended_at"]
    assert first["process_started_at"] == second["process_started_at"]
    assert first["metrics"]["test.op"]["count"] == 2
    assert first["metrics"]["test.op"]["max_ms"] == 100.0
    assert first["counters"]["test.events"] == 3
    assert (first["gauges"]["test.level"], first["gauges_max"]["test.level"]) == (2, 7)
    assert [len(stall["samples"]) for stall in first["stalls"]] == [3]
    # Other threads keep only their most frequent stack.
    assert [len(thread["samples"]) for thread in first["stalls"][0]["threads"]] == [1]
    assert first["stalls"][0]["awaited_by"] == list(stack)
    window_op = second["metrics"]["test.op"]
    assert window_op["count"] == 1
    # Extremes the window did not move are bounded by its bucket (about 10%).
    assert 4.5 <= window_op["min_ms"] <= 5.0 <= window_op["max_ms"] <= 5.5
    assert second["counters"]["test.events"] == 1
    # A gauge that did not change reports its level from the window start.
    assert second["gauges_max"]["test.level"] == 2
    assert second["stalls"] == [] and second["stalls_dropped"] == 0


@pytest.mark.asyncio
async def test_a_started_service_ends_a_window_each_interval(tmp_path: Path) -> None:
    service = _service(tmp_path, history_interval_s=0.05)
    service.start()
    deadline = time.monotonic() + 10
    while not (await service.history())["windows"]:
        assert time.monotonic() < deadline, "no interval window written"
        await asyncio.sleep(0.02)
    await service.aclose()

    reader = _service(tmp_path)
    reasons = [window["reason"] for window in (await reader.history())["windows"]]
    await reader.aclose()
    assert reasons[0] == "interval" and reasons[-1] == "shutdown"


@pytest.mark.asyncio
async def test_history_selects_windows_by_time_and_name_and_skips_torn_lines(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "performance" / "history"
    _append(directory, _window(DAY, metrics={"a": {"count": 1}, "b": {"count": 2}}))
    with (directory / "2026-09-20.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"started_at": "2026-09-20T10:10:00+00:00", "torn')
    _append(directory, _window(DAY + timedelta(minutes=10)))
    _append(directory, _window(DAY + timedelta(hours=14)))
    service = _service(tmp_path)

    def spans(result: dict[str, Any]) -> list[str]:
        return [window["started_at"][11:16] for window in result["windows"]]

    assert spans(await service.history()) == ["10:00", "10:10", "00:00"]
    assert spans(await service.history(since=DAY + timedelta(minutes=15))) == ["10:10", "00:00"]
    assert spans(await service.history(until=DAY + timedelta(minutes=5))) == ["10:00"]
    moment = DAY + timedelta(minutes=10)
    assert spans(await service.history(since=moment, until=moment)) == ["10:00", "10:10"]
    assert spans(await service.history(limit=1)) == ["00:00"]
    named = await service.history(until=DAY, names=["b"])
    assert named["windows"][0]["metrics"] == {"b": {"count": 2}}
    assert (named["interval_seconds"], named["retention_days"]) == (3600, 14)
    with pytest.raises(ValueError, match="offset"):
        await service.history(since=datetime(2026, 9, 20, 10, 0))
    await service.aclose()


@pytest.mark.asyncio
async def test_history_days_are_pruned_by_age_and_total_size(tmp_path: Path) -> None:
    directory = tmp_path / "history"
    for day in (1, 2, 10, 14):
        _append(directory, _window(datetime(2026, 9, day, tzinfo=UTC)))
    line_bytes = (directory / "2026-09-14.jsonl").stat().st_size

    _append(directory, _window(datetime(2026, 9, 15, tzinfo=UTC)), retention_days=14)
    after_age = sorted(path.name for path in directory.iterdir())
    _append(
        directory,
        _window(datetime(2026, 9, 15, 1, tzinfo=UTC)),
        max_bytes=round(line_bytes * 3.5),
    )
    after_size = sorted(path.name for path in directory.iterdir())

    assert after_age == [
        "2026-09-02.jsonl",
        "2026-09-10.jsonl",
        "2026-09-14.jsonl",
        "2026-09-15.jsonl",
    ]
    # The current day holds two lines; one more day fits in the limit.
    assert after_size == ["2026-09-14.jsonl", "2026-09-15.jsonl"]
