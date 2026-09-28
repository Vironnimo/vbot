"""Window summaries of the process-wide measurements, kept on disk across restarts.

At the end of every window (10 minutes by default) and at shutdown, the
service appends one JSON line to the file of the window's UTC end date: what
the histograms and counters gained during the window, the gauges' last and
highest values, and the window's stalls. Earlier processes thus stay
inspectable next to their log files.

The files are diagnostic artifacts like logs, not durable state: lines are
only appended, a line torn by a crash is skipped on read, and whole days are
pruned by age and total size.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from core.performance._metrics import HistogramData
from core.utils.logging import get_logger

_LOGGER = get_logger("performance")

HISTORY_DIRECTORY = "history"
REASON_INTERVAL = "interval"
REASON_SHUTDOWN = "shutdown"
MAX_WINDOW_STALLS = 20
_FILE_SUFFIX = ".jsonl"
_STALL_SAMPLES = 3
_STALL_THREAD_SAMPLES = 1
_FILTERED_SECTIONS = ("metrics", "counters", "gauges", "gauges_max")


@dataclass(frozen=True, slots=True)
class WindowStart:
    """The cumulative measurements a window's changes are counted from."""

    started_at: datetime
    histograms: Mapping[str, HistogramData]
    counters: Mapping[str, int]


def window_record(
    start: WindowStart,
    *,
    ended_at: datetime,
    reason: str,
    process_started_at: datetime,
    histograms: Mapping[str, HistogramData],
    counters: Mapping[str, int],
    gauges: Mapping[str, float],
    gauges_max: Mapping[str, float],
    stalls: Sequence[Mapping[str, Any]],
    stalls_dropped: int,
) -> dict[str, Any]:
    """Return one window line: the changes since ``start`` and the window's stalls."""
    metrics: dict[str, dict[str, float | int]] = {}
    for name in sorted(histograms):
        current = histograms[name]
        earlier = start.histograms.get(name)
        # A smaller count means the measurements were reset (tests only).
        if earlier is None or current.count < earlier.count:
            window = current
        else:
            window = current.since(earlier)
        if window.count > 0:
            metrics[name] = window.summary()
    counter_changes: dict[str, int] = {}
    for name in sorted(counters):
        value = counters[name]
        earlier_value = start.counters.get(name, 0)
        change = value if value < earlier_value else value - earlier_value
        if change:
            counter_changes[name] = change
    return {
        "started_at": start.started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "reason": reason,
        "process_started_at": process_started_at.isoformat(),
        "metrics": metrics,
        "counters": counter_changes,
        "gauges": dict(gauges),
        "gauges_max": dict(gauges_max),
        "stalls": [_compact_stall(stall) for stall in stalls],
        "stalls_dropped": stalls_dropped,
    }


def append_window(
    directory: Path, record: Mapping[str, Any], *, retention_days: int, max_bytes: int
) -> None:
    """Append one window line to its day file, then prune old and excess days."""
    day = datetime.fromisoformat(record["ended_at"]).astimezone(UTC).date()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day.isoformat()}{_FILE_SUFFIX}"
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a+b") as handle:
        # A crash can leave a torn last line; start on a fresh line so only
        # that line is lost.
        if handle.tell() > 0:
            handle.seek(-1, 2)
            if handle.read(1) != b"\n":
                line = "\n" + line
        handle.write(line.encode("utf-8"))
    prune_history(directory, today=day, retention_days=retention_days, max_bytes=max_bytes)


def prune_history(directory: Path, *, today: date, retention_days: int, max_bytes: int) -> None:
    """Delete day files older than ``retention_days`` or beyond ``max_bytes`` in total.

    The newest file is never deleted for size, so the current day always stays.
    """
    total = 0
    for index, (day, path) in enumerate(_day_files(directory)):
        try:
            total += path.stat().st_size
        except OSError:
            continue
        if (today - day).days < retention_days and (index == 0 or total <= max_bytes):
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            _LOGGER.warning("Could not delete a pruned performance history file (day=%s)", day)


def read_history(
    directory: Path,
    *,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    names: Collection[str] | None,
) -> list[dict[str, Any]]:
    """Return the newest ``limit`` windows that overlap ``since``..``until``, oldest first.

    With ``names``, each window keeps only those metrics, counters and gauges.
    """
    windows: list[dict[str, Any]] = []
    since_day = None if since is None else since.astimezone(UTC).date()
    for day, path in _day_files(directory):
        if since_day is not None and day < since_day:
            break
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            _LOGGER.warning("Skipped an unreadable performance history file (day=%s)", day)
            continue
        for line in reversed(lines):
            window = _parse_window(line)
            if window is None:
                continue
            started_at, ended_at, record = window
            if since is not None and ended_at < since:
                continue
            if until is not None and started_at > until:
                continue
            windows.append(_filtered(record, names))
            if len(windows) >= limit:
                return windows[::-1]
    return windows[::-1]


def _day_files(directory: Path) -> list[tuple[date, Path]]:
    """Return the day files of ``directory``, newest day first."""
    try:
        paths = list(directory.glob(f"*{_FILE_SUFFIX}"))
    except OSError:
        return []
    files: list[tuple[date, Path]] = []
    for path in paths:
        try:
            files.append((date.fromisoformat(path.name.removesuffix(_FILE_SUFFIX)), path))
        except ValueError:
            continue
    files.sort(reverse=True)
    return files


def _parse_window(line: str) -> tuple[datetime, datetime, dict[str, Any]] | None:
    try:
        record = json.loads(line)
        started_at = datetime.fromisoformat(record["started_at"])
        ended_at = datetime.fromisoformat(record["ended_at"])
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(record, dict) or started_at.tzinfo is None or ended_at.tzinfo is None:
        return None
    return started_at, ended_at, record


def _filtered(record: dict[str, Any], names: Collection[str] | None) -> dict[str, Any]:
    if names is None:
        return record
    for section in _FILTERED_SECTIONS:
        values = record.get(section)
        if isinstance(values, dict):
            record[section] = {name: value for name, value in values.items() if name in names}
    return record


def _compact_stall(stall: Mapping[str, Any]) -> dict[str, Any]:
    compact = dict(stall)
    samples = compact.get("samples")
    if isinstance(samples, list):
        compact["samples"] = samples[:_STALL_SAMPLES]
    threads = compact.get("threads")
    if isinstance(threads, list):
        compact["threads"] = [
            {**thread, "samples": thread.get("samples", [])[:_STALL_THREAD_SAMPLES]}
            for thread in threads
            if isinstance(thread, dict)
        ]
    return compact
