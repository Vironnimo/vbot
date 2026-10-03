r"""Learning evaluation reports, text-pack export and A/B comparison.

Run an arm with the Provider probe, export packs and compare arms here::

    python -m scripts.provider_probe.learning_eval export-pack DIR
    python scripts/probe_provider_tool_call.py --scenario reflection_workflow \
        --text-pack DIR --repetitions 3 --reflection-report arm-a.json
    python -m scripts.provider_probe.learning_eval compare arm-a.json arm-b.json

Run the module from the repository root. Reports keep every attempt, failed
and crashed ones included, with its transcript; comparison only computes a
pass-rate delta where both arms ran the same number of attempts.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.provider_probe.learning_texts import AppliedTexts, TextPack, checkout_commit

REPORT_FORMAT = 1


def _sha(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def pass_rates(attempts: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return pass counts per (case, scope); a crashed attempt counts as a failure."""
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for attempt in attempts:
        key = (str(attempt["case"]), str(attempt["scope"]))
        cell = cells.setdefault(
            key,
            {
                "case": key[0],
                "scope": key[1],
                "attempts": 0,
                "passed": 0,
                "effect_passed": 0,
                "crashed": 0,
            },
        )
        cell["attempts"] += 1
        cell["passed"] += bool(attempt.get("passed"))
        cell["effect_passed"] += bool(attempt.get("effect_passed"))
        cell["crashed"] += attempt.get("error") is not None
    rows = [cells[key] for key in sorted(cells)]
    for row in rows:
        row["rate"] = round(row["passed"] / row["attempts"], 4)
    return rows


def summarize(attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return overall counts, violation kinds and token totals of a run."""
    usage: dict[str, float] = {}
    for attempt in attempts:
        for key, value in (attempt.get("usage") or {}).items():
            if isinstance(value, int | float) and not isinstance(value, bool):
                usage[key] = usage.get(key, 0) + value
    count = len(attempts)
    passed = sum(bool(attempt.get("passed")) for attempt in attempts)
    return {
        "attempts": count,
        "passed": passed,
        "rate": round(passed / count, 4) if count else None,
        "effect_passed": sum(bool(attempt.get("effect_passed")) for attempt in attempts),
        "crashed": sum(attempt.get("error") is not None for attempt in attempts),
        "unfinished": sum(
            not attempt.get("finished") and attempt.get("error") is None for attempt in attempts
        ),
        "violations": dict(
            Counter(kind for attempt in attempts for kind in attempt.get("violations") or [])
        ),
        "usage": usage,
        "mean_steps": round(sum(attempt.get("steps", 0) for attempt in attempts) / count, 2)
        if count
        else None,
    }


class ReflectionReport:
    """A run's report, rewritten after every attempt so an interrupted run keeps its data."""

    def __init__(self, path: Path | None, run: dict[str, Any]) -> None:
        self._path = path
        self._run = run
        self._attempts: list[dict[str, Any]] = []
        self._system_prompts: dict[str, str] = {}
        self._definitions: dict[str, Any] = {}

    @classmethod
    def start(
        cls,
        args: argparse.Namespace,
        *,
        applied: AppliedTexts,
        pack: TextPack | None,
        selected: list[tuple[str, str]],
        notes: dict[str, str],
    ) -> ReflectionReport:
        from scripts.provider_probe.learning_fixture import TOOL_ROUTE_NOTE

        run = {
            "provider": args.provider,
            "connection": args.connection,
            "model": args.model,
            "thinking_effort": args.thinking_effort,
            "max_tokens": args.max_tokens,
            "repetitions": args.repetitions,
            "workers": args.reflection_workers,
            "pairs": [{"case": case, "scope": scope} for case, scope in selected],
            "case_notes": notes,
            "commit": checkout_commit(),
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "finished_at": None,
            "text_pack": None
            if pack is None
            else {
                "path": str(pack.path),
                "manifest": pack.manifest,
                "changed": applied.changed,
                "warnings": applied.warnings,
            },
            "texts": applied.texts.digests(),
            "tool_route": TOOL_ROUTE_NOTE,
        }
        report = cls(args.reflection_report, run)
        report._write()
        return report

    def add(self, attempt: dict[str, Any]) -> None:
        attempt = dict(attempt)
        system_prompt = attempt.pop("system_prompt", "")
        definitions = attempt.pop("definitions", [])
        if system_prompt:
            attempt["system_prompt_sha"] = _sha(system_prompt)
            self._system_prompts[attempt["system_prompt_sha"]] = system_prompt
        if definitions:
            attempt["definitions_sha"] = _sha(definitions)
            self._definitions[attempt["definitions_sha"]] = definitions
        self._attempts.append(attempt)
        self._write()

    def finish(self) -> None:
        self._run["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        self._write()

    def document(self) -> dict[str, Any]:
        attempts = sorted(
            self._attempts,
            key=lambda attempt: (attempt["case"], attempt["scope"], attempt["repetition"]),
        )
        return {
            "format": REPORT_FORMAT,
            "kind": "learning_evaluation",
            "run": self._run,
            "summary": summarize(attempts),
            "pass_rates": pass_rates(attempts),
            "attempts": attempts,
            "system_prompts": self._system_prompts,
            "definitions": self._definitions,
        }

    def _write(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        temporary.write_text(
            json.dumps(self.document(), indent=1, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, self._path)

    def result(self) -> dict[str, Any]:
        """Return the compact probe result: no transcripts, which stay in the report."""
        document = self.document()
        return {
            "scenario": "reflection_workflow",
            "provider": self._run["provider"],
            "model": self._run["model"],
            "repetitions": self._run["repetitions"],
            "text_pack": (self._run["text_pack"] or {}).get("path"),
            "report": None if self._path is None else str(self._path),
            "summary": document["summary"],
            "pass_rates": document["pass_rates"],
            "attempts": [
                {
                    key: attempt.get(key)
                    for key in (
                        "case",
                        "scope",
                        "repetition",
                        "passed",
                        "effect_passed",
                        "finished",
                        "stopped_reason",
                        "violations",
                        "steps",
                        "error",
                    )
                }
                for attempt in document["attempts"]
            ],
            "passed": bool(document["attempts"])
            and all(attempt.get("passed") for attempt in document["attempts"]),
        }


def format_pass_rate_table(document: Mapping[str, Any]) -> str:
    """Render a report's pass rates and summary for a terminal."""
    lines = [f"{'case':<28} {'scope':<9} {'passed':>7} {'effect':>7} {'crashed':>8}"]
    for row in document["pass_rates"]:
        lines.append(
            f"{row['case']:<28} {row['scope']:<9} "
            f"{row['passed']:>3}/{row['attempts']:<3} "
            f"{row['effect_passed']:>3}/{row['attempts']:<3} {row['crashed']:>8}"
        )
    summary = document["summary"]
    rate = summary["rate"]
    lines.append(
        f"overall: {summary['passed']}/{summary['attempts']} passed"
        + (f" ({rate:.1%})" if rate is not None else "")
        + f", {summary['crashed']} crashed, {summary['unfinished']} unfinished"
    )
    if summary["violations"]:
        lines.append(f"violations: {summary['violations']}")
    return "\n".join(lines)


def compare_reports(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two reports per (case, scope), counting crashed attempts as failures.

    A delta needs equal attempt counts in both arms; other cells are reported
    as not comparable and stay out of the overall delta.
    """
    rates_a = {(row["case"], row["scope"]): row for row in pass_rates(first["attempts"])}
    rates_b = {(row["case"], row["scope"]): row for row in pass_rates(second["attempts"])}
    rows: list[dict[str, Any]] = []
    totals = {"attempts": 0, "a_passed": 0, "b_passed": 0}
    for key in sorted(rates_a.keys() | rates_b.keys()):
        a, b = rates_a.get(key), rates_b.get(key)
        row: dict[str, Any] = {
            "case": key[0],
            "scope": key[1],
            "a": None if a is None else f"{a['passed']}/{a['attempts']}",
            "b": None if b is None else f"{b['passed']}/{b['attempts']}",
            "delta": None,
        }
        if a is None or b is None:
            row["status"] = "missing in " + ("A" if a is None else "B")
        elif a["attempts"] != b["attempts"]:
            row["status"] = f"not comparable ({a['attempts']} vs {b['attempts']} attempts)"
        else:
            row["status"] = "ok"
            row["delta"] = round(b["rate"] - a["rate"], 4)
            totals["attempts"] += a["attempts"]
            totals["a_passed"] += a["passed"]
            totals["b_passed"] += b["passed"]
        rows.append(row)
    texts_a, texts_b = first["run"].get("texts", {}), second["run"].get("texts", {})
    overall = None
    if totals["attempts"]:
        overall = {
            "attempts_per_arm": totals["attempts"],
            "a_passed": totals["a_passed"],
            "b_passed": totals["b_passed"],
            "delta": round((totals["b_passed"] - totals["a_passed"]) / totals["attempts"], 4),
        }
    return {
        "a": _arm(first),
        "b": _arm(second),
        "texts_differing": sorted(
            text_id
            for text_id in texts_a.keys() | texts_b.keys()
            if texts_a.get(text_id) != texts_b.get(text_id)
        ),
        "cells": rows,
        "overall": overall,
    }


def _arm(document: Mapping[str, Any]) -> dict[str, Any]:
    run = document["run"]
    summary = summarize(document["attempts"])
    return {
        "model": f"{run.get('provider')}/{run.get('model')}",
        "text_pack": (run.get("text_pack") or {}).get("path"),
        "commit": run.get("commit"),
        "attempts": summary["attempts"],
        "passed": summary["passed"],
        "crashed": summary["crashed"],
        "violations": summary["violations"],
        "usage": summary["usage"],
    }


def format_comparison(comparison: Mapping[str, Any]) -> str:
    lines = [
        f"A: {comparison['a']['model']} pack={comparison['a']['text_pack']} "
        f"({comparison['a']['passed']}/{comparison['a']['attempts']} passed)",
        f"B: {comparison['b']['model']} pack={comparison['b']['text_pack']} "
        f"({comparison['b']['passed']}/{comparison['b']['attempts']} passed)",
        f"texts differing: {', '.join(comparison['texts_differing']) or 'none'}",
        f"{'case':<28} {'scope':<9} {'A':>7} {'B':>7} {'delta':>8}  status",
    ]
    for row in comparison["cells"]:
        delta = "" if row["delta"] is None else f"{row['delta'] * 100:+.0f}pp"
        lines.append(
            f"{row['case']:<28} {row['scope']:<9} {row['a'] or '-':>7} {row['b'] or '-':>7} "
            f"{delta:>8}  {row['status']}"
        )
    overall = comparison["overall"]
    if overall is None:
        lines.append("overall: no comparable cells")
    else:
        lines.append(
            "overall over comparable cells: "
            f"A {overall['a_passed']}/{overall['attempts_per_arm']}, "
            f"B {overall['b_passed']}/{overall['attempts_per_arm']}, "
            f"delta {overall['delta'] * 100:+.1f}pp"
        )
    for arm in ("a", "b"):
        violations = comparison[arm]["violations"]
        if violations:
            lines.append(f"{arm.upper()} violations: {violations}")
    return "\n".join(lines)


async def export_text_pack(directory: Path) -> Path:
    """Write this checkout's current learning texts as a text pack."""
    from scripts.provider_probe.learning_fixture import EvalWorker
    from scripts.provider_probe.learning_texts import write_text_pack

    worker = EvalWorker(agent_model=None, pack=None)
    try:
        worker.start()
        assert worker.applied is not None
        texts = worker.applied.texts
    finally:
        await worker.aclose()
    return write_text_pack(texts, directory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.provider_probe.learning_eval",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export-pack", help="Write the current texts as a text pack.")
    export.add_argument("directory", type=Path)
    compare = commands.add_parser("compare", help="Compare two learning evaluation reports.")
    compare.add_argument("a", type=Path)
    compare.add_argument("b", type=Path)
    compare.add_argument("--json", action="store_true", help="Print the comparison as JSON.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "export-pack":
        path = asyncio.run(export_text_pack(args.directory))
        print(f"Exported text pack to {path}")
        return 0
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in (args.a, args.b)]
    for path, document in zip((args.a, args.b), documents, strict=True):
        if document.get("kind") != "learning_evaluation":
            print(f"{path} is not a learning evaluation report", file=sys.stderr)
            return 2
    comparison = compare_reports(*documents)
    print(json.dumps(comparison, indent=2) if args.json else format_comparison(comparison))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
