"""Validate a stable release changelog, render GitHub notes, or build the update asset."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.utils import changelog


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check").add_argument("version")
    notes = commands.add_parser("notes")
    notes.add_argument("version")
    notes.add_argument("--previous", required=True)
    commands.add_parser("asset").add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "asset":
            releases = changelog.bundled(limit=changelog.MAX_RELEASES)
            data = json.dumps(
                {"schema_version": 1, "releases": releases},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(data) > changelog.MAX_BYTES:
                raise ValueError("Changelog asset exceeds 256 KiB")
            args.out.write_bytes(data)
        else:
            changelog.read_release(changelog.DIRECTORY, args.version, validate=True)
            if args.command == "notes":
                if not args.previous.startswith("v"):
                    raise ValueError("Expected the previous release tag vX.Y.Z")
                changelog.version_key(args.previous[1:])
                source = changelog.DIRECTORY / f"{args.version}.md"
                print(source.read_text(encoding="utf-8").rstrip())
                print(
                    f"\n**Full Changelog**: https://github.com/Vironnimo/vbot/compare/{args.previous}...v{args.version}"
                )
    except (ValueError, OSError) as error:
        print(f"Release changelog: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
