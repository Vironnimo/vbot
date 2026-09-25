"""Command line of the Tool lab; see the package docstring for the commands."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from scripts.tool_lab import definitions, probe, sessions
from scripts.tool_lab._lab_runtime import DEFAULT_AGENT_ID, lab_runtime, utf8_console


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.tool_lab",
        description="Review vBot Tools the way an Agent meets them.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    shown = commands.add_parser(
        "definitions", help="System Prompt blocks and Tool definitions a fresh Agent receives"
    )
    shown.add_argument("--agent", default=DEFAULT_AGENT_ID, help="Agent id (default: main)")
    shown.add_argument(
        "--all", action="store_true", help="also list Tools not offered to the Agent by default"
    )
    shown.add_argument(
        "--show",
        action="append",
        default=[],
        metavar="NAME",
        help="print one block or Tool in full (repeatable)",
    )

    probed = commands.add_parser(
        "probe", help="dispatch the calls of a case file through the production Tool path"
    )
    probed.add_argument("cases", type=Path, help="case file (JSON), see scripts/tool_lab/probe.py")
    probed.add_argument("--agent", default=DEFAULT_AGENT_ID, help="Agent id (default: main)")
    probed.add_argument(
        "--case", action="append", default=[], help="run cases whose name contains this"
    )
    probed.add_argument("--max", type=int, default=3000, help="clip printed texts (0 = no limit)")
    probed.add_argument(
        "--visible", action="store_true", help="show tabs and carriage returns as \\t and \\r"
    )

    mined = commands.add_parser(
        "sessions", help="measure real Tool calls in a copy of a sessions database"
    )
    mined.add_argument("source", type=Path, help="a sessions.db or a vBot data directory")
    mined.add_argument("--work", type=Path, help="keep the prepared copy here and reuse it")
    mined.add_argument("--tool", help="detailed view of one Tool")
    mined.add_argument(
        "--by-arg", metavar="KEY", help="with --tool: group by the first word of KEY"
    )
    mined.add_argument("--since", help="only calls at or after this ISO date")
    mined.add_argument("--until", help="only calls before this ISO date")
    mined.add_argument("--model", help="only calls whose Model contains this text")
    mined.add_argument("--agent", help="only calls of this Agent id")
    mined.add_argument("--examples", type=int, default=3, help="example calls per failure shape")
    mined.add_argument("--export", type=Path, metavar="FILE", help="write the calls as JSON lines")

    args = parser.parse_args(argv)
    utf8_console()
    if args.command == "definitions":
        return _definitions(args)
    if args.command == "probe":
        return _probe(args)
    return _sessions(args)


def _definitions(args: argparse.Namespace) -> int:
    view = asyncio.run(definitions.collect(args.agent, include_all=args.all))
    if not args.show:
        print(definitions.summary(view))
    for name in args.show:
        print(definitions.show(view, name))
        print()
    return 0


def _probe(args: argparse.Namespace) -> int:
    try:
        files, cases = probe.load_cases(args.cases)
        if args.case:
            cases = [case for case in cases if any(part in case.name for part in args.case)]
        outcomes = asyncio.run(probe.run_cases(files, cases, agent_id=args.agent))
    except probe.CaseFileError as error:
        print(f"Case file error: {error}", file=sys.stderr)
        return 2
    print(probe.report(outcomes, max_chars=args.max, visible=args.visible))
    return 1 if any(outcome.failures for outcome in outcomes) else 0


def _sessions(args: argparse.Namespace) -> int:
    filters = sessions.Filters(
        since=args.since, until=args.until, model=args.model, agent=args.agent
    )
    try:
        with sessions.prepared_database(args.source, work=args.work) as database:
            records = [record for record in sessions.load_calls(database) if filters.admit(record)]
    except sessions.SessionsSourceError as error:
        print(f"Cannot read sessions: {error}", file=sys.stderr)
        return 2
    if args.export is not None:
        count = sessions.export(records, args.export)
        print(f"Wrote {count} calls to {args.export}")
    if args.tool:
        schema_keys = asyncio.run(_schema_keys(args.tool))
        print(
            sessions.tool_view(
                records,
                args.tool,
                schema_keys=schema_keys,
                by_argument=args.by_arg,
                examples=args.examples,
            )
        )
    else:
        print(sessions.overview(records))
    return 0


async def _schema_keys(tool: str) -> frozenset[str] | None:
    """Top-level parameter names of the Tool's current definition, if it still exists."""
    async with lab_runtime() as (runtime, _root):
        _offered, found = definitions.tool_definitions(runtime, DEFAULT_AGENT_ID, include_all=True)
    definition = found.get(tool)
    if definition is None:
        return None
    parameters = definition.get("parameters") or {}
    keys = set((parameters.get("properties") or {}).keys())
    for keyword in ("oneOf", "anyOf"):
        for branch in parameters.get(keyword) or ():
            keys.update((branch.get("properties") or {}).keys())
    return frozenset(keys)


if __name__ == "__main__":
    raise SystemExit(main())
