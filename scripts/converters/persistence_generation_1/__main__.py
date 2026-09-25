"""Convert a stopped vBot data directory to persistence Generation 1.

Run from the vBot checkout::

    python -m scripts.converters.persistence_generation_1 <data-dir> --dry-run
    python -m scripts.converters.persistence_generation_1 <data-dir>

``--dry-run`` converts and verifies into a staging directory, reports and
discards it; the data directory keeps its content. Without it, the verified
result is installed and every replaced file moves to ``pre-generation-1/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scripts.converters.persistence_generation_1._install import (
    REPORT_FILE_NAME,
    InstallError,
)
from scripts.converters.persistence_generation_1._preflight import (
    BACKUP_DIRECTORY,
    RefusedError,
    format_bytes,
)
from scripts.converters.persistence_generation_1.conversion import (
    ConversionFailedError,
    convert_data_directory,
)

# How many skipped or approximated items the summary shows per area.
_EXAMPLES_PER_AREA = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.converters.persistence_generation_1",
        description=(
            "Convert a stopped vBot data directory to persistence Generation 1. Stop vBot "
            "and back up the data directory first."
        ),
    )
    parser.add_argument("data_dir", type=Path, help="the vBot data directory, e.g. ~/.vbot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="convert and verify, then discard the result; the data directory keeps its content",
    )
    parser.add_argument("--report", type=Path, help="also write the JSON report to this file")
    args = parser.parse_args(argv)
    # Reported names come from user data; a console without their characters
    # must not fail after the conversion finished.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")

    try:
        report = convert_data_directory(args.data_dir, dry_run=args.dry_run)
    except RefusedError as error:
        print(f"Refused, nothing changed: {error}", file=sys.stderr)
        return 1
    except ConversionFailedError as error:
        print(f"Conversion failed, nothing changed: {error}", file=sys.stderr)
        return 1
    except InstallError as error:
        print(f"Install interrupted: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted. An install that had started finishes when the same command runs "
            "again; before the install, nothing changed.",
            file=sys.stderr,
        )
        return 130
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(summary(report, report_path=args.report))
    return 0


def summary(report: dict[str, Any], *, report_path: Path | None = None) -> str:
    """A short human summary of a conversion report."""
    data_dir = Path(report["data_directory"])
    installed = report["result"] == "installed"
    lines = [f"vBot persistence Generation 1 conversion of {data_dir}"]
    if installed:
        lines.append(
            "Result: installed"
            + (" (finished an interrupted install)" if report.get("resumed") else "")
        )
    else:
        lines.append("Result: verified; dry run, the data directory keeps its content")
    lines.append("")
    lines.append("Converted:")
    for area, counts in report["areas"].items():
        values = ", ".join(f"{key} {value}" for key, value in counts.items() if value)
        lines.append(f"  {area:<15} {values or 'no data'}")
    lines.append("")
    lines.extend(_verification_lines(report["verification"]))
    skipped = report["skipped"]
    if skipped:
        by_area = ", ".join(f"{area} {count}" for area, count in report["skipped_by_area"].items())
        lines.append(f"Skipped or approximated: {len(skipped)} ({by_area})")
        shown: dict[str, int] = {}
        for item in skipped:
            if shown.get(item["area"], 0) < _EXAMPLES_PER_AREA:
                shown[item["area"]] = shown.get(item["area"], 0) + 1
                lines.append(f"  [{item['area']}] {item['item']}: {item['reason']}")
    sizes = report.get("sizes", {})
    if sizes:
        lines.append(
            f"Sizes: {format_bytes(sizes['moved_aside_bytes'])} replaced or retired, "
            f"{format_bytes(sizes['installed_bytes'])} installed"
        )
    lines.append("")
    if installed:
        backup = data_dir / BACKUP_DIRECTORY
        lines.append(f"Replaced and retired files: {backup}")
        lines.append(f"Report: {backup / REPORT_FILE_NAME}")
    if report_path is not None:
        lines.append(f"Report: {report_path}")
    elif not installed:
        lines.append("The full list of skipped items is in the JSON report (--report <file>).")
    return "\n".join(lines)


def _verification_lines(verification: dict[str, Any]) -> list[str]:
    databases = verification["databases"]
    documents = verification["json_documents"]
    lines = [
        f"Verified: {len(databases)} databases ({', '.join(sorted(databases))}), "
        f"{documents['documents_validated']} JSON documents"
    ]
    sessions = verification.get("sessions")
    if sessions is not None:
        lines.append(
            f"  Sessions: {sessions['sessions_compared']} compared with their source history "
            f"({sessions['view_entries_compared']} entries), "
            f"{sessions['explained_differences']} differences explained by skipped items"
        )
        for loaded in sessions["loaded_through_the_application"]:
            lines.append(f"  Loaded: {loaded}")
    if documents["documents_with_errors"]:
        lines.append(
            f"  {documents['documents_with_errors']} JSON documents have errors vBot will "
            "report; they were carried over unchanged:"
        )
        for error in documents["errors"][:5]:
            lines.append(f"    {error['file']} {error['path']}: {error['message']}")
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
