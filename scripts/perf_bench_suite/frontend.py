"""Run the WebUI Vitest benchmarks and convert their JSON output to result records.

The WebUI benchmarks are ``*.bench.js`` files in ``__benchmarks__`` folders
under ``webui/src``. They run with ``npx vitest bench --run --outputJson``;
Vitest reports milliseconds per operation, which become nanoseconds here.
A benchmark name may carry its size parameters after the name, e.g.
``markdown.stream[20k] (items=201, unit=chunk, chars=20006)``; ``items`` and
``unit`` state how many units one operation processes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from scripts.perf_bench_suite.report import ResultRecord

VITEST_TIMEOUT_SECONDS = 1800
TIME_ENVIRONMENT_VARIABLE = "VBOT_PERF_BENCH_TIME_MS"
_NAME_WITH_PARAMS = re.compile(r"^(?P<name>.+?) \((?P<params>[^()]*)\)$")
# Characters a --filter may contain and still be passed to Vitest as a name
# pattern. ``npx`` is a batch file on Windows, where cmd.exe metacharacters in
# arguments are unsafe; other filters are applied to the parsed results only.
_PATTERN_SAFE_FILTER = re.compile(r"^[A-Za-z0-9_.,=\[\] -]+$")
_MS_TO_NS = 1e6

FrontendStatus = Literal["ok", "failed", "skipped"]


@dataclass(frozen=True)
class FrontendRun:
    """Outcome of one Vitest benchmark run."""

    status: FrontendStatus
    records: list[ResultRecord] = field(default_factory=list)
    detail: str | None = None
    node_version: str | None = None

    def summary(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail, "node": self.node_version}


def benchmark_files(webui_dir: Path) -> list[Path]:
    """The WebUI benchmark files, relative to ``webui_dir``."""
    return sorted(
        path.relative_to(webui_dir)
        for path in (webui_dir / "src").rglob("__benchmarks__/*.bench.js")
        if "node_modules" not in path.parts
    )


def run_frontend_benchmarks(
    webui_dir: Path,
    *,
    output_path: Path,
    name_filter: str | None,
    time_ms: int,
) -> FrontendRun:
    """Run ``npx vitest bench --run`` in ``webui_dir`` and parse its JSON report."""
    npx = shutil.which("npx")
    if npx is None:
        return FrontendRun("skipped", detail="npx was not found on PATH")
    node_version = _node_version()
    command = [
        npx,
        "vitest",
        "bench",
        "--run",
        "--passWithNoTests",
        "--outputJson",
        str(output_path),
    ]
    if name_filter and _PATTERN_SAFE_FILTER.match(name_filter):
        command.extend(["--testNamePattern", case_insensitive_pattern(name_filter)])
    environment = {**os.environ, TIME_ENVIRONMENT_VARIABLE: str(time_ms)}
    try:
        completed = subprocess.run(
            command,
            cwd=webui_dir,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=VITEST_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return FrontendRun(
            "failed", detail=f"could not run Vitest: {exc}", node_version=node_version
        )
    if completed.returncode != 0:
        output = (completed.stdout + completed.stderr).strip()
        return FrontendRun(
            "failed",
            detail=f"vitest bench exited with {completed.returncode}:\n{_tail(output)}",
            node_version=node_version,
        )
    if not output_path.exists():
        return FrontendRun("ok", detail="Vitest ran no benchmarks", node_version=node_version)
    document = json.loads(output_path.read_text(encoding="utf-8"))
    records = parse_vitest_report(document, webui_dir=webui_dir)
    if name_filter:
        needle = name_filter.casefold()
        records = [record for record in records if needle in record.name.casefold()]
    return FrontendRun("ok", records=records, node_version=node_version)


def parse_vitest_report(
    document: Mapping[str, Any], *, webui_dir: Path | None = None
) -> list[ResultRecord]:
    """Convert a ``vitest bench --outputJson`` document to result records."""
    records: list[ResultRecord] = []
    for file in document.get("files", []):
        location = _display_path(str(file.get("filepath", "")), webui_dir)
        for group in file.get("groups", []):
            for benchmark in group.get("benchmarks", []):
                records.append(_record(benchmark, location))
    return records


def split_benchmark_name(full_name: str) -> tuple[str, dict[str, Any]]:
    """Split ``name (key=value, ...)`` into the name and typed parameters."""
    match = _NAME_WITH_PARAMS.match(full_name)
    if match is None:
        return full_name, {}
    params: dict[str, Any] = {}
    for part in match.group("params").split(","):
        key, separator, value = part.strip().partition("=")
        if not separator or not key:
            return full_name, {}
        params[key] = _typed(value)
    return match.group("name"), params


def case_insensitive_pattern(text: str) -> str:
    """A JavaScript RegExp source matching ``text`` literally, ignoring letter case."""
    pieces = []
    for character in text:
        if character.isalpha() and character.lower() != character.upper():
            pieces.append(f"[{character.lower()}{character.upper()}]")
        elif character in r"\^$.*+?()[]{}|/":
            pieces.append("\\" + character)
        else:
            pieces.append(character)
    return "".join(pieces)


def _record(benchmark: Mapping[str, Any], location: str) -> ResultRecord:
    name, params = split_benchmark_name(str(benchmark["name"]))
    items = params.pop("items", 1)
    unit = params.pop("unit", "")
    median = float(benchmark["median"]) * _MS_TO_NS
    margin = float(benchmark.get("moe") or 0.0) * _MS_TO_NS
    return ResultRecord(
        name=name,
        group=name.split(".", 1)[0],
        source="vitest",
        description=f"Vitest bench in {location}" if location else "Vitest bench",
        params=params,
        median_ns=median,
        min_ns=float(benchmark["min"]) * _MS_TO_NS,
        max_ns=float(benchmark["max"]) * _MS_TO_NS,
        mean_ns=float(benchmark["mean"]) * _MS_TO_NS,
        band_low_ns=median - margin,
        band_high_ns=median + margin,
        p99_ns=float(benchmark["p99"]) * _MS_TO_NS,
        stdev_ns=float(benchmark["sd"]) * _MS_TO_NS if "sd" in benchmark else None,
        samples=int(benchmark.get("sampleCount", 0)),
        items=items if isinstance(items, int) and items > 0 else 1,
        item_unit=str(unit),
    )


def _typed(value: str) -> Any:
    for convert in (int, float):
        try:
            return convert(value)
        except ValueError:
            continue
    return value


def _display_path(filepath: str, webui_dir: Path | None) -> str:
    if not filepath:
        return ""
    path = Path(filepath)
    if webui_dir is not None:
        try:
            return path.resolve().relative_to(webui_dir.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _node_version() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    try:
        completed = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.splitlines()[-lines:])
