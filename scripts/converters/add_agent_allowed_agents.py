"""Add the required ``allowed_agents`` wildcard to pre-policy Agent configs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.utils.atomic import atomic_write_text  # noqa: E402


def convert(data_dir: Path) -> tuple[int, int]:
    """Convert valid JSON Agent configs below *data_dir* and return changed/skipped counts."""
    changed = 0
    skipped = 0
    agents_dir = data_dir.expanduser().resolve() / "agents"
    for config_path in sorted(agents_dir.glob("*/agent.json")):
        data: Any = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Agent config must contain an object: {config_path}")
        if "allowed_agents" in data:
            skipped += 1
            continue
        data["allowed_agents"] = ["*"]
        atomic_write_text(
            config_path,
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        )
        changed += 1
    return changed, skipped


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add allowed_agents=['*'] to Agent configs that predate target permissions."
    )
    parser.add_argument("data_dir", type=Path, help="vBot data directory containing agents/")
    args = parser.parse_args()
    changed, skipped = convert(args.data_dir)
    print(f"converted={changed} already_current={skipped}")


if __name__ == "__main__":
    main()
