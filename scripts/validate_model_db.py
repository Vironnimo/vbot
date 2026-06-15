#!/usr/bin/env python
"""Standalone Model DB validator — checks canonical pointers across the layers.

Run explicitly; NOT hooked into the runtime read path. It loads the canonical
layer and every provider/override layer under ``resources/models/`` and reports:

* **dead canonical pointers** — a ``canonical`` pointer (auto in ``<provider>.json``
  or manual in ``<provider>.overrides.json``) whose target is absent from the
  canonical layer (models.dev likely renamed the slug);
* **redundant manual joins** — a manual override pointer that equals the wire-id
  where the wire-id is itself a canonical key, so the deterministic exact-match
  auto-join already covers it.

Usage:
    python scripts/validate_model_db.py [--resources DIR]

Exit code 0 when clean, 1 when any finding is reported, 2 on a usage error. The
detection logic lives in ``core/models/validation.py`` and is unit-tested; this
script only renders findings and sets the exit code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Run standalone against THIS checkout's ``core`` even when an editable install
# points ``core`` at a different worktree: put the project root first on the path
# before importing it.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.models.validation import validate_model_db  # noqa: E402

DEFAULT_RESOURCES_DIR = PROJECT_ROOT / "resources"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the vBot Model DB canonical join")
    parser.add_argument(
        "--resources",
        type=Path,
        default=DEFAULT_RESOURCES_DIR,
        help="Resources directory containing the models/ layer files",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    resources_dir: Path = args.resources

    if not (resources_dir / "models").is_dir():
        print(f"validate-model-db..... ERROR: no models/ dir under {resources_dir}")
        return 2

    findings = validate_model_db(resources_dir)

    print("Model DB Validation")
    print("===================")
    if not findings:
        print("validate-model-db..... OK (no dead pointers, no redundant manual joins)")
        return 0

    for finding in findings:
        print(f"  [{finding.kind}] {finding.message}")
    print(f"validate-model-db..... {len(findings)} finding(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
