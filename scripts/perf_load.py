#!/usr/bin/env python
"""Concurrent-Agent load test for vBot against a scripted fake Provider.

For every concurrency level the harness starts its own vBot server from this
checkout (free loopback port, fresh temporary data directory), seeds a fixture
Project with rooted Identity Agents and one Session per simulated Agent, and
sends every Session the same scripted turns concurrently through ``chat.stream``.
A separate fake OpenAI-compatible Provider process scripts each turn: Tool-call
rounds with real Tool names first, then paced text carrying timing markers.

It reports where vBot itself spends time: TTFT and step overhead beyond the
scripted Provider time, delta latency, Run duration versus ideal, server CPU
and memory, plus the server's own performance recording (Event Loop lag,
worker pools, SQLite writes, request building, stalls). Results land in
``perf-results/load-<UTC timestamp>/`` as ``result.json`` and ``report.md``.

Examples::

    python scripts/perf_load.py --quick
    python scripts/perf_load.py --agents 1,10,20,30 --profile
    python scripts/perf_load.py --compare perf-results/load-<old>/result.json
    python scripts/perf_load.py compare old/result.json new/result.json
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

# Direct execution loads project packages from this checkout.
_checkout_root = Path(__file__).resolve().parents[1]
if sys.path[:1] != [str(_checkout_root)]:
    sys.path.insert(0, str(_checkout_root))

from core.utils.processes import activate_process_containment  # noqa: E402
from scripts.perf_load_suite.directive import DEFAULT_TOOLS  # noqa: E402
from scripts.perf_load_suite.profiling import py_spy_unavailable_reason  # noqa: E402
from scripts.perf_load_suite.report import (  # noqa: E402
    compare_results,
    load_result,
    render_console,
)
from scripts.perf_load_suite.runner import (  # noqa: E402
    DEFAULT_LEVELS,
    DEFAULT_OUTPUT_ROOT,
    QUICK_LEVELS,
    LoadConfig,
    failed_levels,
    run_load,
)

EXIT_OK = 0
EXIT_LEVEL_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


def _positive_int_list(text: str) -> tuple[int, ...]:
    try:
        values = tuple(int(part) for part in text.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected comma-separated integers, got {text!r}"
        ) from exc
    if not values or any(value < 1 for value in values) or len(set(values)) != len(values):
        raise argparse.ArgumentTypeError("expected distinct positive integers, e.g. 1,10,20,30")
    return values


def _name_list(text: str) -> tuple[str, ...]:
    names = tuple(part.strip() for part in text.split(",") if part.strip())
    if not names:
        raise argparse.ArgumentTypeError("expected at least one Tool name")
    return names


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Show defaults for valued options only, not for on/off flags."""

    def _get_help_string(self, action: argparse.Action) -> str | None:
        if action.default is None or isinstance(action.default, bool):
            return action.help
        return super()._get_help_string(action)


def build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perf_load.py",
        description=(
            "Load-test vBot with 1..N concurrent Agents against a scripted fake Provider. "
            "Each level runs on its own disposable server; nothing touches existing "
            "vBot instances or data. Use 'perf_load.py compare A.json B.json' to diff two runs."
        ),
        formatter_class=_HelpFormatter,
    )
    load = parser.add_argument_group("load shape")
    load.add_argument(
        "--agents",
        type=_positive_int_list,
        default=",".join(map(str, DEFAULT_LEVELS)),
        metavar="N,N,...",
        help="concurrency levels; each level runs N Sessions concurrently on a fresh server",
    )
    load.add_argument("--turns", type=int, default=3, help="sequential turns per Session")
    load.add_argument(
        "--identity-agents",
        type=int,
        default=3,
        help="Identity Agents the Sessions of a level are spread over",
    )
    load.add_argument(
        "--history-tokens",
        type=int,
        default=0,
        help="grow every Session's history by about this many tokens before measuring",
    )
    load.add_argument(
        "--quick",
        action="store_true",
        help=f"smoke run: levels {','.join(map(str, QUICK_LEVELS))} with 1 turn "
        "(overrides --agents and --turns)",
    )
    scripted = parser.add_argument_group("scripted Provider turn")
    scripted.add_argument(
        "--steps",
        type=int,
        default=4,
        help="Model requests per turn: steps-1 Tool-call rounds, then one text response",
    )
    scripted.add_argument(
        "--tokens", type=int, default=400, help="text tokens of the final response"
    )
    scripted.add_argument(
        "--rate", type=float, default=80.0, help="streamed tokens per second (0 = unpaced)"
    )
    scripted.add_argument(
        "--think-ms", type=int, default=600, help="delay before the first text token"
    )
    scripted.add_argument(
        "--tools",
        type=_name_list,
        default=",".join(DEFAULT_TOOLS),
        metavar="NAME,NAME,...",
        help="Tools rotated through the Tool-call rounds",
    )
    scripted.add_argument("--calls", type=int, default=1, help="parallel Tool calls per round")
    measure = parser.add_argument_group("measurement")
    measure.add_argument(
        "--run-timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="per-turn limit; a Run exceeding it is cancelled and counted as a timeout",
    )
    measure.add_argument(
        "--recording-max-seconds",
        type=int,
        default=1800,
        help="upper bound for the server-side performance recording of one load phase "
        "(1-3600 seconds)",
    )
    measure.add_argument(
        "--profile",
        action="store_true",
        help="record a py-spy flamegraph of the server during the highest level",
    )
    measure.add_argument(
        "--profile-gil",
        action="store_true",
        help="like --profile, but sample only threads holding the GIL (py-spy --gil)",
    )
    measure.add_argument(
        "--ui",
        action="store_true",
        help="watch one streaming Session in a headless browser (needs Node.js, "
        "npm ci in tests/e2e and a built WebUI); skipped with a message when unavailable",
    )
    output = parser.add_argument_group("output")
    output.add_argument(
        "--output",
        type=Path,
        help="directory that receives the load-<UTC timestamp> result folder "
        "(default: perf-results/ in this checkout)",
    )
    output.add_argument(
        "--compare",
        type=Path,
        metavar="OLD_RESULT_JSON",
        help="after the run, print the difference to an earlier result.json",
    )
    output.add_argument(
        "--keep",
        action="store_true",
        help="keep the temporary data directories and fixture Project",
    )
    return parser


def build_compare_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perf_load.py compare",
        description="Print per-level metric changes between two perf_load result.json files.",
    )
    parser.add_argument("old", type=Path, help="baseline result.json")
    parser.add_argument("new", type=Path, help="result.json to compare against the baseline")
    return parser


def config_from_args(args: argparse.Namespace) -> LoadConfig:
    return LoadConfig(
        levels=QUICK_LEVELS if args.quick else args.agents,
        turns=1 if args.quick else args.turns,
        identity_agents=args.identity_agents,
        steps=args.steps,
        tokens=args.tokens,
        rate=args.rate,
        think_ms=args.think_ms,
        tools=args.tools,
        calls=args.calls,
        history_tokens=args.history_tokens,
        run_timeout_seconds=args.run_timeout,
        recording_max_seconds=args.recording_max_seconds,
        profile=args.profile or args.profile_gil,
        profile_gil=args.profile_gil,
        ui=args.ui,
        keep=args.keep,
        output_root=args.output if args.output is not None else DEFAULT_OUTPUT_ROOT,
    )


def run_compare(argv: Sequence[str]) -> int:
    args = build_compare_parser().parse_args(argv)
    try:
        old, new = load_result(args.old), load_result(args.new)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(compare_results(old, new))
    return EXIT_OK


def run(argv: Sequence[str]) -> int:
    parser = build_run_parser()
    args = parser.parse_args(argv)
    try:
        config = config_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    baseline = None
    if args.compare is not None:
        try:
            baseline = load_result(args.compare)
        except (OSError, ValueError) as exc:
            parser.error(f"--compare: {exc}")
    if config.profile:
        reason = py_spy_unavailable_reason()
        if reason is not None:
            parser.error(f"--profile: {reason}")

    activate_process_containment()
    try:
        run_dir, result = run_load(config)
    except KeyboardInterrupt:
        print("\nInterrupted; harness processes stopped.", file=sys.stderr)
        return EXIT_INTERRUPTED
    print()
    print(render_console(result))
    print()
    print(f"Report: {run_dir / 'report.md'}")
    if baseline is not None:
        print()
        print(compare_results(baseline, result))
    return EXIT_LEVEL_FAILED if failed_levels(result["levels"]) else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["compare"]:
        return run_compare(arguments[1:])
    return run(arguments)


if __name__ == "__main__":
    sys.exit(main())
