"""Integration tests for the Runtime-owned performance measurement service."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.performance import PerformanceService
from core.performance.performance import reset_for_tests
from core.runtime.runtime import Runtime
from core.storage.layout import DataDirectoryLayout
from core.utils.config import Config


@pytest.fixture
def config(tmp_path: Path) -> Iterator[Config]:
    logging.getLogger("vbot").handlers = []
    reset_for_tests()
    yield Config(data_dir=tmp_path / "data")
    reset_for_tests()


def _watchdog(service: PerformanceService) -> threading.Thread | None:
    return service._monitor._thread  # noqa: SLF001 - lifecycle resource under test.


@pytest.mark.asyncio
@pytest.mark.parametrize("safe_startup_mode", [None, "test"])
async def test_runtime_monitors_the_loop_and_records_into_the_data_directory(
    config: Config, safe_startup_mode: str | None
) -> None:
    runtime = Runtime(config, safe_startup_mode=safe_startup_mode)  # type: ignore[arg-type]
    runtime.start()
    try:
        service = runtime.performance
        watchdog = _watchdog(service)
        assert service.monitoring
        service.start_recording()
        deadline = time.monotonic() + 10
        while "runs.queued" not in (await service.snapshot())["gauges"]:
            assert time.monotonic() < deadline, "Run gauges were never sampled"
            await asyncio.sleep(0.05)
        result = await service.stop_recording()
        gauges = (await service.snapshot())["gauges"]
    finally:
        await runtime.aclose()

    assert Path(result["trace_path"]).parent == DataDirectoryLayout(config.data_dir).performance
    assert gauges["runs.active"] == 0 and gauges["runs.queued"] == 0
    assert not service.monitoring
    assert watchdog is not None and not watchdog.is_alive()
    assert runtime._performance is None  # noqa: SLF001
    with pytest.raises(RuntimeError):
        _ = runtime.performance


def test_runtime_started_without_an_event_loop_does_not_monitor(config: Config) -> None:
    runtime = Runtime(config)
    runtime.start()
    try:
        assert not runtime.performance.monitoring
    finally:
        runtime.stop()
    assert runtime._performance is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_failed_startup_after_monitoring_began_stops_the_monitor(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(config)
    started: list[tuple[PerformanceService, threading.Thread | None]] = []
    original_start = Runtime._start_performance_service  # noqa: SLF001

    def start_and_capture(self: Runtime) -> None:
        original_start(self)
        service = self._performance  # noqa: SLF001
        assert service is not None and service.monitoring
        started.append((service, _watchdog(service)))

    def fail(_self: Runtime) -> None:
        raise RuntimeError("provider usage failed to start")

    monkeypatch.setattr(Runtime, "_start_performance_service", start_and_capture)
    monkeypatch.setattr(Runtime, "_start_provider_usage_service", fail)

    with pytest.raises(RuntimeError, match="provider usage failed"):
        runtime.start()
    await asyncio.sleep(0)

    service, watchdog = started[0]
    assert not service.monitoring
    assert watchdog is not None and not watchdog.is_alive()
    assert runtime._performance is None  # noqa: SLF001
