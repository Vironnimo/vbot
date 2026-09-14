#!/usr/bin/env python
"""Manually replace retired search names without widening any capability policy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.projects.projects import PROJECT_DEFAULT_ALLOWED_TOOLS  # noqa: E402
from core.tools.availability import normalize_tool_access  # noqa: E402
from core.utils.atomic import atomic_write_text  # noqa: E402

RETIRED = {"grep", "glob"}


def convert_policy(value: dict, *, ceiling: list[str] | None = None) -> dict:
    policy = normalize_tool_access(value)
    names = set(policy.allowed) | set(policy.denied) | set(policy.granted)
    if not RETIRED.intersection(names):
        return value
    effective = {
        name
        for name in RETIRED
        if (policy.mode == "all" or name in policy.allowed) and name not in policy.denied
    }
    if policy.mode == "none":
        effective.clear()
    if effective and effective != RETIRED:
        raise ValueError(
            "Mixed grep/glob access requires an explicit choice: allow or deny search_files."
        )
    if (
        effective
        and ceiling is not None
        and not RETIRED.issubset(ceiling)
        and "search_files" not in ceiling
    ):
        raise ValueError("The Project ceiling does not permit both retired search capabilities.")
    result = policy.to_dict()
    for key in ("allowed", "denied", "granted"):
        if key in result:
            result[key] = [name for name in result[key] if name not in RETIRED]
    if effective and policy.mode == "selected":
        result["allowed"] = list(dict.fromkeys([*result["allowed"], "search_files"]))
    if not effective and (policy.mode == "all" or RETIRED.intersection(policy.denied)):
        result["denied"] = list(dict.fromkeys([*result.get("denied", []), "search_files"]))
    return normalize_tool_access(result).to_dict()


def convert_ceiling(value: list[str]) -> list[str]:
    if "*" in value:
        raise ValueError("A Project ceiling requires explicit Tool names, not '*'.")
    selected = RETIRED.intersection(value)
    if selected and selected != RETIRED and "search_files" not in value:
        raise ValueError(
            "A Project ceiling allowing only grep or glob requires an explicit search_files choice."
        )
    result = [name for name in value if name not in RETIRED]
    if selected == RETIRED:
        result.append("search_files")
    return list(dict.fromkeys(result))


def convert_search_access(data_dir: Path, *, apply: bool = False) -> list[str]:
    root = data_dir.expanduser().resolve()
    candidates: list[tuple[Path, bytes, dict]] = []
    files = [*root.glob("agents/*/agent.json"), *root.glob("projects/*/project.json")]
    for path in sorted(files):
        if any(p.is_symlink() for p in (path, *path.parents) if p != root.parent):
            raise ValueError(f"Refusing linked configuration path: {path}")
        original = path.read_bytes()
        payload = json.loads(original)
        if not isinstance(payload, dict):
            raise ValueError(f"Expected a configuration object: {path}")
        before = json.dumps(payload, sort_keys=True)
        if path.name == "agent.json":
            if "allowed_tools" in payload:
                raise ValueError(
                    f"Convert legacy allowed_tools first with agent_tool_access.py: {path}"
                )
            if "tool_access" in payload:
                payload["tool_access"] = convert_policy(payload["tool_access"])
        elif path.name == "project.json":
            ceiling = payload.get("allowed_tools", list(PROJECT_DEFAULT_ALLOWED_TOOLS))
            for override in payload.get("overrides", {}).values():
                if "tool_access" in override:
                    override["tool_access"] = convert_policy(
                        override["tool_access"], ceiling=ceiling
                    )
            if "allowed_tools" in payload:
                payload["allowed_tools"] = convert_ceiling(ceiling)
        if json.dumps(payload, sort_keys=True) != before:
            candidates.append((path, original, payload))
    # Complete preflight, including concurrent edits, before writing any file.
    for path, original, _ in candidates:
        if path.read_bytes() != original:
            raise ValueError(f"Configuration changed during preflight: {path}")
    if apply:
        for path, _, payload in candidates:
            atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return [str(path) for path, _, _ in candidates]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument(
        "--apply", action="store_true", help="Apply after complete preflight; default is dry-run."
    )
    args = parser.parse_args(argv)
    try:
        changes = convert_search_access(args.data_dir, apply=args.apply)
    except (OSError, ValueError) as error:
        print(f"search access conversion failed: {error}", file=sys.stderr)
        return 1
    for change in changes:
        print(change)
    print(f"{'Converted' if args.apply else 'Would convert'} {len(changes)} configuration files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
