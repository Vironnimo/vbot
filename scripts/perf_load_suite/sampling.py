"""Background CPU/RSS sampling of harness-owned processes with psutil."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import psutil  # type: ignore[import-untyped]

DEFAULT_INTERVAL_SECONDS = 0.5


@dataclass
class _Series:
    root: psutil.Process
    children: dict[int, psutil.Process] = field(default_factory=dict)
    cpu: list[float] = field(default_factory=list)
    tree_cpu: list[float] = field(default_factory=list)
    rss_mb: list[float] = field(default_factory=list)


class ProcessSampler:
    """Sample named process trees until stopped.

    ``cpu`` is the root process alone (100 = one fully used core); ``tree_cpu``
    adds every descendant, such as Tool subprocesses.
    """

    def __init__(
        self,
        pids: dict[str, int],
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._interval = interval_seconds
        self._series: dict[str, _Series] = {}
        for name, pid in pids.items():
            try:
                process = psutil.Process(pid)
                process.cpu_percent(None)
            except psutil.NoSuchProcess:
                continue
            self._series[name] = _Series(root=process)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="perf-sampler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> dict[str, dict[str, Any]]:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)
        return {name: _summarize(series) for name, series in self._series.items()}

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            for series in self._series.values():
                _sample(series)


def _sample(series: _Series) -> None:
    try:
        cpu = series.root.cpu_percent(None)
        rss = series.root.memory_info().rss / (1024 * 1024)
        current = series.root.children(recursive=True)
    except psutil.Error:
        return
    tree_cpu = cpu
    alive: dict[int, psutil.Process] = {}
    for child in current:
        known = series.children.get(child.pid, child)
        try:
            tree_cpu += known.cpu_percent(None)
        except psutil.Error:
            continue
        alive[child.pid] = known
    series.children = alive
    series.cpu.append(cpu)
    series.tree_cpu.append(tree_cpu)
    series.rss_mb.append(rss)


def _summarize(series: _Series) -> dict[str, Any]:
    def average(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 1) if values else None

    return {
        "samples": len(series.cpu),
        "cpu_avg": average(series.cpu),
        "cpu_max": round(max(series.cpu), 1) if series.cpu else None,
        "tree_cpu_avg": average(series.tree_cpu),
        "tree_cpu_max": round(max(series.tree_cpu), 1) if series.tree_cpu else None,
        "rss_max_mb": round(max(series.rss_mb), 1) if series.rss_mb else None,
    }
