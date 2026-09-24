"""Event Loop lag monitor, resource gauges, stall watchdog and clean shutdown."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.performance import PerformanceService
from core.performance._monitor import StallRecord, code_path, render_stack

WATCHDOG_THREAD = "vbot-performance-watchdog"


def _fast_service(tmp_path: Path, **kwargs) -> PerformanceService:
    return PerformanceService(
        tmp_path / "performance",
        monitor_interval_s=0.02,
        sample_interval_s=0.05,
        stall_threshold_s=0.1,
        watchdog_cadence_s=0.01,
        **kwargs,
    )


async def _wait_for(condition, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not await condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


def block_event_loop_for_test() -> None:
    time.sleep(0.3)


def _is_test_stall(stall: dict) -> bool:
    return any(
        "block_event_loop_for_test" in frame
        for sample in stall["samples"]
        for frame in sample["stack"]
    )


@pytest.mark.asyncio
async def test_blocked_loop_records_lag_and_a_stall_with_the_blocking_stack(
    tmp_path: Path,
) -> None:
    service = _fast_service(tmp_path)
    service.start()
    try:
        await asyncio.sleep(0.05)
        service.start_recording()
        asyncio.get_running_loop().call_soon(block_event_loop_for_test)

        async def stalled() -> bool:
            return any(map(_is_test_stall, (await service.snapshot())["stalls"]))

        await _wait_for(stalled)
        snapshot = await service.snapshot()
        result = await service.stop_recording()
    finally:
        await service.aclose()

    # A busy test host may add unrelated stalls; select the one this test caused.
    stall = next(filter(_is_test_stall, snapshot["stalls"]))
    assert set(stall) == {"started_at", "duration_ms", "samples"}
    assert stall["duration_ms"] >= 150
    datetime.fromisoformat(stall["started_at"])
    frames = [frame for sample in stall["samples"] for frame in sample["stack"]]
    blocking = next(frame for frame in frames if "block_event_loop_for_test" in frame)
    assert blocking.startswith("tests/core/performance/test_monitor.py:")
    assert all("\\" not in frame for frame in frames)
    assert sum(sample["count"] for sample in stall["samples"]) >= 1
    assert snapshot["metrics"]["event_loop.lag"]["max_ms"] >= 150
    assert stall in result["summary"]["stalls"]
    assert result["summary"]["metrics"]["event_loop.lag"]["max_ms"] >= 150


@pytest.mark.asyncio
async def test_monitor_samples_loop_process_and_injected_gauges(tmp_path: Path) -> None:
    service = _fast_service(tmp_path, samplers={"runs.active": lambda: 2, "runs.queued": lambda: 0})
    service.start()
    try:

        async def sampled() -> bool:
            return "runs.queued" in (await service.snapshot())["gauges"]

        await _wait_for(sampled)
        gauges = (await service.snapshot())["gauges"]
    finally:
        await service.aclose()

    for name in (
        "event_loop.utilization",
        "process.cpu_percent",
        "process.rss_mb",
        "process.python_threads",
        "asyncio.tasks",
    ):
        assert isinstance(gauges[name], int | float), name
    assert gauges["process.rss_mb"] > 0
    # The Event Loop thread plus the stall watchdog.
    assert gauges["process.python_threads"] >= 2
    assert gauges["runs.active"] == 2


@pytest.mark.asyncio
async def test_failing_sampler_is_reported_once_and_others_continue(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> float:
        raise RuntimeError("sampler broke")

    service = _fast_service(tmp_path, samplers={"broken": broken, "runs.active": lambda: 1})
    with caplog.at_level(logging.WARNING, logger="vbot.performance"):
        service.start()
        try:

            async def sampled_twice() -> bool:
                gauges: dict[str, float] = (await service.snapshot())["gauges"]
                return gauges.get("runs.active") == 1

            await _wait_for(sampled_twice)
            await asyncio.sleep(0.15)
        finally:
            await service.aclose()

    assert caplog.text.count("gauge=broken") == 1


def test_long_stall_warnings_are_rate_limited_and_count_suppressions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service = PerformanceService(tmp_path / "performance")
    stall = StallRecord(
        started_perf=time.perf_counter(),
        started_at=datetime.now(UTC),
        duration_ms=1500.0,
        samples=((3, ("core/chat/x.py:10 work", "asyncio/events.py:88 Handle._run")),),
    )
    short = StallRecord(stall.started_perf, stall.started_at, 400.0, ())

    with caplog.at_level(logging.WARNING, logger="vbot.performance"):
        service._record_stall(stall)  # noqa: SLF001 - watchdog callback.
        service._record_stall(stall)  # noqa: SLF001
        service._record_stall(short)  # noqa: SLF001
        last_warning = service._last_stall_warning  # noqa: SLF001
        assert last_warning is not None
        service._last_stall_warning = last_warning - 31  # noqa: SLF001 - advance the window.
        service._record_stall(stall)  # noqa: SLF001

    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 2
    assert "stalled for 1500 ms" in warnings[0]
    assert "core/chat/x.py:10 work <- asyncio/events.py:88 Handle._run" in warnings[0]
    assert "suppressed_warnings=0" in warnings[0]
    assert "suppressed_warnings=1" in warnings[1]


def _monitor_resources(service: PerformanceService) -> tuple[asyncio.Task, threading.Thread]:
    monitor = service._monitor  # noqa: SLF001 - lifecycle resources under test.
    assert monitor._task is not None and monitor._thread is not None  # noqa: SLF001
    return monitor._task, monitor._thread  # noqa: SLF001


@pytest.mark.asyncio
async def test_stop_and_aclose_end_the_monitor_task_and_watchdog_thread(tmp_path: Path) -> None:
    for close_async in (False, True):
        service = _fast_service(tmp_path)
        service.start()
        await asyncio.sleep(0.05)
        task, thread = _monitor_resources(service)
        assert service.monitoring
        assert thread.name == WATCHDOG_THREAD and thread.is_alive()

        if close_async:
            await service.aclose()
        else:
            service.stop()
            await asyncio.sleep(0)

        assert not service.monitoring
        assert task.done()
        assert not thread.is_alive()


def test_watchdog_exits_when_its_event_loop_closes_without_stop(tmp_path: Path) -> None:
    service = _fast_service(tmp_path)
    resources: list[tuple[asyncio.Task, threading.Thread]] = []

    async def run_and_leave() -> None:
        service.start()
        resources.append(_monitor_resources(service))
        await asyncio.sleep(0.05)

    loop_thread = threading.Thread(target=asyncio.run, args=(run_and_leave(),))
    loop_thread.start()
    loop_thread.join()
    _task, watchdog = resources[0]

    watchdog.join(timeout=5)
    assert not watchdog.is_alive()
    service.stop()


def test_stack_rendering_uses_project_relative_forward_slash_locations() -> None:
    frames = render_stack(__import__("sys")._getframe())  # noqa: SLF001 - current frame.

    assert frames[0].startswith("tests/core/performance/test_monitor.py:")
    assert frames[0].endswith(" test_stack_rendering_uses_project_relative_forward_slash_locations")
    assert code_path(asyncio.__file__) == "asyncio/__init__.py"
    assert code_path("<frozen runpy>") == "<frozen runpy>"
