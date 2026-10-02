#!/usr/bin/env python
"""Verify one Model's wire profile live and propose its verified profile entry.

Builds the production Provider Runtime of a data directory (its Settings,
``.env`` credentials and OAuth logins), resolves the Model's wire profile
WITHOUT any previously learned facts, and drives real requests through the
Adapter: a plain send and stream, one stream per reasoning rung plus ``none``,
optional sampling values, a Tool Call turn replayed into its continuation, and
an image when the Model takes images. Rejections the wire learns from are
retried and reported. The run prints measurements only (never prompts, keys or
full responses) and the Model entry the evidence supports; ``--write`` merges
that entry into ``resources/wire/<provider>.json``.

Usage:
    python scripts/verify_wire_profile.py --provider ID --model MODEL
        [--connection ID] [--data-dir DIR] [--check NAME ...] [--write]

Exit codes: 0 when every check passed or was skipped, 1 when a check warned or
failed, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Run standalone against THIS checkout's ``core`` even when an editable install
# points ``core`` at a different worktree.
if sys.path[:1] != [str(PROJECT_ROOT)]:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts._wire_verify.checks import CHECKS, CheckResult, run_checks  # noqa: E402
from scripts._wire_verify.environment import RESOURCES_DIR, open_target  # noqa: E402
from scripts._wire_verify.proposal import propose_entry, write_entry  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    parser.add_argument("--provider", required=True, help="Provider id.")
    parser.add_argument("--model", required=True, help="Model id as the Provider names it.")
    parser.add_argument("--connection", default=None, help="Local Connection id.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path.home() / ".vbot",
        help="Data directory with Settings, .env and OAuth logins (default: ~/.vbot).",
    )
    parser.add_argument(
        "--check",
        action="append",
        choices=CHECKS,
        default=None,
        help="Run only this check (repeatable). Default: every check.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Merge the proposed entry into resources/wire/<provider>.json.",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    target = open_target(args.data_dir.expanduser(), args.provider, args.model, args.connection)
    profile = target.adapter.wire_profile(target.model_id)
    print(
        f"{target.provider_id}:{target.connection_id} {target.model_id} "
        f"protocol={profile.protocol} status={profile.status} "
        f"dialect={profile.reasoning.dialect} ladder={list(profile.reasoning.ladder)}"
    )
    vision = bool(target.model and target.model.capabilities.vision)
    try:
        results = await run_checks(
            target.adapter, target.model_id, checks=args.check or CHECKS, vision=vision
        )
    finally:
        await target.adapter.aclose()
    for result in results:
        _print_result(result)

    facts = target.observations.facts_for(target.provider_id, target.connection_id, target.model_id)
    learned = target.adapter.wire_profile(target.model_id)
    entry = propose_entry(learned, facts, results, connection_id=target.connection_id)
    print("learned:", json.dumps(_facts_summary(facts), sort_keys=True))
    print("proposed entry:", json.dumps(entry, indent=2, sort_keys=True))
    if args.write and entry:
        path = write_entry(RESOURCES_DIR / "wire", target.provider_id, target.model_id, entry)
        print(f"wrote {path}")
    return 0 if all(result.status in ("ok", "skipped") for result in results) else 1


def _print_result(result: CheckResult) -> None:
    facts = " ".join(f"{key}={value}" for key, value in result.facts.items())
    detail = f" ({result.detail})" if result.detail else ""
    print(f"  {result.status:7} {result.name}{detail} {facts}".rstrip())


def _facts_summary(facts: object) -> dict[str, object]:
    return {key: value for key, value in vars(facts).items() if value not in (None, (), False)}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
