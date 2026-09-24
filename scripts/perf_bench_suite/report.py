"""Result records, the JSON report, run-to-run comparison and console tables.

All durations are nanoseconds per operation. Python benchmarks report their
p90 over samples and use the sample min/max as their noise band; Vitest
benchmarks report Vitest's p99 and use ``median +/- margin of error`` as their
noise band because Vitest does not expose raw samples through its JSON output.
"""

from __future__ import annotations

import json
import math
import os
import platform
import sqlite3
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

REPORT_SCHEMA = "vbot-perf-bench/1"
LATEST_REPORT_NAME = "bench-latest.json"
DEFAULT_THRESHOLD_PERCENT = 10.0

Source = Literal["python", "vitest"]
Verdict = Literal["slower", "faster", "noise", "new", "removed"]


@dataclass(frozen=True)
class ResultRecord:
    """Measured cost of one benchmark in nanoseconds per operation."""

    name: str
    group: str
    source: Source
    description: str
    median_ns: float
    min_ns: float
    max_ns: float
    mean_ns: float
    band_low_ns: float
    band_high_ns: float
    samples: int
    params: dict[str, Any] = field(default_factory=dict)
    p90_ns: float | None = None
    p99_ns: float | None = None
    stdev_ns: float | None = None
    iterations: int | None = None
    samples_ns: tuple[float, ...] = ()
    items: int = 1
    item_unit: str = ""

    @property
    def ops_per_second(self) -> float:
        return 1e9 / self.median_ns if self.median_ns > 0 else math.inf

    @property
    def median_per_item_ns(self) -> float:
        return self.median_ns / max(self.items, 1)

    @property
    def tail_ns(self) -> float | None:
        """The upper-tail statistic shown in the table: p90 (Python) or p99 (Vitest)."""
        return self.p90_ns if self.p90_ns is not None else self.p99_ns

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["samples_ns"] = list(self.samples_ns)
        data["ops_per_second"] = self.ops_per_second
        data["median_per_item_ns"] = self.median_per_item_ns
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ResultRecord:
        return cls(
            name=str(data["name"]),
            group=str(data["group"]),
            source=data["source"],
            description=str(data.get("description", "")),
            median_ns=float(data["median_ns"]),
            min_ns=float(data["min_ns"]),
            max_ns=float(data["max_ns"]),
            mean_ns=float(data["mean_ns"]),
            band_low_ns=float(data["band_low_ns"]),
            band_high_ns=float(data["band_high_ns"]),
            samples=int(data["samples"]),
            params=dict(data.get("params") or {}),
            p90_ns=_optional_float(data.get("p90_ns")),
            p99_ns=_optional_float(data.get("p99_ns")),
            stdev_ns=_optional_float(data.get("stdev_ns")),
            iterations=_optional_int(data.get("iterations")),
            samples_ns=tuple(float(value) for value in data.get("samples_ns") or ()),
            items=int(data.get("items", 1)),
            item_unit=str(data.get("item_unit", "")),
        )


@dataclass(frozen=True)
class Comparison:
    """One benchmark's median change between an old and a new report."""

    name: str
    verdict: Verdict
    old_median_ns: float | None = None
    new_median_ns: float | None = None
    change_percent: float | None = None
    params_changed: bool = False


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)  # type: ignore[call-overload]


# --- report -----------------------------------------------------------------


def collect_environment(repo_root: Path) -> dict[str, Any]:
    """Describe the machine and checkout a report was measured on."""
    commit = _git(repo_root, "rev-parse", "HEAD")
    status = _git(repo_root, "status", "--porcelain")
    return {
        "python": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "cpu_count": os.cpu_count(),
        "sqlite": sqlite3.sqlite_version,
        "git_commit": commit,
        "git_dirty": None if status is None else bool(status),
    }


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def build_report(
    *,
    created_at: datetime,
    environment: Mapping[str, Any],
    settings: Mapping[str, Any],
    records: Sequence[ResultRecord],
    failures: Sequence[Mapping[str, Any]],
    frontend: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble the JSON report document."""
    return {
        "schema": REPORT_SCHEMA,
        "created_at": created_at.astimezone(UTC).isoformat(timespec="seconds"),
        "environment": dict(environment),
        "settings": dict(settings),
        "frontend": dict(frontend),
        "results": [record.to_dict() for record in records],
        "failures": [dict(failure) for failure in failures],
    }


def write_report(report: Mapping[str, Any], output_dir: Path, created_at: datetime) -> Path:
    """Write ``bench-<UTC timestamp>.json`` and refresh ``bench-latest.json``."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"bench-{stamp}.json"
    suffix = 2
    while path.exists():
        path = output_dir / f"bench-{stamp}-{suffix}.json"
        suffix += 1
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    (output_dir / LATEST_REPORT_NAME).write_text(text, encoding="utf-8")
    return path


def load_records(path: Path) -> list[ResultRecord]:
    """Read the result records of a report written by :func:`write_report`."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != REPORT_SCHEMA:
        raise ValueError(f"{path} is not a {REPORT_SCHEMA} report")
    return [ResultRecord.from_dict(item) for item in document.get("results", [])]


# --- comparison -------------------------------------------------------------


def compare(
    old: Iterable[ResultRecord],
    new: Iterable[ResultRecord],
    *,
    threshold_percent: float = DEFAULT_THRESHOLD_PERCENT,
) -> list[Comparison]:
    """Compare medians by benchmark name, in the new report's order.

    A change counts as ``slower`` or ``faster`` only when it exceeds
    ``threshold_percent`` and the new median falls outside the old report's
    noise band; every other change is ``noise``. Benchmarks present in only one
    report are ``new`` or ``removed``.
    """
    old_by_name = {record.name: record for record in old}
    comparisons: list[Comparison] = []
    seen: set[str] = set()
    for record in new:
        seen.add(record.name)
        previous = old_by_name.get(record.name)
        if previous is None:
            comparisons.append(
                Comparison(name=record.name, verdict="new", new_median_ns=record.median_ns)
            )
            continue
        comparisons.append(_compare_pair(previous, record, threshold_percent))
    comparisons.extend(
        Comparison(name=name, verdict="removed", old_median_ns=record.median_ns)
        for name, record in old_by_name.items()
        if name not in seen
    )
    return comparisons


def _compare_pair(old: ResultRecord, new: ResultRecord, threshold_percent: float) -> Comparison:
    change = (new.median_ns - old.median_ns) / old.median_ns * 100.0 if old.median_ns else 0.0
    outside_band = not old.band_low_ns <= new.median_ns <= old.band_high_ns
    verdict: Verdict = "noise"
    if abs(change) > threshold_percent and outside_band:
        verdict = "slower" if change > 0 else "faster"
    return Comparison(
        name=new.name,
        verdict=verdict,
        old_median_ns=old.median_ns,
        new_median_ns=new.median_ns,
        change_percent=change,
        params_changed=old.params != new.params,
    )


# --- formatting -------------------------------------------------------------


def format_duration(nanoseconds: float | None) -> str:
    """Format a duration with three significant digits and an ASCII unit."""
    if nanoseconds is None or math.isnan(nanoseconds):
        return "-"
    for unit, scale in (("s", 1e9), ("ms", 1e6), ("us", 1e3)):
        if abs(nanoseconds) >= scale:
            return f"{_three_significant(nanoseconds / scale)} {unit}"
    return f"{_three_significant(nanoseconds)} ns"


def format_rate(per_second: float) -> str:
    """Format an operations-per-second rate compactly (``12.3k``, ``1.05M``)."""
    if math.isinf(per_second):
        return "inf"
    for suffix, scale in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if per_second >= scale:
            return f"{_three_significant(per_second / scale)}{suffix}"
    return _three_significant(per_second)


def _three_significant(value: float) -> str:
    magnitude = abs(value)
    if magnitude >= 100:
        return f"{value:.0f}"
    if magnitude >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def format_params(params: Mapping[str, Any]) -> str:
    return " ".join(f"{key}={value}" for key, value in params.items())


def format_results_table(records: Sequence[ResultRecord]) -> str:
    """Render the results as an aligned plain-text table."""
    header = ("benchmark", "median", "tail", "min", "ops/s", "per item", "n x iter", "params")
    rows = [header]
    for record in records:
        per_item = ""
        if record.items > 1:
            unit = record.item_unit or "item"
            per_item = f"{format_duration(record.median_per_item_ns)}/{unit}"
        iterations = "-" if record.iterations is None else str(record.iterations)
        rows.append(
            (
                record.name,
                format_duration(record.median_ns),
                format_duration(record.tail_ns),
                format_duration(record.min_ns),
                format_rate(record.ops_per_second),
                per_item,
                f"{record.samples} x {iterations}",
                format_params(record.params),
            )
        )
    legend = (
        "All times per operation. tail = p90 over samples for Python benchmarks, "
        "Vitest's p99 for frontend benchmarks; n x iter = samples x iterations per sample."
    )
    return _render(rows, right_aligned={1, 2, 3, 4, 5}) + "\n" + legend


def format_comparison_table(comparisons: Sequence[Comparison], threshold_percent: float) -> str:
    """Render a comparison as an aligned plain-text table."""
    rows = [("benchmark", "old median", "new median", "change", "verdict")]
    for item in comparisons:
        change = "-" if item.change_percent is None else f"{item.change_percent:+.1f}%"
        verdict = item.verdict + (" (params changed)" if item.params_changed else "")
        rows.append(
            (
                item.name,
                format_duration(item.old_median_ns),
                format_duration(item.new_median_ns),
                change,
                verdict,
            )
        )
    legend = (
        f"slower/faster: median moved more than {threshold_percent:g}% and left the old "
        "noise band (Python: old sample min..max; Vitest: old median +/- margin of error)."
    )
    return _render(rows, right_aligned={1, 2, 3}) + "\n" + legend


def _render(rows: Sequence[Sequence[str]], *, right_aligned: set[int]) -> str:
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    lines = []
    for index, row in enumerate(rows):
        cells = [
            cell.rjust(widths[column]) if column in right_aligned else cell.ljust(widths[column])
            for column, cell in enumerate(row)
        ]
        lines.append("  ".join(cells).rstrip())
        if index == 0:
            lines.append("  ".join("-" * width for width in widths))
    return "\n".join(lines)
