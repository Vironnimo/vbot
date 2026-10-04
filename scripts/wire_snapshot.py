#!/usr/bin/env python
"""Offline wire snapshot: what vBot's Provider Adapters put on the wire, for every Model.

``capture`` builds the production Provider Runtime in a temporary data
directory with fake credentials for every Connection, routes all HTTP through
an in-process mock transport and renders every chat-capable Model of the
bundled Model DB (plus one synthetic Custom Provider, a few synthetic local
Ollama Models and one unknown Model id per Connection) for every reasoning
effort in send and stream mode. Each chat request is recorded (method, URL,
masked headers, canonical JSON body) and then aborted. The snapshot also
records each Adapter's declarations, the catalog facts of non-chat Models and
how canned protocol responses normalize. Nothing reaches the network and no
real credential is read.

``compare`` diffs two snapshots and groups identical change patterns per
Provider. Use it to check that a refactoring leaves the wire unchanged, or to
review exactly what an intended change alters.

Usage:
    python scripts/wire_snapshot.py capture --out DIR [--provider ID ...]
    python scripts/wire_snapshot.py compare BASELINE NEW [--provider ID ...] [--max-groups N]

Exit codes: 0 on success (``compare``: identical), 1 when ``compare`` finds a
difference or ``capture`` saw a swallowed capture sentinel, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Run standalone against THIS checkout's ``core`` even when an editable install
# points ``core`` at a different worktree: put the project root first on the path
# before importing it.
if sys.path[:1] != [str(PROJECT_ROOT)]:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts._wire_snapshot.capture import capture_snapshot  # noqa: E402
from scripts._wire_snapshot.compare import compare_snapshots  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture", help="Capture a snapshot into a directory.")
    capture.add_argument("--out", type=Path, required=True, help="Snapshot directory to write.")
    capture.add_argument(
        "--provider",
        action="append",
        default=None,
        help="Capture only this Provider id (repeatable). Default: every Provider.",
    )

    compare = commands.add_parser("compare", help="Diff two snapshot directories.")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("new", type=Path)
    compare.add_argument(
        "--provider",
        action="append",
        default=None,
        help="Compare only this Provider id (repeatable). Default: every Provider.",
    )
    compare.add_argument(
        "--max-groups",
        type=int,
        default=20,
        help="Change patterns shown per Provider before the rest is summarized (default 20).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line; return the exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            return _capture(args.out, args.provider)
        return _compare(args.baseline, args.new, args.provider, args.max_groups)
    except ValueError as error:
        parser.error(str(error))


def _capture(out_dir: Path, providers: list[str] | None) -> int:
    result = capture_snapshot(out_dir, providers)
    for provider_id, counts in result.summary["providers"].items():
        records = ", ".join(f"{kind} {count}" for kind, count in counts["records"].items())
        print(f"{provider_id}: {records}")
    totals = ", ".join(f"{kind} {count}" for kind, count in result.summary["totals"].items())
    print(f"total: {totals}")
    print(f"wrote {out_dir} in {result.seconds:.1f}s")
    if result.swallowed:
        print(f"capture sentinel swallowed in {len(result.swallowed)} renders, e.g.:")
        for key in result.swallowed[:10]:
            print(f"  {key}")
        return 1
    return 0


def _compare(baseline: Path, new: Path, providers: list[str] | None, max_groups: int) -> int:
    different, lines = compare_snapshots(baseline, new, providers=providers, max_groups=max_groups)
    for line in lines:
        print(line)
    return 1 if different else 0


if __name__ == "__main__":
    sys.exit(main())
