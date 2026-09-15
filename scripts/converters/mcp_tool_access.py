#!/usr/bin/env python
"""Explicit offline conversion from MCP Agent lists to ordinary Tool opt-ins.

Run against a stopped data directory before starting the updated application.
Dry-run is the default. Apply requires the target port and saves original files
and changed database rows before mutation. Repeating a partial conversion is safe;
the obsolete connection lists are removed last. Session Messages stay unchanged.
"""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.tools.availability import normalize_tool_access  # noqa: E402
from core.utils.atomic import atomic_write_text  # noqa: E402
from resources.extensions.mcp.config import validate_connection  # noqa: E402


def convert_policy(value: dict[str, Any], prior: set[str]) -> dict[str, Any]:
    policy = normalize_tool_access(value)
    if policy.mode == "none":
        return policy.to_dict()
    selected = {name for name in policy.allowed if name.startswith("mcp_")}
    enabled = (prior if policy.mode == "all" else selected) - set(policy.denied)
    result = policy.to_dict()
    grants = list(dict.fromkeys([*policy.granted, *sorted(enabled)]))
    if grants:
        result["granted"] = grants
    return normalize_tool_access(result).to_dict()


def _profile(value: dict[str, Any]) -> dict[str, Any]:
    if "tool_access" in value:
        value["tool_access"] = convert_policy(value["tool_access"], set())
    return value


def _require_stopped(port: int) -> None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            pass
    except ConnectionRefusedError:
        return
    raise ValueError(f"Stop the vBot server on port {port} before applying this conversion")


def convert_mcp_access(
    data_dir: Path, *, apply: bool = False, port: int | None = None
) -> list[str]:
    root = data_dir.expanduser().resolve(strict=True)
    if apply:
        if port is None:
            raise ValueError("Apply requires the exact target server port")
        _require_stopped(port)
    connection_path = root / "mcp/connections.json"
    if not connection_path.exists():
        return []
    original_connections = connection_path.read_text(encoding="utf-8")
    connections = json.loads(original_connections)
    if not isinstance(connections, list):
        raise ValueError("MCP connections must be an array")
    prior: dict[str, set[str]] = {}
    for connection in connections:
        for address in connection.pop("agents", []):
            prior.setdefault(address, set()).add(f"mcp_{connection['id']}")
        validate_connection(connection)
    files: list[tuple[Path, str, str]] = []
    for path in sorted([*root.glob("agents/*/agent.json"), *root.glob("projects/*/project.json")]):
        original = path.read_text(encoding="utf-8")
        value = json.loads(original)
        if path.name == "agent.json":
            policy = value.get("tool_access", {"mode": "all"})
            converted = convert_policy(policy, prior.get(value["id"], set()))
            if converted != policy:
                value["tool_access"] = converted
        else:
            project_id = value["project_id"]
            for agent_id, override in value.get("overrides", {}).items():
                if "tool_access" in override:
                    override["tool_access"] = convert_policy(
                        override["tool_access"], prior.get(f"{agent_id}@{project_id}", set())
                    )
            # Do not synthesize overrides from a ceiling: repository denials must
            # remain authoritative. Such Agents can opt in through their editor.
        if value != json.loads(original):
            files.append((path, original, json.dumps(value, ensure_ascii=False, indent=2) + "\n"))

    # Each location contains configuration, never historical Message content.
    locations = [
        (root / "extension-data/swarm/swarm.db", "profiles", "id", "payload"),
        (root / "extension-data/swarm/swarm.db", "swarms", "id", "profile_snapshot"),
        (root / "sessions.db", "temporary_session_bindings", "session_key", "config_json"),
    ]
    updates: dict[Path, list[tuple[str, str, str, Any, str, str]]] = {}
    for path, table, key, column in locations:
        if not path.exists():
            continue
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            for identity, original in db.execute(f"SELECT {key}, {column} FROM {table}"):
                value = json.loads(original)
                converted = _profile(json.loads(original))
                if converted != value:
                    updates.setdefault(path, []).append(
                        (
                            table,
                            key,
                            column,
                            identity,
                            original,
                            json.dumps(converted, ensure_ascii=False),
                        )
                    )
    if connections != json.loads(original_connections):
        files.append(
            (
                connection_path,
                original_connections,
                json.dumps(connections, ensure_ascii=False, indent=2) + "\n",
            )
        )
    changes = [str(path.relative_to(root)) for path, _, _ in files]
    changes += [
        f"{path.relative_to(root)}: {len(rows)} configuration rows"
        for path, rows in updates.items()
    ]
    if not apply or not changes:
        return changes
    assert port is not None
    _require_stopped(port)
    for path, original, _ in files:
        if path.read_text(encoding="utf-8") != original:
            raise ValueError(f"Configuration changed during preflight: {path}")
    backup = (
        root / "backups" / ("mcp-tool-access-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    )
    backup.mkdir(parents=True, exist_ok=False)
    for path, original, _ in files:
        target = backup / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(original, encoding="utf-8")
    (backup / "database-rows.json").write_text(
        json.dumps(
            {str(path.relative_to(root)): rows for path, rows in updates.items()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for path, rows in updates.items():
        _require_stopped(port)
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for table, key, column, identity, original, converted_text in rows:
                result = db.execute(
                    f"UPDATE {table} SET {column}=? WHERE {key}=? AND {column}=?",
                    (converted_text, identity, original),
                )
                if result.rowcount != 1:
                    raise ValueError(f"Configuration changed during preflight: {path}, {table}")
    for path, original, converted_text in files:
        _require_stopped(port)
        if path.read_text(encoding="utf-8") != original:
            raise ValueError(f"Configuration changed during conversion: {path}")
        atomic_write_text(path, converted_text)
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--port", type=int, help="Exact stopped vBot server port; required for --apply"
    )
    args = parser.parse_args()
    try:
        changes = convert_mcp_access(args.data_dir, apply=args.apply, port=args.port)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"MCP Tool access conversion failed: {error}", file=sys.stderr)
        return 1
    for change in changes:
        print(change)
    print(f"{'Converted' if args.apply else 'Would convert'} {len(changes)} locations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
