#!/usr/bin/env python
"""Manually move OpenCode Go credentials to the shared OpenCode Account slots."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.providers.accounts import (  # noqa: E402
    account_id_from_credential_key,
    derive_credential_key,
)
from core.utils.atomic import atomic_write_bytes  # noqa: E402
from core.utils.config import parse_env_lines  # noqa: E402

OLD_KEY = "OPENCODE_GO_API_KEY"
SHARED_KEY = "OPENCODE_API_KEY"
ASSIGNMENT = re.compile(r"^(\s*)([^\s=#]+)(\s*=.*)$")


def convert_credentials(data_dir: Path, *, apply: bool = False) -> int:
    """Rename Go slots, refusing conflicting Zen slots before changing anything."""
    root = data_dir.expanduser().resolve()
    path = root / ".env"
    if path.is_symlink():
        raise ValueError("Refusing a linked environment file.")
    if not path.exists():
        return 0
    original = path.read_bytes()
    lines = original.decode("utf-8").splitlines(keepends=True)
    values = parse_env_lines(lines)
    renames: dict[str, str] = {}
    targets = dict(values)
    for key, value in values.items():
        if key != OLD_KEY and not key.startswith(f"{OLD_KEY}__"):
            continue
        account = account_id_from_credential_key(OLD_KEY, key)
        if account is None:
            raise ValueError("An OpenCode Go credential has an invalid Account suffix.")
        target = derive_credential_key(SHARED_KEY, account)
        if target in targets and targets[target] != value:
            raise ValueError(
                "OpenCode Go and Zen have different keys in the same Account slot. "
                "Assign distinct named Accounts before converting; no credentials were changed."
            )
        targets[target] = value
        renames[key] = target
    if not renames:
        return 0
    converted: list[str] = []
    for line in lines:
        match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if match and match[2] in renames:
            target = renames[match[2]]
            if target in values:
                continue
            start, end = match.span(2)
            line = line[:start] + target + line[end:]
        converted.append(line)
    if apply:
        if path.read_bytes() != original:
            raise ValueError("Environment file changed during preflight; nothing was written.")
        atomic_write_bytes(path, "".join(converted).encode("utf-8"), data_dir=root, mode=0o600)
    return len(renames)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument(
        "--apply", action="store_true", help="Write changes (default: preview only)"
    )
    args = parser.parse_args()
    try:
        count = convert_credentials(args.data_dir, apply=args.apply)
    except (OSError, UnicodeError, ValueError) as exc:
        # Exceptions above never include credential values.
        parser.exit(1, f"Conversion stopped: {exc}\n")
    print(f"OpenCode Account slots {'converted' if args.apply else 'to convert'}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
