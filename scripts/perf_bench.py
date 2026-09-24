#!/usr/bin/env python
"""Run vBot's microbenchmarks and write a JSON report.

The suite gives repeatable measurements of suspected hot paths so an
optimization can be proven and a regression caught. Every benchmark calls the
real vBot functions on synthetic data inside a temporary directory; nothing
reads or writes a real data directory, and nothing leaves the machine.

Python benchmarks (sessions, chat, provider, server) run in this process on one
Event Loop. Each builds its fixtures once, warms up, calibrates the iteration
count so one sample takes about --target-seconds, then takes --samples samples
timed with time.perf_counter_ns. gc.collect() runs between samples; the
collector stays enabled during them.

WebUI benchmarks (markdown, timeline) are Vitest ``*.bench.js`` files in
``webui/src/**/__benchmarks__/``. They run through
``npx vitest bench --run`` in ``webui/`` and are merged into the same report.

Results are written to ``perf-results/bench-<UTC timestamp>.json`` and copied
to ``perf-results/bench-latest.json`` (git-ignored). Times are per operation.

Examples:
    python scripts/perf_bench.py
    python scripts/perf_bench.py --quick --only backend
    python scripts/perf_bench.py --filter sessions.
    python scripts/perf_bench.py --frontend
    python scripts/perf_bench.py --compare perf-results/bench-20260101T120000Z.json
    python scripts/perf_bench.py --tmp-dir D:/scratch   # fsync cost of another disk
    python scripts/perf_bench.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Import project packages from the invoked checkout.
_project_root = Path(__file__).resolve().parents[1]
if sys.path[:1] != [str(_project_root)]:
    sys.path.insert(0, str(_project_root))

from scripts.perf_bench_suite.catalog import BENCHMARKS  # noqa: E402
from scripts.perf_bench_suite.frontend import (  # noqa: E402
    FrontendRun,
    benchmark_files,
    run_frontend_benchmarks,
)
from scripts.perf_bench_suite.report import (  # noqa: E402
    DEFAULT_THRESHOLD_PERCENT,
    ResultRecord,
    build_report,
    collect_environment,
    compare,
    format_comparison_table,
    format_results_table,
    load_records,
    write_report,
)
from scripts.perf_bench_suite.runner import (  # noqa: E402
    BenchContext,
    BenchFailure,
    Benchmark,
    RunSettings,
    run_benchmarks,
    select_benchmarks,
)

WEBUI_DIR = _project_root / "webui"
DEFAULT_OUTPUT_DIR = _project_root / "perf-results"
DEFAULT_SAMPLES = 10
QUICK_SAMPLES = 3
DEFAULT_TARGET_SECONDS = 0.2
QUICK_TARGET_SECONDS = 0.05
DEFAULT_FRONTEND_TIME_MS = 1000
QUICK_FRONTEND_TIME_MS = 100


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perf_bench.py",
        description=__doc__.split("\n\n", 1)[0] if __doc__ else None,
        epilog=__doc__.split("\n\n", 1)[1] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    selection = parser.add_argument_group("selection")
    selection.add_argument(
        "--filter",
        metavar="TEXT",
        help="run only benchmarks whose name contains TEXT (case-insensitive)",
    )
    side = selection.add_mutually_exclusive_group()
    side.add_argument(
        "--only",
        choices=("backend", "frontend"),
        help="run only the Python (backend) or only the Vitest (frontend) benchmarks",
    )
    side.add_argument(
        "--frontend",
        dest="only",
        action="store_const",
        const="frontend",
        help="shorthand for --only frontend",
    )
    side.add_argument(
        "--backend",
        dest="only",
        action="store_const",
        const="backend",
        help="shorthand for --only backend",
    )
    selection.add_argument(
        "--list", action="store_true", help="list the benchmarks and exit without running"
    )

    sampling = parser.add_argument_group("sampling")
    sampling.add_argument(
        "--quick",
        action="store_true",
        help=(
            f"smoke-run settings: {QUICK_SAMPLES} samples of ~{QUICK_TARGET_SECONDS} s, "
            f"{QUICK_FRONTEND_TIME_MS} ms per Vitest benchmark (explicit options still win)"
        ),
    )
    sampling.add_argument(
        "--samples",
        type=_positive_int,
        metavar="N",
        help=f"samples per Python benchmark (default {DEFAULT_SAMPLES})",
    )
    sampling.add_argument(
        "--target-seconds",
        type=_positive_float,
        metavar="S",
        help=f"calibrated duration of one Python sample (default {DEFAULT_TARGET_SECONDS})",
    )
    sampling.add_argument(
        "--frontend-time-ms",
        type=_positive_int,
        metavar="MS",
        help=f"measuring time per Vitest benchmark (default {DEFAULT_FRONTEND_TIME_MS})",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--compare",
        type=Path,
        metavar="OLD.json",
        help="compare this run's medians with an earlier report and print the change",
    )
    output.add_argument(
        "--threshold",
        type=_positive_float,
        default=DEFAULT_THRESHOLD_PERCENT,
        metavar="PERCENT",
        help=(
            "with --compare: a change counts as slower/faster only above this percentage "
            f"and outside the old noise band (default {DEFAULT_THRESHOLD_PERCENT:g})"
        ),
    )
    output.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        metavar="DIR",
        help="where reports are written (default: perf-results/ in this checkout)",
    )
    output.add_argument(
        "--tmp-dir",
        type=Path,
        metavar="DIR",
        help=(
            "parent directory of the temporary work directory (default: the system "
            "temp directory); its disk determines the sessions.append fsync cost"
        ),
    )
    return parser


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _in_selection(record: ResultRecord, name_filter: str | None, only: str | None) -> bool:
    """Whether this run's --filter/--only would have selected a baseline record."""
    if name_filter and name_filter.casefold() not in record.name.casefold():
        return False
    if only == "backend":
        return record.source == "python"
    if only == "frontend":
        return record.source == "vitest"
    return True


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _list_benchmarks(benchmarks: list[Benchmark]) -> None:
    print("Python benchmarks:")
    for benchmark in benchmarks:
        print(f"  {benchmark.name}\n      {benchmark.description}")
    print("\nVitest benchmark files (names are listed by `npx vitest bench --run`):")
    for path in benchmark_files(WEBUI_DIR):
        print(f"  webui/{path.as_posix()}")


def _run_backend(
    benchmarks: list[Benchmark], settings: RunSettings, tmp_parent: Path | None
) -> tuple[list[ResultRecord], list[BenchFailure]]:
    work_dir = Path(tempfile.mkdtemp(prefix="vbot-perf-bench-", dir=tmp_parent))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    context = BenchContext(work_dir, loop)
    try:
        return run_benchmarks(benchmarks, context, settings, progress=_progress)
    finally:
        try:
            context.close()
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            asyncio.set_event_loop(None)
            loop.close()
            shutil.rmtree(work_dir, ignore_errors=True)


def _run_frontend(name_filter: str | None, time_ms: int) -> FrontendRun:
    _progress("[frontend] npx vitest bench --run (webui/)")
    with tempfile.TemporaryDirectory(prefix="vbot-perf-bench-vitest-") as scratch:
        return run_frontend_benchmarks(
            WEBUI_DIR,
            output_path=Path(scratch) / "vitest-bench.json",
            name_filter=name_filter,
            time_ms=time_ms,
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    selected = select_benchmarks(BENCHMARKS, args.filter)
    if args.list:
        _list_benchmarks(selected)
        return 0

    settings = RunSettings(
        samples=args.samples or (QUICK_SAMPLES if args.quick else DEFAULT_SAMPLES),
        target_sample_seconds=args.target_seconds
        or (QUICK_TARGET_SECONDS if args.quick else DEFAULT_TARGET_SECONDS),
    )
    frontend_time_ms = args.frontend_time_ms or (
        QUICK_FRONTEND_TIME_MS if args.quick else DEFAULT_FRONTEND_TIME_MS
    )
    # Read the baseline first so a bad path fails before a long run.
    baseline = load_records(args.compare) if args.compare is not None else None
    created_at = datetime.now(UTC)

    records: list[ResultRecord] = []
    failures: list[BenchFailure] = []
    if args.only != "frontend" and selected:
        records, failures = _run_backend(selected, settings, args.tmp_dir)
    frontend = FrontendRun("skipped", detail="not requested")
    if args.only != "backend":
        frontend = _run_frontend(args.filter, frontend_time_ms)
        records.extend(frontend.records)

    environment: dict[str, Any] = {
        **collect_environment(_project_root),
        "node": frontend.node_version,
    }
    report = build_report(
        created_at=created_at,
        environment=environment,
        settings={
            **asdict(settings),
            "quick": args.quick,
            "filter": args.filter,
            "only": args.only,
            "frontend_time_ms": frontend_time_ms,
            "tmp_dir": str(args.tmp_dir or Path(tempfile.gettempdir())),
        },
        records=records,
        failures=[asdict(failure) for failure in failures],
        frontend=frontend.summary(),
    )
    path = write_report(report, args.output_dir, created_at)

    if records:
        print(format_results_table(records))
    else:
        print("No benchmark ran.")
    for failure in failures:
        print(f"\nFAILED {failure.name}: {failure.error}\n{failure.traceback}", file=sys.stderr)
    if args.only != "backend" and frontend.detail:
        print(f"\nFrontend benchmarks ({frontend.status}): {frontend.detail}", file=sys.stderr)
    if baseline is not None:
        in_scope = [record for record in baseline if _in_selection(record, args.filter, args.only)]
        comparisons = compare(in_scope, records, threshold_percent=args.threshold)
        print(f"\nCompared with {args.compare}:")
        print(format_comparison_table(comparisons, args.threshold))
    print(f"\nReport: {path}")
    return 1 if failures or frontend.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
