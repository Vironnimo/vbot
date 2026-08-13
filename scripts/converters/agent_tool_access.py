#!/usr/bin/env python
"""Convert legacy Identity Agent ``allowed_tools`` to explicit ``tool_access``."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.tools.availability import ToolAccess, normalize_tool_access  # noqa: E402
from core.utils.atomic import atomic_write_text  # noqa: E402

JsonObject = dict[str, Any]
_LEGACY_RUNTIME_DERIVED_TOOLS = frozenset({"history", "memory", "session_read"})


class AgentToolAccessConversionError(Exception):
    """Raised when legacy Agent data cannot be converted without guessing."""


@dataclass(frozen=True)
class AgentToolAccessConversionResult:
    planned: int
    converted: int
    already_converted: int
    changes: tuple[str, ...]


@dataclass(frozen=True)
class _ConversionCandidate:
    path: Path
    payload: JsonObject
    policy: ToolAccess
    legacy: bool


def convert_agent_tool_access(
    data_dir: Path,
    *,
    apply: bool = False,
    remove_tools: tuple[str, ...] = (),
) -> AgentToolAccessConversionResult:
    """Preflight and optionally convert every Identity Agent in one data directory."""

    retired_tools = frozenset(name.strip() for name in remove_tools if name.strip())
    agents_dir = data_dir.expanduser().resolve() / "agents"
    if not agents_dir.exists():
        return AgentToolAccessConversionResult(0, 0, 0, ())
    if agents_dir.is_symlink() or not agents_dir.is_dir():
        raise AgentToolAccessConversionError(
            f"Agent path must be a real directory, not a symlink or file: {agents_dir}"
        )

    candidates: list[_ConversionCandidate] = []
    already_converted = 0
    for agent_dir in sorted(agents_dir.iterdir(), key=lambda path: path.name):
        if agent_dir.name == "order.json":
            continue
        if agent_dir.is_symlink():
            raise AgentToolAccessConversionError(
                f"Refusing to traverse symlinked Agent directory: {agent_dir}"
            )
        if not agent_dir.is_dir():
            continue
        agent_path = agent_dir / "agent.json"
        if agent_path.is_symlink():
            raise AgentToolAccessConversionError(
                f"Refusing to rewrite symlinked Agent file: {agent_path}"
            )
        if not agent_path.is_file():
            raise AgentToolAccessConversionError(f"Agent file is missing: {agent_path}")
        payload = _load_agent(agent_path)
        has_legacy = "allowed_tools" in payload
        has_current = "tool_access" in payload
        if has_legacy and has_current:
            raise AgentToolAccessConversionError(
                f"Agent contains both allowed_tools and tool_access: {agent_path}"
            )
        if has_current:
            try:
                policy = normalize_tool_access(payload["tool_access"])
            except ValueError as exc:
                raise AgentToolAccessConversionError(
                    f"Invalid tool_access in {agent_path}: {exc}"
                ) from exc
            cleaned_policy = _remove_policy_tools(policy, retired_tools)
            if cleaned_policy != policy:
                candidates.append(
                    _ConversionCandidate(agent_path, payload, cleaned_policy, legacy=False)
                )
                continue
            already_converted += 1
            continue

        policy = _convert_legacy_policy(
            payload.get("allowed_tools"),
            agent_path,
            retired_tools=retired_tools,
        )
        candidates.append(_ConversionCandidate(agent_path, payload, policy, legacy=True))

    changes = tuple(
        f"{candidate.path}: {json.dumps(candidate.policy.to_dict())}" for candidate in candidates
    )
    if apply:
        for candidate in candidates:
            converted = _replace_policy_field(
                candidate.payload,
                candidate.policy,
                legacy=candidate.legacy,
            )
            try:
                atomic_write_text(
                    candidate.path,
                    json.dumps(converted, ensure_ascii=False, indent=2) + "\n",
                )
            except OSError as exc:
                raise AgentToolAccessConversionError(
                    f"Cannot update Agent file {candidate.path}: {exc}"
                ) from exc

    return AgentToolAccessConversionResult(
        planned=len(candidates),
        converted=len(candidates) if apply else 0,
        already_converted=already_converted,
        changes=changes,
    )


def _load_agent(path: Path) -> JsonObject:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AgentToolAccessConversionError(f"Cannot read Agent file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AgentToolAccessConversionError(f"Invalid Agent JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise AgentToolAccessConversionError(f"Agent file must contain an object: {path}")
    return payload


def _convert_legacy_policy(
    value: Any,
    path: Path,
    *,
    retired_tools: frozenset[str] = frozenset(),
) -> ToolAccess:
    if value is None:
        return normalize_tool_access({"mode": "all"})
    if not isinstance(value, list) or not all(
        isinstance(item, str) and bool(item.strip()) for item in value
    ):
        raise AgentToolAccessConversionError(
            f"allowed_tools must be a list of non-empty strings: {path}"
        )
    if "*" in value:
        if value != ["*"]:
            raise AgentToolAccessConversionError(
                f"allowed_tools mixes '*' with explicit names: {path}"
            )
        return normalize_tool_access({"mode": "all"})

    allowed = list(
        dict.fromkeys(
            name
            for name in value
            if name not in _LEGACY_RUNTIME_DERIVED_TOOLS and name not in retired_tools
        )
    )
    return normalize_tool_access({"mode": "selected", "allowed": allowed})


def _remove_policy_tools(policy: ToolAccess, retired_tools: frozenset[str]) -> ToolAccess:
    if not retired_tools:
        return policy
    return ToolAccess(
        mode=policy.mode,
        allowed=tuple(name for name in policy.allowed if name not in retired_tools),
        denied=tuple(name for name in policy.denied if name not in retired_tools),
    )


def _replace_policy_field(
    payload: JsonObject,
    policy: ToolAccess,
    *,
    legacy: bool,
) -> JsonObject:
    """Write the explicit policy in place while preserving surrounding key order."""

    converted: JsonObject = {}
    inserted = False
    source_field = "allowed_tools" if legacy else "tool_access"
    for key, value in payload.items():
        if key == source_field:
            converted["tool_access"] = policy.to_dict()
            inserted = True
        else:
            converted[key] = value
    if not inserted:
        converted["tool_access"] = policy.to_dict()
    return converted


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight and convert Identity Agent allowed_tools fields to explicit "
            "tool_access policies. Dry-run is the default."
        )
    )
    parser.add_argument("data_dir", type=Path, help="Explicit vBot data directory")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the preflighted conversions atomically",
    )
    parser.add_argument(
        "--remove-tool",
        action="append",
        default=[],
        metavar="NAME",
        help="Remove one retired Tool name from explicit allow and deny lists",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = convert_agent_tool_access(
            args.data_dir,
            apply=args.apply,
            remove_tools=tuple(args.remove_tool),
        )
    except AgentToolAccessConversionError as exc:
        print(f"agent-tool-access.............. ERROR: {exc}", file=sys.stderr)
        return 1

    state = "applied" if args.apply else "dry-run"
    print(
        "agent-tool-access.............. "
        f"{state} planned={result.planned} converted={result.converted} "
        f"already_converted={result.already_converted}"
    )
    for change in result.changes:
        print(f"  {change}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
