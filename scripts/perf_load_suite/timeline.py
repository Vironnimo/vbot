"""Retained-state evidence for long runs (``--duration``).

A performance recording summarizes a load phase as distributions; slow growth
of retained state only shows over time. For a duration run the harness
therefore samples ``performance.snapshot`` at a fixed interval into a compact
time series (memory, asyncio Tasks, Python threads, generation-2 collections),
takes a ``performance.heap`` census at the start and the end of the load phase
when the server offers it, and copies the server's persisted performance
history windows out of the disposable data directory.
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.perf_load_suite.recording import METHOD_NOT_FOUND_CODE
from scripts.perf_load_suite.rpc import RpcCaller, RpcCallError, RpcClient

DEFAULT_SNAPSHOT_INTERVAL_SECONDS = 60.0
HEAP_TOP = 25
HEAP_GROWTH_ROWS = 15
HISTORY_DIRECTORY = Path("artifacts") / "performance" / "history"
# Series key -> gauge name in performance.snapshot.
_GAUGES: dict[str, str] = {
    "rss_mb": "process.rss_mb",
    "asyncio_tasks": "asyncio.tasks",
    "python_threads": "process.python_threads",
    "runs_active": "runs.active",
}
TREND_KEYS: tuple[str, ...] = (
    "rss_mb",
    "asyncio_tasks",
    "python_threads",
    "gc_gen2_count",
    "gc_gen2_max_ms",
)


def snapshot_row(snapshot: Mapping[str, Any], elapsed_seconds: float) -> dict[str, Any]:
    """One time-series row from a ``performance.snapshot`` result.

    Histograms in a snapshot are cumulative since the server started, so the
    generation-2 figures grow monotonically; ``counters`` exist only on
    servers that report them.
    """
    gauges = _mapping(snapshot.get("gauges"))
    metrics = _mapping(snapshot.get("metrics"))
    gen2 = _mapping(metrics.get("gc.gen2"))
    lag = _mapping(metrics.get("event_loop.lag"))
    counters = snapshot.get("counters")
    row: dict[str, Any] = {"t_s": round(elapsed_seconds, 1)}
    row.update({key: gauges.get(name) for key, name in _GAUGES.items()})
    row.update(
        {
            "gc_gen2_count": gen2.get("count"),
            "gc_gen2_max_ms": gen2.get("max_ms"),
            "gc_gen2_sum_ms": gen2.get("sum_ms"),
            "lag_p99_ms": lag.get("p99_ms"),
            "recent_stalls": len(snapshot.get("stalls") or []),
        }
    )
    if isinstance(counters, dict):
        row["counters_total"] = sum(
            value for value in counters.values() if isinstance(value, int | float)
        )
    return row


def summarize_series(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """First, last, maximum and change of every trend key over the series."""
    trends: dict[str, Any] = {}
    for key in TREND_KEYS:
        values = [row[key] for row in rows if isinstance(row.get(key), int | float)]
        if not values:
            continue
        trends[key] = {
            "first": values[0],
            "last": values[-1],
            "max": max(values),
            "change": round(values[-1] - values[0], 3),
        }
    return trends


class SnapshotSampler:
    """Sample ``performance.snapshot`` on a background thread until stopped.

    The sampler uses its own RPC client so it never shares a connection with
    the load driver; a failed sample is recorded as an error row.
    """

    def __init__(
        self, base_url: str, *, interval_seconds: float = DEFAULT_SNAPSHOT_INTERVAL_SECONDS
    ) -> None:
        self._client = RpcClient(base_url)
        self._interval = interval_seconds
        self._rows: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="perf-snapshots", daemon=True)

    def start(self) -> None:
        self._started = time.monotonic()
        self._sample()
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        """Take a final sample and return the series with its trends."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=30.0)
        self._sample()
        self._client.close()
        rows = [row for row in self._rows if "error" not in row]
        return {
            "interval_seconds": self._interval,
            "samples": self._rows,
            "trends": summarize_series(rows),
        }

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._sample()

    def _sample(self) -> None:
        elapsed = time.monotonic() - self._started
        try:
            snapshot = self._client.call("performance.snapshot", {})
        except Exception as exc:  # noqa: BLE001 - one failed sample must not end the series
            self._rows.append({"t_s": round(elapsed, 1), "error": f"{type(exc).__name__}: {exc}"})
            return
        self._rows.append(snapshot_row(snapshot, elapsed))


def heap_census(rpc: RpcCaller, *, top: int = HEAP_TOP) -> dict[str, Any] | None:
    """Take one ``performance.heap`` census; ``None`` when the server has none."""
    try:
        return dict(rpc.call("performance.heap", {"top": top}))
    except RpcCallError as exc:
        if exc.code == METHOD_NOT_FOUND_CODE:
            return None
        raise


def heap_digest(start: Mapping[str, Any], end: Mapping[str, Any]) -> dict[str, Any]:
    """Tracked objects at start and end plus the end census's growth rows.

    ``tracked`` excludes the objects the server froze after startup
    (``frozen_end``), which no collection scans.

    The server computes ``growth`` against its previous census, which is the
    start census when nothing else took one in between.
    """
    tracked_start = start.get("tracked")
    tracked_end = end.get("tracked")
    change = (
        tracked_end - tracked_start
        if isinstance(tracked_start, int) and isinstance(tracked_end, int)
        else None
    )
    growth = [row for row in end.get("growth") or [] if isinstance(row, dict)]
    modules = [
        row
        for row in end.get("modules") or []
        if isinstance(row, dict) and isinstance(row.get("change"), int) and row["change"] > 0
    ]
    modules.sort(key=lambda row: -int(row["change"]))
    return {
        "tracked_start": tracked_start,
        "tracked_end": tracked_end,
        "tracked_change": change,
        "frozen_end": end.get("frozen"),
        "census_ms": [start.get("duration_ms"), end.get("duration_ms")],
        "growth": growth[:HEAP_GROWTH_ROWS],
        "module_growth": modules[:HEAP_GROWTH_ROWS],
    }


def copy_history(data_dir: Path, destination: Path) -> Path | None:
    """Copy the server's persisted performance history windows, if any."""
    source = data_dir / HISTORY_DIRECTORY
    if not source.is_dir():
        return None
    shutil.copytree(source, destination, dirs_exist_ok=True)
    return destination


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
