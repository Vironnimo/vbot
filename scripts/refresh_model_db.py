#!/usr/bin/env python
"""Refresh the tracked system Model DB through the development server.

Normal ``vbot model refresh`` writes the complete runtime Model DB under the
server's data directory. This maintainer-only entry point selects the explicit
system target so the complete release database is published in this checkout's
``resources/models/`` directory.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cli.model_management import model_refresh  # noqa: E402
from cli.server_management import resolve_instance  # noqa: E402

_WORKTREE_MARKER = PROJECT_ROOT / ".vbot-worktree"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh the tracked release Model DB through this checkout's server"
    )
    parser.add_argument(
        "provider",
        nargs="?",
        help="Refresh only this Provider; omitted means all refreshable Providers",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Development server host")
    parser.add_argument("--port", type=int, help="Development server port")
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Development data directory; defaults to this checkout's .vbot-worktree marker",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not _is_branch_checkout():
        print("refresh-model-db..... ERROR: this checkout is detached; use a branch checkout")
        return 2

    data_dir = args.data_dir or _worktree_data_dir()
    if data_dir is None:
        print(
            "refresh-model-db..... ERROR: no data directory; pass --data-dir or add .vbot-worktree"
        )
        return 2

    instance = resolve_instance(
        host=args.host,
        port=args.port,
        data_dir=data_dir,
    )
    result = model_refresh(
        instance,
        args.provider,
        target="system",
        expected_resources_dir=PROJECT_ROOT / "resources",
    )
    stream = sys.stdout if result.ok else sys.stderr
    print(result.message, file=stream)
    return 0 if result.ok else 1


def _is_branch_checkout() -> bool:
    completed = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        timeout=10,
    )
    return completed.returncode == 0


def _worktree_data_dir() -> Path | None:
    try:
        payload = json.loads(_WORKTREE_MARKER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw_data_dir = payload.get("data_dir") if isinstance(payload, dict) else None
    if not isinstance(raw_data_dir, str) or not raw_data_dir:
        return None
    return Path(raw_data_dir).expanduser()


if __name__ == "__main__":
    sys.exit(main())
